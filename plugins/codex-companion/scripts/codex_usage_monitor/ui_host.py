from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .cdp import CdpConnection, CdpError, discover_targets
from .config import LoadedConfig
from .storage import Storage
from .ui_launcher import discover_codex_app, launch_codex, reserve_loopback_port
from .widgets import load_widget_report, markdown_to_html, sanitize_html
from .render import derive
from .advisor import evaluate as evaluate_advice
from .budget import evaluate as evaluate_budget, transient_features
from .task_cockpit import build as build_task_cockpit


BINDING = "__codexUsageHost"


def fingerprint(executable: Path) -> dict[str, str]:
    resources = executable.parent / "resources"
    archive = resources / "app.asar"
    result = {"executable": str(executable), "package_version": _package_version(executable)}
    if archive.is_file():
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        result["app_asar_sha256"] = digest.hexdigest().upper()
    return result


def load_adapters(plugin_root: Path) -> list[dict[str, Any]]:
    path = plugin_root / "ui" / "adapters.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("adapters", []) if isinstance(value, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def match_adapter(fingerprint_value: dict[str, str], adapters: list[dict[str, Any]]) -> dict[str, Any] | None:
    digest = fingerprint_value.get("app_asar_sha256", "").upper()
    version = fingerprint_value.get("package_version")
    for adapter in adapters:
        hashes = {str(value).upper() for value in adapter.get("app_asar_sha256", [])}
        versions = set(adapter.get("package_versions", []))
        if digest and digest in hashes and (not versions or version in versions):
            return adapter
    return None


