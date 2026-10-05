#!/usr/bin/env python3
"""Build reproducible, captioned feature walkthroughs from fictional fixtures.

Runs the real renderer, parsers, filters, queue, API and PDF generator. The
videos are illustrated walkthroughs, not a recording of live account access.
No credential, mailbox, application website, or desktop setting is accessed.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html as html_lib
import io
import json
import math
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import assignment_filters as filters
import intern_preparation as preparation
import local_api
import plugin_sources
import refresh_plan
import render_daily_briefing as renderer
import render_my_planner
import run_daily_briefing as runner
import tailored_resume

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=ZoneInfo("America/New_York"))
MEDIA = ROOT / "docs/media"
WORK = ROOT / ".demo-work"
W, H, FPS, SECONDS = 1280, 800, 10, 16
NAVY, PANEL, TEXT, MUTED, MINT = "#091421", "#132337", "#f3f7fc", "#adbed0", "#7de3c5"


def font(size: int, mono: bool = False):
    candidates = (["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"] if mono else
                  ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fit_lines(draw, text, size, width):
    result = []
    for paragraph in str(text).splitlines():
        words, line = paragraph.split(), ""
        for word in words:
            candidate = (line + " " + word).strip()
            if line and draw.textlength(candidate, font=font(size)) > width:
                result.append(line)
                line = word
            else:
                line = candidate
        result.append(line)
    return result


def paragraph(draw, text, box, size=24, color=TEXT, line_gap=10):
    x, y, width, height = box
    lines = fit_lines(draw, text, size, width)
    if len(lines) * (size + line_gap) > height:
        raise ValueError(f"Text overflows its frame: {text[:80]}")
    for line in lines:
        draw.text((x, y), line, font=font(size), fill=color)
        y += size + line_gap


def terminal_image(lines, title="Actual output from the demonstration run"):
    im = Image.new("RGB", (1120, 570), PANEL)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, 1119, 569), radius=20, outline="#37536f", width=2)
    d.text((28, 20), title, font=font(22), fill=MINT)
    y = 76
    for line in lines:
        for wrapped in textwrap.wrap(str(line), width=81, replace_whitespace=False) or [""]:
            if y > 530:
                raise ValueError("Terminal evidence exceeds frame")
            d.text((28, y), wrapped, font=font(20, True), fill=TEXT)
            y += 29
    return im


def cut_card(wallpaper, manifest, kinds):
    cards = [h for h in manifest["hotspots"] if h["kind"] in kinds]
    if not cards:
        return wallpaper
    left = min(h["rect"]["x"] for h in cards)
    top = min(h["rect"]["y"] for h in cards)
    right = max(h["rect"]["x"] + h["rect"]["width"] for h in cards)
    bottom = max(h["rect"]["y"] + h["rect"]["height"] for h in cards)
    return wallpaper.crop((max(0,left-16), max(0,top-16), min(wallpaper.width,right+16), min(wallpaper.height,bottom+16)))


def evidence():
    WORK.mkdir(exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    data = json.loads((ROOT / "examples/daily_briefing.example.json").read_text())
    data["sources"]["outlook"] = {"state": "partial", "checked_at": None, "error": "demo_auth_required"}
    lines = [
        "Tiny Labs: [Data Engineer Intern](https://jobs.example/tiny)",
        "Google: [Software Engineer Intern](https://jobs.example/google)",
        "Limited Co: [Software Engineer Intern](https://jobs.example/limited) — limited to 2 applications",
        "Research Co: [Research Intern - PhD only](https://jobs.example/phd)",
    ]
    body = "\n".join(lines)
    fixture = {"id": "fictional-demo", "threadId": "fictional-demo", "internalDate": str(int(NOW.timestamp()*1000)),
               "labelIds": ["UNREAD"], "payload": {"mimeType": "text/plain", "headers": [
                   {"name": "From", "value": "SWE List <noreply@swelist.com>"},
                   {"name": "Subject", "value": "4 New Internships Posted Today"}],
                   "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
    swe = runner._message_to_item(fixture, NOW)
    valid = runner._validate_swe_message_counts([swe], renderer)
    bad = copy.deepcopy(swe)
    bad["subject"] = "5 New Internships Posted Today"
    invalid = runner._validate_swe_message_counts([bad], renderer)
    assert valid["count_verified"] and not invalid["count_verified"]
    assert fixture["labelIds"] == ["UNREAD"]
    data["messages"].append(swe)
    # No mock eligibility review is inserted: unknown job requirements stay unknown.
    missing_resume = lambda v: {"variant": v, "status": "missing", "ready": False, "exists": False,
                                "path": "", "blockedReason": "demo_no_personal_resume"}
    queue = preparation.build_preparation_queue([swe], {}, NOW, resume_resolver=missing_resume)
    data["intern_preparation_summary"] = preparation.build_preparation_summary([swe], {}, NOW, resume_resolver=missing_resume)
    assert all(not item["submission_attempted"] for item in queue["queue"])
    later = copy.deepcopy(data["exams"][0])
    later.update(title="Final Exam", exam_at="2026-12-08T14:00:00-05:00", exam_date="2026-12-08", exam_end_at="2026-12-08T16:00:00-05:00")
    state = {"assessment_events": data["exams"]+[later], "assessment_coverage": {"EXAMPLE101": {
        "state": "partial", "coverage_note": "Fictional syllabus parsed; announcements still need checking.",
        "pending": ["announcements"]}}}
    config = {"courses": [{"course": "EXAMPLE 101", "announcements_url": "https://example.edu/course/announcements", "schedule_sources": []}]}
    refresh_plan.attach_exam_inventory(data, state, config, NOW)
    plan = refresh_plan.build_refresh_plan(state, config, NOW)
    candidates = copy.deepcopy(data["assignments"])
    candidates += [dict(candidates[0], title="Expired assignment", due_at="2026-10-01T17:00:00-04:00"),
                   dict(candidates[0], title="Submitted assignment", status="submitted"),
                   dict(candidates[0], title="Outside 72 hours", due_at="2026-10-08T18:00:00-04:00")]
    visible, overdue = renderer.split_assignments(candidates, NOW)
    assert len(visible) == 1 and overdue == []
    plugin_dir = WORK / "plugins"
    plugin = json.loads((ROOT / "plugins/campus.example.json").read_text())
    plugin["enabled"] = True
    save_json(plugin_dir / "campus.json", plugin)
    links, errors = plugin_sources.load_resource_plugins(plugin_dir)
    assert len(links) == 1 and not errors
    data["resource_links"] = links
    hotspots = []
    wallpaper = renderer.render(data, hotspot_sink=hotspots).convert("RGB")
    wallpaper.save(MEDIA / "wallpaper.png")
    manifest = renderer.build_hotspot_manifest(generated_at=NOW, image_path=MEDIA / "wallpaper.png", hotspots=hotspots)
    save_json(WORK / "demo-report.json", data)
    save_json(WORK / "manifest.json", manifest)
    html, html_stats = render_my_planner.render_html(data, NOW)
    (MEDIA / "planner-demo.html").write_text('\n'.join(line.rstrip() for line in html.splitlines())+'\n', encoding="utf-8")
    (MEDIA / "briefing-demo.md").write_text(renderer.write_markdown(data), encoding="utf-8")
    profile = json.loads((ROOT / "examples/profile.json").read_text())
    resume_plan = tailored_resume.tailor(profile, (ROOT / "examples/job.txt").read_text())
    tailored_resume.generate_pdf(resume_plan, MEDIA / "resume-demo.pdf")
    subprocess.run(["pdftoppm", "-f", "1", "-singlefile", "-scale-to", "1250", "-png",
                    str(MEDIA / "resume-demo.pdf"), str(WORK / "resume")], check=True)
    # Feed real HTTP request bytes to the actual handler without opening a
    # listening socket. This also works in environments that disallow binding.
    class MemoryTransport:
        def __init__(self, request):
            self.input = io.BytesIO(request)
            self.output = bytearray()

        def makefile(self, *args, **kwargs):
            return self.input

        def sendall(self, chunk):
            self.output.extend(chunk)

    api_results = {}
    for method, url in [("GET", "/health"), ("GET", "/v1/opportunities"), ("POST", "/v1/opportunities")]:
        transport = MemoryTransport(f"{method} {url} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        local_api.handler_for(WORK / "demo-report.json")(transport, ("127.0.0.1", 0), None)
        header, body = bytes(transport.output).split(b"\r\n\r\n", 1)
        api_results[method+" "+url] = {"status": int(header.split()[1]), "body": json.loads(body)}
    assert api_results["GET /v1/opportunities"]["body"]["count"] == 3
    assert api_results["POST /v1/opportunities"]["status"] == 405
    provenance = {"mode": "illustrated_walkthrough", "data": "fictional", "live_mail_access": False,
                  "desktop_settings_changed": False, "generated_at": "2026-10-02", "fixtures_at": NOW.isoformat(),
                  "swe_valid": valid, "swe_mismatch": invalid,
                  "assignment_filter": {"input": len(candidates), "visible": len(visible), "expired_panel": len(overdue)},
                  "queue": [{k:v for k,v in q.items() if k in {"company","role","queue_status","decision_reason","submission_attempted"}} for q in queue["queue"]],
                  "api": api_results, "api_test_transport": "in_memory_http_handler", "html_stats": html_stats, "hotspot_count": len(hotspots),
                  "wallpaper_sha256": manifest["image_sha256"],
                  "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("*.py")}}
    save_json(MEDIA / "evidence.json", provenance)
    return data, wallpaper, manifest, queue, plan, resume_plan, api_results, provenance


def chapters(values):
    data, wall, manifest, queue, plan, resume_plan, api, proof = values
    image = lambda kinds: cut_card(wall, manifest, kinds)
    term = terminal_image
    return [
      {"slug":"01-dashboard", "title":"Your desktop, organized", "summary":"Render one briefing as wallpaper, an HTML planner and a full report.", "steps":[
        ("1 / Provide a structured briefing", "Use examples/daily_briefing.example.json to try the app without connecting an account.", term(["python render_daily_briefing.py", "  --input examples/daily_briefing.example.json", "  --png /tmp/planner.png --markdown /tmp/planner.md", "  --html /tmp/planner.html --hotspots /tmp/hotspots.json"], "Run this command with your local Python environment")),
        ("2 / Read the generated dashboard", "Exams receive priority, followed by imminent assignments, important messages and recruiting.", wall),
        ("3 / Open the local planner", "The HTML and Markdown outputs preserve the details and individual source links.", term([f"HTML rendered: {len((MEDIA/'planner-demo.html').read_text()):,} characters", f"Hotspots: {len(manifest['hotspots'])}", "Outputs: wallpaper.png / planner-demo.html / briefing-demo.md", "Source states remain explicit (demo / partial).", "The demo does not change your wallpaper."]))]},
      {"slug":"02-deadlines-exams", "title":"Know what is due next", "summary":"Show seven days of exams and 72 hours of actionable assignments.", "steps":[
        ("1 / Review upcoming assessments", "Confirmed exams link to their verified review material; missing material falls back to the course source.", image({"exam","assignment"})),
        ("2 / Filter completed and expired work", "The real filter removes submitted work, expired assignments and work outside the 72-hour window.", term([f"Fictional input assignments: {proof['assignment_filter']['input']}", f"Visible assignments: {proof['assignment_filter']['visible']}", "Visible title: Problem set", "Expired assignment: removed", "Submitted assignment: removed", "Outside 72 hours: removed", "Overdue panel: absent"])),
        ("3 / Keep the whole exam inventory", "An exam stays recorded even when it is outside the seven-day display window. Coverage remains partial until all sources are checked.", term([f"Known future exams: {len(data['exam_inventory'])}", f"Visible within seven days: {data['exam_coverage'][0]['visible_7d_count']}", "Next: Midterm Exam / October 6", "Later: Final Exam / December 8", "Course coverage: partial", "Pending: announcements"]))]},
      {"slug":"03-option-navigation", "title":"Reach cards with Option", "summary":"Hold Option to interact; double-tap Option to open the foreground planner.", "illustration":True, "steps":[
        ("1 / Leave the desktop click-through", "With Option released, the transparent overlay lets Finder receive your clicks.", wall),
        ("2 / Hold Option and choose a card", "Highlighted rectangles use the renderer's actual hotspot coordinates. Each card has its own destination.", wall),
        ("3 / Double-tap Option", "Open the full HTML planner in the foreground when another window covers the desktop. This is an interaction illustration, not a live screen recording.", term(["Hold Option -> reveal and enable card hotspots", "Release Option -> return clicks to Finder", "Double-tap Option -> open the local HTML planner", f"Actual generated hotspot count: {len(manifest['hotspots'])}", "Requires the running native app and verified wallpaper.", "Lock-screen wallpaper is display-only."]))]},
      {"slug":"04-important-messages", "title":"See messages needing attention", "summary":"Show concise role, action and urgency summaries with honest source status.", "steps":[
        ("1 / Supply reviewed message summaries", "The renderer accepts source, sender role, action, urgency and a safe source link.", term(["source: gmail", "sender_display: Course staff", "priority: P1", "summary: Please review the updated schedule.", "received_at: 2026-10-01 16:00 EDT", "A source collector supplies these reviewed fields."])),
        ("2 / Read the compact message card", "The dashboard shows the action summary without placing the full email body on the wallpaper.", image({"message"})),
        ("3 / Check source status", "A failed connection is displayed as partial or unavailable; it is not interpreted as an empty inbox.", term(["Gmail: fictional demo data", "Outlook: partial / demo_auth_required", "Canvas/Outlook/LinkedIn collectors: separately configured", "Click the source chip to open its application."]))]},
      {"slug":"05-swe-list", "title":"Verify daily internship updates", "summary":"Parse real email formats and reconcile the headline count with unique job links.", "steps":[
        ("1 / Configure Gmail read-only access", "Provide your own OAuth client and complete consent. Scheduled collection searches the SWE List sender; this demo uses a local mail fixture.", term(["Sender filter: from:noreply@swelist.com", "Scope: https://www.googleapis.com/auth/gmail.readonly", "DESKTOP_ASSISTANT_GMAIL_CREDENTIALS=/private/client.json", "python run_daily_briefing.py --authorize", "Consent is interactive; credentials are never published."], "Setup instructions (not performed in this demonstration)")),
        ("2 / Reconcile every posting link", "The actual parser extracts company, role and application URL from the fictional update, then verifies the count.", term(["Subject: 4 New Internships Posted Today", "Expected: 4   Parsed unique roles: 4", f"count_verified: {proof['swe_valid']['count_verified']}", "Tiny Labs / Data Engineer Intern", "Google / Software Engineer Intern", "Limited Co / Software Engineer Intern", "Research Co / Research Intern - PhD only"])),
        ("3 / Reject incomplete results", "Changing the fixture headline to five while retaining four links makes validation fail; the job queue must not treat it as a complete update.", term(["Mismatch fixture: expected 5, parsed 4", f"count_verified: {proof['swe_mismatch']['count_verified']}", "reason: role_count_mismatch", "Original fixture label: UNREAD (unchanged)", "No live Gmail mailbox was read by this demo."]))]},
      {"slug":"06-application-preparation", "title":"Prepare applications deliberately", "summary":"Keep unknown requirements, manual decisions and confirmed preparation distinct.", "steps":[
        ("1 / Build a local preparation queue", "Only count-verified opportunities enter the queue. Title rules and stable fingerprints remove exclusions and duplicates.", term([f"Verified input roles: {queue['stats']['verified_roles']}"]+[f"{q['company']}: {q['queue_status']}" for q in queue['queue']]+[f"Excluded: {len(queue['excluded'])}"])),
        ("2 / Review job requirements", "Tiny Labs remains pending. No eligibility or application-limit result is invented just to mark a role prepared.", term(["Tiny Labs: job_requirements_fetch_required", "Google: manual decision (large company)", "Limited Co: manual decision (application limit)", "Research Co: excluded (PhD-only)", "Submission attempted: false for every queue entry"])),
        ("3 / Start an interactive review", "The application-center card creates a user-visible review request. An application can only be recorded as submitted after separate confirmation and an official success state.", image({"internship","intern","intern_application","intern_control"}))]},
      {"slug":"07-campus-plugins", "title":"Add your campus resources", "summary":"Enable local JSON plugins to add useful HTTPS resource links.", "steps":[
        ("1 / Copy the example plugin", "Create a local JSON file under plugins/ and give it a unique identifier.", term(["cp plugins/campus.example.json plugins/my-campus.json", '"schema_version": 1', '"id": "my-campus"', '"enabled": true', '"resource_links": [', '  {"title": "Campus library",', '   "url": "https://example.edu/library"}', ']'], "Example configuration")),
        ("2 / Load the enabled resource", "The demo enables a copied plugin inside its own workspace and runs the real plugin loader.", term([f"Loaded resources: {len(data['resource_links'])}", "Title: Campus library", "URL: https://example.edu/library", "Plugin errors: []", "Only HTTPS URLs are accepted."])),
        ("3 / Find it in the planner", "The generated HTML planner contains the campus resource link. Plugins are data-only and do not execute arbitrary code.", term(["Campus resources -> Campus library", "Output: docs/media/planner-demo.html", "Source: plugins/my-campus.json", "Disable a resource by setting enabled to false."]))]},
      {"slug":"08-local-api", "title":"Connect another local tool", "summary":"Expose verified opportunities through a read-only loopback API.", "steps":[
        ("1 / Start the service", "Point the API at a verified report. It binds only to the local loopback interface.", term(["python local_api.py --report /path/to/report.json --port 8765", "curl http://127.0.0.1:8765/health", json.dumps(api['GET /health'])], "Setup command and actual HTTP-handler response (in memory)")),
        ("2 / Fetch verified opportunities", "The demonstration feeds HTTP requests into the real handler using a generated report. It uses in-memory transport, without opening a server port.", term(["GET /v1/opportunities", f"HTTP {api['GET /v1/opportunities']['status']}", f"count: {api['GET /v1/opportunities']['body']['count']}", "Fields: company, role, apply_url, queue_status, resume_variant", "Email body and personal contact fields: absent"])),
        ("3 / Keep the integration read-only", "Mutation requests receive an explicit 405 response. The service is not an unattended application-submission endpoint.", term(["POST /v1/opportunities", f"HTTP {api['POST /v1/opportunities']['status']}", json.dumps(api['POST /v1/opportunities']['body']), "Demonstration transport: in-memory HTTP requests"]))]},
      {"slug":"09-tailored-resume", "title":"Create a factual resume draft", "summary":"Select and reorder supplied experience for a local job description.", "steps":[
        ("1 / Supply your facts and the job text", "Use a private JSON profile and a local job-description file. The included profile is entirely fictional.", term(["python tailored_resume.py", "  --profile examples/profile.json", "  --job-text examples/job.txt", "  --output /tmp/resume-demo.pdf", "Add --dry-run to inspect selected text first."])),
        ("2 / Inspect the selected facts", "The generator ranks existing skills and bullets by overlap with the job description. It does not rewrite experience or call an external model.", term([f"Profile: {resume_plan['name']}", "Skills: "+", ".join(resume_plan['skills']), "Existing bullets selected; original strings retained.", "Resume PDF generated locally.", "No network request or application submission."])),
        ("3 / Review the PDF", "Open the generated draft and check content and layout before using it. This page is the actual output of the sample run.", Image.open(WORK/'resume.png').convert('RGB'))]},
      {"slug":"10-refresh-recovery", "title":"Refresh with visible evidence", "summary":"Plan bounded collection, preserve source failures and verify presentation separately.", "steps":[
        ("1 / Generate the refresh plan", "Inspect the plan before collecting. Announcements still need checking even when a cached syllabus is reusable.", term(["python run_daily_briefing.py --plan", f"Collection budget: {plan['budget']['collection_seconds']} seconds", f"Finalize budget: {plan['budget']['finalize_seconds']} seconds", f"Single-page load budget: {plan['budget']['page_load_seconds']} seconds", "Pending course check: announcements"])),
        ("2 / Finalize locally once", "After authenticated collection, local-only mode prepares the queue and renders. Wallpaper application is a separate explicit flag.", term(["python run_daily_briefing.py --local-only", "  --prepare-applications --set-wallpaper", "Input: collected daily_briefing.json", "Outputs: report, PNG, planner, matching hotspot manifest", "Current desktop / all Spaces / lock-screen source", "are recorded as separate verification results."], "Operational steps (wallpaper not changed in this demo)")),
        ("3 / Verify or preserve last good", "The renderer hashes the image used by its hotspot manifest. Recovery logic is tested with isolated fixtures; this video does not claim to verify a live lock screen.", term([f"Actual generated hotspots: {len(manifest['hotspots'])}", "Wallpaper and manifest image hash: MATCH", "SHA-256: "+manifest['image_sha256'][:32]+"...", "Rollback tests cover failed transitions and newer finals.", "Native wallpaper verification still requires your Mac."]))]},
    ]


def frame(chapter, stage, number, progress, manifest):
    im = Image.new("RGB", (W,H), NAVY)
    d = ImageDraw.Draw(im)
    d.text((42,26), "MY PLANNER  /  FEATURE WALKTHROUGH", font=font(17), fill=MINT)
    d.text((42,58), chapter["title"], font=font(38), fill=TEXT)
    d.text((42,110), "FICTIONAL DATA  •  ILLUSTRATED WALKTHROUGH", font=font(15), fill=MUTED)
    title, caption, source = stage
    d.rounded_rectangle((38,151,824,648),radius=20,fill=PANEL,outline="#2d435a",width=2)
    fitted = ImageOps.contain(source, (762,473), Image.Resampling.LANCZOS)
    origin = (431-fitted.width//2,400-fitted.height//2)
    im.paste(fitted, origin)
    if chapter.get("illustration") and number == 1:
        d = ImageDraw.Draw(im)
        scale = fitted.width/source.width
        for h in manifest["hotspots"]:
            r=h["rect"]
            x,y=origin[0]+r['x']*scale,origin[1]+r['y']*scale
            d.rounded_rectangle((x,y,x+r['width']*scale,y+r['height']*scale),radius=4,outline=MINT,width=2)
        d.rounded_rectangle((635,600,790,634),radius=8,fill="#24594f")
        d.text((650,607), "OPTION HELD",font=font(16),fill=TEXT)
    d = ImageDraw.Draw(im)
    paragraph(d, title, (860,173,365,114),size=26)
    paragraph(d, caption, (860,303,365,312),size=23,color=MUTED)
    d.text((42,677),f"STEP {number+1} OF 3",font=font(18),fill=MINT)
    d.text((42,715),"Runnable examples + source evidence are linked in the repository.",font=font(19),fill=MUTED)
    d.rounded_rectangle((42,763,1238,771),radius=4,fill="#26374b")
    d.rounded_rectangle((42,763,42+int(1196*max(.002,progress)),771),radius=4,fill=MINT)
    return im


def encode(chapter, manifest, include_video=True):
    slug=chapter["slug"]
    poster_stage = 2 if slug == '09-tailored-resume' else 1
    poster=frame(chapter,chapter['steps'][poster_stage],poster_stage,.55,manifest)
    poster.save(MEDIA/f"{slug}.png", optimize=True)
    # A small animated preview works directly inside GitHub Markdown.
    previews=[frame(chapter,s,i,(i+1)/3,manifest).resize((640,400),Image.Resampling.LANCZOS) for i,s in enumerate(chapter['steps'])]
    previews[0].save(MEDIA/f"{slug}.gif",save_all=True,append_images=previews[1:],duration=3500,loop=0,optimize=True)
    if not include_video:
        return
    command=["ffmpeg","-hide_banner","-loglevel","error","-y","-f","rawvideo","-pixel_format","rgb24","-video_size",f"{W}x{H}","-framerate",str(FPS),"-i","-","-an","-c:v","libx264","-preset","fast","-crf","28","-pix_fmt","yuv420p","-movflags","+faststart",str(MEDIA/f"{slug}.mp4")]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE)
    try:
        for index in range(SECONDS*FPS):
            progress=index/(SECONDS*FPS-1)
            number=min(2,int(progress*3))
            proc.stdin.write(frame(chapter,chapter['steps'][number],number,progress,manifest).tobytes())
    finally:
        proc.stdin.close()
    if proc.wait()!=0:
        raise RuntimeError(f"ffmpeg failed: {slug}")
    subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,codec_name,duration","-of","json",str(MEDIA/f"{slug}.mp4")],check=True,stdout=subprocess.DEVNULL)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stills-only",action="store_true")
    args=parser.parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("pdftoppm"):
        parser.error("Install ffmpeg and Poppler (pdftoppm) before rebuilding media.")
    values=evidence()
    catalog=chapters(values)
    for chapter in catalog:
        encode(chapter,values[2],not args.stills_only)
        print(chapter['slug'],flush=True)
    save_json(MEDIA/'catalog.json',[{"slug":c['slug'],"title":c['title'],"summary":c['summary'],"steps":[{"title":s[0],"instruction":s[1]} for s in c['steps']],"kind":"illustrated_walkthrough","duration_seconds":SECONDS} for c in catalog])
    write_guide(catalog)
    print(f"Built {len(catalog)} feature demonstrations in {MEDIA}")


def write_guide(catalog):
    markdown = ["# Feature demonstrations", "", "Every feature below has a step-by-step guide, an image, an animated preview and a 16-second MP4.", "",
                "These are illustrated walkthroughs built from actual program outputs and fictional fixtures. They are not a recording of live Gmail, Canvas or job applications. The Option-key sequence is an interaction illustration. No real messages, credentials or résumés appear.", "",
                "[Watch the video gallery](https://henry653.github.io/macos-desktop-assistant/) · [Demo evidence](media/evidence.json) · [Reproduction script](../tools/build_feature_demos.py)", ""]
    cards = []
    for c in catalog:
        slug = c['slug']
        markdown += [f"## {c['title']}", "", c['summary'], "", f"[![{c['title']}](media/{slug}.png)](media/{slug}.mp4)", "",
                     f"[Watch MP4](media/{slug}.mp4) · [Animated preview](media/{slug}.gif)", ""]
        markdown.extend(f"{i+1}. **{s[0].split(' / ',1)[-1]}:** {s[1]}" for i,s in enumerate(c['steps']))
        markdown += [""]
        steps = ''.join(f"<li><strong>{html_lib.escape(s[0].split(' / ',1)[-1])}</strong><p>{html_lib.escape(s[1])}</p></li>" for s in c['steps'])
        cards.append(f'''<article id="{slug}"><div class="card-heading"><span class="number">{slug[:2]}</span><div><h2>{html_lib.escape(c['title'])}</h2><p>{html_lib.escape(c['summary'])}</p></div></div>
<video controls playsinline preload="none" poster="media/{slug}.png" aria-label="{html_lib.escape(c['title'])} walkthrough"><source src="media/{slug}.mp4" type="video/mp4"><a href="media/{slug}.mp4">Download MP4</a></video><details><summary>Read the three steps</summary><ol>{steps}</ol></details><div class="downloads"><a href="media/{slug}.mp4">MP4</a><a href="media/{slug}.gif">Animated preview</a><a href="media/{slug}.png">Diagram</a></div></article>''')
    (ROOT/'docs/FEATURES.md').write_text('\n'.join(markdown),encoding='utf-8')
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Ten illustrated feature walkthroughs for My Planner, a macOS desktop assistant."><title>My Planner — Feature demos</title><style>
:root{color-scheme:dark;--bg:#091421;--panel:#132337;--line:#29435c;--text:#eef5fc;--muted:#aec0d1;--accent:#7de3c5}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:17px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}a{color:var(--accent);text-underline-offset:4px}a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:5px}header,main,footer{max-width:1180px;margin:auto;padding:32px}header{padding-top:64px}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.16em;font-size:13px;font-weight:650}h1{font-size:clamp(38px,5vw,70px);line-height:1.08;letter-spacing:-.035em;max-width:900px;margin:20px 0}header>p{max-width:820px;color:var(--muted);font-size:20px}.pill{display:inline-block;margin:8px 12px 8px 0;border:1px solid var(--line);border-radius:999px;padding:8px 16px;text-decoration:none}.note{border-left:3px solid var(--accent);padding:14px 22px;background:var(--panel);margin-top:24px;font-size:16px;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px}article{border:1px solid var(--line);border-radius:20px;overflow:hidden;background:var(--panel)}.card-heading{display:flex;gap:16px;padding:24px}.number{color:var(--accent);font-size:19px;margin-top:3px}h2{font-size:23px;line-height:1.25;margin:0 0 10px}.card-heading p{font-size:16px;color:var(--muted);margin:0}video{display:block;width:100%;aspect-ratio:8/5;background:#07111d}details{padding:18px 24px}summary{cursor:pointer;font-weight:600}ol{padding-left:22px}li{margin:18px 0}li p{color:var(--muted);font-size:15px;margin:4px 0}.downloads{display:flex;gap:20px;border-top:1px solid var(--line);padding:14px 24px;font-size:14px}footer{color:var(--muted);font-size:14px;padding-bottom:60px}@media(max-width:800px){.grid{grid-template-columns:1fr}header,main,footer{padding-left:20px;padding-right:20px}}</style></head><body>
<header><div class="eyebrow">My Planner / macOS desktop assistant</div><h1>Every feature.<br>Shown step by step.</h1><p>Coursework, important messages and internship preparation, in one local desktop workflow. Explore ten short walkthroughs of the current build.</p><nav><a class="pill" href="https://github.com/henry653/macos-desktop-assistant">Source on GitHub</a><a class="pill" href="media/planner-demo.html">Open sample planner</a><a class="pill" href="media/resume-demo.pdf">Sample résumé PDF</a></nav><div class="note">Illustrated walkthroughs · fictional data · actual renderer/parser/API/PDF outputs. The Option-key interaction is illustrated; these videos do not claim live account access or a verified lock screen. Each clip has readable captions and a text transcript below.</div></header>
<main><div class="grid">''' + '\n'.join(cards) + '''</div></main><footer>Reproduce the demos with <code>python tools/build_feature_demos.py</code>. <a href="media/evidence.json">Inspect output evidence</a>. Account connections and native wallpaper setup require your own configuration. No analytics or external fonts are loaded.</footer></body></html>'''
    (ROOT/'docs/index.html').write_text(page,encoding='utf-8')
    (ROOT/'docs/.nojekyll').touch()


if __name__=="__main__":
    main()
