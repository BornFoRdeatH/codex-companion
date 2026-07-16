#!/usr/bin/env python3
import argparse
import os
from pathlib import Path

from codex_usage_monitor.collector import serve


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    plugin_root = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
    plugin_data = Path(
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CODEX_USAGE_MONITOR_DATA")
        or Path.home() / ".codex" / "plugin-data" / "codex-usage-monitor"
    )
    return serve(plugin_root, plugin_data)


if __name__ == "__main__":
    raise SystemExit(main())
