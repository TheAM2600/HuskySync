"""Best-effort DOM scraping using the user's saved, interactive browser session.

Blackboard Ultra and publisher pages change between institutions and releases.
The selectors below are intentionally configurable and parsers are fixture tested;
they have not been certified against a live UConn account. This module never
automates credentials or Duo approval and does not call private Blackboard APIs.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
import dateparser
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from pydantic import ValidationError
from sqlalchemy import select

from .config import ensure_local_directories, settings
from .database import SessionLocal, init_db
from .models import Assignment, AssignmentOrigin, AssignmentStatus
from .services import AssignmentInput, upsert_assignment


DEFAULT_ASSIGNMENT_SELECTORS = (
    "[data-assignment-id]",
    "[data-assessment-id]",
    "[data-content-id][data-due-date]",
    "[data-testid='assignment-row']",
    "[data-test-id='assignment-row']",
    "[data-testid='activity-stream-item']",
    "[data-test-id='activity-stream-item']",
    "[data-bbtype='activity-stream-item']",
    "[data-calendar-event]",
    ".assignment-row",
    ".assignment-item",
    ".assignment-card",
    ".activity-stream-entry",
    ".stream-entry",
    ".calendar-event",
    "tr[data-due-date]",
    # Blackboard Ultra's calendar "Due Dates" view, as served at UConn.
    ".element-card.due-item",
)
DEADLINE_VIEW_BUTTON = ".js-viewSwitch-deadline-button"
GRADEBOOK_ROW_SELECTOR = "tr[data-testid^='course-student-grades-table-row-']"
TITLE_SELECTORS = (
    "[data-assignment-title]", "[data-testid='assignment-title']",
    "[data-test-id='assignment-title']", ".assignment-title", ".event-title",
    ".stream-item-title", "h1", "h2", "h3", "h4", ".due-item .name a",
)
COURSE_SELECTORS = (
    "[data-course-code]", "[data-course-name]", "[data-testid='course-name']",
    "[data-test-id='course-name']", ".course-name", ".course-title", ".course-code",
    "a[analytics-id$='openCourseOutline']",
)
DUE_SELECTORS = (
    "[data-due-date]", "time[datetime]", "[data-testid='due-date']",
    "[data-test-id='due-date']", ".due-date", ".dueDate", ".deadline",
)
STATUS_SELECTORS = (
    "[data-submission-status]", "[data-status]", "[data-testid='submission-status']",
    "[data-test-id='submission-status']", ".submission-status", ".assignment-status",
    ".status",
)
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,8})[ \-]*(\d{3,4}[A-Z]?)\b", re.I)
DUE_LABEL_RE = re.compile(r"\b(?:due(?:\s+(?:date|on|by|at))?|deadline(?:\s+(?:is|on|at))?)\s*[:\-]?\s*(.+)", re.I)
PUBLISHER_RE = re.compile(r"\bMcGraw[\s-]*Hill\b|\bmheducation\b", re.I)
PUBLISHER_DOMAINS = ("mheducation.com", "mcgraw-hill.com", "mhhe.com")
SENSITIVE_QUERY_KEYS = {"access_token", "id_token", "token", "oauth_token", "password", "session_id", "nonce"}
BB_ROUTES = ("/ultra/stream", "/ultra/grades", "/ultra/calendar")


class AuthenticationRequired(RuntimeError):
    """The user needs to complete browser authentication interactively."""


class BrowserProfileBusy(RuntimeError):
    """Another HuskySync operation is using the persistent Chromium profile."""


@dataclass
class ParseResult:
    assignments: list[AssignmentInput] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    submission_observed: dict[str, bool] = field(default_factory=dict)


@dataclass
class ScrapeResult:
    assignments_seen: int = 0
    assignments_saved: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class ProfileLock(AbstractContextManager["ProfileLock"]):
    """Nonblocking OS lock, released automatically even when a process exits.

    Keep the lock file in place: deleting it allows two processes to lock distinct
    inodes at the same path. Chromium's own profile lock is a second safeguard.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle: Any = None

    def __enter__(self) -> "ProfileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise BrowserProfileBusy(
                "The browser profile is in use. Finish the other Sync HuskyCT or "
                "login operation, close its browser, and try again."
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, *args: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _first(node: Tag, selectors: tuple[str, ...]) -> Tag | None:
    for selector in selectors:
        match = node.select_one(selector)
        if match is not None:
            return match
    return None


