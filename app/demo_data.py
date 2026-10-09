"""Sample coursework for the public demo. No account is contacted.

``assignments.json`` is written around a reference date. Deadlines are moved by
whole days so that date lands on today, which keeps "due within 48 hours" and
overdue examples visible whenever the demo is opened.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from app.config import PROJECT_ROOT, settings
from app.models import Assignment, AssignmentOrigin, AssignmentStatus, EmailActionItem, GoogleTaskLink
from app.services import AssignmentInput, upsert_assignment


SAMPLE_ASSIGNMENTS = PROJECT_ROOT / "assignments.json"
SAMPLE_EMAILS = PROJECT_ROOT / "emails.json"


def seed_demo_assignments(session_factory: Callable, path: Path | str = SAMPLE_ASSIGNMENTS, *, today: date | None = None) -> int:
    """Load the sample assignments, returning how many were saved."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    shift = timedelta(days=(today - date.fromisoformat(data["reference_date"])).days)
    course_titles = {course["code"]: course["title"] for course in data.get("courses", [])}
    with session_factory() as session:
        for entry in data["assignments"]:
            upsert_assignment(session, AssignmentInput(
                course_code=entry["course"],
                course_name=course_titles.get(entry["course"], entry["course"]),
                title=entry["title"],
                due_date=datetime.fromisoformat(entry["due_at"]) + shift,
                origin=AssignmentOrigin.BLACKBOARD,
                status=AssignmentStatus.SUBMITTED if entry.get("submitted") else AssignmentStatus.NOT_SUBMITTED,
                source_key=f"demo:{entry['id']}",
            ))
        session.commit()
    return len(data["assignments"])


def ensure_demo_data(session_factory: Callable) -> None:
    """Seed an empty demo database once; later visits keep what visitors changed."""
    with session_factory() as session:
        if session.scalar(select(func.count()).select_from(Assignment)):
            return
    seed_demo_assignments(session_factory)


def reset_demo_data(session_factory: Callable) -> None:
    """Discard everything visitors did and start again from the sample files."""
    with session_factory() as session:
        for model in (GoogleTaskLink, EmailActionItem, Assignment):
            session.execute(delete(model))
        session.commit()
    seed_demo_assignments(session_factory)
