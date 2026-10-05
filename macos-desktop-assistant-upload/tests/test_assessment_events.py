from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assignment_filters import visible_assignments  # noqa: E402
from export_widget_snapshot import build_snapshot  # noqa: E402
from render_my_planner import render_html  # noqa: E402
from run_daily_briefing import _merge_assessment_events  # noqa: E402


ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=ZONE)


class AssessmentEventStateTests(unittest.TestCase):
    def test_reschedule_new_source_replaces_old_event_and_dropped_course_is_removed(self) -> None:
        state = {"assessment_events": [
            {"course": "COMP455.002.FA26", "title": "Quiz 0", "exam_date": "2026-09-10", "source_url": "https://example.org/old", "fingerprint": "legacy"},
            {"course": "COMP311.002.FA26", "title": "Midterm", "exam_date": "2026-09-10"},
        ]}
        payload = {"exams": [{"course": "COMP 455", "title": "Quiz 0", "exam_date": "2026-09-11", "source_url": "https://example.org/new"}]}
        events = _merge_assessment_events(payload, state, NOW)
        self.assertEqual(1, len(events))
        self.assertEqual("2026-09-11", events[0]["exam_at"])
        self.assertEqual("https://example.org/new", events[0]["source_url"])

    def test_recovers_future_event_but_payload_only_keeps_seven_days(self) -> None:
        state = {
            "assessment_events": [{
                "course": "STOR 445",
                "title": "Final Exam",
                "exam_at": "2026-09-20T10:00:00-04:00",
                "source_url": "https://uncch.instructure.com/exams/final",
            }]
        }
        payload: dict[str, object] = {"assignments": [], "exams": []}

        recovered = _merge_assessment_events(payload, state, NOW)

        self.assertEqual(1, len(recovered))
        self.assertEqual(1, len(state["assessment_events"]))
        self.assertEqual([], visible_assignments(payload["exams"], NOW))

    def test_date_only_event_expires_after_its_calendar_day(self) -> None:
        state: dict[str, object] = {"assessment_events": []}
        event = {
            "course": "COMP 455",
            "title": "Midterm",
            "exam_date": "2026-09-03",
            "time_precision": "date_only",
            "source_url": "https://uncch.instructure.com/exams/midterm",
        }
        payload: dict[str, object] = {"assignments": [], "exams": [event]}

        self.assertEqual(1, len(_merge_assessment_events(payload, state, NOW)))
        next_day = datetime(2026, 9, 4, 0, 0, tzinfo=ZONE)
        self.assertEqual([], _merge_assessment_events({"assignments": [], "exams": []}, state, next_day))

    def test_html_and_snapshot_use_exam_review_without_warning_surface(self) -> None:
        review_url = "https://uncch.instructure.com/modules/midterm-review"
        data = {
            "generated_at": NOW.isoformat(),
            "timezone": str(ZONE),
            "sources": {},
            "messages": [],
            "assignments": [{
                "course": "STOR 415",
                "title": "Old homework",
                "due_at": "2026-09-02T12:00:00-04:00",
                "status": "past_due_unsubmitted",
                "url": "https://uncch.instructure.com/assignments/old",
            }],
            "exams": [{
                "course": "STOR 445",
                "title": "Midterm 1",
                "exam_date": "2026-09-08",
                "time_precision": "date_only",
                "source_url": "https://uncch.instructure.com/exams/1",
                "review_url": review_url,
                "review_state": "verified",
            }],
        }

        document, counts = render_html(data, NOW)
        snapshot = build_snapshot(data, NOW)

        self.assertIn(review_url, document)
        self.assertIn("复习资料已核验", document)
        self.assertNotIn("12:00 AM", document)
        self.assertNotIn("警告 · Overdue", document)
        self.assertEqual(1, counts["exams"])
        self.assertEqual(0, counts["warnings"])
        exam = next(item for item in snapshot["items"] if item["kind"] == "exam")
        self.assertEqual(review_url, exam["url"])
        self.assertEqual(0, snapshot["stats"]["warnings"])


if __name__ == "__main__":
    unittest.main()
