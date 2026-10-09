"""Read Outlook mail without marking it read and extract reviewable action items.

Microsoft 365 normally requires OAuth2 for IMAP. Supply a delegated access token
with the Outlook ``IMAP.AccessAsUser.All`` scope. Password authentication is only
attempted when a password is explicitly configured and the tenant supports it.
Parsing is deliberately heuristic: one action item is retained per message,
date-only deadlines mean 11:59 PM in the configured university time zone, and
nothing becomes a task until the student reviews it in the dashboard.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Callable
from zoneinfo import ZoneInfo

import dateparser
from bs4 import BeautifulSoup
from dateparser.search import search_dates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import SessionLocal, init_db
from app.models import EmailActionItem, EmailActionStatus, EmailActionType


UTC = timezone.utc
ALLOWED_SENDER_DOMAINS = frozenset(
    {"uconn.edu", "blackboard.com", "mail.blackboard.com", "blackboard.uconn.edu", "huskyct.uconn.edu"}
)
DEADLINE_RE = re.compile(
    r"\b(?:deadline\b"
    r"|due\b(?!\s+to\b)(?:\s+date)?|(?:submit|complete|finish|turn\s+in)\s+by"
    r"|submission(?:s)?\s+(?:close|closes)|last\s+day)"
    r"\s*(?:(?:is|on|by|at|for)\s+|:\s*)?",
    re.IGNORECASE,
)
DEADLINE_CHANGE_RE = re.compile(
    r"\b(?:deadline|due\s+date)\b\s+"
    r"(?:(?:has\s+been|was|is)\s+)?(?:moved|extended|changed)"
    r"(?:\s+from\s+[^\n;!?]{1,120}?)?\s+(?:to|until)\s+",
    re.IGNORECASE,
)
EVENT_RE = re.compile(r"\b(?:event|meeting|workshop|webinar|exam|orientation|seminar)\b", re.IGNORECASE)
EVENT_DATE_RE = re.compile(r"\b(?:on|scheduled\s+for|held\s+on|starts?\s+(?:on|at))\s+", re.IGNORECASE)
ANNOUNCEMENT_RE = re.compile(r"\b(?:announcement|reminder|notice|update|cancelled|canceled|important)\b", re.IGNORECASE)
MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
EXPLICIT_DATE_RE = re.compile(
    rf"\b(?:{MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    r"|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
RELATIVE_DATE_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|(?:next\s+|this\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"|in\s+\d+\s+(?:days?|weeks?|hours?)|\d+\s+(?:days?|weeks?|hours?)\s+(?:from\s+now|later))\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(?:\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?|noon|midnight)\b", re.IGNORECASE)


class IMAPConnectionError(RuntimeError):
    """An actionable IMAP error whose message contains no credentials."""


@dataclass(frozen=True)
class ParsedEmail:
    sender: str
    subject: str
    received_at: datetime
    body: str
    message_id: str | None = None


@dataclass(frozen=True)
class ParsedActionItem:
    extracted_type: EmailActionType
    summary: str
    suggested_deadline: datetime | None


@dataclass
class ScanResult:
    new_count: int = 0
    skipped_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _aware_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return str(value)


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(payload or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_email(raw_message: bytes, received_at: datetime | None = None) -> ParsedEmail:
    """Decode headers and visible text, ignoring attachments and inline images.

    ``received_at`` may be the IMAP INTERNALDATE. If omitted, the Date header is
    used; an invalid Date header falls back to the current UTC time.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    addresses = getaddresses([_decode_header(str(message.get("From", "")))])
    # Multiple From addresses are ambiguous and should not pass the allowlist.
    sender = addresses[0][1].strip().casefold() if len(addresses) == 1 else ""
    if received_at is None:
        try:
            received_at = parsedate_to_datetime(str(message.get("Date", "")))
        except (TypeError, ValueError, OverflowError):
            received_at = datetime.now(UTC)
    plain_parts, html_parts = [], []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_decode_part(part))
        elif content_type == "text/html":
            soup = BeautifulSoup(_decode_part(part), "html.parser")
            for element in soup(["script", "style", "head"]):
                element.decompose()
            html_parts.append(soup.get_text(" ", strip=True))
    body = "\n".join(plain_parts or html_parts)
    body = re.sub(r"[\t ]+", " ", body).strip()
    return ParsedEmail(
        sender=sender,
        subject=_decode_header(str(message.get("Subject", ""))).strip(),
        received_at=_aware_utc(received_at),
        body=body,
        message_id=str(message.get("Message-ID", "")).strip() or None,
    )


