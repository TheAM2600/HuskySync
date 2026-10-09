"""Offline email parsing and read-only IMAP regression checks."""

from __future__ import annotations

import imaplib
import unittest
from datetime import UTC, datetime
from email.message import EmailMessage

from sqlalchemy import select

from app.database import create_session_factory
from app.email_parser import (
    IMAPConnectionError,
    ParsedEmail,
    connect_imap,
    extract_action_item,
    is_allowed_sender,
    parse_email,
    scan_recent_emails,
)
from app.models import EmailActionItem, EmailActionStatus, EmailActionType


def make_message(
    body: str = "Project 2 is due by Friday at 11:59 PM.",
    *,
    sender: str = "Professor Example <instructor@uconn.edu>",
    subject: str = "CSE 3100 project deadline",
    message_id: str | None = "<course-project-2@uconn.edu>",
    html: bool = False,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "student@uconn.edu"
    message["Subject"] = subject
    message["Date"] = "Mon, 05 Oct 2026 08:00:00 -0400"
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


class EmailParsingTests(unittest.TestCase):
    def test_relative_friday_uses_message_date_and_university_timezone(self):
        message = parse_email(make_message())
        action = extract_action_item(message, "America/New_York")
        self.assertEqual(message.sender, "instructor@uconn.edu")
        self.assertEqual(action.extracted_type, EmailActionType.DEADLINE)
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 10, 3, 59, tzinfo=UTC))
        self.assertIn("Project 2", action.summary)

    def test_moved_deadline_html_and_explicit_past_date(self):
        message = parse_email(make_message(
            "<html><head><script>due tomorrow</script></head><body>"
            "<p>The deadline moved to October 1, 2026 at 5:00 PM.</p>"
            "<p>Please upload your project.</p></body></html>",
            html=True,
        ))
        action = extract_action_item(message)
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 1, 21, 0, tzinfo=UTC))
        self.assertNotIn("script", message.body)
        self.assertNotIn("tomorrow", message.body)

    def test_explicit_date_without_year_stays_in_message_year(self):
        message = parse_email(make_message("Homework is due by October 1."))
        action = extract_action_item(message)
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 2, 3, 59, tzinfo=UTC))

    def test_corrected_deadline_supersedes_original_weekday(self):
        action = extract_action_item(parse_email(make_message(
            "Homework due Friday but deadline moved to Monday.",
        )))
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 13, 3, 59, tzinfo=UTC))
        self.assertIn("deadline moved to Monday", action.summary)

    def test_deadline_moved_from_old_date_uses_new_date(self):
        for change in ("moved", "extended", "changed"):
            with self.subTest(change=change):
                action = extract_action_item(parse_email(make_message(
                    f"Homework deadline {change} from October 10 to October 12.",
                )))
                self.assertEqual(action.suggested_deadline, datetime(2026, 10, 13, 3, 59, tzinfo=UTC))

    def test_last_explicit_deadline_correction_wins(self):
        action = extract_action_item(parse_email(make_message(
            "The deadline was moved to October 10. The deadline has been extended to October 12.",
        )))
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 13, 3, 59, tzinfo=UTC))

    def test_moved_from_abbreviated_date_with_clock_uses_new_deadline(self):
        action = extract_action_item(parse_email(make_message(
            "Homework due date was changed from Oct. 10 at 5 PM to October 12 at 6 PM.",
        )))
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 12, 22, tzinfo=UTC))

    def test_unparseable_corrected_deadline_does_not_restore_old_date(self):
        action = extract_action_item(parse_email(make_message(
            "Homework due Friday, but the deadline moved to a date to be announced.",
        )))
        self.assertEqual(action.extracted_type, EmailActionType.DEADLINE)
        self.assertIsNone(action.suggested_deadline)

    def test_causal_due_to_phrase_does_not_create_deadline(self):
        action = extract_action_item(parse_email(make_message(
            "Lecture canceled due to weather Friday.", subject="Lecture update",
        )))
        self.assertEqual(action.extracted_type, EmailActionType.ANNOUNCEMENT)
        self.assertIsNone(action.suggested_deadline)

    def test_another_clause_clock_does_not_change_date_only_deadline(self):
        action = extract_action_item(parse_email(make_message(
            "Homework due Friday, and our lecture starts at 9 AM.",
        )))
        self.assertEqual(action.suggested_deadline, datetime(2026, 10, 10, 3, 59, tzinfo=UTC))
        self.assertNotIn("9 AM", action.summary)

    def test_date_only_next_friday_end_of_day_and_dst(self):
        message = ParsedEmail(
            "professor@uconn.edu", "Lab", datetime(2026, 11, 2, 13, tzinfo=UTC),
            "Please submit by next Friday.",
        )
        action = extract_action_item(message)
        self.assertEqual(action.suggested_deadline, datetime(2026, 11, 7, 4, 59, tzinfo=UTC))

    def test_plain_body_preferred_and_attachments_ignored(self):
        message = EmailMessage()
        message["From"] = "professor@uconn.edu"
        message["Subject"] = "Lab deadline"
        message["Date"] = "Mon, 05 Oct 2026 08:00:00 -0400"
        message.set_content("Lab due tomorrow.")
        message.add_alternative("<p>Incorrect deadline due in 100 days.</p>", subtype="html")
        message.add_attachment(b"Attachment due in 5 days", maintype="text", subtype="plain", filename="notes.txt")
        parsed = parse_email(message.as_bytes())
        self.assertIn("tomorrow", parsed.body)
        self.assertNotIn("100 days", parsed.body)
        self.assertNotIn("Attachment", parsed.body)

    def test_spoofed_sender_domains_rejected(self):
        for address in (
            "uconn.edu@evil.example", "professor@uconn.edu.evil.example",
            "no-reply@blackboard.com.evil.example", "professor@evil-uconn.edu",
        ):
            self.assertFalse(is_allowed_sender(address))
        self.assertTrue(is_allowed_sender("professor@uconn.edu"))
        self.assertTrue(is_allowed_sender("no-reply@mail.blackboard.com"))
        spoof = parse_email(make_message(sender='"professor@uconn.edu" <attacker@example.com>'))
        self.assertFalse(is_allowed_sender(spoof.sender))

    def test_unparseable_deadline_retained_for_review_without_invented_date(self):
        action = extract_action_item(parse_email(make_message("Homework due soon.")))
        self.assertEqual(action.extracted_type, EmailActionType.DEADLINE)
        self.assertIsNone(action.suggested_deadline)

    def test_non_action_mail_skipped(self):
        message = parse_email(make_message("Thanks for joining us!", subject="Welcome"))
        self.assertIsNone(extract_action_item(message))

    def test_event_and_announcement(self):
        event = extract_action_item(parse_email(make_message(
            "The workshop is scheduled for October 20, 2026 at 3 PM.", subject="Career workshop",
        )))
        self.assertEqual(event.extracted_type, EmailActionType.EVENT)
        self.assertEqual(event.suggested_deadline, datetime(2026, 10, 20, 19, tzinfo=UTC))
        announcement = extract_action_item(parse_email(make_message(
            "Important update: library hours have changed.", subject="Campus notice",
        )))
        self.assertEqual(announcement.extracted_type, EmailActionType.ANNOUNCEMENT)
        self.assertIsNone(announcement.suggested_deadline)


