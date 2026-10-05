"""Small, local collection plan: reuse verified schedules, surface coverage gaps.

This module never collects private data, changes source timestamps, or marks a
course checked merely because an old event is in its cache. The browser worker
consumes this plan and records explicit course receipts in assessment_coverage.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from assignment_filters import assignment_due_at, assignment_expiry_at, is_hidden_assignment, is_within_assignment_horizon


def course_key(value: Any) -> str:
    match = re.match(r"([A-Z]+)\s*(\d+)", str(value or "").upper())
    return "".join(match.groups()) if match else str(value or "").strip()


def future_events(state: dict, now: datetime) -> list[dict]:
    result = []
    for event in state.get("assessment_events", []):
        if not isinstance(event, dict):
            continue
        normalized = {**event, "is_exam": True}
        expiry = assignment_expiry_at(normalized)
        if expiry and expiry > now and not is_hidden_assignment(normalized, now):
            result.append(normalized)
    return sorted(result, key=lambda e: str(assignment_due_at(e) or ""))


def attach_exam_inventory(payload: dict, state: dict, config: dict, now: datetime) -> None:
    """Preserve the semester inventory even when wallpaper shows seven days."""
    events = future_events(state, now)
    payload["exam_inventory"] = events
    receipts = state.get("assessment_coverage", {})
    courses = config.get("courses", [])
    if not courses:
        courses = [{"course": key} for key in sorted({course_key(e.get("course")) for e in events})]
    coverage = []
    for course in courses:
        key = course_key(course.get("course"))
        if key == "COMP311":
            continue
        matches = [e for e in events if course_key(e.get("course")) == key]
        receipt = receipts.get(key, {})
        coverage.append({
            "course": key,
            "state": receipt.get("state", "unverified"),
            "checked_at": receipt.get("checked_at"),
            "error": receipt.get("error"),
            "known_future_count": len(matches),
            "visible_7d_count": sum(is_within_assignment_horizon(e, now) for e in matches),
            "next_assessment": {"title": matches[0].get("title"), "date": assignment_due_at(matches[0])} if matches else None,
            "coverage_note": receipt.get("coverage_note", "No complete per-course audit recorded; cached exams are not proof of complete coverage."),
        })
    payload["exam_coverage"] = coverage


def _local_hash(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def build_refresh_plan(state: dict, config: dict, now: datetime) -> dict:
    """Plan full source coverage with incremental reads and a bounded work list.

    Time budgets are worker instructions, not a promise about browser or model
    latency. Expired/changed local sources require re-extraction; fresh files
    with unchanged hashes reuse their verified records. Announcements are still
    checked each run for reschedules and new quiz dates.
    """
    receipts = state.get("assessment_coverage", {})
    tasks = []
    for course in config.get("courses", []):
        key = course_key(course.get("course"))
        if key == "COMP311":
            continue
        receipt = receipts.get(key, {})
        source_tasks = []
        for source in course.get("schedule_sources", []):
            previous = receipt.get("source_cache", {}).get(source["url"], {})
            current_hash = _local_hash(source["local_path"]) if source.get("local_path") else None
            same_file = bool(current_hash and current_hash == previous.get("sha256"))
            try:
                checked = datetime.fromisoformat(str(previous.get("checked_at", "")).replace("Z", "+00:00"))
                fresh = checked.tzinfo is not None and timedelta(0) <= now - checked < timedelta(hours=24)
            except ValueError:
                fresh = False
            action = "reuse_verified_schedule" if fresh and (not source.get("local_path") or same_file) and previous.get("parsed") is True else "read_local_file" if current_hash else "read_source"
            source_tasks.append({**source, "action": action, "sha256": current_hash, "last_checked_at": previous.get("checked_at")})
        tasks.append({
            "course": key,
            "announcements_url": course.get("announcements_url"),
            "announcements_since": receipt.get("announcements_checked_at"),
            "assessment_terms": course.get("assessment_terms", ["exam", "midterm", "test"]),
            "schedule_sources": source_tasks,
            "coverage_state": receipt.get("state", "unverified"),
            "pending": receipt.get("pending", []),
        })
    payload: dict = {}
    attach_exam_inventory(payload, state, config, now)
    return {
        "generated_at": now.isoformat(),
        "collection_deadline_at": (now + timedelta(seconds=240)).isoformat(),
        "target_finish_at": (now + timedelta(seconds=300)).isoformat(),
        "budget": {"collection_seconds": 240, "finalize_seconds": 60, "page_load_seconds": 15, "retry_per_failed_page": 0, "job_description_pages_before_wallpaper": 0},
        "courses": tasks,
        "exam_coverage": payload["exam_coverage"],
        "mail_since": state.get("source_last_successful", {}),
        "rules": [
            "Check all current courses' new announcement previews; retain a separate complete semester inventory.",
            "For missing schedules prefer mapped local files; do not traverse the same folders every run.",
            "Reuse unchanged verified schedules; check fresh announcements for date changes even with a cache hit.",
            "At the collection deadline retain verified results, mark unfinished sources partial/time_budget_exceeded and finalize once.",
            "Do not advance a source cursor or call cached data freshly checked after failure.",
            "Read only. Finalize via run_daily_briefing.py --local-only --prepare-applications --set-wallpaper exactly once.",
        ],
    }
