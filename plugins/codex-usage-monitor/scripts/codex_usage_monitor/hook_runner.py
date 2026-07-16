from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .collector import ensure_collector
from .config import ConfigError, LoadedConfig, load_config
from .render import content_hash, render
from .paths import resolve_plugin_data
from .storage import Storage
from .transcript import TranscriptParser


EVENT_CONFIG = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt",
    "PostToolUse": "tool_complete",
    "Stop": "turn_stop",
    "SubagentStop": "subagent_stop",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
}

REFRESH_CONFIG = {
    "SessionStart": "on_session_start",
    "UserPromptSubmit": "on_user_prompt",
    "PostToolUse": "on_tool_complete",
    "Stop": "on_turn_stop",
    "SubagentStop": "on_subagent_stop",
    "PreCompact": "on_pre_compact",
    "PostCompact": "on_post_compact",
}


def main() -> int:
    started = time.perf_counter()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    plugin_root = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parents[2])
    plugin_data = resolve_plugin_data(plugin_root)
    response: dict[str, Any] = {"continue": True}
    storage: Storage | None = None
    try:
        config = load_config(plugin_root, plugin_data)
        if not config.get("enabled", True):
            _emit(response)
            return 0
        storage = Storage(Path(config.get("storage.database")))
        session_id = str(payload.get("session_id") or "unknown")
        turn_id = payload.get("turn_id")
        event = str(payload.get("hook_event_name") or "Unknown")
        cwd_hash = _path_hash(payload.get("cwd")) if config.get("privacy.redact_paths", True) else payload.get("cwd")
        storage.upsert_session(session_id, payload.get("transcript_path"), payload.get("model"), cwd_hash)
        if payload.get("model"):
            config.data["_runtime"] = {"model": payload["model"]}
        refresh_key = REFRESH_CONFIG.get(event)
        refresh_enabled = refresh_key is None or config.get(f"refresh.{refresh_key}", True)
        if (
            refresh_enabled
            and config.get("data_sources.session_transcript", True)
            and config.get("experimental.parse_session_jsonl", True)
        ):
            TranscriptParser(storage).ingest(payload.get("transcript_path"), session_id)
        _record_event(storage, payload, event, session_id, turn_id)
        if refresh_enabled and config.get("storage.enabled", True):
            ensure_collector(plugin_root, plugin_data, storage)
        _prune_if_due(storage, config)
        event_key = EVENT_CONFIG.get(event)
        ui_suppresses = config.get("ui.enabled", True) and config.get("ui.suppress_hook_system_messages", True)
        if event_key and not ui_suppresses and config.get(f"display.events.{event_key}.enabled", False) and config.get("display.enabled", True):
            summary = storage.summary(session_id, turn_id)
            profile = config.get(f"display.events.{event_key}.profile", config.get("display.default_profile", "adaptive"))
            message = render(summary, config, profile)
            if _should_show(storage, config, event_key, message, summary):
                response["systemMessage"] = message
        elapsed = (time.perf_counter() - started) * 1000
        storage.record_hook(session_id, turn_id, event, elapsed)
        if config.warnings and config.get("diagnostics.show_collection_errors", True):
            _log(config, "\n".join(config.warnings))
    except Exception as exc:  # Hooks must fail open.
        _log_fallback(plugin_data, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        if _show_errors_safely(plugin_root, plugin_data):
            response["systemMessage"] = f"Codex Usage Monitor: collection error ({type(exc).__name__}); see diagnostics log."
    finally:
        if storage:
            storage.close()
    _emit(response)
    return 0


def _record_event(
    storage: Storage,
    payload: dict[str, Any],
    event: str,
    session_id: str,
    turn_id: str | None,
) -> None:
    if event == "UserPromptSubmit" and turn_id:
        storage.start_turn(turn_id, session_id)
    elif event == "Stop" and turn_id:
        storage.end_turn(turn_id)
    elif event == "PreToolUse":
        storage.record_tool_start(payload, session_id, turn_id)
    elif event == "PostToolUse":
        storage.record_tool_end(payload, session_id, turn_id)
    elif event == "PreCompact":
        storage.record_compaction(session_id, turn_id, "pre", payload.get("trigger"))
    elif event == "PostCompact":
        storage.record_compaction(session_id, turn_id, "post", payload.get("trigger"))
    elif event == "SubagentStart":
        storage.record_subagent(payload, session_id, turn_id, True)
    elif event == "SubagentStop":
        storage.record_subagent(payload, session_id, turn_id, False)


def _should_show(
    storage: Storage,
    config: LoadedConfig,
    event_key: str,
    message: str,
    summary: dict[str, Any],
) -> bool:
    if not message:
        return False
    only_changed = config.get(f"display.events.{event_key}.only_when_changed", False)
    key = f"last_render_hash:{event_key}"
    digest = content_hash(message)
    previous = storage.get_meta(key)
    if only_changed and previous == digest:
        return False
    if event_key == "tool_complete":
        turn = summary.get("turn") or {}
        token = summary.get("token") or {}
        delta = int(token.get("total_tokens") or 0) - int(turn.get("baseline_total") or 0)
        minimum = int(config.get("display.events.tool_complete.minimum_token_delta", 0))
        if delta < minimum and previous == digest:
            return False
    storage.set_meta(key, digest)
    return True


def _path_hash(value: Any) -> str | None:
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _emit(response: dict[str, Any]) -> None:
    # ASCII-only JSON keeps hook output valid under Windows legacy code pages.
    # Codex decodes the escapes back to the intended Unicode system message.
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")


def _log(config: LoadedConfig, text: str) -> None:
    if not config.get("diagnostics.enabled", False) and "error" not in text.lower():
        return
    path = Path(config.get("diagnostics.log_file"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def _log_fallback(plugin_data: Path, text: str) -> None:
    try:
        plugin_data.mkdir(parents=True, exist_ok=True)
        with (plugin_data / "usage-monitor.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _show_errors_safely(plugin_root: Path, plugin_data: Path) -> bool:
    try:
        config = load_config(plugin_root, plugin_data, create=False)
        return bool(config.get("diagnostics.show_collection_errors", True))
    except ConfigError:
        return True


def _prune_if_due(storage: Storage, config: LoadedConfig) -> None:
    today = time.strftime("%Y-%m-%d")
    if storage.get_meta("last_prune") == today:
        return
    storage.prune(int(config.get("storage.retention_days", 365)))
    storage.set_meta("last_prune", today)
