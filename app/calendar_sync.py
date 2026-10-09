"""Google Calendar OAuth and repeatable assignment synchronization.

Run ``python -m app.calendar_sync auth`` on the local computer once to grant
access. Subsequent dashboard synchronizations reuse the saved OAuth token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ensure_local_directories, settings
from app.database import SessionLocal, init_db
from app.models import Assignment, AssignmentStatus, now_utc

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]


class CalendarAuthenticationError(RuntimeError):
    """An actionable, credential-free error suitable for the dashboard."""


@dataclass(frozen=True)
class SyncResult:
    event_id: str
    created: bool
    html_link: str | None = None


@dataclass(frozen=True)
class SyncFailure:
    assignment_id: int
    error: str


@dataclass
class BatchSyncResult:
    synced: int = 0
    failures: list[SyncFailure] = field(default_factory=list)


def _write_token(credentials: Credentials, token_path: Path) -> None:
    """Replace the OAuth cache atomically without exposing its contents."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".token-", suffix=".json", dir=token_path.parent
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, token_path)
        os.chmod(token_path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _interactive_credentials(credentials_path: Path) -> Credentials:
    if not credentials_path.is_file():
        raise CalendarAuthenticationError(
            f"Download a Google OAuth Desktop app client JSON to {credentials_path}, "
            "enable the Google Calendar API, then run python -m app.calendar_sync auth."
        )
    try:
        with credentials_path.open(encoding="utf-8") as handle:
            client_config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CalendarAuthenticationError(
            "The Google credentials file is unreadable or invalid JSON. "
            "Download a new OAuth Desktop app client JSON."
        ) from exc
    if "installed" not in client_config:
        raise CalendarAuthenticationError(
            "Use a Google OAuth Desktop app client, whose JSON contains an installed section."
        )
    try:
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        return flow.run_local_server(
            host="localhost",
            bind_addr="127.0.0.1",
            port=0,
            open_browser=True,
            authorization_prompt_message="Open this Google authorization URL in your local browser: {url}",
            success_message="HuskySync is authorized. You may close this window.",
            timeout_seconds=180,
        )
    except (ValueError, OSError, GoogleAuthError) as exc:
        raise CalendarAuthenticationError(
            "Google authorization did not complete. Run python -m app.calendar_sync auth "
            "on your local computer and approve Calendar access."
        ) from exc


def authenticate_google(interactive: bool = False) -> Any:
    """Build a Calendar client; only explicit interactive calls open a browser."""
    return build("calendar", "v3", credentials=google_credentials(interactive, SCOPES[:1]), cache_discovery=False)


def google_credentials(interactive: bool = False, required_scopes: list[str] | None = None) -> Credentials:
    """Load, refresh, or interactively obtain the one token shared by Calendar and Tasks.

    A token saved before Tasks support still serves Calendar; only the caller's
    own scopes are required of it. Interactive authorization requests them all.
    """
    required_scopes = SCOPES if interactive or required_scopes is None else required_scopes
    ensure_local_directories()
    token_path = Path(settings.google_token_path)
    credentials_path = Path(settings.google_credentials_path)
    credentials: Credentials | None = None
    changed = False
    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path))
        except (ValueError, OSError):
            credentials = None
    if credentials is not None and not credentials.has_scopes(required_scopes):
        credentials = None
    if credentials is not None and not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                changed = True
            except GoogleAuthError as exc:
                if not interactive:
                    raise CalendarAuthenticationError(
                        "The saved Google authorization could not be refreshed. "
                        "Run python -m app.calendar_sync auth to authorize again."
                    ) from exc
                credentials = None
        else:
            credentials = None
    if credentials is None or not credentials.valid:
        if not interactive:
            raise CalendarAuthenticationError(
                "Google Calendar and Tasks are not authorized. Place an OAuth Desktop app client JSON "
                f"at {credentials_path} and run python -m app.calendar_sync auth locally."
            )
        credentials = _interactive_credentials(credentials_path)
        changed = True
    if changed:
        _write_token(credentials, token_path)
    return credentials


def _aware_utc(value: datetime) -> datetime:
    # SQLite's DateTime representation can be naive; the database stores UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_body(assignment: Assignment) -> dict[str, Any]:
    due = _aware_utc(assignment.due_date)
    local_timezone = ZoneInfo(settings.timezone)
    origin = getattr(assignment.origin, "value", str(assignment.origin))
    return {
        "summary": f"[{assignment.course_code}] {assignment.title}",
        "description": (
            f"Course: {assignment.course_name}\n"
            f"Platform: {origin}\n"
            f"Assignment: {assignment.direct_url or '(no direct link available)'}\n"
            "Managed by HuskySync."
        ),
        "start": {
            "dateTime": (due - timedelta(hours=1)).astimezone(local_timezone).isoformat(),
            "timeZone": settings.timezone,
        },
        "end": {
            "dateTime": due.astimezone(local_timezone).isoformat(),
            "timeZone": settings.timezone,
        },
    }


