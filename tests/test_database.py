from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

import app.models as models
from app.database import create_session_factory
from app.models import Assignment, AssignmentOrigin, AssignmentStatus, EmailActionItem, EmailActionStatus, EmailActionType
from app.services import AssignmentInput, convert_email_to_assignment, dismiss_email, upsert_assignment


@pytest.fixture
def session_factory(tmp_path):
    factory = create_session_factory(tmp_path / "test.sqlite3")
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def assignment_data(**overrides):
    values = dict(
        course_code="CSE 3100",
        course_name="Systems Programming",
        title="Programming assignment 1",
        due_date=datetime(2026, 10, 20, 23, 59, tzinfo=ZoneInfo("America/New_York")),
        origin=AssignmentOrigin.BLACKBOARD,
        direct_url="https://huskyct.uconn.edu/ultra/courses/course-1/assignment-1",
        source_key="assignment-1",
    )
    values.update(overrides)
    return AssignmentInput(**values)


def email_item(**overrides):
    values = dict(
        sender="professor@uconn.edu",
        subject="Project proposal due Friday",
        received_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
        extracted_type=EmailActionType.DEADLINE,
        summary="Submit the project proposal by Friday.",
        suggested_deadline=datetime(2026, 10, 16, 23, 59, tzinfo=UTC),
        source_key="email-message-1:deadline-1",
    )
    values.update(overrides)
    return EmailActionItem(**values)


def test_dates_round_trip_as_utc_and_sqlite_prerequisites(session_factory):
    original = assignment_data()
    with session_factory() as session:
        record = upsert_assignment(session, original)
        record_id = record.id
        session.commit()
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        assert session.scalar(text("PRAGMA journal_mode")) == "wal"
    with session_factory() as session:
        stored = session.get(Assignment, record_id)
        assert stored.due_date.tzinfo is UTC
        assert stored.due_date == original.due_date.astimezone(UTC)
        assert stored.due_date.hour == 3  # EDT 23:59 is 03:59 UTC next day.


def test_upsert_updates_source_record_and_preserves_calendar_id(session_factory):
    with session_factory() as session:
        record = upsert_assignment(session, assignment_data())
        record.calendar_event_id = "google-event-123"
        first_id = record.id
        session.commit()
    changed_date = datetime(2026, 10, 25, 16, tzinfo=UTC)
    with session_factory() as session:
        record = upsert_assignment(
            session,
            assignment_data(title="Revised programming assignment", due_date=changed_date, status=AssignmentStatus.SUBMITTED),
        )
        session.commit()
        assert record.id == first_id
        assert record.calendar_event_id == "google-event-123"
        assert record.title == "Revised programming assignment"
        assert record.due_date == changed_date
        assert record.status == AssignmentStatus.SUBMITTED
        assert session.scalar(select(func.count()).select_from(Assignment)) == 1


def test_fallback_source_identity_survives_deadline_changes():
    first = assignment_data(source_key=None)
    second = assignment_data(source_key=None, due_date=first.due_date + timedelta(days=1), title="New title")
    assert first.source_key == second.source_key
    no_link = assignment_data(source_key=None, direct_url="")
    equivalent = assignment_data(source_key=None, direct_url="", title=" PROGRAMMING   assignment 1 ")
    assert no_link.source_key == equivalent.source_key


@pytest.mark.parametrize(
    ("hours_until_due", "status", "expected"),
    [
        (-1, AssignmentStatus.NOT_SUBMITTED, False),
        (0, AssignmentStatus.NOT_SUBMITTED, True),
        (47.99, AssignmentStatus.NOT_SUBMITTED, True),
        (48, AssignmentStatus.NOT_SUBMITTED, False),
        (12, AssignmentStatus.SUBMITTED, False),
    ],
)
def test_urgency_boundaries(monkeypatch, hours_until_due, status, expected):
    clock = datetime(2026, 10, 9, 12, tzinfo=UTC)
    monkeypatch.setattr(models, "now_utc", lambda: clock)
    record = Assignment(**assignment_data(due_date=clock + timedelta(hours=hours_until_due), status=status).model_dump())
    assert record.is_urgent is expected


def test_effective_status_and_urgency_follow_time(monkeypatch):
    clock = datetime(2026, 10, 9, 12, tzinfo=UTC)
    monkeypatch.setattr(models, "now_utc", lambda: clock)
    record = Assignment(**assignment_data(due_date=clock + timedelta(hours=1)).model_dump())
    assert record.effective_status == AssignmentStatus.NOT_SUBMITTED
    assert record.is_urgent
    clock += timedelta(hours=2)
    assert record.effective_status == AssignmentStatus.OVERDUE
    assert not record.is_urgent
    record.status = AssignmentStatus.SUBMITTED
    assert record.effective_status == AssignmentStatus.SUBMITTED


def test_conversion_is_idempotent_and_creates_linked_email_task(session_factory):
    with session_factory() as session:
        item = email_item()
        session.add(item)
        session.flush()
        first = convert_email_to_assignment(session, item.id)
        second = convert_email_to_assignment(session, item.id)
        session.commit()
        assert first.id == second.id == item.converted_assignment_id
        assert first.origin == AssignmentOrigin.EMAIL
        assert first.due_date == item.suggested_deadline
        assert item.status == EmailActionStatus.CONVERTED
        assert session.scalar(select(func.count()).select_from(Assignment)) == 1
        with pytest.raises(ValueError, match="Converted"):
            dismiss_email(session, item.id)


def test_conversion_rejects_dismissed_and_undated_items(session_factory):
    with session_factory() as session:
        undated = email_item(suggested_deadline=None)
        dismissed = email_item(source_key="email-message-2", status=EmailActionStatus.DISMISSED)
        session.add_all([undated, dismissed])
        session.flush()
        with pytest.raises(ValueError, match="no parsed deadline"):
            convert_email_to_assignment(session, undated.id)
        with pytest.raises(ValueError, match="Dismissed"):
            convert_email_to_assignment(session, dismissed.id)
        dismiss_email(session, undated.id)
        assert undated.status == EmailActionStatus.DISMISSED
        assert session.scalar(select(func.count()).select_from(Assignment)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_date": datetime(2026, 10, 9, 12)},
        {"direct_url": "javascript:alert(1)"},
        {"direct_url": "https://username:password@example.com/assignment"},
    ],
)
def test_assignment_input_rejects_ambiguous_dates_and_invalid_urls(overrides):
    with pytest.raises(ValidationError):
        assignment_data(**overrides)
