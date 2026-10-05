#!/usr/bin/env python3
"""Run the daily briefing pipeline and optionally prepare SWE List links.

Preparation can identify candidates and, only with an explicit interactive flag,
open their application pages.  Opening a page is never treated as an application
submission.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import importlib.util
import json
import os
import pickle
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from swe_list_summary import (
    TARGET_SENDER,
    collect_verified_roles,
    expected_count as expected_swe_count,
    parse_entries as parse_swe_entries,
    sender_is_verified,
)
from intern_preparation import (
    build_preparation_queue,
    build_preparation_summary,
    stable_job_fingerprint,
)
from assignment_filters import (
    assignment_expiry_at,
    is_hidden_assignment,
    is_exam_assignment,
    visible_assignments,
)
from refresh_plan import build_refresh_plan, attach_exam_inventory
from plugin_sources import load_resource_plugins


def _zone() -> ZoneInfo:
    return ZoneInfo("America/New_York")


GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
GMAIL_CREDENTIALS_PATH = Path(os.environ.get("DESKTOP_ASSISTANT_GMAIL_CREDENTIALS", str(Path.home() / ".config/desktop-assistant/gmail_credentials.json")))
# Keep read-only collection credentials separate from any compose-capable token.
# This prevents the daily briefing from inheriting unnecessary write authority.
GMAIL_TOKEN_PATH = Path(os.environ.get("DESKTOP_ASSISTANT_GMAIL_TOKEN", str(Path.home() / ".config/desktop-assistant/token_gmail_readonly.pickle")))
GMAIL_FROM_FILTER = 'from:noreply@swelist.com subject:"New Internships Posted Today"'
GMAIL_MAX_RESULTS = 100


def _normalize_oauth_scopes(raw_scopes: Any) -> set[str]:
    """Return a comparable scope set without accepting malformed values."""

    if isinstance(raw_scopes, str):
        values = raw_scopes.split()
    elif isinstance(raw_scopes, (list, tuple, set, frozenset)):
        values = raw_scopes
    else:
        return set()
    return {_safe_text(value) for value in values if _safe_text(value)}


def _credentials_are_strictly_read_only(credentials: Credentials) -> bool:
    """Reject credentials carrying any authority beyond Gmail read-only.

    A separate token filename prevents accidental token reuse, but it is not a
    sufficient authority boundary on its own: an OAuth client can return a
    credential containing previously granted scopes. Check both the requested
    and (when Google reports them) granted scopes before using or persisting it.
    """

    allowed = set(GMAIL_SCOPES)
    requested = _normalize_oauth_scopes(credentials.scopes)
    if requested != allowed:
        return False
    granted_raw = getattr(credentials, "granted_scopes", None)
    if granted_raw is not None and _normalize_oauth_scopes(granted_raw) != allowed:
        return False
    return True


def _resolve_path(value: str | None, fallback: Path | None = None) -> Path:
    if value:
        return Path(value).expanduser()
    if fallback is None:
        raise ValueError("Need either explicit path or fallback path")
    return fallback.expanduser()


def _safe_text(value: Any, default: str = "") -> str:
    return str(value).strip() if value is not None else default


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_atomically(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
    temp_path.replace(path)


def _normalize_datetime(value: Any, default: datetime | None = None) -> datetime:
    if default is None:
        default = datetime.now(_zone())
    if value is None:
        return default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_zone())
        return value.astimezone(_zone())
    text = str(value).strip()
    if not text:
        return default
    for candidate in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")), parsedate_to_datetime):
        try:
            parsed = candidate(text)  # type: ignore[misc]
        except Exception:
            continue
        else:
            if not parsed.tzinfo:
                parsed = parsed.replace(tzinfo=_zone())
            return parsed.astimezone(_zone())
    return default


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        data = {
            "last_successful_run": None,
            "timezone": str(_zone()),
            "tracked_message_fingerprints": [],
            "assessment_events": [],
            "intern_auto_apply": {
                "schema_version": 2,
                "mode": "prepare_only",
                "attempted": [],
                "prepared": [],
                "opened_unverified": [],
                "legacy_opened_unverified": [],
                "submitted": [],
            },
        }
        _migrate_intern_application_history(data)
        return data

    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}

    data.setdefault("last_successful_run", None)
    data.setdefault("timezone", str(_zone()))
    data.setdefault("tracked_message_fingerprints", [])
    data.setdefault("assessment_events", [])
    _migrate_intern_application_history(data)
    return data


def _load_renderer_module(path: Path):
    spec = importlib.util.spec_from_file_location("daily_briefing_renderer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load render script module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fingerprint(job: dict[str, Any]) -> str:
    return stable_job_fingerprint(job)


def _message_fingerprint(message: dict[str, Any]) -> str:
    raw = "|".join([
        _safe_text(message.get("source"), "gmail"),
        _safe_text(message.get("message_id")),
        _safe_text(message.get("subject")),
        _safe_text(message.get("sender")),
        _safe_text(message.get("received_at")),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _assessment_fingerprint(event: dict[str, Any]) -> str:
    """Stable exam identity that survives a rescheduled date."""

    explicit_id = _safe_text(
        event.get("id")
        or event.get("event_id")
        or event.get("canvas_event_id")
    )
    # A new announcement can move an exam to a different URL/date. Neither is
    # its identity. Canonicalize the course label across collector formats.
    course = re.match(r"([A-Z]+)\s*(\d+)", _safe_text(event.get("course")).upper())
    course_key = "".join(course.groups()) if course else _safe_text(event.get("course")).casefold()
    raw = "|".join(
        (
            explicit_id,
            course_key,
            re.sub(r"\s+", " ", _safe_text(event.get("title")).casefold()),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _merge_assessment_events(
    payload: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Persist future exams independently from short-lived inbox messages.

    Current data wins changed fields, while a previously collected exam remains
    recoverable until its explicit end.  Date-only exams expire at the end of
    their calendar day; no clock time is invented.
    """

    raw_prior = state.get("assessment_events")
    prior_events = raw_prior if isinstance(raw_prior, list) else []
    raw_exams = payload.get("exams")
    incoming = list(raw_exams) if isinstance(raw_exams, list) else []

    # During schema migration, retain legacy assignment-embedded exams too.
    raw_assignments = payload.get("assignments")
    if isinstance(raw_assignments, list):
        incoming.extend(
            item for item in raw_assignments
            if isinstance(item, dict) and is_exam_assignment(item)
        )

    merged: dict[str, dict[str, Any]] = {}
    for raw in prior_events:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event["is_exam"] = True
        fingerprint = _assessment_fingerprint(event)
        expiry = assignment_expiry_at(event)
        if expiry is None or expiry <= now or is_hidden_assignment(event, now):
            continue
        event["fingerprint"] = fingerprint
        merged[fingerprint] = event

    cancelled: set[str] = set()
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event["is_exam"] = True
        if not event.get("exam_at"):
            event["exam_at"] = event.get("exam_date") or event.get("due_at")
        if not event.get("source_url") and event.get("url"):
            event["source_url"] = event.get("url")
        fingerprint = _assessment_fingerprint(event)
        status = re.sub(
            r"[^a-z0-9]+",
            "_",
            _safe_text(event.get("status")).casefold(),
        ).strip("_")
        if status in {"cancelled", "canceled", "completed", "complete"}:
            cancelled.add(fingerprint)
            merged.pop(fingerprint, None)
            continue
        expiry = assignment_expiry_at(event)
        if expiry is None or expiry <= now or is_hidden_assignment(event, now):
            merged.pop(fingerprint, None)
            continue
        previous = merged.get(fingerprint, {})
        if previous and event.get("exam_at") != (previous.get("exam_at") or previous.get("exam_date")) and not event.get("exam_end_at"):
            previous = {key: value for key, value in previous.items() if key != "exam_end_at"}
        event["fingerprint"] = fingerprint
        event["first_seen_at"] = previous.get("first_seen_at") or now.isoformat()
        event["last_seen_at"] = now.isoformat()
        merged[fingerprint] = {**previous, **event}

    for fingerprint in cancelled:
        merged.pop(fingerprint, None)
    events = sorted(
        merged.values(),
        key=lambda event: _safe_text(event.get("exam_at") or event.get("due_at")),
    )
    state["assessment_events"] = events
    payload["exams"] = [
        {
            key: value
            for key, value in event.items()
            if key not in {"first_seen_at", "last_seen_at"}
        }
        for event in events
    ]
    return payload["exams"]


