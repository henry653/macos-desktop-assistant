#!/usr/bin/env python3
"""Apply a generated briefing through the logged-in macOS GUI session.

macOS turns off "Show on all Spaces" whenever System Events assigns a custom
image.  After using that public setter, this module restores the single
AllSpacesAndDisplays record created by System Settings, backs up the original
store, and verifies the desktop and lock-screen source by reading it back.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import fcntl
import hashlib
import json
import math
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
STORE = Path.home() / "Library/Application Support/com.apple.wallpaper/Store/Index.plist"
REQUEST = ROOT / "wallpaper_request.json"
STATUS = ROOT / "wallpaper_status.json"
TRANSITION_STATUS = ROOT / "wallpaper_transition_status.json"
LAST_GOOD_STATUS = ROOT / "wallpaper_last_good_status.json"
LAST_GOOD_REQUEST = ROOT / "wallpaper_last_good_request.json"
BACKUPS = ROOT / "wallpaper_store_backups"
APPLY_LOCK = ROOT / "wallpaper_apply.lock"
IMAGE_PROVIDER = "com.apple.wallpaper.choice.image"
NEW_YORK = ZoneInfo("America/New_York")
FINAL_SOURCES = {"local_renderer", "cloud_signed_manifest"}
TRANSITION_SOURCE = "login_transition"
VERIFICATION_SCHEMA_VERSION = 2
POST_STORE_RETRY_DELAYS = (1.0, 2.0)
AGENT_RECOVERY_RETRY_DELAYS = (3.0, 5.0, 8.0)
DOCK_RECOVERY_RETRY_DELAYS = (5.0, 8.0)
PRESENTATION_MAX_MEAN_ABSOLUTE_ERROR = 12.0


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _structured_apply_error(
    image_path: Path | None,
    exc: Exception,
    *,
    status_path: Path | None = None,
) -> dict[str, Any]:
    destination = status_path or STATUS
    result = {
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
        "attempted_at": datetime.now(NEW_YORK).isoformat(),
        "checked_at": datetime.now(NEW_YORK).isoformat(),
        "status": "error",
        "target": str(image_path) if image_path is not None else None,
        "current_desktop_verified": False,
        "all_spaces_verified": False,
        "lock_screen_source_verified": False,
        "current_desktop_configured": False,
        "all_spaces_configured": False,
        "lock_screen_source_configured": False,
        "presentation_verified": False,
        "presentation_state": "error",
        "error": f"wallpaper_apply_exception:{type(exc).__name__}",
    }
    _atomic_write_json(destination, result)
    return result


def wallpaper_status_complete(status: dict[str, Any]) -> bool:
    """Return true only for a configuration with a live presentation receipt.

    The legacy ``*_verified`` fields are retained for downstream compatibility,
    but they are not sufficient: older versions only read paths back from
    System Events and Apple's wallpaper store.  A version-2 receipt must also
    prove that Dock's on-screen wallpaper window matches the requested image.
    """
    return bool(
        status.get("verification_schema_version") == VERIFICATION_SCHEMA_VERSION
        and status.get("status") == "ok"
        and status.get("current_desktop_configured") is True
        and status.get("all_spaces_configured") is True
        and status.get("lock_screen_source_configured") is True
        and status.get("presentation_verified") is True
        and status.get("presentation_state") == "verified"
    )


def _record_last_good(request: dict[str, Any], status: dict[str, Any]) -> None:
    if (
        request.get("schema_version") != 1
        or not wallpaper_status_complete(status)
        or request.get("request_id") != status.get("request_id")
    ):
        raise ValueError("cannot commit unverified wallpaper receipt")
    committed_request = dict(request)
    committed_request["defer_last_good"] = False
    _atomic_write_json(LAST_GOOD_REQUEST, committed_request)
    _atomic_write_json(LAST_GOOD_STATUS, status)


def _run_applescript(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/osascript", "-e", script, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _set_current_desktops(image_path: Path) -> dict[str, Any]:
    script = """
on run argv
  set wallpaperFile to POSIX file (item 1 of argv)
  tell application "System Events"
    set desktopCount to count of desktops
    if desktopCount is 0 then error "No active desktops"
    repeat with desktopItem in desktops
      set picture of desktopItem to wallpaperFile
    end repeat
    return desktopCount
  end tell
