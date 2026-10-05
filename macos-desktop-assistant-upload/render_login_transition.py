#!/usr/bin/env python3
"""Render and apply a dated login-transition wallpaper.

The transition is intentionally not a completed briefing.  It gives immediate
visual confirmation that the new day was detected while the signed cloud sync
or the full Chrome collector runs.  It never replaces the last-good receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import wallpaper_manager


WIDTH = 3024
HEIGHT = 1964
TIMEZONE = ZoneInfo("America/New_York")
ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
TRANSITION_DIR = ROOT / "login_transitions"
TRANSITION_STATUS = ROOT / "login_transition_status.json"
REGULAR_FONT = Path("/System/Library/Fonts/STHeiti Light.ttc")
BOLD_FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")
FINAL_SOURCES = {"local_renderer", "cloud_signed_manifest"}
TRANSITION_SOURCE = "login_transition"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = BOLD_FONT if bold else REGULAR_FONT
    if not path.exists():
        path = Path("/System/Library/Fonts/Helvetica.ttc")
    return ImageFont.truetype(str(path), size=size)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
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


def _atomic_save_png(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".png", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        image.save(temporary_path, format="PNG", optimize=True)
        with Image.open(temporary_path) as check:
            if check.size != (WIDTH, HEIGHT) or check.mode != "RGB":
                raise ValueError("transition PNG failed validation")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_for(now: datetime) -> Path:
    return TRANSITION_DIR / f"My_Planner_Updating_{now.strftime('%Y%m%d')}.png"


def render_transition(now: datetime) -> Image.Image:
    """Create a deterministic, privacy-safe transition for ``now``'s date."""
    now = now.astimezone(TIMEZONE)
    image = Image.new("RGB", (WIDTH, HEIGHT), (7, 18, 34))
    draw = ImageDraw.Draw(image, "RGBA")

    # A deterministic gradient keeps repeat renders byte-stable for a date.
    for y in range(HEIGHT):
        ratio = y / max(1, HEIGHT - 1)
        red = int(8 + 13 * ratio)
        green = int(24 + 16 * ratio)
        blue = int(47 + 35 * ratio)
        draw.line((0, y, WIDTH, y), fill=(red, green, blue, 255))
    draw.ellipse((-460, -720, 1560, 1300), fill=(28, 136, 214, 34))
    draw.ellipse((1770, 590, 3620, 2440), fill=(111, 73, 220, 30))

    hero = _font(96, bold=True)
    subtitle = _font(46, bold=True)
    date_font = _font(68, bold=True)
    title = _font(54, bold=True)
    body = _font(38)
    small = _font(28)

    date_text = now.strftime("%A · %B %d, %Y").replace(" 0", " ")
    center = WIDTH // 2
    draw.rounded_rectangle((350, 300, WIDTH - 350, HEIGHT - 300), radius=54,
                           fill=(4, 13, 27, 210), outline=(90, 132, 177, 170), width=3)
    draw.text((center, 520), "My Planner", font=hero, anchor="mm", fill=(245, 249, 255, 255))
    draw.text((center, 616), "每日计划", font=subtitle, anchor="mm", fill=(188, 207, 232, 255))
    draw.text((center, 670), date_text, font=date_font, anchor="mm", fill=(124, 204, 255, 255))

    draw.rounded_rectangle((640, 820, WIDTH - 640, 1010), radius=40,
                           fill=(18, 48, 79, 230), outline=(69, 145, 203, 180), width=2)
    draw.ellipse((730, 884, 774, 928), fill=(80, 225, 170, 255))
    draw.text((center + 28, 914), "Mac 已识别到新的一天", font=title, anchor="mm",
              fill=(244, 248, 253, 255))
    draw.text((center, 1160), "正在更新今天的课程与重要消息…", font=title, anchor="mm",
              fill=(239, 244, 252, 255))
    draw.text((center, 1280), "完整简报准备完成后会自动替换这张过渡壁纸", font=body, anchor="mm",
              fill=(170, 192, 218, 255))
    draw.text((center, 1460), "America/New_York · 登录补跑已启动", font=small, anchor="mm",
              fill=(119, 150, 184, 255))
    return image


def ensure_transition_file(now: datetime) -> Path:
    destination = _target_for(now)
    if destination.is_file():
        try:
            with Image.open(destination) as existing:
                if existing.size == (WIDTH, HEIGHT) and existing.mode == "RGB":
                    return destination
        except OSError:
            pass
    _atomic_save_png(render_transition(now), destination)
    return destination


