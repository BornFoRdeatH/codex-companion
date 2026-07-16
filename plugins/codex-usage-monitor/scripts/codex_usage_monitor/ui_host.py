from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from .cdp import CdpConnection, CdpError, discover_targets
from .config import LoadedConfig
from .storage import Storage
from .ui_launcher import discover_codex_app, launch_codex, reserve_loopback_port
from .widgets import load_widgets, markdown_to_html, sanitize_html


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
    def __init__(self, plugin_root: Path, plugin_data: Path, config: LoadedConfig, storage: Storage):
        self.plugin_root = plugin_root
        self.plugin_data = plugin_data
        self.config = config
        self.storage = storage
        self.stop = False
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))

    def run(self) -> int:
        executable = discover_codex_app(self.plugin_data)
        if not executable:
            self._write_status(state="error", error="Codex desktop executable not found")
            return 2
        port = reserve_loopback_port()
        process = launch_codex(executable, port)
        fp = fingerprint(executable)
        adapter = match_adapter(fp, load_adapters(self.plugin_root))
        policy = self.config.get("ui.unknown_version_policy", "dock_only")
        supported = adapter is not None
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
                    self._attach(connection, runtime, supported, adapter)
                    last_target = target_id
                    self._write_status(state="attached", pid=process.pid, port=port, fingerprint=fp, adapter=adapter)
                self._drain_events(connection)
                self._push_snapshot(connection)
                time.sleep(max(0.1, int(self.config.get("ui.refresh_interval_ms", 200)) / 1000))
            except (OSError, CdpError, KeyError, json.JSONDecodeError) as exc:
                self._write_status(state="reconnecting", pid=process.pid, port=port, error=str(exc), fingerprint=fp)
                if connection:
                    connection.close()
                connection = None
                time.sleep(0.25)
        if connection:
            connection.close()
        self._write_status(state="stopped", exit_code=process.poll(), fingerprint=fp)
        return int(process.poll() or 0)

    def _attach(self, connection: CdpConnection, runtime: str, supported: bool, adapter: dict[str, Any] | None) -> None:
        connection.call("Page.enable")
        connection.call("Runtime.enable")
        connection.call("Runtime.addBinding", {"name": BINDING})
        boot = self._boot_payload(supported, adapter)
        source = f"window.__CODEX_USAGE_BOOT__={json.dumps(boot, separators=(',', ':'))};\n{runtime}"
        connection.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        connection.call("Runtime.evaluate", {"expression": source, "awaitPromise": False})

    def _boot_payload(self, supported: bool, adapter: dict[str, Any] | None) -> dict[str, Any]:
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
            "adapter": adapter or {},
            "dockPosition": self.config.get("ui.dock_position", "right_dock"),
            "dockSize": self.config.get("ui.dock_size", 340),
            "footerPhases": self.config.get("ui.footer_phases", ["commentary", "final_answer"]),
            "widgets": widgets,
            "security": self.config.get("ui.security", {}),
        }

    def _push_snapshot(self, connection: CdpConnection) -> None:
        snapshot = self.storage.summary(None, None)
        payload = {"snapshot": snapshot, "history": self.storage.message_snapshots(limit=500), "at": time.time()}
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
            if message.get("type") == "item" and message.get("threadId") and message.get("itemId"):
                self.storage.save_message_snapshot(
                    str(message["threadId"]),
                    str(message["itemId"]),
                    str(message.get("turnId")) if message.get("turnId") else None,
                    str(message.get("phase") or "unknown"),
                    bool(message.get("completed")),
                    self.storage.summary(None, message.get("turnId")),
                )

    def _write_status(self, **value: Any) -> None:
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        value["updated_at"] = time.time()
        value["host_pid"] = os.getpid()
        temp = self.plugin_data / "ui-status.json.tmp"
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temp.replace(self.plugin_data / "ui-status.json")


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