def is_allowed_sender(sender: str) -> bool:
    """Check the parsed mailbox domain, never a display name or substring."""
    if sender.count("@") != 1 or any(character.isspace() for character in sender):
        return False
    mailbox, domain = sender.rsplit("@", 1)
    return bool(mailbox) and domain.casefold() in ALLOWED_SENDER_DOMAINS


def _candidate_after(text: str, end: int) -> str:
    candidate = text[end : end + 220].strip()
    # Keep month abbreviations (Oct.) and AM/PM intact while trimming prose.
    candidate = re.split(r"\n|;|[.!?](?=\s+[A-Z])", candidate, maxsplit=1)[0]
    # Do not attach another clause's event time to the assignment's date.
    # For example, "Friday, and our lecture starts at 9 AM" means Friday's
    # date-only deadline, rather than a 9 AM assignment deadline.
    candidate = re.split(r"\b(?:and|but|however|whereas|while)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
    candidate = re.split(r"\b(?:please|remember|make\s+sure|to\s+avoid|via|through)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
    return candidate.strip(" :-.,")


def _extract_date(candidate: str, reference: datetime, local_timezone: str, deadline: bool) -> datetime | None:
    # Do not turn unrelated numbers or broad terms such as "soon" into dates.
    explicit = EXPLICIT_DATE_RE.search(candidate)
    relative = RELATIVE_DATE_RE.search(candidate)
    time_match = TIME_RE.search(candidate)
    if not explicit and not relative and not time_match:
        return None
    base = reference.astimezone(ZoneInfo(local_timezone))
    parser_settings = {
        "RELATIVE_BASE": base.replace(tzinfo=None),
        "TIMEZONE": local_timezone,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "current_period" if explicit else "future",
        "DATE_ORDER": "MDY",
        "PREFER_LOCALE_DATE_ORDER": False,
    }
    # dateparser does not consistently recognize the English "next Friday".
    # Treat next weekdays as the next occurrence, retaining message-based dates.
    normalized = re.sub(r"\b(?:next|this)\s+(?=Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)", "", candidate, flags=re.IGNORECASE)
    parsed = dateparser.parse(normalized, languages=["en"], settings=parser_settings)
    if parsed is None:
        matches = search_dates(normalized, languages=["en"], settings=parser_settings) or []
        parsed = matches[0][1] if matches else None
        # A matcher may separate a date from its following clock time.
        if parsed is not None and time_match:
            clock = dateparser.parse(time_match.group(), languages=["en"], settings={**parser_settings, "RELATIVE_BASE": parsed.replace(tzinfo=None)})
            if clock is not None:
                parsed = parsed.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if parsed is None:
        return None
    if deadline and not time_match and not re.search(r"\bhours?\b", candidate, re.IGNORECASE):
        parsed = parsed.replace(hour=23, minute=59, second=0, microsecond=0)
    return _aware_utc(parsed)


def _summary(text: str, match: re.Match | None) -> str:
    if match:
        start = max(text.rfind("\n", 0, match.start()), text.rfind(". ", 0, match.start())) + 1
        fragment = text[start : match.end()] + _candidate_after(text, match.end())
    else:
        fragment = text.split("\n", 1)[0]
    fragment = " ".join(fragment.split()).strip()
    return fragment[:497] + "..." if len(fragment) > 500 else fragment


def extract_action_item(message: ParsedEmail, local_timezone: str | None = None) -> ParsedActionItem | None:
    """Extract one deadline, event, or announcement for human review.

    Sender filtering belongs to the scanner, so this function can also parse
    manually supplied messages in tests or other local workflows.
    """
    local_timezone = local_timezone or settings.timezone
    text = f"{message.subject}\n{message.body}".strip()
    # An explicit correction supersedes dates stated earlier in the message.
    # Prefer the final correction in the body, then the subject. If that date
    # cannot be understood, retain it for review instead of reviving an old date.
    for content in (message.body, message.subject):
        corrections = list(DEADLINE_CHANGE_RE.finditer(content))
        if corrections:
            match = corrections[-1]
            parsed = _extract_date(_candidate_after(content, match.end()), message.received_at, local_timezone, deadline=True)
            return ParsedActionItem(EmailActionType.DEADLINE, _summary(content, match), parsed)
    # Prefer the body when the subject merely says "Deadline reminder".
    for content in (message.body, message.subject):
        for match in DEADLINE_RE.finditer(content):
            candidate = _candidate_after(content, match.end())
            parsed = _extract_date(candidate, message.received_at, local_timezone, deadline=True)
            if parsed is not None:
                return ParsedActionItem(EmailActionType.DEADLINE, _summary(content, match), parsed)
    if DEADLINE_RE.search(text):
        # An unparseable deadline is still useful to review; no date is invented.
        return ParsedActionItem(EmailActionType.DEADLINE, _summary(text, DEADLINE_RE.search(text)), None)
    if EVENT_RE.search(text):
        for match in EVENT_DATE_RE.finditer(text):
            parsed = _extract_date(_candidate_after(text, match.end()), message.received_at, local_timezone, deadline=False)
            if parsed is not None:
                return ParsedActionItem(EmailActionType.EVENT, _summary(text, match), parsed)
        return ParsedActionItem(EmailActionType.EVENT, _summary(text, None), None)
    if ANNOUNCEMENT_RE.search(text):
        return ParsedActionItem(EmailActionType.ANNOUNCEMENT, _summary(text, None), None)
    return None


def connect_imap(
    *,
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
    client_factory: Callable | None = None,
):
    """Return an authenticated TLS IMAP client; the caller must log out."""
    host = host or settings.imap_host
    username = username if username is not None else settings.imap_username
    password = password if password is not None else settings.imap_password
    access_token = access_token if access_token is not None else settings.imap_access_token
    if not username:
        raise IMAPConnectionError("Set HUSKYSYNC_IMAP_USERNAME to your UConn mailbox address.")
    if not access_token and not password:
        raise IMAPConnectionError(
            "Set HUSKYSYNC_IMAP_ACCESS_TOKEN to a Microsoft OAuth2 access token with the delegated "
            "https://outlook.office.com/IMAP.AccessAsUser.All scope. The mailbox must have IMAP enabled."
        )
    factory = client_factory or imaplib.IMAP4_SSL
    client = None
    try:
        client = factory(host, port=993, timeout=30)
        if access_token:
            payload = f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
            status, _ = client.authenticate("XOAUTH2", lambda _challenge: payload)
        else:
            status, _ = client.login(username, password)
        if status != "OK":
            raise imaplib.IMAP4.error("Authentication rejected")
        return client
    except (imaplib.IMAP4.error, OSError):
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        if access_token:
            raise IMAPConnectionError(
                "Outlook IMAP connection or OAuth2 authentication failed. Check network access, renew the "
                "access token, verify its Outlook IMAP scope, and ask UConn IT whether IMAP is enabled."
            ) from None
        raise IMAPConnectionError(
            "Outlook IMAP connection or password authentication failed. Microsoft 365 usually disables "
            "basic authentication; use a delegated Outlook OAuth2 token or ask UConn IT about IMAP access."
        ) from None


def _internal_date(metadata: bytes) -> datetime | None:
    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(UTC)
    except (ValueError, UnicodeError):
        return None


def _source_key(message: ParsedEmail, raw: bytes, account: str, host: str, uidvalidity: str, uid: bytes) -> str:
    if message.message_id:
        identity = f"{host}|{account.casefold()}|message:{message.message_id}"
    elif uidvalidity:
        identity = f"{host}|{account.casefold()}|uid:{uidvalidity}:{uid.decode('ascii')}"
    else:
        identity = f"{host}|{account.casefold()}|content:{hashlib.sha256(raw).hexdigest()}"
    return "email:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def scan_recent_emails(
    days: int = 14,
    limit: int = 100,
    *,
    session_factory: Callable | None = None,
    client_factory: Callable | None = None,
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
) -> ScanResult:
    """Read recent unread mail, deduplicate it, and persist pending action items.

    Read-only selection and BODY.PEEK[] preserve unread status. Existing action
    items, including dismissed or converted ones, are left unchanged on reruns.
    """
    if not 1 <= days <= 365 or not 1 <= limit <= 1000:
        raise ValueError("days must be between 1 and 365; limit must be between 1 and 1000")
    if session_factory is None:
        init_db()
        session_factory = SessionLocal
    host = host or settings.imap_host
    username = username if username is not None else settings.imap_username
    client = connect_imap(host=host, username=username, password=password, access_token=access_token, client_factory=client_factory)
    result = ScanResult()
    try:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise IMAPConnectionError("Unable to open Outlook INBOX in read-only mode. Check mailbox IMAP permissions.")
        _, validity_values = client.response("UIDVALIDITY")
        uidvalidity = (validity_values or [b""])[0]
        if isinstance(uidvalidity, bytes):
            uidvalidity = uidvalidity.decode("ascii", errors="ignore")
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")
        status, values = client.uid("search", None, "UNSEEN", "SINCE", since)
        if status != "OK":
            raise IMAPConnectionError("Outlook could not search unread messages. Check mailbox IMAP permissions.")
        uids = (values[0] or b"").split() if values else []
        if len(uids) > limit:
            result.warnings.append(f"Scan limited to the newest {limit} unread messages; older matches remain unread.")
        with session_factory() as session:
            for uid in uids[-limit:]:
                status, parts = client.uid("fetch", uid, "(BODY.PEEK[] INTERNALDATE)")
                payload = next((part for part in (parts or []) if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
                if status != "OK" or payload is None:
                    result.skipped_count += 1
                    result.warnings.append("A message could not be fetched and was left unchanged.")
                    continue
                raw = payload[1]
                message = parse_email(raw, received_at=_internal_date(payload[0]))
                if not is_allowed_sender(message.sender):
                    result.skipped_count += 1
                    continue
                action = extract_action_item(message)
                if action is None:
                    result.skipped_count += 1
                    continue
                source_key = _source_key(message, raw, username, host, str(uidvalidity or ""), uid)
                if session.scalar(select(EmailActionItem.id).where(EmailActionItem.source_key == source_key)) is not None:
                    result.skipped_count += 1
                    continue
                try:
                    with session.begin_nested():
                        session.add(EmailActionItem(
                            sender=message.sender,
                            subject=message.subject,
                            received_at=message.received_at,
                            extracted_type=action.extracted_type,
                            summary=action.summary,
                            suggested_deadline=action.suggested_deadline,
                            status=EmailActionStatus.PENDING,
                            source_key=source_key,
                        ))
                        session.flush()
                    result.new_count += 1
                except IntegrityError:
                    # The source key also protects against concurrent scans.
                    result.skipped_count += 1
            session.commit()
        return result
    except (imaplib.IMAP4.error, OSError):
        raise IMAPConnectionError("Outlook IMAP scan failed. Check the connection and renew the OAuth2 token before retrying.") from None
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
