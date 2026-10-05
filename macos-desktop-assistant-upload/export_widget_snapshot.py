#!/usr/bin/env python3
"""Export a small, private WidgetKit snapshot from daily_briefing.json.

The exporter intentionally has no dependency on the wallpaper renderer.  A login,
wake, or scheduled refresh can run it after the briefing JSON is promoted.  The
snapshot contains only the fields a WidgetKit extension needs and is atomically
written into the shared App Group directory.  For this local prototype it is also
mirrored into the host app and widget extension's own Application Support
containers: ad-hoc signatures have no TeamIdentifier and macOS can deny their App
Group access.  A properly provisioned product can continue to use the App Group.

WidgetCenter must be called by a signed process that belongs to the app containing
the widget extension.  This script therefore supports two safe hand-off methods:

* ``--reload-helper`` executes a signed helper supplied by the host app.
* ``--reload-url`` opens the host app's custom URL handler.

It also always writes ``widget_reload_request.json`` when a reload is needed.  The
host app can consume that request and call
``WidgetCenter.shared.reloadTimelines(ofKind:)`` even if it was not running when
the data was exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

MODULE_DIR = str(Path(__file__).resolve().parent)
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from swe_list_summary import analyze_message, collect_verified_roles, latest_summary
from intern_preparation import strict_display_items
from assignment_filters import (
    assignment_due_at,
    has_verified_exam_review,
    is_exam_assignment,
    is_hidden_assignment,
    is_within_assignment_horizon,
    merge_assignments_and_exams,
    preferred_assignment_url,
)


ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
DEFAULT_INPUT = ROOT / "daily_briefing.json"
DEFAULT_GROUP_DIR = (
    Path(os.environ["MY_PLANNER_APP_GROUP_DIR"])
    if os.environ.get("MY_PLANNER_APP_GROUP_DIR")
    else Path.home() / "Library/Group Containers/group.io.local.desktop-assistant"
)
DEFAULT_WIDGET_EXTENSION_BUNDLE_ID = "io.local.DesktopAssistant.Widget"
DEFAULT_WIDGET_EXTENSION_SUPPORT_DIR = (
    Path(os.environ["MY_PLANNER_WIDGET_EXTENSION_SUPPORT_DIR"])
    if os.environ.get("MY_PLANNER_WIDGET_EXTENSION_SUPPORT_DIR")
    else Path.home()
    / "Library/Containers"
    / DEFAULT_WIDGET_EXTENSION_BUNDLE_ID
    / "Data/Library/Application Support"
)
DEFAULT_HOST_APP_BUNDLE_ID = "io.local.DesktopAssistant"
DEFAULT_HOST_APP_SUPPORT_DIR = (
    Path(os.environ["MY_PLANNER_HOST_APP_SUPPORT_DIR"])
    if os.environ.get("MY_PLANNER_HOST_APP_SUPPORT_DIR")
    else Path.home()
    / "Library/Containers"
    / DEFAULT_HOST_APP_BUNDLE_ID
    / "Data/Library/Application Support"
)
DEFAULT_SNAPSHOT_NAME = "MyPlannerSnapshot.json"
DEFAULT_STATUS = ROOT / "widget_export_status.json"
DEFAULT_WIDGET_KIND = "MyPlannerWidget"
DEFAULT_RELOAD_SCHEME = "myplanner-native"
DEFAULT_RELOAD_HOST = "reload-widget"
DEFAULT_ZONE = ZoneInfo("America/New_York")

SCHEMA_VERSION = 1
RED_HOURS = 12
YELLOW_HOURS = 48
WINDOW_HOURS = 72

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)")
MONEY_RE = re.compile(r"(?<!\w)[$€£]\s?\d[\d,.]*(?:\.\d{2})?")
GRADE_RE = re.compile(r"(?i)(?:grade|score|成绩|分数)\s*[:：-]?\s*\d+(?:\.\d+)?\s*(?:%|/\s*\d+)?")
CODE_RE = re.compile(
    r"(?i)(?:(?:verification\s*code|security\s*code|one[- ]time\s*code|otp|"
    r"验证码)\b\s*[:：-]?\s*[A-Z0-9-]{4,16}\b|"
    r"code\b\s*[:：-]\s*[A-Z0-9-]{4,16}\b)"
)
STOR_455_COURSE_RE = re.compile(r"(?i)^STOR\s*455(?=$|[\s._:-])")
COMPLETE_BEFORE_CLASS_RE = re.compile(r"(?i)^COMPLETE\s+BEFORE\s+CLASS\b")
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "code",
    "credential",
    "id_token",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "signature",
    "sig",
    "state",
    "token",
}
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
COMPLETED_STATUSES = {
    "submitted",
    "completed",
    "complete",
    "excused",
    "cancelled",
    "canceled",
    "past_due_submitted",
}


def _read_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("daily briefing must be a JSON object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some App Group container filesystems do not permit directory fsync.
            pass
    finally:
        temporary_path.unlink(missing_ok=True)


def _parse_datetime(value: Any, zone: ZoneInfo = DEFAULT_ZONE) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _iso_datetime(value: Any, zone: ZoneInfo = DEFAULT_ZONE) -> str | None:
    parsed = _parse_datetime(value, zone)
    return parsed.isoformat() if parsed else None


def _safe_text(value: Any, fallback: str = "", limit: int = 220) -> str:
    text = str(value).strip() if value is not None else fallback
    text = " ".join(text.split())
    text = CODE_RE.sub("[验证码已隐藏]", text)
    text = EMAIL_RE.sub("[邮箱已隐藏]", text)
    text = PHONE_RE.sub("[号码已隐藏]", text)
    text = MONEY_RE.sub("[金额已隐藏]", text)
    text = GRADE_RE.sub("[成绩已隐藏]", text)
    text = URL_RE.sub("[链接已隐藏]", text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text or fallback


def _safe_url(value: Any) -> str | None:
    """Return an HTTPS deep link with credentials and sensitive tracking removed."""

    candidate = str(value or "").strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None

    clean_query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.casefold()
        if normalized in SENSITIVE_QUERY_KEYS:
            continue
        if normalized in TRACKING_QUERY_KEYS or normalized.startswith(TRACKING_QUERY_PREFIXES):
            continue
        clean_query.append((key, item))

    fragment = parsed.fragment
    # OAuth implicit-flow credentials occasionally appear in URL fragments.
    # Preserve ordinary deep-link fragments (for example Gmail's inbox/thread)
    # but discard parameter-like fragments containing credential names.
    fragment_pairs = parse_qsl(fragment, keep_blank_values=True)
    if fragment_pairs and any(key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in fragment_pairs):
        fragment = ""

    return urlunparse(
        (
            "https",
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            urlencode(clean_query, doseq=True),
            fragment,
        )
    )


def _stable_id(kind: str, *values: Any) -> str:
    basis = "\x1f".join([kind, *(str(value or "").strip().casefold() for value in values)])
    return f"{kind[:1]}_{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:20]}"


def _urgency(due_at: str | None, now: datetime) -> str:
    due = _parse_datetime(due_at, ZoneInfo(str(now.tzinfo))) if due_at else None
    if due is None:
        return "unknown"
    hours = (due - now).total_seconds() / 3600
    if hours < 0:
        return "overdue"
    if hours <= RED_HOURS:
        return "red"
    if hours <= YELLOW_HOURS:
        return "yellow"
    return "blue"


def _source_from_url(url: str | None) -> str:
    hostname = (urlparse(url).hostname or "").casefold() if url else ""
    if "instructure.com" in hostname:
        return "canvas"
    if "gradescope.com" in hostname:
        return "gradescope"
    return "course"


def _is_hidden_complete_before_class(item: dict[str, Any]) -> bool:
    course = " ".join(str(item.get("course") or "").split())
    title = " ".join(str(item.get("title") or "").split())
    return bool(STOR_455_COURSE_RE.match(course) and COMPLETE_BEFORE_CLASS_RE.match(title))


def _assignment_items(raw_items: Any, now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        if is_hidden_assignment(raw, now):
            continue
        if not is_within_assignment_horizon(raw, now):
            continue
        status = str(raw.get("status") or "unknown").strip().casefold()
        if status in COMPLETED_STATUSES:
            continue
        exam = is_exam_assignment(raw)
        title = _safe_text(raw.get("title"), "Untitled assignment", 180)
        course = _safe_text(raw.get("course"), "Course", 80)
        due_at = _iso_datetime(assignment_due_at(raw), zone)
        url = _safe_url(preferred_assignment_url(raw))
        kind = "exam" if exam else "assignment"
        output.append(
            {
                "id": _stable_id(kind, course, title, due_at, url),
                "kind": kind,
                "title": title,
                "subtitle": course,
                "detail": (
                    "复习资料已核验"
                    if exam and has_verified_exam_review(raw)
                    else ("暂无已核验复习资料" if exam else "尚未完成")
                ),
                "dueAt": due_at,
                "receivedAt": None,
                "urgency": _urgency(due_at, now),
                "priority": None,
                "source": _source_from_url(url),
                "url": url,
                "allDay": bool(
                    str(raw.get("time_precision") or "").casefold() == "date_only"
                    or re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(assignment_due_at(raw) or "").strip())
                ),
            }
        )
    output.sort(key=lambda item: (item["kind"] != "exam", item["dueAt"] or "9999", item["title"]))
    return output


def _message_items(raw_items: Any, zone: ZoneInfo) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        priority = str(raw.get("priority") or "P2").upper()
        if priority not in {"P0", "P1", "P2"}:
            priority = "P2"
        source = _safe_text(raw.get("source"), "message", 32).casefold()
        sender = _safe_text(raw.get("sender_display") or raw.get("sender"), "Important sender", 100)
        title = _safe_text(raw.get("subject"), "Important message", 160)
        detail = _safe_text(raw.get("summary"), "请打开对应应用查看。", 220)
        received_at = _iso_datetime(raw.get("received_at"), zone)
        url = _safe_url(raw.get("url"))
        output.append(
            {
                "id": _stable_id("message", raw.get("fingerprint"), source, sender, title, url),
                "kind": "message",
                "title": title,
                "subtitle": f"{source.upper()} · {sender}",
                "detail": detail,
                "dueAt": None,
                "receivedAt": received_at,
                "urgency": "red" if priority == "P0" else ("yellow" if priority == "P1" else "blue"),
                "priority": priority,
                "source": source,
                "url": url,
            }
        )
    rank = {"P0": 0, "P1": 1, "P2": 2}
    output.sort(key=lambda item: (rank[item["priority"]], item["receivedAt"] or ""), reverse=False)
    return output


def _verified_internships(
    raw_messages: Any,
    maximum: int,
    zone: ZoneInfo = DEFAULT_ZONE,
    preparation_summary: Any = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list) or maximum <= 0:
        return []
    candidates: list[dict[str, Any]] = []
    strict_roles = strict_display_items(raw_messages, preparation_summary, zone=zone)
    for raw in strict_roles:
        company = _safe_text(raw.get("company"), "Company", 100)
        role = _safe_text(raw.get("role"), "Internship role", 180)
        url = _safe_url(raw.get("apply_url"))
        if not url:
            continue
        status = str(raw.get("queue_status") or "")
        detail_by_status = {
            "manual_decision": "手动决定",
            "manual_review": "要求待核验",
            "prepared": "可准备申请，提交前确认",
        }
        if status not in detail_by_status:
            continue
        candidates.append(
            {
                "id": _stable_id("internship", company, role, url),
                "kind": "internship",
                "title": role,
                "subtitle": company,
                "detail": detail_by_status[status],
                "dueAt": None,
                "receivedAt": None,
                "urgency": "yellow" if status == "manual_decision" else "blue",
                "priority": None,
                "source": "swe_list",
                "url": url,
                "internCategory": status,
            }
        )
    return candidates[:maximum]


def _swe_intern_summary(
    raw_messages: Any,
    generated_at: Any,
    zone: ZoneInfo,
    preparation_summary: Any = None,
) -> dict[str, Any] | None:
    """Return today's independently verified, sanitized SWE List aggregate.

    The small widget must not infer a total from the capped internship cards.  It
    receives only this aggregate and the already-sanitized inbox deep link.  A
    message qualifies only when its subject count, parser count, and unique links
    all agree.  The newest verified update at or before the report time wins; the
    widget labels an older update with its date instead of calling it "today".
    """

    if not isinstance(raw_messages, list):
        return None
    summaries, _ = collect_verified_roles(raw_messages, zone)
    if not summaries:
        return None
    summary = latest_summary(summaries)

    # Preserve the first widget prototype's field names while adding the
    # explicit categories required by the production ingestion layer.
    summary["reportDate"] = summary.get("date")
    summary["verifiedCount"] = summary.get("totalUniqueRoles", 0)
    summary["sourceURL"] = None
    for raw in raw_messages:
        analysis = analyze_message(raw, zone)
        if analysis and analysis.get("date") == summary.get("date"):
            summary["sourceURL"] = _safe_url(raw.get("url"))
            if summary["sourceURL"]:
                break

    # Title-only classification is not enough to call a small-company role
    # ready.  Use only the strict queue projection produced after eligibility
    # and application-limit review.  Missing/invalid projections fail closed.
    strict = preparation_summary if isinstance(preparation_summary, dict) else {}
    strict_valid = (
        strict.get("count_verified") is True
        and str(strict.get("report_date") or "") == str(summary.get("date") or "")
        and type(strict.get("verified_role_count")) is int
        and strict.get("verified_role_count") == summary.get("totalUniqueRoles")
    )
    if strict_valid:
        manual_decision = max(0, int(strict.get("manual_decision_count") or 0))
        requirements_review = max(0, int(strict.get("manual_review_count") or 0))
        prepared = max(0, int(strict.get("prepared_count") or 0))
        excluded = max(0, int(strict.get("excluded_count") or 0))
        strict_valid = (
            manual_decision + requirements_review + prepared + excluded
            == summary.get("totalUniqueRoles")
        )
    if strict_valid:
        # Historical field name retained for decoding compatibility.  Its
        # product meaning is now exactly manual_decision_count.
        summary["manualReviewCount"] = manual_decision
        summary["requirementsReviewCount"] = requirements_review
        summary["eligibleSmallCompanyPreparationCount"] = prepared
        summary["skippedIneligibleCount"] = excluded
        summary["strictPreparationVerified"] = True
        summary["preparationFreshness"] = str(strict.get("freshness") or "")
        summary["sourceReused"] = bool(strict.get("source_reused"))
        summary["sourceURL"] = _safe_url(strict.get("source_url")) or summary["sourceURL"]
    else:
        # Without a strict projection no title-only category is actionable.
        # Keep the verified total, but expose zero manual decisions and zero
        # prepared roles; optionally surface the whole set as requirements work.
        summary["manualReviewCount"] = 0
        summary["requirementsReviewCount"] = int(summary.get("totalUniqueRoles") or 0)
        summary["eligibleSmallCompanyPreparationCount"] = 0
        summary["skippedIneligibleCount"] = 0
        summary["strictPreparationVerified"] = False
    return summary


def _source_statuses(raw_sources: Any, zone: ZoneInfo) -> list[dict[str, Any]]:
    if not isinstance(raw_sources, dict):
        return []
    output: list[dict[str, Any]] = []
    for source in ("canvas", "gmail", "outlook", "linkedin"):
        value = raw_sources.get(source) if isinstance(raw_sources.get(source), dict) else {}
        state = str(value.get("state") or "unknown").casefold()
        if state not in {"ok", "auth_required", "error", "partial", "unknown"}:
            state = "error"
        raw_error = str(value.get("error") or "").casefold()
        if state == "auth_required" or "auth" in raw_error or "session" in raw_error:
            error_code = "auth_required"
        elif "count_mismatch" in raw_error:
            error_code = "count_mismatch"
        elif state in {"error", "partial"}:
            error_code = "source_unavailable"
        else:
            error_code = None
        output.append(
            {
                "id": source,
                "state": state,
                "checkedAt": _iso_datetime(value.get("checked_at"), zone),
                "errorCode": error_code,
            }
        )
    return output


def build_snapshot(
    data: dict[str, Any],
    now: datetime | None = None,
    *,
    max_internships: int = 24,
) -> dict[str, Any]:
    timezone_name = str(data.get("timezone") or "America/New_York")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = "America/New_York"
        zone = DEFAULT_ZONE
    now = (now or datetime.now(zone)).astimezone(zone)

    assignments = _assignment_items(
        merge_assignments_and_exams(data.get("assignments"), data.get("exams")),
        now,
        zone,
    )
    messages = _message_items(data.get("messages"), zone)
    internships = _verified_internships(
        data.get("messages"),
        max_internships,
        zone,
        data.get("intern_preparation_summary"),
    )
    swe_daily, _ = collect_verified_roles(data.get("messages") or [], zone)
    swe_intern = _swe_intern_summary(
        data.get("messages"),
        data.get("generated_at"),
        zone,
        data.get("intern_preparation_summary"),
    )
    items = assignments + messages + internships
    stats = {
        "assignments": sum(item["kind"] == "assignment" for item in items),
        "exams": sum(item["kind"] == "exam" for item in items),
        "warnings": 0,
        "messages": len(messages),
        "p0Messages": sum(item.get("priority") == "P0" and item["kind"] == "message" for item in items),
        "p1Messages": sum(item.get("priority") == "P1" and item["kind"] == "message" for item in items),
        # ``internships`` is the verified daily total, not the display-card cap.
        # Keep a separate field so older diagnostics can still see card volume.
        "internships": swe_intern.get("totalUniqueRoles", 0) if swe_intern else 0,
        "displayedInternships": len(internships),
        "sweInternManualReview": swe_intern.get("manualReviewCount", 0) if swe_intern else 0,
        "sweInternRequirementsReview": (
            swe_intern.get("requirementsReviewCount", 0) if swe_intern else 0
        ),
        "sweInternEligiblePreparation": (
            swe_intern.get("eligibleSmallCompanyPreparationCount", 0) if swe_intern else 0
        ),
        "sweInternSkippedIneligible": (
            swe_intern.get("skippedIneligibleCount", 0) if swe_intern else 0
        ),
        "clickableItems": sum(bool(item.get("url")) for item in items),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _iso_datetime(data.get("generated_at"), zone),
        "exportedAt": now.isoformat(),
        "timezone": timezone_name,
        "thresholds": {
            "redHours": RED_HOURS,
            "yellowHours": YELLOW_HOURS,
            "windowHours": WINDOW_HOURS,
        },
        "sources": _source_statuses(data.get("sources"), zone),
        "stats": stats,
        "sweIntern": swe_intern,
        "sweInternDaily": swe_daily,
        "items": items,
    }


def _content_digest(snapshot: dict[str, Any]) -> str:
    logical = dict(snapshot)
    logical.pop("exportedAt", None)
    canonical = json.dumps(logical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        current = _read_object(path)
        return str(current.get("contentDigest") or "") or _content_digest(current)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _safe_reload_url(value: str, widget_kind: str, digest: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme.casefold() != DEFAULT_RELOAD_SCHEME
        or (parsed.hostname or "").casefold() != DEFAULT_RELOAD_HOST
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"reload URL must be exactly {DEFAULT_RELOAD_SCHEME}://{DEFAULT_RELOAD_HOST}"
        )
    query = urlencode((("kind", widget_kind), ("digest", digest)))
    return urlunparse((DEFAULT_RELOAD_SCHEME, DEFAULT_RELOAD_HOST, "", "", query, ""))


def _request_reload(
    group_dir: Path,
    widget_kind: str,
    digest: str,
    requested_at: datetime,
    *,
    reload_helper: Path | None,
    reload_url: str | None,
    run_command: Any = subprocess.run,
) -> dict[str, Any]:
    request_path = group_dir / "widget_reload_request.json"
    request = {
        "schemaVersion": 1,
        "requestedAt": requested_at.isoformat(),
        "widgetKind": widget_kind,
        "contentDigest": digest,
        "action": "WidgetCenter.reloadTimelines",
    }
    _atomic_json(request_path, request)

    method = "request_file"
    status = "deferred"
    error_code = None
    try:
        if reload_helper is not None:
            helper = reload_helper.expanduser().resolve()
            if not helper.is_file() or not os.access(helper, os.X_OK):
                raise FileNotFoundError("reload helper is not executable")
            run_command(
                [str(helper), "--kind", widget_kind, "--digest", digest],
                check=True,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            method = "signed_helper"
            status = "succeeded"
        elif reload_url:
            safe_url = _safe_reload_url(reload_url, widget_kind, digest)
            run_command(
                ["/usr/bin/open", "-g", safe_url],
                check=True,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            method = "host_app_url"
            status = "requested"
    except (OSError, ValueError, subprocess.SubprocessError):
        status = "failed"
        error_code = "widget_reload_handoff_failed"

    return {
        "requested": True,
        "status": status,
        "method": method,
        "errorCode": error_code,
        "requestPath": str(request_path),
    }


def export(
    input_path: Path,
    group_dir: Path,
    status_path: Path,
    *,
    now: datetime | None = None,
    max_internships: int = 24,
    widget_kind: str = DEFAULT_WIDGET_KIND,
    reload_helper: Path | None = None,
    reload_url: str | None = None,
    force_reload: bool = False,
    widget_extension_support_dir: Path | None = DEFAULT_WIDGET_EXTENSION_SUPPORT_DIR,
    host_app_support_dir: Path | None = DEFAULT_HOST_APP_SUPPORT_DIR,
    run_command: Any = subprocess.run,
) -> dict[str, Any]:
    data = _read_object(input_path)
    timezone_name = str(data.get("timezone") or "America/New_York")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = DEFAULT_ZONE
    current_time = (now or datetime.now(zone)).astimezone(zone)
    snapshot = build_snapshot(data, current_time, max_internships=max_internships)
    digest = _content_digest(snapshot)
    snapshot["contentDigest"] = digest

    snapshot_path = group_dir / DEFAULT_SNAPSHOT_NAME
    snapshot_targets = [("app_group", snapshot_path)]
    if widget_extension_support_dir is not None:
        fallback_path = widget_extension_support_dir / DEFAULT_SNAPSHOT_NAME
        if fallback_path.expanduser().absolute() != snapshot_path.expanduser().absolute():
            snapshot_targets.append(("widget_extension_container", fallback_path))
    if host_app_support_dir is not None:
        host_path = host_app_support_dir / DEFAULT_SNAPSHOT_NAME
        existing_paths = {path.expanduser().absolute() for _, path in snapshot_targets}
        if host_path.expanduser().absolute() not in existing_paths:
            snapshot_targets.append(("host_app_container", host_path))

    snapshot_writes: list[dict[str, Any]] = []
    for role, target in snapshot_targets:
        target_updated = _existing_digest(target) != digest
        try:
            if target_updated:
                _atomic_json(target, snapshot)
            snapshot_writes.append(
                {
                    "role": role,
                    "path": str(target),
                    "state": "ok",
                    "updated": target_updated,
                    "errorCode": None,
                }
            )
        except OSError:
            snapshot_writes.append(
                {
                    "role": role,
                    "path": str(target),
                    "state": "failed",
                    "updated": False,
                    "errorCode": "snapshot_write_failed",
                }
            )

    successful_writes = [item for item in snapshot_writes if item["state"] == "ok"]
    if not successful_writes:
        raise OSError("all widget snapshot writes failed")
    updated = any(item["updated"] for item in successful_writes)

    should_reload = updated or force_reload
    if should_reload:
        reload_result = _request_reload(
            group_dir,
            widget_kind,
            digest,
            current_time,
            reload_helper=reload_helper,
            reload_url=reload_url,
            run_command=run_command,
        )
    else:
        reload_result = {
            "requested": False,
            "status": "not_needed",
            "method": None,
            "errorCode": None,
            "requestPath": str(group_dir / "widget_reload_request.json"),
        }

    state = (
        "ok"
        if reload_result["status"] != "failed"
        and all(item["state"] == "ok" for item in snapshot_writes)
        else "degraded"
    )
    result = {
        "state": state,
        "exportedAt": current_time.isoformat(),
        "input": str(input_path),
        "snapshot": str(snapshot_path),
        "snapshotWrites": snapshot_writes,
        "updated": updated,
        "contentDigest": digest,
        "stats": snapshot["stats"],
        "widgetReload": reload_result,
    }
    _atomic_json(status_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--app-group-dir", type=Path, default=DEFAULT_GROUP_DIR)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-internships", type=int, default=24)
    parser.add_argument("--widget-kind", default=DEFAULT_WIDGET_KIND)
    parser.add_argument("--reload-helper", type=Path)
    parser.add_argument("--reload-url")
    parser.add_argument("--force-reload", action="store_true")
    parser.add_argument(
        "--widget-extension-support-dir",
        type=Path,
        default=DEFAULT_WIDGET_EXTENSION_SUPPORT_DIR,
        help="local ad-hoc Widget container mirror; App Group remains primary",
    )
    parser.add_argument(
        "--host-app-support-dir",
        type=Path,
        default=DEFAULT_HOST_APP_SUPPORT_DIR,
        help="local ad-hoc host app container mirror",
    )
    args = parser.parse_args()
    if args.max_internships < 0 or args.max_internships > 200:
        parser.error("--max-internships must be between 0 and 200")
    if not str(args.widget_kind).strip():
        parser.error("--widget-kind cannot be empty")

    try:
        result = export(
            args.input.expanduser().resolve(),
            args.app_group_dir.expanduser(),
            args.status.expanduser(),
            max_internships=args.max_internships,
            widget_kind=str(args.widget_kind).strip(),
            reload_helper=args.reload_helper,
            reload_url=args.reload_url,
            force_reload=args.force_reload,
            widget_extension_support_dir=args.widget_extension_support_dir.expanduser(),
            host_app_support_dir=args.host_app_support_dir.expanduser(),
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"state": "failed", "error": type(error).__name__}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