def _deterministic_event_id(assignment: Assignment, replaces: str | None = None) -> str:
    """Use Google's allowed base32hex alphabet to recover after partial failures."""
    source_key = getattr(assignment, "source_key", None)
    origin = getattr(assignment.origin, "value", str(assignment.origin))
    identity = json.dumps(
        [origin, assignment.course_code, source_key]
        if source_key
        else [assignment.id, origin, assignment.course_code, assignment.direct_url, assignment.title],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    # Hex is a subset of Google's lowercase base32hex alphabet (0-9, a-v).
    if replaces:
        identity += f"|replaces:{replaces}"
    return "husk" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _status(error: HttpError) -> int | None:
    return getattr(error.resp, "status", None)


def _create_or_recover(
    service: Any, event_id: str, body: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    events = service.events()
    try:
        event = events.insert(
            calendarId=settings.google_calendar_id,
            body={**body, "id": event_id},
        ).execute()
        return event, True
    except HttpError as exc:
        if _status(exc) != 409:
            raise
        # The insert may have succeeded before a network/database failure. The
        # deterministic ID makes a retry recover and update that same event.
        events.get(calendarId=settings.google_calendar_id, eventId=event_id).execute()
        event = events.update(
            calendarId=settings.google_calendar_id, eventId=event_id, body=body
        ).execute()
        return event, False


def sync_assignment(
    assignment_id: int,
    service: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> SyncResult:
    """Create or update an event, saving its ID only after Google succeeds."""
    session_factory = session_factory or SessionLocal
    with session_factory() as session:
        assignment = session.get(Assignment, assignment_id)
        if assignment is None:
            raise ValueError(f"Assignment {assignment_id} does not exist.")
        service = service if service is not None else authenticate_google()
        body = _event_body(assignment)
        if assignment.calendar_event_id:
            try:
                event = service.events().update(
                    calendarId=settings.google_calendar_id,
                    eventId=assignment.calendar_event_id,
                    body=body,
                ).execute()
                created = False
            except HttpError as exc:
                if _status(exc) not in (404, 410):
                    raise
                event, created = _create_or_recover(
                    service, _deterministic_event_id(assignment, assignment.calendar_event_id), body
                )
        else:
            event, created = _create_or_recover(
                service, _deterministic_event_id(assignment), body
            )
        event_id = event.get("id")
        if not event_id:
            raise RuntimeError("Google Calendar returned an event without an ID.")
        assignment.calendar_event_id = event_id
        session.commit()
        return SyncResult(event_id, created, event.get("htmlLink"))


def sync_all_urgent(
    service: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> BatchSyncResult:
    """Sync future, unsubmitted, unsynced assignments due in less than 48 hours."""
    session_factory = session_factory or SessionLocal
    current = now_utc()
    with session_factory() as session:
        assignment_ids = list(
            session.scalars(
                select(Assignment.id)
                .where(
                    Assignment.status != AssignmentStatus.SUBMITTED,
                    Assignment.calendar_event_id.is_(None),
                    Assignment.due_date >= current,
                    Assignment.due_date < current + timedelta(hours=48),
                )
                .order_by(Assignment.due_date)
            )
        )
    result = BatchSyncResult()
    if not assignment_ids:
        return result
    service = service if service is not None else authenticate_google()
    for assignment_id in assignment_ids:
        try:
            sync_assignment(assignment_id, service, session_factory)
            result.synced += 1
        except HttpError as exc:
            # Report status rather than API error content, which may contain
            # private assignment descriptions or authenticated request URLs.
            result.failures.append(
                SyncFailure(assignment_id, f"Google Calendar request failed (HTTP {_status(exc)}).")
            )
        except Exception as exc:
            result.failures.append(
                SyncFailure(assignment_id, f"Synchronization failed ({type(exc).__name__}).")
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("auth", "sync-urgent"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "auth":
            authenticate_google(interactive=True)
            print("Google Calendar authorization saved securely.")
        else:
            init_db()
            result = sync_all_urgent()
            print(f"Synced {result.synced} assignment(s); {len(result.failures)} failed.")
            for failure in result.failures:
                print(f"Assignment {failure.assignment_id}: {failure.error}")
            if result.failures:
                raise SystemExit(1)
    except CalendarAuthenticationError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
