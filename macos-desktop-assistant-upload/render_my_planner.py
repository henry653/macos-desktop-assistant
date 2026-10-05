#!/usr/bin/env python3
"""Render a private, fully local, clickable My Planner from daily_briefing.json."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
from intern_preparation import strict_display_items


ROOT = Path.home() / "Library/Application Support/Codex/Daily Briefing"
DEFAULT_INPUT = ROOT / "daily_briefing.json"
DEFAULT_OUTPUT = Path.home() / "Desktop/My_Planner.html"
DEFAULT_STATUS = ROOT / "my_planner_status.json"
ZONE = ZoneInfo("America/New_York")

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)")
MONEY_RE = re.compile(r"(?<!\w)[$€£]\s?\d[\d,.]*(?:\.\d{2})?")
GRADE_RE = re.compile(r"(?i)(?:grade|score|成绩|分数)\s*[:：-]?\s*\d+(?:\.\d+)?\s*(?:%|/\s*\d+)?")
CODE_RE = re.compile(r"(?i)(?:verification\s*code|security\s*code|one[- ]time\s*code|otp|验证码|code)\s*[:：-]?\s*\d{4,8}")
INTERN_LINK_RE = re.compile(
    r"(?m)^\s*([^:\n]{1,120}):\s*\[([^\]\n]{1,260})\]\((https?://[^)\s]+)\)\s*$"
)
STOR_455_COURSE_RE = re.compile(r"(?i)^STOR\s*455(?=$|[\s._:-])")
COMPLETE_BEFORE_CLASS_RE = re.compile(r"(?i)^COMPLETE\s+BEFORE\s+CLASS\b")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("daily briefing must be a JSON object")
    return value


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_text(value: Any, fallback: str = "", limit: int = 260) -> str:
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


def _is_hidden_complete_before_class(item: dict[str, Any]) -> bool:
    course = " ".join(str(item.get("course") or "").split())
    title = " ".join(str(item.get("title") or "").split())
    return bool(STOR_455_COURSE_RE.match(course) and COMPLETE_BEFORE_CLASS_RE.match(title))


def _safe_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return candidate


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE)


def _relative_time(target: datetime | None, now: datetime) -> tuple[str, str]:
    if target is None:
        return "时间未提供", "unknown"
    hours = (target - now).total_seconds() / 3600
    if hours < 0:
        elapsed = abs(hours)
        return (f"已逾期 {elapsed:.0f} 小时" if elapsed >= 1 else "刚刚逾期"), "overdue"
    if hours <= 12:
        return (f"还剩 {hours:.0f} 小时" if hours >= 1 else f"还剩 {max(1, round(hours * 60))} 分钟"), "red"
    if hours <= 48:
        return f"还剩 {hours:.0f} 小时", "yellow"
    if hours <= 72:
        return f"还剩 {hours:.0f} 小时", "blue"
    return f"还剩 {hours / 24:.1f} 天", "blue"


def _format_time(value: Any) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "时间未提供"
    return parsed.strftime("%a · %b %-d · %-I:%M %p")


def _link_card(url: str, classes: str, body: str, label: str) -> str:
    if not url:
        return f'<article class="card {classes} disabled" aria-label="{_escape(label)}，没有可用链接">{body}</article>'
    return (
        f'<a class="card {classes}" href="{_escape(url)}" '
        f'rel="noopener noreferrer" referrerpolicy="no-referrer" aria-label="打开 {_escape(label)}">'
        f"{body}</a>"
    )


def _timeline(item: dict[str, Any], now: datetime) -> str:
    due_at = assignment_due_at(item)
    parsed = _parse_datetime(due_at)
    date_only = bool(
        is_exam_assignment(item)
        and (
            _safe_text(item.get("time_precision")).casefold() == "date_only"
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due_at or "").strip())
        )
    )
    expiry = assignment_expiry_at(item)
    relative_target = expiry if date_only else parsed
    label, state = _relative_time(relative_target, now)
    iso_value = relative_target.isoformat() if relative_target else ""
    horizon = assignment_horizon_hours(item)
    if date_only and parsed is not None:
        due_label = parsed.strftime("%a · %b %-d · 时间待确认")
        days = (parsed.date() - now.date()).days
        label = (f"{days} 天后" if days > 0 else "今天") + " · 具体时刻待确认"
    else:
        due_label = _format_time(due_at)
    start_label = "7d" if horizon > 72 else "72h+"
    return f"""
      <div class="deadline-row">
        <span class="remaining remaining-{_escape(state)}" data-remaining>{_escape(label)}</span>
        <span class="due-label">{_escape('考试' if is_exam_assignment(item) else '截止')} {_escape(due_label)}</span>
      </div>
      <div class="time-track" data-deadline="{_escape(iso_value)}" data-date-only="{_escape(parsed.date().isoformat() if date_only and parsed else '')}" data-horizon="{horizon}" role="img" aria-label="{_escape(label)}">
        <span class="track-zone zone-blue"></span><span class="track-zone zone-yellow"></span><span class="track-zone zone-red"></span>
        <span class="time-marker" data-marker></span>
      </div>
      <div class="time-scale" aria-hidden="true"><span>{start_label}</span><span>48h</span><span>12h</span><span>截止</span></div>
    """


def _assignment_cards(assignments: list[Any], now: datetime, exam_only: bool) -> str:
    cards: list[str] = []
    for raw in assignments:
        if not isinstance(raw, dict):
            continue
        if is_hidden_assignment(raw, now):
            continue
        exam = is_exam_assignment(raw)
        if exam != exam_only or not is_within_assignment_horizon(raw, now):
            continue
        course = _safe_text(raw.get("course"), "Course", 80)
        title = _safe_text(raw.get("title"), "Untitled assignment", 180)
        status = (
            "复习资料已核验"
            if exam and has_verified_exam_review(raw)
            else ("暂无已核验复习资料" if exam else "尚未完成")
        )
        body = f"""
          <div class="card-top"><span class="eyebrow">{_escape(('EXAM · ' if exam else '') + course)}</span><span class="open-cue">打开 ↗</span></div>
          <h3>{_escape(title)}</h3>
          <div class="status-line"><span>{_escape(status)}</span></div>
          {_timeline(raw, now)}
        """
        classes = "assignment exam-card" if exam else "assignment"
        cards.append(_link_card(_safe_url(preferred_assignment_url(raw)), classes, body, f"{course} {title}"))
    if cards:
        return "".join(cards)
    return '<div class="empty">这一栏目前没有事项。</div>'


def _message_cards(messages: list[Any]) -> str:
    cards: list[str] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        source = _safe_text(raw.get("source"), "Message", 40).upper()
        sender = _safe_text(raw.get("sender_display") or raw.get("sender"), "Important sender", 100)
        subject = _safe_text(raw.get("subject"), "Important message", 160)
        summary = _safe_text(raw.get("summary"), "请打开对应应用查看。", 240)
        priority = _safe_text(raw.get("priority"), "P2", 4).upper()
        if priority not in {"P0", "P1", "P2"}:
            priority = "P2"
        received = _format_time(raw.get("received_at"))
        body = f"""
          <div class="card-top"><span class="priority priority-{_escape(priority.lower())}">{_escape(priority)}</span><span class="open-cue">打开 ↗</span></div>
          <div class="message-source">{_escape(source)} · {_escape(sender)}</div>
          <h3>{_escape(subject)}</h3>
          <p>{_escape(summary)}</p>
          <div class="message-time">收到于 {_escape(received)}</div>
        """
        cards.append(_link_card(_safe_url(raw.get("url")), f"message message-{priority.lower()}", body, f"{source} {subject}"))
    if cards:
        return "".join(cards)
    return '<div class="empty">没有需要立即处理的重要消息。</div>'


def _internships(messages: list[Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        body = str(raw.get("body") or "")
        for company, role, url in INTERN_LINK_RE.findall(body):
            clean_url = _safe_url(url)
            clean_company = _safe_text(company, "Company", 100)
            clean_role = _safe_text(role, "Internship role", 180)
            key = (clean_company.casefold(), clean_role.casefold(), clean_url)
            if not clean_url or key in seen:
                continue
            seen.add(key)
            found.append({"company": clean_company, "role": clean_role, "url": clean_url})
    return found


def _intern_cards(items: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in items:
        company = _safe_text(item.get("company"), "Company", 100)
        role = _safe_text(item.get("role"), "Internship role", 180)
        url = _safe_url(item.get("apply_url") or item.get("url"))
        host = urlparse(url).hostname or "application site"
        queue_status = _safe_text(item.get("queue_status"), "manual_review", 32)
        status_label = {
            "manual_decision": "手动决定 · 大公司或限投",
            "manual_review": "待核验 · 要求尚未确认",
            "prepared": "READY · 小公司可进入准备批次",
        }.get(queue_status, "待核验")
        resume_variant = _safe_text(item.get("resume_variant"), "", 12)
        resume_label = f" · 建议简历 {resume_variant}" if resume_variant else ""
        body = f"""
          <div class="card-top"><span class="eyebrow">{_escape(company)}</span><span class="queue queue-{_escape(queue_status)}">{_escape(status_label)}</span></div>
          <h3>{_escape(role)}</h3>
          <div class="message-time">{_escape(host + resume_label)} · 查看岗位 ↗</div>
        """
        cards.append(_link_card(url, f"intern intern-{queue_status}", body, f"{company} {role}"))
    if cards:
        return "".join(cards)
    return '<div class="empty">当前没有通过严格队列校验、可展示的 SWE List 岗位。</div>'


def _application_center_summary(items: list[dict[str, Any]]) -> str:
    counts = {
        status: sum(item.get("queue_status") == status for item in items)
        for status in ("prepared", "manual_review", "manual_decision")
    }
    return f"""
      <div class="application-center">
        <div><span class="center-kicker">LOCAL APPLICATION PIPELINE</span><h3>自动申请中心</h3></div>
        <div class="center-stats">
          <span><b>{counts['prepared']}</b> READY</span>
          <span><b>{counts['manual_review']}</b> 待核验</span>
          <span><b>{counts['manual_decision']}</b> 手动决定</span>
        </div>
        <p>系统先在本机完成资格筛选、限投检查和简历匹配。大公司、限投岗位、TikTok、PhD-only 或身份不符岗位不会进入自动提交；READY 岗位可进入交互申请批次，并在最终提交前要求确认。</p>
      </div>
    """


def _source_chips(sources: Any) -> str:
    if not isinstance(sources, dict):
        return ""
    chips: list[str] = []
    labels = {"canvas": "Canvas", "gmail": "Gmail", "outlook": "Outlook", "linkedin": "LinkedIn"}
    for key, label in labels.items():
        raw = sources.get(key) if isinstance(sources.get(key), dict) else {}
        state = str(raw.get("state") or "unknown").lower()
        style = "ok" if state == "ok" else "warning"
        readable = "已同步" if state == "ok" else ("需登录" if state == "auth_required" else "需检查")
        chips.append(f'<span class="source-chip {style}"><i></i>{label} · {readable}</span>')
    return "".join(chips)


def _resource_cards(resources: Any) -> str:
    if not isinstance(resources, list):
        return '<div class="empty">尚未启用校园资源插件。</div>'
    cards: list[str] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        if not url.startswith("https://"):
            continue
        title = _safe_text(item.get("title"), "Campus resource", 100)
        provider = _safe_text(item.get("plugin"), "Resource", 40)
        body = f'<div class="card-top"><span class="eyebrow">{_escape(provider)}</span><span class="open-cue">打开 ↗</span></div><h3>{_escape(title)}</h3>'
        cards.append(_link_card(url, "resource", body, title))
    return "".join(cards) or '<div class="empty">尚未启用校园资源插件。</div>'


def render_html(data: dict[str, Any], now: datetime | None = None) -> tuple[str, dict[str, int]]:
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    assignments = merge_assignments_and_exams(
        data.get("assignments"),
        data.get("exams"),
    )
    assignments = [
        item
        for item in assignments
        if isinstance(item, dict)
        and not is_hidden_assignment(item, now)
        and is_within_assignment_horizon(item, now)
    ]
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    interns = strict_display_items(messages, data.get("intern_preparation_summary"), zone=ZONE)
    exam_count = sum(1 for item in assignments if is_exam_assignment(item))
    upcoming_count = len(assignments) - exam_count
    message_count = sum(1 for item in messages if isinstance(item, dict))
    generated = _parse_datetime(data.get("generated_at"))
    generated_label = generated.strftime("%a · %b %-d · %-I:%M %p") if generated else "时间未知"
    build_version = _hash_bytes(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8"))[:12]

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>My Planner · 每日计划</title>
  <style>
    :root {{ color-scheme: dark; --bg:#07111f; --panel:#0d1a2b; --panel-2:#112238; --line:#253b55; --text:#f5f8fc; --muted:#9db0c7; --blue:#4aa3ff; --yellow:#ffd75e; --red:#ff6b6b; --green:#50d890; --shadow:0 16px 44px rgba(0,0,0,.28); }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; min-height:100vh; background:radial-gradient(circle at 15% 0%,#173153 0,transparent 34%),radial-gradient(circle at 92% 8%,#192f4a 0,transparent 28%),var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue",sans-serif; }}
    a {{ color:inherit; }}
    .shell {{ width:min(1440px,calc(100% - 40px)); margin:0 auto; padding:42px 0 84px; }}
    header {{ display:grid; grid-template-columns:1fr auto; gap:24px; align-items:end; margin-bottom:28px; }}
    .kicker {{ color:#8fc6ff; text-transform:uppercase; letter-spacing:.15em; font-size:12px; font-weight:800; }}
    h1 {{ margin:5px 0 6px; font-size:clamp(38px,5vw,72px); line-height:.96; letter-spacing:-.05em; }}
    h1 span {{ color:#90a8c3; font-weight:620; }}
    .updated {{ color:var(--muted); font-size:14px; }}
    .refresh-note {{ text-align:right; color:var(--muted); font-size:13px; line-height:1.5; }}
    .refresh-note strong {{ color:var(--text); display:block; }}
    .source-row {{ display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 24px; }}
    .source-chip {{ display:inline-flex; align-items:center; gap:7px; padding:8px 11px; border:1px solid var(--line); border-radius:999px; background:rgba(13,26,43,.7); font-size:12px; color:#c4d2e2; }}
    .source-chip i {{ width:7px; height:7px; border-radius:50%; background:var(--green); box-shadow:0 0 10px currentColor; }}
    .source-chip.warning i {{ background:var(--yellow); }}
    .overview {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0 0 24px; }}
    .metric {{ padding:18px 20px; background:rgba(13,26,43,.78); border:1px solid var(--line); border-radius:16px; }}
    .metric b {{ display:block; font-size:30px; letter-spacing:-.04em; }}
    .metric span {{ color:var(--muted); font-size:12px; }}
    .legend {{ position:sticky; top:10px; z-index:5; display:flex; align-items:center; flex-wrap:wrap; gap:13px; padding:12px 14px; margin:18px 0 24px; background:rgba(8,18,32,.9); backdrop-filter:blur(18px); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); }}
    .legend strong {{ margin-right:4px; }} .legend span {{ display:flex; align-items:center; gap:6px; color:#c8d6e5; font-size:12px; }}
    .legend i {{ width:22px; height:8px; border-radius:20px; display:inline-block; }}
    .search {{ margin-left:auto; min-width:250px; border:1px solid #314965; background:#0b1726; color:var(--text); border-radius:10px; padding:9px 12px; outline:none; }}
    .search:focus {{ border-color:var(--blue); box-shadow:0 0 0 3px rgba(74,163,255,.14); }}
    section {{ scroll-margin-top:92px; margin-top:34px; }}
    .section-title {{ display:flex; align-items:baseline; justify-content:space-between; gap:14px; padding:0 2px 12px; border-bottom:1px solid var(--line); margin-bottom:14px; }}
    .section-title h2 {{ margin:0; font-size:22px; letter-spacing:-.02em; }} .section-title span {{ color:var(--muted); font-size:12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px; }}
    .card {{ position:relative; min-width:0; display:block; padding:18px; background:linear-gradient(150deg,rgba(18,37,60,.96),rgba(11,25,42,.96)); border:1px solid var(--line); border-radius:17px; text-decoration:none; box-shadow:0 9px 26px rgba(0,0,0,.14); transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease; }}
    a.card:hover,a.card:focus-visible {{ transform:translateY(-2px); border-color:#5687b8; box-shadow:0 16px 35px rgba(0,0,0,.25); outline:none; }}
    .card.disabled {{ opacity:.68; }} .card[hidden] {{ display:none; }}
    .card-top {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }}
    .eyebrow,.message-source {{ color:#9ccaff; text-transform:uppercase; letter-spacing:.08em; font-size:11px; font-weight:800; }}
    .open-cue {{ color:#c8d7e8; font-size:12px; }}
    h3 {{ margin:10px 0 12px; font-size:17px; line-height:1.32; letter-spacing:-.01em; }}
    .card p {{ color:#bfcede; line-height:1.45; font-size:13px; margin:8px 0 15px; }}
    .status-line,.message-time {{ color:var(--muted); font-size:12px; }}
    .deadline-row {{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:14px; }}
    .remaining {{ font-weight:800; font-size:12px; }} .remaining-blue {{ color:#8bc5ff; }} .remaining-yellow {{ color:#ffe487; }} .remaining-red,.remaining-overdue {{ color:#ff9898; }}
    .due-label {{ color:#b6c6d8; font-size:11px; text-align:right; }}
    .time-track {{ position:relative; display:flex; width:100%; height:13px; margin-top:10px; overflow:visible; border-radius:999px; box-shadow:inset 0 0 0 1px rgba(255,255,255,.17); }}
    .track-zone {{ height:100%; }} .zone-blue {{ width:33.333%; background:var(--blue); border-radius:999px 0 0 999px; }} .zone-yellow {{ width:50%; background:var(--yellow); }} .zone-red {{ width:16.667%; background:var(--red); border-radius:0 999px 999px 0; }}
    .time-marker {{ position:absolute; left:0; top:50%; width:3px; height:21px; transform:translate(-50%,-50%); background:white; border-radius:4px; box-shadow:0 0 0 2px #07111f,0 2px 8px rgba(0,0,0,.7); }}
    .time-track.is-overdue {{ background:repeating-linear-gradient(135deg,#9f2830 0 9px,#e6535e 9px 18px); overflow:hidden; }} .time-track.is-overdue .track-zone {{ opacity:0; }}
    .time-scale {{ position:relative; display:grid; grid-template-columns:2fr 1fr; margin-top:6px; height:13px; color:#748aa3; font-size:9px; }}
    .time-scale span {{ position:absolute; transform:translateX(-50%); }} .time-scale span:nth-child(1) {{ left:0; transform:none; }} .time-scale span:nth-child(2) {{ left:33.333%; }} .time-scale span:nth-child(3) {{ left:83.333%; }} .time-scale span:nth-child(4) {{ right:0; left:auto; transform:none; }}
    .priority {{ display:inline-flex; align-items:center; justify-content:center; min-width:35px; padding:5px 8px; border-radius:8px; font-size:11px; font-weight:850; }}
    .priority-p0 {{ background:rgba(255,107,107,.18); color:#ff9b9b; border:1px solid rgba(255,107,107,.5); }}
    .priority-p1 {{ background:rgba(255,215,94,.15); color:#ffe286; border:1px solid rgba(255,215,94,.4); }}
    .priority-p2 {{ background:rgba(74,163,255,.15); color:#9acbff; border:1px solid rgba(74,163,255,.4); }}
    .exam-card {{ border-color:rgba(74,163,255,.58); box-shadow:0 9px 28px rgba(32,108,190,.13); }}
    .intern {{ min-height:128px; }}
    .application-center {{ display:grid; grid-template-columns:1fr auto; gap:10px 28px; align-items:center; margin:0 0 14px; padding:18px 20px; border:1px solid #31679b; border-radius:17px; background:linear-gradient(135deg,rgba(18,57,91,.92),rgba(11,31,51,.94)); }}
    .application-center h3 {{ margin:4px 0 0; font-size:21px; }} .center-kicker {{ color:#8ec9ff; font-size:10px; font-weight:850; letter-spacing:.14em; }}
    .center-stats {{ display:flex; gap:9px; flex-wrap:wrap; justify-content:flex-end; }} .center-stats span {{ padding:8px 10px; border:1px solid #31577d; border-radius:10px; color:#bcd1e6; font-size:11px; }} .center-stats b {{ color:#fff; font-size:15px; margin-right:3px; }}
    .application-center p {{ grid-column:1/-1; margin:2px 0 0; color:#b9cadb; font-size:12px; line-height:1.55; }}
    .queue {{ padding:5px 8px; border-radius:9px; font-size:10px; font-weight:800; }} .queue-prepared {{ color:#91efbb; border:1px solid rgba(80,216,144,.45); background:rgba(80,216,144,.12); }} .queue-manual_review {{ color:#9acbff; border:1px solid rgba(74,163,255,.42); background:rgba(74,163,255,.12); }} .queue-manual_decision {{ color:#ffe286; border:1px solid rgba(255,215,94,.42); background:rgba(255,215,94,.12); }}
    .empty {{ grid-column:1/-1; padding:32px; border:1px dashed #34506d; color:var(--muted); border-radius:16px; text-align:center; }}
    .privacy {{ margin-top:38px; padding-top:18px; border-top:1px solid var(--line); color:#7f95ae; font-size:11px; line-height:1.6; }}
    @media (max-width:850px) {{ .shell {{ width:min(100% - 24px,720px); padding-top:24px; }} header {{ grid-template-columns:1fr; }} .refresh-note {{ text-align:left; }} .overview {{ grid-template-columns:repeat(2,1fr); }} .grid {{ grid-template-columns:1fr; }} .legend {{ top:5px; }} .search {{ width:100%; margin-left:0; }} }}
    @media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important; transition:none!important; }} }}
  </style>
</head>
<body data-build="{build_version}">
  <main class="shell">
    <header>
      <div><div class="kicker">Private · Local · Actionable</div><h1>My Planner <span>每日计划</span></h1><div class="updated">数据更新于 {_escape(generated_label)} · America/New_York</div></div>
      <div class="refresh-note"><strong>时间每分钟实时更新</strong>报告变化后，本地查看器会自动载入新内容</div>
    </header>
    <div class="source-row">{_source_chips(data.get("sources"))}</div>
    <div class="overview">
      <div class="metric"><b>{exam_count}</b><span>未来 7 天考试</span></div><div class="metric"><b>{upcoming_count}</b><span>未来 72 小时作业</span></div><div class="metric"><b>{message_count}</b><span>重要消息</span></div><div class="metric"><b>{len(interns)}</b><span>实习岗位</span></div>
    </div>
    <div class="legend">
      <strong>Deadline 时间轴</strong><span><i style="background:var(--blue)"></i>蓝色 · 48–72+ 小时</span><span><i style="background:var(--yellow)"></i>黄色 · 12–48 小时</span><span><i style="background:var(--red)"></i>红色 · 0–12 小时</span>
      <input class="search" id="planner-search" type="search" placeholder="搜索课程、消息或公司" autocomplete="off" aria-label="搜索每日计划">
    </div>
    <section id="exams"><div class="section-title"><h2>考试 · Next 7 days</h2><span>优先跳转至已核验复习资料</span></div><div class="grid">{_assignment_cards(assignments, now, True)}</div></section>
    <section id="upcoming"><div class="section-title"><h2>作业 · Next 72 hours</h2><span>整张卡片均可点击</span></div><div class="grid">{_assignment_cards(assignments, now, False)}</div></section>
    <section id="messages"><div class="section-title"><h2>重要消息 · Messages</h2><span>跳转至对应邮箱或消息</span></div><div class="grid">{_message_cards(messages)}</div></section>
    <section id="internships"><div class="section-title"><h2>实习申请 · Internships</h2><span>{len(interns)} 个严格筛选后的行动项</span></div>{_application_center_summary(interns)}<div class="grid">{_intern_cards(interns)}</div></section>
    <section id="resources"><div class="section-title"><h2>校园资源 · Plugins</h2><span>本地启用的只读链接</span></div><div class="grid">{_resource_cards(data.get("resource_links"))}</div></section>
    <div class="privacy">本页面完全由本机的 daily_briefing.json 生成，不连接第三方分析服务。页面不显示完整邮箱、正文、验证码、电话号码、金额、成绩或完整 URL；链接仅作为整卡跳转目标，并限制为 HTTPS/HTTP。</div>
  </main>
  <script>
    (() => {{
      const updateDeadlines = () => {{
        const now = Date.now();
        document.querySelectorAll('[data-deadline]').forEach(track => {{
          const due = Date.parse(track.dataset.deadline || '');
          if (!Number.isFinite(due)) return;
          const hours = (due - now) / 3600000;
          const marker = track.querySelector('[data-marker]');
          const card = track.closest('.card');
          const label = card ? card.querySelector('[data-remaining]') : null;
          let text = '';
          let state = 'blue';
          if (hours <= 0) {{ if (card) card.hidden = true; return; }}
          else if (hours <= 12) {{ text = hours < 1 ? `还剩 ${{Math.max(1, Math.round(hours * 60))}} 分钟` : `还剩 ${{hours.toFixed(0)}} 小时`; state = 'red'; }}
          else if (hours <= 48) {{ text = `还剩 ${{hours.toFixed(0)}} 小时`; state = 'yellow'; }}
          else if (hours <= 72) {{ text = `还剩 ${{hours.toFixed(0)}} 小时`; state = 'blue'; }}
          else {{ text = `还剩 ${{(hours / 24).toFixed(1)}} 天`; state = 'blue'; }}
          if (track.dataset.dateOnly) {{
            const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}}).formatToParts(now).map(p => [p.type,p.value]));
            const today = Date.parse(`${{parts.year}}-${{parts.month}}-${{parts.day}}T00:00:00Z`);
            const days = Math.round((Date.parse(track.dataset.dateOnly + 'T00:00:00Z') - today) / 86400000);
            text = (days > 0 ? `${{days}} 天后` : '今天') + ' · 具体时刻待确认';
          }}
          const horizon = Math.max(72, Number(track.dataset.horizon || 72));
          const position = Math.max(0, Math.min(100, 100 - (hours / horizon * 100)));
          if (marker) marker.style.left = `${{position}}%`;
          track.classList.toggle('is-overdue', state === 'overdue');
          track.setAttribute('aria-label', text);
          if (label) {{ label.textContent = text; label.className = `remaining remaining-${{state}}`; }}
        }});
      }};
      const search = document.getElementById('planner-search');
      if (search) search.addEventListener('input', () => {{
        const query = search.value.trim().toLocaleLowerCase();
        document.querySelectorAll('.card').forEach(card => {{ card.hidden = Boolean(query) && !card.textContent.toLocaleLowerCase().includes(query); }});
      }});
      updateDeadlines();
      window.setInterval(updateDeadlines, 60000);
    }})();
  </script>
</body>
</html>
"""
    return document, {
        "assignments": upcoming_count,
        "exams": exam_count,
        "warnings": 0,
        "messages": message_count,
        "internships": len(interns),
        "clickable_items": sum(1 for match in re.finditer(r'<a class="card ', document)),
    }


def render(input_path: Path, output_path: Path, status_path: Path, force: bool = False) -> dict[str, Any]:
    source_bytes = input_path.read_bytes()
    source_hash = _hash_bytes(source_bytes)
    existing_status: dict[str, Any] = {}
    if status_path.exists():
        try:
            existing_status = _read_json(status_path)
        except (OSError, ValueError, json.JSONDecodeError):
            existing_status = {}
    if not force and output_path.exists() and existing_status.get("input_sha256") == source_hash:
        return {**existing_status, "status": "unchanged"}

    data = json.loads(source_bytes.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("daily briefing must be a JSON object")
    document, counts = render_html(data)
    _atomic_text(output_path, document)
    output_hash = _hash_bytes(output_path.read_bytes())
    status = {
        "status": "ok",
        "generated_at": datetime.now(ZONE).isoformat(),
        "report_generated_at": data.get("generated_at"),
        "input": str(input_path),
        "output": str(output_path),
        "input_sha256": source_hash,
        "output_sha256": output_hash,
        "counts": counts,
        "privacy_mode": "redacted_local_html",
    }
    _atomic_text(status_path, json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    result = render(arguments.input.expanduser(), arguments.output.expanduser(), arguments.status.expanduser(), arguments.force)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
