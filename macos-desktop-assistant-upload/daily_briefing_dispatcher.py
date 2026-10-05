#!/usr/bin/env python3
"""Dispatch the full Codex briefing after login or a missed scheduled run.

This helper never scrapes sites or rewrites the report itself. The complete
Chrome-based heartbeat remains the only data collector. This process keeps the
last verified wallpaper visible while it waits for the morning window. Only
when it is about to start cloud collection or queue the Codex collector does it
apply a dated transition image. Failed starts are rolled back to the last
verified wallpaper.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
REPORT = ROOT / "daily_briefing.json"
WALLPAPER_STATUS = ROOT / "wallpaper_status.json"
WALLPAPER_REQUEST = ROOT / "wallpaper_request.json"
WALLPAPER_TRANSITION_STATUS = ROOT / "wallpaper_transition_status.json"
WALLPAPER_APPLY_LOCK = ROOT / "wallpaper_apply.lock"
CLOUD_SYNC_STATUS = ROOT / "cloud_sync_status.json"
RUN_STATUS = ROOT / "daily_run_status.json"
LOCK = ROOT / "daily_briefing_dispatch.lock"
CODEX_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CHATGPT_APP = Path("/Applications/ChatGPT.app")
CLOUD_CLIENT = Path(os.environ.get("DESKTOP_ASSISTANT_CLOUD_CLIENT", str(ROOT / "cloud_briefing_client.py")))
PYTHON = Path(os.environ.get("DESKTOP_ASSISTANT_PYTHON", "/usr/bin/python3"))
RUNTIME_PYTHON = Path(os.environ.get("DESKTOP_ASSISTANT_GRAPHICS_PYTHON", str(PYTHON)))
TRANSITION_RENDERER = ROOT / "render_login_transition.py"
WALLPAPER_MANAGER = ROOT / "wallpaper_manager.py"
BRIEFING_RENDERER = ROOT / "render_daily_briefing.py"
DESKTOP_PNG = Path.home() / "Desktop/Daily_Briefing.png"
DESKTOP_MARKDOWN = Path.home() / "Desktop/Daily_Briefing.md"
THREAD_ID = os.environ.get("DESKTOP_ASSISTANT_CODEX_THREAD_ID", "")
TIMEZONE = ZoneInfo("America/New_York")
MORNING_FRESHNESS_FLOOR = clock_time(7, 55)
EVENING_FRESHNESS_FLOOR = clock_time(18, 0)
LEASE_MINUTES = 120
QUEUED_TRANSITION_TIMEOUT = timedelta(minutes=3)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def boot_session_uuid() -> str | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def try_cloud_sync() -> dict[str, Any]:
    if not CLOUD_CLIENT.is_file() or not PYTHON.is_file():
        return {"status": "skipped", "reason": "cloud_client_not_installed"}
    try:
        result = subprocess.run(
            [str(PYTHON), str(CLOUD_CLIENT), "--sync-if-configured"],
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "reason": "cloud_sync_unavailable"}
    try:
        parsed = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if result.returncode != 0:
        return {
            "status": "error",
            "reason": parsed.get("reason") or parsed.get("error") or "cloud_sync_failed",
            "returncode": result.returncode,
        }
    return {
        "status": parsed.get("status") or parsed.get("state") or ("ok" if result.returncode == 0 else "error"),
        "reason": parsed.get("reason") or parsed.get("error"),
        "returncode": result.returncode,
    }


def try_login_transition() -> dict[str, Any]:
    """Apply a same-day transition without treating it as a fresh briefing."""
    if not RUNTIME_PYTHON.is_file() or not TRANSITION_RENDERER.is_file():
        return {"status": "skipped", "reason": "login_transition_renderer_unavailable"}
    try:
        result = subprocess.run(
            [str(RUNTIME_PYTHON), str(TRANSITION_RENDERER), "--apply"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "reason": "login_transition_unavailable"}
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        parsed = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if result.returncode != 0:
        return {
            "status": "error",
            "reason": parsed.get("reason") or parsed.get("error") or "login_transition_failed",
            "returncode": result.returncode,
        }
    return {
        "status": parsed.get("status") or "ok",
        "action": parsed.get("action"),
        "target": parsed.get("target"),
        "current_desktop_verified": parsed.get("current_desktop_verified"),
        "all_spaces_verified": parsed.get("all_spaces_verified"),
        "lock_screen_source_verified": parsed.get("lock_screen_source_verified"),
        "current_desktop_configured": parsed.get("current_desktop_configured"),
        "all_spaces_configured": parsed.get("all_spaces_configured"),
        "lock_screen_source_configured": parsed.get("lock_screen_source_configured"),
        "presentation_verified": parsed.get("presentation_verified"),
        "elapsed_ms": parsed.get("elapsed_ms"),
        "not_a_completed_briefing": True,
        "returncode": result.returncode,
    }


def transition_may_be_active(now: datetime) -> bool:
    """Return whether today's transition is newer than the final receipt.

    The wallpaper manager deliberately keeps transition and final receipts in
    separate files. Comparing their presentation timestamps lets the dispatcher
    avoid invoking a GUI rollback every minute while still recovering from a
    process that was interrupted after painting the transition.
    """
    transition = read_json(WALLPAPER_TRANSITION_STATUS)
    if (
        transition.get("source") != "login_transition"
        or transition.get("valid_for_date") != now.date().isoformat()
    ):
        return False
    transition_checked = parse_datetime(
        transition.get("presentation_verified_at") or transition.get("checked_at")
    )
    if transition_checked is None:
        return False
    final = read_json(WALLPAPER_STATUS)
    final_checked = parse_datetime(
        final.get("presentation_verified_at") or final.get("checked_at")
    )
    return final_checked is None or transition_checked > final_checked


def try_restore_previous_wallpaper(now: datetime) -> dict[str, Any]:
    """Restore last-good only when the manager confirms transition is active."""
    if not transition_may_be_active(now):
        return {"status": "not_needed", "reason": "transition_not_active"}
    if not RUNTIME_PYTHON.is_file() or not WALLPAPER_MANAGER.is_file():
        return {"status": "error", "error": "wallpaper_manager_unavailable"}
    return _run_local_json_command(
        [str(RUNTIME_PYTHON), str(WALLPAPER_MANAGER), "--rollback-login-transition"],
        timeout=45,
    )


def _run_local_json_command(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": f"local_helper_unavailable:{type(exc).__name__}"}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if result.returncode != 0 and not parsed:
        return {"status": "error", "error": "local_helper_failed", "returncode": result.returncode}
    parsed["returncode"] = result.returncode
    return parsed


def try_wallpaper_repair() -> dict[str, Any]:
    """Repair a valid same-day request without re-running any data source."""
    if not RUNTIME_PYTHON.is_file() or not WALLPAPER_MANAGER.is_file():
        return {"status": "error", "error": "wallpaper_manager_unavailable"}
    return _run_local_json_command(
        [str(RUNTIME_PYTHON), str(WALLPAPER_MANAGER), "--consume-request"],
        timeout=45,
    )


def try_local_wallpaper_rebuild() -> dict[str, Any]:
    """Rebuild a wallpaper from today's existing report; never scrape sites."""
    if not RUNTIME_PYTHON.is_file() or not BRIEFING_RENDERER.is_file() or not REPORT.is_file():
        return {"status": "error", "error": "local_renderer_unavailable"}
    return _run_local_json_command(
        [
            str(RUNTIME_PYTHON),
            str(BRIEFING_RENDERER),
            "--input", str(REPORT),
            "--png", str(DESKTOP_PNG),
            "--markdown", str(DESKTOP_MARKDOWN),
            "--set-wallpaper",
        ],
        timeout=60,
    )