def _attr_or_text(node: Tag | None, *attributes: str) -> str:
    if node is None:
        return ""
    for attr in attributes:
        value = node.get(attr)
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
    return _clean_text(node.get_text(" ", strip=True))


def _safe_url(href: str, base_url: str) -> str:
    parsed = urlparse(urljoin(base_url, href))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    # Do not retain one-use launch tokens in SQLite or Calendar descriptions.
    query = [(key, value) for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
             if key.lower() not in SENSITIVE_QUERY_KEYS for value in values]
    return urlunparse(parsed._replace(query=urlencode(query)))


def _publisher_host(hostname: str | None) -> bool:
    hostname = (hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith("." + domain) for domain in PUBLISHER_DOMAINS)


def _assignment_link(node: Tag, base_url: str) -> str:
    direct = node.get("data-assignment-url")
    if isinstance(direct, str):
        return _safe_url(direct, base_url)
    candidates = node.select("a[href]")
    # Prefer assignment/publisher links over the course-navigation link.
    for link in candidates:
        href = str(link.get("href", ""))
        parsed = urlparse(urljoin(base_url, href))
        if _publisher_host(parsed.hostname) or re.search(r"content[_-]?id|assessment|assignment|/outline", href, re.I):
            return _safe_url(href, base_url)
    title = _first(node, TITLE_SELECTORS)
    if title is not None:
        link = title if title.name == "a" else title.select_one("a[href]")
        if link is not None:
            return _safe_url(str(link.get("href", "")), base_url)
    # A single non-course link is useful; avoid choosing a random action link.
    useful = [link for link in candidates if not any(link in course.parents or course in link.parents or course == link for course in node.select(",".join(COURSE_SELECTORS)))]
    if len(useful) == 1:
        return _safe_url(str(useful[0].get("href", "")), base_url)
    return ""


def _identity(node: Tag, url: str, course_code: str, title: str, origin: AssignmentOrigin) -> str:
    prefix = "connect" if origin == AssignmentOrigin.MCGRAW_CONNECT else "blackboard"
    parsed = urlparse(url)
    query = {key.lower(): values[0] for key, values in parse_qs(parsed.query).items() if values}
    course_match = re.search(r"/courses/([^/]+)", parsed.path)
    course_id = query.get("course_id") or query.get("courseid") or (course_match.group(1) if course_match else course_code)
    item_id = query.get("content_id") or query.get("contentid") or query.get("assessment_id") or query.get("assessmentid")
    if not item_id:
        for attr in ("data-assignment-id", "data-assessment-id", "data-content-id"):
            value = node.get(attr)
            if isinstance(value, str) and value.strip():
                item_id = value.strip()
                break
    if item_id:
        return f"{prefix}:{course_id}:{item_id}"
    # Excluding deadline/status/route means a deadline change updates one item.
    signature = f"{course_code.casefold()}\0{title.casefold()}"
    return f"{prefix}:fallback:{hashlib.sha256(signature.encode()).hexdigest()}"


