from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_daily_briefing as briefing  # noqa: E402


ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 2, 20, 0, tzinfo=ZONE)
URL = "https://jobs.example.test/tiny-labs-data-intern"


def candidate() -> dict[str, object]:
    return {
        "company": "Tiny Labs",
        "role": "Data Engineer Intern",
        "apply_url": URL,
        "source": "gmail",
        "sender_display": "SWE List",
        "received_at": "2026-09-02T16:00:00-04:00",
        "should_auto_apply": True,
        "is_limited": False,
    }


def verified_message() -> dict[str, object]:
    return {
        "source": "gmail",
        "sender": "SWE List <noreply@swelist.com>",
        "sender_address": "noreply@swelist.com",
        "sender_display": "SWE List",
        "subject": "1 New Internship Posted Today",
        "body": f"Tiny Labs: [Data Engineer Intern]({URL})",
        "received_at": "2026-09-02T16:00:00-04:00",
        "expected_internship_count": 1,
        "parsed_internship_count": 1,
        "count_verified": True,
    }


def eligible_state() -> dict[str, object]:
    return {
        "intern_auto_apply": {
            "requirement_reviews": [
                {
                    "company": "Tiny Labs",
                    "role": "Data Engineer Intern",
                    "eligibility_verified": True,
                    "application_limit_checked": True,
                    "eligible": True,
                    "has_application_limit": False,
                }
            ]
        }
    }


class FakeRenderer:
    @staticmethod
    def partition_intern_messages(_messages: object) -> tuple[list[dict[str, object]], list[object]]:
        return [candidate()], []


class ApplicationPreparationStateTests(unittest.TestCase):
    def test_mail_reader_uses_a_separate_read_only_token(self) -> None:
        self.assertEqual(
            briefing.GMAIL_SCOPES,
            ("https://www.googleapis.com/auth/gmail.readonly",),
        )
        self.assertEqual(briefing.GMAIL_TOKEN_PATH.name, "token_gmail_readonly.pickle")

    def test_legacy_applied_records_are_downgraded_without_evidence(self) -> None:
        state = {
            "intern_auto_apply": {
                "applied": [
                    {
                        "company": "Tiny Labs",
                        "role": "Data Engineer Intern",
                        "apply_url": URL,
                        "opened_at": "2026-09-01T10:00:00-04:00",
                    },
                    {
                        "company": "Confirmed Co",
                        "role": "Software Engineer Intern",
                        "apply_url": "https://jobs.example.test/confirmed",
                        "status": "submitted",
                        "confirmation": "Official application site displayed a success page",
                    },
                    {
                        "company": "False Flag Co",
                        "role": "Software Engineer Intern",
                        "apply_url": "https://jobs.example.test/false-flag",
                        "status": "submitted",
                        "success_confirmation": False,
                    },
                    {
                        "company": "Confirmed Flag Co",
                        "role": "Software Engineer Intern",
                        "apply_url": "https://jobs.example.test/confirmed-flag",
                        "success_confirmation": True,
                    },
                ]
            }
        }

        history = briefing._migrate_intern_application_history(state)

        self.assertNotIn("applied", history)
        self.assertEqual(2, len(history["legacy_opened_unverified"]))
        self.assertTrue(all(
            item["status"] == "legacy_opened_unverified"
            for item in history["legacy_opened_unverified"]
        ))
        self.assertEqual(2, len(history["submitted"]))
        self.assertEqual(
            {"Confirmed Co", "Confirmed Flag Co"},
            {item["company"] for item in history["submitted"]},
        )

    def test_prepare_only_default_never_opens_browser(self) -> None:
        state = eligible_state()
        with patch.object(
            briefing,
            "_open_urls_in_chrome",
            side_effect=AssertionError("browser must not open in prepare-only mode"),
        ) as browser_open:
            stats = briefing._handle_application_preparation(
                {"messages": [verified_message()]},
                state,
                FakeRenderer,
                max_open=30,
                now=NOW,
            )

        browser_open.assert_not_called()
        history = state["intern_auto_apply"]
        self.assertNotIn("applied", history)
        self.assertEqual("prepared", history["prepared"][0]["status"])
        self.assertEqual([], history["opened_unverified"])
        self.assertEqual(0, stats["opened_unverified"])
        self.assertFalse(stats["submission_attempted"])
        self.assertEqual(0, stats["submitted"])

    def test_submitted_without_confirmation_is_downgraded(self) -> None:
        state = {"intern_auto_apply": {"submitted": [{
            "company": "Unverified Co",
            "role": "Software Engineer Intern",
            "apply_url": URL,
            "status": "submitted",
        }]}}
        history = briefing._migrate_intern_application_history(state)
        self.assertEqual([], history["submitted"])
        self.assertEqual(1, len(history["legacy_opened_unverified"]))
        self.assertEqual(
            "submitted_without_success_confirmation",
            history["legacy_opened_unverified"][0]["migration_source"],
        )

    def test_opened_link_is_recorded_as_unverified_not_applied(self) -> None:
        state = eligible_state()
        queue_result = briefing.build_preparation_queue([verified_message()], state, NOW)
        self.assertEqual(queue_result["queue"][0]["queue_status"], "prepared")
        queue_result["queue"][0]["resume_attachment_ready"] = True
        with patch.object(briefing, "build_preparation_queue", return_value=queue_result), \
             patch.object(briefing, "_open_urls_in_chrome", return_value=([URL], [])) as browser_open:
            stats = briefing._handle_application_preparation(
                {"messages": [verified_message()]},
                state,
                FakeRenderer,
                max_open=1,
                now=NOW,
                open_browser=True,
            )

        browser_open.assert_called_once_with([URL])
        history = state["intern_auto_apply"]
        self.assertNotIn("applied", history)
        self.assertEqual([], history["submitted"])
        self.assertEqual(1, len(history["opened_unverified"]))
        self.assertEqual("opened_unverified", history["opened_unverified"][0]["status"])
        self.assertEqual(1, stats["opened_unverified"])
        self.assertFalse(stats["submission_attempted"])
        self.assertEqual(0, stats["submitted"])

    def test_deprecated_auto_apply_shim_does_not_open_browser(self) -> None:
        state = eligible_state()
        with patch.object(
            briefing,
            "_open_urls_in_chrome",
            side_effect=AssertionError("deprecated flag must remain prepare-only"),
        ) as browser_open:
            stats = briefing._handle_auto_apply(
                {"messages": [verified_message()]},
                state,
                FakeRenderer,
                max_open=30,
                now=NOW,
            )

        browser_open.assert_not_called()
        self.assertEqual(1, stats["prepared"])
        self.assertEqual(0, stats["opened_unverified"])
        self.assertNotIn("applied", state["intern_auto_apply"])


if __name__ == "__main__":
    unittest.main()
