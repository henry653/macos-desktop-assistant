"""Atomic run-lease receipts for the read-only daily briefing worker."""
from __future__ import annotations

import argparse
import fcntl
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_briefing_dispatcher import atomic_write_json, read_json, active_lease

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["begin", "blocked"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--scheduled-at", required=True)
    parser.add_argument("--error", default="mac_locked")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("America/New_York"))
    path = ROOT / "daily_run_status.json"
    with (ROOT / "daily_briefing_dispatch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = read_json(path)
        if args.action == "begin":
            matching_queue = prior.get("state") == "queued" and prior.get("run_id") == args.run_id and prior.get("refresh_slot_key") == args.slot
            if (active_lease(prior, now) and not matching_queue) or (prior.get("refresh_slot_key") == args.slot and prior.get("state") in {"ok", "degraded_complete"}):
                print(json.dumps({"state": "duplicate_skipped", "run_id": prior.get("run_id")}))
                return
            status = {
                "state": "in_progress", "run_id": args.run_id,
                "trigger": "login_catchup" if args.run_id.startswith("login-catchup-") else "heartbeat", "refresh_slot_key": args.slot,
                "refresh_slot_kind": args.slot.rsplit("-", 1)[-1],
                "refresh_slot_scheduled_at": args.scheduled_at,
                "started_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=10)).isoformat(),
                "perf": {"started_at": now.isoformat(), "collection_deadline_at": (now + timedelta(minutes=4)).isoformat()},
                "previous_verified_wallpaper": prior.get("wallpaper") or prior.get("previous_verified_wallpaper"),
            }
        else:
            if prior.get("run_id") != args.run_id or prior.get("state") != "in_progress":
                raise SystemExit("Run lease is not owned by this invocation")
            status = prior
            sources = {key: {"state": "error", "checked_at": None, "attempted_at": now.isoformat(), "error": args.error, "auth_required": False, "cursor_advance_allowed": False} for key in ("canvas", "gmail", "outlook", "linkedin")}
            report = read_json(ROOT / "daily_briefing.json")
            status.update({
                "state": "failed", "finished_at": now.isoformat(), "lease_expires_at": None,
                "error": args.error, "sources": sources,
                "report_generated_at": report.get("generated_at"),
                "data_report": {"status": "preserved_previous", "generated_this_run": False, "verified_this_run": False},
                "wallpaper": {"status": "preserved_previous", "target": (prior.get("previous_verified_wallpaper") or {}).get("target"), "current_desktop_verified": False, "all_spaces_verified": False, "lock_screen_source_verified": False, "verification_attempted": False},
            })
            status["perf"].update({"finished_at": now.isoformat(), "duration_seconds": round((now - datetime.fromisoformat(prior["started_at"])).total_seconds(), 3), "sources": {key: {"started_at": None, "finished_at": None, "error": args.error} for key in sources}})
        atomic_write_json(path, status)
        print(json.dumps({"state": status["state"], "run_id": status["run_id"], "error": status.get("error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