def _status(node: Tag) -> tuple[AssignmentStatus, bool]:
    explicit = str(node.get("data-submission-status") or node.get("data-status") or "")
    status_node = _first(node, STATUS_SELECTORS)
    label = _clean_text(explicit or _attr_or_text(status_node, "data-submission-status", "data-status"))
    if not label:
        # Only standalone labels outside titles are submission evidence. An
        # assignment titled "Completed worksheet" is not a completed submission.
        title_nodes = node.select(",".join((*TITLE_SELECTORS, "a[href]")))
        labels = []
        for fragment in node.find_all(string=True):
            if any(title == fragment.parent or title in fragment.parents for title in title_nodes):
                continue
            text = _clean_text(str(fragment))
            if re.fullmatch(r"(?:not\s+submitted|unsubmitted|past\s+due|overdue|submitted|completed)", text, re.I):
                labels.append(text)
        label = " ".join(labels)
    normalized = re.sub(r"[_-]+", " ", label).casefold()
    if re.search(r"\b(?:not submitted|unsubmitted|not completed|incomplete)\b", normalized):
        return AssignmentStatus.NOT_SUBMITTED, True
    if re.search(r"\b(?:submitted|completed)\b", normalized):
        return AssignmentStatus.SUBMITTED, True
    if re.search(r"\b(?:past due|overdue)\b", normalized):
        return AssignmentStatus.OVERDUE, True
    return AssignmentStatus.NOT_SUBMITTED, False