def required_refresh_slot(now: datetime) -> dict[str, str]:
    """Return the latest briefing slot that must be represented at ``now``.

    The morning job may begin at 07:55 so the 08:00 plan is ready on time. The
    evening job becomes due at 18:00. Between midnight and 07:55, yesterday's
    evening slot remains the latest obligation; this makes a Mac that slept
    through 18:00 catch up on its next login or wake.
    """
    if now.time() >= EVENING_FRESHNESS_FLOOR:
        kind = "evening"
        scheduled_at = now.replace(hour=18, minute=0, second=0, microsecond=0)
        trigger = "login_or_missed_1800"
    elif now.time() >= MORNING_FRESHNESS_FLOOR:
        kind = "morning"
        scheduled_at = now.replace(
            hour=MORNING_FRESHNESS_FLOOR.hour,
            minute=MORNING_FRESHNESS_FLOOR.minute,
            second=0,
            microsecond=0,
        )
        trigger = "login_or_missed_0800"
    else:
        kind = "evening"
        yesterday = now - timedelta(days=1)
        scheduled_at = yesterday.replace(hour=18, minute=0, second=0, microsecond=0)
        trigger = "login_or_missed_1800"
    return {
        "key": f"{scheduled_at.date().isoformat()}-{kind}",
        "kind": kind,
        "scheduled_at": scheduled_at.isoformat(),
        "trigger": trigger,
    }