class UiHost:
    def __init__(self, plugin_root: Path, plugin_data: Path, config: LoadedConfig, storage: Storage, restart_existing: bool = False):
        self.plugin_root = plugin_root
        self.plugin_data = plugin_data
        self.config = config
        self.storage = storage
        self.restart_existing = restart_existing
        self.stop = False
        self._last_heartbeat = 0.0
        self.runtime_compatibility = "unknown"
        self.native_context_by_turn: dict[tuple[str, str], float] = {}
        self.active_thread_id: str | None = None
        self.active_session_state = "pending"
        self.active_thread_switched_at: float | None = None
        self.history_focus: dict[str, Any] = {
            "thread_id": None, "compatible": None, "total_turns": 0, "mounted_turns": 0,
            "window_start": None, "visible_window_turns": 0, "hidden_logical_turns": 0,
            "focus_state": "pending_boundary", "boundary_turn": None, "boundary_scroll_top": None,
            "scroll_direction": None, "guard_active": False,
        }
        self.transient_budget_features: dict[str, dict[str, Any]] = {}
        self.cockpit_events: list[dict[str, Any]] = []
        self.budget_diagnostics: dict[str, Any] = {"last_action": None, "last_action_at": None}
        self.performance_state = "active"
        self.performance_diagnostics: dict[str, Any] = {"state": "active"}
        self.handoff_diagnostics: dict[str, Any] = {
            "exact_adapter": False, "composer": False, "new_task_anchor": False,
            "clipboard": False, "preview_capture": False, "fallback": False,
        }
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))

    def run(self) -> int:
        executable = discover_codex_app(self.plugin_data)
        if not executable:
            self._write_status(state="error", error="Codex desktop executable not found")
            return 2
        port = reserve_loopback_port()
        try:
            process = launch_codex(executable, port, self.restart_existing)
        except (OSError, RuntimeError) as exc:
            self._write_status(state="error", error=str(exc))
            return 2
        fp = fingerprint(executable)
        adapters = load_adapters(self.plugin_root)
        adapter = match_adapter(fp, adapters)
        policy = self.config.get("ui.unknown_version_policy", "dock_only")
        supported = adapter is not None
        self.runtime_compatibility = "exact" if supported else "probing"
        if not supported and policy == "disable":
            self._write_status(state="unsupported", pid=process.pid, port=port, fingerprint=fp)
            return 3
        self._write_status(state="starting", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
        history_focus = (self.plugin_root / "ui" / "history_focus.js").read_text(encoding="utf-8")
        runtime = history_focus + "\n" + (self.plugin_root / "ui" / "runtime.js").read_text(encoding="utf-8")
        attach_deadline = time.monotonic() + 15.0
        last_target = None
        connection: CdpConnection | None = None
        while not self.stop and process.poll() is None:
            try:
                target = _primary_target(discover_targets(port))
                if not target:
                    if time.monotonic() >= attach_deadline:
                        self._write_status(state="error", pid=process.pid, port=port, fingerprint=fp,
                                           error="Timed out waiting for Codex renderer/CDP target")
                        if process.poll() is None:
                            process.terminate()
                        return 4
                    time.sleep(0.1)
                    continue
                target_id = target.get("id")
                if connection is None or connection.closed or target_id != last_target:
                    if connection:
                        connection.close()
                    connection = CdpConnection(str(target["webSocketDebuggerUrl"]))
                    self._attach(connection, runtime, supported, adapter, adapters)
                    last_target = target_id
                    attach_deadline = time.monotonic() + 15.0
                    self._write_status(state="attached", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
                self._drain_events(connection)
                self._push_snapshot(connection)
                if time.monotonic() - self._last_heartbeat >= 5.0:
                    self._write_status(state="attached", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
                    self._last_heartbeat = time.monotonic()
                time.sleep(self._refresh_delay())
            except (OSError, CdpError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
                self._log_error(exc)
                self._write_status(state="reconnecting", pid=process.pid, port=port, error=str(exc), fingerprint=fp)
                if connection:
                    connection.close()
                connection = None
                time.sleep(0.25)
            except Exception as exc:
                # The UI is optional, but it must remain self-healing when renderer or schema details drift.
                self._log_error(exc)
                self._write_status(state="reconnecting", pid=process.pid, port=port, error=str(exc), fingerprint=fp)
                if connection:
                    connection.close()
                connection = None
                time.sleep(0.5)
        if connection:
            connection.close()
        self._write_status(state="stopped", exit_code=process.poll(), fingerprint=fp)
        return int(process.poll() or 0)

    def _attach(
        self,
        connection: CdpConnection,
        runtime: str,
        supported: bool,
        adapter: dict[str, Any] | None,
        adapters: list[dict[str, Any]],
    ) -> None:
        # A renderer reload discards the in-memory preview by contract.
        self.storage.cancel_pending_handoffs()
        connection.call("Page.enable")
        connection.call("Runtime.enable")
        connection.call("Runtime.addBinding", {"name": BINDING})
        boot = self._boot_payload(supported, adapter, adapters)
        source = f"window.__CODEX_USAGE_BOOT__={json.dumps(boot, separators=(',', ':'))};\n{runtime}"
        connection.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        connection.call("Runtime.evaluate", {"expression": source, "awaitPromise": False})

    def _boot_payload(
        self, supported: bool, adapter: dict[str, Any] | None, adapters: list[dict[str, Any]]
    ) -> dict[str, Any]:
        widget_report = load_widget_report(
            list(self.config.get("ui.widgets.directories", [])) if self.config.get("ui.widgets.enabled", True) else [],
            bool(self.config.get("ui.security.scripts_enabled", True)),
        )
        widgets = widget_report["widgets"]
        configured_order = list(self.config.get("ui.widgets.ordering", []))
        order_index = {widget_id: index for index, widget_id in enumerate(configured_order)}
        widgets.sort(key=lambda widget: (order_index.get(widget["id"], len(order_index)), widget["order"]))
        for widget in widgets:
            if widget["content_type"] == "html":
                widget["source"] = sanitize_html(widget["source"])
            elif widget["content_type"] == "markdown":
                widget["source"] = markdown_to_html(widget["source"])
        return {
            "supported": supported,
            "probeUnknown": not supported and self.config.get("ui.unknown_version_policy", "dock_only") == "dock_only",
            "probeItemTypes": sorted({
                str(item_type)
                for known in adapters
                for item_type in known.get("fiber_item_types", [])
            }),
            "adapter": adapter or {},
            "dockPosition": self.config.get("ui.dock_position", "right_dock"),
            "dockSize": self.config.get("ui.dock_size", 340),
            "composerToggle": self.config.get("ui.composer_toggle", True),
            "layoutMode": self.config.get("ui.layout_mode", "reserve_space"),
            "footerPhases": self.config.get("ui.footer_phases", ["commentary", "final_answer"]),
            "locale": "auto" if self.config.get("ui.auto_locale", True) else self.config.get("locale.language", "en"),
            "widgets": widgets,
            "widgetErrors": widget_report["errors"],
            "security": self.config.get("ui.security", {}),
            "guard": self.config.get("ui.guard", {}),
            "historyConfig": self.config.get("ui.history", {}),
            "advisorConfig": self.config.get("ui.advisor", {}),
            "focusMode": self.config.get("ui.focus_mode", {}),
            "commandPalette": self.config.get("ui.command_palette", {}),
            "budgetConfig": self.config.get("ui.budget", {}),
            "projectsConfig": self.config.get("ui.projects", {}),
            "performanceConfig": self.config.get("ui.performance", {}),
            "handoffConfig": self.config.get("ui.handoff", {}),
        }

    def _push_snapshot(self, connection: CdpConnection) -> None:
        if self.active_thread_id:
            self.active_session_state = "available" if self.storage.has_session(self.active_thread_id) else "pending"
        snapshot = self._summary_payload(None, self.active_thread_id)
        turn = snapshot.get("turn") or {}
        if turn.get("turn_id") and turn.get("ended_at"):
            self.storage.refresh_completed_turn_snapshots(str(turn["turn_id"]), snapshot)
            context = (snapshot.get("view") or {}).get("context") or {}
            self.storage.materialize_turn(str(turn["turn_id"]), context.get("used_percent"), context.get("source"))
        history = self.storage.message_snapshots(self.active_thread_id, limit=500) if self.active_thread_id else []
        payload = {"snapshot": snapshot, "history": history, "at": time.time(), "activeThreadId": self.active_thread_id,
                   "pendingHandoffs": self.storage.pending_handoffs(self.active_thread_id)}
        expression = f"window.__codexUsageUpdate&&window.__codexUsageUpdate({json.dumps(payload, separators=(',', ':'))})"
        connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": False}, timeout=1.0)

    def _drain_events(self, connection: CdpConnection) -> None:
        while event := connection.next_event():
            if event.get("method") != "Runtime.bindingCalled":
                continue
            params = event.get("params") or {}
            if params.get("name") != BINDING:
                continue
            try:
                message = json.loads(params.get("payload", "{}"))
            except json.JSONDecodeError:
                continue
            if message.get("type") == "history_request":
                self._respond_history(connection, message)
            elif message.get("type") == "project_request":
                self._respond_projects(connection, message)
            elif message.get("type") == "project_alias":
                self._set_project_alias(connection, message)
            elif message.get("type") == "budget_features" and self.active_thread_id:
                self.transient_budget_features[self.active_thread_id] = transient_features(message.get("features"))
            elif message.get("type") == "budget_action":
                action = str(message.get("action") or "")
                if action in {"checkpoint", "handoff", "new_task"}:
                    self.budget_diagnostics = {"last_action": action, "last_action_at": time.time()}
                    self._write_status(state="budget_action")
            elif message.get("type") in {
                "recommendation_action", "recommendation_dismissed", "task_review_opened",
                "action_requested", "action_ready", "action_completed", "action_failed",
                "action_registered", "widget_registered", "widget_error", "layout_changed",
                "runtime_reconnected", "launcher_state_changed",
            }:
                event_type = str(message.get("type"))
                event = {"type": event_type, "at": time.time()}
                for key in ("code", "action", "widgetId", "state", "placement", "reason"):
                    value = message.get(key)
                    if isinstance(value, str) and value and len(value) <= 80:
                        event[key] = value
                self.cockpit_events.append(event)
                self.cockpit_events = self.cockpit_events[-20:]
            elif message.get("type") == "performance_state":
                value = str(message.get("state") or "")
                if value in {"active", "idle", "background"}:
                    self.performance_state = value
                    metrics = message.get("diagnostics")
                    self.performance_diagnostics = {"state": value, **(metrics if isinstance(metrics, dict) else {})}
            elif message.get("type") == "handoff_created" and self.active_thread_id:
                nonce = str(message.get("nonce") or "")
                mode = str(message.get("mode") or "handoff")
                try:
                    self.storage.create_handoff(self.active_thread_id, nonce, mode)
                except ValueError:
                    pass
            elif message.get("type") in {
                "handoff_submitted", "handoff_captured", "handoff_open_started", "handoff_prefilling",
                "handoff_prefill_confirmed", "handoff_prefill_failed", "handoff_fallback",
            }:
                self._handle_handoff_event(message)
            elif message.get("type") == "handoff_diagnostics":
                self._handle_handoff_event(message)
            elif message.get("type") == "git_summary_request":
                self._respond_git_summary(connection, message)
            elif message.get("type") == "handoff_complete" and self.active_thread_id and message.get("turnId"):
                self.storage.finish_handoff(self.active_thread_id, str(message["turnId"]), "completed")
            elif message.get("type") == "active_thread":
                raw_id = message.get("threadId")
                thread_id = str(raw_id)[:128] if raw_id and not str(raw_id).startswith("client-new-thread:") else None
                if thread_id != self.active_thread_id:
                    self.active_thread_id = thread_id
                    self.cockpit_events = []
                    self.active_session_state = "available" if thread_id and self.storage.has_session(thread_id) else "pending"
                    self.active_thread_switched_at = time.time()
                    self._write_status(state="attached")
            elif message.get("type") == "item" and message.get("threadId") and message.get("itemId"):
                turn_id = str(message.get("turnId")) if message.get("turnId") else None
                thread_id = str(message["threadId"])
                context_percent = message.get("contextUsedPercent")
                if turn_id and thread_id == self.active_thread_id and isinstance(context_percent, (int, float)) and 0 <= context_percent <= 100:
                    self.native_context_by_turn[(thread_id, turn_id)] = float(context_percent)
                self.storage.save_message_snapshot(
                    thread_id,
                    str(message["itemId"]),
                    turn_id,
                    str(message.get("phase") or "unknown"),
                    bool(message.get("completed")),
                    self._summary_payload(turn_id, thread_id),
                )
            elif message.get("type") == "context" and message.get("turnId") and message.get("threadId"):
                context_percent = message.get("usedPercent")
                thread_id, turn_id = str(message["threadId"]), str(message["turnId"])
                if thread_id == self.active_thread_id and isinstance(context_percent, (int, float)) and 0 <= context_percent <= 100:
                    self.native_context_by_turn[(thread_id, turn_id)] = float(context_percent)
            elif message.get("type") == "compatibility" and message.get("compatible") is True:
                self.runtime_compatibility = str(message.get("evidence") or "structural")
            elif message.get("type") == "compatibility" and message.get("compatible") is False:
                self.runtime_compatibility = str(message.get("evidence") or "incompatible")
            elif message.get("type") == "history_focus":
                raw_thread = message.get("thread_id")
                thread_id = str(raw_thread)[:128] if raw_thread else None
                keys = ("total_turns", "mounted_turns", "visible_window_turns", "hidden_logical_turns")
                values = [message.get(key) for key in keys]
                valid_counts = all(
                    isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1_000_000
                    for value in values
                )
                window_start = message.get("window_start")
                valid_window = window_start is None or (
                    isinstance(window_start, (int, float)) and not isinstance(window_start, bool)
                    and 1 <= window_start <= 1_000_000
                )
                focus_state = message.get("focus_state")
                boundary_turn = message.get("boundary_turn")
                boundary_scroll_top = message.get("boundary_scroll_top")
                scroll_direction = message.get("scroll_direction")
                guard_active = message.get("guard_active")
                valid_focus = focus_state in {"disabled", "fail_open", "pending_boundary", "gate_only", "active", "complete"}
                valid_boundary = boundary_turn is None or (
                    isinstance(boundary_turn, (int, float)) and not isinstance(boundary_turn, bool)
                    and 1 <= boundary_turn <= 1_000_000
                )
                valid_scroll = boundary_scroll_top is None or (
                    isinstance(boundary_scroll_top, (int, float)) and not isinstance(boundary_scroll_top, bool)
                    and math.isfinite(boundary_scroll_top) and abs(boundary_scroll_top) <= 1_000_000_000
                )
                valid_direction = scroll_direction is None or scroll_direction in {"normal", "column-reverse"}
                if (thread_id == self.active_thread_id and isinstance(message.get("compatible"), bool)
                        and valid_counts and valid_window and valid_focus and valid_boundary and valid_scroll
                        and valid_direction and isinstance(guard_active, bool)):
                    self.history_focus = {
                        "thread_id": thread_id,
                        "compatible": message["compatible"],
                        **{key: int(value) for key, value in zip(keys, values)},
                        "window_start": int(window_start) if window_start is not None else None,
                        "focus_state": focus_state,
                        "boundary_turn": int(boundary_turn) if boundary_turn is not None else None,
                        "boundary_scroll_top": float(boundary_scroll_top) if boundary_scroll_top is not None else None,
                        "scroll_direction": scroll_direction,
                        "guard_active": guard_active,
                    }

    def _respond_history(self, connection: CdpConnection, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId") or "")[:80]
        scope = str(message.get("scope") or self.config.get("ui.history.default_scope", "current_chat"))
        range_name = str(message.get("range") or self.config.get("ui.history.default_range", "7d"))
        seconds = {"24h": 86400, "7d": 7*86400, "30d": 30*86400, "all": None}.get(range_name, 7*86400)
        since = time.time()-seconds if seconds else None
        try:
            rows = self.storage.history(self.active_thread_id, since, scope,
                                        int(self.config.get("ui.history.max_turns", 500)),
                                        int(self.config.get("ui.advisor.baseline_window", 50)))
            payload = {"requestId": request_id, "scope": scope, "range": range_name, "turns": rows,
                       "activeThreadId": self.active_thread_id}
        except (ValueError, sqlite3.Error) as exc:
            payload = {"requestId": request_id, "error": str(exc), "turns": []}
        expression = f"window.__codexUsageHistoryUpdate&&window.__codexUsageHistoryUpdate({json.dumps(payload, separators=(',', ':'))})"
        connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": False}, timeout=1.0)

    def _respond_projects(self, connection: CdpConnection, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId") or "")[:80]
        range_name = str(message.get("range") or self.config.get("ui.projects.default_range", "30d"))
        seconds = {"7d": 7*86400, "30d": 30*86400, "90d": 90*86400, "all": None}.get(range_name, 30*86400)
        project = self.storage.project_for_session(self.active_thread_id)
        try:
            payload = {"requestId": request_id, "range": range_name, "project": project,
                       "suggestedBasename": self._project_basename(project),
                       "insights": self.storage.project_insights(project["cwd_hash"], time.time()-seconds if seconds else None) if project else None}
        except (ValueError, sqlite3.Error) as exc:
            payload = {"requestId": request_id, "error": str(exc), "project": project}
        expression = f"window.__codexCompanionProjectsUpdate&&window.__codexCompanionProjectsUpdate({json.dumps(payload, separators=(',', ':'))})"
        connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": False}, timeout=1.0)

    def _project_basename(self, project: dict[str, Any] | None) -> str | None:
        if not project or project.get("alias") or not self.active_thread_id:
            return None
        row = self.storage.conn.execute(
            "SELECT transcript_path FROM sessions WHERE session_id=?", (self.active_thread_id,)
        ).fetchone()
        path = Path(row["transcript_path"]) if row and row["transcript_path"] else None
        if not path or not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for _ in range(20):
                    line = handle.readline()
                    if not line:
                        break
                    value = json.loads(line)
                    if value.get("type") != "session_meta":
                        continue
                    cwd = (value.get("payload") or {}).get("cwd")
                    if cwd and hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:16] == project["cwd_hash"]:
                        return Path(str(cwd)).name[:80]
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _set_project_alias(self, connection: CdpConnection, message: dict[str, Any]) -> None:
        project = self.storage.project_for_session(self.active_thread_id)
        try:
            if not project or str(message.get("cwdHash") or "") != project["cwd_hash"]:
                raise ValueError("Project does not match the active task")
            self.storage.set_project_alias(project["cwd_hash"], str(message.get("alias") or ""))
        except (ValueError, sqlite3.Error) as exc:
            self._log_error(exc)
        self._respond_projects(connection, message)

    def _refresh_delay(self) -> float:
        if not self.config.get("ui.performance.enabled", True):
            return max(0.1, int(self.config.get("ui.refresh_interval_ms", 200)) / 1000)
        key = {"active": "active_refresh_ms", "idle": "idle_refresh_ms", "background": "background_refresh_ms"}.get(self.performance_state, "idle_refresh_ms")
        return max(0.1, int(self.config.get(f"ui.performance.{key}", 1000)) / 1000)

    def _respond_git_summary(self, connection: CdpConnection, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId") or "")[:80]
        payload: dict[str, Any] = {"requestId": request_id, "files": [], "diffStat": []}
        if not self.config.get("ui.handoff.include_git_summary", True):
            payload["disabled"] = True
        else:
            cwd = self._session_cwd()
            if cwd:
                try:
                    status = subprocess.run(
                        ["git", "status", "--porcelain=v1", "--untracked-files=normal"], cwd=cwd,
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2, check=False,
                    )
                    stat = subprocess.run(
                        ["git", "diff", "--stat", "--"], cwd=cwd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=2, check=False,
                    )
                    payload["files"] = [line[:500] for line in status.stdout.splitlines()[:200]]
                    payload["diffStat"] = [line[:500] for line in stat.stdout.splitlines()[:201]]
                    payload["truncated"] = len(status.stdout.splitlines()) > 200
                except (OSError, subprocess.SubprocessError) as exc:
                    payload["error"] = type(exc).__name__
            else:
                payload["error"] = "cwd_unavailable"
        expression = f"window.__codexCompanionGitUpdate&&window.__codexCompanionGitUpdate({json.dumps(payload, separators=(',', ':'))})"
        connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": False}, timeout=1.0)

    def _session_cwd(self) -> Path | None:
        if not self.active_thread_id:
            return None
        row = self.storage.conn.execute(
            "SELECT transcript_path,cwd_hash FROM sessions WHERE session_id=?", (self.active_thread_id,)
        ).fetchone()
        path = Path(row["transcript_path"]) if row and row["transcript_path"] else None
        if not path or not path.is_file() or not row["cwd_hash"]:
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for _ in range(20):
                    line = handle.readline()
                    if not line:
                        break
                    value = json.loads(line)
                    if value.get("type") != "session_meta":
                        continue
                    cwd = (value.get("payload") or {}).get("cwd")
                    candidate = Path(str(cwd)).resolve() if cwd else None
                    digest = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:16] if cwd else None
                    if candidate and candidate.is_dir() and digest == row["cwd_hash"]:
                        return candidate
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _summary_payload(self, turn_id: str | None, session_id: str | None = None) -> dict[str, Any]:
        summary = self.storage.summary(session_id, turn_id, int(self.config.get("ui.advisor.baseline_window", 50)))
        summary["view"] = derive(summary, self.config)
        selected_turn = summary.get("turn") or {}
        selected_turn_id = str(selected_turn.get("turn_id") or turn_id or "")
        context = summary["view"].get("context") or {}
        selected_session_id = str(selected_turn.get("session_id") or session_id or "")
        native_percent = self.native_context_by_turn.get((selected_session_id, selected_turn_id))
        if native_percent is not None:
            window = context.get("window")
            context.update(
                {
                    "used": round(float(window) * native_percent / 100.0) if window else None,
                    "used_percent": native_percent,
                    "remaining": round(float(window) * (100.0 - native_percent) / 100.0) if window else None,
                    "remaining_percent": 100.0 - native_percent,
                    "source": "observed_renderer",
                }
            )
        elif turn_id and selected_turn.get("ended_at"):
            context.update({"used": None, "used_percent": None, "remaining": None, "remaining_percent": None, "source": "unavailable"})
        summary["view"]["advisor"] = evaluate_advice(summary, summary["view"], self.config)
        summary["view"]["budget"] = evaluate_budget(
            summary, self.config, self.transient_budget_features.get(selected_session_id)
        )
        cockpit = build_task_cockpit(summary, summary["view"])
        summary["view"]["task_health"] = {key: value for key, value in cockpit.items() if key != "activity"}
        summary["view"]["task_activity"] = cockpit["activity"]
        if self.cockpit_events:
            summary["view"]["task_activity"]["events"] = (summary["view"]["task_activity"].get("events") or []) + self.cockpit_events
            summary["view"]["task_activity"]["events"] = summary["view"]["task_activity"]["events"][-20:]
            summary["view"]["task_activity"]["last_event"] = summary["view"]["task_activity"]["events"][-1]["type"]
        if selected_turn.get("ended_at") and selected_turn_id and selected_session_id:
            self.storage.save_advice(
                selected_session_id, selected_turn_id,
                summary["view"]["advisor"].get("all_items") or summary["view"]["advisor"].get("items") or [],
            )
        return summary

    def _write_status(self, **value: Any) -> None:
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        value["updated_at"] = time.time()
        value["host_pid"] = os.getpid()
        value.setdefault("runtime_compatibility", self.runtime_compatibility)
        value.setdefault("active_thread_id", self.active_thread_id)
        value.setdefault("active_session_state", self.active_session_state)
        value.setdefault("active_thread_switched_at", self.active_thread_switched_at)
        value.setdefault("history_focus", self.history_focus)
        value.setdefault("performance", self.performance_diagnostics)
        value.setdefault("budget", self.budget_diagnostics)
        value.setdefault("handoff", self.handoff_diagnostics)
        temp = self.plugin_data / "ui-status.json.tmp"
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temp.replace(self.plugin_data / "ui-status.json")

    def _handle_handoff_event(self, message: dict[str, Any]) -> None:
        nonce = str(message.get("nonce") or "")
        event = str(message.get("type") or "")
        diagnostics = message.get("diagnostics")
        if isinstance(diagnostics, dict):
            allowed = {"exact_adapter", "composer", "new_task_anchor", "clipboard", "preview_capture", "fallback"}
            self.handoff_diagnostics.update({key: bool(value) for key, value in diagnostics.items() if key in allowed})
        if not nonce:
            return
        state_by_event = {
            "handoff_submitted": "submitted", "handoff_captured": "captured", "handoff_open_started": "opened",
            "handoff_prefilling": "prefilling", "handoff_prefill_confirmed": "ready", "handoff_prefill_failed": "fallback",
            "handoff_fallback": "fallback",
        }
        state = state_by_event.get(event)
        if state:
            try:
                self.storage.transition_handoff(
                    nonce, state,
                    source_turn_id=str(message.get("turnId")) if message.get("turnId") else None,
                    target_session_id=self.active_thread_id if state == "ready" else None,
                )
            except ValueError:
                return
    def _log_error(self, exc: Exception) -> None:
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(exc).__name__}: {exc}\n"
        path = self.plugin_data / "ui-host-error.log"
        try:
            previous = path.read_text(encoding="utf-8")[-16000:] if path.is_file() else ""
            path.write_text(previous + line, encoding="utf-8")
        except OSError:
            pass


def _primary_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    pages = [target for target in targets if target.get("type") == "page" and target.get("webSocketDebuggerUrl")]
    preferred = [target for target in pages if str(target.get("url", "")).startswith(("app://", "file://"))]
    return (preferred or pages or [None])[0]


def _package_version(executable: Path) -> str:
    # Microsoft Store package directory is authoritative for the installed desktop package.
    for parent in executable.parents:
        if parent.name.startswith("OpenAI.Codex_"):
            parts = parent.name.split("_")
            return parts[1] if len(parts) > 1 else "unknown"
    return "unknown"