def parse_due_date(raw: str, *, now: datetime | None = None, overdue: bool = False) -> datetime | None:
    """Parse a published deadline, returning UTC; never infer a missing date.

    A date without a clock time is not an exact deadline and is skipped. This is
    preferable to silently placing an incorrectly timed event on the calendar.
    """
    text = _clean_text(raw)
    text = re.sub(r"^(?:due(?:\s+(?:date|on|by|at))?|deadline(?:\s+(?:is|on|at))?)\s*[:\-]?\s*", "", text, flags=re.I)
    text = re.sub(r"\b(?:Eastern Time|ET)\b", "", text, flags=re.I).strip()
    if not text or len(text) > 180:
        return None
    months = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    month_day = re.search(r"\b" + months + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)", text, re.I)
    day_month = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + months + r"\b", text, re.I)
    numeric = re.search(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})(?!\d)", text)
    if month_day and not 1 <= int(month_day.group(1)) <= 31:
        return None
    if day_month and not 1 <= int(day_month.group(1)) <= 31:
        return None
    # ISO dates are validated by dateparser; do not read their month/day as a
    # standalone MDY numeric date (e.g. 2026-10-31).
    iso_date = bool(re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", text))
    if numeric and not iso_date and not (1 <= int(numeric.group(1)) <= 12 and 1 <= int(numeric.group(2)) <= 31):
        return None
    has_date = iso_date or bool(numeric or month_day or day_month) or bool(re.search(
        r"\b(?:today|tomorrow|yesterday|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", text, re.I,
    ))
    has_time = bool(re.search(r"(?<!\d)\d{1,2}:\d{2}|\b\d{1,2}\s*(?:am|pm)\b|\b(?:noon|midnight)\b", text, re.I))
    if not has_date or not has_time:
        return None
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings.timezone))
    # An explicit semester date without a year stays in the reference year.
    # Only a weekday phrase needs future/past interpretation.
    weekday_only = bool(re.search(r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", text, re.I)) and not bool(re.search(r"\d{1,2}[/.-]\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", text, re.I))
    preference = ("past" if overdue else "future") if weekday_only else "current_period"
    if weekday_only:
        modifier = re.search(r"\b(next|last|this)\s+(?=(?:mon|tue|wed|thu|fri|sat|sun))", text, re.I)
        if modifier:
            preference = {"next": "future", "last": "past", "this": "current_period"}[modifier.group(1).lower()]
            text = text[:modifier.start()] + text[modifier.end():]
        elif re.search(r"\b" + local_now.strftime("%a") + r"(?:day|sday|nesday|rsday|urday)?\b", text, re.I):
            # dateparser's future preference always advances a bare Friday on
            # Friday by a week, even when tonight's deadline is still ahead.
            preference = "current_period"
    parsed = dateparser.parse(text, languages=["en"], settings={
        "TIMEZONE": settings.timezone,
        "TO_TIMEZONE": "UTC",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": local_now,
        "PREFER_DATES_FROM": preference,
        "DATE_ORDER": "MDY",
        "PREFER_LOCALE_DATE_ORDER": False,
    })
    return parsed.astimezone(timezone.utc) if parsed is not None else None


def parse_html(
    html: str,
    *,
    base_url: str,
    source_route: str = "",
    now: datetime | None = None,
    selectors: tuple[str, ...] | None = None,
    publisher: bool = False,
) -> ParseResult:
    """Extract scoped assignment records from a rendered DOM snapshot.

    Set HUSKYSYNC_ASSIGNMENT_SELECTORS to a comma-separated selector list when an
    institution's DOM differs. Only items with a title and an explicit, parseable
    date AND clock time are saved. Missing course labels are visibly flagged.
    """
    soup = BeautifulSoup(html, "html.parser")
    if selectors is None:
        override = os.getenv("HUSKYSYNC_ASSIGNMENT_SELECTORS", "").strip()
        selectors = tuple(part.strip() for part in override.split(",") if part.strip()) if override else DEFAULT_ASSIGNMENT_SELECTORS
    result = ParseResult()
    try:
        nodes = soup.select(",".join(selectors))
    except Exception as exc:
        raise ValueError("HUSKYSYNC_ASSIGNMENT_SELECTORS contains an invalid CSS selector.") from exc
    # Some selectors match both a parent item and nested content. Parse only the
    # outermost matching container, so deadline fragments do not become tasks.
    node_ids = {id(node) for node in nodes}
    containers = [node for node in nodes if not any(id(parent) in node_ids for parent in node.parents)]
    missing_course = 0
    missing_publisher_deadline = 0
    for node in containers:
        title = _attr_or_text(node, "data-assignment-title") if node.has_attr("data-assignment-title") else _attr_or_text(_first(node, TITLE_SELECTORS), "data-assignment-title")
        if not title:
            link = node.select_one("a[data-assignment-url], a[aria-label]")
            title = _attr_or_text(link, "aria-label")
        if not title:
            assignment_url = _assignment_link(node, base_url)
            for link in node.select("a[href]"):
                if assignment_url and _safe_url(str(link.get("href", "")), base_url) == assignment_url:
                    title = _attr_or_text(link)
                    break
        course_node = _first(node, COURSE_SELECTORS)
        course_name = str(node.get("data-course-name") or _attr_or_text(course_node, "data-course-name", "data-course-code"))
        course_code = str(node.get("data-course-code") or "")
        if not course_code and course_node is not None:
            course_code = str(course_node.get("data-course-code") or "")
        match = COURSE_CODE_RE.search(course_code or course_name)
        if match:
            course_code = f"{match.group(1).upper()} {match.group(2).upper()}"
        elif not course_code:
            course_code = "UNKNOWN"
        course_name = _clean_text(course_name) or course_code
        status, submission_observed = _status(node)
        due_node = _first(node, DUE_SELECTORS)
        raw_due = str(node.get("data-due-date") or _attr_or_text(due_node, "data-due-date", "datetime"))
        if not raw_due:
            for line in node.stripped_strings:
                match = DUE_LABEL_RE.search(str(line))
                if match:
                    raw_due = match.group(1)
                    break
        due_date = parse_due_date(raw_due, now=now, overdue=status in {AssignmentStatus.OVERDUE, AssignmentStatus.SUBMITTED})
        # Ultra renders the due-item course link without its "courses" segment.
        direct_url = re.sub(r"/ultra//(?=_\d+_\d+/)", "/ultra/courses/", _assignment_link(node, base_url))
        text = node.get_text(" ", strip=True)
        is_external = publisher or _publisher_host(urlparse(direct_url).hostname) or bool(PUBLISHER_RE.search(text))
        if not title or due_date is None:
            result.skipped += 1
            if is_external and due_date is None:
                missing_publisher_deadline += 1
            continue
        origin = AssignmentOrigin.MCGRAW_CONNECT if is_external else AssignmentOrigin.BLACKBOARD
        source_key = _identity(node, direct_url, course_name if course_code == "UNKNOWN" else course_code, title, origin)
        try:
            item = AssignmentInput(
                course_code=course_code,
                course_name=course_name,
                title=title,
                due_date=due_date,
                origin=origin,
                direct_url=direct_url,
                status=status,
                source_key=source_key,
                is_external=is_external,
            )
        except ValidationError:
            result.skipped += 1
            continue
        key = _assignment_key(item)
        if key not in result.submission_observed and course_code == "UNKNOWN":
            missing_course += 1
        _merge_observations(result, ParseResult(
            assignments=[item], submission_observed={key: submission_observed},
        ))
    if result.skipped:
        result.warnings.append(f"{source_route or 'Page'}: skipped {result.skipped} item(s) with no title or no explicit parseable deadline time.")
    if missing_course:
        result.warnings.append(f"{source_route or 'Page'}: {missing_course} assignment(s) have no course code in the page; shown as UNKNOWN.")
    if missing_publisher_deadline:
        result.warnings.append(f"{source_route or 'Page'}: flagged {missing_publisher_deadline} external publisher launch(es) with no usable deadline. Open Connect during interactive login and configure its assignment-list URL; a launch alone supplies no publisher submission status.")
    return result


def parse_gradebook_html(html: str, *, base_url: str, source_route: str = "", now: datetime | None = None) -> ParseResult:
    """Extract past and submitted work from one course's Ultra gradebook table.

    The table lists a due date without a clock time. Finished or past-due rows
    are recorded at 11:59 PM local time; upcoming unsubmitted rows are left to
    the calendar's Due Dates view, which carries the exact deadline.
    """
    soup = BeautifulSoup(html, "html.parser")
    result = ParseResult()
    page_title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    course_name = _clean_text(page_title.split("/", 1)[1]) if "/" in page_title else ""
    match = COURSE_CODE_RE.search(course_name)
    course_code = f"{match.group(1).upper()} {match.group(2).upper()}" if match else "UNKNOWN"
    course_name = course_name or course_code
    current = now or datetime.now(timezone.utc)
    for row in soup.select(GRADEBOOK_ROW_SELECTOR):
        cells = {str(cell.get("aria-describedby", "")).rsplit("-", 1)[-1]: cell for cell in row.find_all("td")}
        title = _attr_or_text(row.select_one("[id^='course-student-grades-item-name-']"))
        raw_due = _attr_or_text(cells.get("dueDate"))
        if not title or not raw_due:
            continue
        detail = f"{_attr_or_text(cells.get('status'))} {_attr_or_text(row.select_one('[data-testid=item-description]'))}"
        graded = cells.get("grade") is not None and cells["grade"].select_one(".js-pill-grade") is not None
        submitted = graded or bool(re.search(r"\bgraded\b|\bsubmitted\b|\bparticipated\b|\bcompleted?\b", detail, re.I))
        status = AssignmentStatus.SUBMITTED if submitted else AssignmentStatus.NOT_SUBMITTED
        due_date = parse_due_date(raw_due, now=now, overdue=True) or parse_due_date(f"{raw_due} 11:59 PM", now=now, overdue=True)
        if due_date is None:
            result.skipped += 1
            continue
        if not submitted and due_date > current:
            continue
        # Matches the fallback identity of the same item in the calendar view.
        signature = f"{(course_name if course_code == 'UNKNOWN' else course_code).casefold()}\0{title.casefold()}"
        try:
            item = AssignmentInput(
                course_code=course_code,
                course_name=course_name,
                title=title,
                due_date=due_date,
                origin=AssignmentOrigin.BLACKBOARD,
                direct_url=_safe_url(source_route, base_url),
                status=status,
                source_key=f"blackboard:fallback:{hashlib.sha256(signature.encode()).hexdigest()}",
                is_external=False,
            )
        except ValidationError:
            result.skipped += 1
            continue
        _merge_observations(result, ParseResult(assignments=[item], submission_observed={_assignment_key(item): True}))
    if result.skipped:
        result.warnings.append(f"{source_route or 'Gradebook'}: skipped {result.skipped} item(s) with an unreadable due date.")
    return result


def _assignment_key(item: AssignmentInput) -> str:
    return f"{item.origin.value}\0{item.course_code}\0{item.source_key}"


def _merge_observations(target: ParseResult, incoming: ParseResult) -> None:
    """Combine views without downgrading explicit grades to calendar defaults."""
    items = {_assignment_key(item): item for item in target.assignments}
    ranks = {AssignmentStatus.SUBMITTED: 3, AssignmentStatus.OVERDUE: 2, AssignmentStatus.NOT_SUBMITTED: 1}
    for item in incoming.assignments:
        key = _assignment_key(item)
        known = incoming.submission_observed.get(key, False)
        existing = items.get(key)
        existing_known = target.submission_observed.get(key, False)
        if existing is not None and existing_known and (not known or ranks[existing.status] > ranks[item.status]):
            item = item.model_copy(update={"status": existing.status})
        items[key] = item
        target.submission_observed[key] = existing_known or known
    target.assignments = list(items.values())
    target.skipped += incoming.skipped
    target.warnings.extend(warning for warning in incoming.warnings if warning not in target.warnings)


async def _check_authentication(page: Any, *, publisher: bool = False) -> None:
    parsed = urlparse(page.url)
    expected = _publisher_host(parsed.hostname) if publisher else parsed.hostname == urlparse(settings.blackboard_base_url).hostname
    login_route = bool(re.search(r"/(?:webapps/login|login|signin|sign-in)(?:[/.?]|$)", parsed.path, re.I))
    password_field = await page.locator("input[type='password'], input[name='j_username'], input[name='username']").count()
    content = (await page.locator("body").inner_text(timeout=10_000)).casefold()
    expired = any(phrase in content for phrase in ("your session has expired", "sign in to blackboard", "you are not currently logged in"))
    if not expected or login_route or password_field or expired:
        platform = "The publisher" if publisher else "HuskyCT"
        raise AuthenticationRequired(
            f"{platform} needs an interactive login. Run `python -m app.scraper login`, "
            "complete NetID SSO and Duo in the browser, then retry. Publisher sites may require "
            "a separate interactive login launched from the course."
        )


def _browser_options(headless: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": headless,
        "viewport": {"width": 1440, "height": 1050},
        "timezone_id": settings.timezone,
        "locale": "en-US",
        "accept_downloads": False,
    }
    channel = os.getenv("HUSKYSYNC_BROWSER_CHANNEL", "").strip()
    if channel:
        options["channel"] = channel
    executable = os.getenv("HUSKYSYNC_BROWSER_EXECUTABLE_PATH", "").strip()
    if executable:
        options["executable_path"] = executable
    return options


def _session_cookie_path() -> Path:
    return Path(settings.browser_profile_dir).with_name("browser_session.json")


async def _save_session_cookies(context: Any) -> None:
    """Keep session-only cookies, which Chromium discards when the browser closes.

    Blackboard's login cookie has no expiry, so the persistent profile alone
    cannot carry a login into the next launch. The file grants account access.
    """
    keys = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    cookies = [{key: cookie[key] for key in keys if key in cookie} for cookie in await context.cookies() if cookie.get("expires", -1) == -1]
    path = _session_cookie_path()
    path.write_text(json.dumps(cookies), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def _restore_session_cookies(context: Any) -> None:
    path = _session_cookie_path()
    if not path.exists():
        return
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if cookies:
            await context.add_cookies(cookies)
    except (OSError, ValueError, PlaywrightError):
        # An unreadable file only means the user has to log in again.
        return


async def _wait_for_activity_stream(page: Any, *, timeout_seconds: int = 600) -> None:
    """Without a terminal to confirm in, wait until the login visibly completes."""
    host = urlparse(settings.blackboard_base_url).hostname
    for _ in range(timeout_seconds // 2):
        parsed = urlparse(page.url)
        if parsed.hostname == host and parsed.path.startswith("/ultra/"):
            return
        await page.wait_for_timeout(2_000)
    raise AuthenticationRequired("Login was not completed in time. Run `python -m app.scraper login` again.")


async def login_huskyct() -> None:
    """Open Chromium for SSO/Duo; wait for the user's explicit terminal input."""
    ensure_local_directories()
    with ProfileLock(settings.browser_lock_path):
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(settings.browser_profile_dir), **_browser_options(headless=False)
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(urljoin(settings.blackboard_base_url, "/ultra/stream"), wait_until="domcontentloaded", timeout=60_000)
                print("Complete UConn NetID SSO and Duo in the browser. Open any McGraw-Hill launches in another tab and sign in there too. Return this original tab to the HuskyCT Ultra activity stream before continuing.")
                try:
                    await asyncio.to_thread(input, "When the HuskyCT activity stream is visible, press Enter here to save the session: ")
                except EOFError:
                    # No terminal to confirm in (isatty() is unreliable for NUL on Windows).
                    await _wait_for_activity_stream(page)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
                await _check_authentication(page)
                if not urlparse(page.url).path.startswith("/ultra/"):
                    raise AuthenticationRequired(
                        "Return the original browser tab to the HuskyCT Ultra activity stream before "
                        "pressing Enter. Run `python -m app.scraper login` again to finish saving the session."
                    )
                await _save_session_cookies(context)
                print("Browser session saved locally. You can now run `python -m app.scraper sync`.")
            finally:
                await context.close()


def _connect_urls(configured: list[str] | None) -> list[str]:
    urls = configured if configured is not None else [part.strip() for part in os.getenv("HUSKYSYNC_CONNECT_URLS", "").split(",") if part.strip()]
    safe: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not _publisher_host(parsed.hostname) or parsed.username or parsed.password:
            raise ValueError("Connect URLs must use HTTPS and a mheducation.com, mcgraw-hill.com, or mhhe.com host.")
        sanitized = _safe_url(url, url)
        if sanitized not in safe:
            safe.append(sanitized)
    return safe


async def _snapshot_gradebooks(page: Any) -> ParseResult:
    """Read each course gradebook linked from the open /ultra/grades overview.

    The overview only previews a few rows per course; the per-course table is
    the one place that lists finished and past-due work with its status.
    """
    aggregate = ParseResult()
    try:
        await page.wait_for_selector("bb-base-grades-student[id^='card_']", state="attached", timeout=15_000)
    except PlaywrightTimeoutError:
        await _check_authentication(page)
        return ParseResult(warnings=["/ultra/grades: no course gradebooks were listed, so past and submitted work could not be read."])
    course_ids = list(dict.fromkeys(re.findall(r'id="card_(_\d+_\d+)"', await page.content())))
    for course_id in course_ids:
        route = f"/ultra/courses/{course_id}/grades"
        try:
            await page.goto(urljoin(settings.blackboard_base_url, route), wait_until="domcontentloaded", timeout=60_000)
            await _check_authentication(page)
            await page.wait_for_selector(GRADEBOOK_ROW_SELECTOR, state="attached", timeout=15_000)
        except AuthenticationRequired:
            raise
        except PlaywrightError:
            # A course without graded items renders no rows.
            continue
        _merge_observations(aggregate, parse_gradebook_html(await page.content(), base_url=page.url, source_route=route))
    return aggregate


async def _snapshot_page(page: Any, url: str, *, publisher: bool = False) -> ParseResult:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await _check_authentication(page, publisher=publisher)
    if not publisher and urlparse(url).path.rstrip("/").endswith("/ultra/grades"):
        return await _snapshot_gradebooks(page)
    if urlparse(url).path.rstrip("/").endswith("/ultra/calendar"):
        # The calendar opens on a schedule grid; deadlines are listed in its other view.
        try:
            await page.click(DEADLINE_VIEW_BUTTON, timeout=15_000)
        except PlaywrightError:
            pass
    configured = os.getenv("HUSKYSYNC_ASSIGNMENT_SELECTORS", "").strip()
    selector = configured or ",".join(DEFAULT_ASSIGNMENT_SELECTORS)
    try:
        await page.wait_for_selector(selector, state="attached", timeout=12_000)
    except PlaywrightTimeoutError:
        await _check_authentication(page, publisher=publisher)
        # A genuine empty course view and an unsupported DOM cannot be reliably
        # distinguished without the institution's current selectors.
        return ParseResult(warnings=[f"{urlparse(url).path}: no recognizable assignment containers. The page may be empty or selectors may need updating."])
    aggregate = ParseResult()
    await _check_authentication(page, publisher=publisher)
    passes = max(1, min(10, int(os.getenv("HUSKYSYNC_SCROLL_PASSES", "3"))))
    for index in range(passes):
        parsed = parse_html(await page.content(), base_url=page.url, source_route=urlparse(url).path, publisher=publisher)
        previous_skipped = aggregate.skipped
        _merge_observations(aggregate, parsed)
        aggregate.skipped = max(previous_skipped, parsed.skipped)
        if index < passes - 1:
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(600)
    return aggregate


async def sync_huskyct(*, headless: bool = True, connect_urls: list[str] | None = None) -> ScrapeResult:
    """Visit documented Ultra surfaces and optional already-authenticated Connect URLs.

    External launch cards are flagged, but merely opening an LTI link never proves
    submission or exposes the publisher's internal due date. Configure actual
    publisher assignment-list URLs to attempt a separate publisher DOM scrape.
    """
    ensure_local_directories()
    init_db()
    publisher_urls = _connect_urls(connect_urls)
    result = ScrapeResult()
    aggregate = ParseResult()
    with ProfileLock(settings.browser_lock_path):
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(settings.browser_profile_dir), **_browser_options(headless=headless)
            )
            try:
                await _restore_session_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                for route in BB_ROUTES:
                    try:
                        parsed = await _snapshot_page(page, urljoin(settings.blackboard_base_url, route))
                    except AuthenticationRequired:
                        raise
                    except (PlaywrightError, ValueError) as exc:
                        result.warnings.append(f"{route}: could not read this page ({type(exc).__name__}); other routes will still be checked.")
                        continue
                    _merge_observations(aggregate, parsed)
                for url in publisher_urls:
                    try:
                        parsed = await _snapshot_page(page, url, publisher=True)
                    except AuthenticationRequired:
                        result.warnings.append("A Connect assignment-list page needs an interactive publisher login. Launch it from HuskyCT during `python -m app.scraper login`.")
                        continue
                    except (PlaywrightError, ValueError) as exc:
                        result.warnings.append(f"A Connect page could not be read ({type(exc).__name__}); check its configured URL and selectors.")
                        continue
                    _merge_observations(aggregate, parsed)
            finally:
                await context.close()
    result.assignments_seen = len(aggregate.assignments)
    result.skipped = aggregate.skipped
    result.warnings.extend(aggregate.warnings)
    if not aggregate.assignments:
        result.warnings.append("No assignments with an explicit deadline time were found. This does not confirm that you have no coursework; check the live page and update selectors if needed.")
    with SessionLocal() as session:
        for item in aggregate.assignments:
            if not aggregate.submission_observed.get(_assignment_key(item), False):
                # A later calendar-only run cannot undo a previously observed
                # submission. An explicit new status is still allowed to update.
                existing = session.scalar(select(Assignment).where(
                    Assignment.origin == item.origin,
                    Assignment.course_code == item.course_code,
                    Assignment.source_key == item.source_key,
                ))
                if existing is not None and existing.status == AssignmentStatus.SUBMITTED:
                    item = item.model_copy(update={"status": AssignmentStatus.SUBMITTED})
            upsert_assignment(session, item)
            result.assignments_saved += 1
        session.commit()
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Save your interactive HuskyCT session or scrape coursework.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Open a local browser for NetID SSO and Duo.")
    sync = commands.add_parser("sync", help="Read assignments using your saved browser profile.")
    sync.add_argument("--headed", action="store_true", help="Show Chromium while scraping.")
    sync.add_argument("--connect-url", action="append", default=None, help="An authenticated Connect assignment-list URL; repeat as needed.")
    args = parser.parse_args()
    try:
        if args.command == "login":
            asyncio.run(login_huskyct())
        else:
            result = asyncio.run(sync_huskyct(headless=not args.headed, connect_urls=args.connect_url))
            print(json.dumps(asdict(result), indent=2))
    except (AuthenticationRequired, BrowserProfileBusy, PlaywrightError, ValueError) as exc:
        print(f"HuskySync: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
