from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .collector import ensure_collector, find_codex_executable
from .budget import evaluate as evaluate_budget
from .cdp import CdpConnection, CdpError, discover_targets
from .config import ConfigError, load_config
from .render import render
from .render import derive
from .storage import Storage
from .paths import resolve_plugin_data
from .ui_host import UiHost, _primary_target, fingerprint, load_adapters, match_adapter
from .ui_launcher import discover_codex_app, install_launcher, status as ui_status, uninstall_launcher


_ASCII_CONSOLE = str.maketrans({
    "╭": "+", "╮": "+", "╰": "+", "╯": "+", "─": "-", "│": "|",
    "·": ".", "≈": "~", "Δ": "delta ", "█": "#", "░": "-", "✓": "+", "×": "x",
})


def console_safe(value: str, encoding: str | None = None) -> str:
    encoding = encoding or sys.stdout.encoding or "utf-8"
    try:
        value.encode(encoding)
        return value
    except (LookupError, UnicodeEncodeError):
        fallback = value.translate(_ASCII_CONSOLE)
        return fallback.encode(encoding, errors="replace").decode(encoding, errors="replace")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="usage-monitor")
    result.add_argument("--data-dir", type=Path)
    sub = result.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--profile", choices=("compact", "normal", "full", "adaptive"), default="full")
    status.add_argument("--session-id")
    sub.add_parser("doctor")
    sub.add_parser("config-path")
    sub.add_parser("validate-config")
    export = sub.add_parser("export-summary")
    export.add_argument("--session-id")
    history = sub.add_parser("export-history")
    history.add_argument("--session-id")
    history.add_argument("--since", default="7d")
    history.add_argument("--format", choices=("json", "csv"), default="json")
    advice = sub.add_parser("advice")
    advice.add_argument("--session-id", required=True)
    budget = sub.add_parser("budget")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    budget_status = budget_sub.add_parser("status")
    budget_status.add_argument("--session-id")
    projects = sub.add_parser("projects")
    projects_sub = projects.add_subparsers(dest="projects_command", required=True)
    projects_sub.add_parser("list")
    alias = projects_sub.add_parser("alias")
    alias.add_argument("--cwd-hash", required=True)
    alias.add_argument("--name", required=True)
    export_project = sub.add_parser("export-project")
    export_project.add_argument("--cwd-hash", required=True)
    export_project.add_argument("--since", default="30d")
    export_project.add_argument("--format", choices=("json", "csv"), default="json")
    handoff = sub.add_parser("handoff")
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_sub.add_parser("doctor")
    reset = sub.add_parser("reset-cache")
    reset.add_argument("--yes", action="store_true")
    ui = sub.add_parser("ui")
    ui_sub = ui.add_subparsers(dest="ui_command", required=True)
    launch = ui_sub.add_parser("launch")
    launch.add_argument("--restart-existing", action="store_true")
    ui_sub.add_parser("install")
    ui_sub.add_parser("uninstall")
    ui_sub.add_parser("doctor")
    ui_sub.add_parser("inspect")
    ui_sub.add_parser("status")
    ui_sub.add_parser("adapters")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plugin_root = Path(__file__).resolve().parents[2]
    plugin_data = resolve_plugin_data(plugin_root, args.data_dir)
    try:
        config = load_config(plugin_root, plugin_data)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "config-path":
        print(config.path)
        return 0
    if args.command == "validate-config":
        if config.warnings:
            print("\n".join(config.warnings))
            return 1
        print("Configuration is valid.")
        return 0
    storage = Storage(Path(config.get("storage.database")))
    try:
        if args.command == "ui":
            if args.ui_command == "launch":
                if not config.get("ui.enabled", True):
                    print("UI is disabled in config.toml.", file=sys.stderr)
                    return 2
                return UiHost(plugin_root, plugin_data, config, storage, restart_existing=args.restart_existing).run()
            if args.ui_command == "install":
                for path in install_launcher(plugin_root, plugin_data):
                    print(path)
                return 0
            if args.ui_command == "uninstall":
                removed = uninstall_launcher(plugin_data)
                print("\n".join(map(str, removed)) if removed else "No launcher was installed.")
                return 0
            if args.ui_command == "status":
                print(json.dumps(ui_status(plugin_data), indent=2, ensure_ascii=False))
                return 0
            if args.ui_command == "inspect":
                details = ui_status(plugin_data)
                port = details.get("port")
                if not isinstance(port, int) or port <= 0:
                    print(json.dumps({"available": False, "reason": "host_port_unavailable"}, indent=2))
                    return 1
                target = _primary_target(discover_targets(port))
                if not target:
                    print(json.dumps({"available": False, "reason": "renderer_target_unavailable"}, indent=2))
                    return 1
                connection = CdpConnection(str(target["webSocketDebuggerUrl"]))
                try:
                    result = connection.call(
                        "Runtime.evaluate",
                        {
                            "expression": "window.__codexCompanionDomInspect&&window.__codexCompanionDomInspect()",
                            "returnByValue": True,
                        },
                        timeout=2.0,
                    )
                    value = ((result or {}).get("result") or {}).get("value")
                    print(json.dumps(value or {"available": False, "reason": "inspection_api_unavailable"}, indent=2, ensure_ascii=False))
                    return 0 if value else 1
                except (CdpError, OSError) as exc:
                    print(json.dumps({"available": False, "reason": type(exc).__name__}, indent=2))
                    return 1
                finally:
                    connection.close()
            executable = discover_codex_app(plugin_data)
            fp = fingerprint(executable) if executable else None
            adapters = load_adapters(plugin_root)
            if args.ui_command == "adapters":
                print(json.dumps({"fingerprint": fp, "matched": match_adapter(fp, adapters) if fp else None, "adapters": adapters}, indent=2))
                return 0
            if args.ui_command == "doctor":
                details = ui_status(plugin_data)
                details.update({"fingerprint": fp, "adapter": match_adapter(fp, adapters) if fp else None, "loopback_only": True})
                print(json.dumps(details, indent=2, ensure_ascii=False))
                return 0 if executable else 1
        if args.command == "status":
            print(console_safe(render(storage.summary(args.session_id, None, int(config.get("ui.advisor.baseline_window", 50))), config, args.profile)))
            return 0
        if args.command == "export-summary":
            print(json.dumps(storage.summary(args.session_id, None, int(config.get("ui.advisor.baseline_window", 50))), indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command == "advice":
            summary = storage.summary(args.session_id, None, int(config.get("ui.advisor.baseline_window", 50)))
            payload = derive(summary, config).get("advisor") or {"items": [], "highest": None}
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command == "budget":
            summary = storage.summary(args.session_id, None, int(config.get("ui.budget.baseline_window", 50)))
            summary["view"] = derive(summary, config)
            print(json.dumps(evaluate_budget(summary, config), indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command == "projects":
            if args.projects_command == "alias":
                storage.set_project_alias(args.cwd_hash, args.name)
                print(json.dumps(storage.project_insights(args.cwd_hash), indent=2, ensure_ascii=False, default=str))
            else:
                print(json.dumps(storage.list_projects(), indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command == "export-project":
            payload = storage.project_insights(args.cwd_hash, _since_timestamp(args.since))
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                rows = payload.get("daily") or []
                output = io.StringIO(newline="")
                fields = ["day", "turns", "total_tokens", "duration_seconds", "tool_seconds", "tool_calls",
                          "failed_tool_calls", "file_edits", "compactions", "primary_quota_delta"]
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                print(output.getvalue(), end="")
            return 0
        if args.command == "handoff":
            status = ui_status(plugin_data)
            lifecycle = storage.handoff_lifecycle(session_id=None)
            handoff_status = (status.get("handoff") or {}) if isinstance(status, dict) else {}
            columns = {row[1] for row in storage.conn.execute("PRAGMA table_info(handoff_lifecycle)")}
            forbidden_columns = {"prompt", "summary", "content", "markdown", "text", "diff"}
            checks = {
                "exact_adapter": status.get("runtime_compatibility") == "exact",
                "composer": bool(handoff_status.get("composer")),
                "native_new_task_anchor": bool(handoff_status.get("new_task_anchor")),
                "clipboard": bool(handoff_status.get("clipboard")),
                "preview_capture": bool(handoff_status.get("preview_capture")),
                "fallback": bool(handoff_status.get("fallback")),
                "metadata_only_schema": not bool(columns & forbidden_columns),
            }
            details = {
                "enabled": bool(config.get("ui.handoff.enabled", True)),
                "generation": config.get("ui.handoff.generation"),
                "privacy": {"stores_prompt": False, "stores_summary": False, "stores_diff_contents": False},
                "pending_requests": storage.conn.execute(
                    "SELECT COUNT(*) count FROM handoff_requests WHERE state='pending' AND expires_at>?", (time.time(),)
                ).fetchone()["count"],
                "exact_adapter_required": True,
                "copy_fallback": bool(config.get("ui.handoff.copy_fallback", True)),
                "schema_version": storage.get_meta("schema_version"),
                "checks": checks,
                "lifecycle": lifecycle,
            }
            print(json.dumps(details, indent=2, ensure_ascii=False))
            return 0 if all(checks.values()) else 1
        if args.command == "export-history":
            rows = storage.history(args.session_id, _since_timestamp(args.since),
                                   "current_chat" if args.session_id else "all_chats", 5000,
                                   int(config.get("ui.advisor.baseline_window", 50)))
            if args.format == "json":
                print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
            else:
                output = io.StringIO(newline="")
                fields = list(rows[0]) if rows else ["turn_id", "session_id", "started_at", "ended_at"]
                writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                print(output.getvalue(), end="")
            return 0
        if args.command == "doctor":
            details = {
                "plugin_root": str(plugin_root),
                "plugin_data": str(plugin_data),
                "config": str(config.path),
                "database": str(storage.path),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "codex": find_codex_executable(),
                "collector_pid": storage.get_meta("collector_pid"),
                "collector_error": storage.get_meta("collector_error"),
                "warnings": list(config.warnings),
            }
            print(json.dumps(details, indent=2, ensure_ascii=False))
            return 0 if details["python"] >= "3.11" else 1
        if args.command == "reset-cache":
            if not args.yes:
                print("Refusing to reset cache without --yes.", file=sys.stderr)
                return 2
            path = storage.path
            storage.reset()
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            print(f"Reset {path}")
            return 0
    finally:
        try:
            storage.close()
        except sqlite3.ProgrammingError:
            pass
    return 2


def _since_timestamp(value: str) -> float | None:
    if value == "all":
        return None
    unit = value[-1:].lower()
    try:
        amount = float(value[:-1])
    except (TypeError, ValueError):
        raise SystemExit("--since must be 24h, 7d, 30d, all, or another positive duration")
    seconds = {"h": 3600, "d": 86400}.get(unit)
    if not seconds or amount < 0:
        raise SystemExit("--since must use h or d")
    return time.time() - amount * seconds
