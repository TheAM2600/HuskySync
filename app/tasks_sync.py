"""Opt-in synchronization of chosen assignments to Google Tasks.

Tasks appear in Google Calendar's Tasks view. Google Tasks stores a due *date*
only, so the exact deadline time is written into the task notes. Authorization
is shared with Calendar: run ``python -m app.calendar_sync auth``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.calendar_sync import SCOPES, BatchSyncResult, SyncFailure, _aware_utc, _status, google_credentials
from app.config import settings
from app.database import SessionLocal
from app.models import Assignment, AssignmentStatus, GoogleTaskLink


def authenticate_tasks(interactive: bool = False) -> Any:
    """Build a Tasks client from the saved Google authorization."""
    return build("tasks", "v1", credentials=google_credentials(interactive, SCOPES[1:]), cache_discovery=False)


def _task_body(assignment: Assignment) -> dict[str, Any]:
    local_due = _aware_utc(assignment.due_date).astimezone(ZoneInfo(settings.timezone))
    origin = getattr(assignment.origin, "value", str(assignment.origin))
    submitted = assignment.status == AssignmentStatus.SUBMITTED
    return {
        "title": f"[{assignment.course_code}] {assignment.title}",
        "notes": (
            f"Due: {local_due.strftime('%a, %b %d, %Y at %I:%M %p %Z')}\n"
            f"Course: {assignment.course_name}\n"
            f"Platform: {origin}\n"
            f"Assignment: {assignment.direct_url or '(no direct link available)'}\n"
            "Managed by HuskySync."
        ),
        # The API discards the time of day; send the local calendar date.
        "due": f"{local_due.date().isoformat()}T00:00:00.000Z",
        "status": "completed" if submitted else "needsAction",
    }


def sync_assignment_to_task(
    assignment_id: int,
    service: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """Create or update one task; returns True when a new task was created."""
    session_factory = session_factory or SessionLocal
    with session_factory() as session:
        assignment = session.get(Assignment, assignment_id)
        if assignment is None:
            raise ValueError(f"Assignment {assignment_id} does not exist.")
        service = service if service is not None else authenticate_tasks()
        body = _task_body(assignment)
        link = session.get(GoogleTaskLink, assignment_id)
        task = None
        if link is not None:
            try:
                task = service.tasks().patch(tasklist=link.tasklist_id, task=link.task_id, body=body).execute()
            except HttpError as exc:
                if _status(exc) not in (404, 410):
                    raise
        if task is not None:
            return False
        task = service.tasks().insert(tasklist=settings.google_tasklist_id, body=body).execute()
        task_id = task.get("id")
        if not task_id:
            raise RuntimeError("Google Tasks returned a task without an ID.")
        if link is None:
            session.add(GoogleTaskLink(assignment_id=assignment_id, tasklist_id=settings.google_tasklist_id, task_id=task_id))
        else:
            link.tasklist_id, link.task_id = settings.google_tasklist_id, task_id
        session.commit()
        return True


def sync_assignments_to_tasks(
    assignment_ids: Iterable[int],
    service: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> BatchSyncResult:
    """Sync only the assignments the user chose; one failure does not stop the rest."""
    result = BatchSyncResult()
    assignment_ids = list(assignment_ids)
    if not assignment_ids:
        return result
    service = service if service is not None else authenticate_tasks()
    for assignment_id in assignment_ids:
        try:
            sync_assignment_to_task(assignment_id, service, session_factory)
            result.synced += 1
        except HttpError as exc:
            result.failures.append(SyncFailure(assignment_id, f"Google Tasks request failed (HTTP {_status(exc)})."))
        except Exception as exc:
            result.failures.append(SyncFailure(assignment_id, f"Synchronization failed ({type(exc).__name__})."))
    return result
