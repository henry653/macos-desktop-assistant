#!/usr/bin/env python3
"""Build a local-only, evidence-safe SWE internship preparation queue.

This module does not contain browser automation, form filling, CAPTCHA handling,
or submission code.  It only consumes count-verified SWE List messages, applies
deterministic eligibility and de-duplication rules, and records which existing
resume would be appropriate for a later, user-reviewed application.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from swe_list_summary import analyze_message, collect_verified_roles, is_big_company


CURRENT_RESUME_DIR = Path(os.environ.get("DESKTOP_ASSISTANT_RESUME_DIR", str(Path.home() / "Documents/Resumes"))).expanduser()
LEGACY_RESUME_DIR = CURRENT_RESUME_DIR

# Current paths come first.  A future clean A variant is detected automatically;
# no resume is copied, renamed, or edited by this module.
RESUME_CANDIDATES: dict[str, tuple[Path, ...]] = {
    "P": (
        CURRENT_RESUME_DIR / "resume_p.pdf",
        CURRENT_RESUME_DIR / "resume_product.pdf",
    ),
    "A": (
        CURRENT_RESUME_DIR / "resume_a.pdf",
        CURRENT_RESUME_DIR / "resume_ai.pdf",
    ),
}

UNSAFE_RESUME_MARKERS = (
    "fictional character resume",
    "manuscript development only",
    "includes invented campus/independent material",
)

TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
TRACKING_QUERY_PREFIXES = ("utm_",)

AI_ENGINEERING_RESUME_RE = re.compile(
    r"(?i)\b(?:artificial intelligence|ai|machine learning|ml|deep learning|"
    r"data science|data scientist|software(?: engineering| engineer)?|developer|"
    r"full[ -]?stack|front[ -]?end|back[ -]?end|algorithm|quant(?:itative)?|"
    r"nlp|natural language|computer vision|research (?:engineer|scientist)|"
    r"model(?:ing)?|firmware|cloud|cybersecurity)\b"
)
PRODUCT_SYSTEMS_RESUME_RE = re.compile(
    r"(?i)\b(?:product|business intelligence|analytics?|technology consulting|"
    r"application engineer|solutions? engineer|systems? analyst|operations research)\b"
)

TARGET_TECHNICAL_RE = re.compile(
    r"(?i)\b(?:software|developer|data (?:engineer|scientist|analyst)|data science|"
    r"analytics?|artificial intelligence|ai|machine learning|ml|algorithm|"
    r"quant(?:itative)?|computer science|application engineer|solutions? engineer|"
    r"full[ -]?stack|front[ -]?end|back[ -]?end|firmware|cloud|cyber|"
    r"business intelligence|research (?:engineer|scientist)|rpa|automation|"
    r"technology consulting|computer engineer)\b"
)
OBVIOUS_NONTECHNICAL_RE = re.compile(
    r"(?i)\b(?:human resources|recruit(?:ing|er)|public relations|communications?|"
    r"marketing|sales|accounting|legal|law|healthcare policy|sustainability|"
    r"supply chain|logistics|merchandising|graphic design|industrial design|"
    r"product manager|product management|project controls?|administrative|"
    r"customer service)\b"
)

PHD_ONLY_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:ph\.?d\.?|doctoral|postdoc(?:toral)?)\s+"
    r"(?:only|required|students?|candidates?|(?:research\s+)?intern(?:ship)?)\b"
    r"|[-–—:/()]\s*(?:ph\.?d\.?|doctoral)\s*(?:role|track|intern(?:ship)?)?\s*$"
    r"|\b(?:masters?|graduate)\s+(?:or|and)\s+(?:ph\.?d\.?|doctoral)\b"
    r")"
)
US_CITIZEN_ONLY_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:u\.?s\.?|united\s+states)\s+citizens?(?:hip)?\s*(?:only|required)\b"
    r"|\bmust\s+be\s+(?:a\s+)?(?:u\.?s\.?|united\s+states)\s+citizen\b"
    r"|\b(?:only|limited\s+to)\s+(?:u\.?s\.?|united\s+states)\s+citizens?\b"
    r")"
)
PERMANENT_RESIDENT_ONLY_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:u\.?s\.?\s+)?permanent\s+residents?\s*(?:only|required)\b"
    r"|\bmust\s+be\s+(?:a\s+)?(?:u\.?s\.?\s+)?permanent\s+resident\b"
    r"|\b(?:citizenship|permanent\s+residen(?:cy|t\s+status))\s+(?:is\s+)?required\b"
    r")"
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalized_words(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).casefold()).strip()


def canonical_job_url(value: Any) -> str:
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


def stable_job_fingerprint(job: dict[str, Any]) -> str:
    company = _normalized_words(job.get("company"))
    role = _normalized_words(job.get("role"))
    url = canonical_job_url(job.get("apply_url") or job.get("url"))
    raw = f"{company}|{role}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _role_identity(job: dict[str, Any]) -> tuple[str, str]:
    return (_normalized_words(job.get("company")), _normalized_words(job.get("role")))


def eligibility_exclusion_reason(role: dict[str, Any]) -> str | None:
    """Return a conservative, explicit reason to keep a role out of the queue."""

    company = _normalized_words(role.get("company"))
    title = _text(role.get("role"))
    notes = _text(role.get("listingNotes") or role.get("listing_notes"))
    combined = f"{title} {notes}".strip()

    # This is an explicit user preference, not an inference about company size.
    if company == "tiktok" or company.startswith("tiktok "):
        return "user_excluded_tiktok"
    if PHD_ONLY_RE.search(combined):
        return "graduate_degree_only"
    if US_CITIZEN_ONLY_RE.search(combined):
        return "us_citizen_only"
    if PERMANENT_RESIDENT_ONLY_RE.search(combined):
        return "us_permanent_resident_only"
    if not TARGET_TECHNICAL_RE.search(title):
        return "outside_target_technical_profile"
    if OBVIOUS_NONTECHNICAL_RE.search(title):
        # A strong technical phrase wins only when the title is genuinely a
        # technical job (for example, "Marketing Data Analyst").
        strong = re.search(
            r"(?i)\b(?:software|developer|data (?:engineer|scientist|analyst)|"
            r"machine learning|artificial intelligence|algorithm|quantitative|"
            r"application engineer|full[ -]?stack|front[ -]?end|back[ -]?end)\b",
            title,
        )
        if not strong:
            return "obvious_nontechnical_mismatch"
    return None


def choose_resume_variant(role_title: Any) -> tuple[str, str]:
    """Choose A or P using title-only rules that can be shown to the user."""

    title = _text(role_title)
    if AI_ENGINEERING_RESUME_RE.search(title):
        return "A", "title_matches_ai_data_or_software_engineering"
    if PRODUCT_SYSTEMS_RESUME_RE.search(title):
        return "P", "title_matches_product_analytics_or_systems"
    return "P", "default_product_systems_resume"


def _extract_pdf_text(path: Path) -> tuple[str, str | None]:
    executable = shutil.which("pdftotext")
    if not executable:
        return "", "pdftotext_unavailable"
    try:
        result = subprocess.run(
            [executable, "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return "", f"resume_text_extraction_failed:{type(error).__name__}"
    return result.stdout, None


def inspect_resume(
    path: Path,
    *,
    extractor: Callable[[Path], tuple[str, str | None]] = _extract_pdf_text,
) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "ready": False, "blockedReason": "resume_missing"}
    text, error = extractor(path)
    if error:
        return {"path": str(path), "exists": True, "ready": False, "blockedReason": error}
    normalized = _text(text).casefold()
    markers = [marker for marker in UNSAFE_RESUME_MARKERS if marker in normalized]
    if markers:
        return {
            "path": str(path),
            "exists": True,
            "ready": False,
            "blockedReason": "resume_contains_manuscript_or_fictional_footer",
        }
    if not normalized:
        return {"path": str(path), "exists": True, "ready": False, "blockedReason": "resume_text_empty"}
    return {"path": str(path), "exists": True, "ready": True, "blockedReason": None}


def resolve_resume(
    variant: str,
    *,
    candidates: dict[str, Iterable[Path]] = RESUME_CANDIDATES,
    extractor: Callable[[Path], tuple[str, str | None]] = _extract_pdf_text,
) -> dict[str, Any]:
    candidate_paths = tuple(candidates.get(variant, ()))
    first_existing: dict[str, Any] | None = None
    for path in candidate_paths:
        result = inspect_resume(Path(path), extractor=extractor)
        if not result["exists"]:
            continue
        if first_existing is None:
            first_existing = result
        if result["ready"]:
            return {"variant": variant, "status": "ready", **result}
    if first_existing is not None:
        return {"variant": variant, "status": "manual_cleanup", **first_existing}
    fallback = str(candidate_paths[0]) if candidate_paths else ""
    return {
        "variant": variant,
        "status": "manual_cleanup",
        "path": fallback,
        "exists": False,
        "ready": False,
        "blockedReason": "resume_missing",
    }


def _has_submission_evidence(record: dict[str, Any]) -> bool:
    status = _normalized_words(record.get("status"))
    submitted_status = status in {
        "submitted",
        "application submitted",
        "confirmed submitted",
        "submission confirmed",
    }
    flag = record.get("success_confirmation")
    explicit_flag = flag is True or _normalized_words(flag) in {
        "true",
        "yes",
        "confirmed",
        "success",
        "submitted",
    }
    textual_evidence = bool(
        _text(record.get("confirmation")) or _text(record.get("confirmation_evidence"))
    )
    return explicit_flag or (submitted_status and textual_evidence)


def _submitted_indexes(state: dict[str, Any]) -> tuple[set[tuple[str, str]], set[str]]:
    history = state.get("intern_auto_apply")
    if not isinstance(history, dict):
        return set(), set()
    records = history.get("submitted")
    if not isinstance(records, list):
        return set(), set()
    identities: set[tuple[str, str]] = set()
    urls: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not _has_submission_evidence(record):
            continue
        identity = _role_identity(record)
        if all(identity):
            identities.add(identity)
        url = canonical_job_url(record.get("apply_url") or record.get("url"))
        if url:
            urls.add(url)
    return identities, urls


def _requirement_review_indexes(
    state: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """Index explicit, previously recorded job-detail reviews.

    Absence of a review is never interpreted as absence of a restriction.  A
    collector may populate ``requirement_reviews`` only after checking the
    actual posting; this local queue merely consumes those facts.
    """

    history = state.get("intern_auto_apply")
    records = history.get("requirement_reviews") if isinstance(history, dict) else None
    by_fingerprint: dict[str, dict[str, Any]] = {}
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(records, list):
        return by_fingerprint, by_identity
    for record in records:
        if not isinstance(record, dict):
            continue
        fp = _text(record.get("fingerprint"))
        if fp:
            by_fingerprint[fp] = record
        identity = _role_identity(record)
        if all(identity):
            by_identity[identity] = record
    return by_fingerprint, by_identity


def _review_is_complete(review: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(review, dict)
        and review.get("eligibility_verified") is True
        and review.get("application_limit_checked") is True
    )


def _review_exclusion_reason(review: dict[str, Any]) -> str:
    raw = _normalized_words(review.get("ineligible_reason"))
    if "citizen" in raw:
        return "us_citizen_only"
    if "permanent resident" in raw or "green card" in raw:
        return "us_permanent_resident_only"
    if "phd" in raw or "doctoral" in raw or "graduate" in raw:
        return "graduate_degree_only"
    return "reviewed_ineligible"


def build_preparation_queue(
    messages: Iterable[Any],
    state: dict[str, Any],
    now: datetime,
    *,
    resume_resolver: Callable[[str], dict[str, Any]] = resolve_resume,
) -> dict[str, Any]:
    """Return a deterministic local queue and an exclusion audit.

    Only roles from messages that pass ``collect_verified_roles``' strict sender,
    subject-count, parsed-count, and date checks can reach this function's queue.
    """

    summaries, roles = collect_verified_roles(messages)
    verified_dates = {summary.get("date") for summary in summaries if summary.get("countVerified")}
    submitted_identities, submitted_urls = _submitted_indexes(state)
    reviews_by_fingerprint, reviews_by_identity = _requirement_review_indexes(state)

    history = state.get("intern_auto_apply")
    if not isinstance(history, dict):
        history = {}
    prior_queue = history.get("preparation_queue")
    prior_by_fingerprint = {
        _text(record.get("fingerprint")): record
        for record in prior_queue
        if isinstance(record, dict) and _text(record.get("fingerprint"))
    } if isinstance(prior_queue, list) else {}

    queue: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    now_iso = now.isoformat()

    # collect_verified_roles sorts newest first.  That makes de-duplication keep
    # the newest occurrence without relying on mutable inbox ordering.
    for source_role in roles:
        if source_role.get("date") not in verified_dates:
            continue
        role = {
            "company": _text(source_role.get("company")),
            "role": _text(source_role.get("role")),
            "apply_url": canonical_job_url(source_role.get("url")),
            "source": "gmail",
            "source_email_date": _text(source_role.get("date")),
            "is_limited": bool(source_role.get("isLimited")),
            "limit_count": source_role.get("limitCount"),
            "listing_notes": _text(source_role.get("listingNotes")),
        }
        fingerprint = stable_job_fingerprint(role)
        identity = _role_identity(role)
        url = role["apply_url"]

        exclusion_reason = eligibility_exclusion_reason({**source_role, **role})
        if exclusion_reason:
            excluded.append({**role, "fingerprint": fingerprint, "reason": exclusion_reason})
            continue
        if not url:
            excluded.append({**role, "fingerprint": fingerprint, "reason": "missing_or_unsafe_url"})
            continue
        if identity in submitted_identities or url in submitted_urls:
            excluded.append({**role, "fingerprint": fingerprint, "reason": "already_submitted"})
            continue
        if identity in seen_identities or url in seen_urls:
            excluded.append({**role, "fingerprint": fingerprint, "reason": "duplicate_role"})
            continue
        seen_identities.add(identity)
        seen_urls.add(url)

        big = bool(source_role.get("isBigCompany")) or is_big_company(role["company"])
        review = reviews_by_fingerprint.get(fingerprint) or reviews_by_identity.get(identity)
        if _review_is_complete(review) and review.get("eligible") is not True:
            excluded.append(
                {
                    **role,
                    "fingerprint": fingerprint,
                    "reason": _review_exclusion_reason(review),
                }
            )
            continue
        reviewed_limit = bool(
            _review_is_complete(review)
            and (
                review.get("has_application_limit") is True
                or (
                    type(review.get("application_limit")) is int
                    and int(review.get("application_limit")) > 0
                )
            )
        )
        if role["is_limited"]:
            queue_status = "manual_decision"
            decision_reason = "application_limit"
        elif reviewed_limit:
            queue_status = "manual_decision"
            decision_reason = "application_limit"
        elif big:
            queue_status = "manual_decision"
            decision_reason = "big_company"
        elif not _review_is_complete(review):
            queue_status = "manual_review"
            decision_reason = "job_requirements_fetch_required"
        else:
            queue_status = "prepared"
            decision_reason = "eligible_smaller_company_requirements_verified"

        resume_variant, resume_match_reason = choose_resume_variant(role["role"])
        resume = resume_resolver(resume_variant)
        prior = prior_by_fingerprint.get(fingerprint, {})
        first_seen_at = _text(prior.get("first_seen_at")) or now_iso
        queue.append(
            {
                **role,
                "fingerprint": fingerprint,
                "queue_status": queue_status,
                "decision_reason": decision_reason,
                "first_seen_at": first_seen_at,
                "last_seen_at": now_iso,
                "submission_attempted": False,
                "resume_variant": resume_variant,
                "resume_match_reason": resume_match_reason,
                "resume_path": _text(resume.get("path")),
                "resume_status": _text(resume.get("status")) or "manual_cleanup",
                "resume_attachment_ready": bool(resume.get("ready")),
                "resume_blocked_reason": resume.get("blockedReason"),
            }
        )

    queue.sort(
        key=lambda item: (
            0 if item["queue_status"] == "manual_decision" else 1,
            item["source_email_date"],
            _normalized_words(item["company"]),
            _normalized_words(item["role"]),
            item["apply_url"],
        )
    )
    excluded.sort(
        key=lambda item: (
            item["source_email_date"],
            _normalized_words(item["company"]),
            _normalized_words(item["role"]),
            item["reason"],
        )
    )

    return {
        "schema_version": 1,
        "generated_at": now_iso,
        "verified_email_dates": sorted(date for date in verified_dates if date),
        "queue": queue,
        "excluded": excluded,
        "stats": {
            "verified_roles": len(roles),
            "queued": len(queue),
            "manual_decision": sum(item["queue_status"] == "manual_decision" for item in queue),
            "manual_review": sum(item["queue_status"] == "manual_review" for item in queue),
            "prepared": sum(item["queue_status"] == "prepared" for item in queue),
            "resume_ready": sum(item["resume_attachment_ready"] for item in queue),
            "resume_cleanup_required": sum(not item["resume_attachment_ready"] for item in queue),
            "excluded": len(excluded),
            "submission_attempted": False,
            "submitted": 0,
        },
    }


def _safe_source_url(value: Any) -> str | None:
    """Keep a navigable HTTPS inbox link without credentials or query secrets."""

    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    # Gmail thread links use an ordinary fragment.  Preserve it, but reject
    # parameter-like fragments that could carry an OAuth credential.
    fragment = parsed.fragment
    lowered_fragment = fragment.casefold()
    if any(
        marker in lowered_fragment
        for marker in ("access_token=", "id_token=", "code=", "credential=", "password=")
    ):
        fragment = ""
    return urlunparse(("https", parsed.netloc.casefold(), parsed.path or "/", "", "", fragment))


def build_preparation_summary(
    messages: Iterable[Any],
    state: dict[str, Any],
    now: datetime,
    *,
    source_reused: bool = False,
    resume_resolver: Callable[[str], dict[str, Any]] = resolve_resume,
) -> dict[str, Any] | None:
    """Build a sanitized, latest-update summary for renderers and widgets.

    The preparation queue remains the source of truth.  This projection omits
    message bodies, resume paths, and application history, and never mutates
    ``state``.  It deliberately summarizes only the newest count-verified email
    date so its counts reconcile with the small widget's displayed total.
    """

    message_list = [item for item in messages if isinstance(item, dict)]
    daily, _ = collect_verified_roles(message_list)
    if not daily:
        return None
    latest = daily[0]
    report_date = _text(latest.get("date"))
    if not report_date or latest.get("countVerified") is not True:
        return None

    latest_messages: list[dict[str, Any]] = []
    source_url: str | None = None
    for message in message_list:
        analysis = analyze_message(message)
        if not analysis or analysis.get("date") != report_date:
            continue
        latest_messages.append(message)
        source_url = source_url or _safe_source_url(message.get("url"))

    resume_cache: dict[str, dict[str, Any]] = {}

    def cached_resume(variant: str) -> dict[str, Any]:
        if variant not in resume_cache:
            resume_cache[variant] = resume_resolver(variant)
        return resume_cache[variant]

    result = build_preparation_queue(
        latest_messages,
        state,
        now,
        resume_resolver=cached_resume,
    )
    stats = result["stats"]
    verified_total = int(latest.get("totalUniqueRoles") or 0)
    if int(stats.get("verified_roles") or 0) != verified_total:
        # Fail closed if the queue and independently verified daily aggregate
        # ever disagree; consumers must not present partial data as complete.
        return None

    items: list[dict[str, Any]] = []
    for item in result["queue"]:
        items.append(
            {
                "fingerprint": _text(item.get("fingerprint")),
                "company": _text(item.get("company")),
                "role": _text(item.get("role")),
                "apply_url": canonical_job_url(item.get("apply_url")),
                "source_email_date": _text(item.get("source_email_date")),
                "queue_status": _text(item.get("queue_status")),
                "decision_reason": _text(item.get("decision_reason")),
                "is_limited": bool(item.get("is_limited")),
                "limit_count": item.get("limit_count"),
                "resume_variant": _text(item.get("resume_variant")),
                "resume_attachment_ready": bool(item.get("resume_attachment_ready")),
            }
        )
    for item in result["excluded"]:
        items.append(
            {
                "fingerprint": _text(item.get("fingerprint")),
                "company": _text(item.get("company")),
                "role": _text(item.get("role")),
                "apply_url": canonical_job_url(item.get("apply_url")),
                "source_email_date": _text(item.get("source_email_date")),
                "queue_status": "excluded",
                "decision_reason": _text(item.get("reason")),
                "is_limited": bool(item.get("is_limited")),
                "limit_count": item.get("limit_count"),
                "resume_variant": "",
                "resume_attachment_ready": False,
            }
        )

    is_current_day = report_date == now.date().isoformat()
    if source_reused:
        freshness = "reused_previous_verified"
    elif is_current_day:
        freshness = "current_day"
    else:
        freshness = "prior_verified_update"

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "report_date": report_date,
        "count_verified": True,
        "verified_role_count": verified_total,
        "queued_count": int(stats.get("queued") or 0),
        "manual_decision_count": int(stats.get("manual_decision") or 0),
        "manual_review_count": int(stats.get("manual_review") or 0),
        "prepared_count": int(stats.get("prepared") or 0),
        "excluded_count": int(stats.get("excluded") or 0),
        "source_url": source_url,
        "source_reused": bool(source_reused),
        "is_current_day": bool(is_current_day and not source_reused),
        "freshness": freshness,
        "submission_attempted": False,
        "submitted_count": 0,
        "items": items,
    }


STRICT_QUEUE_STATUSES = {"manual_decision", "manual_review", "prepared", "excluded"}


def strict_display_items(
    messages: Iterable[Any],
    preparation_summary: Any,
    *,
    zone: Any = None,
) -> list[dict[str, Any]]:
    """Join strict queue decisions to verified raw roles by canonical URL.

    This is the single display-layer gate used by both the wallpaper and the
    Widget snapshot.  Any missing or internally inconsistent strict projection
    returns no cards, rather than falling back to title-only READY/Big-Co
    guesses.  Excluded rows remain available in the Markdown source list but
    can never become visual action cards.
    """

    if not isinstance(preparation_summary, dict):
        return []
    if preparation_summary.get("count_verified") is not True:
        return []
    report_date = _text(preparation_summary.get("report_date"))
    verified_total = preparation_summary.get("verified_role_count")
    strict_rows = preparation_summary.get("items")
    if not report_date or type(verified_total) is not int or not isinstance(strict_rows, list):
        return []
    if len(strict_rows) != verified_total:
        return []

    expected_counts = {
        "manual_decision": preparation_summary.get("manual_decision_count"),
        "manual_review": preparation_summary.get("manual_review_count"),
        "prepared": preparation_summary.get("prepared_count"),
        "excluded": preparation_summary.get("excluded_count"),
    }
    if any(type(value) is not int or value < 0 for value in expected_counts.values()):
        return []
    if sum(expected_counts.values()) != verified_total:
        return []

    message_list = [message for message in messages if isinstance(message, dict)]
    if zone is None:
        daily, raw_roles = collect_verified_roles(message_list)
    else:
        daily, raw_roles = collect_verified_roles(message_list, zone)
    raw_daily = next(
        (item for item in daily if item.get("date") == report_date and item.get("countVerified") is True),
        None,
    )
    latest_roles = [role for role in raw_roles if _text(role.get("date")) == report_date]
    if not raw_daily or int(raw_daily.get("totalUniqueRoles") or 0) != verified_total:
        return []

    raw_by_url: dict[str, list[dict[str, Any]]] = {}
    for role in latest_roles:
        url = canonical_job_url(role.get("url"))
        if not url:
            return []
        raw_by_url.setdefault(url, []).append(role)

    observed_counts = {key: 0 for key in expected_counts}
    normalized_rows: list[dict[str, Any]] = []
    for row in strict_rows:
        if not isinstance(row, dict):
            return []
        status = _text(row.get("queue_status"))
        if status not in STRICT_QUEUE_STATUSES:
            return []
        observed_counts[status] += 1
        url = canonical_job_url(row.get("apply_url"))
        if not url or url not in raw_by_url:
            return []
        normalized_rows.append({**row, "queue_status": status, "apply_url": url})
    if observed_counts != expected_counts:
        return []

    # The queue precedes exclusions in the projection.  Still sort explicitly
    # so an excluded duplicate can never shadow its actionable canonical URL.
    status_rank = {"manual_decision": 0, "manual_review": 1, "prepared": 2, "excluded": 3}
    normalized_rows.sort(key=lambda row: status_rank[row["queue_status"]])
    output: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in normalized_rows:
        status = row["queue_status"]
        if status == "excluded":
            continue
        url = row["apply_url"]
        if url in seen_urls:
            continue
        raw_candidates = raw_by_url[url]
        strict_identity = _role_identity(row)
        raw = next(
            (candidate for candidate in raw_candidates if _role_identity(candidate) == strict_identity),
            raw_candidates[0],
        )
        seen_urls.add(url)
        output.append(
            {
                "source": "gmail",
                "sender_display": "SWE List",
                "subject": "",
                "company": _text(raw.get("company")),
                "role": _text(raw.get("role")),
                "apply_url": url,
                "received_at": report_date,
                "source_email_date": report_date,
                "queue_status": status,
                "decision_reason": _text(row.get("decision_reason")),
                "is_limited": bool(row.get("is_limited")),
                "limit_count": row.get("limit_count"),
                "is_big": _text(row.get("decision_reason")) == "big_company",
                "should_auto_apply": status == "prepared",
                "resume_variant": _text(row.get("resume_variant")),
                "resume_attachment_ready": bool(row.get("resume_attachment_ready")),
                "count_verified": True,
            }
        )

    if len(output) != int(preparation_summary.get("queued_count") or 0):
        return []
    return output
