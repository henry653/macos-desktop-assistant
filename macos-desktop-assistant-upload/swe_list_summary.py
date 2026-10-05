#!/usr/bin/env python3
"""Strict, local-only SWE List parsing and widget-safe summaries.

This module deliberately has no mail, browser, or application-submission code.
It accepts messages that have already been collected by a read-only source,
re-verifies the sender, subject count, and parsed role count, then returns only
the small amount of data needed by My Planner.

The daily briefing schema is intentionally left intact.  Callers may add the
returned summary as an optional field without rewriting any existing state.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import parseaddr
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo


TARGET_SENDER = "noreply@swelist.com"
DEFAULT_ZONE = ZoneInfo("America/New_York")
SUBJECT_COUNT_RE = re.compile(r"(?i)\b(\d+)\s+New\s+Internships?\s+Posted\s+Today\b")
ENTRY_RE = re.compile(
    r"^\s*(?:[-*\u2022]\s*)?(?:\*\*)?(.{1,120}?)\s*:\s*(?:\*\*)?\s*"
    r"\[([^\]\n]{1,260})\]\((https?://[^)\s]+)\)(.*)$",
    re.IGNORECASE,
)

# The list is intentionally conservative.  False positives only send a role to
# manual review; they can never trigger an application from this module.
BIG_COMPANIES = {
    "adobe",
    "airbnb",
    "alphabet",
    "amazon",
    "american express",
    "apple",
    "blackrock",
    "blackstone",
    "bloomberg",
    "bny",
    "bp",
    "bytedance",
    "capital one",
    "cigna",
    "coinbase",
    "databricks",
    "deloitte",
    "disney",
    "facebook",
    "ge aerospace",
    "goldman sachs",
    "google",
    "home depot",
    "ibm",
    "intel",
    "jpmorgan",
    "johnson johnson",
    "linkedin",
    "mastercard",
    "meta",
    "microsoft",
    "netflix",
    "nvidia",
    "openai",
    "oracle",
    "palantir",
    "pimco",
    "procter gamble",
    "royal bank of canada",
    "salesforce",
    "stripe",
    "stryker",
    "tesla",
    "the walt disney company",
    "tiktok",
    "two sigma",
    "uber",
    "waymo",
    "yahoo",
}

TARGET_ROLE_RE = re.compile(
    r"(?i)\b(?:software|developer|data|analytics?|artificial intelligence|ai|"
    r"machine learning|ml|algorithm|quant(?:itative)?|computer science|"
    r"application engineer|full[ -]?stack|front[ -]?end|back[ -]?end|firmware|"
    r"cloud|cyber|business intelligence|research (?:engineer|scientist)|"
    r"product engineer|technology|rpa|automation)\b"
)
INELIGIBLE_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("graduate_degree_required", re.compile(r"(?i)\b(?:ph\.?d\.?|doctoral|postdoc(?:toral)?|masters?\s+(?:or|and)\s+ph\.?d\.?)\b")),
    ("graduate_program_required", re.compile(r"(?i)\b(?:graduate|mba|j\.?d\.?)\s+(?:student|intern|program|degree)\b")),
    ("citizenship_required", re.compile(r"(?i)\b(?:u\.?s\.?|united states)\s+citizens?(?:hip)?\s+(?:only|required)\b|\bmust\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen\b")),
    ("clearance_required", re.compile(r"(?i)\b(?:active\s+)?(?:security|secret|top secret)\s+clearance\s+(?:required|only)\b")),
)
LIMIT_RE = re.compile(
    r"(?i)\b(?:only|at\s+most|max(?:imum)?|limited\s+to)\s*(\d+)\s*"
    r"(?:applications?|positions?|roles?|openings?)\b|"
    r"\b(?:applications?|positions?|roles?|openings?)\s+(?:are\s+)?limited\s+to\s*(\d+)\b"
)

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _sender_addresses(message: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    for key in ("sender_address", "sender", "from", "sender_display"):
        raw = _text(message.get(key))
        if not raw:
            continue
        _, address = parseaddr(raw)
        candidate = (address or raw).strip().casefold()
        if "@" in candidate and candidate not in addresses:
            addresses.append(candidate)
    return addresses


def _legacy_sender_attested(message: dict[str, Any]) -> bool:
    """Accept the pre-hardening local record format without trusting lookalikes.

    Older, already-verified local reports stored the display name but not the
    address.  Requiring all four fields below prevents an arbitrary message
    merely named "SWE List" from entering the widget while preserving those
    reports until a newly collected strict record replaces them.
    """

    return (
        _text(message.get("source")).casefold() in {"gmail", "outlook"}
        and _text(message.get("sender_display")).casefold() == "swe list"
        and _text(message.get("fingerprint")).casefold().startswith("gmail:swelist:")
        and message.get("count_verified") is True
    )


def sender_is_verified(message: dict[str, Any]) -> bool:
    addresses = _sender_addresses(message)
    return (
        bool(addresses) and all(address == TARGET_SENDER for address in addresses)
    ) or (not addresses and _legacy_sender_attested(message))


def expected_count(subject: Any) -> int | None:
    match = SUBJECT_COUNT_RE.search(_text(subject))
    return int(match.group(1)) if match else None


def is_candidate_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    return sender_is_verified(message) and expected_count(message.get("subject")) is not None


def _canonical_url(value: Any) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_QUERY_KEYS
        and not key.casefold().startswith(TRACKING_QUERY_PREFIXES)
    ]
    return urlunparse(
        (
            "https",
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "/",
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def _normal_company(value: Any) -> str:
    text = _text(value).strip("* _-:\u2013\u2014")
    return re.sub(r"\s+", " ", text)[:120]


def _normal_role(value: Any) -> str:
    text = _text(value).strip("* _-:\u2013\u2014")
    return re.sub(r"\s+", " ", text)[:260]


def parse_entries(body: Any) -> list[dict[str, Any]]:
    """Parse and de-duplicate the canonical ``Company: [Role](URL)`` lines."""

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_line in str(body or "").splitlines():
        match = ENTRY_RE.match(raw_line)
        if not match:
            continue
        company = _normal_company(match.group(1))
        role = _normal_role(match.group(2))
        url = _canonical_url(match.group(3))
        trailing = _text(match.group(4))
        if not company or not role or not url:
            continue
        key = (company.casefold(), role.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        limit_match = LIMIT_RE.search(f"{role} {trailing}")
        limit = next((part for part in (limit_match.groups() if limit_match else ()) if part), None)
        entries.append(
            {
                "company": company,
                "role": role,
                "url": url,
                "isLimited": bool(limit_match),
                "limitCount": int(limit) if limit else None,
                # Public, same-line listing qualifiers such as application caps
                # or explicit eligibility restrictions.  Keeping only the
                # normalized trailing text lets the local preparation queue
                # enforce those qualifiers without opening the job page.
                "listingNotes": trailing[:500],
            }
        )
    return entries


def _normalized_company_key(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", company.casefold()).strip()


def is_big_company(company: Any) -> bool:
    normalized = _normalized_company_key(_text(company))
    if not normalized:
        return False
    return any(
        normalized == known or normalized.startswith(f"{known} ") or normalized.endswith(f" {known}")
        for known in BIG_COMPANIES
    )


def _ineligible_reason(role: str) -> str | None:
    for reason, pattern in INELIGIBLE_ROLE_PATTERNS:
        if pattern.search(role):
            return reason
    if not TARGET_ROLE_RE.search(role):
        return "outside_target_profile"
    return None


def classify_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a mutually-exclusive, conservative preparation category."""

    reason = _ineligible_reason(_text(entry.get("role")))
    big = is_big_company(entry.get("company"))
    limited = bool(entry.get("isLimited"))
    if reason:
        category = "skipped_ineligible"
        category_reason = reason
    elif big or limited:
        category = "manual_review"
        category_reason = "big_company" if big else "application_limit"
    else:
        category = "eligible_small_company_preparation"
        category_reason = "target_role_no_explicit_disqualifier"
    return {
        **entry,
        "category": category,
        "categoryReason": category_reason,
        "isBigCompany": big,
    }


