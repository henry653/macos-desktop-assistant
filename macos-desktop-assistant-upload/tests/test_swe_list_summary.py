from __future__ import annotations

import base64
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from export_widget_snapshot import build_snapshot  # noqa: E402
from run_daily_briefing import _message_to_item, _validate_swe_message_counts  # noqa: E402
from swe_list_summary import (  # noqa: E402
    analyze_message,
    collect_verified_roles,
    is_candidate_message,
    parse_entries,
)


ZONE = ZoneInfo("America/New_York")


def message(
    body: str,
    *,
    count: int = 4,
    sender: str = "SWE List <noreply@swelist.com>",
    received_at: str = "2026-09-02T08:15:00-04:00",
) -> dict[str, object]:
    return {
        "source": "gmail",
        "sender": sender,
        "sender_address": "noreply@swelist.com" if "noreply@swelist.com" in sender else sender,
        "sender_display": "SWE List",
        "subject": f"{count} New Internships Posted Today",
        "body": body,
        "received_at": received_at,
        "expected_internship_count": count,
        "parsed_internship_count": count,
        "count_verified": True,
        "url": "https://mail.google.com/mail/u/0/#inbox/thread-123",
    }


BODY = "\n".join(
    [
        "**Google:** [Software Engineer Intern](https://simplify.jobs/p/google?utm_source=swelist)",
        "Tiny Labs: [Data Engineer Intern](https://simplify.jobs/p/tiny-data)",
        "Tiny Labs: [Data Scientist Intern - PhD](https://simplify.jobs/p/tiny-phd)",
        "Small Systems: [Software Engineer Intern](https://simplify.jobs/p/small) — limited to 2 applications",
    ]
)


class SweListSummaryTests(unittest.TestCase):
    def test_gmail_ingestion_rejects_missing_or_spoofed_headers(self) -> None:
        encoded = base64.urlsafe_b64encode(BODY.encode()).decode().rstrip("=")

        def gmail(from_value: str | None) -> dict[str, object]:
            headers = [{"name": "Subject", "value": "4 New Internships Posted Today"}]
            if from_value is not None:
                headers.append({"name": "From", "value": from_value})
            return {
                "id": "message-id",
                "internalDate": str(int(datetime(2026, 9, 2, 9, 0, tzinfo=ZONE).timestamp() * 1000)),
                "payload": {
                    "mimeType": "text/plain",
                    "headers": headers,
                    "body": {"data": encoded},
                },
            }

        now = datetime(2026, 9, 2, 9, 0, tzinfo=ZONE)
        self.assertIsNone(_message_to_item(gmail(None), now))
        self.assertIsNone(_message_to_item(gmail("SWE List <attacker@example.com>"), now))
        accepted = _message_to_item(gmail("SWE List <noreply@swelist.com>"), now)
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual("noreply@swelist.com", accepted["sender_address"])
        self.assertEqual(BODY, accepted["body"])
        self.assertNotIn("simplify.jobs", accepted["summary"])

    def test_strict_sender_and_subject_gate(self) -> None:
        valid = message(BODY)
        self.assertTrue(is_candidate_message(valid))
        self.assertIsNotNone(analyze_message(valid))

        spoofed = message(BODY, sender="SWE List <noreply@swelist.co>")
        self.assertFalse(is_candidate_message(spoofed))
        self.assertIsNone(analyze_message(spoofed))

        contradictory = message(BODY)
        contradictory["sender"] = "SWE List <attacker@example.com>"
        self.assertFalse(is_candidate_message(contradictory))

        wrong_subject = message(BODY)
        wrong_subject["subject"] = "Internship roundup"
        self.assertFalse(is_candidate_message(wrong_subject))

    def test_count_mismatch_is_not_verified(self) -> None:
        mismatched = message(BODY, count=5)
        self.assertIsNone(analyze_message(mismatched))
        summaries, roles = collect_verified_roles([mismatched])
        self.assertEqual([], summaries)
        self.assertEqual([], roles)

        stats = _validate_swe_message_counts([mismatched], None)
        self.assertFalse(stats["count_verified"])
        self.assertEqual("role_count_mismatch", stats["count_mismatches"][0]["reason"])
        self.assertFalse(_validate_swe_message_counts([], None)["count_verified"])

    def test_tracking_parameters_do_not_defeat_deduplication(self) -> None:
        duplicate = (
            "Tiny Labs: [Data Engineer Intern](https://simplify.jobs/p/tiny-data?utm_source=one)\n"
            "Tiny Labs: [Data Engineer Intern](https://simplify.jobs/p/tiny-data?utm_source=two)"
        )
        entries = parse_entries(duplicate)
        self.assertEqual(1, len(entries))
        self.assertEqual("https://simplify.jobs/p/tiny-data", entries[0]["url"])

    def test_categories_are_mutually_exclusive_and_sanitized(self) -> None:
        summaries, roles = collect_verified_roles([message(BODY)])
        self.assertEqual(1, len(summaries))
        summary = summaries[0]
        self.assertEqual(4, summary["totalUniqueRoles"])
        self.assertEqual(2, summary["manualReviewCount"])
        self.assertEqual(1, summary["bigCompanyCount"])
        self.assertEqual(1, summary["limitedApplicationCount"])
        self.assertEqual(1, summary["eligibleSmallCompanyPreparationCount"])
        self.assertEqual(1, summary["skippedIneligibleCount"])
        self.assertEqual(4, sum(
            summary[key]
            for key in (
                "manualReviewCount",
                "eligibleSmallCompanyPreparationCount",
                "skippedIneligibleCount",
            )
        ))
        self.assertEqual(4, len(roles))
        encoded = json.dumps(summary, sort_keys=True)
        self.assertNotIn("noreply@", encoded)
        self.assertNotIn("simplify.jobs", encoded)

    def test_widget_keeps_verified_total_but_fails_closed_without_strict_queue(self) -> None:
        data = {
            "generated_at": "2026-09-02T09:00:00-04:00",
            "timezone": "America/New_York",
            "sources": {"gmail": {"state": "ok", "checked_at": "2026-09-02T09:00:00-04:00"}},
            "assignments": [],
            "messages": [message(BODY)],
        }
        snapshot = build_snapshot(
            data,
            datetime(2026, 9, 2, 9, 0, tzinfo=ZONE),
            max_internships=2,
        )
        self.assertEqual(4, snapshot["stats"]["internships"])
        self.assertEqual(0, snapshot["stats"]["displayedInternships"])
        self.assertEqual(0, snapshot["stats"]["sweInternManualReview"])
        self.assertEqual(4, snapshot["stats"]["sweInternRequirementsReview"])
        self.assertEqual(0, snapshot["stats"]["sweInternEligiblePreparation"])
        self.assertEqual(0, snapshot["stats"]["sweInternSkippedIneligible"])
        self.assertEqual(4, snapshot["sweIntern"]["verifiedCount"])
        self.assertEqual("2026-09-02", snapshot["sweIntern"]["reportDate"])
        self.assertEqual(1, len(snapshot["sweInternDaily"]))
        internship_items = [item for item in snapshot["items"] if item["kind"] == "internship"]
        self.assertEqual(0, len(internship_items))


if __name__ == "__main__":
    unittest.main()