def _trim_records(records: list[dict[str, Any]], cap: int = 300) -> list[dict[str, Any]]:
    if len(records) <= cap:
        return records
    return records[-cap:]


def record_presentation_revision(root: Path, payload: dict, renderer: dict, metrics: dict) -> None:
    """Keep the scheduler receipt aligned after a local data correction.

    Do not turn a local render into a successful live source read, finish an
    agent-owned lease, or rewrite the original run duration.
    """
    wallpaper = renderer.get("wallpaper") or {}
    if not all(wallpaper.get(key) is True for key in ("current_desktop_verified", "all_spaces_verified", "lock_screen_source_verified", "presentation_verified")):
        return
    status_path = root / "daily_run_status.json"
    if not status_path.exists():
        return
    status = _read_json(status_path)
    keys = ("status", "target", "image_sha256", "current_desktop_verified", "all_spaces_verified", "lock_screen_source_verified", "presentation_verified", "presentation_verified_at")
    status["wallpaper"] = {key: wallpaper.get(key) for key in keys}
    status["report_generated_at"] = payload.get("generated_at")
    status["sources"] = payload.get("sources", {})
    status["last_local_revision"] = {"at": wallpaper.get("checked_at"), "metrics": metrics, "live_mail_collected": False, "exam_count": len(payload.get("exams", []))}
    _write_json_atomically(status_path, status)


def _record_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _ensure_record_fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    if not _safe_text(normalized.get("fingerprint")):
        normalized["fingerprint"] = _fingerprint(normalized)
    return normalized


def _has_explicit_success_confirmation(record: dict[str, Any]) -> bool:
    """Return true only when a record contains explicit submission evidence."""

    status = _safe_text(record.get("status")).lower().replace("-", "_").replace(" ", "_")
    success_status = status in {
        "submitted",
        "application_submitted",
        "confirmed_submitted",
        "submission_confirmed",
    }
    raw_flag = record.get("success_confirmation")
    if isinstance(raw_flag, bool):
        explicit_flag = raw_flag
    else:
        flag_text = _safe_text(raw_flag).lower()
        explicit_flag = flag_text in {
            "true",
            "yes",
            "1",
            "confirmed",
            "success",
            "submitted",
            "application_submitted",
            "submission_confirmed",
        }
    textual_evidence = bool(
        _safe_text(record.get("confirmation"))
        or _safe_text(record.get("confirmation_evidence"))
    )
    return explicit_flag or (success_status and textual_evidence)


