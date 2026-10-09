"""Validated assignment inputs and transactional data operations.

Helpers flush but leave commit/rollback ownership with the calling service.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Assignment, AssignmentOrigin, AssignmentStatus, EmailActionItem, EmailActionStatus


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


class AssignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    course_code: str = Field(min_length=1, max_length=100)
    course_name: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=1000)
    due_date: datetime
    origin: AssignmentOrigin
    direct_url: str = ""
    status: AssignmentStatus = AssignmentStatus.NOT_SUBMITTED
    source_key: str | None = Field(default=None, min_length=1, max_length=512)
    is_external: bool = False

    @field_validator("due_date")
    @classmethod
    def _aware_due_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_date must include a timezone")
        return value

    @field_validator("direct_url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("direct_url must be an HTTP(S) URL without embedded credentials")
        return value

    @model_validator(mode="after")
    def _set_source_key(self) -> AssignmentInput:
        if not self.source_key:
            # Prefer a source-provided ID. A URL is a stable fallback across
            # deadline/title edits; otherwise use normalized course and title.
            identity = self.direct_url or f"{_normalized(self.course_code)}|{_normalized(self.title)}"
            self.source_key = "derived:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self


def upsert_assignment(session: Session, data: AssignmentInput) -> Assignment:
    assignment = session.scalar(
        select(Assignment).where(
            Assignment.origin == data.origin,
            Assignment.course_code == data.course_code,
            Assignment.source_key == data.source_key,
        )
    )
    values = data.model_dump()
    if assignment is None:
        assignment = Assignment(**values)
        session.add(assignment)
    else:
        for name, value in values.items():
            setattr(assignment, name, value)
    session.flush()
    return assignment


def convert_email_to_assignment(
    session: Session,
    email_id: int,
    course_code: str = "EMAIL",
    course_name: str = "Email tasks",
) -> Assignment:
    item = session.get(EmailActionItem, email_id)
    if item is None:
        raise ValueError(f"Email action {email_id} does not exist")
    if item.status == EmailActionStatus.DISMISSED:
        raise ValueError("Dismissed email actions cannot be converted")
    if item.converted_assignment_id is not None:
        assignment = session.get(Assignment, item.converted_assignment_id)
        if assignment is not None:
            return assignment
    if item.suggested_deadline is None:
        raise ValueError("This email has no parsed deadline; a deadline is required to create a task")
    assignment = upsert_assignment(
        session,
        AssignmentInput(
            course_code=course_code,
            course_name=course_name,
            title=item.subject or item.summary[:1000] or "Email task",
            due_date=item.suggested_deadline,
            origin=AssignmentOrigin.EMAIL,
            direct_url="",
            source_key=f"email:{item.source_key}" if len(item.source_key) < 506 else "email:" + hashlib.sha256(item.source_key.encode()).hexdigest(),
        ),
    )
    item.status = EmailActionStatus.CONVERTED
    item.converted_assignment_id = assignment.id
    session.flush()
    return assignment


def dismiss_email(session: Session, email_id: int) -> None:
    item = session.get(EmailActionItem, email_id)
    if item is None:
        raise ValueError(f"Email action {email_id} does not exist")
    if item.status == EmailActionStatus.CONVERTED:
        raise ValueError("Converted actions cannot be dismissed; manage the created task instead")
    item.status = EmailActionStatus.DISMISSED
    session.flush()
