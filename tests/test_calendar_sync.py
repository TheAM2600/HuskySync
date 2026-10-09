from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import calendar_sync
from app.database import create_session_factory
from app.models import Assignment, AssignmentOrigin, AssignmentStatus


@pytest.fixture
def database(tmp_path):
    return create_session_factory(tmp_path / "calendar-test.sqlite3")


@pytest.fixture
def service():
    client = Mock()
    client.events.return_value.insert.side_effect = lambda **kwargs: Mock(
        execute=lambda: {
            "id": kwargs["body"]["id"],
            "htmlLink": "https://calendar.google.com/event/example",
        }
    )
    return client


def make_assignment(database, **overrides):
    values = {
        "course_code": "CSE 3100",
        "course_name": "Systems Programming",
        "title": "Lab 2",
        "due_date": datetime(2026, 10, 10, 3, 59, tzinfo=UTC),
        "origin": AssignmentOrigin.BLACKBOARD,
        "direct_url": "https://huskyct.uconn.edu/ultra/courses/course/outline/item",
        "status": AssignmentStatus.NOT_SUBMITTED,
        "source_key": "lab-2",
    }
    values.update(overrides)
    with database() as session:
        item = Assignment(**values)
        session.add(item)
        session.commit()
        return item.id


def http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b"{}")


def test_sync_creates_event_with_exact_due_time_and_saves_id(database, service):
    assignment_id = make_assignment(database)

    result = calendar_sync.sync_assignment(assignment_id, service, database)

    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["summary"] == "[CSE 3100] Lab 2"
    assert "Platform: BLACKBOARD" in body["description"]
    assert "https://huskyct.uconn.edu/" in body["description"]
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert end.astimezone(UTC) == datetime(2026, 10, 10, 3, 59, tzinfo=UTC)
    assert end - start == timedelta(hours=1)
    assert body["end"]["timeZone"] == "America/New_York"
    assert re.fullmatch("[0-9a-v]{5,1024}", result.event_id)
    assert result.created
    with database() as session:
        assert session.get(Assignment, assignment_id).calendar_event_id == result.event_id


def test_resync_updates_existing_event_after_deadline_change(database, service):
    assignment_id = make_assignment(database, calendar_event_id="existing-calendar-event")
    with database() as session:
        session.get(Assignment, assignment_id).due_date += timedelta(days=1)
        session.commit()
    service.events.return_value.update.return_value.execute.return_value = {
        "id": "existing-calendar-event"
    }

    result = calendar_sync.sync_assignment(assignment_id, service, database)

    assert not result.created
    service.events.return_value.insert.assert_not_called()
    arguments = service.events.return_value.update.call_args.kwargs
    assert arguments["eventId"] == "existing-calendar-event"
    assert datetime.fromisoformat(arguments["body"]["end"]["dateTime"]).astimezone(UTC) == datetime(
        2026, 10, 11, 3, 59, tzinfo=UTC
    )


def test_same_source_key_in_different_courses_or_platforms_has_distinct_events(database, service):
    assignments = [
        make_assignment(database, course_code="CSE 3100", source_key="lab-2"),
        make_assignment(database, course_code="CSE 2301", source_key="lab-2"),
        make_assignment(
            database,
            course_code="CSE 3100",
            source_key="lab-2",
            origin=AssignmentOrigin.MCGRAW_CONNECT,
        ),
    ]

    results = [calendar_sync.sync_assignment(identifier, service, database) for identifier in assignments]

    assert len({result.event_id for result in results}) == 3


def test_partial_insert_failure_recovers_conflict_instead_of_duplicating(database, service):
    assignment_id = make_assignment(database)
    service.events.return_value.insert.side_effect = None
    service.events.return_value.insert.return_value.execute.side_effect = http_error(409)
    service.events.return_value.get.return_value.execute.return_value = {"id": "recovered"}
    service.events.return_value.update.return_value.execute.return_value = {"id": "recovered"}

    result = calendar_sync.sync_assignment(assignment_id, service, database)

    assert result.event_id == "recovered"
    assert not result.created
    get_id = service.events.return_value.get.call_args.kwargs["eventId"]
    update_id = service.events.return_value.update.call_args.kwargs["eventId"]
    assert get_id == update_id
    with database() as session:
        assert session.get(Assignment, assignment_id).calendar_event_id == "recovered"


@pytest.mark.parametrize("status", [404, 410])
def test_deleted_event_gets_a_new_repeatable_id(database, service, status):
    assignment_id = make_assignment(database)
    first = calendar_sync.sync_assignment(assignment_id, service, database)
    service.events.return_value.update.return_value.execute.side_effect = http_error(status)

    replacement = calendar_sync.sync_assignment(assignment_id, service, database)

    assert replacement.created
    assert replacement.event_id != first.event_id
    with database() as session:
        assert session.get(Assignment, assignment_id).calendar_event_id == replacement.event_id


def test_api_failure_does_not_mark_assignment_as_synced(database, service):
    assignment_id = make_assignment(database)
    service.events.return_value.insert.side_effect = None
    service.events.return_value.insert.return_value.execute.side_effect = http_error(403)

    with pytest.raises(HttpError):
        calendar_sync.sync_assignment(assignment_id, service, database)

    with database() as session:
        assert session.get(Assignment, assignment_id).calendar_event_id is None


def test_batch_selects_only_future_unsynced_unsubmitted_assignments(database, service, monkeypatch):
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    monkeypatch.setattr(calendar_sync, "now_utc", lambda: now)
    for hours, status, event_id, key in [
        (1, AssignmentStatus.NOT_SUBMITTED, None, "urgent"),
        (-1, AssignmentStatus.OVERDUE, None, "past"),
        (48, AssignmentStatus.NOT_SUBMITTED, None, "boundary"),
        (49, AssignmentStatus.NOT_SUBMITTED, None, "later"),
        (1, AssignmentStatus.SUBMITTED, None, "done"),
        (1, AssignmentStatus.NOT_SUBMITTED, "already-synced", "synced"),
    ]:
        make_assignment(
            database,
            due_date=now + timedelta(hours=hours),
            status=status,
            calendar_event_id=event_id,
            source_key=key,
        )

    result = calendar_sync.sync_all_urgent(service, database)

    assert result.synced == 1
    assert result.failures == []
    service.events.return_value.insert.assert_called_once()


def test_authentication_is_actionable_without_opening_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_sync, "ensure_local_directories", lambda: None)
    monkeypatch.setattr(
        calendar_sync,
        "settings",
        SimpleNamespace(
            google_credentials_path=tmp_path / "credentials.json",
            google_token_path=tmp_path / "token.json",
        ),
    )
    interactive = Mock()
    monkeypatch.setattr(calendar_sync, "_interactive_credentials", interactive)

    with pytest.raises(calendar_sync.CalendarAuthenticationError, match="auth locally"):
        calendar_sync.authenticate_google()

    interactive.assert_not_called()


def test_cached_token_is_private_and_atomically_replaced(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("old-token", encoding="utf-8")
    credentials = Mock()
    credentials.to_json.return_value = '{"token": "fake-test-token"}'

    calendar_sync._write_token(credentials, token_path)

    assert token_path.read_text(encoding="utf-8") == '{"token": "fake-test-token"}'
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".token-*")) == []