def _migrate_intern_application_history(state: dict[str, Any]) -> dict[str, Any]:
    """Migrate ambiguous legacy ``applied`` records to an evidence-safe schema.

    The old implementation put every successfully opened URL in ``applied``.
    Those records cannot prove that a form was submitted.  They are therefore
    preserved as ``legacy_opened_unverified`` unless the record itself contains
    an explicit submitted status *and* success evidence.
    """

    raw_history = state.get("intern_auto_apply")
    history = raw_history if isinstance(raw_history, dict) else {}
    state["intern_auto_apply"] = history

    prepared = [_ensure_record_fingerprint(item) for item in _record_list(history.get("prepared"))]
    opened_unverified = [
        _ensure_record_fingerprint(item) for item in _record_list(history.get("opened_unverified"))
    ]
    legacy_opened_unverified = [
        _ensure_record_fingerprint(item)
        for item in _record_list(history.get("legacy_opened_unverified"))
    ]
    attempted = _record_list(history.get("attempted"))

    submitted: list[dict[str, Any]] = []
    for raw_record in _record_list(history.get("submitted")):
        normalized = _ensure_record_fingerprint(raw_record)
        if _has_explicit_success_confirmation(normalized):
            normalized["status"] = "submitted"
            _append_unique(submitted, normalized)
        else:
            normalized["status"] = "legacy_opened_unverified"
            normalized["migration_source"] = "submitted_without_success_confirmation"
            _append_unique(legacy_opened_unverified, normalized)

    for legacy_record in _record_list(history.pop("applied", [])):
        migrated = _ensure_record_fingerprint(legacy_record)
        if _has_explicit_success_confirmation(migrated):
            migrated["status"] = "submitted"
            migrated["migration_source"] = "legacy_applied_with_success_confirmation"
            _append_unique(submitted, migrated)
        else:
            migrated["status"] = "legacy_opened_unverified"
            migrated["migration_source"] = "legacy_applied_without_submission_evidence"
            _append_unique(legacy_opened_unverified, migrated)

    confirmed_fingerprints = {
        _safe_text(item.get("fingerprint"))
        for item in submitted
        if _has_explicit_success_confirmation(item)
    }
    normalized_attempted: list[dict[str, Any]] = []
    for record in attempted:
        normalized = _ensure_record_fingerprint(record)
        status = _safe_text(normalized.get("status")).lower()
        if status == "opened":
            normalized["status"] = "opened_unverified"
        elif status == "already_applied":
            normalized["status"] = (
                "already_confirmed_submitted"
                if _safe_text(normalized.get("fingerprint")) in confirmed_fingerprints
                else "legacy_opened_unverified"
            )
        normalized_attempted.append(normalized)

    history["schema_version"] = 3
    history["mode"] = "prepare_only"
    history["submitted"] = _trim_records(submitted)
    history["prepared"] = _trim_records(prepared)
    history["opened_unverified"] = _trim_records(opened_unverified)
    history["legacy_opened_unverified"] = _trim_records(legacy_opened_unverified)
    history["attempted"] = _trim_records(normalized_attempted)
    return history


def _open_urls_in_chrome(urls: list[str]) -> tuple[list[str], list[str]]:
    opened: list[str] = []
    failed: list[str] = []
    for url in urls:
        try:
            subprocess.run(["open", "-a", "Google Chrome", url], check=True, capture_output=True, text=True)
            opened.append(url)
        except subprocess.CalledProcessError:
            failed.append(url)
    return opened, failed


def _append_unique(records: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if not any(entry.get("fingerprint") == item.get("fingerprint") and entry.get("apply_url") == item.get("apply_url") for entry in records):
        records.append(item)


def _run_renderer(renderer_path: Path, input_path: Path, png_path: Path, markdown_path: Path, set_wallpaper: bool) -> dict[str, Any]:
    # The graphics runtime includes AppKit bindings used by wallpaper_manager.
    # Collection/preparation can use system Python; rendering must keep the
    # already configured runtime instead of inheriting whichever CLI invoked us.
    graphics_python = Path(os.environ.get("DESKTOP_ASSISTANT_GRAPHICS_PYTHON", sys.executable))
    cmd = [
        str(graphics_python) if graphics_python.is_file() else sys.executable,
        str(renderer_path),
        "--input",
        str(input_path),
        "--png",
        str(png_path),
        "--markdown",
        str(markdown_path),
    ]
    if set_wallpaper:
        cmd.append("--set-wallpaper")

    result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"Renderer failed ({result.returncode}): {result.stderr[-3000:]}")
    return json.loads(result.stdout)