def _request_matches(
    now: datetime,
    *,
    allowed_sources: set[str],
    require_verified_status: bool,
) -> bool:
    request = _read_json(wallpaper_manager.REQUEST)
    status = _read_json(wallpaper_manager.STATUS)
    target = Path(str(request.get("target") or "")).expanduser()
    if (
        request.get("schema_version") != 1
        or request.get("valid_for_date") != now.date().isoformat()
        or request.get("source") not in allowed_sources
        or not target.is_file()
        or request.get("image_sha256") != _sha256(target)
    ):
        return False
    if not require_verified_status:
        return True
    return bool(
        wallpaper_manager.wallpaper_status_complete(status)
        and status.get("request_id") == request.get("request_id")
        and status.get("target") == str(target.resolve())
        and status.get("image_sha256") == request.get("image_sha256")
    )


def apply_login_transition(now: datetime | None = None) -> dict[str, Any]:
    started = datetime.now(TIMEZONE)
    now = (now or started).astimezone(TIMEZONE)

    # A final image may have appeared after the dispatcher's first freshness
    # check. Never paint a transition over a verified final image.
    if _request_matches(now, allowed_sources=FINAL_SOURCES, require_verified_status=True):
        result = {
            "status": "ok",
            "action": "final_already_verified",
            "date": now.date().isoformat(),
            "not_a_completed_briefing": True,
        }
        _atomic_write_json(TRANSITION_STATUS, result)
        return result

    # If today's final request exists but the GUI readback is stale, repairing
    # that request is faster and more useful than showing a transition.
    if _request_matches(now, allowed_sources=FINAL_SOURCES, require_verified_status=False):
        repaired = wallpaper_manager.consume_request(max_age_days=1)
        if repaired.get("status") == "ok":
            result = {
                "status": "ok",
                "action": "final_reapplied",
                "date": now.date().isoformat(),
                "target": repaired.get("target"),
                "current_desktop_verified": repaired.get("current_desktop_verified") is True,
                "all_spaces_verified": repaired.get("all_spaces_verified") is True,
                "lock_screen_source_verified": repaired.get("lock_screen_source_verified") is True,
                "current_desktop_configured": repaired.get("current_desktop_configured") is True,
                "all_spaces_configured": repaired.get("all_spaces_configured") is True,
                "lock_screen_source_configured": repaired.get("lock_screen_source_configured") is True,
                "presentation_verified": repaired.get("presentation_verified") is True,
                "not_a_completed_briefing": True,
            }
            _atomic_write_json(TRANSITION_STATUS, result)
            return result

    destination = ensure_transition_file(now)
    digest = _sha256(destination)
    applied = wallpaper_manager.apply_login_transition_wallpaper(
        destination,
        valid_for_date=now.date().isoformat(),
        source_generated_at=now.isoformat(),
    )
    action = str(applied.get("action") or "transition_pending")
    applied_target = str(applied.get("target") or destination.resolve())
    applied_hash = str(applied.get("image_sha256") or digest)
    result = {
        "status": applied.get("status", "error"),
        "action": action,
        "detected_at": started.isoformat(),
        "date": now.date().isoformat(),
        "target": applied_target,
        "image_sha256": applied_hash,
        "request_id": applied.get("request_id"),
        "current_desktop_verified": applied.get("current_desktop_verified") is True,
        "all_spaces_verified": applied.get("all_spaces_verified") is True,
        "lock_screen_source_verified": applied.get("lock_screen_source_verified") is True,
        "current_desktop_configured": applied.get("current_desktop_configured") is True,
        "all_spaces_configured": applied.get("all_spaces_configured") is True,
        "lock_screen_source_configured": applied.get("lock_screen_source_configured") is True,
        "presentation_verified": applied.get("presentation_verified") is True,
        "elapsed_ms": round((datetime.now(TIMEZONE) - started).total_seconds() * 1000),
        "not_a_completed_briefing": action != "final_preserved",
        "committed_as_last_good": action == "final_preserved",
        "error": applied.get("error"),
    }
    _atomic_write_json(TRANSITION_STATUS, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the transition through wallpaper_manager")
    parser.add_argument("--render-only", type=Path, help="write a PNG without changing wallpaper state")
    parser.add_argument("--now", help="ISO time override; only valid with --render-only")
    args = parser.parse_args()
    if args.apply == bool(args.render_only):
        parser.error("choose exactly one of --apply or --render-only")
    if args.now and args.apply:
        parser.error("--now is only allowed with --render-only")
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)
    now = now.astimezone(TIMEZONE)
    if args.render_only:
        _atomic_save_png(render_transition(now), args.render_only.expanduser().resolve())
        result = {"status": "ok", "action": "rendered_only", "target": str(args.render_only.expanduser().resolve())}
    else:
        result = apply_login_transition(now)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
