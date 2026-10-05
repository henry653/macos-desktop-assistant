#!/usr/bin/env python3
"""Shared user-level assignment visibility rules for My Planner."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("America/New_York")
COMP_311_RE = re.compile(r"(?i)^COMP\s*311(?=$|[\s._:-])")
STOR_455_RE = re.compile(r"(?i)^STOR\s*455(?=$|[\s._:-])")
COMPLETE_BEFORE_CLASS_RE = re.compile(
    r"(?i)^COMPLETE[\s._:/\-–—]+BEFORE[\s._:/\-–—]+CLASS\b"
)
PAST_DUE_STATUSES = {
    "past_due",
    "past_due_unsubmitted",
    "overdue",
    "late_unsubmitted",
}
DEFAULT_ASSIGNMENT_HORIZON_HOURS = 72
EXAM_HORIZON_HOURS = 7 * 24

# An explicit Canvas/API type is the strongest signal.  Title matching is kept
# deliberately narrow so that items such as "Exam review" or "practice exam"
# are treated as study material/ordinary coursework rather than as the exam
# itself.
EXAM_TYPE_RE = re.compile(
    r"(?i)(?:^|[^a-z])(?:exam(?:ination)?|mid[\s_-]?term|final[\s_-]?exam|unit[\s_-]+test|course[\s_-]+test)(?:$|[^a-z])"
)
EXAM_TITLE_RE = re.compile(
    r"(?i)(?:\b(?:exam(?:ination)?|mid[\s_-]?term|final\s+exam|unit\s+test|course\s+test)\b|期中(?:考试)?|期末(?:考试)?|考试)"
)
NON_EXAM_TITLE_RE = re.compile(
    r"(?i)(?:\b(?:review|practice|sample|mock|study\s+guide)\b|复习|模拟)"
)
FORMAL_QUIZ_COURSES = {"COMP455", "STOR415"}
NUMBERED_QUIZ_RE = re.compile(r"(?i)\b(?:quiz|qz)\s*[-:#]?\s*\d+\b")
NUMBERED_TEST_RE = re.compile(r"(?i)\b(?:module\s+\d+\s+test|test\s*\d+)\b")

# These fields are intentionally explicit: a generic attachment must not
# silently replace the exam destination.  Collection code may populate any of
# these aliases while preserving the canonical assignment schema.
EXAM_STUDY_URL_FIELDS = (
    "review_url",
    "review_materials_url",
    "review_material_url",
    "study_materials_url",
    "study_material_url",
    "study_guide_url",
    "review_page_url",
    "materials_url",
)
EXAM_STUDY_CONTAINER_FIELDS = (
    "review_materials",
    "study_materials",
    "study_guide",
)
EXAM_URL_FIELDS = ("exam_url", "source_url", "url", "html_url")

# This exact COMP 455 item was completed outside the LMS workflow and the user
# emailed the professor. Keeping the due time in the key prevents a future
# semester's similarly named assignment from being hidden.
RESOLVED_ASSIGNMENTS = {
    ("COMP455", "HW00MATHREVIEW", "2026-08-26T23:59:00-04:00"),
}


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


def _compact(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _status(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _nested_url(value: Any) -> str:
    """Extract only an explicitly labelled URL value from a small container."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "href", "html_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    if isinstance(value, (list, tuple)):
        for candidate in value:
            url = _nested_url(candidate)
            if url:
                return url
    return ""


def is_exam_assignment(item: Any) -> bool:
    """Return whether an item is the actual exam (not merely exam prep)."""

    if not isinstance(item, dict):
        return False
    title = " ".join(str(item.get("title") or "").split())
    # A review/practice item is never promoted into an exam merely because an
    # upstream source used a broad `exam` category.
    if title and NON_EXAM_TITLE_RE.search(title):
        return False
    explicit = item.get("is_exam")
    if explicit is True:
        return True
    if explicit is False:
        return False

    # These courses use scheduled, graded classroom quizzes as major
    # assessments. Keep the official name; do not promote homework quizzes or
    # study material just because they contain the word "quiz".
    course_match = re.match(r"([A-Z]+)\s*(\d+)", str(item.get("course") or "").upper())
    course = "".join(course_match.groups()) if course_match else ""
    if course in FORMAL_QUIZ_COURSES and NUMBERED_QUIZ_RE.search(title):
        return not re.search(r"(?i)\b(?:homework|practice|self[- ]check)\b", title)
    if NUMBERED_TEST_RE.search(title):
        return True

    # ``exam_at`` is part of the dedicated top-level ``exams`` schema.  Its
    # presence is an explicit structural signal even if the title is localized.
    if str(item.get("exam_at") or item.get("exam_date") or "").strip():
        return True

    for key in ("type", "kind", "item_type", "event_type", "assignment_type", "category"):
        value = str(item.get(key) or "").strip()
        if value and EXAM_TYPE_RE.search(value):
            return True

    if not title:
        return False
    return bool(EXAM_TITLE_RE.search(title))


def assignment_horizon_hours(item: Any) -> int:
    """Exams appear for seven days; ordinary work keeps the 72-hour window."""

    return EXAM_HORIZON_HOURS if is_exam_assignment(item) else DEFAULT_ASSIGNMENT_HORIZON_HOURS


def assignment_due_at(item: Any) -> Any:
    """Return the canonical deadline/start value for assignments and exams."""

    if not isinstance(item, dict):
        return None
    if is_exam_assignment(item) and item.get("exam_at"):
        return item.get("exam_at")
    if is_exam_assignment(item) and item.get("exam_date"):
        return item.get("exam_date")
    return item.get("due_at")


