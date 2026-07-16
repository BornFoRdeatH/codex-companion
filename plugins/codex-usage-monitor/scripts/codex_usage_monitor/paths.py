from __future__ import annotations

import os
from pathlib import Path


def resolve_plugin_data(plugin_root: Path, explicit: Path | None = None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("CODEX_USAGE_MONITOR_DATA")
    if configured:
        return Path(configured).expanduser().resolve()
    data_root = Path.home() / ".codex" / "plugins" / "data"
    marketplace = _marketplace_from_root(plugin_root)
    if marketplace:
        exact = data_root / f"codex-usage-monitor-{marketplace}"
        return exact
    candidates = list(data_root.glob("codex-usage-monitor-*")) if data_root.is_dir() else []
    if candidates:
        # Prefer the active Git marketplace, then the freshest existing installation.
        candidates.sort(
            key=lambda path: ("bornfordeath-plugins" in path.name, _modified(path)), reverse=True
        )
        return candidates[0]
    return Path.home() / ".codex" / "plugins" / "data" / "codex-usage-monitor-local"


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _marketplace_from_root(plugin_root: Path) -> str | None:
    parts = plugin_root.resolve().parts
    for marker in ("cache", "marketplaces"):
        indices = [index for index, value in enumerate(parts) if value.lower() == marker]
        if indices and indices[-1] + 1 < len(parts):
            return parts[indices[-1] + 1]
    return None
