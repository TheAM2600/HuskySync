"""Local Streamlit dashboard. Run from the checkout with Streamlit, not Python."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

# Streamlit executes this file as a script and may put only app/ on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st
from googleapiclient.errors import HttpError
from sqlalchemy import select

from app.calendar_sync import sync_all_urgent, sync_assignment
from app.config import settings
from app.database import SessionLocal, init_db
from app.email_parser import scan_recent_emails
from app.models import Assignment, AssignmentOrigin, AssignmentStatus, EmailActionItem, EmailActionStatus
from app.scraper import sync_huskyct
from app.services import convert_email_to_assignment, dismiss_email


def run_browser_sync():
    """Give Playwright a fresh loop with subprocess support, including on Windows."""
    def work():
        if sys.platform == "win32":
            with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                return runner.run(sync_huskyct(headless=True))
        return asyncio.run(sync_huskyct(headless=True))

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(work).result()


def local_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo(settings.timezone)).strftime("%a, %b %d, %Y · %I:%M %p %Z")


def flash(message: str) -> None:
    st.session_state["husky_sync_notice"] = message
    st.rerun()


def readable_error(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        return f"Google Calendar request failed (HTTP {exc.resp.status}). Check calendar access and authorization."
    return str(exc)


def calendar_button(assignment: Assignment, key: str) -> None:
    label = "Update Google Calendar" if assignment.calendar_event_id else "Add to Google Calendar"
    if st.button(label, key=key, type="primary"):
        try:
            with st.spinner("Saving calendar event…"):
                result = sync_assignment(assignment.id)
            flash("Calendar event created." if result.created else "Calendar event updated.")
        except Exception as exc:
            st.error(f"Calendar sync failed: {readable_error(exc)}")


def draw_header() -> None:
    st.title("🐾 HuskySync")
    st.caption("Your coursework, email action items, and Google Calendar in one local dashboard.")
    status = st.columns(3)
    status[0].caption("SQLite · connected")
    profile_saved = settings.browser_profile_dir.exists() and any(settings.browser_profile_dir.iterdir())
    status[1].caption("HuskyCT · browser profile saved" if profile_saved else "HuskyCT · login needed")
    status[2].caption("Google · token cached" if settings.google_token_path.exists() else "Google · authorization needed")
    st.caption("Saved profiles and tokens may expire; sync verifies access when you use it.")

    controls = st.columns(3)
    if controls[0].button("Sync HuskyCT", use_container_width=True):
        try:
            with st.spinner("Reading HuskyCT pages…"):
                result = run_browser_sync()
            st.success(f"Saved {result.assignments_saved} assignment records; skipped {result.skipped} rows.")
            for warning in result.warnings:
                st.warning(warning)
        except Exception as exc:
            st.error(f"HuskyCT sync failed: {exc}")
    if controls[1].button("Scan Emails", use_container_width=True):
        try:
            with st.spinner("Reading recent unread messages…"):
                result = scan_recent_emails()
            st.success(f"Added {result.new_count} email action items; skipped {result.skipped_count} messages.")
            for warning in result.warnings:
                st.warning(warning)
        except Exception as exc:
            st.error(f"Email scan failed: {exc}")
    if controls[2].button("Sync all urgent", use_container_width=True):
        try:
            with st.spinner("Adding upcoming deadlines…"):
                result = sync_all_urgent()
            st.success(f"Synced {result.synced} assignments.")
            for failure in result.failures:
                st.error(f"Assignment {failure.assignment_id}: {failure.error}")
        except Exception as exc:
            st.error(f"Calendar sync failed: {readable_error(exc)}")

    with st.expander("Connection setup"):
        st.markdown("Complete UConn NetID and Duo in the local browser opened by this command:")
        st.code("python -m app.scraper login", language="bash")
        st.markdown("Authorize your Google account after placing a Desktop OAuth client file at the configured credentials path:")
        st.code("python -m app.calendar_sync auth", language="bash")
        st.caption(f"Google credentials file: {settings.google_credentials_path}")
        st.markdown("For Outlook, set `HUSKYSYNC_IMAP_USERNAME` and `HUSKYSYNC_IMAP_ACCESS_TOKEN` before starting the app. See the README for Microsoft tenant requirements.")


def draw_critical_alerts(assignments: list[Assignment]) -> None:
    urgent = [item for item in assignments if item.is_urgent]
    overdue = [item for item in assignments if item.effective_status == AssignmentStatus.OVERDUE]
    st.subheader("Critical alerts")
    if not assignments:
        st.info("Import coursework to check upcoming deadlines.")
    elif urgent:
        st.warning(f"{len(urgent)} unsubmitted assignment(s) due within 48 hours.")
        for item in urgent:
            with st.container(border=True):
                left, right = st.columns([3, 2])
                with left:
                    st.write(f"**[{item.course_code}] {item.title}**")
                    st.caption(local_time(item.due_date))
                    if item.is_external:
                        st.caption("External publisher · verify submission in Connect")
                    if item.direct_url:
                        st.link_button("Open assignment", item.direct_url)
                with right:
                    calendar_button(item, f"urgent_calendar_{item.id}")
    else:
        st.success("No unsubmitted assignments due in the next 48 hours.")
    if overdue:
        st.error(f"{len(overdue)} assignment(s) are past due and have no recorded submission.")


STATUS_LABELS = {
    AssignmentStatus.SUBMITTED: "Submitted",
    AssignmentStatus.NOT_SUBMITTED: "Not Submitted",
    AssignmentStatus.OVERDUE: "Overdue",
}
STATUS_COLORS = {"Submitted": "#d8f3dc", "Not Submitted": "#fff3bf", "Overdue": "#ffd6d6"}


def draw_assignment_tracker(assignments: list[Assignment]) -> None:
    st.subheader("Assignment tracker")
    left, right = st.columns(2)
    course = left.selectbox("Course", ["All courses", *sorted({a.course_code for a in assignments})])
    status = right.selectbox("Status", ["All statuses", *STATUS_COLORS])
    filtered = [a for a in assignments if (course == "All courses" or a.course_code == course)
                and (status == "All statuses" or STATUS_LABELS[a.effective_status] == status)]
    if not filtered:
        st.info("No assignments match. Sync HuskyCT or convert a dated email action item to get started.")
        return
    rows = [{"Course": a.course_code, "Course name": a.course_name, "Assignment": a.title,
             "Due": local_time(a.due_date), "Status": STATUS_LABELS[a.effective_status],
             "Origin": a.origin.value, "External": a.is_external,
             "Calendar": "Synced" if a.calendar_event_id else "Not synced", "Link": a.direct_url}
            for a in filtered]
    frame = pd.DataFrame(rows)
    styled = frame.style.map(lambda value: f"background-color: {STATUS_COLORS[value]}; color: #171717; border-radius: 6px;", subset=["Status"])
    st.dataframe(styled, hide_index=True, use_container_width=True,
                 column_config={"Link": st.column_config.LinkColumn("Assignment link", display_text="Open")})
    st.caption("Submission states reflect the last import. Overdue is computed from the current time; due dates display in your configured timezone.")
    with st.expander("Add or update an individual calendar event"):
        selected_id = st.selectbox("Assignment", [a.id for a in filtered],
                                   format_func=lambda item_id: next(f"[{a.course_code}] {a.title}" for a in filtered if a.id == item_id))
        selected = next(a for a in filtered if a.id == selected_id)
        calendar_button(selected, f"tracker_calendar_{selected.id}")
        st.caption("Use Update after a deadline changes. Calendar events run from one hour before the deadline until the deadline.")
        if selected.origin == AssignmentOrigin.EMAIL:
            completed = selected.status == AssignmentStatus.SUBMITTED
            label = "Reopen email task" if completed else "Mark email task completed"
            if st.button(label, key=f"task_complete_{selected.id}"):
                with SessionLocal() as session:
                    task = session.get(Assignment, selected.id)
                    if task is not None:
                        task.status = AssignmentStatus.NOT_SUBMITTED if completed else AssignmentStatus.SUBMITTED
                        session.commit()
                flash("Email task reopened." if completed else "Email task completed.")


def draw_email_actions(items: list[EmailActionItem]) -> None:
    with st.expander(f"Email action drawer · {len(items)} pending", expanded=True):
        st.caption("Review extracted dates before adding tasks. Email parsing uses heuristics and can miss or misread a deadline.")
        if not items:
            st.info("No pending email action items.")
        for item in items:
            with st.container(border=True):
                st.write(f"**{item.subject}**")
                st.caption(f"{item.sender} · {local_time(item.received_at)} · {item.extracted_type.value.title()}")
                st.write(item.summary)
                if item.suggested_deadline:
                    st.write(f"Suggested deadline: **{local_time(item.suggested_deadline)}**")
                else:
                    st.caption("No definite deadline found. Review this message in Outlook.")
                course = st.text_input("Task course code", value="EMAIL", key=f"email_course_{item.id}", max_chars=40)
                left, right = st.columns(2)
                if left.button("Add to Tasks", key=f"email_convert_{item.id}", disabled=not item.suggested_deadline):
                    try:
                        with SessionLocal() as session:
                            convert_email_to_assignment(session, item.id, course_code=course.strip() or "EMAIL", course_name="Email tasks")
                            session.commit()
                        flash("Email action item added to the assignment tracker.")
                    except Exception as exc:
                        st.error(f"Could not add task: {exc}")
                if right.button("Dismiss", key=f"email_dismiss_{item.id}"):
                    try:
                        with SessionLocal() as session:
                            dismiss_email(session, item.id)
                            session.commit()
                        flash("Email action item dismissed.")
                    except Exception as exc:
                        st.error(f"Could not dismiss action item: {exc}")


def main() -> None:
    st.set_page_config(page_title="HuskySync", page_icon="🐾", layout="wide")
    init_db()
    if notice := st.session_state.pop("husky_sync_notice", None):
        st.success(notice)
    draw_header()
    with SessionLocal() as session:
        assignments = list(session.scalars(select(Assignment).order_by(Assignment.due_date)))
        items = list(session.scalars(select(EmailActionItem).where(EmailActionItem.status == EmailActionStatus.PENDING)
                                    .order_by(EmailActionItem.received_at.desc())))
        # Keep the read session open during rendering; mutations use separate short sessions.
        draw_critical_alerts(assignments)
        draw_assignment_tracker(assignments)
        draw_email_actions(items)


if __name__ == "__main__":
    main()