def data_refresh_state(now: datetime, report: dict[str, Any]) -> dict[str, Any]:
    """Verify report/source freshness against the latest required slot."""
    slot = required_refresh_slot(now)
    scheduled_at = parse_datetime(slot["scheduled_at"])
    assert scheduled_at is not None
    reasons: list[str] = []
    generated_at = parse_datetime(report.get("generated_at"))
    if generated_at is None:
        reasons.append(f"report_missing_for_{slot['kind']}_window")
    elif generated_at < scheduled_at:
        reasons.append(f"report_predates_{slot['kind']}_window")

    sources = report.get("sources") if isinstance(report.get("sources"), dict) else {}
    checked_after_slot = [
        source
        for source in sources.values()
        if isinstance(source, dict)
        and source.get("state") == "ok"
        and (checked := parse_datetime(source.get("checked_at"))) is not None
        and checked >= scheduled_at
    ]
    if not checked_after_slot:
        reasons.append(f"no_source_verified_since_{slot['kind']}_window")
    return {
        "fresh": not reasons,
        "reasons": reasons,
        "generated_at": generated_at,
        "refresh_slot": slot,
    }


def verification_state(now: datetime) -> dict[str, Any]:
    report = read_json(REPORT)
    refresh = data_refresh_state(now, report)
    reasons = list(refresh["reasons"])
    generated_at = refresh["generated_at"]
    report_fresh = bool(refresh["fresh"])

    WALLPAPER_APPLY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with WALLPAPER_APPLY_LOCK.open("a+", encoding="utf-8") as wallpaper_lock:
        fcntl.flock(wallpaper_lock.fileno(), fcntl.LOCK_SH)
        status = read_json(WALLPAPER_STATUS)
        request = read_json(WALLPAPER_REQUEST)
        target = Path(str(status.get("target") or "")).expanduser()
        request_target = Path(str(request.get("target") or "")).expanduser()
        checked_at = parse_datetime(status.get("checked_at"))
        presentation_checked_at = parse_datetime(
            status.get("presentation_verified_at") or status.get("checked_at")
        )
        current_boot_session = boot_session_uuid()
        actual_hash = file_sha256(target)
        expected_hash = status.get("image_sha256")
        receipt_matches = (
            request.get("schema_version") == 1
            and isinstance(request.get("request_id"), str)
            and request.get("request_id") == status.get("request_id")
            and request.get("valid_for_date") == now.date().isoformat()
            and status.get("valid_for_date") == now.date().isoformat()
            and target.is_file()
            and request_target.resolve() == target.resolve()
            and isinstance(expected_hash, str)
            and request.get("image_sha256") == expected_hash == actual_hash
            and request.get("source") == status.get("source")
            and request.get("source_generated_at") == status.get("source_generated_at")
        )
        source = status.get("source")
        source_generated = parse_datetime(status.get("source_generated_at"))
        provenance_verified = (
            source_generated is not None
            and source_generated.date() == now.date()
            and generated_at is not None
            and abs((source_generated - generated_at).total_seconds()) < 1
        )
        if source == "cloud_signed_manifest":
            cloud_status = read_json(CLOUD_SYNC_STATUS)
            provenance_verified = provenance_verified and (
                status.get("manifest_signature_verified") is True
                and request.get("manifest_signature_verified") is True
                and cloud_status.get("state") == "ok"
                and cloud_status.get("briefing_date") == now.date().isoformat()
                and cloud_status.get("generated_at") == status.get("source_generated_at")
                and cloud_status.get("target") == str(target.resolve())
                and cloud_status.get("wallpaper_sha256") == actual_hash
            )
        elif source != "local_renderer":
            provenance_verified = False
        presentation_verified = (
            status.get("verification_schema_version") == 2
            and status.get("status") == "ok"
            and status.get("current_desktop_configured") is True
            and status.get("all_spaces_configured") is True
            and status.get("lock_screen_source_configured") is True
            and status.get("presentation_verified") is True
            and status.get("presentation_state") == "verified"
            and current_boot_session is not None
            and status.get("boot_session_uuid") == current_boot_session
            and presentation_checked_at is not None
            and abs((now - presentation_checked_at).total_seconds()) <= 180
            and checked_at is not None
            and checked_at.date() == now.date()
            and receipt_matches
            and provenance_verified
        )
    if not receipt_matches:
        reasons.append("wallpaper_receipt_not_verified")
    if not provenance_verified:
        reasons.append("wallpaper_provenance_not_verified")
    if not presentation_verified:
        reasons.append("wallpaper_not_verified_today")
    request_valid = bool(receipt_matches and provenance_verified)
    return {
        "complete": not reasons,
        "report_fresh": report_fresh,
        "report_generated_at": generated_at.isoformat() if generated_at is not None else None,
        "request_valid": request_valid,
        "presentation_verified": bool(presentation_verified),
        "reasons": reasons,
        "refresh_slot": refresh["refresh_slot"],
    }


