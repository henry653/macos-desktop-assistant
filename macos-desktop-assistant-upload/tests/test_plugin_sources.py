from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugin_sources import load_resource_plugins
from render_my_planner import _resource_cards


class ResourcePluginTests(unittest.TestCase):
    def test_data_only_plugin_loads_and_escapes_markup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "campus.json"
            path.write_text(json.dumps({
                "schema_version": 1, "id": "campus", "enabled": True,
                "resource_links": [{"title": "Library <script>", "url": "https://library.example.edu"}],
            }), encoding="utf-8")
            links, errors = load_resource_plugins(Path(temporary))
        self.assertEqual(errors, [])
        self.assertEqual(len(links), 1)
        markup = _resource_cards(links)
        self.assertIn("Library &lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)

    def test_unsafe_plugin_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text(json.dumps({
                "schema_version": 1, "id": "bad", "enabled": True,
                "resource_links": [{"title": "Bad", "url": "javascript:alert(1)"}],
            }), encoding="utf-8")
            links, errors = load_resource_plugins(Path(temporary))
        self.assertEqual(links, [])
        self.assertTrue(errors)