end run
"""
    result = _run_applescript(script, str(image_path))
    return {
        "ok": result.returncode == 0,
        "desktop_count": int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else None,
        "error": (result.stderr or result.stdout).strip() if result.returncode else None,
    }


def _current_desktop_paths() -> dict[str, Any]:
    result = _run_applescript('tell application "System Events" to get picture of every desktop')
    if result.returncode != 0:
        return {"ok": False, "paths": [], "error": (result.stderr or result.stdout).strip()}
    paths = [item.strip() for item in result.stdout.strip().split(",") if item.strip()]
    return {"ok": True, "paths": paths, "error": None}


def _choice_uri(section: Any) -> str | None:
    if not isinstance(section, dict):
        return None
    choices = ((section.get("Content") or {}).get("Choices") or [])
    for choice in choices:
        if isinstance(choice, dict) and choice.get("Provider") == IMAGE_PROVIDER:
            files = choice.get("Files") or []
            if files and isinstance(files[0], dict):
                return str(files[0].get("relative") or "")
    return None


def _store_readback(image_uri: str) -> dict[str, Any]:
    if not STORE.is_file():
        return {
            "available": False,
            "show_on_all_spaces": False,
            "all_spaces_uri": None,
            "system_default_uri": None,
            "display_uris": [],
            "space_uris": [],
            "all_spaces_configured": False,
            "lock_screen_source_configured": False,
            "all_spaces_verified": False,
            "lock_screen_source_verified": False,
            "error": "wallpaper_store_missing",
        }
    try:
        with STORE.open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        return {
            "available": True,
            "show_on_all_spaces": False,
            "all_spaces_uri": None,
            "system_default_uri": None,
            "display_uris": [],
            "space_uris": [],
            "all_spaces_configured": False,
            "lock_screen_source_configured": False,
            "all_spaces_verified": False,
            "lock_screen_source_verified": False,
            "error": str(exc),
        }

    all_spaces_uri = _choice_uri((value.get("AllSpacesAndDisplays") or {}).get("Desktop"))
    system_default_uri = _choice_uri((value.get("SystemDefault") or {}).get("Desktop"))
    display_uris = [
        uri
        for display in (value.get("Displays") or {}).values()
        if (uri := _choice_uri((display or {}).get("Desktop"))) is not None
    ]
    space_uris: list[str] = []
    for space in (value.get("Spaces") or {}).values():
        default_uri = _choice_uri(((space or {}).get("Default") or {}).get("Desktop"))
        if default_uri is not None:
            space_uris.append(default_uri)
        for display in ((space or {}).get("Displays") or {}).values():
            uri = _choice_uri((display or {}).get("Desktop"))
            if uri is not None:
                space_uris.append(uri)
    show_on_all_spaces = all_spaces_uri is not None
    display_sources_verified = bool(display_uris) and all(uri == image_uri for uri in display_uris)
    space_sources_verified = bool(space_uris) and all(uri == image_uri for uri in space_uris)
    all_spaces_verified = (
        show_on_all_spaces
        and all_spaces_uri == image_uri
        and display_sources_verified
        and space_sources_verified
    )
    # On macOS 15 the lock screen derives from the user's desktop/default
    # records.  The old /Library/Caches/.../lockscreen.png is not authoritative.
    lock_screen_source_verified = (
        system_default_uri == image_uri
        and display_sources_verified
        and all_spaces_uri == image_uri
    )
    return {
        "available": True,
        "show_on_all_spaces": show_on_all_spaces,
        "all_spaces_uri": all_spaces_uri,
        "system_default_uri": system_default_uri,
        "display_uris": display_uris,
        "space_uris": space_uris,
        "display_sources_verified": display_sources_verified,
        "space_sources_verified": space_sources_verified,
        "all_spaces_configured": all_spaces_verified,
        "lock_screen_source_configured": lock_screen_source_verified,
        # Legacy metadata aliases. They do not prove visible presentation.
        "all_spaces_verified": all_spaces_verified,
        "lock_screen_source_verified": lock_screen_source_verified,
        "error": None,
    }


def _set_choice_uri(section: dict[str, Any], image_uri: str) -> bool:
    choices = ((section.get("Content") or {}).get("Choices") or [])
    for choice in choices:
        if isinstance(choice, dict) and choice.get("Provider") == IMAGE_PROVIDER:
            choice["Files"] = [{"relative": image_uri}]
            return True
    return False


def _write_plist_atomically(path: Path, value: dict[str, Any], file_mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".Index-", suffix=".plist", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(value, handle, fmt=plistlib.FMT_BINARY, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, file_mode)
        with temporary_path.open("rb") as handle:
            verified = plistlib.load(handle)
        if not isinstance(verified, dict):
            raise ValueError("updated wallpaper store failed validation")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sync_all_spaces_record(image_uri: str) -> dict[str, Any]:
    """Mirror macOS's Show on all Spaces setting and all desktop sources.

    AppleScript turns that setting off whenever it assigns a custom file.  The
    public API has no all-Spaces flag, so we restore the backing
    AllSpacesAndDisplays/Desktop record that System Settings itself creates.
    We also update the existing system-default, display, and Mission Control
    Space desktop records so they cannot later pull the machine back to an old
    image.  The complete store is backed up before the atomic update.
    """
    if not STORE.is_file():
        return {"ok": False, "error": "wallpaper_store_missing", "backup": None}
    file_mode = stat.S_IMODE(STORE.stat().st_mode)
    with STORE.open("rb") as handle:
        value = plistlib.load(handle)
    if not isinstance(value, dict):
        return {"ok": False, "error": "invalid_wallpaper_store", "backup": None}

    all_spaces = value.setdefault("AllSpacesAndDisplays", {})
    desktop = all_spaces.get("Desktop")
    if not isinstance(desktop, dict):
        desktop = copy.deepcopy((value.get("SystemDefault") or {}).get("Desktop"))
    if not isinstance(desktop, dict):
        for display in (value.get("Displays") or {}).values():
            candidate = (display or {}).get("Desktop")
            if isinstance(candidate, dict):
                desktop = copy.deepcopy(candidate)
                break
    if not isinstance(desktop, dict) or not _set_choice_uri(desktop, image_uri):
        return {"ok": False, "error": "no_image_desktop_template", "backup": None}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    def update_desktop(target: Any) -> bool:
        if not isinstance(target, dict) or not _set_choice_uri(target, image_uri):
            return False
        target["LastSet"] = now
        target["LastUse"] = now
        return True

    update_desktop(desktop)
    all_spaces["Desktop"] = desktop

    updated_records = 1
    system_default = value.get("SystemDefault") or {}
    if update_desktop(system_default.get("Desktop")):
        updated_records += 1
    for display in (value.get("Displays") or {}).values():
        if update_desktop((display or {}).get("Desktop")):
            updated_records += 1
    for space in (value.get("Spaces") or {}).values():
        if update_desktop(((space or {}).get("Default") or {}).get("Desktop")):
            updated_records += 1
        for display in ((space or {}).get("Displays") or {}).values():
            if update_desktop((display or {}).get("Desktop")):
                updated_records += 1

    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUPS / f"Index_{stamp}.plist"
    shutil.copy2(STORE, backup)
    old_backups = sorted(BACKUPS.glob("Index_*.plist"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in old_backups[7:]:
        old.unlink(missing_ok=True)

    _write_plist_atomically(STORE, value, file_mode)
    return {
        "ok": True,
        "error": None,
        "backup": str(backup),
        "updated_records": updated_records,
    }


def _boot_session_uuid() -> str | None:
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


def _list_onscreen_wallpaper_windows() -> dict[str, Any]:
    """Read Dock's live wallpaper windows without capturing other apps."""
    try:
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        core_graphics.CGPreflightScreenCaptureAccess.argtypes = []
        core_graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        # CGPreflightScreenCaptureAccess can report false for a launchd child
        # even when the allow-listed /usr/sbin/screencapture helper can capture
        # a specific Dock wallpaper window.  Treat it as diagnostic metadata,
        # not proof that capture is impossible; the actual wallpaper-only
        # capture below is authoritative.
        preflight_authorized = bool(core_graphics.CGPreflightScreenCaptureAccess())

        core_graphics.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        core_graphics.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        core_foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        core_foundation.CFArrayGetCount.restype = ctypes.c_long
        core_foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        core_foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        core_foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
        core_foundation.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        core_foundation.CFStringGetCString.restype = ctypes.c_bool
        core_foundation.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        core_foundation.CFNumberGetValue.restype = ctypes.c_bool
        core_foundation.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        core_foundation.CFBooleanGetValue.restype = ctypes.c_bool
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

        keys = {
            name: ctypes.c_void_p.in_dll(core_graphics, name).value
            for name in (
                "kCGWindowOwnerName",
                "kCGWindowName",
                "kCGWindowLayer",
                "kCGWindowNumber",
                "kCGWindowIsOnscreen",
            )
        }

        def dictionary_value(dictionary: int, key: str) -> int | None:
            value = core_foundation.CFDictionaryGetValue(dictionary, keys[key])
            return int(value) if value else None

        def string_value(value: int | None) -> str:
            if not value:
                return ""
            buffer = ctypes.create_string_buffer(4096)
            copied = core_foundation.CFStringGetCString(
                value,
                buffer,
                len(buffer),
                0x08000100,  # kCFStringEncodingUTF8
            )
            return buffer.value.decode("utf-8", "replace") if copied else ""

        def number_value(value: int | None) -> int | None:
            if not value:
                return None
            number = ctypes.c_long()
            copied = core_foundation.CFNumberGetValue(
                value,
                4,  # kCFNumberSInt64Type
                ctypes.byref(number),
            )
            return int(number.value) if copied else None

        array = core_graphics.CGWindowListCopyWindowInfo(0, 0)
        if not array:
            return {
                "ok": False,
                "windows": [],
                "screen_capture_preflight_authorized": preflight_authorized,
                "error": "window_list_unavailable",
            }
        windows: list[dict[str, Any]] = []
        try:
            for index in range(core_foundation.CFArrayGetCount(array)):
                dictionary = core_foundation.CFArrayGetValueAtIndex(array, index)
                name = string_value(dictionary_value(dictionary, "kCGWindowName"))
                onscreen_value = dictionary_value(dictionary, "kCGWindowIsOnscreen")
                is_onscreen = bool(
                    onscreen_value
                    and core_foundation.CFBooleanGetValue(onscreen_value)
                )
                window_id = number_value(dictionary_value(dictionary, "kCGWindowNumber"))
                if not is_onscreen or not name.startswith("Wallpaper-") or not window_id:
                    continue
                windows.append({
                    "window_id": window_id,
                    "owner": string_value(dictionary_value(dictionary, "kCGWindowOwnerName")),
                    "name": name,
                    "layer": number_value(dictionary_value(dictionary, "kCGWindowLayer")),
                })
        finally:
            core_foundation.CFRelease(array)
        if not windows:
            return {
                "ok": False,
                "windows": [],
                "screen_capture_preflight_authorized": preflight_authorized,
                "error": "no_onscreen_wallpaper_window",
            }
        windows.sort(key=lambda item: int(item["window_id"]))
        return {
            "ok": True,
            "windows": windows,
            "screen_capture_preflight_authorized": preflight_authorized,
            "error": None,
        }
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "windows": [],
            "screen_capture_preflight_authorized": None,
            "error": f"wallpaper_window_inspection_unavailable:{type(exc).__name__}",
        }


