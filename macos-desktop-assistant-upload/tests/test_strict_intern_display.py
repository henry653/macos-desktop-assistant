from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import export_widget_snapshot as exporter  # noqa: E402
import render_daily_briefing as renderer  # noqa: E402
from intern_preparation import strict_display_items  # noqa: E402


ZONE = ZoneInfo("America/New_York")


def fixture() -> dict[str, object]:
    body = "\n".join(
        [
            "Google: [Software Engineer Intern](https://jobs.example/google?utm_source=mail)",
            "Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny?utm_source=mail)",
            "Ready Co: [AI Engineer Intern](https://jobs.example/ready?utm_source=mail)",
            "TikTok: [Machine Learning Engineer Intern](https://jobs.example/tiktok?utm_source=mail)",
            "Research Co: [Data Scientist Intern - PhD](https://jobs.example/phd?utm_source=mail)",
        ]
    )
    message = {
        "source": "gmail",
        "sender": "SWE List <noreply@swelist.com>",
        "sender_address": "noreply@swelist.com",
        "sender_display": "SWE List",
        "subject": "5 New Internships Posted Today",
        "body": body,
        "received_at": "2026-09-02T16:00:00-04:00",
        "expected_internship_count": 5,
        "parsed_internship_count": 5,
        "count_verified": True,
        "url": "https://mail.google.com/mail/u/0/#inbox/thread",
    }
    statuses = [
        ("Google", "Software Engineer Intern", "google", "manual_decision", "big_company"),
        ("Tiny Labs", "Data Engineer Intern", "tiny", "manual_review", "job_requirements_fetch_required"),
        ("Ready Co", "AI Engineer Intern", "ready", "prepared", "eligible_smaller_company_requirements_verified"),
        ("TikTok", "Machine Learning Engineer Intern", "tiktok", "excluded", "user_excluded_tiktok"),
        ("Research Co", "Data Scientist Intern - PhD", "phd", "excluded", "graduate_degree_only"),
    ]
    items = [
        {
            "company": company,
            "role": role,
            "apply_url": f"https://jobs.example/{slug}",
            "source_email_date": "2026-09-02",
            "queue_status": status,
            "decision_reason": reason,
            "is_limited": False,
            "resume_variant": "A",
            "resume_attachment_ready": status != "excluded",
        }
        for company, role, slug, status, reason in statuses
    ]
    return {
        "generated_at": "2026-09-02T20:00:00-04:00",
        "timezone": "America/New_York",
        "sources": {},
        "assignments": [],
        "messages": [message],
        "intern_preparation_summary": {
            "schema_version": 1,
            "report_date": "2026-09-02",
            "count_verified": True,
            "verified_role_count": 5,
            "queued_count": 3,
            "manual_decision_count": 1,
            "manual_review_count": 1,
            "prepared_count": 1,
            "excluded_count": 2,
            "items": items,
        },
    }


class StrictInternDisplayTests(unittest.TestCase):
    def test_canonical_join_hides_excluded_and_preserves_exact_statuses(self) -> None:
        data = fixture()
        cards = strict_display_items(
            data["messages"], data["intern_preparation_summary"], zone=ZONE
        )
        self.assertEqual(3, len(cards))
        self.assertEqual(
            {"manual_decision", "manual_review", "prepared"},
            {card["queue_status"] for card in cards},
        )
        self.assertNotIn("TikTok", {card["company"] for card in cards})
        self.assertNotIn("Research Co", {card["company"] for card in cards})
        self.assertTrue(all("utm_" not in card["apply_url"] for card in cards))

    def test_missing_or_inconsistent_strict_summary_fails_closed(self) -> None:
        data = fixture()
        self.assertEqual([], strict_display_items(data["messages"], None, zone=ZONE))
        bad = dict(data["intern_preparation_summary"])
        bad["prepared_count"] = 2
        self.assertEqual([], strict_display_items(data["messages"], bad, zone=ZONE))

    def test_widget_cards_use_strict_status_labels_only(self) -> None:
        data = fixture()
        snapshot = exporter.build_snapshot(
            data,
            now=datetime(2026, 9, 2, 20, 0, tzinfo=ZONE),
            max_internships=10,
        )
        cards = [item for item in snapshot["items"] if item["kind"] == "internship"]
        self.assertEqual(3, len(cards))
        self.assertEqual(
            {"手动决定", "要求待核验", "可准备申请，提交前确认"},
            {card["detail"] for card in cards},
        )
        self.assertEqual(1, snapshot["sweIntern"]["manualReviewCount"])
        self.assertEqual(1, snapshot["sweIntern"]["requirementsReviewCount"])
        self.assertEqual(1, snapshot["sweIntern"]["eligibleSmallCompanyPreparationCount"])

    def test_wallpaper_cards_are_strict_but_markdown_keeps_all_verified_roles(self) -> None:
        data = fixture()
        drawn: list[dict[str, object]] = []

        def capture(_draw: object, item: dict[str, object], _box: object) -> None:
            drawn.append(item)

        with patch.object(renderer, "draw_intern_card", side_effect=capture):
            renderer.render(data)
        self.assertEqual(3, len(drawn))
        self.assertEqual(
            {"manual_decision", "manual_review", "prepared"},
            {item["queue_status"] for item in drawn},
        )
        self.assertNotIn("TikTok", {item["company"] for item in drawn})

        markdown = renderer.write_markdown(data)
        for company in ("Google", "Tiny Labs", "Ready Co", "TikTok", "Research Co"):
            self.assertIn(company, markdown)
        self.assertIn("已核验完整原始清单", markdown)


if __name__ == "__main__":
    unittest.main()
