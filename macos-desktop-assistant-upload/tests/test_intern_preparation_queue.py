from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intern_preparation import (  # noqa: E402
    CURRENT_RESUME_DIR,
    build_preparation_queue,
    build_preparation_summary,
    choose_resume_variant,
    inspect_resume,
    resolve_resume,
)


ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 2, 20, 0, tzinfo=ZONE)


def message(lines: list[str], *, day: str = "2026-09-02", verified: bool = True) -> dict[str, object]:
    count = len(lines)
    return {
        "source": "gmail",
        "sender": "SWE List <noreply@swelist.com>",
        "sender_address": "noreply@swelist.com",
        "sender_display": "SWE List",
        "subject": f"{count} New Internships Posted Today",
        "body": "\n".join(lines),
        "received_at": f"{day}T16:00:00-04:00",
        "expected_internship_count": count,
        "parsed_internship_count": count,
        "count_verified": verified,
    }


def fake_resume(variant: str) -> dict[str, object]:
    return {
        "variant": variant,
        "status": "ready",
        "path": f"/safe/Resume-{variant}.pdf",
        "exists": True,
        "ready": True,
        "blockedReason": None,
    }


class InternPreparationQueueTests(unittest.TestCase):
    def test_unknown_requirements_are_fetch_required_not_prepared(self) -> None:
        result = build_preparation_queue(
            [message(["Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny)"])],
            {},
            NOW,
            resume_resolver=fake_resume,
        )

        self.assertEqual(1, result["stats"]["manual_review"])
        self.assertEqual(0, result["stats"]["prepared"])
        self.assertEqual("manual_review", result["queue"][0]["queue_status"])
        self.assertEqual("job_requirements_fetch_required", result["queue"][0]["decision_reason"])
        self.assertFalse(result["queue"][0]["submission_attempted"])

    def test_verified_eligible_small_role_is_prepared(self) -> None:
        state = {
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
        result = build_preparation_queue(
            [message(["Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny)"])],
            state,
            NOW,
            resume_resolver=fake_resume,
        )

        self.assertEqual(1, result["stats"]["prepared"])
        item = result["queue"][0]
        self.assertEqual("prepared", item["queue_status"])
        self.assertEqual("eligible_smaller_company_requirements_verified", item["decision_reason"])
        self.assertTrue(item["resume_attachment_ready"])

    def test_sanitized_summary_requires_requirement_review_before_ready(self) -> None:
        messages = [message(["Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny)"])]
        unknown = build_preparation_summary(
            messages, {}, NOW, resume_resolver=fake_resume
        )
        assert unknown is not None
        self.assertEqual(1, unknown["manual_review_count"])
        self.assertEqual(0, unknown["prepared_count"])
        self.assertNotIn("resume_path", str(unknown))

        verified = build_preparation_summary(
            messages, {
                "intern_auto_apply": {"requirement_reviews": [{
                    "company": "Tiny Labs",
                    "role": "Data Engineer Intern",
                    "eligibility_verified": True,
                    "application_limit_checked": True,
                    "eligible": True,
                    "has_application_limit": False,
                }]}
            }, NOW, resume_resolver=fake_resume
        )
        assert verified is not None
        self.assertEqual(0, verified["manual_review_count"])
        self.assertEqual(1, verified["prepared_count"])

    def test_exclusions_big_company_limit_dedup_and_confirmed_submission(self) -> None:
        lines = [
            "TikTok: [Software Engineer Intern](https://jobs.example/tiktok)",
            "Tiny Research: [Data Scientist Intern - PhD](https://jobs.example/phd)",
            "Secure Co: [Software Engineer Intern](https://jobs.example/citizen) — U.S. citizens only",
            "Green Co: [Data Engineer Intern](https://jobs.example/green) — permanent residents only",
            "Brand Co: [Marketing Intern](https://jobs.example/marketing)",
            "Google: [Software Engineer Intern](https://jobs.example/google)",
            "Limited Co: [Software Engineer Intern](https://jobs.example/limited) — limited to 2 applications",
            "Done Co: [Software Engineer Intern](https://jobs.example/done)",
            "Duplicate Co: [Data Analyst Intern](https://jobs.example/duplicate?utm_source=mail)",
        ]
        older_duplicate = message(
            ["Duplicate Co: [Data Analyst Intern](https://jobs.example/duplicate?utm_source=older)"],
            day="2026-09-01",
        )
        state = {
            "intern_auto_apply": {
                "submitted": [
                    {
                        "company": "Done Co",
                        "role": "Software Engineer Intern",
                        "apply_url": "https://jobs.example/done?utm_source=old",
                        "status": "submitted",
                        "confirmation": "Official site showed success",
                    }
                ]
            }
        }

        result = build_preparation_queue(
            [message(lines), older_duplicate],
            state,
            NOW,
            resume_resolver=fake_resume,
        )
        reasons = [item["reason"] for item in result["excluded"]]
        self.assertIn("user_excluded_tiktok", reasons)
        self.assertIn("graduate_degree_only", reasons)
        self.assertIn("us_citizen_only", reasons)
        self.assertIn("us_permanent_resident_only", reasons)
        self.assertIn("outside_target_technical_profile", reasons)
        self.assertIn("already_submitted", reasons)
        self.assertIn("duplicate_role", reasons)

        queue_by_company = {item["company"]: item for item in result["queue"]}
        self.assertEqual("manual_decision", queue_by_company["Google"]["queue_status"])
        self.assertEqual("big_company", queue_by_company["Google"]["decision_reason"])
        self.assertEqual("manual_decision", queue_by_company["Limited Co"]["queue_status"])
        self.assertEqual("application_limit", queue_by_company["Limited Co"]["decision_reason"])
        self.assertEqual("manual_review", queue_by_company["Duplicate Co"]["queue_status"])

    def test_unverified_message_never_enters_queue(self) -> None:
        result = build_preparation_queue(
            [message(["Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny)"], verified=False)],
            {},
            NOW,
            resume_resolver=fake_resume,
        )
        self.assertEqual([], result["queue"])
        self.assertEqual(0, result["stats"]["verified_roles"])

    def test_phd_preferred_is_not_misread_as_phd_only(self) -> None:
        result = build_preparation_queue(
            [
                message(
                    [
                        "Research Co: [Data Scientist Intern - PhD preferred]"
                        "(https://jobs.example/phd-preferred)"
                    ]
                )
            ],
            {},
            NOW,
            resume_resolver=fake_resume,
        )
        self.assertEqual(1, len(result["queue"]))
        self.assertEqual("manual_review", result["queue"][0]["queue_status"])
        self.assertNotIn(
            "graduate_degree_only",
            [item["reason"] for item in result["excluded"]],
        )

    def test_resume_title_rules_are_transparent(self) -> None:
        self.assertEqual(
            ("A", "title_matches_ai_data_or_software_engineering"),
            choose_resume_variant("Machine Learning Engineer Intern"),
        )
        self.assertEqual(
            ("P", "title_matches_product_analytics_or_systems"),
            choose_resume_variant("Product Analytics Intern"),
        )

    def test_resume_footer_gate_fails_closed_and_clean_file_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = root / "Resume A.pdf"
            clean = root / "Resume A - Clean.pdf"
            unsafe.write_text("placeholder", encoding="utf-8")
            clean.write_text("placeholder", encoding="utf-8")

            def extractor(path: Path) -> tuple[str, str | None]:
                if path == unsafe:
                    return "FICTIONAL CHARACTER RESUME - MANUSCRIPT DEVELOPMENT ONLY", None
                return "Applied AI engineer with Python and PyTorch experience", None

            direct = inspect_resume(unsafe, extractor=extractor)
            self.assertFalse(direct["ready"])
            self.assertEqual(
                "resume_contains_manuscript_or_fictional_footer",
                direct["blockedReason"],
            )
            resolved = resolve_resume(
                "A",
                candidates={"A": (clean, unsafe)},
                extractor=extractor,
            )
            self.assertTrue(resolved["ready"])
            self.assertEqual(str(clean), resolved["path"])

    def test_configured_resume_files_are_the_only_selected_ready_variants(self) -> None:
        p = resolve_resume("P")
        a = resolve_resume("A")
        self.assertEqual(str(CURRENT_RESUME_DIR / "resume_p.pdf"), p["path"])
        self.assertEqual(str(CURRENT_RESUME_DIR / "resume_a.pdf"), a["path"])
        self.assertFalse(p["ready"])
        self.assertFalse(a["ready"])


if __name__ == "__main__":
    unittest.main()