def today_is_verified(now: datetime) -> tuple[bool, list[str]]:
    state = verification_state(now)
    return bool(state["complete"]), list(state["reasons"])


def active_lease(status: dict[str, Any], now: datetime) -> bool:
    lease = parse_datetime(status.get("lease_expires_at"))
    return status.get("state") in {"dispatching", "queued", "in_progress"} and lease is not None and lease > now


def accepted_queue_for_slot(status: dict[str, Any], refresh_slot_key: str) -> bool:
    """Return whether Codex already accepted this slot's collection request.

    A lease bounds worker ownership; it is not permission to enqueue a second
    copy of a request that the Codex host already accepted.  In particular, a
    busy thread may leave a queued request untouched past ``lease_expires_at``.
    The successful queue receipt is therefore the durable idempotency signal
    for the slot until the worker changes the state to a terminal value.
    """
    return (
        status.get("refresh_slot_key") == refresh_slot_key
        and status.get("queue_returncode") == 0
        and status.get("state") in {"dispatching", "queued", "in_progress"}
    )


def retry_wait_active(status: dict[str, Any], now: datetime) -> bool:
    retry_at = parse_datetime(status.get("next_retry_at"))
    return status.get("state") == "retry_wait" and retry_at is not None and retry_at > now


def wallpaper_repair_wait_active(status: dict[str, Any], now: datetime) -> bool:
    retry_at = parse_datetime(status.get("next_retry_at"))
    return (
        status.get("state") == "wallpaper_repair_pending"
        and retry_at is not None
        and retry_at > now
    )