def assignment_expiry_at(item: Any) -> datetime | None:
    """Return the exclusive time after which an item must disappear.

    A timed exam uses its explicit ``exam_end_at`` when supplied and otherwise
    its explicit start.  A date-only exam has no invented clock time: it is a
    calendar-day event and expires at the start of the following local day.
    """

    if not isinstance(item, dict):
        return None
    if is_exam_assignment(item):
        explicit_end = _parse_datetime(item.get("exam_end_at"))
        if explicit_end is not None:
            return explicit_end
        raw_start = str(
            item.get("exam_at")
            or item.get("exam_date")
            or assignment_due_at(item)
            or ""
        ).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_start):
            start = _parse_datetime(raw_start)
            return start + timedelta(days=1) if start else None
    return _parse_datetime(assignment_due_at(item))


def preferred_assignment_url(item: Any) -> str:
    """Choose an exam's study/review destination before its exam page."""

    if not isinstance(item, dict):
        return ""
    if is_exam_assignment(item):
        review_state = _status(item.get("review_state"))
        # Fail closed: a URL is a review destination only after the collector
        # explicitly verified that it belongs to this exam.
        review_allowed = review_state == "verified"
        if review_allowed:
            for key in EXAM_STUDY_URL_FIELDS:
                url = _nested_url(item.get(key))
                if url:
                    return url
            for key in EXAM_STUDY_CONTAINER_FIELDS:
                url = _nested_url(item.get(key))
                if url:
                    return url
        for key in EXAM_URL_FIELDS:
            url = _nested_url(item.get(key))
            if url:
                return url
        return ""
    return _nested_url(item.get("url"))


def has_verified_exam_review(item: Any) -> bool:
    """Whether an exam has a usable, non-rejected review-material target."""

    if not is_exam_assignment(item):
        return False
    review_url = ""
    for key in EXAM_STUDY_URL_FIELDS:
        review_url = _nested_url(item.get(key))
        if review_url:
            break
    if not review_url:
        for key in EXAM_STUDY_CONTAINER_FIELDS:
            review_url = _nested_url(item.get(key))
            if review_url:
                break
    if not review_url:
        return False
    return _status(item.get("review_state")) == "verified"


def is_within_assignment_horizon(item: Any, now: datetime) -> bool:
    """Return True only for future items inside their product display window."""

    if not isinstance(item, dict):
        return False
    due = _parse_datetime(assignment_due_at(item))
    expiry = assignment_expiry_at(item)
    if due is None or expiry is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZONE)
    local_now = now.astimezone(ZONE)
    return local_now < expiry and due <= local_now + timedelta(hours=assignment_horizon_hours(item))


def merge_assignments_and_exams(assignments: Any, exams: Any) -> list[dict[str, Any]]:
    """Merge legacy assignment-embedded exams with the dedicated exam list.

    Dedicated exam records win field conflicts because they can carry the
    reviewed study-material destination.  A stable course/title/time key keeps
    the same exam from appearing twice during schema migration.
    """

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []

    def add(raw: Any, *, dedicated_exam: bool) -> None:
        if not isinstance(raw, dict):
            return
        item = dict(raw)
        if dedicated_exam:
            item["is_exam"] = True
            if not item.get("exam_at") and item.get("exam_date"):
                item["exam_at"] = item.get("exam_date")
            if not item.get("due_at") and assignment_due_at(item):
                item["due_at"] = assignment_due_at(item)
            if not item.get("url") and item.get("source_url"):
                item["url"] = item.get("source_url")
        key = (
            _compact(item.get("course")),
            _compact(item.get("title")),
            str(assignment_due_at(item) or "").strip(),
        )
        if key not in merged:
            merged[key] = item
            order.append(key)
        elif dedicated_exam:
            merged[key] = {**merged[key], **item}

    if isinstance(assignments, list):
        for raw in assignments:
            add(raw, dedicated_exam=False)
    if isinstance(exams, list):
        for raw in exams:
            add(raw, dedicated_exam=True)
    return [merged[key] for key in order]


def is_complete_before_class(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    course = " ".join(str(item.get("course") or "").split())
    title = " ".join(str(item.get("title") or "").split())
    return bool(STOR_455_RE.match(course) and COMPLETE_BEFORE_CLASS_RE.match(title))


def is_resolved_assignment(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    course = _compact(item.get("course"))
    title = _compact(item.get("title"))
    due = str(item.get("due_at") or "").strip()
    return any(
        course.startswith(resolved_course)
        and title.startswith(resolved_title)
        and due == resolved_due
        for resolved_course, resolved_title, resolved_due in RESOLVED_ASSIGNMENTS
    )


def is_hidden_assignment(item: Any, now: datetime | None = None) -> bool:
    """Apply the user's course-specific visibility preferences.

    COMP 311 is permanently hidden because the course was dropped. The one
    resolved COMP 455 homework stays hidden, but other COMP 455 work is treated
    normally. Every item disappears at its deadline; My Planner intentionally
    has no overdue/WARNING queue.  STOR 455 "Complete Before Class" follows the
    same deadline boundary even when Canvas leaves its status unchanged.
    """

    if not isinstance(item, dict):
        return False
    course = " ".join(str(item.get("course") or "").split())
    if COMP_311_RE.match(course):
        return True
    if is_resolved_assignment(item):
        return True

    expiry = assignment_expiry_at(item)
    if expiry is None:
        return _status(item.get("status")) in PAST_DUE_STATUSES
    if now is None:
        return _status(item.get("status")) in PAST_DUE_STATUSES
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZONE)
    return expiry <= now.astimezone(ZONE)


def visible_assignments(items: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and not is_hidden_assignment(item, now)
        and (now is None or is_within_assignment_horizon(item, now))
    ]
