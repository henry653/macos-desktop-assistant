from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assignment_filters import (  # noqa: E402
    has_verified_exam_review,
    is_exam_assignment,
    is_hidden_assignment,
    is_within_assignment_horizon,
    merge_assignments_and_exams,
    preferred_assignment_url,
    visible_assignments,
)


ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 2, 10, 0, tzinfo=ZONE)


class AssignmentFilterTests(unittest.TestCase):
    def test_scheduled_classroom_quizzes_get_seven_day_window(self) -> None:
        for course, title in [("COMP455.002.FA26", "Quiz 0"), ("STOR 415", "Quiz 1"), ("STOR 455", "Module 1 Test")]:
            item = {"course": course, "title": title, "due_at": "2026-09-08"}
            self.assertTrue(is_exam_assignment(item))
            self.assertTrue(is_within_assignment_horizon(item, NOW))
        for course, title in [("COMP455", "Quiz Review"), ("STOR415", "Practice Quiz 1"), ("STOR455", "Homework Quiz 1"), ("OTHER101", "Quiz 1")]:
            self.assertFalse(is_exam_assignment({"course": course, "title": title}))

    def test_applies_course_specific_visibility_rules(self) -> None:
        items = [
            {"course": "COMP311.002.FA26", "status": "not_submitted", "due_at": "2026-09-03T12:00:00-04:00"},
            {"course": "COMP 455", "title": "Other homework", "status": "past_due_unsubmitted", "due_at": "2026-09-01T12:00:00-04:00"},
            {"course": "COMP455.002.FA26", "title": "HW00: Math Review（Gradescope 显示 No Submission）", "status": "past_due_unsubmitted", "due_at": "2026-08-26T23:59:00-04:00"},
            {"course": "STOR455.001", "title": "Complete-Before_Class: reading", "status": "not_submitted", "due_at": "2026-09-03T08:00:00-04:00"},
            {"course": "STOR 455", "title": "Complete Before Class 2", "status": "past_due_unsubmitted", "due_at": "2026-09-01T08:00:00-04:00"},
            {"course": "STOR 455", "title": "Homework", "status": "past_due_unsubmitted", "due_at": "2026-09-01T08:00:00-04:00"},
        ]

        kept = visible_assignments(items, NOW)

        self.assertEqual([items[3]], kept)
        self.assertTrue(is_hidden_assignment(items[0], NOW))
        self.assertTrue(is_hidden_assignment(items[1], NOW))
        self.assertTrue(is_hidden_assignment(items[2], NOW))
        self.assertFalse(is_hidden_assignment(items[3], NOW))
        self.assertTrue(is_hidden_assignment(items[4], NOW))
        self.assertTrue(is_hidden_assignment(items[5], NOW))

    def test_exam_window_review_link_and_date_only_boundary(self) -> None:
        exam = {
            "course": "STOR 445",
            "title": "Midterm 1",
            "exam_date": "2026-09-08",
            "time_precision": "date_only",
            "source_url": "https://uncch.instructure.com/exams/1",
            "review_url": "https://uncch.instructure.com/modules/review-1",
            "review_state": "verified",
        }
        self.assertTrue(is_exam_assignment(exam))
        self.assertTrue(is_within_assignment_horizon(exam, NOW))
        self.assertEqual(exam["review_url"], preferred_assignment_url(exam))
        self.assertTrue(has_verified_exam_review(exam))
        unverified = {**exam, "review_state": "pending"}
        self.assertEqual(exam["source_url"], preferred_assignment_url(unverified))
        self.assertFalse(has_verified_exam_review(unverified))
        missing_state = {key: value for key, value in exam.items() if key != "review_state"}
        self.assertEqual(exam["source_url"], preferred_assignment_url(missing_state))
        self.assertFalse(has_verified_exam_review(missing_state))
        self.assertFalse(is_hidden_assignment(exam, datetime(2026, 9, 8, 12, tzinfo=ZONE)))
        self.assertTrue(is_hidden_assignment(exam, datetime(2026, 9, 9, 0, tzinfo=ZONE)))

    def test_exam_detection_rejects_review_and_merges_dedicated_schema(self) -> None:
        review = {
            "course": "COMP 455",
            "title": "Practice Exam Review",
            "is_exam": True,
            "due_at": "2026-09-08T10:00:00-04:00",
        }
        self.assertFalse(is_exam_assignment(review))
        merged = merge_assignments_and_exams([], [{
            "course": "COMP 455",
            "title": "Final Exam",
            "exam_at": "2026-09-08T10:00:00-04:00",
            "source_url": "https://uncch.instructure.com/exams/final",
        }])
        self.assertEqual(1, len(merged))
        self.assertTrue(is_exam_assignment(merged[0]))
        self.assertEqual("2026-09-08T10:00:00-04:00", merged[0]["due_at"])


if __name__ == "__main__":
    unittest.main()
