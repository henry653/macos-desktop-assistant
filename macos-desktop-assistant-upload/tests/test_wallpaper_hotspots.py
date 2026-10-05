from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import render_daily_briefing as renderer  # noqa: E402


def fixture() -> dict[str, object]:
    swe_body = "\n".join(
        [
            "Google: [Software Engineer Intern](https://jobs.example/google?utm_source=mail)",
            "TikTok: [Machine Learning Engineer Intern](https://jobs.example/tiktok?utm_source=mail)",
        ]
    )
    return {
        "generated_at": "2026-09-02T09:30:00-04:00",
        "timezone": "America/New_York",
        "sources": {key: {"state": "ok"} for key in renderer.SOURCE_ORDER},
        "assignments": [
            {
                "course": "COMP 455",
                "title": "Upcoming assignment",
                "due_at": "2026-09-03T12:00:00-04:00",
                "status": "not_submitted",
                "url": "https://uncch.instructure.com/assignments/1",
            },
            {
                "course": "COMP 311",
                "title": "Dropped-course upcoming assignment",
                "due_at": "2026-09-03T13:00:00-04:00",
                "status": "not_submitted",
                "url": "https://uncch.instructure.com/assignments/hidden-comp-311-upcoming",
            },
            {
                "course": "STOR455.001",
                "title": "Complete-Before_Class: reading",
                "due_at": "2026-09-03T08:00:00-04:00",
                "status": "not_submitted",
                "url": "https://uncch.instructure.com/assignments/hidden",
            },
            {
                "course": "COMP455.002.FA26",
                "title": "HW00: Math Review（Gradescope 显示 No Submission）",
                "due_at": "2026-08-26T23:59:00-04:00",
                "status": "past_due_unsubmitted",
                "url": "https://uncch.instructure.com/assignments/hidden-comp-455",
            },
            {
                "course": "COMP311.002.FA26",
                "title": "Old COMP 311 lecture",
                "due_at": "2026-09-01T13:00:00-04:00",
                "status": "past_due_unsubmitted",
                "url": "https://uncch.instructure.com/assignments/hidden-comp-311",
            },
            {
                "course": "STOR 445",
                "title": "Visible overdue assignment",
                "due_at": "2026-09-01T14:00:00-04:00",
                "status": "past_due_unsubmitted",
                "url": "javascript:alert(1)",
            },
        ],
        "exams": [
            {
                "course": "STOR 445",
                "title": "Midterm 1",
                "exam_at": "2026-09-08T10:00:00-04:00",
                "status": "scheduled",
                "source_url": "https://uncch.instructure.com/exams/1",
                "review_url": "https://uncch.instructure.com/modules/midterm-review",
                "review_state": "verified",
            }
        ],
        "messages": [
            {
                "source": "gmail",
                "sender_display": "SWE List",
                "sender_address": "noreply@swelist.com",
                "subject": "2 New Internships Posted Today",
                "body": swe_body,
                "received_at": "2026-09-02T08:00:00-04:00",
                "expected_internship_count": 2,
                "parsed_internship_count": 2,
                "count_verified": True,
                "url": "https://mail.google.com/mail/u/0/#inbox/swe",
            },
            {
                "source": "gmail",
                "sender_display": "Professor",
                "subject": "Action today",
                "summary": "Reply before class",
                "priority": "P0",
                "received_at": "2026-09-02T09:00:00-04:00",
                "url": "https://mail.google.com/mail/u/0/#inbox/one",
            },
            {
                "source": "outlook",
                "sender_display": "Registrar",
                "subject": "Review notice",
                "summary": "Review the notice",
                "priority": "P1",
                "received_at": "2026-09-02T09:05:00-04:00",
                "url": "file:///tmp/not-allowed",
            },
            {
                "source": "linkedin",
                "sender_display": "Recruiter",
                "subject": "Follow up",
                "summary": "Follow up this week",
                "priority": "P2",
                "received_at": "2026-09-02T09:10:00-04:00",
                "url": "https://www.linkedin.com/messaging/thread/3",
            },
        ],
        "intern_preparation_summary": {
            "schema_version": 1,
            "report_date": "2026-09-02",
            "count_verified": True,
            "verified_role_count": 2,
            "queued_count": 1,
            "manual_decision_count": 1,
            "manual_review_count": 0,
            "prepared_count": 0,
            "excluded_count": 1,
            "items": [
                {
                    "company": "Google",
                    "role": "Software Engineer Intern",
                    "apply_url": "https://jobs.example/google",
                    "queue_status": "manual_decision",
                    "decision_reason": "big_company",
                },
                {
                    "company": "TikTok",
                    "role": "Machine Learning Engineer Intern",
                    "apply_url": "https://jobs.example/tiktok",
                    "queue_status": "excluded",
                    "decision_reason": "user_excluded_tiktok",
                },
            ],
        },
    }


