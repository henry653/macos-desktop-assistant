#!/usr/bin/env python3
"""Render a privacy-conscious daily briefing as a macOS wallpaper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import tempfile
from urllib.parse import unquote, urlparse
from datetime import datetime
from pathlib import Path

from wallpaper_manager import request_wallpaper_update
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from export_widget_snapshot import DEFAULT_GROUP_DIR as WIDGET_GROUP_DIR
from export_widget_snapshot import DEFAULT_STATUS as WIDGET_EXPORT_STATUS
from export_widget_snapshot import export as export_widget_snapshot
from assignment_filters import (
    assignment_due_at,
    assignment_expiry_at,
    assignment_horizon_hours,
    has_verified_exam_review,
    is_exam_assignment,
    is_hidden_assignment,
    is_within_assignment_horizon,
    merge_assignments_and_exams,
    preferred_assignment_url,
)
from render_my_planner import DEFAULT_STATUS as MY_PLANNER_STATUS
from render_my_planner import render as render_clickable_planner
from intern_preparation import canonical_job_url, strict_display_items
from swe_list_summary import collect_verified_roles


WIDTH = 3024
HEIGHT = 1964
TIMEZONE = ZoneInfo("America/New_York")
WIDGET_RELOAD_URL = "myplanner-native://reload-widget"
REGULAR_FONT = Path("/System/Library/Fonts/STHeiti Light.ttc")
BOLD_FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")
SOURCE_ORDER = ("canvas", "gmail", "outlook", "linkedin")
SOURCE_LABELS = {
    "canvas": "Canvas",
    "gmail": "Gmail",
    "outlook": "Outlook",
    "linkedin": "LinkedIn",
}
SOURCE_URLS = {
    "canvas": "https://uncch.instructure.com/calendar#view_name=agenda",
    "gmail": "https://mail.google.com/mail/u/0/#inbox",
    "outlook": "https://outlook.cloud.microsoft/mail/",
    "linkedin": "https://www.linkedin.com/messaging/",
}
DEFAULT_HOTSPOT_MANIFEST = (
    Path.home() / "Library/Application Support/Codex/Daily Briefing/wallpaper_hotspots.json"
)
DEADLINE_HORIZON_HOURS = 72.0
DEADLINE_RED_HOURS = 12.0
DEADLINE_BLUE_HOURS = 48.0
DEADLINE_COLORS = {
    "blue": (72, 151, 255, 255),
    "yellow": (255, 196, 61, 255),
    "red": (255, 82, 96, 255),
    "unknown": (159, 174, 196, 255),
}
INTERN_SOURCE_HINTS = ("swe list", "swe-list", "intern list", "internship list")
SWE_JOB_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
SWE_SUBJECT_COUNT_RE = re.compile(r"(?i)\b(\d+)\s+New Internships? Posted Today\b")
STOR_455_COURSE_RE = re.compile(r"(?i)^STOR\s*455(?=$|[\s._:-])")
COMPLETE_BEFORE_CLASS_RE = re.compile(
    r"(?i)^COMPLETE[\s._:/\-–—]+BEFORE[\s._:/\-–—]+CLASS\b"
)
LIMIT_PATTERNS = (
    re.compile(r"(?i)\b(?:only|just|at\s+most|max(?:imum)?|限|仅|最多|最多可)\s*(\d+)\s*(?:\w+\s*)*(?:岗位|职位|positions|opening[s]?|role[s]?|internship[s]?|application[s]?)\b"),
    re.compile(r"(?i)\b(\d+)\s*(?:个|项|slots?)\s*(?:的?\s*)?(?:岗位|职位|application[s]?|opening[s]?|名额|vacancy)\b.*\b(?:limited|limited\s+to|cap|capit|上限|仅限)\b"),
    re.compile(r"(?i)\b(?:limited|限|名额\s*有限|only)\b(?:\s+(?:to|to\s+apply))?\s*(\d+)\s*(?:\w+\s*)*(?:岗位|职位|applications?|openings|positions)\b"),
)
BIG_COMPANY_HINTS = {
    "apple",
    "amazon",
    "google",
    "alphabet",
    "meta",
    "microsoft",
    "tesla",
    "facebook",
    "x",
    "linkedin",
    "netflix",
    "openai",
    "nvidia",
    "intel",
    "ibm",
    "oracle",
    "salesforce",
    "uber",
    "airbnb",
    "stripe",
    "bytedance",
    "tiktok",
    "byte dance",
    "databricks",
    "coinbase",
    "bloomberg",
    "adobe",
    "deloitte",
    "jpmorgan",
    "goldman sachs",
    "two sigma",
    "blackrock",
    "palantir",
    "yahoo",
    "waymo",
}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = BOLD_FONT if bold else REGULAR_FONT
    if not path.exists():
        path = Path("/System/Library/Fonts/Helvetica.ttc")
    return ImageFont.truetype(str(path), size=size)


FONTS = {
    "hero": load_font(82, True),
    "date": load_font(38),
    "section": load_font(42, True),
    "count": load_font(31, True),
    "title": load_font(35, True),
    "body": load_font(29),
    "small": load_font(25),
    "tiny": load_font(22),
}


def parse_datetime(value: str) -> datetime:
    if not value:
        raise ValueError("A non-empty ISO datetime is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def safe_text(value: Any, fallback: str = "") -> str:
    text = str(value).strip() if value is not None else fallback
    return " ".join(text.split())


def safe_hotspot_url(value: Any) -> str:
    """Return a credential-free HTTP(S) URL suitable for a click target."""

    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    lowered = candidate.casefold()
    if any(
        marker in lowered
        for marker in (
            "access_token=",
            "id_token=",
            "credential=",
            "password=",
        )
    ):
        return ""
    return candidate


def hotspot_id(kind: str, identity: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{kind}|{identity}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{ordinal + 1}-{digest}"


def add_hotspot(
    hotspots: list[dict[str, Any]] | None,
    *,
    kind: str,
    label: str,
    box: tuple[int, int, int, int],
    identity: str,
    url: Any = "",
    local_section: str = "",
    local_action: str = "",
) -> None:
    """Record the exact rectangle just drawn, using only controlled targets."""

    if hotspots is None:
        return
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= WIDTH and 0 <= y1 < y2 <= HEIGHT):
        raise ValueError(f"Hotspot rectangle is outside the wallpaper canvas: {box}")
    target = safe_hotspot_url(url)
    item: dict[str, Any] = {
        "id": hotspot_id(kind, identity, len(hotspots)),
        "kind": kind,
        "label": safe_text(label, kind),
        "rect": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1},
    }
    if target:
        item["url"] = target
    else:
        # Never emit file:// or arbitrary local paths.  The companion app may
        # implement one of these fixed actions without accepting a path from
        # the manifest.
        item["action"] = (
            local_action
            if local_action in {"open_local_report", "start_intern_application_batch"}
            else "open_local_report"
        )
        item["section"] = safe_text(local_section, "overview")
    hotspots.append(item)


def is_hidden_complete_before_class(item: dict[str, Any]) -> bool:
    """Return whether this STOR 455 in-class preparation item should be hidden."""
    course = safe_text(item.get("course"))
    title = safe_text(item.get("title"))
    return bool(STOR_455_COURSE_RE.match(course) and COMPLETE_BEFORE_CLASS_RE.match(title))


def preserve_text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text


def ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    text = safe_text(text)
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "…"
    while text and draw.textlength(text + suffix, font=font) > width:
        text = text[:-1]
    return text.rstrip() + suffix


def wrap_px(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
    max_lines: int,
) -> list[str]:
    text = safe_text(text)
    if not text:
        return []
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    consumed = " ".join(lines)
    if consumed != text and lines:
        lines[-1] = ellipsize(draw, lines[-1] + "…", font, width)
    return lines


def rounded_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int] | None = None,
    radius: int = 34,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def make_background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#07111f")
    draw = ImageDraw.Draw(image)
    top = (8, 19, 34)
    bottom = (19, 31, 49)
    for y in range(HEIGHT):
        ratio = y / max(1, HEIGHT - 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line((0, y, WIDTH, y), fill=color)

    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-320, -480, 1380, 1160), fill=(36, 117, 181, 88))
    glow_draw.ellipse((1960, -360, 3480, 1120), fill=(86, 73, 190, 64))
    glow_draw.ellipse((1520, 1220, 3220, 2620), fill=(14, 116, 120, 44))
    glow = glow.filter(ImageFilter.GaussianBlur(190))
    return Image.alpha_composite(image.convert("RGBA"), glow)


def status_state(source: Any) -> str:
    if isinstance(source, dict):
        return safe_text(source.get("state"), "unknown").lower()
    return "unknown"


def draw_source_chips(
    draw: ImageDraw.ImageDraw,
    sources: dict[str, Any],
    y: int,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    x = 166
    for key in SOURCE_ORDER:
        state = status_state(sources.get(key))
        if state == "ok":
            dot = (78, 214, 158, 255)
            label = "OK"
        elif state == "partial":
            dot = (251, 191, 36, 255)
            label = "PARTIAL"
        elif state in {"error", "unavailable", "auth_required"}:
            dot = (251, 191, 36, 255)
            label = "CHECK"
        else:
            dot = (148, 163, 184, 255)
            label = "UNKNOWN"
        chip_width = 292
        box = (x, y, x + chip_width, y + 64)
        rounded_panel(draw, box, (18, 31, 49, 255), (65, 84, 108, 255), 30, 2)
        draw.ellipse((x + 22, y + 22, x + 42, y + 42), fill=dot)
        draw.text((x + 57, y + 15), f"{SOURCE_LABELS[key]}  {label}", font=FONTS["tiny"], fill=(225, 233, 244, 255))
        boxes.append((key, box))
        x += chip_width + 18
    return boxes


def swe_header_lines(data: dict[str, Any], intern_counts: dict[str, int]) -> tuple[str, str]:
    """Keep the last verified source distinct from a newer unverified email."""
    daily, _ = collect_verified_roles(data.get("messages") or [], TIMEZONE)
    verified = daily[0] if daily else {}
    verified_date = safe_text(verified.get("date"))
    if verified_date:
        first = (
            f"SWE {verified_date} · 末次已核验{verified['totalUniqueRoles']}"
            f" · 手动决定{intern_counts['manual_decision']}"
        )
    else:
        first = "SWE List · 暂无完整已核验清单"

    sources = data.get("sources") or {}
    gmail = sources.get("gmail") if isinstance(sources, dict) else None
    gmail = gmail if isinstance(gmail, dict) else {}
    latest_received = safe_text(gmail.get("latest_swe_list_received_at"))
    try:
        latest_date = parse_datetime(latest_received).date().isoformat()
    except (ValueError, TypeError):
        latest_date = ""
    if latest_date and gmail.get("latest_count_verified") is False:
        expected = safe_text(gmail.get("latest_expected_internship_count"), "?")
        parsed = safe_text(gmail.get("latest_parsed_internship_count"), "?")
        second = f"最新 {latest_date} · {expected}未核验 · 已解析{parsed}"
    elif verified_date:
        second = "以上为邮件来源日期；非本次刷新日期"
    else:
        second = "最新来源待核验 · 详情见报告"
    return first, second


def course_color(course: str) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(course.encode("utf-8")).digest()
    palette = (
        (70, 152, 255, 255),
        (90, 201, 168, 255),
        (180, 126, 255, 255),
        (255, 154, 92, 255),
        (236, 111, 154, 255),
        (87, 194, 224, 255),
    )
    return palette[digest[0] % len(palette)]


def priority_style(value: Any) -> tuple[str, tuple[int, int, int, int]]:
    raw = safe_text(value, "P1").upper()
    if raw in {"P0", "URGENT", "HIGH"}:
        return "P0", (255, 103, 112, 255)
    if raw in {"P2", "LOW", "FYI"}:
        return "P2", (111, 190, 255, 255)
    try:
        number = int(raw)
        if number >= 90:
            return "P0", (255, 103, 112, 255)
        if number < 60:
            return "P2", (111, 190, 255, 255)
    except ValueError:
        pass
    return "P1", (255, 190, 76, 255)


def deadline_urgency(due_at: str, generated_at: datetime) -> dict[str, Any]:
    """Describe a deadline using the wallpaper's fixed 72-hour scale.

    The same boundaries are printed in the panel legend and on every card:
    blue means over 48 hours, yellow means over 12 through 48 hours, and red
    means 12 hours or less.  Past-due work is a separate red warning state.
    """
    try:
        due = parse_datetime(due_at)
    except ValueError:
        return {
            "state": "unknown",
            "color": DEADLINE_COLORS["unknown"],
            "remaining_hours": None,
            "detail_label": "无法计算剩余时间",
        }

    remaining_hours = (due - generated_at).total_seconds() / 3600.0
    if remaining_hours < 0:
        state = "overdue"
        color = DEADLINE_COLORS["red"]
        detail_label = f"已逾期 {abs(remaining_hours):.1f}h · 请立即处理"
    elif remaining_hours <= DEADLINE_RED_HOURS:
        state = "red"
        color = DEADLINE_COLORS["red"]
        detail_label = f"剩余 {remaining_hours:.1f}h · 红色 ≤12h"
    elif remaining_hours <= DEADLINE_BLUE_HOURS:
        state = "yellow"
        color = DEADLINE_COLORS["yellow"]
        detail_label = f"剩余 {remaining_hours:.1f}h · 黄色 12–48h"
    else:
        state = "blue"
        color = DEADLINE_COLORS["blue"]
        detail_label = f"剩余 {remaining_hours:.1f}h · 蓝色 >48h"
    return {
        "state": state,
        "color": color,
        "remaining_hours": remaining_hours,
        "detail_label": detail_label,
    }


def due_label(due_at: str, generated_at: datetime) -> tuple[str, tuple[int, int, int, int]]:
    try:
        due = parse_datetime(due_at)
    except ValueError:
        return "No due time", DEADLINE_COLORS["unknown"]
    urgency = deadline_urgency(due_at, generated_at)
    prefix = "PAST DUE" if urgency["state"] == "overdue" else "DUE"
    return f"{prefix} · {due.strftime('%a %I:%M %p').replace(' 0', ' ')}", urgency["color"]


def draw_deadline_legend(draw: ImageDraw.ImageDraw, y: int) -> None:
    """Draw the exact deadline thresholds above the assignment stack."""
    draw.text((198, y + 4), "截止紧迫度", font=FONTS["tiny"], fill=(176, 193, 215, 255))
    x = 352
    entries = (
        ("蓝 >48h", DEADLINE_COLORS["blue"], 152),
        ("黄 12–48h", DEADLINE_COLORS["yellow"], 174),
        ("红 ≤12h", DEADLINE_COLORS["red"], 152),
    )
    for label, color, width in entries:
        fill = tuple(max(20, int(channel * 0.24)) for channel in color[:3]) + (255,)
        draw.rounded_rectangle((x, y, x + width, y + 36), radius=16, fill=fill, outline=color, width=2)
        draw.ellipse((x + 12, y + 10, x + 28, y + 26), fill=color)
        draw.text((x + 36, y + 4), label, font=FONTS["tiny"], fill=(239, 244, 252, 255))
        x += width + 12


def draw_deadline_meter(
    draw: ImageDraw.ImageDraw,
    urgency: dict[str, Any],
    box: tuple[int, int, int, int],
    horizon_hours: float = DEADLINE_HORIZON_HOURS,
) -> None:
    """Draw horizon→48h, yellow 48→12h, red 12→0h plus a pointer."""
    x1, y1, x2, y2 = box
    if urgency.get("state") == "overdue":
        draw.rounded_rectangle(box, radius=6, fill=DEADLINE_COLORS["red"])
        return

    total_width = x2 - x1
    horizon_hours = max(DEADLINE_BLUE_HOURS, float(horizon_hours))
    blue_end = x1 + round(total_width * (horizon_hours - DEADLINE_BLUE_HOURS) / horizon_hours)
    yellow_end = x1 + round(total_width * (horizon_hours - DEADLINE_RED_HOURS) / horizon_hours)
    draw.rectangle((x1, y1, blue_end, y2), fill=DEADLINE_COLORS["blue"])
    draw.rectangle((blue_end, y1, yellow_end, y2), fill=DEADLINE_COLORS["yellow"])
    draw.rectangle((yellow_end, y1, x2, y2), fill=DEADLINE_COLORS["red"])
    draw.rounded_rectangle(box, radius=6, outline=(224, 233, 245, 210), width=2)

    remaining = urgency.get("remaining_hours")
    if isinstance(remaining, (int, float)):
        clamped = max(0.0, min(horizon_hours, float(remaining)))
        marker_x = x1 + round(total_width * (horizon_hours - clamped) / horizon_hours)
        marker_x = max(x1 + 3, min(x2 - 3, marker_x))
        draw.line((marker_x, y1 - 5, marker_x, y2 + 5), fill=(255, 255, 255, 255), width=5)
        draw.polygon(
            ((marker_x - 8, y1 - 7), (marker_x + 8, y1 - 7), (marker_x, y1 + 2)),
            fill=(255, 255, 255, 255),
        )


def parse_limit_info(text: str) -> tuple[bool, str | None, str | None]:
    note = safe_text(text)
    if not note:
        return False, None, None
    lowered = note.lower()
    for pattern in LIMIT_PATTERNS:
        match = pattern.search(lowered)
        if match:
            limit_number = safe_text(match.group(1)) if match.groups() else None
            snippet = match.group(0).strip()
            return True, limit_number, snippet
    return False, None, None


def is_swe_list_message(item: dict[str, Any]) -> bool:
    fields = (
        safe_text(item.get("sender_display")),
        safe_text(item.get("sender")),
        safe_text(item.get("subject")),
        safe_text(item.get("summary")),
        safe_text(item.get("importance_reason")),
    )
    text = " ".join(fields).lower()
    return any(hint in text for hint in INTERN_SOURCE_HINTS)


def normalize_role_token(token: str) -> str:
    cleaned = safe_text(token)
    cleaned = re.sub(r"^\W+|\W+$", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_company_prefix(prefix: str) -> str:
    stripped = safe_text(prefix)
    if not stripped:
        return ""
    if ":" in stripped:
        stripped = stripped.rsplit(":", 1)[0]
    elif "：" in stripped:
        stripped = stripped.rsplit("：", 1)[0]
    else:
        return ""

    stripped = re.sub(r"^[\s\-•]+\s*", "", stripped)
    stripped = re.sub(r"\*+", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    stripped = stripped.rstrip(":")
    stripped = stripped.strip()
    return clean_company_token(stripped)


def extract_swe_list_entries(message_text: str) -> list[dict[str, str]]:
    combined = str(message_text or "").strip()
    if not combined:
        return []
    entries: list[dict[str, str]] = []
    for line in combined.splitlines():
        for link_match in SWE_JOB_LINK_RE.finditer(line):
            role = normalize_role_token(link_match.group(1))
            url = safe_text(link_match.group(2))
            if not url:
                continue
            company = parse_company_prefix(line[:link_match.start()])
            if not company or not is_reasonable_company(company):
                continue
            is_limited, limit_number, limit_snippet = parse_limit_info(line)
            entries.append({
                "company": company,
                "role": role or "Internship role",
                "url": url,
                "is_limited": is_limited,
                "limit_count": limit_number or "",
                "limit_note": limit_snippet or "",
            })
    if not entries:
        return []

    dedup: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in entries:
        key = (safe_text(item["company"]).lower(), safe_text(item["role"]).lower(), safe_text(item["url"]))
        dedup[key] = item
    return list(dedup.values())


def expected_swe_entry_count(subject: str) -> int | None:
    match = SWE_SUBJECT_COUNT_RE.search(safe_text(subject))
    if not match:
        return None
    return int(match.group(1))


def clean_company_token(token: str) -> str:
    cleaned = safe_text(token)
    if not cleaned:
        return ""
    cleaned = re.sub(r"^\W+|\W+$", "", cleaned)
    cleaned = re.sub(r"(?i)^\s*(?:company|companies|co\.?|firm|organization)\s*[:：\-]\s*", "", cleaned)
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    cleaned = re.sub(r"(?i)\b(?:internship|intern|software|software engineer|engineer|developer|岗位|职位)\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;|-–—")
    cleaned = re.sub(r"[:：\s]+(roles?|positions?|applications?|jobs?)\b.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_reasonable_company(token: str) -> bool:
    compact = safe_text(token).lower()
    compact = re.sub(r"[^a-z0-9& ]", "", compact)
    if not compact:
        return False
    if compact in {"", "swe", "intern", "list", "company"}:
        return False
    if len(compact.split()) > 4:
        return False
    banned = ("only", "limit", "limited", "max", "at most", "apply", "apply to", "positions", "position", "jobs", "job", "roles", "role", "openings", "slots", "spots", "applications")
    if any(word in compact for word in banned):
        return False
    if not re.search(r"[a-z]", compact):
        return False
    return True


def extract_intern_companies(message_text: str) -> list[str]:
    combined = safe_text(message_text)
    if not combined:
        return []

    # Try removing common headers first so company names are more likely to remain.
    simplified = re.sub(r"(?i)^\s*(?:swe\s*list|intern(?:ship)?\s*list)\s*[:：\-]?\s*", "", combined)
    candidates: list[str] = []

    for part in re.split(r"\n|[;；]| \| ", simplified):
        for token in part.split(","):
            token = clean_company_token(token)
            if not token:
                continue
            if len(token) > 55 or not is_reasonable_company(token):
                continue
            candidates.append(token.title())

    for match in re.finditer(r"(?i)\b(?:at|from|within)\s+([A-Za-z][A-Za-z0-9&.'+\-]{1,40})(?:\s|$|[;,])", combined):
        token = clean_company_token(match.group(1))
        if token and len(token) <= 55 and is_reasonable_company(token):
            candidates.append(token.title())

    if not candidates:
        fallback = clean_company_token(simplified)
        if fallback and len(fallback) <= 55:
            candidates.append(fallback.title())

    deduped: list[str] = []
    seen = set()
    for name in candidates:
        key = re.sub(r"\W+", "", name.lower())
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped


def is_big_company(name: str) -> bool:
    lowered = safe_text(name).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    for keyword in BIG_COMPANY_HINTS:
        if keyword == "x":
            if normalized in {"x", "x corp", "x corporation"}:
                return True
            continue
        if keyword in normalized:
            return True
    return False


def partition_intern_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    intern_items: list[dict[str, Any]] = []
    intern_indexes: set[int] = set()
    for index, message in enumerate(messages):
        if not is_swe_list_message(message):
            continue
        source_text = "\n".join([
            preserve_text(message.get("subject")),
            preserve_text(message.get("summary")),
            preserve_text(message.get("importance_reason")),
            preserve_text(message.get("body")),
            preserve_text(message.get("snippet")),
        ]).strip()

        swe_entries = extract_swe_list_entries(source_text)
        expected_count = expected_swe_entry_count(safe_text(message.get("subject")))
        count_verified = expected_count is None or expected_count == len(swe_entries)
        if swe_entries:
            for entry in swe_entries:
                company = safe_text(entry.get("company"), "Unknown")
                intern_items.append({
                    "source": safe_text(message.get("source"), "gmail"),
                    "sender_display": safe_text(message.get("sender_display") or message.get("sender"), "SWE List"),
                    "subject": safe_text(message.get("subject")),
                    "company": company,
                    "role": safe_text(entry.get("role"), "Internship role"),
                    "apply_url": safe_text(entry.get("url")),
                    "limit_count": safe_text(entry.get("limit_count")),
                    "limit_note": safe_text(entry.get("limit_note")),
                    "summary": safe_text(message.get("summary")),
                    "received_at": safe_text(message.get("received_at")),
                    "is_limited": bool(entry.get("is_limited", False)),
                    "is_big": is_big_company(company),
                    "should_auto_apply": not is_big_company(company),
                    "expected_email_count": expected_count,
                    "parsed_email_count": len(swe_entries),
                    "count_verified": count_verified,
                    "__source_index": index,
                })
        else:
            companies = extract_intern_companies(source_text)
            if not companies:
                companies = [safe_text(message.get("subject")) or safe_text(message.get("summary")) or "SWE 公司"]
            for company in companies[:3]:
                is_limited, limit_number, limit_snippet = parse_limit_info(source_text)
                intern_items.append({
                    "source": safe_text(message.get("source"), "gmail"),
                    "sender_display": safe_text(message.get("sender_display") or message.get("sender"), "SWE List"),
                    "subject": safe_text(message.get("subject")),
                    "company": company.title(),
                    "role": "Internship role",
                    "apply_url": "",
                    "limit_count": limit_number,
                    "limit_note": limit_snippet,
                    "summary": safe_text(message.get("summary")),
                    "received_at": safe_text(message.get("received_at")),
                    "is_limited": is_limited,
                    "is_big": is_big_company(company),
                    "should_auto_apply": not is_big_company(company),
                    "__source_index": index,
                })
        intern_indexes.add(index)
    # Sort big companies first, then limited-entry items, then newest received.
    intern_items.sort(
        key=lambda item: (
            0 if item.get("is_big") else 1,
            0 if item.get("is_limited") else 1,
            -(
                parse_datetime(item.get("received_at")).timestamp()
                if item.get("received_at")
                else 0.0
            ),
        )
    )

    remaining_messages = [message for idx, message in enumerate(messages) if idx not in intern_indexes]
    return intern_items, remaining_messages


def draw_intern_card(draw: ImageDraw.ImageDraw, item: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    rounded_panel(draw, box, (16, 33, 54, 255), (45, 64, 87, 255), 24, 2)
    company = safe_text(item.get("company"), "Company")
    role = safe_text(item.get("role"), "Internship role")
    accent = course_color(company)
    draw.rounded_rectangle((x1, y1, x1 + 9, y2), radius=4, fill=accent)
    draw.rounded_rectangle((x1 + 24, y1 + 22, x1 + 96, y1 + 56), radius=14, fill=(32, 65, 94, 255))
    draw.text((x1 + 40, y1 + 26), "SWE", font=FONTS["tiny"], fill=(160, 194, 255, 255))

    queue_status = safe_text(item.get("queue_status"))
    badge_style = {
        "manual_decision": ("手动决定", (255, 190, 76, 255), (76, 58, 16, 255)),
        "manual_review": ("要求待核验", (122, 194, 255, 255), (20, 55, 86, 255)),
        "prepared": ("READY", (124, 232, 177, 255), (18, 67, 49, 255)),
    }
    badge, color, badge_fill = badge_style.get(
        queue_status,
        ("待核验", (178, 190, 208, 255), (45, 55, 69, 255)),
    )
    width = int(draw.textlength(badge, font=FONTS["tiny"])) + 30
    draw.rounded_rectangle((x2 - width - 26, y1 + 16, x2 - 24, y1 + 52), radius=14, fill=badge_fill)
    draw.text((x2 - width - 11, y1 + 21), badge, font=FONTS["tiny"], fill=color)

    sender = safe_text(item.get("sender_display"), "SWE List")
    header = f"{sender} · {safe_text(item.get('received_at')).split('T')[0] if safe_text(item.get('received_at')) else 'recent'}"
    draw.text((x1 + 116, y1 + 25), ellipsize(draw, header, FONTS["tiny"], x2 - x1 - 300), font=FONTS["tiny"], fill=(171, 190, 214, 255))
    draw.text((x1 + 50, y1 + 54), ellipsize(draw, company, FONTS["title"], x2 - x1 - 88), font=FONTS["title"], fill=(244, 248, 252, 255))
    role_color = (245, 188, 188, 255) if item.get("is_limited") else (180, 199, 221, 255)
    role_prefix = "LIMIT · " if item.get("is_limited") else ""
    draw.text((x1 + 50, y1 + 94), ellipsize(draw, role_prefix + role, FONTS["tiny"], x2 - x1 - 88), font=FONTS["tiny"], fill=role_color)
def is_submitted_status(value: Any) -> bool:
    status = re.sub(r"[^a-z0-9]+", " ", safe_text(value).lower())
    compact = " ".join(status.split())
    status = f" {compact} "
    if not compact:
        return False
    if " not completed " in status or " not submitted " in status:
        return False

    submitted_markers = (
        "submitted",
        "completed",
        "graded",
        "turnedin",
        "turned in",
        "final",
        "done",
    )
    if " turned in " in status:
        return True
    for marker in submitted_markers:
        if marker not in {"turnedin", "turned in"} and f" {marker} " in status:
            return True
    return False


def split_assignments(assignments: list[dict[str, Any]], generated_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return actionable future items and an intentionally empty legacy list.

    The second element is retained for API compatibility with older callers,
    but overdue work is no longer a My Planner product surface.  Exams use a
    seven-day window; ordinary assignments continue to use 72 hours.
    """

    upcoming: list[dict[str, Any]] = []

    for item in assignments:
        if is_submitted_status(item.get("status")):
            continue
        try:
            due = parse_datetime(safe_text(assignment_due_at(item)))
        except ValueError:
            continue

        if is_hidden_assignment(item, generated_at):
            continue
        if is_within_assignment_horizon(item, generated_at):
            upcoming.append(item)

    # Reserve the scarce wallpaper slots for exams first, then order each class
    # by time.  This guarantees a seven-day exam cannot be hidden behind three
    # ordinary assignments in the 72-hour window.
    upcoming.sort(
        key=lambda item: (
            0 if is_exam_assignment(item) else 1,
            safe_text(assignment_due_at(item)),
        )
    )
    return upcoming, []


