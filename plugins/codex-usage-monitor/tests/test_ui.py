from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_usage_monitor.cdp import CdpError, CdpConnection
from codex_usage_monitor.storage import Storage
from codex_usage_monitor.ui_host import match_adapter
from codex_usage_monitor.ui_launcher import (
    _bootstrap_source, _plugin_family, _user_visible_path, discover_codex_app, reserve_loopback_port,
)
from codex_usage_monitor.widgets import WidgetError, load_widgets, markdown_to_html, sanitize_html, validate_manifest


class UiTests(unittest.TestCase):
    def test_loopback_port_and_non_loopback_cdp_rejection(self) -> None:
        self.assertGreater(reserve_loopback_port(), 0)
        with self.assertRaises(CdpError):
            CdpConnection("ws://example.com/devtools/page/1")

    def test_app_discovery_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Codex.exe"
            executable.touch()
            with mock.patch.dict(os.environ, {"CODEX_DESKTOP_EXECUTABLE": str(executable)}):
                self.assertEqual(discover_codex_app(), executable)

    @unittest.skipUnless(os.name == "nt", "Windows path mapping")
    def test_sandbox_path_is_mapped_to_real_home(self) -> None:
        reference = Path.home() / ".codex" / "plugins" / "data" / "codex-usage-monitor-market"
        value = _user_visible_path(Path(r"C:\Users\CodexSandboxOffline\.codex\plugins\cache"), reference)
        self.assertEqual(value, Path.home() / ".codex" / "plugins" / "cache")

    def test_launcher_uses_stable_version_family_and_ignores_empty_cache_entries(self) -> None:
        family = Path.home() / ".codex" / "plugins" / "cache" / "market" / "codex-usage-monitor"
        version = family / "0.2.4"
        self.assertEqual(_plugin_family(version), family)
        source = _bootstrap_source(family, Path.home() / "data")
        self.assertIn("usage_monitor.py\").is_file()", source)
        self.assertIn("max(candidates", source)
        self.assertIn('"--check" in sys.argv', source)
        self.assertIn("launcher-error.log", source)
        self.assertNotIn(str(version), source)

    def test_adapter_requires_hash_and_version(self) -> None:
        adapters = [{"id": "one", "app_asar_sha256": ["ABC"], "package_versions": ["1.0"]}]
        self.assertEqual(match_adapter({"app_asar_sha256": "abc", "package_version": "1.0"}, adapters)["id"], "one")
        self.assertIsNone(match_adapter({"app_asar_sha256": "abc", "package_version": "2.0"}, adapters))

    def test_widget_traversal_and_scripted_footer_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            widget = root / "widget"
            widget.mkdir()
            (widget / "entry.js").write_text("api.getSnapshot()", encoding="utf-8")
            manifest = {
                "schema_version": 1, "id": "unsafe", "name": "Unsafe", "entry": "entry.js",
                "content_type": "javascript", "placements": ["message_footer"], "default_placement": "message_footer"
            }
            path = widget / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(WidgetError):
                validate_manifest(path)
            manifest.update({"content_type": "html", "entry": "../secret.html"})
            (root / "secret.html").write_text("secret", encoding="utf-8")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(WidgetError):
                validate_manifest(path)

    def test_html_markdown_security(self) -> None:
        value = sanitize_html('<script>steal()</script><style>@import "https://x"; .x{background:url(https://x)}</style><p onclick="x">safe</p>')
        self.assertNotIn("script", value)
        self.assertNotIn("https://", value)
        self.assertNotIn("onclick", value)
        self.assertIn("safe", value)
        self.assertNotIn("<img", markdown_to_html("![x](https://evil)"))

    def test_message_snapshot_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            storage.save_message_snapshot("thread", "item", "turn", "commentary", True, {"token": {"total_tokens": 4}})
            rows = storage.message_snapshots("thread")
            self.assertEqual(rows[0]["snapshot"]["token"]["total_tokens"], 4)
            self.assertTrue(rows[0]["completed"])
            self.assertIsNotNone(rows[0]["first_seen_at"])
            self.assertIsNotNone(rows[0]["completed_at"])
            self.assertGreaterEqual(rows[0]["duration_seconds"], 0)
            storage.close()

    def test_message_snapshot_preserves_first_seen_and_completion_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            storage.save_message_snapshot("thread", "item", "turn", "commentary", False, {})
            first = storage.message_snapshots("thread")[0]["first_seen_at"]
            storage.save_message_snapshot("thread", "item", "turn", "commentary", True, {})
            row = storage.message_snapshots("thread")[0]
            self.assertEqual(row["first_seen_at"], first)
            self.assertIsNotNone(row["completed_at"])
            storage.close()

    def test_final_snapshot_can_be_refined_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            storage.save_message_snapshot("thread", "item", "turn", "final_answer", True, {"old": True})
            self.assertEqual(storage.refresh_completed_turn_snapshots("turn", {"final": True}), 1)
            self.assertEqual(storage.message_snapshots("thread")[0]["snapshot"], {"final": True})
            storage.close()

    def test_runtime_uses_dynamic_windows_remaining_and_native_style(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "runtime.js").read_text(encoding="utf-8")
        self.assertIn("window_minutes", source)
        self.assertIn("лишилось", source)
        self.assertIn("quota_primary_delta", source)
        self.assertNotIn("5h used", source)
        self.assertNotIn("backdrop-filter", source)


if __name__ == "__main__":
    unittest.main()
