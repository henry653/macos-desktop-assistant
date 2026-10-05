"""Read opt-in, data-only local plugins for campus resource links.

Plugins are JSON, never executable code. They cannot inject HTML or initiate
network requests. Rendering escapes titles and accepts HTTPS destinations only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def load_resource_plugins(directory: Path) -> tuple[list[dict[str, str]], list[str]]:
    links: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    if not directory.is_dir():
        return links, errors
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("schema_version")
            plugin_id = value.get("id")
            if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
                raise ValueError("id")
            if value.get("enabled") is not True:
                continue
            items = value.get("resource_links")
            if not isinstance(items, list) or len(items) > 25:
                raise ValueError("resource_links")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("resource_link")
                title, url = item.get("title"), item.get("url")
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
                    raise ValueError("title")
                if not isinstance(url, str) or len(url) > 2048:
                    raise ValueError("url")
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("unsafe_url")
                key = (title.casefold(), url)
                if key not in seen:
                    seen.add(key)
                    links.append({"plugin": plugin_id, "title": title.strip(), "url": url})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{path.name}:{type(error).__name__}")
    return links, errors