def _load_daily_data(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object")
    return data


def _decode_body_data(payload_part: dict[str, Any]) -> str:
    body = payload_part.get("body") or {}
    data = body.get("data")
    if not data:
        return ""
    try:
        safe_data = str(data).replace("-", "+").replace("_", "/")
        padding = "=" * ((4 - len(safe_data) % 4) % 4)
        return base64.urlsafe_b64decode(f"{safe_data}{padding}").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collect_body_text(parts: list[dict[str, Any]]) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in parts:
        mime = str(part.get("mimeType", "")).lower()
        if "parts" in part and isinstance(part.get("parts"), list):
            nested = part.get("parts") or []
            nested_plain, nested_html = _collect_body_text(nested)
            if nested_plain:
                plain_parts.append(nested_plain)
            if nested_html:
                html_parts.append(nested_html)
            continue

        if mime.startswith("text/plain"):
            decoded = _decode_body_data(part)
            if decoded:
                plain_parts.append(decoded)
            continue
        if mime.startswith("text/html"):
            decoded = _decode_body_data(part)
            if decoded:
                html_parts.append(_html_to_markdown(decoded))

    return "\n".join(plain_parts).strip(), "\n".join(html_parts).strip()


def _html_to_markdown(html_text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html_text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", "", text)
    text = re.sub(
        r"(?is)<a\s+[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        lambda match: f"[{html.unescape(re.sub(r'<[^>]+>', '', match.group(2)).strip() or 'Open role')}](%s)" % match.group(1),
        text,
    )
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\/p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return {}
    parsed: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, dict):
            continue
        key = _safe_text(header.get("name")).lower()
        value = _safe_text(header.get("value"))
        if key and value:
            parsed[key] = value
    return parsed


def _normalize_email_links(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if "[" in stripped and "](" in stripped and "http" in stripped:
            lines.append(stripped)
            continue

        if "http" in stripped:
            converted = stripped
            bare_match = re.search(r"(?i)(https?://[^\s]+)", stripped)
            if bare_match:
                url = bare_match.group(1).strip(").,;>")
                before = stripped[:bare_match.start(1)].strip(" \t-*•")
                after = stripped[bare_match.end(1):].strip()
                if before:
                    label = " ".join((before + (" " + after if after else "")).split())
                    converted = re.sub(re.escape(url), f"[{label}]({url})", stripped)
                else:
                    converted = stripped.replace(url, f"[Internship role]({url})")
            lines.append(converted)
        else:
            lines.append(stripped)
    return "\n".join(lines)


def _normalize_message_payload(text: str) -> str:
    if "<" in text:
        text = _html_to_markdown(text)
    return _normalize_email_links(text)


def _message_to_item(gmail_message: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    payload = gmail_message.get("payload")
    if not isinstance(payload, dict):
        return None

    headers = _extract_headers(payload)
    sender = _safe_text(headers.get("from"))
    subject = _safe_text(headers.get("subject"))
    _, sender_address = parseaddr(sender)
    sender_address = sender_address.strip().casefold()
    if sender_address != TARGET_SENDER or expected_swe_count(subject) is None:
        return None
    # A missing or malformed Date header must not make an old listing appear
    # to have arrived during this run. Gmail's internalDate is the fallback.
    raw_date = _safe_text(headers.get("date"))
    try:
        dated = parsedate_to_datetime(raw_date) if raw_date else None
        if dated is None or dated.tzinfo is None:
            raise ValueError("missing timezone in Date header")
        received = dated.astimezone(_zone()).isoformat()
    except (TypeError, ValueError):
        try:
            received = datetime.fromtimestamp(
                int(gmail_message["internalDate"]) / 1000, tz=_zone()
            ).isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    if "parts" in payload and isinstance(payload.get("parts"), list):
        plain_text, html_text = _collect_body_text(payload.get("parts") or [])
        # SWE List's HTML part carries the per-role hrefs. Some plain-text
        # alternatives contain the names but omit those links entirely.
        source_text = max(
            (plain_text, html_text), key=lambda text: len(parse_swe_entries(text))
        )
    else:
        source_text = _decode_body_data(payload)

    if not source_text:
        source_text = _safe_text(gmail_message.get("snippet"))

    source_text = _normalize_message_payload(source_text)
    message_id = _safe_text(gmail_message.get("id"))
    thread_id = _safe_text(gmail_message.get("threadId"), message_id)
    item = {
        "source": "gmail",
        "sender": sender,
        "sender_address": sender_address,
        "sender_display": "SWE List",
        "subject": subject,
        "summary": "SWE List daily update; pending local count verification.",
        "body": source_text[:200000],
        "snippet": "",
        "received_at": received,
        "priority": "P1",
        "importance_reason": "internship list",
        "url": (
            f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
            if thread_id
            else "https://mail.google.com/mail/u/0/#inbox"
        ),
    }
    item["message_id"] = message_id
    item["fingerprint"] = _message_fingerprint(item)
    return item


def _load_gmail_credentials(
    interactive: bool = False,
    auth_code: str | None = None,
) -> tuple[Credentials | None, dict[str, Any]]:
    credentials_path = GMAIL_CREDENTIALS_PATH.expanduser()
    token_path = GMAIL_TOKEN_PATH.expanduser()
    status: dict[str, Any] = {"state": "ok"}

    credentials: Credentials | None = None
    if token_path.exists():
        try:
            with token_path.open("rb") as handle:
                loaded = pickle.load(handle)
            if isinstance(loaded, Credentials):
                credentials = loaded
        except Exception as error:
            status = {"state": "auth_required", "error": f"token_read_error:{error}"}

    if credentials is not None:
        if not _credentials_are_strictly_read_only(credentials):
            status = {"state": "auth_required", "error": "scope_mismatch"}
            credentials = None
        else:
            try:
                if credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                if not _credentials_are_strictly_read_only(credentials):
                    status = {"state": "auth_required", "error": "scope_mismatch"}
                    credentials = None
            except Exception as error:
                status = {"state": "auth_required", "error": str(error)}
                credentials = None

    if credentials is None:
        if not credentials_path.exists():
            status = {"state": "auth_required", "error": f"missing_credentials:{credentials_path}"}
            return None, status
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GMAIL_SCOPES)
            if auth_code:
                flow.fetch_token(code=auth_code)
                credentials = flow.credentials
                if not _credentials_are_strictly_read_only(credentials):
                    return None, {"state": "auth_required", "error": "scope_mismatch"}
                status = {"state": "ok"}
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with token_path.open("wb") as handle:
                    pickle.dump(credentials, handle)
                return credentials, status
            if interactive:
                credentials = flow.run_local_server(port=0)
                if not _credentials_are_strictly_read_only(credentials):
                    return None, {"state": "auth_required", "error": "scope_mismatch"}
                status = {"state": "ok"}
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with token_path.open("wb") as handle:
                    pickle.dump(credentials, handle)
            else:
                auth_url, _ = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                )
                status = {
                    "state": "auth_required",
                    "error": "oauth_scope_upgrade_required",
                    "auth_url": auth_url,
                }
                return None, status
        except Exception as error:
            status = {"state": "auth_required", "error": str(error)}
            return None, status

    if credentials and token_path.exists():
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with token_path.open("wb") as handle:
                pickle.dump(credentials, handle)
        except Exception:
            pass

    return credentials, status


def _collect_swe_gmail_messages(
    state: dict[str, Any],
    now: datetime,
    interactive_gmail: bool,
    gmail_auth_code: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    # A render is not a successful mailbox read. Do not advance past missed mail
    # when another source succeeded or when yesterday's report was re-rendered.
    since = _normalize_datetime(state.get("swe_last_successful_at"), default=now - timedelta(hours=36))
    since = min(since, now) - timedelta(minutes=5)
    query = f"{GMAIL_FROM_FILTER} after:{int(since.timestamp())}"
    source_state = {"state": "ok", "checked_at": now.isoformat(), "since": since.isoformat()}

    credentials, credentials_status = _load_gmail_credentials(
        interactive=interactive_gmail,
        auth_code=gmail_auth_code,
    )
    if credentials_status.get("state") != "ok" or credentials is None:
        source_state.update({
            "state": "auth_required",
            "error": _safe_text(credentials_status.get("error"), "auth_required"),
        })
        auth_url = _safe_text(credentials_status.get("auth_url"))
        if auth_url:
            source_state["auth_url"] = auth_url
        return [], source_state, False

    since_ms = int(since.timestamp() * 1000)
    try:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        response = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=GMAIL_MAX_RESULTS,
            labelIds=["INBOX"],
        ).execute()
    except Exception as error:
        source_state["state"] = "error"
        source_state["error"] = str(error)
        return [], source_state, False

    seen_ids = set(state.get("tracked_message_fingerprints", []))
    messages: list[dict[str, Any]] = []
    fetched = 0
    matched = 0
    detail_fetch_failures = 0

    while True:
        batch = response.get("messages", []) or []
        for summary in batch:
            msg_id = _safe_text(summary.get("id"))
            if not msg_id:
                continue
            try:
                detail = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            except Exception:
                detail_fetch_failures += 1
                continue
            if not isinstance(detail, dict):
                detail_fetch_failures += 1
                continue
            try:
                if int(_safe_text(detail.get("internalDate"), "0") or "0") < since_ms:
                    continue
            except ValueError:
                pass

            item = _message_to_item(detail, now)
            if item is None:
                continue
            if "swelist.com" not in _safe_text(item.get("sender")).lower():
                continue

            fetched += 1
            fp = _safe_text(item.get("fingerprint"))
            if fp not in seen_ids:
                matched += 1
                seen_ids.add(fp)
            else:
                if detail.get("labelIds") and "UNREAD" in detail.get("labelIds"):
                    matched += 1
            messages.append(item)

        next_token = response.get("nextPageToken")
        if not next_token:
            break
        try:
            response = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=GMAIL_MAX_RESULTS,
                labelIds=["INBOX"],
                pageToken=next_token,
            ).execute()
        except Exception as error:
            source_state["state"] = "error"
            source_state["error"] = f"gmail_list_pagination_failed:{error}"
            return [], source_state, False

    # A partial inbox read must not masquerade as a complete SWE List update.
    # Returning no new messages makes the caller preserve the prior verified
    # section while exposing the source error for remediation.
    if detail_fetch_failures:
        source_state["state"] = "error"
        source_state["error"] = "gmail_message_fetch_failed"
        source_state["failed_message_count"] = detail_fetch_failures
        return [], source_state, False

    state["tracked_message_fingerprints"] = list(seen_ids)[-500:]
    source_state["gmail_messages_found"] = fetched
    source_state["new_messages_added"] = matched

    # Keep only messages in the last 2 weeks if old state has too much noise.
    cutoff = now - timedelta(days=14)
    messages = [item for item in messages if _normalize_datetime(item.get("received_at"), default=now) >= cutoff]
    messages.sort(key=lambda item: _safe_text(item.get("received_at")), reverse=True)

    return messages, source_state, matched > 0