def draw_assignment_card(
    draw: ImageDraw.ImageDraw,
    item: dict[str, Any],
    box: tuple[int, int, int, int],
    generated_at: datetime,
) -> None:
    x1, y1, x2, y2 = box
    rounded_panel(draw, box, (17, 31, 49, 255), (45, 64, 87, 255), 28, 2)
    course = safe_text(item.get("course"), "COURSE")
    badge_label = f"EXAM · {course}" if is_exam_assignment(item) else course
    accent = course_color(course)
    draw.rounded_rectangle((x1, y1, x1 + 9, y2), radius=4, fill=accent)
    badge_width = min(520, int(draw.textlength(badge_label, font=FONTS["tiny"])) + 48)
    badge_fill = tuple(max(24, int(channel * 0.34)) for channel in accent[:3]) + (255,)
    draw.rounded_rectangle((x1 + 34, y1 + 25, x1 + 34 + badge_width, y1 + 66), radius=19, fill=badge_fill)
    draw.text((x1 + 55, y1 + 31), ellipsize(draw, badge_label, FONTS["tiny"], badge_width - 42), font=FONTS["tiny"], fill=(228, 241, 255, 255))
    due_value = safe_text(assignment_due_at(item))
    date_only_exam = bool(
        is_exam_assignment(item)
        and (
            safe_text(item.get("time_precision")).casefold() == "date_only"
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_value)
        )
    )
    if date_only_exam:
        exam_day = parse_datetime(due_value)
        due = f"EXAM · {exam_day.strftime('%a %b %d').replace(' 0', ' ')} · 时间待确认"
        due_color = DEADLINE_COLORS["blue"]
    else:
        due, due_color = due_label(due_value, generated_at)
    due_width = int(draw.textlength(due, font=FONTS["tiny"]))
    draw.text((x2 - 32 - due_width, y1 + 31), due, font=FONTS["tiny"], fill=due_color)
    title = ellipsize(draw, safe_text(item.get("title"), "Untitled assignment"), FONTS["title"], x2 - x1 - 76)
    draw.text((x1 + 36, y1 + 78), title, font=FONTS["title"], fill=(245, 248, 252, 255))
    urgency_value = due_value
    urgency = deadline_urgency(urgency_value, generated_at)
    detail_label = urgency["detail_label"]
    if date_only_exam:
        days = (exam_day.date() - generated_at.date()).days
        detail_label = (f"{days} 天后" if days > 0 else "今天") + " · 具体时刻待确认"
    if is_exam_assignment(item):
        review_label = "复习资料已核验" if has_verified_exam_review(item) else "暂无已核验复习资料"
        detail_label = f"{detail_label} · {review_label}"
    draw.text((x1 + 36, y1 + 120), detail_label, font=FONTS["tiny"], fill=urgency["color"])
    draw_deadline_meter(
        draw,
        urgency,
        (x1 + 36, y1 + 149, x2 - 36, y1 + 160),
        assignment_horizon_hours(item),
    )


