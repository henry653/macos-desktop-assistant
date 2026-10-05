#!/usr/bin/env python3
"""Queue one guarded, user-visible internship application batch from the wallpaper."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intern_preparation import strict_display_items


ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
REPORT = ROOT / "daily_briefing.json"
REQUEST = ROOT / "intern_application_request.json"
LOCK = ROOT / "intern_application_dispatch.lock"
CODEX_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CHATGPT_APP = Path("/Applications/ChatGPT.app")
THREAD_ID = os.environ.get("DESKTOP_ASSISTANT_CODEX_THREAD_ID", "")
ZONE = ZoneInfo("America/New_York")
DEDUPE_WINDOW = timedelta(minutes=5)


def read_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE)


def build_request(report: dict[str, Any], now: datetime) -> dict[str, Any]:
    messages = report.get("messages") if isinstance(report.get("messages"), list) else []
    items = strict_display_items(messages, report.get("intern_preparation_summary"), zone=ZONE)
    safe_items = [
        {
            "fingerprint": str(item.get("fingerprint") or ""),
            "company": str(item.get("company") or ""),
            "role": str(item.get("role") or ""),
            "apply_url": str(item.get("apply_url") or ""),
            "queue_status": str(item.get("queue_status") or ""),
            "decision_reason": str(item.get("decision_reason") or ""),
            "resume_variant": str(item.get("resume_variant") or ""),
            "resume_attachment_ready": bool(item.get("resume_attachment_ready")),
        }
        for item in items
    ]
    counts = {
        status: sum(item["queue_status"] == status for item in safe_items)
        for status in ("prepared", "manual_review", "manual_decision")
    }
    return {
        "schema_version": 1,
        "request_id": f"intern-batch-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "created_at": now.isoformat(),
        "source_report_generated_at": report.get("generated_at"),
        "state": "created",
        "trigger": "wallpaper" ,
        "counts": counts,
        "items": safe_items,
        "guardrails": {
            "large_or_limited": "manual_decision_only",
            "excluded": ["TikTok", "PhD-only", "citizenship_or_residency_mismatch", "background_mismatch"],
            "form_fill_requires_user_visible_interactive_task": True,
            "final_submission_requires_action_time_confirmation": True,
            "captcha_requires_user_handoff": True,
            "submitted_requires_success_evidence": True,
        },
    }


def queue_task(request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    if not THREAD_ID:
        return subprocess.CompletedProcess([], 2, "", "DESKTOP_ASSISTANT_CODEX_THREAD_ID is not configured")
    request_id = request["request_id"]
    counts = request["counts"]
    message = (
        "我刚从桌面壁纸里的‘自动申请中心’发起了实习申请批次。"
        f"request_id={request_id}。请读取 {REQUEST} 和 {ROOT / 'state.json'}。先对 manual_review 岗位做只读资格与限投核验，"
        "严格排除 TikTok、PhD-only、明确要求但我不满足的公民/永久居民条件、背景明显不符、已申请或重复岗位；"
        "大公司及限投岗位只列出供我手动决定。对合格小公司按岗位匹配 Resume P/A，向我展示准确的待申请清单。"
        "在向第三方表单输入个人资料以及最终提交申请前，必须在本任务里按实际批次请求确认；验证码必须停下让我接管。"
        "只有看到申请网站明确成功页或确认状态才能记录 submitted。"
        f"本地严格队列当前为 READY {counts['prepared']}、待核验 {counts['manual_review']}、手动决定 {counts['manual_decision']}。"
    )
    subprocess.run(
        ["/usr/bin/open", "-gj", "-a", "ChatGPT"],
        check=False,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        [str(CODEX_CLI), "queue", "--thread", THREAD_ID, "--message", message],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-wallpaper", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZONE)

    with LOCK.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        prior = read_object(REQUEST)
        prior_created = parse_time(prior.get("created_at"))
        if (
            prior.get("state") in {"created", "queued"}
            and prior_created is not None
            and now - prior_created < DEDUPE_WINDOW
        ):
            print(json.dumps({"status": "deduplicated", "request_id": prior.get("request_id")}, ensure_ascii=False))
            return 0

        report = read_object(REPORT)
        if not report:
            print(json.dumps({"status": "blocked", "reason": "daily_briefing_missing"}, ensure_ascii=False))
            return 2
        request = build_request(report, now)
        if args.dry_run:
            print(json.dumps({"status": "dry_run", **request}, ensure_ascii=False))
            return 0
        if not CODEX_CLI.is_file() or not CHATGPT_APP.is_dir():
            request.update({"state": "failed", "error": "codex_host_unavailable"})
            atomic_write(REQUEST, request)
            return 3

        atomic_write(REQUEST, request)
        result = queue_task(request)
        request.update({
            "state": "queued" if result.returncode == 0 else "failed",
            "queued_at": datetime.now(ZONE).isoformat(),
            "queue_returncode": result.returncode,
            "error": None if result.returncode == 0 else "codex_queue_failed",
        })
        atomic_write(REQUEST, request)
        print(json.dumps({
            "status": request["state"],
            "request_id": request["request_id"],
            "counts": request["counts"],
        }, ensure_ascii=False))
        return 0 if result.returncode == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