def _prepare_sources(payload: dict[str, Any], source_updates: dict[str, dict[str, Any]]) -> None:
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        sources = {}

    for key in ("canvas", "gmail", "outlook", "linkedin"):
        current = sources.get(key)
        if not isinstance(current, dict):
            current = {}
        next_state = source_updates.get(key, current)
        if key not in current and key not in source_updates:
            next_state.setdefault("state", "unknown")
        sources[key] = next_state

    payload["sources"] = sources


def _validate_swe_message_counts(messages: list[dict[str, Any]], renderer_module: Any) -> dict[str, Any]:
    checked = 0
    expected_total = 0
    parsed_total = 0
    mismatches: list[dict[str, Any]] = []

    for message in messages:
        subject = _safe_text(message.get("subject"))
        expected = expected_swe_count(subject)
        if expected is None:
            continue
        if not sender_is_verified(message):
            mismatches.append({
                "subject": subject,
                "expected": expected,
                "parsed": 0,
                "reason": "sender_not_verified",
            })
            continue
        parsed = len(parse_swe_entries(message.get("body")))
        checked += 1
        expected_total += expected
        parsed_total += parsed
        message["expected_internship_count"] = expected
        message["parsed_internship_count"] = parsed
        message["count_verified"] = parsed == expected
        if parsed != expected:
            mismatches.append({
                "subject": subject,
                "expected": expected,
                "parsed": parsed,
                "reason": "role_count_mismatch",
            })

    return {
        "emails_checked": checked,
        "expected_internship_count": expected_total,
        "parsed_internship_count": parsed_total,
        "count_verified": checked > 0 and not mismatches,
        "count_mismatches": mismatches,
    }


