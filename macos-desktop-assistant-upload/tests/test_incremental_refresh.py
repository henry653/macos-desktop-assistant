from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_daily_briefing as pipeline
from refresh_plan import attach_exam_inventory, build_refresh_plan

NOW = datetime(2026, 9, 3, 19, tzinfo=ZoneInfo("America/New_York"))


class IncrementalRefreshTests(unittest.TestCase):
    def test_inventory_distinguishes_hidden_future_exam_and_missing_coverage(self):
        state = {"assessment_events": [{"course": "STOR455.002.FA26", "title": "Module 1 Exam", "exam_date": "2026-09-11"}]}
        config = {"courses": [{"course": "STOR455"}, {"course": "COMP455"}]}
        payload = {}
        attach_exam_inventory(payload, state, config, NOW)
        self.assertEqual(1, len(payload["exam_inventory"]))
        self.assertEqual(0, payload["exam_coverage"][0]["visible_7d_count"])
        self.assertEqual("unverified", payload["exam_coverage"][1]["state"])
        attach_exam_inventory(payload, state, config, NOW + timedelta(days=1))
        self.assertEqual(1, payload["exam_coverage"][0]["visible_7d_count"])

    def test_schedule_cache_invalidates_when_file_changes_but_preserves_announcement_check(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "syllabus.txt"
            source.write_text("test schedule")
            config = {"courses": [{"course": "STOR415", "announcements_url": "https://example.org/announcements", "schedule_sources": [{"url": "https://example.org/syllabus", "local_path": str(source)}]}]}
            state = {"assessment_coverage": {"STOR415": {"source_cache": {"https://example.org/syllabus": {"checked_at": NOW.isoformat(), "parsed": True, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}}}}
            plan = build_refresh_plan(state, config, NOW)
            self.assertEqual("reuse_verified_schedule", plan["courses"][0]["schedule_sources"][0]["action"])
            self.assertEqual("https://example.org/announcements", plan["courses"][0]["announcements_url"])
            source.write_text("changed schedule")
            self.assertEqual("read_local_file", build_refresh_plan(state, config, NOW)["courses"][0]["schedule_sources"][0]["action"])

    def test_local_finalize_does_not_fetch_mail_advance_cursor_or_render_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_path, state_path = root / "input.json", root / "state.json"
            source = {"state": "error", "checked_at": "2026-09-02T08:00:00-04:00", "error": "auth_required"}
            data_path.write_text(json.dumps({"assignments": [], "exams": [], "messages": [], "sources": {"gmail": source}}))
            state_path.write_text(json.dumps({"swe_last_successful_at": "2026-09-01T08:00:00-04:00"}))
            argv = ["runner", "--input", str(data_path), "--state", str(state_path), "--local-only", "--prepare-applications"]
            with patch.object(sys, "argv", argv), patch.object(pipeline, "_collect_swe_gmail_messages", side_effect=AssertionError("network used")), patch.object(pipeline, "_run_renderer", return_value={"wallpaper": None}) as render, contextlib.redirect_stdout(io.StringIO()):
                pipeline.main()
            self.assertEqual(1, render.call_count)
            result = json.loads(data_path.read_text())
            state = json.loads(state_path.read_text())
            self.assertEqual(source["checked_at"], result["sources"]["gmail"]["checked_at"])
            self.assertEqual(source["error"], result["sources"]["gmail"]["error"])
            self.assertEqual("2026-09-01T08:00:00-04:00", state["swe_last_successful_at"])
            self.assertNotIn("last_successful_run", state) if "last_successful_run" not in state else self.assertIsNone(state["last_successful_run"])


if __name__ == "__main__":
    unittest.main()
