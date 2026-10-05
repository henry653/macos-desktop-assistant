from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "wallpaper_manager.py"
SPEC = importlib.util.spec_from_file_location("wallpaper_manager_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 9, 2, 8, 5, tzinfo=ZoneInfo("America/New_York"))
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


class TransitionRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "Daily Briefing"
        self.root.mkdir()
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch.object(manager, "datetime", FrozenDateTime))
        for name, value in {
            "ROOT": self.root,
            "REQUEST": self.root / "request.json",
            "STATUS": self.root / "status.json",
            "TRANSITION_STATUS": self.root / "transition.json",
            "LAST_GOOD_REQUEST": self.root / "last-good-request.json",
            "LAST_GOOD_STATUS": self.root / "last-good-status.json",
            "APPLY_LOCK": self.root / "apply.lock",
        }.items():
            self.stack.enter_context(mock.patch.object(manager, name, value))

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _artifact(self, name: str) -> tuple[Path, str]:
        path = self.root / name
        path.write_bytes(name.encode("utf-8"))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def _transition(self) -> None:
        self._write(manager.TRANSITION_STATUS, {
            "source": "login_transition",
            "valid_for_date": "2026-09-02",
            "checked_at": "2026-09-02T08:00:00-04:00",
        })

    def test_active_transition_restores_last_good(self) -> None:
        self._transition()
        target, digest = self._artifact("yesterday.png")
        request = {
            "schema_version": 1,
            "source": "local_renderer",
            "valid_for_date": "2026-09-01",
            "target": str(target),
            "image_sha256": digest,
        }
        self._write(manager.REQUEST, request)
        self._write(manager.LAST_GOOD_REQUEST, request)
        self._write(manager.STATUS, {"checked_at": "2026-09-01T20:00:00-04:00"})
        apply = self.stack.enter_context(mock.patch.object(
            manager,
            "_apply_wallpaper_locked",
            return_value={"status": "ok", "target": str(target), "image_sha256": digest},
        ))
        self.stack.enter_context(mock.patch.object(manager, "_record_last_good"))

        result = manager.rollback_login_transition()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "last_good_restored")
        apply.assert_called_once()

    def test_same_day_final_wins_race_with_rollback(self) -> None:
        self._transition()
        target, digest = self._artifact("today.png")
        self._write(manager.REQUEST, {
            "schema_version": 1,
            "source": "local_renderer",
            "valid_for_date": "2026-09-02",
            "target": str(target),
            "image_sha256": digest,
            "defer_last_good": False,
        })
        apply = self.stack.enter_context(mock.patch.object(
            manager,
            "_apply_wallpaper_locked",
            return_value={"status": "ok", "target": str(target), "image_sha256": digest},
        ))
        self.stack.enter_context(mock.patch.object(manager, "_record_last_good"))

        result = manager.rollback_login_transition()

        self.assertEqual(result["action"], "newer_final_preserved")
        apply.assert_called_once()

    def test_newer_final_receipt_makes_rollback_a_noop(self) -> None:
        self._transition()
        self._write(manager.REQUEST, {})
        self._write(manager.STATUS, {"checked_at": "2026-09-02T08:04:00-04:00"})
        apply = self.stack.enter_context(mock.patch.object(manager, "_apply_wallpaper_locked"))

        result = manager.rollback_login_transition()

        self.assertEqual(result, {"status": "idle", "reason": "transition_not_active"})
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
