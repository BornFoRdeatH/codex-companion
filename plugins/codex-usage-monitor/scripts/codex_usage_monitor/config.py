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
    for key in ("progress_bar_width", "max_width", "max_lines"):
        if data["display"][key] < 1:
            warnings.append(f"display.{key} must be positive; using default")
    data["privacy"]["never_store_auth_tokens"] = True
    data["privacy"]["never_store_prompt_contents"] = True
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
    warnings.extend(_validate(data))
    database = str(data["storage"]["database"]).replace("${PLUGIN_DATA}", str(plugin_data))
    log_file = str(data["diagnostics"]["log_file"]).replace("${PLUGIN_DATA}", str(plugin_data))
    data["storage"]["database"] = os.path.expandvars(database)
    data["diagnostics"]["log_file"] = os.path.expandvars(log_file)
    return LoadedConfig(data=data, path=config_path, warnings=tuple(warnings))
