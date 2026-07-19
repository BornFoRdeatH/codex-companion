from __future__ import annotations

import copy
import difflib
import os
import shutil
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedConfig:
    data: dict[str, Any]
    path: Path
    warnings: tuple[str, ...]

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.data
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


IMMUTABLE_CONFIG_PATHS = frozenset({
    "privacy.never_store_auth_tokens",
    "privacy.never_store_prompt_contents",
    "privacy.never_store_assistant_text",
    "storage.store_prompt_text",
    "storage.store_assistant_text",
    "storage.store_tool_inputs",
    "storage.store_tool_outputs",
    "ui.security.page_dom_denied",
    "ui.security.message_contents_denied",
    "ui.security.network_denied",
})


def _dotted_values(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in value.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            result.update(_dotted_values(child, dotted))
        else:
            result[dotted] = child
    return result


def validate_config_text(plugin_root: Path, plugin_data: Path, text: str) -> dict[str, Any]:
    """Validate editor input without changing the active config file."""
    if not isinstance(text, str):
        return {"valid": False, "warnings": [], "errors": ["Configuration must be text"], "immutable": []}
    try:
        override = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {"valid": False, "warnings": [], "errors": [f"TOML error: {exc}"], "immutable": []}
    immutable = []
    for dotted, value in _dotted_values(override).items():
        if dotted in IMMUTABLE_CONFIG_PATHS:
            expected = True if dotted.startswith(("privacy.", "ui.security.")) else False
            if value != expected:
                immutable.append(dotted)
    if immutable:
        return {"valid": False, "warnings": [], "errors": [f"Protected setting cannot be changed: {key}" for key in immutable], "immutable": immutable}
    with tempfile.TemporaryDirectory(prefix="codex-companion-config-") as directory:
        candidate = Path(directory) / "config.toml"
        candidate.write_text(text, encoding="utf-8", newline="\n")
        try:
            loaded = load_config(plugin_root, Path(directory), create=False)
        except (ConfigError, OSError, tomllib.TOMLDecodeError) as exc:
            return {"valid": False, "warnings": [], "errors": [str(exc)], "immutable": []}
    return {"valid": True, "warnings": list(loaded.warnings), "errors": [], "immutable": [], "schema_version": loaded.get("schema_version")}


def config_preview(plugin_root: Path, plugin_data: Path, text: str) -> dict[str, Any]:
    validation = validate_config_text(plugin_root, plugin_data, text)
    if not validation["valid"]:
        return {**validation, "diff": ""}
    current = plugin_data / "config.toml"
    before = current.read_text(encoding="utf-8").splitlines() if current.exists() else []
    after = text.splitlines()
    diff = "\n".join(difflib.unified_diff(before, after, fromfile="config.toml", tofile="edited config.toml", lineterm=""))
    return {**validation, "diff": diff}


def save_config_text(plugin_root: Path, plugin_data: Path, text: str) -> dict[str, Any]:
    validation = validate_config_text(plugin_root, plugin_data, text)
    if not validation["valid"]:
        return validation
    plugin_data.mkdir(parents=True, exist_ok=True)
    target = plugin_data / "config.toml"
    backup = None
    if target.exists():
        backup = plugin_data / f"config.toml.bak.{int(time.time())}"
        shutil.copy2(target, backup)
    fd, temp_name = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=plugin_data)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return {**validation, "saved": True, "backup": str(backup) if backup else None, "path": str(target)}


def reset_config(plugin_root: Path, plugin_data: Path) -> dict[str, Any]:
    default_path = plugin_root / "config.default.toml"
    return save_config_text(plugin_root, plugin_data, default_path.read_text(encoding="utf-8"))


