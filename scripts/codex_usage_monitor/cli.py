from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path

from .collector import ensure_collector
from .config import ConfigError, load_config
from .render import render
from .storage import Storage


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
    reset = sub.add_parser("reset-cache")
    reset.add_argument("--yes", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plugin_root = Path(__file__).resolve().parents[2]
    plugin_data = args.data_dir or Path(
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CODEX_USAGE_MONITOR_DATA")
        or Path.home() / ".codex" / "plugin-data" / "codex-usage-monitor"
    )
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
        if args.command == "status":
            print(render(storage.summary(args.session_id, None), config, args.profile))
            return 0
        if args.command == "export-summary":
            print(json.dumps(storage.summary(args.session_id, None), indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command == "doctor":
            details = {
                "plugin_root": str(plugin_root),
                "plugin_data": str(plugin_data),
                "config": str(config.path),
                "database": str(storage.path),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "codex": shutil.which("codex"),
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
