from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from app.database import create_session_factory
from app.models import AssignmentOrigin, AssignmentStatus, GoogleTaskLink
from app.services import AssignmentInput, upsert_assignment
from app.tasks_sync import sync_assignments_to_tasks


def make_assignment(factory, title, status=AssignmentStatus.NOT_SUBMITTED):
    with factory() as session:
        item = upsert_assignment(session, AssignmentInput(
            course_code="OPIM 5601", course_name="Communications", title=title,
            # 03:59 UTC on the 12th is 11:59 PM on the 11th in New York.
            due_date=datetime(2026, 10, 12, 3, 59, tzinfo=UTC), origin=AssignmentOrigin.BLACKBOARD,
            direct_url="https://lms.uconn.edu/ultra/courses/_1_1/outline", status=status, source_key=title,
        ))
        session.commit()
        return item.id


def fake_service(patch_error=None):
    service = MagicMock()
    counter = iter(range(1, 100))
    service.tasks().insert.side_effect = lambda **kwargs: MagicMock(execute=lambda: {"id": f"task-{next(counter)}"})
    if patch_error is not None:
        service.tasks().patch.return_value.execute.side_effect = patch_error
    else:
        service.tasks().patch.return_value.execute.return_value = {"id": "task-1"}
    service.tasks().insert.reset_mock()
    return service


def test_only_chosen_assignments_become_tasks_and_resync_updates(tmp_path):
    factory = create_session_factory(tmp_path / "tasks.sqlite3")
    try:
        chosen = make_assignment(factory, "Week 6 Self-Assessment")
        done = make_assignment(factory, "Orientation Quiz", AssignmentStatus.SUBMITTED)
        skipped = make_assignment(factory, "Not chosen")
        service = fake_service()

        result = sync_assignments_to_tasks([chosen, done], service, factory)
        assert (result.synced, result.failures) == (2, [])
        bodies = [call.kwargs["body"] for call in service.tasks().insert.call_args_list]
        assert bodies[0]["title"] == "[OPIM 5601] Week 6 Self-Assessment"
        assert bodies[0]["due"] == "2026-10-11T00:00:00.000Z"
        assert "11:59 PM" in bodies[0]["notes"]
        assert [body["status"] for body in bodies] == ["needsAction", "completed"]
        with factory() as session:
            assert session.get(GoogleTaskLink, chosen).task_id == "task-1"
            assert session.get(GoogleTaskLink, skipped) is None

        sync_assignments_to_tasks([chosen], service, factory)
        assert service.tasks().insert.call_count == 2
        assert service.tasks().patch.call_args.kwargs["task"] == "task-1"
    finally:
        factory.kw["bind"].dispose()


def test_deleted_task_is_recreated_and_failures_are_reported(tmp_path):
    factory = create_session_factory(tmp_path / "tasks.sqlite3")
    try:
        item = make_assignment(factory, "Assignment 5")
        sync_assignments_to_tasks([item], fake_service(), factory)
        gone = fake_service(HttpError(SimpleNamespace(status=404, reason="Not Found"), b""))
        assert sync_assignments_to_tasks([item], gone, factory).synced == 1
        assert gone.tasks().insert.call_count == 1

        denied = fake_service(HttpError(SimpleNamespace(status=403, reason="Forbidden"), b""))
        result = sync_assignments_to_tasks([item, 9999], denied, factory)
        assert result.synced == 0
        assert [failure.error for failure in result.failures] == [
            "Google Tasks request failed (HTTP 403).", "Synchronization failed (ValueError).",
        ]
    finally:
        factory.kw["bind"].dispose()