class WallpaperHotspotTests(unittest.TestCase):
    def test_native_overlay_defaults_to_finder_and_requires_option_for_links(self) -> None:
        source = (ROOT / "MyPlannerCompanion.m").read_text(encoding="utf-8")

        self.assertIn("panel.ignoresMouseEvents = !self.linkModeActive;", source)
        self.assertIn("CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)", source)
        self.assertIn("kCGEventFlagMaskAlternate", source)
        self.assertIn("event.modifierFlags & NSEventModifierFlagOption", source)
        self.assertIn("wallpaper_aligned_option_hold_hotspots", source)
        self.assertIn("card_reveal_in_link_mode", source)
        self.assertIn("double_tap_option", source)
        self.assertIn("openURL:self.localReportURL", source)
        self.assertNotIn("panel.ignoresMouseEvents = NO;", source)

    def test_hotspots_follow_the_exact_visible_card_layout(self) -> None:
        data = fixture()
        hotspots: list[dict[str, object]] = []
        image = renderer.render(data, hotspot_sink=hotspots)
        self.assertEqual((3024, 1964), image.size)

        kinds = [item["kind"] for item in hotspots]
        self.assertEqual(4, kinds.count("source"))
        self.assertEqual(2, kinds.count("assignment"))
        self.assertEqual(1, kinds.count("exam"))
        self.assertEqual(0, kinds.count("warning"))
        self.assertEqual(2, kinds.count("message"))
        self.assertEqual(1, kinds.count("intern"))
        self.assertEqual(1, kinds.count("intern_control"))
        self.assertEqual(1, kinds.count("report"))
        self.assertIn("more", kinds)

        by_kind = {}
        for item in hotspots:
            by_kind.setdefault(item["kind"], []).append(item)
        self.assertEqual(
            {"x": 166, "y": 276, "width": 292, "height": 64},
            by_kind["source"][0]["rect"],
        )
        self.assertEqual(
            {"x": 190, "y": 520, "width": 1460, "height": 169},
            by_kind["exam"][0]["rect"],
        )
        self.assertEqual(
            [
                {"x": 190, "y": 707, "width": 1460, "height": 169},
                {"x": 190, "y": 894, "width": 1460, "height": 169},
            ],
            [item["rect"] for item in by_kind["assignment"]],
        )
        self.assertEqual(
            [
                {"x": 1760, "y": 502, "width": 930, "height": 169},
                {"x": 1760, "y": 689, "width": 930, "height": 169},
            ],
            [item["rect"] for item in by_kind["message"]],
        )
        self.assertEqual(
            {"x": 1760, "y": 980, "width": 930, "height": 126},
            by_kind["intern"][0]["rect"],
        )

        labels = " ".join(str(item["label"]) for item in hotspots)
        self.assertIn("Complete-Before_Class", labels)
        self.assertNotIn("HW00: Math Review", labels)
        self.assertNotIn("Old COMP 311 lecture", labels)
        self.assertNotIn("Dropped-course upcoming assignment", labels)
        self.assertNotIn("Visible overdue assignment", labels)
        self.assertIn("Midterm 1", labels)
        self.assertNotIn("TikTok", labels)
        self.assertIn("Google", labels)

    def test_only_http_urls_or_controlled_local_actions_are_emitted(self) -> None:
        hotspots: list[dict[str, object]] = []
        renderer.render(fixture(), hotspot_sink=hotspots)
        for item in hotspots:
            if "url" in item:
                self.assertRegex(str(item["url"]), r"^https?://")
                self.assertNotIn("file:", str(item["url"]))
                self.assertNotIn("javascript:", str(item["url"]))
            else:
                self.assertIn(
                    item.get("action"),
                    {"open_local_report", "start_intern_application_batch"},
                )
                self.assertTrue(item.get("section"))

        exam = next(item for item in hotspots if item["kind"] == "exam")
        self.assertEqual(
            "https://uncch.instructure.com/modules/midterm-review",
            exam["url"],
        )

        intern_control = next(item for item in hotspots if item["kind"] == "intern_control")
        self.assertEqual("start_intern_application_batch", intern_control["action"])
        self.assertEqual("internships", intern_control["section"])

    def test_manifest_schema_digest_and_atomic_write(self) -> None:
        data = fixture()
        hotspots: list[dict[str, object]] = []
        image = renderer.render(data, hotspot_sink=hotspots)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            png = root / "wallpaper.png"
            output = root / "wallpaper_hotspots.json"
            renderer.atomic_save_png(image, png)
            manifest = renderer.build_hotspot_manifest(
                generated_at=renderer.parse_datetime(str(data["generated_at"])),
                image_path=png,
                hotspots=hotspots,
            )
            renderer.atomic_save_json(manifest, output)
            saved = json.loads(output.read_text(encoding="utf-8"))
            expected_digest = hashlib.sha256(png.read_bytes()).hexdigest()

        self.assertEqual(1, saved["schema_version"])
        self.assertEqual(
            {"width": 3024, "height": 1964, "coordinate_origin": "top_left"},
            saved["canvas"],
        )
        self.assertEqual(expected_digest, saved["image_sha256"])
        self.assertEqual(len(hotspots), len(saved["hotspots"]))
        self.assertEqual(len(hotspots), len({item["id"] for item in saved["hotspots"]}))


if __name__ == "__main__":
    unittest.main()