def draw_message_card(draw: ImageDraw.ImageDraw, item: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    rounded_panel(draw, box, (17, 31, 49, 255), (45, 64, 87, 255), 28, 2)
    level, color = priority_style(item.get("priority"))
    priority_fill = tuple(max(26, int(channel * 0.34)) for channel in color[:3]) + (255,)
    draw.rounded_rectangle((x1 + 28, y1 + 25, x1 + 96, y1 + 67), radius=18, fill=priority_fill)
    draw.text((x1 + 45, y1 + 31), level, font=FONTS["tiny"], fill=color)
    source = SOURCE_LABELS.get(safe_text(item.get("source")).lower(), safe_text(item.get("source"), "Message"))
    sender = safe_text(item.get("sender_display") or item.get("sender"), "Important sender")
    header = f"{source} · {sender}"
    draw.text((x1 + 116, y1 + 31), ellipsize(draw, header, FONTS["tiny"], x2 - x1 - 154), font=FONTS["tiny"], fill=(176, 193, 215, 255))
    summary = safe_text(item.get("summary") or item.get("subject"), "Important message")
    lines = wrap_px(draw, summary, FONTS["title"], x2 - x1 - 66, 2)
    y = y1 + 82
    for line in lines:
        draw.text((x1 + 32, y), line, font=FONTS["title"], fill=(245, 248, 252, 255))
        y += 44
    reason = safe_text(item.get("importance_reason"))
    if reason and len(lines) <= 1:
        draw.text((x1 + 32, y2 - 43), ellipsize(draw, reason, FONTS["tiny"], x2 - x1 - 64), font=FONTS["tiny"], fill=(154, 173, 198, 255))


def draw_empty_state(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], message: str) -> None:
    x1, y1, x2, y2 = box
    center_x = (x1 + x2) // 2
    center_y = (y1 + y2) // 2
    draw.ellipse((center_x - 49, center_y - 88, center_x + 49, center_y + 10), fill=(62, 205, 150, 42), outline=(90, 219, 166, 220), width=4)
    draw.line((center_x - 23, center_y - 39, center_x - 5, center_y - 19), fill=(108, 231, 180, 255), width=7)
    draw.line((center_x - 5, center_y - 19, center_x + 31, center_y - 57), fill=(108, 231, 180, 255), width=7)
    width = int(draw.textlength(message, font=FONTS["body"]))
    draw.text((center_x - width // 2, center_y + 43), message, font=FONTS["body"], fill=(195, 211, 230, 255))


def render(
    data: dict[str, Any],
    *,
    hotspot_sink: list[dict[str, Any]] | None = None,
) -> Image.Image:
    generated_at = parse_datetime(safe_text(data.get("generated_at")))
    assignments = merge_assignments_and_exams(
        data.get("assignments"),
        data.get("exams"),
    )
    all_messages = list(data.get("messages") or [])
    sources = dict(data.get("sources") or {})
    upcoming_assignments, _ = split_assignments(assignments, generated_at)
    _, messages = partition_intern_messages(all_messages)
    intern_applications = strict_display_items(
        all_messages,
        data.get("intern_preparation_summary"),
        zone=TIMEZONE,
    )
    intern_counts = {
        status: sum(item.get("queue_status") == status for item in intern_applications)
        for status in ("manual_decision", "manual_review", "prepared")
    }
    status_order = ("manual_decision", "manual_review", "prepared")
    visible_interns: list[dict[str, Any]] = []
    # Give each available class one representative card, then fill remaining
    # slots in strict priority order.  This prevents one large-company batch
    # from hiding every requirements-review or actually prepared role.
    for status in status_order:
        match = next((item for item in intern_applications if item.get("queue_status") == status), None)
        if match is not None:
            visible_interns.append(match)
    for item in intern_applications:
        if len(visible_interns) >= 4:
            break
        if item not in visible_interns:
            visible_interns.append(item)
    messages.sort(key=lambda item: ({"P0": 0, "P1": 1, "P2": 2}.get(priority_style(item.get("priority"))[0], 1), safe_text(item.get("received_at"))), reverse=False)

    image = make_background()
    draw = ImageDraw.Draw(image, "RGBA")

    draw.text((160, 108), "My Planner  ·  每日计划", font=FONTS["hero"], fill=(247, 250, 255, 255))
    date_text = generated_at.strftime("%A, %B %d, %Y").replace(" 0", " ")
    time_text = generated_at.strftime("Updated %I:%M %p · America/New_York").replace(" 0", " ")
    draw.text((164, 212), f"{date_text}    {time_text}", font=FONTS["date"], fill=(170, 190, 216, 255))
    for key, box in draw_source_chips(draw, sources, 276):
        add_hotspot(
            hotspot_sink,
            kind="source",
            label=SOURCE_LABELS[key],
            box=box,
            identity=key,
            url=SOURCE_URLS[key],
        )

    left = (150, 382, 1690, 1804)
    right = (1720, 382, 2730, 1804)
    rounded_panel(draw, left, (5, 14, 26, 255), (78, 96, 121, 255), 40, 2)
    rounded_panel(draw, right, (5, 14, 26, 255), (78, 96, 121, 255), 40, 2)

    draw.text((198, 424), "Canvas · 作业 72 小时 / 考试 7 天", font=FONTS["section"], fill=(239, 245, 252, 255))
    draw.text((1498, 431), str(len(upcoming_assignments)), font=FONTS["count"], fill=(122, 194, 255, 255))
    draw_deadline_legend(draw, 467)
    draw.text((1768, 410), "Important messages · 重要消息", font=FONTS["section"], fill=(239, 245, 252, 255))
    draw.text((2580, 417), str(len(messages)), font=FONTS["count"], fill=(255, 190, 76, 255))
    for header_y, header_line in zip((455, 477), swe_header_lines(data, intern_counts)):
        draw.text(
            (1768, header_y),
            ellipsize(draw, header_line, FONTS["tiny"], 502),
            font=FONTS["tiny"],
            fill=(170, 212, 255, 255),
        )
    control_box = (2290, 457, 2690, 500)
    draw.rounded_rectangle(control_box, radius=18, fill=(18, 56, 88, 255), outline=(91, 169, 242, 255), width=2)
    control_label = f"自动申请中心 · READY {intern_counts['prepared']}  ↗"
    draw.text((2310, 466), control_label, font=FONTS["tiny"], fill=(188, 222, 255, 255))
    add_hotspot(
        hotspot_sink,
        kind="intern_control",
        label=(
            f"打开自动申请中心：{intern_counts['prepared']} 个可准备，"
            f"{intern_counts['manual_decision']} 个需手动决定，"
            f"{intern_counts['manual_review']} 个待核验"
        ),
        box=control_box,
        identity="intern-application-center",
        local_section="internships",
        local_action="start_intern_application_batch",
    )

    visible_assignments = upcoming_assignments[:6]
    card_height = 169
    y = 520
    if visible_assignments:
        for index, item in enumerate(visible_assignments):
            card_box = (190, y, 1650, y + card_height)
            draw_assignment_card(draw, item, card_box, generated_at)
            add_hotspot(
                hotspot_sink,
                kind="exam" if is_exam_assignment(item) else "assignment",
                label=f"{safe_text(item.get('course'), 'Course')} · {safe_text(item.get('title'), 'Untitled assignment')}",
                box=card_box,
                identity=f"upcoming|{safe_text(item.get('course'))}|{safe_text(item.get('title'))}|{safe_text(assignment_due_at(item))}|{index}",
                url=preferred_assignment_url(item),
                local_section="exams" if is_exam_assignment(item) else "upcoming",
            )
            y += card_height + 18
        if len(upcoming_assignments) > len(visible_assignments):
            more_box = (190, 1738, 1650, 1791)
            draw.text((210, 1750), f"+ {len(upcoming_assignments) - len(visible_assignments)} more · see Markdown report", font=FONTS["small"], fill=(145, 166, 193, 255))
            add_hotspot(
                hotspot_sink,
                kind="more",
                label="更多未来作业和考试",
                box=more_box,
                identity="more-upcoming-assignments",
                local_section="upcoming",
            )
    else:
        draw_empty_state(draw, (190, 520, 1650, 1715), "未来 72 小时无作业，未来 7 天无考试")

    message_limit = 2 if intern_applications else 4
    visible_messages = messages[:message_limit]
    if visible_messages:
        y = 502
        card_height = 169
        for index, item in enumerate(visible_messages):
            card_box = (1760, y, 2690, y + card_height)
            draw_message_card(draw, item, card_box)
            add_hotspot(
                hotspot_sink,
                kind="message",
                label=f"{safe_text(item.get('sender_display') or item.get('sender'), 'Important sender')} · {safe_text(item.get('summary') or item.get('subject'), 'Important message')}",
                box=card_box,
                identity=f"message|{safe_text(item.get('source'))}|{safe_text(item.get('sender_display') or item.get('sender'))}|{safe_text(item.get('subject'))}|{index}",
                url=item.get("url"),
                local_section="messages",
            )
            y += card_height + 18
        if len(messages) > len(visible_messages):
            more_y = y
            draw.text((1780, more_y), f"+ {len(messages) - len(visible_messages)} more important messages · see Markdown report", font=FONTS["small"], fill=(145, 166, 193, 255))
            add_hotspot(
                hotspot_sink,
                kind="more",
                label="更多重要消息",
                box=(1760, max(0, more_y - 12), 2690, min(HEIGHT, more_y + 41)),
                identity="more-important-messages",
                local_section="messages",
            )
            y += 42
    else:
        draw_empty_state(draw, (1760, 510, 2690, 1715), "今天没有需要立即处理的重要消息")
        y = 1160

    if visible_interns:
        if not visible_messages:
            y = 1210
        elif len(visible_messages) < 4:
            y += 30
        else:
            y += 34
        section_labels = {
            "manual_decision": "手动决定（大公司 / 申请数量限制）",
            "manual_review": "要求待核验（学历 / 身份 / 申请限制）",
            "prepared": "小公司（要求已核验，可准备申请）",
        }
        for status in status_order:
            section_items = [item for item in visible_interns if item.get("queue_status") == status]
            if not section_items:
                continue
            draw.text((1768, y), section_labels[status], font=FONTS["tiny"], fill=(170, 212, 255, 255))
            y += 32
            for index, item in enumerate(section_items):
                card_box = (1760, y, 2690, y + 126)
                draw_intern_card(draw, item, card_box)
                add_hotspot(
                    hotspot_sink,
                    kind="intern",
                    label=f"{safe_text(item.get('company'), 'Company')} · {safe_text(item.get('role'), 'Internship role')}",
                    box=card_box,
                    identity=f"intern|{safe_text(item.get('queue_status'))}|{safe_text(item.get('company'))}|{safe_text(item.get('role'))}|{safe_text(item.get('apply_url'))}|{index}",
                    url=item.get("apply_url"),
                    local_section="internships",
                )
                y += 138
            y += 8

        remaining = len(intern_applications) - len(visible_interns)
        if remaining > 0:
            draw.text((1780, 1735), f"+ {remaining} more SWE List opportunities · see Markdown report", font=FONTS["small"], fill=(145, 166, 193, 255))
            add_hotspot(
                hotspot_sink,
                kind="more",
                label="更多 SWE List 岗位",
                box=(1760, 1723, 2690, 1776),
                identity="more-internships",
                local_section="internships",
            )

    auth_warnings = []
    warnings = []
    for key in SOURCE_ORDER:
        source = sources.get(key)
        if status_state(source) == "auth_required" or (
            isinstance(source, dict) and source.get("auth_required") is True
        ):
            auth_warnings.append(SOURCE_LABELS.get(key, key))
        elif status_state(source) != "ok":
            warnings.append(SOURCE_LABELS.get(key, key))
    footer = "普通点击桌面文件 · 按住 Option 键显示并点击计划卡片 · 完整详情见桌面 My Planner 报告"
    if warnings:
        footer += " · Check source status: " + ", ".join(warnings)
    if auth_warnings:
        footer += " · Check login: " + ", ".join(auth_warnings)
    draw.text((164, 1870), footer, font=FONTS["small"], fill=(127, 150, 180, 255))
    add_hotspot(
        hotspot_sink,
        kind="report",
        label="打开完整 My Planner 报告",
        box=(150, 1848, 2730, 1925),
        identity="full-report",
        local_section="overview",
    )
    return image.convert("RGB")


def write_markdown(data: dict[str, Any]) -> str:
    generated_at = parse_datetime(safe_text(data.get("generated_at")))
    sources = dict(data.get("sources") or {})
    assignments = merge_assignments_and_exams(
        data.get("assignments"),
        data.get("exams"),
    )
    all_messages = list(data.get("messages") or [])
    _, messages = partition_intern_messages(all_messages)
    _, verified_intern_roles = collect_verified_roles(all_messages, TIMEZONE)
    upcoming_assignments, _ = split_assignments(assignments, generated_at)
    lines = [
        "# My Planner · 每日计划",
        "",
        f"Updated: {generated_at.strftime('%Y-%m-%d %I:%M %p %Z').replace(' 0', ' ')}",
        "",
        "## Source status",
        "",
    ]
    for key in SOURCE_ORDER:
        source = sources.get(key) if isinstance(sources.get(key), dict) else {}
        state = safe_text(source.get("state"), "unknown")
        error = safe_text(source.get("error"))
        lines.append(f"- {SOURCE_LABELS[key]}: {state}" + (f" — {error}" if error else ""))

    collection_notes = data.get("collection_notes")
    if isinstance(collection_notes, list):
        notes = [safe_text(note) for note in collection_notes if safe_text(note)]
        if notes:
            lines.extend(["", "### Collection notes", ""])
            lines.extend(f"- {note}" for note in notes)

    lines.extend(["", "## Canvas · assignments within 72 hours / exams within 7 days", ""])
    if not upcoming_assignments:
        lines.append("No actionable assignments are due within 72 hours and no exams are scheduled within 7 days.")
    for item in upcoming_assignments:
        course = safe_text(item.get("course"), "Course")
        title = safe_text(item.get("title"), "Untitled assignment")
        due = safe_text(assignment_due_at(item), "No due time")
        url = safe_hotspot_url(preferred_assignment_url(item))
        link = f"[{title}]({url})" if url else title
        prefix = "EXAM · " if is_exam_assignment(item) else ""
        lines.append(f"- **{prefix}{course}** — {link}; due {due}")

    coverage = data.get("exam_coverage") or []
    if coverage:
        lines.extend(["", "## 全学期考试记录 · 桌面提前 7 天显示", ""])
        for course in coverage:
            next_event = course.get("next_assessment") or {}
            lines.append(f"- **{course['course']}**：已记录 {course['known_future_count']} 场，其中 {course['visible_7d_count']} 场在一周内；最近：{next_event.get('title', '日期待核实')} {next_event.get('date', '')}。核验：{course['state']}；{course.get('coverage_note', '')}")
        lines.append("")
        for event in data.get("exam_inventory") or []:
            url = safe_hotspot_url(preferred_assignment_url(event))
            title = safe_text(event.get("title"))
            link = f"[{title}]({url})" if url else title
            lines.append(f"- {event.get('course')} · {link} · {assignment_due_at(event)}")

    lines.extend(["", "## Intern 申请（SWE List）· 已核验完整原始清单", ""])
    lines.append("本节保留全部数量核验成功的原始岗位，包括随后被严格队列排除的岗位；桌面卡片仅显示严格队列中的行动项。")
    lines.append("")
    if not verified_intern_roles:
        lines.append("No count-verified SWE List internship entries are available.")
    for item in verified_intern_roles:
        company = safe_text(item.get("company"), "Company")
        role = safe_text(item.get("role"), "Internship role")
        report_date = safe_text(item.get("date"), "unknown date")
        url = canonical_job_url(item.get("url"))
        link = f" [Open]({url})" if url else " — URL unavailable"
        lines.append(f"- **{report_date} · {company} · {role}**{link}")

    lines.extend(["", "## Important messages", ""])
    if not messages:
        lines.append("No important messages need immediate attention.")
    for item in messages:
        level, _ = priority_style(item.get("priority"))
        source = SOURCE_LABELS.get(safe_text(item.get("source")).lower(), safe_text(item.get("source"), "Message"))
        sender = safe_text(item.get("sender_display") or item.get("sender"), "Important sender")
        summary = safe_text(item.get("summary") or item.get("subject"), "Important message")
        reason = safe_text(item.get("importance_reason"))
        url = safe_text(item.get("url"))
        link = f"[Open]({url})" if url else "Open the source app to review."
        lines.append(f"- **{level} · {source} · {sender}** — {summary}. {reason} {link}".strip())

    lines.extend(["", "_The wallpaper intentionally omits message bodies, codes, private identifiers, and sensitive links._", ""])
    return "\n".join(lines)


def atomic_save_png(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".png", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        image.save(temporary_path, format="PNG", optimize=True)
        with Image.open(temporary_path) as check:
            if check.size != (WIDTH, HEIGHT) or check.mode != "RGB":
                raise ValueError("Rendered PNG failed validation")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_save_text(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".md", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_hotspot_manifest(
    *,
    generated_at: datetime,
    image_path: Path,
    hotspots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build and validate the fixed 3024×1964 top-left hotspot contract."""

    image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
    identifiers: set[str] = set()
    for item in hotspots:
        identifier = safe_text(item.get("id"))
        if not identifier or identifier in identifiers:
            raise ValueError(f"Hotspot IDs must be non-empty and unique: {identifier!r}")
        identifiers.add(identifier)
        remote_url = item.get("url")
        local_action = item.get("action")
        if remote_url:
            if safe_hotspot_url(remote_url) != remote_url or local_action:
                raise ValueError(f"Unsafe or ambiguous hotspot target: {identifier}")
        elif local_action not in {"open_local_report", "start_intern_application_batch"} or not safe_text(item.get("section")):
            raise ValueError(f"Hotspot has no controlled target: {identifier}")

    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "canvas": {
            "width": WIDTH,
            "height": HEIGHT,
            "coordinate_origin": "top_left",
        },
        "image_sha256": image_sha256,
        "hotspots": hotspots,
    }


def atomic_save_json(value: dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".json", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def cached_wallpaper_copy(source: Path, generated_at: datetime) -> Path:
    cache = Path.home() / "Library/Application Support/Codex/Daily Briefing/wallpapers"
    cache.mkdir(parents=True, exist_ok=True)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    destination = cache / (
        f"Daily_Briefing_{generated_at.strftime('%Y%m%d_%H%M%S')}_{source_digest}.png"
    )
    shutil.copy2(source, destination)
    wallpapers = sorted(cache.glob("Daily_Briefing_*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    referenced: set[Path] = set()
    store = Path.home() / "Library/Application Support/com.apple.wallpaper/Store/Index.plist"
    try:
        with store.open("rb") as handle:
            wallpaper_store = plistlib.load(handle)

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect(child)
            elif isinstance(value, str):
                candidate: Path | None = None
                if value.startswith("file://"):
                    parsed = urlparse(value)
                    candidate = Path(unquote(parsed.path))
                elif value.startswith("/"):
                    candidate = Path(value)
                if candidate is not None:
                    referenced.add(candidate.expanduser().resolve())

        collect(wallpaper_store)
    except (OSError, plistlib.InvalidFileException, ValueError):
        # Cache cleanup is optional; never risk deleting an active wallpaper
        # just because Apple's store cannot be read at this moment.
        referenced = {path.resolve() for path in wallpapers}
    for old in wallpapers[10:]:
        if old.resolve() not in referenced:
            old.unlink(missing_ok=True)
    return destination


def set_all_desktops(image_path: Path, generated_at: datetime) -> dict[str, Any]:
    result = request_wallpaper_update(
        image_path,
        valid_for_date=generated_at.date().isoformat(),
        source="local_renderer",
        source_generated_at=generated_at.isoformat(),
    )
    if result.get("status") != "ok" or result.get("presentation_verified") is not True:
        raise RuntimeError(f"Wallpaper update failed: {json.dumps(result, ensure_ascii=False)}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--png", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument(
        "--html",
        type=Path,
        default=Path.home() / "Desktop/My_Planner.html",
        help="Local interactive planner; each card links to its source item.",
    )
    parser.add_argument(
        "--hotspots",
        type=Path,
        default=DEFAULT_HOTSPOT_MANIFEST,
        help="Atomic clickable-region manifest aligned to the rendered 3024x1964 wallpaper.",
    )
    parser.add_argument(
        "--export-widget-snapshot",
        action="store_true",
        help="Opt in to the retired white WidgetKit surfaces; disabled by default.",
    )
    parser.add_argument("--set-wallpaper", action="store_true")
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object")
    generated_at = parse_datetime(safe_text(data.get("generated_at")))

    hotspots: list[dict[str, Any]] = []
    image = render(data, hotspot_sink=hotspots)
    markdown = write_markdown(data)
    png_path = args.png.expanduser().resolve()
    atomic_save_png(image, png_path)
    atomic_save_text(markdown, args.markdown.expanduser().resolve())
    hotspot_path = args.hotspots.expanduser().resolve()
    hotspot_manifest = build_hotspot_manifest(
        generated_at=generated_at,
        image_path=png_path,
        hotspots=hotspots,
    )
    atomic_save_json(hotspot_manifest, hotspot_path)
    interactive_result = render_clickable_planner(
        args.input.expanduser().resolve(),
        args.html.expanduser().resolve(),
        MY_PLANNER_STATUS,
        force=True,
    )
    if args.export_widget_snapshot:
        widget_result = export_widget_snapshot(
            args.input.expanduser().resolve(),
            WIDGET_GROUP_DIR,
            WIDGET_EXPORT_STATUS,
            reload_url=WIDGET_RELOAD_URL,
            force_reload=True,
        )
    else:
        widget_result = {
            "status": "disabled",
            "reason": "native_widgets_retired_in_favor_of_wallpaper_hotspots",
        }

    active_wallpaper = None
    wallpaper_result = None
    if args.set_wallpaper:
        active_wallpaper = cached_wallpaper_copy(args.png.expanduser().resolve(), generated_at)
        wallpaper_result = set_all_desktops(active_wallpaper, generated_at)

    print(json.dumps({
        "png": str(args.png.expanduser().resolve()),
        "markdown": str(args.markdown.expanduser().resolve()),
        "html": str(args.html.expanduser().resolve()),
        "hotspots": str(hotspot_path),
        "hotspot_count": len(hotspots),
        "interactive": interactive_result,
        "widget_snapshot": widget_result,
        "active_wallpaper": str(active_wallpaper) if active_wallpaper else None,
        "wallpaper": wallpaper_result,
        "assignments": len(
            split_assignments(
                merge_assignments_and_exams(data.get("assignments"), data.get("exams")),
                generated_at,
            )[0]
        ),
        "messages": len(data.get("messages") or []),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
