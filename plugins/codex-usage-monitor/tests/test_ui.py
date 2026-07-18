from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_usage_monitor.cdp import CdpError, CdpConnection
from codex_usage_monitor.storage import Storage
from codex_usage_monitor.ui_host import match_adapter
from codex_usage_monitor.ui_launcher import (
    _bootstrap_source, _plugin_family, _user_visible_path, discover_codex_app, reserve_loopback_port,
    restart_existing_codex,
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

    def test_bootstrap_launches_from_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="usage monitor ") as directory:
            root = Path(directory)
            family = root / "cache"
            script = family / "0.2.9" / "scripts" / "usage_monitor.py"
            script.parent.mkdir(parents=True)
            script.write_text("import sys; print('|'.join(sys.argv[1:]))", encoding="utf-8")
            bootstrap = root / "launcher.py"
            bootstrap.write_text(_bootstrap_source(family, root / "plugin data"), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(bootstrap), "--smoke"], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--data-dir", result.stdout)
            self.assertIn("plugin data|ui|launch|--smoke", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows process restart")
    def test_restart_existing_codex_terminates_only_root_process_tree(self) -> None:
        executable = Path(r"C:\Program Files\WindowsApps\OpenAI.Codex_test\app\ChatGPT.exe")
        entries = [
            (10, 1, executable),
            (11, 10, executable),
        ]
        with mock.patch("codex_usage_monitor.ui_launcher._windows_process_entries", side_effect=[entries, []]), \
             mock.patch("codex_usage_monitor.ui_launcher.subprocess.run") as run:
            self.assertEqual(restart_existing_codex(executable), 2)
            self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "10", "/T", "/F"])

    def test_adapter_requires_hash_and_version(self) -> None:
        adapters = [{"id": "one", "app_asar_sha256": ["ABC"], "package_versions": ["1.0"]}]
        self.assertEqual(match_adapter({"app_asar_sha256": "abc", "package_version": "1.0"}, adapters)["id"], "one")
        self.assertIsNone(match_adapter({"app_asar_sha256": "abc", "package_version": "2.0"}, adapters))

    def test_current_windows_renderer_adapter_is_registered(self) -> None:
        path = Path(__file__).resolve().parents[1] / "ui" / "adapters.json"
        adapters = json.loads(path.read_text(encoding="utf-8"))["adapters"]
        matched = match_adapter(
            {
                "app_asar_sha256": "545941B6174CCED0EED94438BB39EA5814F568B5BC6B14C7921FED8D5B694153",
                "package_version": "26.715.3651.0",
            },
            adapters,
        )
        self.assertEqual(matched["fiber_item_types"], ["agentMessage"])

        current = match_adapter(
            {
                "app_asar_sha256": "4F81FE8CFADD0ECD1D55A46F4B101B1DB70ABBB372B63A0120218B1D868008A3",
                "package_version": "26.715.4045.0",
            },
            adapters,
        )
        self.assertEqual(current["turn_wrapper_contract"]["identity"], ["conversationId", "turnId"])

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

    def test_legacy_index_snapshots_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            storage = Storage(path)
            storage.save_message_snapshot("/index.html", "legacy", None, "commentary", True, {})
            storage.set_meta("removed_legacy_index_snapshots", "0")
            storage.close()
            storage = Storage(path)
            self.assertEqual(storage.message_snapshots("/index.html"), [])
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
        self.assertIn("const appShell=", source)
        self.assertIn("completionFromProps", source)
        self.assertIn("detectTitlebarHeight", source)
        self.assertIn("attachShadow({mode:\"open\"})", source)
        self.assertIn("probeUnknown", source)
        self.assertIn("repeated_missing_identity_contract", source)
        self.assertIn("data-codex-usage-dock-toggle", source)
        self.assertIn("codexUsageDockVisible", source)
        self.assertIn("findComposerToggleGroup", source)
        self.assertIn("identityFromProps", source)
        self.assertIn("conversationId", source)
        self.assertIn('type:"active_thread"', source)
        self.assertIn("activeConversationFromComposer", source)
        self.assertIn('value===null||value===undefined||value===""', source)
        self.assertIn('type:"history_request"', source)
        self.assertIn("__codexUsageHistoryUpdate", source)
        self.assertIn("codexUsageGuardDismiss", source)
        self.assertIn("data-codex-usage-guard-badge", source)
        self.assertIn("data-codex-usage-advisor-badge", source)
        self.assertIn("advisorConfig", source)
        self.assertIn("data-codex-usage-turn-hidden", source)
        self.assertIn("data-codex-usage-history-gate", source)
        self.assertIn("turn_wrapper_contract", source)
        self.assertIn("record.addedNodes", source)
        self.assertIn("turnOriginalStyles:new WeakMap()", source)
        self.assertIn('type:"history_virtualization"', source)
        self.assertIn("mcpTurn", source)
        self.assertIn("row?.phase===\"final_answer\"", source)
        self.assertIn("Порада з оптимізації", source)
        self.assertIn("nativeContextPercent", source)
        self.assertIn('type:"context"', source)
        self.assertIn("turn.total", source)
        self.assertIn('startsWith("uk")?"uk":"en"', source)
        self.assertNotIn('<div id="footers">', source)
        self.assertNotIn("const rect=anchor.element.getBoundingClientRect()", source)
        self.assertNotIn('startsWith("ru")', source)
        self.assertNotIn("5h used", source)
        self.assertNotIn("backdrop-filter", source)

    def test_builtin_widget_uses_localized_placeholders(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "ui" / "widgets" / "usage-summary" / "widget.html"
        ).read_text(encoding="utf-8")
        self.assertIn("{ui.live_telemetry}", source)
        self.assertIn("{ui.protected_surface}", source)


if __name__ == "__main__":
    unittest.main()