def record_verified_snapshot(status: dict[str, Any], now: datetime) -> None:
    """Clear stale failures and persist the evidence used for this check."""
    report = read_json(REPORT)
    wallpaper = read_json(WALLPAPER_STATUS)
    sources = report.get("sources") if isinstance(report.get("sources"), dict) else {}
    refresh_slot = required_refresh_slot(now)
    status.update({
        "state": "ok",
        "completion_status": "verified_complete",
        "verified_at": now.isoformat(),
        "report_generated_at": report.get("generated_at"),
        "source_states": {
            name: {
                "state": source.get("state"),
                "checked_at": source.get("checked_at"),
                "error": source.get("error"),
            }
            for name, source in sources.items()
            if isinstance(source, dict)
        },
        "wallpaper": {
            "status": wallpaper.get("status"),
            "target": wallpaper.get("target"),
            "presentation_verified": wallpaper.get("presentation_verified") is True,
            "presentation_verified_at": wallpaper.get("presentation_verified_at"),
            "current_desktop_verified": wallpaper.get("current_desktop_verified") is True,
            "all_spaces_verified": wallpaper.get("all_spaces_verified") is True,
            "lock_screen_source_verified": wallpaper.get("lock_screen_source_verified") is True,
        },
        "wallpaper_repair_pending": False,
        "last_verified_refresh_slot": refresh_slot,
        "error": None,
    })
    # Older collectors wrote these top-level presentation receipts.  Keep them
    # synchronized so a successful dispatcher verification cannot coexist with
    # stale ``pending_gui_session`` values from an earlier attempt.
    status["desktop_wallpaper"] = {
        "status": "ok",
        "path": str(DESKTOP_PNG),
        "target": wallpaper.get("target"),
        "sha256": wallpaper.get("image_sha256"),
        "verified": True,
        "current_desktop_verified": True,
        "all_spaces_verified": True,
        "presentation_verified": True,
        "presentation_verified_at": wallpaper.get("presentation_verified_at"),
    }
    status["lock_screen_source"] = {
        "status": "ok",
        "target": wallpaper.get("target"),
        "sha256": wallpaper.get("image_sha256"),
        "verified": True,
        "lock_screen_source_verified": True,
        "verified_at": wallpaper.get("presentation_verified_at") or now.isoformat(),
    }
    status.pop("next_retry_at", None)
    status.pop("lease_expires_at", None)


def catch_up_run_id(
    status: dict[str, Any],
    now: datetime,
    refresh_slot_key: str | None = None,
) -> tuple[str, str]:
    """Reuse one run id only for retries of the same scheduled slot."""
    started = parse_datetime(status.get("started_at"))
    existing_id = status.get("run_id")
    if refresh_slot_key is None:
        same_slot = started is not None and started.date() == now.date()
    else:
        same_slot = status.get("refresh_slot_key") == refresh_slot_key
    if (
        isinstance(existing_id, str)
        and existing_id.startswith("login-catchup-")
        and started is not None
        and same_slot
        and status.get("state") in {
            "dispatching",
            "queued",
            "in_progress",
            "retry_wait",
            "failed",
        }
    ):
        return existing_id, started.isoformat()
    slot_fragment = refresh_slot_key or now.strftime("%Y%m%d")
    return (
        f"login-catchup-{slot_fragment}-{uuid.uuid4().hex[:8]}",
        now.isoformat(),
    )