def _merge(defaults: dict[str, Any], override: dict[str, Any], prefix: str = "") -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(defaults)
    warnings: list[str] = []
    for key, value in override.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key not in defaults:
            warnings.append(f"Unknown config key: {dotted}")
            continue
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                warnings.append(f"Expected table for {dotted}; using default")
                continue
            merged, nested = _merge(defaults[key], value, dotted)
            result[key] = merged
            warnings.extend(nested)
        elif not isinstance(value, type(defaults[key])) and defaults[key] is not None:
            if isinstance(defaults[key], float) and isinstance(value, int):
                result[key] = float(value)
            else:
                warnings.append(f"Invalid type for {dotted}; using default")
        else:
            result[key] = value
    return result, warnings


def _validate(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if data.get("schema_version") != 1:
        raise ConfigError("Only schema_version = 1 is supported")
    if data["display"]["default_profile"] not in {"compact", "normal", "full", "adaptive"}:
        warnings.append("Invalid display.default_profile; using adaptive")
        data["display"]["default_profile"] = "adaptive"
    if data["data_sources"]["thread_usage_strategy"] not in {"auto", "transcript", "estimate", "off"}:
        warnings.append("Invalid data_sources.thread_usage_strategy; using auto")
        data["data_sources"]["thread_usage_strategy"] = "auto"
    if data["ui"]["dock_position"] not in {"right_dock", "left_dock", "bottom_dock", "floating"}:
        warnings.append("Invalid ui.dock_position; using right_dock")
        data["ui"]["dock_position"] = "right_dock"
    if data["ui"]["layout_mode"] not in {"reserve_space", "overlay"}:
        warnings.append("Invalid ui.layout_mode; using reserve_space")
        data["ui"]["layout_mode"] = "reserve_space"
    if data["ui"]["unknown_version_policy"] not in {"dock_only", "disable"}:
        warnings.append("Invalid ui.unknown_version_policy; using dock_only")
        data["ui"]["unknown_version_policy"] = "dock_only"
    if data["locale"]["language"] not in {"uk", "en"}:
        warnings.append("Invalid locale.language; using en (supported: uk, en)")
        data["locale"]["language"] = "en"
    if data["ui"]["refresh_interval_ms"] < 100:
        warnings.append("ui.refresh_interval_ms must be at least 100; using 200")
        data["ui"]["refresh_interval_ms"] = 200
    if data["ui"]["dock_size"] < 180:
        warnings.append("ui.dock_size must be at least 180; using 340")
        data["ui"]["dock_size"] = 340
    if data["ui"]["guard"]["cooldown_minutes"] < 0:
        warnings.append("ui.guard.cooldown_minutes must be non-negative; using 15")
        data["ui"]["guard"]["cooldown_minutes"] = 15
    if data["ui"]["history"]["default_scope"] not in {"current_chat", "all_chats"}:
        warnings.append("Invalid ui.history.default_scope; using current_chat")
        data["ui"]["history"]["default_scope"] = "current_chat"
    if data["ui"]["history"]["default_range"] not in {"24h", "7d", "30d", "all"}:
        warnings.append("Invalid ui.history.default_range; using 7d")
        data["ui"]["history"]["default_range"] = "7d"
    if data["ui"]["history"]["max_turns"] < 1:
        warnings.append("ui.history.max_turns must be positive; using 500")
        data["ui"]["history"]["max_turns"] = 500
    focus_mode = data["ui"]["focus_mode"]
    for key in ("visible_turns", "load_batch"):
        if not 3 <= focus_mode[key] <= 100:
            warnings.append(f"ui.focus_mode.{key} must be between 3 and 100; using 3")
            focus_mode[key] = 3
    if focus_mode["unknown_version_policy"] not in {"probe", "disable"}:
        warnings.append("Invalid ui.focus_mode.unknown_version_policy; using probe")
        focus_mode["unknown_version_policy"] = "probe"
    advisor = data["ui"]["advisor"]
    if advisor["cooldown_minutes"] < 0:
        warnings.append("ui.advisor.cooldown_minutes must be non-negative; using 30")
        advisor["cooldown_minutes"] = 30
    if advisor["min_personal_turns"] < 1:
        warnings.append("ui.advisor.min_personal_turns must be positive; using 10")
        advisor["min_personal_turns"] = 10
    if advisor["baseline_window"] < advisor["min_personal_turns"]:
        warnings.append("ui.advisor.baseline_window must cover min_personal_turns; using 50")
        advisor["baseline_window"] = 50
    if advisor["max_visible"] < 1:
        warnings.append("ui.advisor.max_visible must be positive; using 1")
        advisor["max_visible"] = 1
    budget = data["ui"]["budget"]
    if budget.get("optimizer_action_mode") not in {"advisory"}:
        warnings.append("Invalid ui.budget.optimizer_action_mode; using advisory")
        budget["optimizer_action_mode"] = "advisory"
    if budget["per_turn_tokens"] < 0:
        warnings.append("ui.budget.per_turn_tokens must be non-negative; using adaptive")
        budget["per_turn_tokens"] = 0
    for key, fallback in (("weekly_remaining_reserve_percent", 10), ("warn_at_percent", 80), ("critical_at_percent", 100)):
        if not 0 <= budget[key] <= 100:
            warnings.append(f"ui.budget.{key} must be between 0 and 100; using {fallback}")
            budget[key] = fallback
    if budget["critical_at_percent"] < budget["warn_at_percent"]:
        warnings.append("ui.budget.critical_at_percent must cover warn_at_percent; using 100")
        budget["critical_at_percent"] = 100
    if budget["min_personal_turns"] < 1:
        warnings.append("ui.budget.min_personal_turns must be positive; using 10")
        budget["min_personal_turns"] = 10
    if budget["baseline_window"] < budget["min_personal_turns"]:
        warnings.append("ui.budget.baseline_window must cover min_personal_turns; using 50")
        budget["baseline_window"] = 50
    if budget.get("minimum_context_samples", 3) < 1:
        warnings.append("ui.budget.minimum_context_samples must be positive; using 3")
        budget["minimum_context_samples"] = 3
    for key, fallback in (("context_warning_percent", 70), ("context_checkpoint_percent", 80),
                          ("context_handoff_percent", 88), ("context_new_task_percent", 93),
                          ("context_safety_reserve_percent", 5)):
        if not 0 <= budget.get(key, fallback) <= 100:
            warnings.append(f"ui.budget.{key} must be between 0 and 100; using {fallback}")
            budget[key] = fallback
    if not (budget["context_warning_percent"] <= budget["context_checkpoint_percent"]
            <= budget["context_handoff_percent"] <= budget["context_new_task_percent"]):
        warnings.append("Context optimizer thresholds must be ascending; using defaults")
        budget.update({"context_warning_percent": 70, "context_checkpoint_percent": 80,
                       "context_handoff_percent": 88, "context_new_task_percent": 93})
    if data["ui"]["projects"]["default_range"] not in {"7d", "30d", "90d", "all"}:
        warnings.append("Invalid ui.projects.default_range; using 30d")
        data["ui"]["projects"]["default_range"] = "30d"
    performance = data["ui"]["performance"]
    for key, fallback in (("active_refresh_ms", 200), ("idle_refresh_ms", 1000), ("background_refresh_ms", 5000)):
        if performance[key] < 100:
            warnings.append(f"ui.performance.{key} must be at least 100; using {fallback}")
            performance[key] = fallback
    handoff = data["ui"]["handoff"]
    if handoff["generation"] != "marked_current_turn":
        warnings.append("Invalid ui.handoff.generation; using marked_current_turn")
        handoff["generation"] = "marked_current_turn"
    if not 1000 <= handoff["max_summary_chars"] <= 100000:
        warnings.append("ui.handoff.max_summary_chars must be 1000-100000; using 20000")
        handoff["max_summary_chars"] = 20000
    if not 1000 <= handoff["max_checkpoint_chars"] <= handoff["max_summary_chars"]:
        warnings.append("ui.handoff.max_checkpoint_chars must be between 1000 and max_summary_chars; using 8000")
        handoff["max_checkpoint_chars"] = min(8000, handoff["max_summary_chars"])
    if not 500 <= handoff["navigation_timeout_ms"] <= 10000:
        warnings.append("ui.handoff.navigation_timeout_ms must be between 500 and 10000; using 2500")
        handoff["navigation_timeout_ms"] = 2500
    sections = handoff.get("required_sections")
    if not isinstance(sections, list) or not all(isinstance(value, str) and value.strip() for value in sections):
        warnings.append("ui.handoff.required_sections must be a non-empty string list; using defaults")
        handoff["required_sections"] = ["Goal", "Current state", "Completed work", "Decisions and constraints", "Changed files", "Verification", "Open issues", "Next steps"]
    widgets = data["ui"].get("widgets", {})
    if not isinstance(widgets.get("directories"), list) or not all(isinstance(value, str) and value.strip() for value in widgets["directories"]):
        warnings.append("ui.widgets.directories must be a string list; using plugin and data directories")
        widgets["directories"] = ["${PLUGIN_ROOT}/ui/widgets", "${PLUGIN_DATA}/ui/widgets"]
    if not isinstance(widgets.get("ordering", []), list) or not all(isinstance(value, str) for value in widgets.get("ordering", [])):
        warnings.append("ui.widgets.ordering must be a string list; using empty ordering")
        widgets["ordering"] = []
    widgets["enabled"] = bool(widgets.get("enabled", True))
    widgets["manager_enabled"] = bool(widgets.get("manager_enabled", True))
    widgets["allow_local"] = bool(widgets.get("allow_local", True))
    widgets["enabled_by_default"] = bool(widgets.get("enabled_by_default", False))
    for key in ("progress_bar_width", "max_width", "max_lines"):
        if data["display"][key] < 1:
            warnings.append(f"display.{key} must be positive; using default")
    data["privacy"]["never_store_auth_tokens"] = True
    data["privacy"]["never_store_prompt_contents"] = True
    data["privacy"]["never_store_assistant_text"] = True
    data["ui"]["security"]["page_dom_denied"] = True
    data["ui"]["security"]["message_contents_denied"] = True
    data["ui"]["security"]["network_denied"] = True
    if data["storage"]["store_prompt_text"]:
        warnings.append("storage.store_prompt_text is disabled by privacy invariant")
        data["storage"]["store_prompt_text"] = False
    if data["storage"]["store_assistant_text"]:
        warnings.append("storage.store_assistant_text is disabled by privacy invariant")
        data["storage"]["store_assistant_text"] = False
    if data["storage"]["store_tool_inputs"] or data["storage"]["store_tool_outputs"]:
        warnings.append("Tool input/output persistence is disabled in v1")
        data["storage"]["store_tool_inputs"] = False
        data["storage"]["store_tool_outputs"] = False
    return warnings


def load_config(plugin_root: Path, plugin_data: Path, create: bool = True) -> LoadedConfig:
    default_path = plugin_root / "config.default.toml"
    config_path = plugin_data / "config.toml"
    plugin_data.mkdir(parents=True, exist_ok=True)
    if create and not config_path.exists():
        shutil.copyfile(default_path, config_path)
    with default_path.open("rb") as handle:
        defaults = tomllib.load(handle)
    override: dict[str, Any] = {}
    if config_path.exists():
        try:
            with config_path.open("rb") as handle:
                override = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot read {config_path}: {exc}") from exc
    override_ui = override.get("ui") if isinstance(override.get("ui"), dict) else {}
    original_has_focus = "focus_mode" in override_ui
    focus_override = override_ui.get("focus_mode") if isinstance(override_ui.get("focus_mode"), dict) else {}
    migrated_focus_defaults = (
        original_has_focus
        and focus_override.get("visible_turns") == 10
        and focus_override.get("load_batch") == 10
    )
    if migrated_focus_defaults:
        focus_override["visible_turns"] = 3
        focus_override["load_batch"] = 3
    legacy = override_ui.pop("chat_virtualization", None)
    used_legacy = not original_has_focus and isinstance(legacy, dict)
    if used_legacy:
        override_ui["focus_mode"] = {
            key: value for key, value in legacy.items()
            if key in {"enabled", "visible_turns", "load_batch", "reset_on_thread_switch", "unknown_version_policy"}
        }
        override_ui["focus_mode"]["scroll_guard"] = True
    data, warnings = _merge(defaults, override)
    if isinstance(legacy, dict):
        warnings.append("ui.chat_virtualization is deprecated; migrated to ui.focus_mode")
    _migrate_legacy_rate_labels(data)
    warnings.extend(_validate(data))
    if create and (not original_has_focus or isinstance(legacy, dict) or migrated_focus_defaults):
        focus = data["ui"]["focus_mode"]
        text = config_path.read_text(encoding="utf-8")
        if isinstance(legacy, dict):
            lines: list[str] = []
            skipping = False
            for line in text.splitlines(keepends=True):
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    skipping = stripped == "[ui.chat_virtualization]"
                    if skipping:
                        continue
                if not skipping:
                    lines.append(line)
            text = "".join(lines)
        if migrated_focus_defaults:
            lines = []
            in_focus = False
            for line in text.splitlines(keepends=True):
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_focus = stripped == "[ui.focus_mode]"
                if in_focus and stripped.startswith("visible_turns = 10"):
                    line = line.replace("visible_turns = 10", "visible_turns = 3")
                elif in_focus and stripped.startswith("load_batch = 10"):
                    line = line.replace("load_batch = 10", "load_batch = 3")
                lines.append(line)
            text = "".join(lines)
            warnings.append("Migrated ui.focus_mode defaults from 10 turns to 3 turns")
        if not original_has_focus:
            text = text.rstrip() + (
                "\n\n[ui.focus_mode]\n"
                f"enabled = {str(bool(focus['enabled'])).lower()}\n"
                f"visible_turns = {focus['visible_turns']}\n"
                f"load_batch = {focus['load_batch']}\n"
                f"reset_on_thread_switch = {str(bool(focus['reset_on_thread_switch'])).lower()}\n"
                f"scroll_guard = {str(bool(focus['scroll_guard'])).lower()}\n"
                f"unknown_version_policy = {focus['unknown_version_policy']!r}\n".replace("'", '"')
            )
            warnings.append("Added ui.focus_mode defaults to config.toml")
        try:
            config_path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            if migrated_focus_defaults:
                warnings.append(f"Could not persist ui.focus_mode migration: {exc}; using migrated values in memory")
            else:
                raise
    def expand(value: str) -> str:
        return os.path.expandvars(
            value.replace("${PLUGIN_DATA}", str(plugin_data)).replace("${PLUGIN_ROOT}", str(plugin_root))
        )

    database = expand(str(data["storage"]["database"]))
    log_file = expand(str(data["diagnostics"]["log_file"]))
    data["storage"]["database"] = os.path.expandvars(database)
    data["diagnostics"]["log_file"] = os.path.expandvars(log_file)
    data["ui"]["widgets"]["directories"] = [expand(str(value)) for value in data["ui"]["widgets"]["directories"]]
    return LoadedConfig(data=data, path=config_path, warnings=tuple(warnings))


def _migrate_legacy_rate_labels(data: dict[str, Any]) -> None:
    """Keep customized v1 templates but replace the old fixed window captions."""
    for profile in ("compact", "normal", "full", "adaptive"):
        section = data.get("format", {}).get(profile)
        if not isinstance(section, dict) or not isinstance(section.get("template"), str):
            continue
        value = section["template"]
        value = value.replace("5h {primary.", "{primary.label} {primary.")
        value = value.replace("5h   {primary.", "{primary.label} {primary.")
        value = value.replace("Week {secondary.", "{secondary.label} {secondary.")
        section["template"] = value
