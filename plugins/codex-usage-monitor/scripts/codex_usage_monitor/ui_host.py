from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from .cdp import CdpConnection, CdpError, discover_targets
from .config import LoadedConfig
from .storage import Storage
from .ui_launcher import discover_codex_app, launch_codex, reserve_loopback_port
from .widgets import load_widgets, markdown_to_html, sanitize_html
from .render import derive


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
        runtime = (self.plugin_root / "ui" / "runtime.js").read_text(encoding="utf-8")
        last_target = None
        connection: CdpConnection | None = None
        while not self.stop and process.poll() is None:
            try:
                target = _primary_target(discover_targets(port))
                if not target:
                    time.sleep(0.1)
                    continue
                target_id = target.get("id")
                if connection is None or connection.closed or target_id != last_target:
                    if connection:
                        connection.close()
                    connection = CdpConnection(str(target["webSocketDebuggerUrl"]))
                    self._attach(connection, runtime, supported, adapter, adapters)
                    last_target = target_id
                    self._write_status(state="attached", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
                self._drain_events(connection)
                self._push_snapshot(connection)
                if time.monotonic() - self._last_heartbeat >= 5.0:
                    self._write_status(state="attached", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
                    self._last_heartbeat = time.monotonic()
                time.sleep(max(0.1, int(self.config.get("ui.refresh_interval_ms", 200)) / 1000))
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
        widgets = load_widgets(
            list(self.config.get("ui.widgets.directories", [])), bool(self.config.get("ui.security.scripts_enabled", True))
        )
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
            "security": self.config.get("ui.security", {}),
        }

    def _push_snapshot(self, connection: CdpConnection) -> None:
        snapshot = self._summary_payload(None, self.active_thread_id)
        turn = snapshot.get("turn") or {}
        if turn.get("turn_id") and turn.get("ended_at"):
            self.storage.refresh_completed_turn_snapshots(str(turn["turn_id"]), snapshot)
        history = self.storage.message_snapshots(self.active_thread_id, limit=500) if self.active_thread_id else []
        payload = {"snapshot": snapshot, "history": history, "at": time.time(), "activeThreadId": self.active_thread_id}
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
            if message.get("type") == "active_thread":
                raw_id = message.get("threadId")
                thread_id = str(raw_id)[:128] if raw_id and not str(raw_id).startswith("client-new-thread:") else None
                if thread_id != self.active_thread_id or message.get("state") != self.active_session_state:
                    self.active_thread_id = thread_id
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

    def _summary_payload(self, turn_id: str | None, session_id: str | None = None) -> dict[str, Any]:
        summary = self.storage.summary(session_id, turn_id)
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
        return summary

    def _write_status(self, **value: Any) -> None:
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        value["updated_at"] = time.time()
        value["host_pid"] = os.getpid()
        value.setdefault("runtime_compatibility", self.runtime_compatibility)
        value.setdefault("active_thread_id", self.active_thread_id)
        value.setdefault("active_session_state", self.active_session_state)
        value.setdefault("active_thread_switched_at", self.active_thread_switched_at)
        temp = self.plugin_data / "ui-status.json.tmp"
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temp.replace(self.plugin_data / "ui-status.json")

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
