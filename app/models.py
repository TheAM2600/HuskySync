"""SQLAlchemy models with UTC dates and live, derived urgency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SQLAEnum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def now_utc() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite stores naive UTC; application code always receives aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class AssignmentOrigin(str, Enum):
    BLACKBOARD = "BLACKBOARD"
    MCGRAW_CONNECT = "MCGRAW_CONNECT"
    EMAIL = "EMAIL"


class AssignmentStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    OVERDUE = "OVERDUE"


class EmailActionType(str, Enum):
    DEADLINE = "DEADLINE"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    EVENT = "EVENT"


class EmailActionStatus(str, Enum):
    PENDING = "PENDING"
    CONVERTED = "CONVERTED"
    DISMISSED = "DISMISSED"


def _enum(enum: type[Enum]) -> SQLAEnum:
    return SQLAEnum(enum, native_enum=False, create_constraint=True, validate_strings=True)


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("origin", "course_code", "source_key", name="uq_assignment_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_code: Mapped[str] = mapped_column(String(100), nullable=False)
    course_name: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    due_date: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    origin: Mapped[AssignmentOrigin] = mapped_column(_enum(AssignmentOrigin), nullable=False)
    direct_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[AssignmentStatus] = mapped_column(_enum(AssignmentStatus), nullable=False, default=AssignmentStatus.NOT_SUBMITTED)
    calendar_event_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_key: Mapped[str] = mapped_column(String(512), nullable=False)
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def is_urgent(self) -> bool:
        now = now_utc()
        due_date = self.due_date
        if due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=UTC)
        return (
            self.status != AssignmentStatus.SUBMITTED
            and now <= due_date < now + timedelta(hours=48)
        )

    @property
    def effective_status(self) -> AssignmentStatus:
        if self.status == AssignmentStatus.SUBMITTED:
            return AssignmentStatus.SUBMITTED
        due_date = self.due_date
        if due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=UTC)
        if due_date < now_utc():
            return AssignmentStatus.OVERDUE
        return AssignmentStatus.NOT_SUBMITTED


class GoogleTaskLink(Base):
    """The Google Task created for an assignment; a separate table so existing databases need no migration."""

    __tablename__ = "google_task_links"

    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True)
    tasklist_id: Mapped[str] = mapped_column(String(300), nullable=False)
    task_id: Mapped[str] = mapped_column(String(300), nullable=False)


class EmailActionItem(Base):
    __tablename__ = "email_action_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sender: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(1000), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    extracted_type: Mapped[EmailActionType] = mapped_column(_enum(EmailActionType), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[EmailActionStatus] = mapped_column(_enum(EmailActionStatus), nullable=False, default=EmailActionStatus.PENDING)
    source_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    converted_assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"), nullable=True)
