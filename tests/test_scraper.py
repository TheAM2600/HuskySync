"""Scraper fixtures and browser-lifecycle checks; no live account access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.database import create_session_factory
from app.models import Assignment, AssignmentOrigin, AssignmentStatus
from app.services import upsert_assignment
from app.scraper import (
    AuthenticationRequired,
    BrowserProfileBusy,
    ParseResult,
    ProfileLock,
    _browser_options,
    _check_authentication,
    _connect_urls,
    _merge_observations,
    parse_due_date,
    parse_html,
    sync_huskyct,
)


NOW = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
BASE = "https://huskyct.uconn.edu"


def assignment_html(
    *,
    item_id: str = "_456_1",
    course: str = "CSE 3100",
    title: str = "Homework 4",
    status: str | None = "Not Submitted",
    deadline: str = "2026-10-10T23:59:00-04:00",
    href: str = "/ultra/courses/_123_1/cl/outline?contentId=_456_1",
) -> str:
    status_markup = f'<span class="submission-status">{status}</span>' if status is not None else ""
    return f'''<article data-assignment-id="{item_id}" data-course-code="{course}">
      <span class="course-name">{course} Systems Programming</span>
      <h3 class="assignment-title"><a href="{href}">{title}</a></h3>
      <time datetime="{deadline}">Due tomorrow</time>{status_markup}
    </article>'''


class ParserTests(unittest.TestCase):
    def parse(self, html: str, **kwargs):
        return parse_html(html, base_url=BASE, now=NOW, **kwargs)

    def test_titles_courses_dates_and_submission_states(self):
        html = "".join([
            assignment_html(item_id="1", href="/assignment/1", title="Submitted work", status="Submitted"),
            assignment_html(item_id="2", href="/assignment/2", title="Pending work", status="Not Submitted"),
            assignment_html(item_id="3", href="/assignment/3", title="Late work", status="Past Due", deadline="October 8, 2026 at 11:59 PM"),
        ])
        result = self.parse(html)
        self.assertEqual(len(result.assignments), 3)
        self.assertEqual([item.status for item in result.assignments], [
            AssignmentStatus.SUBMITTED, AssignmentStatus.NOT_SUBMITTED, AssignmentStatus.OVERDUE,
        ])
        self.assertEqual(result.assignments[0].course_code, "CSE 3100")
        self.assertEqual(result.assignments[0].due_date, datetime(2026, 10, 11, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(result.assignments[2].due_date, datetime(2026, 10, 9, 3, 59, tzinfo=timezone.utc))

    def test_title_cannot_be_submission_evidence(self):
        for title in ["Completed worksheet", "Submitted", "Not Submitted chapter review"]:
            result = self.parse(assignment_html(title=title, status=None))
            self.assertEqual(result.assignments[0].status, AssignmentStatus.NOT_SUBMITTED)
            self.assertFalse(any(result.submission_observed.values()))

    def test_opened_publisher_launch_is_not_a_submission(self):
        result = self.parse(assignment_html(
            href="https://connect.mheducation.com/course/456", title="McGraw-Hill Connect chapter 4", status="Opened",
        ))
        item = result.assignments[0]
        self.assertEqual(item.origin, AssignmentOrigin.MCGRAW_CONNECT)
        self.assertTrue(item.is_external)
        self.assertEqual(item.status, AssignmentStatus.NOT_SUBMITTED)
        self.assertFalse(any(result.submission_observed.values()))

    def test_lti_launch_is_flagged_even_without_a_publisher_url(self):
        result = self.parse(assignment_html(
            href="/webapps/blackboard/execute/blti/launchLink?course_id=_123_1&content_id=_456_1",
            title="McGraw-Hill Connect problem set",
        ))
        self.assertTrue(result.assignments[0].is_external)
        self.assertEqual(result.assignments[0].origin, AssignmentOrigin.MCGRAW_CONNECT)

    def test_publisher_launch_without_due_time_is_flagged_and_skipped(self):
        result = self.parse(assignment_html(title="McGraw-Hill Connect", deadline="October 10, 2026"))
        self.assertEqual(result.assignments, [])
        self.assertEqual(result.skipped, 1)
        self.assertTrue(any("external publisher launch" in warning for warning in result.warnings))

    def test_missing_or_malformed_due_dates_are_not_invented(self):
        for value in ["", "Friday", "11:59 PM", "October 99 at 11:59 PM", "October 10, 2026"]:
            result = self.parse(assignment_html(deadline=value))
            self.assertEqual(result.assignments, [], value)
            self.assertEqual(result.skipped, 1, value)

    def test_changed_deadline_and_route_keep_identity(self):
        stream = self.parse(assignment_html(), source_route="/ultra/stream")
        grades = self.parse(assignment_html(
            deadline="October 12, 2026 at 11:59 PM",
            href="/webapps/assignment/uploadAssignment?course_id=_123_1&content_id=_456_1",
            course="cse3100",
        ), source_route="/ultra/grades")
        self.assertEqual(stream.assignments[0].source_key, grades.assignments[0].source_key)
        self.assertEqual(stream.assignments[0].course_code, grades.assignments[0].course_code)
        self.assertNotEqual(stream.assignments[0].due_date, grades.assignments[0].due_date)

    def test_title_course_fallback_identity_excludes_due_date(self):
        first = self.parse(assignment_html().replace('data-assignment-id="_456_1"', 'class="assignment-row"').replace("?contentId=_456_1", ""))
        second = self.parse(assignment_html(deadline="October 12, 2026 at 11:59 PM").replace('data-assignment-id="_456_1"', 'class="assignment-row"').replace("?contentId=_456_1", ""))
        self.assertEqual(first.assignments[0].source_key, second.assignments[0].source_key)

    def test_duplicate_snapshots_keep_explicit_submission_status(self):
        aggregate = ParseResult()
        _merge_observations(aggregate, self.parse(assignment_html(status="Submitted")))
        _merge_observations(aggregate, self.parse(assignment_html(status=None)))
        self.assertEqual(len(aggregate.assignments), 1)
        self.assertEqual(aggregate.assignments[0].status, AssignmentStatus.SUBMITTED)
        self.assertTrue(all(aggregate.submission_observed.values()))
        _merge_observations(aggregate, self.parse(assignment_html(status="Past Due")))
        self.assertEqual(aggregate.assignments[0].status, AssignmentStatus.SUBMITTED)

    def test_duplicate_snapshots_add_status_evidence_to_unknown(self):
        aggregate = self.parse(assignment_html(status=None))
        _merge_observations(aggregate, self.parse(assignment_html(status="Past Due")))
        self.assertEqual(aggregate.assignments[0].status, AssignmentStatus.OVERDUE)

    def test_duplicate_items_in_same_snapshot_keep_submission_evidence(self):
        result = self.parse(assignment_html(status=None) + assignment_html(status="Submitted"))
        self.assertEqual(len(result.assignments), 1)
        self.assertEqual(result.assignments[0].status, AssignmentStatus.SUBMITTED)

    def test_same_title_in_different_courses_does_not_merge(self):
        result = self.parse(assignment_html(course="CSE 3100", href="/assignment/1", item_id="1") + assignment_html(course="MATH 2110", href="/assignment/1", item_id="1"))
        self.assertEqual(len(result.assignments), 2)

    def test_nested_selectors_do_not_duplicate_assignment(self):
        html = assignment_html().replace('<time datetime=', '<time data-content-id="_456_1" data-due-date="2026-10-10T23:59:00-04:00" datetime=')
        result = self.parse(html)
        self.assertEqual(len(result.assignments), 1)

    def test_sensitive_launch_query_is_not_stored(self):
        result = self.parse(assignment_html(href="/assignment/1?token=private&nonce=oneuse&content_id=_456_1"))
        url = result.assignments[0].direct_url
        self.assertNotIn("private", url)
        self.assertNotIn("nonce", url)
        self.assertIn("content_id=", url)

    def test_unknown_course_is_visible(self):
        html = assignment_html().replace('data-course-code="CSE 3100"', "").replace("CSE 3100 Systems Programming", "Systems Programming")
        result = self.parse(html)
        self.assertEqual(result.assignments[0].course_code, "UNKNOWN")
        self.assertTrue(any("UNKNOWN" in warning for warning in result.warnings))

    def test_configurable_container_selector(self):
        with patch.dict("os.environ", {"HUSKYSYNC_ASSIGNMENT_SELECTORS": ".institution-row"}):
            html = assignment_html().replace('data-assignment-id="_456_1"', 'class="institution-row"')
            self.assertEqual(len(self.parse(html).assignments), 1)


class DeadlineTests(unittest.TestCase):
    def test_explicit_old_dates_do_not_roll_forward(self):
        self.assertEqual(parse_due_date("October 1 at 11:59 PM", now=NOW), datetime(2026, 10, 2, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(parse_due_date("10/1 at 11:59 PM", now=NOW), datetime(2026, 10, 2, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(parse_due_date("October 1, 2025 at 11:59 PM", now=NOW), datetime(2025, 10, 2, 3, 59, tzinfo=timezone.utc))

    def test_weekday_on_current_day_means_tonight(self):
        self.assertEqual(parse_due_date("Friday at 11:59 PM", now=NOW), datetime(2026, 10, 10, 3, 59, tzinfo=timezone.utc))

    def test_future_and_overdue_weekdays(self):
        self.assertEqual(parse_due_date("Monday at 11:59 PM", now=NOW), datetime(2026, 10, 13, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(parse_due_date("Monday at 11:59 PM", now=NOW, overdue=True), datetime(2026, 10, 6, 3, 59, tzinfo=timezone.utc))
        self.assertEqual(parse_due_date("next Friday at 11:59 PM", now=NOW), datetime(2026, 10, 17, 3, 59, tzinfo=timezone.utc))

    def test_relative_days_and_explicit_timezone(self):
        self.assertEqual(parse_due_date("Tomorrow at 5 PM", now=NOW), datetime(2026, 10, 10, 21, tzinfo=timezone.utc))
        self.assertEqual(parse_due_date("2026-10-10T23:59Z", now=NOW), datetime(2026, 10, 10, 23, 59, tzinfo=timezone.utc))

    def test_daylight_saving_uses_institution_timezone(self):
        self.assertEqual(parse_due_date("November 2, 2026 at 11:59 PM", now=NOW), datetime(2026, 11, 3, 4, 59, tzinfo=timezone.utc))


class BrowserConfigurationTests(unittest.TestCase):
    def test_profile_lock_is_nonblocking_and_released(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "browser.lock"
            with ProfileLock(path):
                with self.assertRaises(BrowserProfileBusy):
                    with ProfileLock(path):
                        pass
            with ProfileLock(path):
                self.assertTrue(path.exists())

    def test_connect_urls_reject_nonpublisher_and_insecure_urls(self):
        for url in ["https://evil.example/", "https://mheducation.com.evil.example/", "http://connect.mheducation.com/", "https://user:password@connect.mheducation.com/"]:
            with self.assertRaises(ValueError, msg=url):
                _connect_urls([url])
        self.assertEqual(_connect_urls(["https://connect.mheducation.com/assignments"]), ["https://connect.mheducation.com/assignments"])

    def test_system_browser_override_is_opt_in(self):
        with patch.dict("os.environ", {"HUSKYSYNC_BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium"}):
            options = _browser_options(True)
        self.assertEqual(options["executable_path"], "/usr/bin/chromium")
        self.assertTrue(options["headless"])
        self.assertNotIn("args", options)

    def test_authentication_redirect_produces_actionable_error(self):
        class Locator:
            async def count(self):
                return 0

            async def inner_text(self, **kwargs):
                return "UConn NetID Login"

        page = SimpleNamespace(url="https://login.uconn.edu/", locator=lambda selector: Locator())
        with self.assertRaisesRegex(AuthenticationRequired, "python -m app.scraper login"):
            asyncio.run(_check_authentication(page))


def test_sync_preserves_persisted_submission_without_new_evidence_and_upserts(tmp_path):
    factory = create_session_factory(tmp_path / "scraper.sqlite3")
    submitted = parse_html(assignment_html(status="Submitted"), base_url=BASE, now=NOW)
    with factory() as session:
        item = upsert_assignment(session, submitted.assignments[0])
        item_id = item.id
        item.calendar_event_id = "existing-calendar-event"
        session.commit()

    fake_context = SimpleNamespace(pages=[object()], close=AsyncMock())
    scope = AsyncMock()
    scope.__aenter__.return_value = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=fake_context))
    )
    fake_settings = SimpleNamespace(
        browser_lock_path=tmp_path / "browser.lock", browser_profile_dir=tmp_path / "browser",
        blackboard_base_url=BASE, timezone="America/New_York",
    )
    try:
        for status, expected in [(None, AssignmentStatus.SUBMITTED), ("Not Submitted", AssignmentStatus.NOT_SUBMITTED)]:
            incoming = parse_html(assignment_html(status=status, deadline="October 12, 2026 at 11:59 PM"), base_url=BASE, now=NOW)
            with (
                patch("app.scraper.SessionLocal", factory),
                patch("app.scraper.init_db"),
                patch("app.scraper.ensure_local_directories"),
                patch("app.scraper.settings", fake_settings),
                patch("app.scraper.async_playwright", return_value=scope),
                patch("app.scraper._snapshot_page", new=AsyncMock(return_value=incoming)),
            ):
                result = asyncio.run(sync_huskyct(connect_urls=[]))
            assert result.assignments_saved == 1
            with factory() as session:
                records = session.scalars(select(Assignment)).all()
                assert len(records) == 1
                assert records[0].id == item_id
                assert records[0].status == expected
                assert records[0].calendar_event_id == "existing-calendar-event"
                assert records[0].due_date == datetime(2026, 10, 13, 3, 59, tzinfo=timezone.utc)
        assert fake_context.close.await_count == 2
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    unittest.main()
