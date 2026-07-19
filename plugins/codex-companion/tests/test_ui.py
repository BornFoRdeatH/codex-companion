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
from codex_usage_monitor.ui_host import _primary_target, _safe_origin, _target_summary, match_adapter
from codex_usage_monitor.ui_launcher import (
    _bootstrap_source, _plugin_family, _user_visible_path, discover_codex_app, launch_codex, reserve_loopback_port,
    codex_process_running, legacy_launcher_paths, launcher_paths, restart_existing_codex,
)
from codex_usage_monitor.widgets import WidgetError, load_widget_report, load_widgets, markdown_to_html, sanitize_html, validate_manifest


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

    def test_companion_launcher_rebrands_without_losing_legacy_cleanup_paths(self) -> None:
        with mock.patch("codex_usage_monitor.ui_launcher.platform.system", return_value="Windows"), \
             mock.patch.dict(os.environ, {"USERPROFILE": r"C:\Users\Person", "APPDATA": r"C:\Users\Person\AppData\Roaming"}):
            self.assertTrue(all("Codex Companion" in path.name for path in launcher_paths()))
            self.assertTrue(all("Codex Usage UI" in path.name for path in legacy_launcher_paths()))

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

    @unittest.skipUnless(os.name == "nt", "Windows launcher flags")
    def test_launcher_uses_no_window_process_creation(self) -> None:
        executable = Path(r"C:\Program Files\Codex\ChatGPT.exe")
        with mock.patch("codex_usage_monitor.ui_launcher.restart_existing_codex"), \
             mock.patch("codex_usage_monitor.ui_launcher.subprocess.Popen") as popen:
            launch_codex(executable, 43123)
            flags = popen.call_args.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)

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
        self.assertEqual(current["native_focus_contract"]["turn_number"], "turnNumber")
        self.assertEqual(current["native_focus_contract"]["wrapper_parent_levels"], 1)
        self.assertEqual(current["new_task_contract"]["strategy"], "project-new-task-button")

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

    def test_widget_schema_v2_supports_composer_and_control_center(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            widget = Path(directory) / "context-runway"
            widget.mkdir()
            (widget / "widget.html").write_text("<p>safe</p>", encoding="utf-8")
            manifest = {
                "schema_version": 2, "id": "context-runway", "name": "Context Runway",
                "entry": "widget.html", "content_type": "html",
                "placements": ["bottom_dock", "composer_footer", "control_center"],
                "default_placement": "bottom_dock", "actions": ["open_cockpit"],
                "permissions": ["telemetry", "theme", "actions", "unsafe"],
                "enabled_by_default": False,
            }
            (widget / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = validate_manifest(widget / "manifest.json")
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["actions"], ["open_cockpit"])
            self.assertEqual(result["permissions"], ["telemetry", "theme", "actions"])
            self.assertFalse(result["enabled_by_default"])

    def test_widget_report_is_safe_and_surfaces_invalid_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad"
            bad.mkdir()
            (bad / "manifest.json").write_text("{\"schema_version\": 2}", encoding="utf-8")
            report = load_widget_report([str(root)])
            self.assertEqual(report["widgets"], [])
            self.assertEqual(len(report["errors"]), 1)
            self.assertNotIn("prompt", str(report))

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
        self.assertIn("data-codex-companion-focus-hidden", source)
        self.assertIn("data-codex-companion-history-gate", source)
        self.assertIn("native_focus_contract", source)
        self.assertIn("[data-turn-key]", source)
        self.assertIn("totalTurnCount", source)
        self.assertIn("turnNumber", source)
        self.assertIn("isMostRecentTurn", source)
        self.assertIn('typeof props.entry==="object"', source)
        self.assertIn("const incrementalRoots", source)
        self.assertIn("onFocusScroll", source)
        self.assertIn("signedBoundaryScrollTop", source)
        self.assertIn("compensatedScrollTop", source)
        self.assertIn("data-codex-companion-palette-toggle", source)
        self.assertIn('type:"budget_features"', source)
        self.assertIn("context_optimizer", source)
        self.assertIn("task_health", source)
        self.assertIn("task_activity", source)
        self.assertIn("renderTaskCockpit", source)
        self.assertIn("renderTaskReview", source)
        self.assertIn("cockpitGauge", source)
        self.assertIn("recommendation_action", source)
        self.assertIn("recommendation_dismissed", source)
        self.assertIn("task_review_opened", source)
        self.assertIn("advisory_only", source)
        self.assertIn("taskThreadId", source)
        self.assertIn("registerAction", source)
        self.assertIn("registerFooterControl", source)
        self.assertIn("invokeAction", source)
        self.assertIn("action_requested", source)
        self.assertIn("action_completed", source)
        self.assertIn("data-codex-companion-footer-actions", source)
        self.assertIn("widgetSettings", source)
        self.assertIn("codexCompanionFeatureSettings:v2", source)
        self.assertIn("widgetsEnabledByDefault===true", source)
        self.assertIn("actionsForPlacement", source)
        self.assertIn("if(!surface&&!actions.length)return", source)
        self.assertIn('featureEnabled(w.id)&&w.default_placement==="message_footer"', source)
        self.assertIn("widgetErrors", source)
        self.assertIn("allow-scripts", source)
        self.assertIn("openPanel", source)
        self.assertIn("CREATE_NO_WINDOW", (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_launcher.py").read_text(encoding="utf-8"))
        self.assertIn("Timed out waiting for Codex renderer/CDP target", (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_host.py").read_text(encoding="utf-8"))
        self.assertIn('id="controlCenter"', source)
        self.assertIn('setAttribute("role","dialog")', source)
        self.assertIn('aria-selected', source)
        self.assertIn("uiState", source)
        self.assertIn("openControlCenter", source)
        self.assertIn("closeControlCenter", source)
        self.assertIn("readLocalJson", source)
        self.assertIn("onWidgetMessage", source)
        self.assertIn("onGlobalKeydown", source)
        self.assertIn("onVisibilityChange", source)
        self.assertIn("removeEventListener", source)
        self.assertIn("lastSnapshotAt", source)
        self.assertIn("max-width:min(92vw,440px)", source)
        self.assertIn("Math.min(440", source)
        self.assertIn('if(position==="left_dock")position="floating"', source)
        self.assertIn('setAttribute("role","button")', source)
        self.assertIn("safe_turns_remaining", source)
        self.assertIn("Create checkpoint", source)
        self.assertIn("budget_action", source)
        self.assertIn('type:"project_request"', source)
        self.assertIn('type:"performance_state"', source)
        self.assertIn("IntersectionObserver", source)
        self.assertIn("codex-companion-handoff:", source)
        self.assertIn("maybeCaptureHandoff", source)
        self.assertIn("__codexCompanionGitUpdate", source)
        self.assertIn("navigator.clipboard.writeText", source)
        self.assertNotIn('type:"handoff_content"', source)
        self.assertIn("validateHandoff", source)
        self.assertIn("handoff_prefill_confirmed", source)
        self.assertIn("prefillTimeout", source)
        self.assertIn("continue-handoff", source)
        self.assertIn("checkpoint", source)
        self.assertIn("Delivery and continuity", source)
        self.assertIn("shouldClampScroll", source)
        self.assertIn('focusState:"pending_boundary"', source)
        self.assertIn('scroller.style.overflowAnchor="none"', source)
        self.assertNotIn('row.style.display="none"', source)
        self.assertIn("record.addedNodes", source)
        self.assertIn('node.matches?.("[data-turn-key]")', source)
        self.assertIn("turnOriginalStyles:new WeakMap()", source)
        self.assertIn('type:"history_focus"', source)
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
        host_source = (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_host.py").read_text(encoding="utf-8")
        for field in ("focus_state", "boundary_turn", "boundary_scroll_top", "scroll_direction", "guard_active"):
            self.assertIn(field, host_source)

    def test_ui_host_reports_codex_exit_before_attach(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_host.py").read_text(encoding="utf-8")
        self.assertIn("Windows Store apps may exit the launch stub", source)
        self.assertIn("renderer/CDP target before timeout", source)
        self.assertIn("companion_attach_disabled", source)
        self.assertIn("target_discovery", source)
        self.assertIn("stale_codex_process", source)
        self.assertIn("runtime_attach_failed", source)
        self.assertIn("discover_version(port)", source)
        self.assertIn('self._write_status(state="error"', source)

    def test_target_summary_is_attachable_and_redacted(self) -> None:
        targets = [
            {"type": "page", "url": "app://codex/index.html#/secret", "webSocketDebuggerUrl": "ws://127.0.0.1:1/a"},
            {"type": "worker", "url": "https://example.test/private/path?token=abc"},
            {"type": "webview", "url": "file:///C:/Users/name/private.txt", "webSocketDebuggerUrl": "ws://127.0.0.1:1/b"},
        ]
        summary = _target_summary(targets)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["attachable"], 2)
        self.assertIn("page", summary["types"])
        self.assertIn("app://codex", summary["origins"])
        self.assertNotIn("secret", json.dumps(summary))
        self.assertNotIn("private", json.dumps(summary))
        self.assertEqual(_primary_target(targets)["type"], "page")
        self.assertEqual(_safe_origin("not-a-known-url-with-private-data"), "other")

    def test_launcher_supports_attach_disabled_and_restart_helpers(self) -> None:
        launcher = (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_launcher.py").read_text(encoding="utf-8")
        host = (Path(__file__).resolve().parents[1] / "scripts" / "codex_usage_monitor" / "ui_host.py").read_text(encoding="utf-8")
        self.assertIn("attach_enabled: bool = True", launcher)
        self.assertIn("if attach_enabled:", launcher)
        self.assertIn("CODEX_COMPANION_ATTACH", host)
        self.assertIn("codex_process_running", launcher)
        self.assertIn("Path()", launcher)
        self.assertTrue(callable(codex_process_running))
        self.assertTrue(callable(restart_existing_codex))

    def test_runtime_extension_surface_is_disabled_by_default(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "ui" / "runtime.js").read_text(encoding="utf-8")
        self.assertIn("data-codex-companion-footer-actions", source)
        self.assertIn("data-codex-companion-widget-footer", source)
        self.assertIn("mountWhenReady", source)
        self.assertIn("return widget?boot.widgetsEnabledByDefault===true", source)
        self.assertIn("return settings[id]===true", source)
        self.assertIn("renderTaskCockpit", source)
        self.assertIn("onWidgetMessage", source)

    def test_builtin_widget_uses_localized_placeholders(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "ui" / "widgets" / "usage-summary" / "widget.html"
        ).read_text(encoding="utf-8")
        self.assertIn("{ui.live_telemetry}", source)
        self.assertIn("{ui.protected_surface}", source)


if __name__ == "__main__":
    unittest.main()
