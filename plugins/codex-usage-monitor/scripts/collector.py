#!/usr/bin/env python3
import argparse
import os
from pathlib import Path

from codex_usage_monitor.collector import serve
from codex_usage_monitor.paths import resolve_plugin_data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    plugin_root = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
    plugin_data = resolve_plugin_data(plugin_root)
    return serve(plugin_root, plugin_data)


if __name__ == "__main__":
    raise SystemExit(main())
