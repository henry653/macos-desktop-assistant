#!/usr/bin/env python3
"""Check that the staged release contains complete, portable demonstrations."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
media = ROOT / "docs/media"
catalog = json.loads((media / "catalog.json").read_text())
assert len(catalog) == 10
for item in catalog:
    for suffix in ("png", "gif", "mp4"):
        path = media / (item["slug"] + "." + suffix)
        assert path.is_file() and path.stat().st_size > 1000, path
    details = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,duration",
        "-select_streams", "v:0", "-of", "json", str(media / (item["slug"] + ".mp4"))]))
    stream = details["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"]) == ("h264", 1280, 800)
    assert abs(float(stream["duration"]) - 16) < 0.1
    assert len(item["steps"]) == 3
evidence = json.loads((media / "evidence.json").read_text())
assert evidence["live_mail_access"] is False
assert evidence["wallpaper_sha256"] == hashlib.sha256((media / "wallpaper.png").read_bytes()).hexdigest()
assert evidence["swe_valid"]["count_verified"] is True
assert evidence["swe_mismatch"]["count_verified"] is False
assert evidence["api"]["POST /v1/opportunities"]["status"] == 405
for path, body in [(ROOT / "README.md", (ROOT / "README.md").read_text()),
                   (ROOT / "docs/FEATURES.md", (ROOT / "docs/FEATURES.md").read_text()),
                   (ROOT / "docs/index.html", (ROOT / "docs/index.html").read_text())]:
    refs = re.findall(r'(?:src|poster|href)="([^"]+)"', body) if path.suffix == '.html' else re.findall(r'\]\(([^)]+)\)', body)
    for ref in refs:
        if ref.startswith(('http:', 'https:', '#', 'mailto:')):
            continue
        assert (path.parent / ref.split('#')[0]).exists(), (path, ref)
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split('\0')
manifest = []
private_pattern = re.compile(r'/Users/[^/\s]+/|[A-Za-z0-9._%+-]+@ad\.unc\.edu|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}')
for relative in filter(None, tracked):
    path = ROOT / relative
    content = path.read_bytes()
    if path.suffix not in {'.png','.gif','.mp4','.pdf','.zip'}:
        # This checker contains the literal patterns, not private values.
        if relative != 'tools/verify_release.py':
            assert not private_pattern.search(content.decode()), f"Private material in {relative}"
    assert path.stat().st_size < 20_000_000, relative
    sha = hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
    manifest.append({"path":relative,"bytes":len(content),"sha":sha,"sha256":hashlib.sha256(content).hexdigest(),
                     "binary":path.suffix in {'.png','.gif','.mp4','.pdf','.zip'},"mode":"100644"})
(ROOT/'.demo-work').mkdir(exist_ok=True)
(ROOT/'.demo-work/release-manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps({"features":len(catalog),"mp4s":len(catalog),"diagrams":len(catalog),
                  "animated_previews":len(catalog),"published_files":len(manifest),
                  "total_bytes":sum(f['bytes'] for f in manifest),"checks":"passed"},indent=2))