def _presentation_metrics(target_path: Path, captured_path: Path) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as exc:
        return {
            "verified": False,
            "error": f"presentation_dependencies_unavailable:{type(exc).__name__}",
        }
    try:
        with Image.open(target_path) as target_source, Image.open(captured_path) as captured_source:
            target = target_source.convert("RGB")
            captured = captured_source.convert("RGB")
            width, height = captured.size
            if width <= 0 or height <= 0:
                raise ValueError("empty wallpaper capture")

            candidates = [target.resize(captured.size, Image.Resampling.LANCZOS)]
            fitted = ImageOps.fit(
                target,
                captured.size,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            if fitted.tobytes() != candidates[0].tobytes():
                candidates.append(fitted)

            sample_width = min(512, width)
            sample_height = max(1, round(height * sample_width / width))
            captured_gray = ImageOps.grayscale(captured).resize(
                (sample_width, sample_height), Image.Resampling.LANCZOS
            )
            captured_array = np.asarray(captured_gray, dtype=np.float32)
            captured_edges = np.concatenate((
                np.diff(captured_array, axis=0).ravel(),
                np.diff(captured_array, axis=1).ravel(),
            ))

            best: dict[str, Any] | None = None
            for candidate in candidates:
                candidate_gray = ImageOps.grayscale(candidate).resize(
                    (sample_width, sample_height), Image.Resampling.LANCZOS
                )
                candidate_array = np.asarray(candidate_gray, dtype=np.float32)
                candidate_edges = np.concatenate((
                    np.diff(candidate_array, axis=0).ravel(),
                    np.diff(candidate_array, axis=1).ravel(),
                ))
                edge_std = float(np.std(candidate_edges))
                captured_std = float(np.std(captured_edges))
                if edge_std < 1e-6 or captured_std < 1e-6:
                    edge_correlation = 1.0 if np.array_equal(candidate_array, captured_array) else 0.0
                else:
                    edge_correlation = float(np.corrcoef(candidate_edges, captured_edges)[0, 1])
                    if not math.isfinite(edge_correlation):
                        edge_correlation = 0.0
                absolute_difference = np.abs(candidate_array - captured_array)
                mean_absolute_error = float(np.mean(absolute_difference))
                within_ten = float(np.mean(absolute_difference <= 10.0))
                metrics = {
                    "edge_correlation": round(edge_correlation, 6),
                    "mean_absolute_error": round(mean_absolute_error, 3),
                    "within_ten_fraction": round(within_ten, 6),
                }
                if best is None or metrics["edge_correlation"] > best["edge_correlation"]:
                    best = metrics

            assert best is not None
            verified = bool(
                best["edge_correlation"] >= 0.995
                and best["within_ten_fraction"] >= 0.98
                and best["mean_absolute_error"] <= PRESENTATION_MAX_MEAN_ABSOLUTE_ERROR
            )
            return {
                "verified": verified,
                "capture_size": [width, height],
                "target_size": list(target.size),
                **best,
                "error": None if verified else "presentation_mismatch",
            }
    except (OSError, ValueError) as exc:
        return {
            "verified": False,
            "error": f"presentation_compare_failed:{type(exc).__name__}",
        }


def _verify_desktop_presentation(image_path: Path) -> dict[str, Any]:
    window_readback = _list_onscreen_wallpaper_windows()
    preflight_authorized = window_readback.get("screen_capture_preflight_authorized")
    if not window_readback.get("ok"):
        error = window_readback.get("error")
        if preflight_authorized is False:
            error = "screen_recording_permission_required"
        return {
            "verified": False,
            "state": "indeterminate",
            "method": "dock_wallpaper_window_capture",
            "windows": [],
            "screen_capture_preflight_authorized": preflight_authorized,
            "error": error,
        }

    results: list[dict[str, Any]] = []
    for window in window_readback["windows"]:
        fd, temporary = tempfile.mkstemp(prefix="daily-briefing-wallpaper-", suffix=".png")
        os.close(fd)
        temporary_path = Path(temporary)
        temporary_path.unlink(missing_ok=True)
        try:
            try:
                capture = subprocess.run(
                    [
                        "/usr/sbin/screencapture",
                        "-x",
                        "-l",
                        str(window["window_id"]),
                        str(temporary_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append({
                    **window,
                    "capture_succeeded": False,
                    "verified": False,
                    "error": f"wallpaper_capture_failed:{type(exc).__name__}",
                })
                continue
            if capture.returncode != 0 or not temporary_path.is_file():
                results.append({
                    **window,
                    "capture_succeeded": False,
                    "verified": False,
                    "error": "wallpaper_capture_failed",
                })
                continue
            results.append({
                **window,
                "capture_succeeded": True,
                **_presentation_metrics(image_path, temporary_path),
            })
        finally:
            # The capture contains only Dock's wallpaper layer and is never retained.
            temporary_path.unlink(missing_ok=True)

    captured_any = any(result.get("capture_succeeded") is True for result in results)
    verified = bool(results) and all(result.get("verified") is True for result in results)
    if verified:
        error = None
        state = "verified"
    elif not captured_any and preflight_authorized is False:
        error = "screen_recording_permission_required"
        state = "indeterminate"
    else:
        error = next(
            (str(result.get("error")) for result in results if result.get("error")),
            "presentation_mismatch",
        )
        state = "mismatch" if captured_any else "indeterminate"
    return {
        "verified": verified,
        "state": state,
        "method": "dock_wallpaper_window_capture",
        "active_display_count": len(results),
        "windows": results,
        "screen_capture_preflight_authorized": preflight_authorized,
        "checked_at": datetime.now(NEW_YORK).isoformat(),
        "privacy": "wallpaper_only_capture_deleted_immediately",
        "error": error,
    }


def _wait_for_presentation(
    image_path: Path,
    delays: tuple[float, ...],
    *,
    reapply_between_attempts: bool,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    reapply_results: list[dict[str, Any]] = []
    final: dict[str, Any] = {
        "verified": False,
        "state": "indeterminate",
        "method": "dock_wallpaper_window_capture",
        "windows": [],
        "error": "presentation_not_checked",
    }
    for index, delay in enumerate(delays, start=1):
        time.sleep(delay)
        final = _verify_desktop_presentation(image_path)
        attempts.append({
            "attempt": index,
            "delay_seconds": delay,
            "verified": final.get("verified") is True,
            "state": final.get("state"),
            "error": final.get("error"),
        })
        if final.get("verified") is True:
            break
        if (
            reapply_between_attempts
            and index < len(delays)
            and final.get("error") != "screen_recording_permission_required"
        ):
            reapply_results.append(_set_current_desktops(image_path))
    final = dict(final)
    final["attempts"] = attempts
    final["reapply_results"] = reapply_results
    return final


def _restart_wallpaper_process(process_name: str) -> dict[str, Any]:
    if process_name not in {"WallpaperAgent", "Dock"}:
        return {"ok": False, "process": process_name, "error": "process_not_allowlisted"}
    try:
        result = subprocess.run(
            ["/usr/bin/killall", process_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "process": process_name,
            "error": f"process_restart_failed:{type(exc).__name__}",
        }
    return {
        "ok": result.returncode == 0,
        "process": process_name,
        "error": None if result.returncode == 0 else "process_not_running_or_restart_denied",
    }


def _already_verified(
    image_path: Path,
    image_sha256: str,
    request_receipt: dict[str, Any] | None = None,
    *,
    status_path: Path | None = None,
) -> dict[str, Any] | None:
    status = _read_json(status_path or STATUS)
    if status.get("target") != str(image_path):
        return None
    if status.get("image_sha256") != image_sha256:
        return None
    if not wallpaper_status_complete(status):
        return None
    current = _current_desktop_paths()
    current_configured = (
        current.get("ok")
        and bool(current.get("paths"))
        and all(Path(path).expanduser().resolve() == image_path for path in current["paths"])
    )
    store = _store_readback(image_path.as_uri())
    presentation = _verify_desktop_presentation(image_path)
    if (
        current_configured
        and store.get("all_spaces_configured")
        and store.get("lock_screen_source_configured")
        and presentation.get("verified") is True
    ):
        status["checked_at"] = datetime.now(NEW_YORK).isoformat()
        status["current_readback"] = current
        status["store"] = store
        status["boot_session_uuid"] = _boot_session_uuid()
        status["current_desktop_configured"] = True
        status["all_spaces_configured"] = True
        status["lock_screen_source_configured"] = True
        status["configuration_verified"] = True
        status["presentation"] = presentation
        status["presentation_verified"] = True
        status["presentation_state"] = "verified"
        status["presentation_verified_at"] = presentation.get("checked_at")
        # Compatibility fields now have explicit meanings: current is visual;
        # all-Spaces and lock-screen source are configuration readbacks only.
        status["current_desktop_verified"] = True
        status["all_spaces_verified"] = True
        status["lock_screen_source_verified"] = True
        status["error"] = None
        if request_receipt is not None:
            status.update({
                "request_id": request_receipt.get("request_id"),
                "valid_for_date": request_receipt.get("valid_for_date"),
                "source": request_receipt.get("source"),
                "source_generated_at": request_receipt.get("source_generated_at"),
                "manifest_signature_verified": request_receipt.get("manifest_signature_verified") is True,
            })
        return status
    return None


def _apply_wallpaper_locked(
    image_path: Path,
    request_receipt: dict[str, Any] | None = None,
    *,
    status_path: Path | None = None,
) -> dict[str, Any]:
    destination = status_path or STATUS
    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Wallpaper image not found: {image_path}")
    image_sha256 = _file_sha256(image_path)
    cached = _already_verified(
        image_path,
        image_sha256,
        request_receipt,
        status_path=destination,
    )
    if cached is not None:
        _atomic_write_json(destination, cached)
        return cached

    attempted_at = datetime.now(NEW_YORK).isoformat()
    previous = _read_json(destination)
    same_previous_target = bool(
        previous.get("target") == str(image_path)
        and previous.get("image_sha256") == image_sha256
    )
    attempt_count = int(previous.get("attempt_count") or 0) + 1 if same_previous_target else 1
    previous_agent_restarts = (
        int(previous.get("wallpaper_agent_restart_count") or 0)
        if same_previous_target
        else 0
    )
    previous_dock_restarts = (
        int(previous.get("dock_restart_count") or 0)
        if same_previous_target
        else 0
    )
    setter = _set_current_desktops(image_path)
    if setter.get("ok"):
        # Let WallpaperAgent finish its legacy setter transaction before the
        # all-Spaces record is updated. The subsequent live receipt is the
        # authority; merely reading this store back is never considered enough.
        time.sleep(0.5)
    all_spaces_sync = _sync_all_spaces_record(image_path.as_uri()) if setter.get("ok") else {
        "ok": False,
        "error": setter.get("error") or "desktop_set_failed",
        "backup": None,
    }
    presentation = (
        _wait_for_presentation(
            image_path,
            POST_STORE_RETRY_DELAYS,
            reapply_between_attempts=False,
        )
        if all_spaces_sync.get("ok")
        else {
            "verified": False,
            "state": "indeterminate",
            "method": "dock_wallpaper_window_capture",
            "windows": [],
            "error": all_spaces_sync.get("error") or "wallpaper_configuration_not_ready",
            "attempts": [],
            "reapply_results": [],
        }
    )

    wallpaper_agent_restart: dict[str, Any] | None = None
    dock_restart: dict[str, Any] | None = None
    wallpaper_agent_restart_count = previous_agent_restarts
    dock_restart_count = previous_dock_restarts
    if (
        all_spaces_sync.get("ok")
        and presentation.get("verified") is not True
        and presentation.get("error") != "screen_recording_permission_required"
        and previous_agent_restarts < 2
    ):
        wallpaper_agent_restart = _restart_wallpaper_process("WallpaperAgent")
        if wallpaper_agent_restart.get("ok"):
            wallpaper_agent_restart_count += 1
            presentation = _wait_for_presentation(
                image_path,
                AGENT_RECOVERY_RETRY_DELAYS,
                reapply_between_attempts=False,
            )
    if (
        all_spaces_sync.get("ok")
        and presentation.get("verified") is not True
        and presentation.get("error") != "screen_recording_permission_required"
        and previous_dock_restarts < 1
    ):
        dock_restart = _restart_wallpaper_process("Dock")
        if dock_restart.get("ok"):
            dock_restart_count += 1
            presentation = _wait_for_presentation(
                image_path,
                DOCK_RECOVERY_RETRY_DELAYS,
                reapply_between_attempts=False,
            )

    current = _current_desktop_paths()
    current_configured = (
        current.get("ok")
        and bool(current.get("paths"))
        and all(Path(path).expanduser().resolve() == image_path for path in current["paths"])
    )
    store = _store_readback(image_path.as_uri())
    all_spaces_configured = bool(store.get("all_spaces_configured"))
    lock_screen_source_configured = bool(store.get("lock_screen_source_configured"))
    configuration_verified = bool(
        current_configured and all_spaces_configured and lock_screen_source_configured
    )
    presentation_verified = presentation.get("verified") is True

    if configuration_verified and presentation_verified:
        status_name = "ok"
        error = None
    elif not setter.get("ok") or not current.get("ok"):
        status_name = "pending_gui_session"
        error = setter.get("error") or current.get("error")
    elif presentation.get("error") == "screen_recording_permission_required":
        status_name = "pending_presentation_permission"
        error = "screen_recording_permission_required"
    elif not presentation_verified:
        status_name = "pending_render_refresh"
        error = presentation.get("error") or "desktop_presentation_not_verified"
    elif not all_spaces_sync.get("ok") or not configuration_verified:
        status_name = "pending_configuration_refresh"
        error = all_spaces_sync.get("error") or "wallpaper_configuration_not_verified"
    else:
        status_name = "pending_render_refresh"
        error = "wallpaper_not_fully_verified"

    result = {
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
        "attempted_at": attempted_at,
        "checked_at": datetime.now(NEW_YORK).isoformat(),
        "status": status_name,
        "target": str(image_path),
        "image_sha256": image_sha256,
        "attempt_count": attempt_count,
        "wallpaper_agent_restart_count": wallpaper_agent_restart_count,
        "dock_restart_count": dock_restart_count,
        "boot_session_uuid": _boot_session_uuid(),
        "setter": setter,
        "all_spaces_sync": all_spaces_sync,
        "wallpaper_agent_restart": wallpaper_agent_restart,
        "dock_restart": dock_restart,
        "current_readback": current,
        "current_desktop_configured": bool(current_configured),
        "all_spaces_configured": all_spaces_configured,
        "lock_screen_source_configured": lock_screen_source_configured,
        "configuration_verified": configuration_verified,
        "presentation": presentation,
        "presentation_verified": presentation_verified,
        "presentation_state": "verified" if presentation_verified else str(presentation.get("state") or "indeterminate"),
        "presentation_verified_at": presentation.get("checked_at") if presentation_verified else None,
        # Compatibility output. Only current_desktop_verified is a visual proof.
        "current_desktop_verified": presentation_verified,
        "all_spaces_verified": all_spaces_configured,
        "lock_screen_source_verified": lock_screen_source_configured,
        "error": error,
    }
    if status_name != "ok":
        result["next_retry_at"] = (
            datetime.now(NEW_YORK) + timedelta(seconds=60)
        ).isoformat()
    if request_receipt is not None:
        result.update({
            "request_id": request_receipt.get("request_id"),
            "valid_for_date": request_receipt.get("valid_for_date"),
            "source": request_receipt.get("source"),
            "source_generated_at": request_receipt.get("source_generated_at"),
            "manifest_signature_verified": request_receipt.get("manifest_signature_verified") is True,
        })
    _atomic_write_json(destination, result)
    return result

def apply_wallpaper(image_path: Path) -> dict[str, Any]:
    """Serialize renderer and LaunchAgent attempts against the wallpaper store."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _apply_wallpaper_locked(image_path)
        except Exception as exc:
            return _structured_apply_error(image_path, exc)


def request_wallpaper_update(
    image_path: Path,
    *,
    valid_for_date: str | None = None,
    source: str = "local_renderer",
    source_generated_at: str | None = None,
    manifest_signature_verified: bool = False,
    commit_last_good: bool = True,
) -> dict[str, Any]:
    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Wallpaper image not found: {image_path}")
    now = datetime.now(NEW_YORK)
    requested_date = valid_for_date or now.date().isoformat()
    try:
        parsed_date = datetime.strptime(requested_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid wallpaper valid_for_date") from exc
    if parsed_date != now.date():
        raise ValueError("wallpaper request is not valid for today in America/New_York")
    if source_generated_at:
        generated = datetime.fromisoformat(source_generated_at.replace("Z", "+00:00"))
        if generated.tzinfo is None or generated.astimezone(NEW_YORK).date() != parsed_date:
            raise ValueError("wallpaper source_generated_at does not match valid_for_date")
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        request = {
            "schema_version": 1,
            "request_id": uuid.uuid4().hex,
            "requested_at": now.isoformat(),
            "valid_for_date": requested_date,
            "target": str(image_path),
            "image_sha256": _file_sha256(image_path),
            "source": source,
            "source_generated_at": source_generated_at,
            "manifest_signature_verified": manifest_signature_verified is True,
            "defer_last_good": not commit_last_good,
        }
        _atomic_write_json(REQUEST, request)
        try:
            result = _apply_wallpaper_locked(image_path, request)
            if result.get("status") == "ok" and commit_last_good:
                _record_last_good(request, result)
            return result
        except Exception as exc:
            result = _structured_apply_error(image_path, exc)
            result.update({
                "request_id": request["request_id"],
                "valid_for_date": request["valid_for_date"],
                "source": request["source"],
                "source_generated_at": request["source_generated_at"],
                "manifest_signature_verified": request["manifest_signature_verified"],
            })
            _atomic_write_json(STATUS, result)
            return result


def apply_login_transition_wallpaper(
    image_path: Path,
    *,
    valid_for_date: str,
    source_generated_at: str,
) -> dict[str, Any]:
    """Apply a temporary login image without touching the final receipt.

    The final-request check and the transition write happen under the same
    wallpaper lock. If a cloud or local final request wins the race, that
    image is applied instead and its receipt remains authoritative. Transition
    readback is written only to TRANSITION_STATUS; REQUEST, STATUS and both
    last-good files remain the source of truth for completed briefings.
    """
    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Wallpaper image not found: {image_path}")
    now = datetime.now(NEW_YORK)
    try:
        parsed_date = datetime.strptime(valid_for_date, "%Y-%m-%d").date()
        generated = datetime.fromisoformat(source_generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid login-transition provenance") from exc
    if generated.tzinfo is None:
        raise ValueError("login-transition source_generated_at must include timezone")
    if parsed_date != now.date() or generated.astimezone(NEW_YORK).date() != parsed_date:
        raise ValueError("login transition is not valid for today in America/New_York")

    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        current_request = _read_json(REQUEST)
        current_target = Path(str(current_request.get("target") or "")).expanduser().resolve()
        current_hash = current_request.get("image_sha256")
        current_is_valid = bool(
            current_request.get("schema_version") == 1
            and current_request.get("valid_for_date") == valid_for_date
            and current_target.is_file()
            and isinstance(current_hash, str)
            and _file_sha256(current_target) == current_hash
        )

        # The final renderer and this transition share APPLY_LOCK. Checking and
        # physically applying here closes the check/write race.
        if current_is_valid and current_request.get("source") in FINAL_SOURCES:
            try:
                result = _apply_wallpaper_locked(
                    current_target,
                    current_request,
                    status_path=STATUS,
                )
            except Exception as exc:
                return _structured_apply_error(current_target, exc)
            if result.get("status") == "ok" and current_request.get("defer_last_good") is not True:
                _record_last_good(current_request, result)
                result = dict(result)
                result.update({
                    "action": "final_preserved",
                    "artifact_kind": "final",
                    "completion_eligible": True,
                })
                return result

        transition_hash = _file_sha256(image_path)
        existing_transition = _read_json(TRANSITION_STATUS)
        existing_matches = bool(
            existing_transition.get("valid_for_date") == valid_for_date
            and existing_transition.get("source") == TRANSITION_SOURCE
            and existing_transition.get("target") == str(image_path)
            and existing_transition.get("image_sha256") == transition_hash
            and isinstance(existing_transition.get("request_id"), str)
        )
        transition_receipt = {
            "schema_version": 1,
            "request_id": existing_transition.get("request_id") if existing_matches else uuid.uuid4().hex,
            "requested_at": existing_transition.get("requested_at") if existing_matches else now.isoformat(),
            "valid_for_date": valid_for_date,
            "target": str(image_path),
            "image_sha256": transition_hash,
            "source": TRANSITION_SOURCE,
            "source_generated_at": (
                existing_transition.get("source_generated_at")
                if existing_matches
                else source_generated_at
            ),
            "manifest_signature_verified": False,
            "defer_last_good": True,
            "artifact_kind": "transition",
            "completion_eligible": False,
        }
        try:
            result = _apply_wallpaper_locked(
                image_path,
                transition_receipt,
                status_path=TRANSITION_STATUS,
            )
        except Exception as exc:
            result = _structured_apply_error(
                image_path,
                exc,
                status_path=TRANSITION_STATUS,
            )
            result.update({
                "request_id": transition_receipt["request_id"],
                "requested_at": transition_receipt["requested_at"],
                "valid_for_date": transition_receipt["valid_for_date"],
                "source": transition_receipt["source"],
                "source_generated_at": transition_receipt["source_generated_at"],
                "manifest_signature_verified": False,
                "artifact_kind": "transition",
                "completion_eligible": False,
            })
            _atomic_write_json(TRANSITION_STATUS, result)
        result = dict(result)
        result.update({
            "requested_at": transition_receipt["requested_at"],
            "artifact_kind": "transition",
            "completion_eligible": False,
            "action": (
                "transition_already_present"
                if existing_matches and result.get("status") == "ok"
                else "transition_applied"
                if result.get("status") == "ok"
                else "transition_pending"
            ),
        })
        _atomic_write_json(TRANSITION_STATUS, result)
        return result


def consume_request(max_age_days: int = 7) -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        request = _read_json(REQUEST)
        target = request.get("target")
        requested_at = request.get("requested_at")
        if not target or not requested_at:
            return {"status": "idle", "reason": "no_wallpaper_request"}
        try:
            requested = datetime.fromisoformat(str(requested_at))
        except ValueError:
            return {"status": "error", "reason": "invalid_request_timestamp"}
        now = datetime.now(NEW_YORK)
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=now.tzinfo)
        if request.get("schema_version") != 1 or not isinstance(request.get("request_id"), str):
            return {"status": "idle", "reason": "legacy_wallpaper_request", "target": str(target)}
        valid_for_date = request.get("valid_for_date")
        if not isinstance(valid_for_date, str):
            return {"status": "idle", "reason": "legacy_wallpaper_request", "target": str(target)}
        if requested - now > timedelta(minutes=5):
            return {"status": "error", "reason": "wallpaper_request_from_future", "target": str(target)}
        if valid_for_date != now.date().isoformat() or now - requested > timedelta(days=max_age_days):
            return {"status": "idle", "reason": "stale_wallpaper_request", "target": str(target)}
        image_path = Path(str(target)).expanduser().resolve()
        if not image_path.is_file():
            return {"status": "error", "reason": "wallpaper_file_missing", "target": str(target)}
        expected_hash = request.get("image_sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            return {"status": "error", "reason": "wallpaper_request_hash_missing", "target": str(target)}
        if _file_sha256(image_path) != expected_hash:
            return {"status": "error", "reason": "wallpaper_request_hash_mismatch", "target": str(target)}
        try:
            result = _apply_wallpaper_locked(image_path, request)
            if result.get("status") == "ok" and request.get("defer_last_good") is not True:
                _record_last_good(request, result)
            return result
        except Exception as exc:
            return _structured_apply_error(image_path, exc)


def commit_current_as_last_good() -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        request = _read_json(REQUEST)
        status = _read_json(STATUS)
        target = Path(str(request.get("target") or "")).expanduser().resolve()
        expected_hash = request.get("image_sha256")
        if (
            request.get("schema_version") != 1
            or not target.is_file()
            or not isinstance(expected_hash, str)
            or _file_sha256(target) != expected_hash
            or status.get("target") != str(target)
            or status.get("image_sha256") != expected_hash
            or not wallpaper_status_complete(status)
        ):
            return {"status": "error", "reason": "current_wallpaper_not_committable"}
        request["defer_last_good"] = False
        _atomic_write_json(REQUEST, request)
        _record_last_good(request, status)
        committed = dict(status)
        committed["committed_as_last_good"] = True
        return committed


def _rollback_last_good_locked() -> dict[str, Any]:
    """Restore the committed wallpaper while the caller holds APPLY_LOCK."""
    request = _read_json(LAST_GOOD_REQUEST)
    target = Path(str(request.get("target") or "")).expanduser().resolve()
    expected_hash = request.get("image_sha256")
    if (
        request.get("schema_version") != 1
        or not target.is_file()
        or not isinstance(expected_hash, str)
        or _file_sha256(target) != expected_hash
    ):
        return {"status": "error", "reason": "last_good_wallpaper_unavailable"}
    request["defer_last_good"] = False
    _atomic_write_json(REQUEST, request)
    try:
        result = _apply_wallpaper_locked(target, request)
    except Exception as exc:
        return _structured_apply_error(target, exc)
    if result.get("status") == "ok":
        _record_last_good(request, result)
        result = dict(result)
        result["action"] = "last_good_restored"
    return result


def rollback_last_good() -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _rollback_last_good_locked()


def _receipt_time(value: dict[str, Any]) -> datetime | None:
    raw = value.get("presentation_verified_at") or value.get("checked_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NEW_YORK)
    return parsed.astimezone(NEW_YORK)


def rollback_login_transition() -> dict[str, Any]:
    """Atomically roll back an active transition, never a newer final image.

    A collector can finish between the dispatcher's failure check and this
    operation. The final-request check therefore runs while APPLY_LOCK is held;
    a same-day final artifact always wins that race.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    with APPLY_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        now = datetime.now(NEW_YORK)
        transition = _read_json(TRANSITION_STATUS)
        transition_checked = _receipt_time(transition)
        if (
            transition.get("source") != TRANSITION_SOURCE
            or transition.get("valid_for_date") != now.date().isoformat()
            or transition_checked is None
        ):
            return {"status": "idle", "reason": "transition_not_active"}

        current_request = _read_json(REQUEST)
        current_target = Path(str(current_request.get("target") or "")).expanduser().resolve()
        current_hash = current_request.get("image_sha256")
        current_final_is_valid = bool(
            current_request.get("schema_version") == 1
            and current_request.get("source") in FINAL_SOURCES
            and current_request.get("valid_for_date") == now.date().isoformat()
            and current_target.is_file()
            and isinstance(current_hash, str)
            and _file_sha256(current_target) == current_hash
        )
        if current_final_is_valid:
            try:
                result = _apply_wallpaper_locked(current_target, current_request)
            except Exception as exc:
                return _structured_apply_error(current_target, exc)
            if result.get("status") == "ok" and current_request.get("defer_last_good") is not True:
                _record_last_good(current_request, result)
            result = dict(result)
            result["action"] = "newer_final_preserved"
            return result

        final_status = _read_json(STATUS)
        final_checked = _receipt_time(final_status)
        if final_checked is not None and final_checked >= transition_checked:
            return {"status": "idle", "reason": "transition_not_active"}

        return _rollback_last_good_locked()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path)
    parser.add_argument("--consume-request", action="store_true")
    parser.add_argument("--request", type=Path, help="record a dated, hashed request and apply it")
    parser.add_argument("--valid-for-date")
    parser.add_argument("--request-source", default="local_renderer")
    parser.add_argument("--source-generated-at")
    parser.add_argument("--manifest-signature-verified", action="store_true")
    parser.add_argument("--defer-last-good", action="store_true")
    parser.add_argument("--commit-current-as-last-good", action="store_true")
    parser.add_argument("--rollback-last-good", action="store_true")
    parser.add_argument("--rollback-login-transition", action="store_true")
    args = parser.parse_args()
    if args.rollback_login_transition:
        result = rollback_login_transition()
    elif args.commit_current_as_last_good:
        result = commit_current_as_last_good()
    elif args.rollback_last_good:
        result = rollback_last_good()
    elif args.consume_request:
        result = consume_request()
    elif args.request is not None:
        result = request_wallpaper_update(
            args.request,
            valid_for_date=args.valid_for_date,
            source=args.request_source,
            source_generated_at=args.source_generated_at,
            manifest_signature_verified=args.manifest_signature_verified,
            commit_last_good=not args.defer_last_good,
        )
    elif args.image is not None:
        result = apply_wallpaper(args.image)
    else:
        parser.error("provide IMAGE or --consume-request")
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