class FakeIMAP:
    def __init__(self, messages=None):
        self.messages = messages or {b"1": make_message()}
        self.calls = []
        self.auth_payload = None
        self.logged_out = False

    def authenticate(self, method, callback):
        self.calls.append(("authenticate", method))
        self.auth_payload = callback(b"")
        return "OK", []

    def login(self, username, password):
        self.calls.append(("login", username))
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"123456"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        uid = args[0]
        metadata = b'1 (UID ' + uid + b' INTERNALDATE "05-Oct-2026 08:00:00 -0400" BODY[] {300})'
        return "OK", [(metadata, self.messages[uid]), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", []


class EmailScanTests(unittest.TestCase):
    def setUp(self):
        self.session_factory = create_session_factory(":memory:")
        self.clients = []

    def tearDown(self):
        self.session_factory.kw["bind"].dispose()

    def factory(self, host, *, port, timeout):
        self.assertEqual(host, "outlook.office365.com")
        self.assertEqual(port, 993)
        self.assertEqual(timeout, 30)
        client = FakeIMAP({
            b"1": make_message(),
            b"2": make_message(sender="professor@uconn.edu.attacker.example", message_id="<spoof@example.com>"),
            b"3": make_message("Thanks!", subject="Hello", message_id="<hello@uconn.edu>"),
        })
        self.clients.append(client)
        return client

    def scan(self):
        return scan_recent_emails(
            session_factory=self.session_factory,
            client_factory=self.factory,
            username="student@uconn.edu",
            access_token="test-access-token",
            password="",
        )

    def test_scan_is_readonly_unread_safe_and_idempotent(self):
        result = self.scan()
        self.assertEqual(result.new_count, 1)
        self.assertEqual(result.skipped_count, 2)
        self.assertIn(("select", "INBOX", True), self.clients[0].calls)
        fetches = [call for call in self.clients[0].calls if call[:2] == ("uid", "fetch")]
        self.assertTrue(all("BODY.PEEK[]" in call[-1] for call in fetches))
        self.assertIn(("authenticate", "XOAUTH2"), self.clients[0].calls)
        self.assertTrue(self.clients[0].logged_out)
        with self.session_factory() as session:
            row = session.scalar(select(EmailActionItem))
            row.status = EmailActionStatus.DISMISSED
            session.commit()
        again = self.scan()
        self.assertEqual(again.new_count, 0)
        with self.session_factory() as session:
            rows = session.scalars(select(EmailActionItem)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, EmailActionStatus.DISMISSED)

    def test_missing_authentication_has_actionable_error(self):
        with self.assertRaisesRegex(IMAPConnectionError, "IMAP_ACCESS_TOKEN"):
            connect_imap(username="student@uconn.edu", password="", access_token="")

    def test_authentication_failure_does_not_expose_server_or_token(self):
        class RejectedIMAP(FakeIMAP):
            def authenticate(self, method, callback):
                raise imaplib.IMAP4.error("secret-server-response")
        with self.assertRaises(IMAPConnectionError) as caught:
            connect_imap(
                username="student@uconn.edu", access_token="private-token", password="",
                client_factory=lambda *_args, **_kwargs: RejectedIMAP(),
            )
        self.assertNotIn("private-token", str(caught.exception))
        self.assertNotIn("secret-server-response", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


def test_sample_email_file_uses_the_live_filter_and_is_idempotent(tmp_path):
    import json

    from app.email_parser import import_sample_emails

    sample = tmp_path / "emails.json"
    sample.write_text(json.dumps({"emails": [
        {"id": "a", "from_email": "instructor@uconn.edu", "subject": "Project", "received_at": "2026-10-05T08:00:00-04:00",
         "body": "Project 2 is due by October 9 at 5 PM."},
        {"id": "b", "from_email": "club@gmail.com", "subject": "Deadline", "received_at": "2026-10-05T08:00:00-04:00",
         "body": "Sign up is due by October 9 at 5 PM."},
        {"id": "c", "subject": "Missing sender"},
    ]}), encoding="utf-8")
    factory = create_session_factory(tmp_path / "sample.sqlite3")
    try:
        first = import_sample_emails(sample, session_factory=factory)
        assert (first.new_count, first.skipped_count) == (1, 2)
        with factory() as session:
            item = session.scalars(select(EmailActionItem)).one()
            assert item.sender == "instructor@uconn.edu"
            assert item.suggested_deadline == datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
        assert import_sample_emails(sample, session_factory=factory).new_count == 0
    finally:
        factory.kw["bind"].dispose()
