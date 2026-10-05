#!/usr/bin/env python3
"""Loopback-only read-only API for the existing outreach tool.

No email bodies or personal messages are returned. There are no mutation
endpoints, application submission endpoints, or browser actions.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from intern_preparation import strict_display_items


def opportunities(report: dict) -> dict:
    messages = report.get("messages") if isinstance(report.get("messages"), list) else []
    items = strict_display_items(messages, report.get("intern_preparation_summary"), zone=ZoneInfo("America/New_York"))
    return {"generated_at": report.get("generated_at"), "count": len(items), "items": [
        {key: item.get(key) for key in ("fingerprint", "company", "role", "apply_url", "queue_status", "resume_variant")}
        for item in items
    ]}


def handler_for(report_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._json(200, {"state": "ok", "mode": "read_only"})
                return
            if self.path != "/v1/opportunities":
                self._json(404, {"error": "not_found"})
                return
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    raise ValueError("invalid report")
                self._json(200, opportunities(report))
            except (OSError, ValueError, json.JSONDecodeError):
                self._json(503, {"error": "verified_report_unavailable"})

        def do_POST(self) -> None:
            self._json(405, {"error": "read_only"})

        def _json(self, status: int, value: dict) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parent / "daily_briefing.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.report))
    server.serve_forever()


if __name__ == "__main__":
    main()
