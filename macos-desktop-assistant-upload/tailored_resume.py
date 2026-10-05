#!/usr/bin/env python3
"""Generate a local PDF resume by selecting only user-authored facts.

This tool never reads mail, calls a model, or uploads data. Tailoring changes
ordering and selection based on the supplied job text; it never rewrites facts.
"""

from __future__ import annotations

import argparse
import json
import re
from html import escape
from pathlib import Path
from typing import Any


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z][a-z0-9+#.-]{2,}", value.casefold())
            if token not in {"and", "the", "with", "for", "from", "that", "this"}}


def _score(text: str, job_tokens: set[str]) -> int:
    return len(_tokens(text) & job_tokens)


def _clean(value: Any, label: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {label}")
    return value.strip()


def tailor(profile: dict[str, Any], job_text: str) -> dict[str, Any]:
    """Return a plan containing original strings, never invented claims."""
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    name = _clean(profile.get("name"), "name", 100)
    contact = _clean(profile.get("contact"), "contact", 180)
    summary = profile.get("summary", "")
    if summary and (not isinstance(summary, str) or len(summary) > 800):
        raise ValueError("Invalid summary")
    job_tokens = _tokens(_clean(job_text, "job text", 30000))
    sections: list[dict[str, Any]] = []
    for section_name in ("experience", "projects", "education"):
        raw_section = profile.get(section_name, [])
        if not isinstance(raw_section, list) or len(raw_section) > 30:
            raise ValueError(f"Invalid {section_name}")
        prepared: list[dict[str, Any]] = []
        for raw in raw_section:
            if not isinstance(raw, dict):
                raise ValueError(f"Invalid {section_name} entry")
            title = _clean(raw.get("title"), "title", 180)
            details = raw.get("details", "")
            if details and (not isinstance(details, str) or len(details) > 240):
                raise ValueError("Invalid details")
            bullets = raw.get("bullets", [])
            if not isinstance(bullets, list) or len(bullets) > 20:
                raise ValueError("Invalid bullets")
            clean_bullets = [_clean(bullet, "bullet", 600) for bullet in bullets]
            ranked = sorted(enumerate(clean_bullets), key=lambda pair: (-_score(pair[1], job_tokens), pair[0]))
            selected = [bullet for _, bullet in ranked[:4]]
            score = _score(title + " " + details, job_tokens) + sum(_score(b, job_tokens) for b in selected)
            prepared.append({"title": title, "details": details, "bullets": selected, "score": score})
        if section_name == "projects":
            prepared.sort(key=lambda item: -item["score"])
            prepared = prepared[:4]
        sections.append({"name": section_name, "entries": prepared})
    skills = profile.get("skills", [])
    if not isinstance(skills, list) or len(skills) > 100:
        raise ValueError("Invalid skills")
    clean_skills = [_clean(skill, "skill", 80) for skill in skills]
    clean_skills.sort(key=lambda skill: -_score(skill, job_tokens))
    return {"name": name, "contact": contact, "summary": summary.strip(),
            "skills": clean_skills, "sections": sections}


def generate_pdf(plan: dict[str, Any], output: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether

    if output.suffix.casefold() != ".pdf":
        raise ValueError("Output must be a PDF")
    output.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ResumeName", parent=styles["Title"], alignment=TA_CENTER, fontSize=17, leading=21))
    styles.add(ParagraphStyle(name="ResumeContact", parent=styles["Normal"], alignment=TA_CENTER, fontSize=8.5, leading=11))
    styles.add(ParagraphStyle(name="ResumeSection", parent=styles["Heading2"], fontSize=10.5, leading=13, textColor=colors.HexColor("#17365D"), spaceBefore=10))
    styles.add(ParagraphStyle(name="ResumeBody", parent=styles["BodyText"], fontSize=9, leading=12, spaceAfter=2))
    story: list[Any] = [Paragraph(escape(plan["name"]), styles["ResumeName"]),
                        Paragraph(escape(plan["contact"]), styles["ResumeContact"]), Spacer(1, 8)]
    if plan["summary"]:
        story.append(Paragraph(escape(plan["summary"]), styles["ResumeBody"]))
    for section in plan["sections"]:
        if not section["entries"]:
            continue
        story.append(Paragraph(section["name"].title(), styles["ResumeSection"]))
        for entry in section["entries"]:
            parts = [Paragraph(f"<b>{escape(entry['title'])}</b> {escape(entry['details'])}", styles["ResumeBody"])]
            parts.extend(Paragraph("• " + escape(bullet), styles["ResumeBody"]) for bullet in entry["bullets"])
            story.append(KeepTogether(parts))
    if plan["skills"]:
        story.append(Paragraph("Skills", styles["ResumeSection"]))
        story.append(Paragraph(escape(" · ".join(plan["skills"])), styles["ResumeBody"]))
    SimpleDocTemplate(str(output), pagesize=letter, leftMargin=42, rightMargin=42,
                      topMargin=36, bottomMargin=36, title=f"Resume - {plan['name']}").build(story)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path, help="Local user-authored JSON profile")
    parser.add_argument("--job-text", required=True, type=Path, help="Local job description text")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Show a selection plan without writing a PDF")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    plan = tailor(profile, args.job_text.read_text(encoding="utf-8"))
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    generate_pdf(plan, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
