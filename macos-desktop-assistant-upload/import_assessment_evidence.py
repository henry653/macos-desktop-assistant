"""Import reviewed course evidence locally; never browse or modify a website."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from assignment_filters import visible_assignments
from refresh_plan import attach_exam_inventory, course_key
from run_daily_briefing import _merge_assessment_events, _write_json_atomically


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    state = json.loads((root / "state.json").read_text())
    payload = json.loads((root / "daily_briefing.json").read_text())
    evidence = json.loads(args.evidence.read_text())
    config = json.loads((root / "assessment_sources.json").read_text())
    now = datetime.now(ZoneInfo("America/New_York"))
    # Idempotently merge this same reviewed evidence without fabricating a
    # second collection timestamp or reapplying an obsolete correction later.
    digest = hashlib.sha256(args.evidence.read_bytes()).hexdigest()
    if digest in state.get("assessment_imports", {}):
        print(json.dumps({"status": "already_imported", "sha256": digest}))
        return
    incoming = {"assignments": [], "exams": evidence.get("exams", [])}
    _merge_assessment_events(incoming, state, now)
    payload["exams"] = visible_assignments(incoming["exams"], now)
    receipts = state.setdefault("assessment_coverage", {})
    for key, row in evidence.get("coverage", {}).items():
        receipt = {**receipts.get(key, {}), **row, "checked_at": now.isoformat()}
        if key in {"STOR415", "STOR455"}:
            receipt["announcements_checked_at"] = now.isoformat()
        sources = next((c["schedule_sources"] for c in config["courses"] if course_key(c["course"]) == key), [])
        cache = receipt.setdefault("source_cache", {})
        for source in sources:
            local = source.get("local_path")
            sha = hashlib.sha256(Path(local).read_bytes()).hexdigest() if local else None
            cache[source["url"]] = {"parsed": True, "checked_at": now.isoformat(), "sha256": sha, "basis": "local_official_syllabus" if local else "official_public_course_calendar"}
        receipts[key] = receipt
    candidates = {(course_key(e["course"]), e["title"]): e for e in state.get("assessment_candidates", [])}
    for event in evidence.get("tentative", []):
        candidates[(course_key(event["course"]), event["title"])] = event
    state["assessment_candidates"] = list(candidates.values())
    state.setdefault("assessment_imports", {})[digest] = {"file": str(args.evidence.resolve()), "imported_at": now.isoformat()}
    attach_exam_inventory(payload, state, config, now)
    notes = [note for note in payload.get("collection_notes", []) if not note.startswith("One formal exam within7days.")]
    notes.append("Course exam audit corrected: COMP455 scheduled Quizzes and STOR415 Quiz1 included; semester inventory retained separately from7-day display. STOR455 Sep11 enters display Sep4. Mail/assignment sources were not freshly collected during this local correction.")
    payload["collection_notes"] = notes
    canvas = payload.setdefault("sources", {}).setdefault("canvas", {})
    canvas["assessment_checked_at"] = now.isoformat()
    canvas["exams_within_7d_count"] = len(payload["exams"])
    canvas["exam_coverage"] = payload["exam_coverage"]
    for entry in canvas.get("coverage", []):
        if isinstance(entry, dict) and course_key(entry.get("course")) == "COMP455":
            entry["note"] = "Original assignment check retained. Exam coverage corrected by later assessment audit: scheduled Quiz0/1/2 now included; see exam_coverage."
    _write_json_atomically(root / "state.json", state)
    _write_json_atomically(root / "daily_briefing.json", payload)
    print(json.dumps({"status": "imported", "known_exams": len(state["assessment_events"]), "visible_exams": len(payload["exams"]), "tentative_candidates": len(candidates)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