def _parse_received_date(value: Any, zone: ZoneInfo) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).date().isoformat()


def analyze_message(message: Any, zone: ZoneInfo = DEFAULT_ZONE) -> dict[str, Any] | None:
    """Re-verify one already-collected message and return local role records."""

    if not isinstance(message, dict) or not is_candidate_message(message):
        return None
    subject_expected = expected_count(message.get("subject"))
    if subject_expected is None:
        return None
    body = message.get("body")
    if not isinstance(body, str):
        return None
    entries = parse_entries(body)
    parsed = len(entries)
    try:
        stored_expected = int(message.get("expected_internship_count", subject_expected))
        stored_parsed = int(message.get("parsed_internship_count", parsed))
    except (TypeError, ValueError):
        return None
    if message.get("count_verified") is False:
        return None
    if stored_expected != subject_expected or stored_parsed != parsed or parsed != subject_expected:
        return None
    received_date = _parse_received_date(message.get("received_at"), zone)
    if not received_date:
        return None
    classified = [classify_entry(entry) for entry in entries]
    return {
        "date": received_date,
        "expectedCount": subject_expected,
        "parsedCount": parsed,
        "senderVerification": (
            "address" if _sender_addresses(message) else "legacy_attested"
        ),
        "roles": classified,
    }


def _role_key(role: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(role.get("company")).casefold(),
        _text(role.get("role")).casefold(),
        _text(role.get("url")),
    )