def _handle_application_preparation(
    payload: dict[str, Any],
    state: dict[str, Any],
    renderer_module: Any,
    max_open: int,
    now: datetime,
    *,
    open_browser: bool = False,
) -> dict[str, Any]:
    """Build the verified local queue; optionally open only explicitly opted-in links.

    Queue creation is completely local.  Big-company and application-limited
    roles stay in ``manual_decision``; TikTok, explicit degree/citizenship
    exclusions, obvious nontechnical mismatches, duplicates, and confirmed
    submissions never become preparation candidates.
    """

    _ = renderer_module  # Retained for backwards-compatible callers.
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    history = _migrate_intern_application_history(state)
    queue_result = build_preparation_queue(messages, state, now)
    queue = queue_result["queue"]
    preparation_candidates = [item for item in queue if item["queue_status"] == "prepared"]
    manual_candidates = [item for item in queue if item["queue_status"] == "manual_decision"]
    review_candidates = [item for item in queue if item["queue_status"] == "manual_review"]

    submitted = _record_list(history.get("submitted"))
    prepared = _record_list(history.get("prepared"))
    opened_unverified = _record_list(history.get("opened_unverified"))
    legacy_opened_unverified = _record_list(history.get("legacy_opened_unverified"))
    attempted = _record_list(history.get("attempted"))
    previously_opened_fingerprints = {
        _safe_text(record.get("fingerprint"))
        for record in opened_unverified + legacy_opened_unverified
    }

    now_iso = now.isoformat()
    to_open_urls: list[str] = []
    skipped_opened_count = 0
    for item in preparation_candidates:
        fp = _safe_text(item.get("fingerprint")) or _fingerprint(item)
        url = _safe_text(item.get("apply_url"))
        record = {
            **item,
            "fingerprint": fp,
            "status": "prepared",
            "prepared_at": now_iso,
            "submission_attempted": False,
        }
        _append_unique(prepared, record)
        if fp in previously_opened_fingerprints:
            skipped_opened_count += 1
            continue
        # Fail closed: an explicit interactive open is allowed only after the
        # selected resume file has passed the local footer/content safety gate.
        if open_browser and item.get("resume_attachment_ready") and url not in to_open_urls:
            to_open_urls.append(url)

    for item in manual_candidates:
        _append_unique(
            attempted,
            {
                **item,
                "status": "manual_decision_needed",
                "attempted_at": now_iso,
                "submission_attempted": False,
            },
        )

    for item in review_candidates:
        _append_unique(
            attempted,
            {
                **item,
                "status": "job_requirements_fetch_required",
                "attempted_at": now_iso,
                "submission_attempted": False,
            },
        )

    to_open_urls = to_open_urls[:max(0, max_open)]
    opened_urls: list[str] = []
    failed_urls: list[str] = []
    if open_browser and to_open_urls:
        opened_urls, failed_urls = _open_urls_in_chrome(to_open_urls)

    for item in preparation_candidates:
        fp = _safe_text(item.get("fingerprint")) or _fingerprint(item)
        url = _safe_text(item.get("apply_url"))
        status = "prepared"
        if url in opened_urls:
            status = "opened_unverified"
        elif url in to_open_urls:
            status = "browser_open_failed"
        elif fp in previously_opened_fingerprints:
            status = "already_opened_unverified"
        elif not item.get("resume_attachment_ready"):
            status = "prepared_resume_cleanup_required"
        _append_unique(
            attempted,
            {
                **item,
                "status": status,
                "attempted_at": now_iso,
                "submission_attempted": False,
            },
        )

    for url in opened_urls:
        match = next(
            (item for item in preparation_candidates if _safe_text(item.get("apply_url")) == url),
            None,
        )
        if match is None:
            continue
        _append_unique(
            opened_unverified,
            {
                **match,
                "status": "opened_unverified",
                "opened_at": now_iso,
                "submission_attempted": False,
            },
        )

    history["schema_version"] = 3
    history["mode"] = "prepare_only"
    history["submitted"] = _trim_records(submitted)
    history["prepared"] = _trim_records(prepared)
    history["opened_unverified"] = _trim_records(opened_unverified)
    history["legacy_opened_unverified"] = _trim_records(legacy_opened_unverified)
    history["attempted"] = _trim_records(attempted)
    history["preparation_queue"] = queue
    history["preparation_exclusions"] = queue_result["excluded"]
    history["verified_email_dates"] = queue_result["verified_email_dates"]
    history["last_preparation_at"] = now_iso
    if opened_urls:
        history["last_browser_open_at"] = now_iso

    exclusion_counts: dict[str, int] = {}
    for item in queue_result["excluded"]:
        reason = _safe_text(item.get("reason"), "unknown")
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    queue_stats = queue_result["stats"]
    history["stats"] = {
        "verified_roles": queue_stats["verified_roles"],
        "candidates": len(preparation_candidates),
        "queued": queue_stats["queued"],
        "prepared": queue_stats["prepared"],
        "manual_decision": queue_stats["manual_decision"],
        "manual_review": queue_stats["manual_review"],
        "manual_limit_candidates": sum(
            item.get("decision_reason") == "application_limit" for item in manual_candidates
        ),
        "manual_big_company_candidates": sum(
            item.get("decision_reason") == "big_company" for item in manual_candidates
        ),
        "resume_ready": queue_stats["resume_ready"],
        "resume_cleanup_required": queue_stats["resume_cleanup_required"],
        "excluded": queue_stats["excluded"],
        "exclusion_counts": exclusion_counts,
        "opened_unverified": len(opened_urls),
        "browser_open_failed": len(failed_urls),
        "skipped_previously_opened_unverified": skipped_opened_count,
        "submission_attempted": False,
        "submitted": 0,
    }

    return history["stats"]


