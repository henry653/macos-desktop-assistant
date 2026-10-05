from __future__ import annotations

import base64
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_daily_briefing as briefing  # noqa: E402


ZONE = ZoneInfo("America/New_York")
READ_ONLY = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, object]:
        return self.payload


class _ReadOnlyMessagesResource:
    """Fake exposing only list/get; a mutation attempt fails the test."""

    def __init__(self, detail: dict[str, object]) -> None:
        self.detail = detail
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list(self, **kwargs: object) -> _Response:
        self.calls.append(("list", kwargs))
        return _Response({"messages": [{"id": self.detail["id"]}]})

    def get(self, **kwargs: object) -> _Response:
        self.calls.append(("get", kwargs))
        return _Response(self.detail)


class _FailingMessagesResource(_ReadOnlyMessagesResource):
    def get(self, **kwargs: object) -> _Response:
        self.calls.append(("get", kwargs))
        raise RuntimeError("simulated read failure")


class _UsersResource:
    def __init__(self, messages: _ReadOnlyMessagesResource) -> None:
        self.messages_resource = messages

    def messages(self) -> _ReadOnlyMessagesResource:
        return self.messages_resource


class _GmailService:
    def __init__(self, messages: _ReadOnlyMessagesResource) -> None:
        self.users_resource = _UsersResource(messages)

    def users(self) -> _UsersResource:
        return self.users_resource


class GmailReadOnlyReaderTests(unittest.TestCase):
    def test_html_links_win_when_plain_alternative_omits_urls(self) -> None:
        now = datetime(2026, 10, 1, 18, 0, tzinfo=ZONE)
        plain = "PrizePicks: Data Engineering Intern"
        html = '<p>PrizePicks: <a href="https://simplify.jobs/p/role1?utm_source=swelist">Data Engineering Intern</a></p>'
        encode = lambda value: base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        detail = {
            "id": "message-html",
            "threadId": "thread-html",
            "internalDate": str(int(now.timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "From", "value": "SWE List <noreply@swelist.com>"},
                    {"name": "Subject", "value": "1 New Internships Posted Today"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encode(plain)}},
                    {"mimeType": "text/html", "body": {"data": encode(html)}},
                ],
            },
        }
        item = briefing._message_to_item(detail, now)
        self.assertIsNotNone(item)
        self.assertEqual("2026-10-01T18:00:00-04:00", item["received_at"])
        self.assertEqual(1, len(briefing.parse_swe_entries(item["body"])))

    def test_missing_real_message_date_is_rejected(self) -> None:
        detail = {
            "id": "no-date",
            "payload": {
                "headers": [
                    {"name": "From", "value": "SWE List <noreply@swelist.com>"},
                    {"name": "Subject", "value": "1 New Internships Posted Today"},
                ],
            },
        }
        self.assertIsNone(briefing._message_to_item(detail, datetime(2026, 10, 1, tzinfo=ZONE)))

    def test_credentials_must_have_exactly_the_read_only_scope(self) -> None:
        read_only = Credentials(token="test", scopes=[READ_ONLY])
        broader_requested = Credentials(token="test", scopes=[READ_ONLY, COMPOSE])
        broader_granted = Credentials(
            token="test",
            scopes=[READ_ONLY],
            granted_scopes=[READ_ONLY, COMPOSE],
        )

        self.assertTrue(briefing._credentials_are_strictly_read_only(read_only))
        self.assertFalse(briefing._credentials_are_strictly_read_only(broader_requested))
        self.assertFalse(briefing._credentials_are_strictly_read_only(broader_granted))

    def test_unread_message_is_parsed_via_list_and_get_without_mutation(self) -> None:
        now = datetime(2026, 9, 2, 9, 0, tzinfo=ZONE)
        body = "Tiny Labs: [Software Engineer Intern](https://simplify.jobs/p/tiny)"
        encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        detail: dict[str, object] = {
            "id": "message-123",
            "threadId": "thread-123",
            "internalDate": str(int(now.timestamp() * 1000)),
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "SWE List <noreply@swelist.com>"},
                    {"name": "Subject", "value": "1 New Internships Posted Today"},
                    {"name": "Date", "value": "Wed, 2 Sep 2026 09:00:00 -0400"},
                ],
                "body": {"data": encoded},
            },
        }
        original = deepcopy(detail)
        messages_resource = _ReadOnlyMessagesResource(detail)
        service = _GmailService(messages_resource)
        state = {"last_successful_run": "2026-09-02T08:00:00-04:00"}

        with (
            patch.object(
                briefing,
                "_load_gmail_credentials",
                return_value=(object(), {"state": "ok"}),
            ),
            patch.object(briefing, "build", return_value=service),
        ):
            messages, source_state, found_new = briefing._collect_swe_gmail_messages(
                state=state,
                now=now,
                interactive_gmail=False,
            )

        self.assertTrue(found_new)
        self.assertEqual("ok", source_state["state"])
        self.assertEqual(1, len(messages))
        self.assertEqual(body, messages[0]["body"])
        self.assertEqual(
            "https://mail.google.com/mail/u/0/#inbox/thread-123",
            messages[0]["url"],
        )
        self.assertEqual(original, detail)
        self.assertIn("UNREAD", detail["labelIds"])
        self.assertEqual(["list", "get"], [name for name, _ in messages_resource.calls])
        list_kwargs = messages_resource.calls[0][1]
        get_kwargs = messages_resource.calls[1][1]
        self.assertEqual(["INBOX"], list_kwargs["labelIds"])
        self.assertIn(briefing.GMAIL_FROM_FILTER, list_kwargs["q"])
        self.assertRegex(list_kwargs["q"], r"after:\d+$")
        self.assertEqual("full", get_kwargs["format"])

    def test_partial_message_read_fails_closed(self) -> None:
        now = datetime(2026, 9, 2, 9, 0, tzinfo=ZONE)
        detail: dict[str, object] = {"id": "message-123"}
        messages_resource = _FailingMessagesResource(detail)
        service = _GmailService(messages_resource)

        with (
            patch.object(
                briefing,
                "_load_gmail_credentials",
                return_value=(object(), {"state": "ok"}),
            ),
            patch.object(briefing, "build", return_value=service),
        ):
            messages, source_state, found_new = briefing._collect_swe_gmail_messages(
                state={"last_successful_run": "2026-09-02T08:00:00-04:00"},
                now=now,
                interactive_gmail=False,
            )

        self.assertEqual([], messages)
        self.assertFalse(found_new)
        self.assertEqual("error", source_state["state"])
        self.assertEqual("gmail_message_fetch_failed", source_state["error"])
        self.assertEqual(1, source_state["failed_message_count"])


if __name__ == "__main__":
    unittest.main()
