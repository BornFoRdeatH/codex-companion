from __future__ import annotations

import copy
import os
import shutil
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
    if data["ui"]["refresh_interval_ms"] < 100:
        warnings.append("ui.refresh_interval_ms must be at least 100; using 200")
        data["ui"]["refresh_interval_ms"] = 200
    if data["ui"]["dock_size"] < 180:
        warnings.append("ui.dock_size must be at least 180; using 340")
        data["ui"]["dock_size"] = 340
    for key in ("progress_bar_width", "max_width", "max_lines"):
        if data["display"][key] < 1:
            warnings.append(f"display.{key} must be positive; using default")
    data["privacy"]["never_store_auth_tokens"] = True
    data["privacy"]["never_store_prompt_contents"] = True
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
    data, warnings = _merge(defaults, override)
    _migrate_legacy_rate_labels(data)
    warnings.extend(_validate(data))
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