def collect_verified_roles(
    messages: Iterable[Any], zone: ZoneInfo = DEFAULT_ZONE
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return sanitized per-day summaries and classified, de-duplicated roles."""

    per_day: dict[str, dict[str, Any]] = {}
    roles_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for message in messages:
        analysis = analyze_message(message, zone)
        if analysis is None:
            continue
        day = analysis["date"]
        bucket = per_day.setdefault(
            day,
            {
                "date": day,
                "verifiedMessageCount": 0,
                "expectedRoleCount": 0,
                "parsedRoleCount": 0,
                "senderAddressVerified": True,
                "_keys": set(),
                "_roles": [],
            },
        )
        bucket["verifiedMessageCount"] += 1
        bucket["expectedRoleCount"] += analysis["expectedCount"]
        bucket["parsedRoleCount"] += analysis["parsedCount"]
        if analysis["senderVerification"] != "address":
            bucket["senderAddressVerified"] = False
        for role in analysis["roles"]:
            key = _role_key(role)
            if key not in bucket["_keys"]:
                bucket["_keys"].add(key)
                day_role = {**role, "date": day}
                bucket["_roles"].append(day_role)
            roles_by_key[(day, *key)] = {**role, "date": day}

    summaries: list[dict[str, Any]] = []
    for day in sorted(per_day, reverse=True):
        bucket = per_day[day]
        roles = bucket.pop("_roles")
        bucket.pop("_keys")
        categories = [role["category"] for role in roles]
        manual_roles = [role for role in roles if role["category"] == "manual_review"]
        summary = {
            **bucket,
            "countVerified": bucket["expectedRoleCount"] == bucket["parsedRoleCount"],
            "totalUniqueRoles": len(roles),
            "manualReviewCount": categories.count("manual_review"),
            "bigCompanyCount": sum(role.get("isBigCompany") is True for role in manual_roles),
            "limitedApplicationCount": sum(role.get("isLimited") is True for role in manual_roles),
            "eligibleSmallCompanyPreparationCount": categories.count(
                "eligible_small_company_preparation"
            ),
            "skippedIneligibleCount": categories.count("skipped_ineligible"),
        }
        summaries.append(summary)

    roles = sorted(
        roles_by_key.values(),
        key=lambda role: (
            role["date"],
            {"manual_review": 2, "eligible_small_company_preparation": 1}.get(
                role["category"], 0
            ),
            _text(role.get("company")).casefold(),
            _text(role.get("role")).casefold(),
        ),
        reverse=True,
    )
    return summaries, roles


def latest_summary(summaries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    first = next(iter(summaries), None)
    if isinstance(first, dict):
        return dict(first)
    return {
        "date": None,
        "verifiedMessageCount": 0,
        "expectedRoleCount": 0,
        "parsedRoleCount": 0,
        "senderAddressVerified": False,
        "countVerified": False,
        "totalUniqueRoles": 0,
        "manualReviewCount": 0,
        "bigCompanyCount": 0,
        "limitedApplicationCount": 0,
        "eligibleSmallCompanyPreparationCount": 0,
        "skippedIneligibleCount": 0,
    }