def queue_catch_up(run_id: str) -> subprocess.CompletedProcess[str]:
    if not THREAD_ID:
        return subprocess.CompletedProcess([], 2, "", "DESKTOP_ASSISTANT_CODEX_THREAD_ID is not configured")
    message = (
        "这是一条由用户授权的开机/唤醒补跑请求。"
        f"run_id={run_id}。请立即读取并完整执行本机 automation id=automation 的现有 prompt，"
        f"先使用 {RUN_STATUS} 做幂等检查和运行租约；完成后原子更新该状态，分别记录数据报告、"
        "桌面壁纸和锁屏来源是否验证成功。不要打开、填写或提交任何实习申请；"
        "不要运行实习自动申请或产生任何外部写操作。"
    )
    subprocess.run(
        ["/usr/bin/open", "-gj", "-a", "ChatGPT"],
        check=False,
        capture_output=True,
        text=True,
    )
    # Exactly one queue attempt per scheduler invocation. A successful receipt
    # is persisted by ``main`` and prevents later launch/wake events from
    # enqueueing the same refresh slot again.
    return subprocess.run(
        [str(CODEX_CLI), "queue", "--thread", THREAD_ID, "--message", message],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)

    with LOCK.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        now = datetime.now(TIMEZONE)
        verification = verification_state(now)
        verified = bool(verification["complete"])
        reasons = list(verification["reasons"])
        refresh_slot = dict(verification["refresh_slot"])
        existing_status = read_json(RUN_STATUS)
        if args.dry_run:
            print(json.dumps({
                "status": "dry_run",
                "action": "none" if verified else "would_sync_or_queue",
                "fresh": verified,
                "reasons": reasons,
                "report_fresh": verification["report_fresh"],
                "request_valid": verification["request_valid"],
                "presentation_verified": verification["presentation_verified"],
                "refresh_slot": refresh_slot,
            }, ensure_ascii=False))
            return 0

        # Before the morning window, a verified previous-evening report is the
        # correct last-good state. Do not rebuild it merely to change the date,
        # and never leave a stale transition image on screen. If the evening
        # slot is missing, this branch is intentionally skipped so login/wake
        # proceeds to the full catch-up queue below.
        refresh_scheduled_at = parse_datetime(refresh_slot.get("scheduled_at"))
        report_generated_at = parse_datetime(verification.get("report_generated_at"))
        prior_evening_is_fresh = (
            not verified
            and verification["report_fresh"]
            and now.time() < MORNING_FRESHNESS_FLOOR
            and refresh_slot.get("kind") == "evening"
            and refresh_scheduled_at is not None
            and refresh_scheduled_at.date() < now.date()
            and report_generated_at is not None
            and report_generated_at.date() < now.date()
        )
        if prior_evening_is_fresh:
            if active_lease(existing_status, now):
                existing_status.update({
                    "scheduler_checked_at": now.isoformat(),
                    "scheduler_fresh": False,
                    "scheduler_reasons": reasons,
                    "required_refresh_slot": refresh_slot,
                })
                atomic_write_json(RUN_STATUS, existing_status)
                print(json.dumps({
                    "status": "ok",
                    "action": "deduplicated",
                    "fresh": False,
                    "refresh_slot": refresh_slot["key"],
                }, ensure_ascii=False))
                return 0
            rollback = try_restore_previous_wallpaper(now)
            now = datetime.now(TIMEZONE)
            existing_status.update({
                "state": "waiting_for_morning_window",
                "scheduler_checked_at": now.isoformat(),
                "scheduler_fresh": False,
                "scheduler_reasons": reasons,
                "required_refresh_slot": refresh_slot,
                "login_transition": {"status": "not_started"},
                "transition_rollback": rollback,
            })
            existing_status.pop("lease_expires_at", None)
            atomic_write_json(RUN_STATUS, existing_status)
            print(json.dumps({
                "status": "ok",
                "action": "waiting_until_0755",
                "fresh": False,
                "refresh_slot": refresh_slot["key"],
            }, ensure_ascii=False))
            return 0

        # A fresh report with a valid artifact never needs another browser/data
        # collection run. Repair only the local presentation; a later
        # login/wake, request-file event, or scheduled invocation can retry it
        # if the macOS GUI session is not ready yet.
        if not verified and verification["report_fresh"]:
            if wallpaper_repair_wait_active(existing_status, now):
                existing_status.update({
                    "scheduler_checked_at": now.isoformat(),
                    "scheduler_fresh": False,
                    "scheduler_reasons": reasons,
                    "required_refresh_slot": refresh_slot,
                })
                atomic_write_json(RUN_STATUS, existing_status)
                print(json.dumps({
                    "status": "ok",
                    "action": "waiting_for_wallpaper_repair",
                    "fresh": False,
                }, ensure_ascii=False))
                return 0

            repair = (
                try_wallpaper_repair()
                if verification["request_valid"]
                else try_local_wallpaper_rebuild()
            )
            now = datetime.now(TIMEZONE)
            verification = verification_state(now)
            verified = bool(verification["complete"])
            reasons = list(verification["reasons"])
            refresh_slot = dict(verification["refresh_slot"])
            status = read_json(RUN_STATUS)
            status.update({
                "scheduler_checked_at": now.isoformat(),
                "scheduler_fresh": verified,
                "scheduler_reasons": reasons,
                "wallpaper_repair": repair,
                "wallpaper_repair_attempt_count": int(
                    status.get("wallpaper_repair_attempt_count") or 0
                ) + 1,
            })
            if verified:
                record_verified_snapshot(status, now)
                atomic_write_json(RUN_STATUS, status)
                print(json.dumps({
                    "status": "ok",
                    "action": "wallpaper_repaired",
                    "fresh": True,
                }, ensure_ascii=False))
                return 0

            status.update({
                "state": "wallpaper_repair_pending",
                "wallpaper_repair_pending": True,
                "next_retry_at": (now + timedelta(seconds=60)).isoformat(),
                "error": repair.get("error") or repair.get("status") or "wallpaper_repair_pending",
            })
            status.pop("lease_expires_at", None)
            atomic_write_json(RUN_STATUS, status)
            print(json.dumps({
                "status": "pending",
                "action": "wallpaper_repair_scheduled",
                "fresh": False,
            }, ensure_ascii=False))
            return 0

        login_transition = {"status": "not_started"}

        # A successful Codex queue receipt is stronger than the expiring worker
        # lease.  A busy conversation can legitimately remain queued beyond
        # that lease, so never enqueue another copy for the same slot.  Preserve
        # the original run_id and record the explicit deduplication receipt.
        if not verified and accepted_queue_for_slot(existing_status, refresh_slot["key"]):
            queued_at = parse_datetime(
                existing_status.get("queued_at")
                or existing_status.get("last_queue_attempt_at")
            )
            queued_transition_timed_out = (
                existing_status.get("state") == "queued"
                and existing_status.get("worker_claimed") is not True
                and queued_at is not None
                and now - queued_at >= QUEUED_TRANSITION_TIMEOUT
                and transition_may_be_active(now)
            )
            if queued_transition_timed_out:
                existing_status["transition_rollback"] = try_restore_previous_wallpaper(now)
            existing_status.update({
                "scheduler_checked_at": now.isoformat(),
                "scheduler_fresh": False,
                "scheduler_reasons": reasons,
                "required_refresh_slot": refresh_slot,
                "scheduler_action": "accepted_queue_deduplicated",
                "accepted_queue_deduplicated_at": now.isoformat(),
            })
            atomic_write_json(RUN_STATUS, existing_status)
            print(json.dumps({
                "status": "ok",
                "action": "accepted_queue_deduplicated",
                "fresh": False,
                "run_id": existing_status.get("run_id"),
                "refresh_slot": refresh_slot["key"],
            }, ensure_ascii=False))
            return 0

        # A collector that reports failure may have left its temporary image on
        # screen. Restore last-good once, then wait for the next login/wake or
        # scheduled retry event.
        # The wallpaper manager performs a second, lock-protected check so a
        # concurrently completed final image can never be rolled back.
        previous_run_failed = existing_status.get("state") == "failed"
        if not verified and previous_run_failed and transition_may_be_active(now):
            rollback = try_restore_previous_wallpaper(now)
            now = datetime.now(TIMEZONE)
            existing_status.update({
                "state": "retry_wait",
                "scheduler_checked_at": now.isoformat(),
                "scheduler_fresh": False,
                "scheduler_reasons": reasons,
                "transition_rollback": rollback,
                "next_retry_at": (now + timedelta(seconds=60)).isoformat(),
                "error": existing_status.get("error") or "previous_collection_failed",
            })
            existing_status.pop("lease_expires_at", None)
            atomic_write_json(RUN_STATUS, existing_status)
            print(json.dumps({
                "status": "pending",
                "action": "failed_run_wallpaper_restored",
                "fresh": False,
            }, ensure_ascii=False))
            return 0

        # An active collector owns its transition. A queue can, however, wait
        # behind another conversation for a long time. In that case restore the
        # last-good wallpaper after a short grace period while preserving the
        # queue lease; the completed collector will still install the new image.
        if not verified and active_lease(existing_status, now):
            queued_at = parse_datetime(
                existing_status.get("queued_at")
                or existing_status.get("last_queue_attempt_at")
            )
            queued_transition_timed_out = (
                existing_status.get("state") == "queued"
                and existing_status.get("worker_claimed") is not True
                and queued_at is not None
                and now - queued_at >= QUEUED_TRANSITION_TIMEOUT
                and transition_may_be_active(now)
            )
            if queued_transition_timed_out:
                existing_status["transition_rollback"] = try_restore_previous_wallpaper(now)
            existing_status["scheduler_checked_at"] = now.isoformat()
            existing_status["scheduler_fresh"] = False
            existing_status["scheduler_reasons"] = reasons
            existing_status["required_refresh_slot"] = refresh_slot
            atomic_write_json(RUN_STATUS, existing_status)
            print(json.dumps({"status": "ok", "action": "deduplicated", "fresh": False}, ensure_ascii=False))
            return 0

        if not verified and retry_wait_active(existing_status, now):
            rollback = try_restore_previous_wallpaper(now)
            existing_status["scheduler_checked_at"] = now.isoformat()
            existing_status["scheduler_fresh"] = False
            existing_status["scheduler_reasons"] = reasons
            existing_status["required_refresh_slot"] = refresh_slot
            existing_status["transition_rollback"] = rollback
            atomic_write_json(RUN_STATUS, existing_status)
            print(json.dumps({"status": "ok", "action": "waiting_to_retry", "fresh": False}, ensure_ascii=False))
            return 0

        # We are now in an actual morning/evening execution path (including a
        # missed-evening login/wake catch-up). Paint the temporary image only
        # immediately before network sync / collector queuing.
        if not verified:
            login_transition = try_login_transition()
            now = datetime.now(TIMEZONE)
            verified, reasons = today_is_verified(now)
            refresh_slot = required_refresh_slot(now)

        cloud_sync = {"status": "not_needed"}
        if not verified:
            cloud_sync = try_cloud_sync()
            now = datetime.now(TIMEZONE)
            verified, reasons = today_is_verified(now)
            refresh_slot = required_refresh_slot(now)
        status = read_json(RUN_STATUS)
        status["scheduler_checked_at"] = now.isoformat()
        status["scheduler_fresh"] = verified
        status["scheduler_reasons"] = reasons
        status["required_refresh_slot"] = refresh_slot
        status["cloud_sync"] = cloud_sync
        status["login_transition"] = login_transition

        if verified:
            if not active_lease(status, now):
                record_verified_snapshot(status, now)
            atomic_write_json(RUN_STATUS, status)
            action = (
                "cloud_synced" if cloud_sync.get("status") == "ok"
                else "verified_lease_preserved" if active_lease(status, now)
                else "none"
            )
            print(json.dumps({"status": "ok", "action": action, "fresh": True}, ensure_ascii=False))
            return 0

        if not CODEX_CLI.is_file() or not CHATGPT_APP.is_dir():
            rollback = try_restore_previous_wallpaper(now)
            now = datetime.now(TIMEZONE)
            status.update({
                "state": "failed",
                "error": "codex_desktop_host_missing",
                "finished_at": now.isoformat(),
                "transition_rollback": rollback,
            })
            atomic_write_json(RUN_STATUS, status)
            return 1

        run_id, started_at = catch_up_run_id(status, now, refresh_slot["key"])
        status.update({
            "state": "dispatching",
            "run_id": run_id,
            "trigger": refresh_slot["trigger"],
            "refresh_slot_key": refresh_slot["key"],
            "refresh_slot_kind": refresh_slot["kind"],
            "refresh_slot_scheduled_at": refresh_slot["scheduled_at"],
            "started_at": started_at,
            "last_queue_attempt_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
            "error": None,
        })
        status.pop("next_retry_at", None)
        atomic_write_json(RUN_STATUS, status)

        result = queue_catch_up(run_id)
        finished_at = datetime.now(TIMEZONE)
        status["finished_at"] = finished_at.isoformat()
        status["queue_returncode"] = result.returncode
        if result.returncode == 0:
            status.update({
                "state": "queued",
                "queued_at": finished_at.isoformat(),
                "worker_claimed": False,
                "error": None,
            })
            atomic_write_json(RUN_STATUS, status)
            print(json.dumps({"status": "ok", "action": "queued", "run_id": run_id}, ensure_ascii=False))
            return 0

        rollback = try_restore_previous_wallpaper(finished_at)
        retry_started_at = datetime.now(TIMEZONE)
        status.update({
            "state": "retry_wait",
            "error": "codex_queue_failed",
            "next_retry_at": (retry_started_at + timedelta(seconds=60)).isoformat(),
            "transition_rollback": rollback,
        })
        status.pop("lease_expires_at", None)
        atomic_write_json(RUN_STATUS, status)
        print(json.dumps({"status": "error", "action": "queue_retry_scheduled", "run_id": run_id}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
