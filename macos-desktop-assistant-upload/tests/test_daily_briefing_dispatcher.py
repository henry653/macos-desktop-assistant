from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "daily_briefing_dispatcher.py"
LAUNCH_AGENT_PATH = Path.home() / "Library/LaunchAgents/io.local.desktop-assistant-login.plist"
WALLPAPER_AGENT_PATH = Path.home() / "Library/LaunchAgents/io.local.desktop-assistant-wallpaper.plist"
PLANNER_AGENT_PATH = Path.home() / "Library/LaunchAgents/io.local.desktop-assistant-hotspots.plist"
SPEC = importlib.util.spec_from_file_location("daily_briefing_dispatcher_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dispatcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatcher)


class FrozenDateTime(datetime):
    current = datetime(2026, 9, 2, 3, 50, tzinfo=ZoneInfo("America/New_York"))

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


class DispatcherTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "Daily Briefing"
        self.root.mkdir()
        self.cli = base / "codex"
        self.cli.write_text("", encoding="utf-8")
        self.app = base / "ChatGPT.app"
        self.app.mkdir()
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch.object(dispatcher, "datetime", FrozenDateTime))
        for name, value in {
            "ROOT": self.root,
            "REPORT": self.root / "daily_briefing.json",
            "RUN_STATUS": self.root / "daily_run_status.json",
            "LOCK": self.root / "dispatcher.lock",
            "WALLPAPER_STATUS": self.root / "wallpaper_status.json",
            "WALLPAPER_TRANSITION_STATUS": self.root / "wallpaper_transition_status.json",
            "CODEX_CLI": self.cli,
            "CHATGPT_APP": self.app,
        }.items():
            self.stack.enter_context(mock.patch.object(dispatcher, name, value))
        self.stack.enter_context(mock.patch.object(sys, "argv", ["dispatcher-test"]))
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "verification_state",
            side_effect=lambda now: {
                "complete": False,
                "report_fresh": False,
                "request_valid": False,
                "presentation_verified": False,
                "reasons": ["report_not_generated_today"],
                "refresh_slot": dispatcher.required_refresh_slot(now),
            },
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "today_is_verified",
            return_value=(False, ["report_not_generated_today"]),
        ))

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _status(self) -> dict:
        return json.loads(dispatcher.RUN_STATUS.read_text(encoding="utf-8"))

    def test_before_morning_window_never_paints_transition_and_repairs_old_one(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 3, 50, tzinfo=dispatcher.TIMEZONE)
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "verification_state",
            return_value={
                "complete": False,
                "report_fresh": True,
                "report_generated_at": "2026-09-01T18:15:00-04:00",
                "request_valid": False,
                "presentation_verified": False,
                "reasons": ["wallpaper_not_verified_today"],
                "refresh_slot": dispatcher.required_refresh_slot(FrozenDateTime.current),
            },
        ))
        transition = self.stack.enter_context(mock.patch.object(dispatcher, "try_login_transition"))
        restore = self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_restore_previous_wallpaper",
            return_value={"status": "ok", "action": "last_good_restored"},
        ))
        self.stack.enter_context(mock.patch.object(dispatcher, "transition_may_be_active", return_value=True))
        cloud = self.stack.enter_context(mock.patch.object(dispatcher, "try_cloud_sync"))
        queue = self.stack.enter_context(mock.patch.object(dispatcher, "queue_catch_up"))

        self.assertEqual(dispatcher.main(), 0)

        transition.assert_not_called()
        cloud.assert_not_called()
        queue.assert_not_called()
        restore.assert_called_once_with(FrozenDateTime.current)
        self.assertEqual(self._status()["state"], "waiting_for_morning_window")

    def test_missed_evening_queues_on_next_pre_morning_wake(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 3, 50, tzinfo=dispatcher.TIMEZONE)
        self.stack.enter_context(mock.patch.object(
            dispatcher, "try_login_transition", return_value={"status": "ok"}
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher, "try_cloud_sync", return_value={"status": "skipped"}
        ))
        queue = self.stack.enter_context(mock.patch.object(
            dispatcher,
            "queue_catch_up",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ))

        self.assertEqual(dispatcher.main(), 0)

        queue.assert_called_once()
        status = self._status()
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["trigger"], "login_or_missed_1800")
        self.assertEqual(status["refresh_slot_key"], "2026-09-01-evening")

    def test_evening_window_deduplicates_an_existing_live_lease(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 18, 5, tzinfo=dispatcher.TIMEZONE)
        dispatcher.RUN_STATUS.write_text(json.dumps({
            "state": "queued",
            "run_id": "login-catchup-2026-09-02-evening-existing",
            "refresh_slot_key": "2026-09-02-evening",
            "lease_expires_at": "2026-09-02T19:00:00-04:00",
        }))
        queue = self.stack.enter_context(mock.patch.object(dispatcher, "queue_catch_up"))
        transition = self.stack.enter_context(mock.patch.object(dispatcher, "try_login_transition"))

        self.assertEqual(dispatcher.main(), 0)

        queue.assert_not_called()
        transition.assert_not_called()
        self.assertEqual(
            self._status()["run_id"],
            "login-catchup-2026-09-02-evening-existing",
        )

    def test_expired_lease_does_not_requeue_an_accepted_slot(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 8, 5, tzinfo=dispatcher.TIMEZONE)
        dispatcher.RUN_STATUS.write_text(json.dumps({
            "state": "queued",
            "run_id": "login-catchup-2026-09-02-morning-accepted",
            "refresh_slot_key": "2026-09-02-morning",
            "queue_returncode": 0,
            "queued_at": "2026-09-02T08:00:00-04:00",
            "lease_expires_at": "2026-09-02T08:01:00-04:00",
            "worker_claimed": False,
        }), encoding="utf-8")
        queue = self.stack.enter_context(mock.patch.object(dispatcher, "queue_catch_up"))
        transition = self.stack.enter_context(mock.patch.object(dispatcher, "try_login_transition"))
        cloud = self.stack.enter_context(mock.patch.object(dispatcher, "try_cloud_sync"))
        self.stack.enter_context(mock.patch.object(
            dispatcher, "transition_may_be_active", return_value=False
        ))

        self.assertEqual(dispatcher.main(), 0)

        queue.assert_not_called()
        transition.assert_not_called()
        cloud.assert_not_called()
        status = self._status()
        self.assertEqual(status["state"], "queued")
        self.assertEqual(
            status["run_id"],
            "login-catchup-2026-09-02-morning-accepted",
        )
        self.assertEqual(status["scheduler_action"], "accepted_queue_deduplicated")
        self.assertEqual(
            status["accepted_queue_deduplicated_at"],
            "2026-09-02T08:05:00-04:00",
        )

    def test_successful_queue_receipt_is_durable_for_all_active_states(self) -> None:
        for state in ("dispatching", "queued", "in_progress"):
            with self.subTest(state=state):
                self.assertTrue(dispatcher.accepted_queue_for_slot({
                    "state": state,
                    "refresh_slot_key": "2026-09-02-morning",
                    "queue_returncode": 0,
                    "lease_expires_at": "2026-09-02T07:00:00-04:00",
                }, "2026-09-02-morning"))
        self.assertFalse(dispatcher.accepted_queue_for_slot({
            "state": "queued",
            "refresh_slot_key": "2026-09-01-evening",
            "queue_returncode": 0,
        }, "2026-09-02-morning"))

    def test_waiting_queue_restores_transition_after_short_grace_period(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 8, 5, tzinfo=dispatcher.TIMEZONE)
        dispatcher.RUN_STATUS.write_text(json.dumps({
            "state": "queued",
            "run_id": "login-catchup-2026-09-02-morning-existing",
            "refresh_slot_key": "2026-09-02-morning",
            "queued_at": "2026-09-02T08:00:00-04:00",
            "lease_expires_at": "2026-09-02T10:00:00-04:00",
            "worker_claimed": False,
        }))
        self.stack.enter_context(mock.patch.object(
            dispatcher, "transition_may_be_active", return_value=True
        ))
        restore = self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_restore_previous_wallpaper",
            return_value={"status": "ok", "action": "last_good_restored"},
        ))
        queue = self.stack.enter_context(mock.patch.object(dispatcher, "queue_catch_up"))

        self.assertEqual(dispatcher.main(), 0)

        queue.assert_not_called()
        restore.assert_called_once_with(FrozenDateTime.current)
        status = self._status()
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["transition_rollback"]["action"], "last_good_restored")

    def test_morning_and_evening_slots_have_independent_run_ids(self) -> None:
        now = datetime(2026, 9, 2, 18, 5, tzinfo=dispatcher.TIMEZONE)
        existing = {
            "state": "failed",
            "run_id": "login-catchup-2026-09-02-morning-old",
            "refresh_slot_key": "2026-09-02-morning",
            "started_at": "2026-09-02T08:00:00-04:00",
        }

        run_id, _ = dispatcher.catch_up_run_id(existing, now, "2026-09-02-evening")

        self.assertNotEqual(run_id, existing["run_id"])
        self.assertIn("2026-09-02-evening", run_id)

    def test_refresh_slots_cover_morning_evening_and_overnight(self) -> None:
        morning = dispatcher.required_refresh_slot(
            datetime(2026, 9, 2, 8, 0, tzinfo=dispatcher.TIMEZONE)
        )
        evening = dispatcher.required_refresh_slot(
            datetime(2026, 9, 2, 18, 0, tzinfo=dispatcher.TIMEZONE)
        )
        overnight = dispatcher.required_refresh_slot(
            datetime(2026, 9, 3, 7, 0, tzinfo=dispatcher.TIMEZONE)
        )

        self.assertEqual(morning["key"], "2026-09-02-morning")
        self.assertEqual(evening["key"], "2026-09-02-evening")
        self.assertEqual(overnight["key"], "2026-09-02-evening")

    def test_evening_freshness_requires_report_and_source_after_1800(self) -> None:
        now = datetime(2026, 9, 2, 18, 5, tzinfo=dispatcher.TIMEZONE)
        stale = dispatcher.data_refresh_state(now, {
            "generated_at": "2026-09-02T17:59:59-04:00",
            "sources": {
                "gmail": {"state": "ok", "checked_at": "2026-09-02T17:59:59-04:00"},
            },
        })
        fresh = dispatcher.data_refresh_state(now, {
            "generated_at": "2026-09-02T18:01:00-04:00",
            "sources": {
                "gmail": {"state": "ok", "checked_at": "2026-09-02T18:00:30-04:00"},
            },
        })

        self.assertFalse(stale["fresh"])
        self.assertIn("report_predates_evening_window", stale["reasons"])
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["refresh_slot"]["key"], "2026-09-02-evening")

    def test_login_launch_agent_has_no_duplicate_scheduler_or_minute_polling(self) -> None:
        if not LAUNCH_AGENT_PATH.exists():
            self.skipTest("optional installed launch agent is absent")
        with LAUNCH_AGENT_PATH.open("rb") as handle:
            value = plistlib.load(handle)
        self.assertTrue(value["RunAtLoad"])
        self.assertEqual(value["LimitLoadToSessionType"], "Aqua")
        self.assertNotIn("StartCalendarInterval", value)
        self.assertNotIn("StartInterval", value)

    def test_file_watch_launch_agents_do_not_poll(self) -> None:
        if not WALLPAPER_AGENT_PATH.exists() or not PLANNER_AGENT_PATH.exists():
            self.skipTest("optional installed launch agents are absent")
        with WALLPAPER_AGENT_PATH.open("rb") as handle:
            wallpaper = plistlib.load(handle)
        self.assertTrue(wallpaper["RunAtLoad"])
        self.assertNotIn("StartInterval", wallpaper)
        self.assertTrue(any(
            watched.endswith("wallpaper_request.json")
            for watched in wallpaper["WatchPaths"]
        ))

        with PLANNER_AGENT_PATH.open("rb") as handle:
            planner = plistlib.load(handle)
        self.assertTrue(planner["RunAtLoad"])
        self.assertNotIn("StartInterval", planner)
        self.assertTrue(planner["ProgramArguments"][0].endswith("My Planner.app/Contents/MacOS/MyPlanner"))

    def test_verified_snapshot_replaces_legacy_pending_receipts(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 12, 0, tzinfo=dispatcher.TIMEZONE)
        dispatcher.REPORT.write_text(json.dumps({
            "generated_at": "2026-09-02T09:30:25-04:00",
            "sources": {
                "canvas": {
                    "state": "ok",
                    "checked_at": "2026-09-02T09:23:06-04:00",
                    "error": None,
                },
            },
        }), encoding="utf-8")
        dispatcher.WALLPAPER_STATUS.write_text(json.dumps({
            "status": "ok",
            "target": "/tmp/verified-wallpaper.png",
            "image_sha256": "abc123",
            "presentation_verified": True,
            "presentation_verified_at": "2026-09-02T11:53:04-04:00",
            "current_desktop_verified": True,
            "all_spaces_verified": True,
            "lock_screen_source_verified": True,
        }), encoding="utf-8")
        status = {
            "completion_status": "deduplicated_report_complete_wallpaper_pending_gui",
            "desktop_wallpaper": {"status": "pending_gui_session", "verified": False},
            "lock_screen_source": {"status": "pending_gui_session", "verified": False},
        }

        dispatcher.record_verified_snapshot(status, FrozenDateTime.current)

        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["completion_status"], "verified_complete")
        self.assertEqual(status["desktop_wallpaper"]["status"], "ok")
        self.assertTrue(status["desktop_wallpaper"]["verified"])
        self.assertTrue(status["desktop_wallpaper"]["current_desktop_verified"])
        self.assertTrue(status["desktop_wallpaper"]["all_spaces_verified"])
        self.assertTrue(status["desktop_wallpaper"]["presentation_verified"])
        self.assertEqual(status["lock_screen_source"]["status"], "ok")
        self.assertTrue(status["lock_screen_source"]["verified"])
        self.assertTrue(status["lock_screen_source"]["lock_screen_source_verified"])

    def test_transition_starts_only_on_real_morning_execution_path(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 8, 0, tzinfo=dispatcher.TIMEZONE)
        order: list[str] = []
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_login_transition",
            side_effect=lambda: order.append("transition") or {"status": "ok"},
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_cloud_sync",
            side_effect=lambda: order.append("cloud") or {"status": "skipped"},
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "queue_catch_up",
            side_effect=lambda run_id: order.append("queue") or subprocess.CompletedProcess([], 0, "", ""),
        ))

        self.assertEqual(dispatcher.main(), 0)

        self.assertEqual(order, ["transition", "cloud", "queue"])
        self.assertEqual(self._status()["state"], "queued")

    def test_queue_failure_rolls_transition_back(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 8, 0, tzinfo=dispatcher.TIMEZONE)
        self.stack.enter_context(mock.patch.object(
            dispatcher, "try_login_transition", return_value={"status": "ok"}
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher, "try_cloud_sync", return_value={"status": "skipped"}
        ))
        self.stack.enter_context(mock.patch.object(
            dispatcher,
            "queue_catch_up",
            return_value=subprocess.CompletedProcess([], 1, "", "queue failed"),
        ))
        restore = self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_restore_previous_wallpaper",
            return_value={"status": "ok", "action": "last_good_restored"},
        ))

        self.assertEqual(dispatcher.main(), 1)

        restore.assert_called_once()
        status = self._status()
        self.assertEqual(status["state"], "retry_wait")
        self.assertEqual(status["transition_rollback"]["action"], "last_good_restored")

    def test_observed_collector_failure_restores_before_retrying(self) -> None:
        FrozenDateTime.current = datetime(2026, 9, 2, 8, 5, tzinfo=dispatcher.TIMEZONE)
        dispatcher.RUN_STATUS.write_text(json.dumps({"state": "failed", "error": "collector_failed"}))
        self.stack.enter_context(mock.patch.object(dispatcher, "transition_may_be_active", return_value=True))
        restore = self.stack.enter_context(mock.patch.object(
            dispatcher,
            "try_restore_previous_wallpaper",
            return_value={"status": "ok", "action": "last_good_restored"},
        ))
        transition = self.stack.enter_context(mock.patch.object(dispatcher, "try_login_transition"))
        queue = self.stack.enter_context(mock.patch.object(dispatcher, "queue_catch_up"))

        self.assertEqual(dispatcher.main(), 0)

        restore.assert_called_once()
        transition.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(self._status()["state"], "retry_wait")


if __name__ == "__main__":
    unittest.main()