def _handle_auto_apply(
    payload: dict[str, Any],
    state: dict[str, Any],
    renderer_module: Any,
    max_open: int,
    now: datetime,
) -> dict[str, Any]:
    """Deprecated compatibility shim: prepare only; never open or submit."""

    return _handle_application_preparation(
        payload,
        state,
        renderer_module,
        max_open=max_open,
        now=now,
        open_browser=False,
    )


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(root / "daily_briefing.json"))
    parser.add_argument("--png", default=str(Path.home() / "Desktop/Daily_Briefing.png"))
    parser.add_argument("--markdown", default=str(Path.home() / "Desktop/Daily_Briefing.md"))
    parser.add_argument("--state", default=str(root / "state.json"))
    parser.add_argument("--local-only", action="store_true", help="Use the collected input only; no Gmail fetch, OAuth or browser operations.")
    parser.add_argument("--plan", action="store_true", help="Print incremental collection plan and budgets without network access or rendering.")
    parser.add_argument("--set-wallpaper", action="store_true")
    parser.add_argument(
        "--prepare-applications",
        action="store_true",
        help="Record eligible internship links as prepared; never submit or open a browser.",
    )
    parser.add_argument(
        "--open-prepared-links",
        action="store_true",
        help="Explicit interactive opt-in to open prepared links; opened pages remain unverified.",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Deprecated safe alias for --prepare-applications; never opens or submits.",
    )
    parser.add_argument("--max-open", type=int, default=30)
    parser.add_argument("--authorize", action="store_true", help="Open interactive OAuth flow if Gmail token needs re-authorization.")
    parser.add_argument("--auth-code", default="")

    args = parser.parse_args()
    started = time.perf_counter()
    if args.local_only and (args.authorize or args.auth_code or args.open_prepared_links):
        parser.error("--local-only cannot authorize or open external pages")

    input_path = _resolve_path(args.input)
    png_path = _resolve_path(args.png)
    markdown_path = _resolve_path(args.markdown)
    state_path = _resolve_path(args.state)
    renderer_path = root / "render_daily_briefing.py"

    state = _load_state(state_path)
    now = datetime.now(_zone())
    config_path = root / "assessment_sources.json"
    config = _read_json(config_path) if config_path.exists() else {}
    if args.plan:
        print(json.dumps(build_refresh_plan(state, config, now), ensure_ascii=False, indent=2))
        return

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_path}")
    if not renderer_path.exists():
        raise FileNotFoundError(f"Renderer not found: {renderer_path}")

    payload = _load_daily_data(input_path)
    payload["resource_links"], payload["plugin_errors"] = load_resource_plugins(root / "plugins")
    _merge_assessment_events(payload, state, now)

    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []

    renderer_module = _load_renderer_module(renderer_path)

    # The Gmail reader below is deliberately SWE-only.  Preserve other Gmail
    # alerts and the last verified SWE snapshot instead of treating an empty or
    # malformed fetch as an authoritative empty inbox.
    non_gmail_messages = [message for message in messages if _safe_text(message.get("source")).lower() != "gmail"]
    prior_gmail_messages = [message for message in messages if _safe_text(message.get("source")).lower() == "gmail"]
    prior_swe_messages = [
        message
        for message in prior_gmail_messages
        if expected_swe_count(message.get("subject")) is not None
    ]
    prior_other_gmail_messages = [
        message
        for message in prior_gmail_messages
        if expected_swe_count(message.get("subject")) is None
    ]
    if args.local_only:
        gmail_messages = prior_swe_messages
        gmail_source_state = dict((payload.get("sources") or {}).get("gmail") or {})
    else:
        gmail_messages, gmail_source_state, _ = _collect_swe_gmail_messages(
            state=state,
            now=now,
            interactive_gmail=bool(args.authorize),
            gmail_auth_code=_safe_text(args.auth_code),
        )
    if gmail_source_state.get("state") != "ok":
        gmail_messages = prior_swe_messages
        if prior_swe_messages:
            gmail_source_state["reused_last_verified_swe_list"] = True
    elif not gmail_messages and prior_swe_messages:
        gmail_messages = prior_swe_messages
        gmail_source_state["reused_last_verified_swe_list"] = True

    count_stats = _validate_swe_message_counts(gmail_messages, renderer_module)
    gmail_source_state.update(count_stats)
    if gmail_source_state.get("state") == "ok" and not count_stats["count_verified"]:
        gmail_source_state["state"] = "error"
        gmail_source_state["error"] = (
            "swe_list_count_mismatch" if count_stats["emails_checked"] else "swe_list_not_found"
        )
        if prior_swe_messages:
            gmail_messages = prior_swe_messages
            gmail_source_state["reused_last_verified_swe_list"] = True

    merged_messages = non_gmail_messages + prior_other_gmail_messages + gmail_messages
    cutoff = now - timedelta(days=14)
    dedup_map = {}
    for message in merged_messages:
        fp = _safe_text(message.get("fingerprint"))
        if not fp:
            message["fingerprint"] = _message_fingerprint(message)
            fp = _safe_text(message.get("fingerprint"))
        if fp in dedup_map:
            continue
        if _normalize_datetime(message.get("received_at"), default=now) < cutoff:
            continue
        dedup_map[fp] = message

    payload["messages"] = sorted(
        dedup_map.values(),
        key=lambda item: _safe_text(item.get("received_at")),
        reverse=True,
    )

    swe_daily, _ = collect_verified_roles(payload["messages"], _zone())
    if swe_daily:
        latest_swe = swe_daily[0]
        payload["swe_list_summary"] = {
            "schema_version": 1,
            "latest": latest_swe,
            "daily": swe_daily,
        }
        gmail_source_state["swe_list_latest"] = latest_swe

    preparation_summary = build_preparation_summary(
        payload["messages"],
        state,
        now,
        source_reused=bool(gmail_source_state.get("reused_last_verified_swe_list")),
    )
    if preparation_summary is not None:
        payload["intern_preparation_summary"] = preparation_summary
        # Only requirement-reviewed candidates are ready to prepare. The raw
        # newsletter title classifier must not advertise unreviewed roles as
        # eligible in the report's source counters.
        latest = (payload.get("swe_list_summary") or {}).get("latest")
        if isinstance(latest, dict):
            latest.update({
                "totalUniqueRoles": preparation_summary["verified_role_count"],
                "manualDecisionCount": preparation_summary["manual_decision_count"],
                "requirementsReviewCount": preparation_summary["manual_review_count"],
                "eligibleSmallCompanyPreparationCount": preparation_summary["prepared_count"],
                "excludedCount": preparation_summary["excluded_count"],
                "eligibility_basis": "strict_requirement_reviews_only",
            })
            gmail_source_state["swe_list_latest"] = latest
    else:
        payload.pop("intern_preparation_summary", None)

    payload["assignments"] = visible_assignments(payload.get("assignments"), now)
    attach_exam_inventory(payload, state, config, now)
    payload["exams"] = visible_assignments(payload.get("exams"), now)
    payload["generated_at"] = now.isoformat()
    payload["timezone"] = payload.get("timezone", str(_zone()))
    source_snapshot = payload.get("sources", {})
    _prepare_sources(
        payload,
        {
            "gmail": gmail_source_state,
            "canvas": source_snapshot.get("canvas", {}),
            "outlook": source_snapshot.get("outlook", {}),
            "linkedin": source_snapshot.get("linkedin", {}),
        },
    )
    run_stats = {
        "status": "ok",
        "message": "Rendered only; no application preparation or browser opening enabled",
        "auto_apply": False,
        "application_preparation": False,
        "browser_opened": False,
        "submission_attempted": False,
    }
    preparation_requested = bool(
        args.prepare_applications or args.open_prepared_links or args.auto_apply
    )
    if preparation_requested:
        if gmail_source_state.get("state") == "ok" and count_stats["count_verified"]:
            preparation_stats = _handle_application_preparation(
                payload,
                state,
                renderer_module,
                max_open=args.max_open,
                now=now,
                open_browser=bool(args.open_prepared_links),
            )
            run_stats = {
                "status": "ok",
                "auto_apply": False,
                "application_preparation": True,
                "browser_opened": bool(args.open_prepared_links),
                "submission_attempted": False,
                "deprecated_auto_apply_flag": bool(args.auto_apply),
                "application_preparation_stats": preparation_stats,
            }
        else:
            run_stats = {
                "status": "blocked",
                "auto_apply": False,
                "application_preparation": False,
                "browser_opened": False,
                "submission_attempted": False,
                "reason": "Gmail retrieval or SWE List count verification failed",
            }

    # Prepare before rendering so this run needs exactly one image generation.
    preparation_summary = build_preparation_summary(
        payload["messages"], state, now,
        source_reused=bool(gmail_source_state.get("reused_last_verified_swe_list")),
    )
    if preparation_summary is not None:
        payload["intern_preparation_summary"] = preparation_summary
    local_processing_ms = round((time.perf_counter() - started) * 1000)
    payload["pipeline_metrics"] = {"mode": "local_only" if args.local_only else "gmail_incremental", "local_processing_ms": local_processing_ms}
    _write_json_atomically(input_path, payload)
    render_started = time.perf_counter()
    render_result = _run_renderer(renderer_path, input_path, png_path, markdown_path, set_wallpaper=args.set_wallpaper)
    metrics = {**payload["pipeline_metrics"], "render_and_apply_ms": round((time.perf_counter() - render_started) * 1000), "total_local_ms": round((time.perf_counter() - started) * 1000)}
    state["last_pipeline_metrics"] = metrics
    state["last_render_at"] = now.isoformat()
    if not args.local_only and gmail_source_state.get("state") == "ok" and count_stats["count_verified"]:
        state["swe_last_successful_at"] = now.isoformat()
    _write_json_atomically(state_path, state)
    if args.local_only and args.set_wallpaper and input_path.resolve() == (root / "daily_briefing.json"):
        record_presentation_revision(root, payload, render_result, metrics)

    print(json.dumps({
        "status": "ok",
        "input": str(input_path),
        "png": str(png_path),
        "markdown": str(markdown_path),
        "set_wallpaper": bool(args.set_wallpaper),
        "run": run_stats,
        "metrics": metrics,
        "render": render_result,
        "sources": payload.get("sources"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
