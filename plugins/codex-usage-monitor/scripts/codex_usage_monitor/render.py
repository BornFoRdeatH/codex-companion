from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import LoadedConfig


def compact_number(value: Any, decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(number) >= divisor:
            rendered = f"{number / divisor:.{decimals}f}".rstrip("0").rstrip(".")
            return f"{rendered}{suffix}"
    return str(int(number)) if number.is_integer() else f"{number:.{decimals}f}"


def duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "N/A"
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


def progress(percent: float | None, width: int, unicode: bool = True, remaining: bool = False) -> str:
    if percent is None:
        return "?" * width
    value = 100.0 - percent if remaining else percent
    filled = round(max(0.0, min(100.0, value)) * width / 100.0)
    return ("█" * filled + "░" * (width - filled)) if unicode else ("#" * filled + "-" * (width - filled))


def local_time(timestamp: float | None, timezone_name: str, fmt: str) -> str:
    if not timestamp:
        return "N/A"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = None
    return datetime.fromtimestamp(float(timestamp), tz=zone).strftime(fmt)


def _selected_rates(summary: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    rates = summary.get("rates") or {}
    primary = rates.get("codex:primary")
    secondary = rates.get("codex:secondary")
    if not primary:
        primary = next((v for k, v in rates.items() if k.endswith(":primary")), None)
    if not secondary:
        secondary = next((v for k, v in rates.items() if k.endswith(":secondary")), None)
    return primary, secondary


def derive(summary: dict[str, Any], config: LoadedConfig) -> dict[str, Any]:
    token = summary.get("token") or {}
    turn = summary.get("turn") or {}
    tools = summary.get("tools") or {}
    account = summary.get("account") or {}
    primary, secondary = _selected_rates(summary)
    has_token = bool(token)
    has_turn = bool(turn)
    latest_total = int(token.get("total_tokens") or 0)
    baseline_total = int(turn.get("baseline_total") or 0)
    latest_input = int(token.get("input_tokens") or 0)
    latest_cached = int(token.get("cached_input_tokens") or 0)
    latest_output = int(token.get("output_tokens") or 0)
    latest_reasoning = int(token.get("reasoning_output_tokens") or 0)
    last_input = int(token.get("last_input_tokens") or 0)
    context_window = token.get("model_context_window")
    context_percent = (last_input / context_window * 100.0) if context_window else None
    timezone_name = config.get("locale.timezone", "UTC")
    date_fmt = config.get("locale.date_format", "%d.%m.%Y %H:%M:%S")
    now = time.time()
    turn_started = turn.get("started_at")
    turn_duration = (turn.get("ended_at") or now) - turn_started if turn_started else None
    turn_total = max(0, latest_total - baseline_total) if turn else 0
    primary_delta = _delta(primary, turn.get("baseline_primary_percent"))
    secondary_delta = _delta(secondary, turn.get("baseline_secondary_percent"))
    burn = _burn_rate(summary, primary)
    exhaustion = ((100.0 - float(primary["used_percent"])) / burn) if primary and burn and burn > 0 else None
    average_turn = _average_turn(summary)
    remaining_turns = ((100.0 - float(primary["used_percent"])) / primary_delta) if primary and primary_delta and primary_delta > 0 else None
    daily = _daily_buckets(account)
    today = _bucket_sum(daily, 1)
    seven = _bucket_sum(daily, 7)
    thirty = _bucket_sum(daily, 30)
    result = {
        "model": config.get("_runtime.model", ""),
        "primary": _rate_view(primary, now, timezone_name, date_fmt, config),
        "secondary": _rate_view(secondary, now, timezone_name, date_fmt, config),
        "thread": {
            "total": latest_total if has_token else None,
            "input": latest_input if has_token else None,
            "cached": latest_cached if has_token else None,
            "uncached": max(0, latest_input - latest_cached) if has_token else None,
            "output": latest_output if has_token else None,
            "reasoning": latest_reasoning if has_token else None,
            "cache_hit": (latest_cached / latest_input * 100.0) if latest_input else None,
            "source": token.get("source"),
        },
        "turn": {
            "id": turn.get("turn_id"),
            "duration": turn_duration,
            "total": turn_total if has_turn and has_token else None,
            "input": max(0, latest_input - int(turn.get("baseline_input") or 0)) if has_turn and has_token else None,
            "cached": max(0, latest_cached - int(turn.get("baseline_cached") or 0)) if has_turn and has_token else None,
            "output": max(0, latest_output - int(turn.get("baseline_output") or 0)) if has_turn and has_token else None,
            "reasoning": max(0, latest_reasoning - int(turn.get("baseline_reasoning") or 0)) if has_turn and has_token else None,
            "model_calls": _model_calls(summary, turn) if has_turn and has_token else None,
            "quota_primary_delta": primary_delta,
            "quota_secondary_delta": secondary_delta,
        },
        "context": {
            "window": context_window,
            "used": last_input,
            "used_percent": context_percent,
            "remaining": max(0, context_window - last_input) if context_window else None,
            "remaining_percent": 100.0 - context_percent if context_percent is not None else None,
            "source": "estimated",
        },
        "tools": tools,
        "compactions": summary.get("compactions") or {},
        "subagents": summary.get("subagents") or {},
        "account": {
            "lifetime": account.get("lifetime_tokens"),
            "today": today,
            "seven_days": seven,
            "thirty_days": thirty,
            "daily_average": (seven / min(7, len(daily))) if daily else None,
            "peak": account.get("peak_daily_tokens"),
            "longest_turn": account.get("longest_running_turn_sec"),
            "current_streak": account.get("current_streak_days"),
            "longest_streak": account.get("longest_streak_days"),
            "source": account.get("source"),
        },
        "forecast": {
            "burn_per_hour": burn,
            "exhaustion_hours": exhaustion,
            "remaining_turns": remaining_turns,
            "average_tokens_per_turn": average_turn,
            "confidence": "low" if exhaustion is not None else None,
            "rolling": summary.get("rolling_forecast") or {"windows": {}, "remaining_turns": None},
        },
    }
    rolling = result["forecast"]["rolling"]
    preferred = (rolling.get("windows") or {}).get("60") or (rolling.get("windows") or {}).get("15")
    if preferred:
        result["forecast"].update({"burn_per_hour": preferred.get("burn_percent_per_hour"),
                                   "exhaustion_hours": preferred.get("projected_exhaustion_hours"),
                                   "confidence": preferred.get("confidence")})
    if rolling.get("remaining_turns") is not None:
        result["forecast"]["remaining_turns"] = rolling["remaining_turns"]
    result["guard"] = _guard(result, config)
    return result


def _guard(view: dict[str, Any], config: LoadedConfig) -> dict[str, Any]:
    if not config.get("ui.guard.enabled", True):
        return {"alerts": [], "highest": None}
    alerts: list[dict[str, Any]] = []
    thresholds = config.get("thresholds", {})
    def add(condition: str, used: float | None, warning: float, critical: float | None,
            provenance: str, global_value: bool = False) -> None:
        if used is None:
            return
        level = "critical" if critical is not None and used >= critical else "warning" if used >= warning else None
        if level:
            alerts.append({"condition": condition, "level": level, "value": used, "provenance": provenance,
                           "estimated": provenance == "estimated", "global": global_value})
    primary = view.get("primary") or {}
    add("quota", primary.get("used_percent"), float(thresholds.get("quota_warning_percent", 70)),
        float(thresholds.get("quota_critical_percent", 90)), str(primary.get("source") or "official"), True)
    context = view.get("context") or {}
    source = str(context.get("source") or "unavailable")
    allow_estimated = bool(config.get("ui.guard.allow_estimated_alerts", False))
    if source != "estimated" or allow_estimated:
        critical = float(thresholds.get("context_critical_percent", 90)) if source in {"official", "observed_renderer"} else None
        add("context", context.get("used_percent"), float(thresholds.get("context_warning_percent", 70)), critical, source)
    turn = view.get("turn") or {}
    if turn.get("duration") is not None and turn["duration"] >= float(thresholds.get("slow_turn_seconds", 120)):
        alerts.append({"condition": "slow_turn", "level": "warning", "value": turn["duration"], "provenance": "observed", "estimated": False, "global": False})
    if turn.get("total") is not None and turn["total"] >= float(thresholds.get("expensive_turn_tokens", 100000)):
        alerts.append({"condition": "expensive_turn", "level": "warning", "value": turn["total"], "provenance": "observed", "estimated": False, "global": False})
    turn_input, turn_cached = turn.get("input"), turn.get("cached")
    hit = (100.0 * float(turn_cached) / float(turn_input)) if turn_input else None
    if hit is not None and hit < float(thresholds.get("low_cache_hit_percent", 50)):
        alerts.append({"condition": "low_cache_hit", "level": "info", "value": hit, "provenance": "observed", "estimated": False, "global": False})
    order = {"info": 1, "warning": 2, "critical": 3}
    highest = max((alert["level"] for alert in alerts), key=lambda value: order[value], default=None)
    return {"alerts": alerts, "highest": highest}


def render(summary: dict[str, Any], config: LoadedConfig, profile: str) -> str:
    data = derive(summary, config)
    if profile == "adaptive":
        profile = "full" if data["turn"]["duration"] and data["turn"]["duration"] >= 120 else "normal"
    template = config.get(f"format.{profile}.template")
    if isinstance(template, str) and template.strip():
        return render_template(template, data, config).strip()
    if profile == "compact":
        return _compact(data, config)
    if profile == "normal":
        return _normal(data, config)
    return _full(data, config)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_template(template: str, data: dict[str, Any], config: LoadedConfig) -> str:
    width = int(config.get("display.progress_bar_width", 12))
    unicode = bool(config.get("display.unicode_progress_bars", True))
    primary = data.get("primary") or {}
    secondary = data.get("secondary") or {}
    context = data.get("context") or {}
    turn = data.get("turn") or {}
    thread = data.get("thread") or {}
    values = {
        "primary.label": _rate_label(primary) if primary else "Primary",
        "primary.used_bar": progress(primary.get("used_percent"), width, unicode),
        "primary.remaining_bar": progress(primary.get("used_percent"), width, unicode, remaining=True),
        "primary.used_percent": _fmt(primary.get("used_percent")),
        "primary.remaining_percent": _fmt(100 - primary["used_percent"] if primary.get("used_percent") is not None else None),
        "primary.reset_local": primary.get("reset_local", "N/A"),
        "primary.reset_in": primary.get("reset_in", "N/A"),
        "secondary.used_bar": progress(secondary.get("used_percent"), width, unicode),
        "secondary.remaining_bar": progress(secondary.get("used_percent"), width, unicode, remaining=True),
        "secondary.used_percent": _fmt(secondary.get("used_percent")),
        "secondary.remaining_percent": _fmt(100 - secondary["used_percent"] if secondary.get("used_percent") is not None else None),
        "secondary.reset_local": secondary.get("reset_local", "N/A"),
        "secondary.reset_in": secondary.get("reset_in", "N/A"),
        "secondary.label": _rate_label(secondary) if secondary else "Secondary",
        "turn.total_tokens": compact_number(turn.get("total")),
        "turn.input_tokens": compact_number(turn.get("input")),
        "turn.cached_input_tokens": compact_number(turn.get("cached")),
        "turn.output_tokens": compact_number(turn.get("output")),
        "thread.total_tokens": compact_number(thread.get("total")),
        "context.used_percent": _fmt(context.get("used_percent"), estimated=True),
        "context.remaining_percent": _fmt(context.get("remaining_percent"), estimated=True),
    }
    return re.sub(r"\{([a-zA-Z0-9_.]+)\}", lambda match: str(values.get(match.group(1), "N/A")), template)


def _compact(d: dict[str, Any], c: LoadedConfig) -> str:
    p, s, ctx = d["primary"], d["secondary"], d["context"]
    parts = ["Usage Δ"]
    if c.get("fields.turn.enabled", True) and c.get("fields.turn.total_tokens", True):
        parts.append(f"+{compact_number(d['turn']['total'])} tok")
    if c.get("fields.rate_limits.enabled", True) and p:
        parts.append(f"{_rate_label(p)} {p['used_percent']:.1f}% used")
    if c.get("fields.rate_limits.enabled", True) and s:
        parts.append(f"{_rate_label(s)} {s['used_percent']:.1f}% used")
    if c.get("fields.thread.context_used_percent", True) and ctx["used_percent"] is not None:
        parts.append(f"Ctx ≈{ctx['used_percent']:.1f}%")
    return " │ ".join(parts)


def _normal(d: dict[str, Any], c: LoadedConfig) -> str:
    width = int(c.get("display.progress_bar_width", 12))
    unicode = bool(c.get("display.unicode_progress_bars", True))
    lines = ["Codex usage"]
    for rate in (d["primary"], d["secondary"]) if c.get("fields.rate_limits.enabled", True) else ():
        if rate:
            lines.append(
                f"{_rate_label(rate):7} {progress(rate['used_percent'], width, unicode)} "
                f"{rate['used_percent']:.1f}% used · reset {rate['reset_local']} ({rate['reset_in']})"
            )
    turn = d["turn"]
    if c.get("fields.turn.enabled", True):
        fields = []
        if c.get("fields.turn.total_tokens", True):
            fields.append(f"Turn {compact_number(turn['total'])}")
        if c.get("fields.turn.input_tokens", True):
            fields.append(f"in {compact_number(turn['input'])}")
        if c.get("fields.turn.cached_input_tokens", True):
            fields.append(f"cached {compact_number(turn['cached'])}")
        if c.get("fields.turn.output_tokens", True):
            fields.append(f"out {compact_number(turn['output'])}")
        if fields:
            lines.append(" · ".join(fields))
    ctx = d["context"]
    if c.get("fields.thread.enabled", True) and ctx["used_percent"] is not None:
        lines.append(f"Thread {compact_number(d['thread']['total'])} · Context ≈{ctx['used_percent']:.1f}% used")
    return "\n".join(lines)


def _full(d: dict[str, Any], c: LoadedConfig) -> str:
    width = int(c.get("display.progress_bar_width", 12))
    unicode = bool(c.get("display.unicode_progress_bars", True))
    lines = ["╭─ CODEX USAGE " + "─" * 35]
    model = d.get("model") or "unknown"
    lines.append(f"│ Model: {model} · Turn: {duration(d['turn']['duration'])}")
    for rate in (d["primary"], d["secondary"]) if c.get("fields.rate_limits.enabled", True) else ():
        if rate:
            lines.extend(
                [
                    "│",
                    f"│ {_rate_label(rate):8} {progress(rate['used_percent'], width, unicode)} "
                    f"{rate['used_percent']:.1f}% used · {100-rate['used_percent']:.1f}% left",
                    f"│          Reset: {rate['reset_local']} · in {rate['reset_in']}",
                ]
            )
    t = d["turn"]
    if c.get("fields.turn.enabled", True):
        lines.extend(["│", "│ TURN"])
        first = []
        second = []
        for key, label in (("total_tokens", "Total"), ("input_tokens", "Input"), ("cached_input_tokens", "Cached")):
            if c.get(f"fields.turn.{key}", True):
                first.append(f"{label} {compact_number(t[{'total_tokens':'total','input_tokens':'input','cached_input_tokens':'cached'}[key]])}")
        if c.get("fields.turn.output_tokens", True):
            second.append(f"Output {compact_number(t['output'])}")
        if c.get("fields.turn.reasoning_output_tokens", True):
            second.append(f"Reasoning {compact_number(t['reasoning'])}")
        if c.get("fields.turn.model_calls", True):
            second.append(f"Model calls {t['model_calls']}")
        if first:
            lines.append("│ " + " · ".join(first))
        if second:
            lines.append("│ " + " · ".join(second))
        if c.get("fields.turn.quota_delta_primary", True) and t["quota_primary_delta"] is not None:
            lines.append(f"│ Quota Δ {t['quota_primary_delta']:+.1f}%")
    ctx = d["context"]
    if c.get("fields.thread.enabled", True):
        lines.extend(["│", "│ THREAD"])
    if c.get("fields.thread.enabled", True) and ctx["used_percent"] is not None:
        lines.append(
            f"│ Total {compact_number(d['thread']['total'])} · Context ≈"
            f"{progress(ctx['used_percent'], width, unicode)} {ctx['used_percent']:.1f}% used"
        )
        lines.append(
            f"│ Remaining ≈{compact_number(ctx['remaining'])} / {compact_number(ctx['window'])} "
            f"· Compactions {d['compactions'].get('count', 0)}"
        )
    elif c.get("fields.thread.enabled", True):
        lines.append(f"│ Total {compact_number(d['thread']['total'])} · Context unavailable")
    tools = d["tools"]
    if c.get("fields.tools.enabled", True):
        lines.extend(
        [
            "│",
            "│ OPERATIONS",
            f"│ Tools {tools.get('total_calls') or 0} · Bash {tools.get('bash_calls') or 0} · "
            f"Edits {tools.get('file_edits') or 0} · MCP {tools.get('mcp_calls') or 0} · Failed {tools.get('failed_calls') or 0}",
            f"│ Tool time {duration(tools.get('tool_seconds'))} · Subagents "
            f"{d['subagents'].get('completed') or 0}/{d['subagents'].get('started') or 0}",
        ]
        )
    account = d["account"]
    if c.get("fields.account_usage.enabled", True) and any(account.get(key) is not None for key in ("today", "seven_days", "lifetime")):
        lines.extend(
            [
                "│",
                "│ ACCOUNT",
                f"│ Today {compact_number(account['today'])} · 7 days {compact_number(account['seven_days'])} "
                f"· Lifetime {compact_number(account['lifetime'])}",
                f"│ Daily peak {compact_number(account['peak'])} · Active streak {account['current_streak'] or 'N/A'} days",
            ]
        )
    forecast = d["forecast"]
    if c.get("fields.predictions.enabled", True) and forecast["burn_per_hour"] is not None and forecast["burn_per_hour"] > 0:
        lines.extend(
            [
                "│",
                "│ FORECAST",
                f"│ ≈{forecast['burn_per_hour']:.2f}%/h · exhaustion in "
                f"{duration(forecast['exhaustion_hours'] * 3600 if forecast['exhaustion_hours'] else None)} "
                f"· confidence {forecast['confidence']}",
            ]
        )
    source = d["thread"].get("source")
    if source and "experimental" in source:
        lines.append("│ experimental: thread/context data parsed from session JSONL")
    lines.append("╰" + "─" * 50)
    max_lines = int(c.get("display.max_lines", 40))
    return "\n".join(lines[:max_lines])


def _rate_view(rate: dict[str, Any] | None, now: float, zone: str, fmt: str, config: LoadedConfig) -> dict[str, Any] | None:
    if not rate or rate.get("used_percent") is None:
        return None
    reset = rate.get("resets_at")
    return {
        **rate,
        "used_percent": float(rate["used_percent"]),
        "reset_local": local_time(reset, zone, fmt),
        "reset_in": duration(float(reset) - now) if reset else "N/A",
    }


def _rate_label(rate: dict[str, Any]) -> str:
    minutes = rate.get("window_minutes")
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "Week"
    if minutes:
        return duration(float(minutes) * 60)
    return rate.get("limit_name") or rate.get("limit_id") or "Limit"


def _delta(rate: dict[str, Any] | None, baseline: Any) -> float | None:
    if not rate or rate.get("used_percent") is None or baseline is None:
        return None
    delta = float(rate["used_percent"]) - float(baseline)
    # A negative delta means the quota window reset during the turn. Without
    # the pre-reset remainder, attributing a percentage to this request would
    # be misleading.
    return delta if delta >= 0 else None


def _daily_buckets(account: dict[str, Any]) -> list[dict[str, Any]]:
    raw = account.get("daily_buckets_json")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def _bucket_sum(buckets: list[dict[str, Any]], days: int) -> int | None:
    if not buckets:
        return None
    return sum(int(row.get("tokens") or 0) for row in buckets[-days:])


def _burn_rate(summary: dict[str, Any], primary: dict[str, Any] | None) -> float | None:
    # Conservative v1 estimate: use the active turn's quota delta over elapsed turn time.
    turn = summary.get("turn") or {}
    if not primary or turn.get("baseline_primary_percent") is None or not turn.get("started_at"):
        return None
    elapsed_hours = max((time.time() - float(turn["started_at"])) / 3600.0, 1 / 60)
    delta = float(primary["used_percent"]) - float(turn["baseline_primary_percent"])
    return max(0.0, delta / elapsed_hours)


def _average_turn(summary: dict[str, Any]) -> float | None:
    turn = summary.get("turn") or {}
    token = summary.get("token") or {}
    if not turn or not token:
        return None
    return max(0, int(token.get("total_tokens") or 0) - int(turn.get("baseline_total") or 0))


def _model_calls(summary: dict[str, Any], turn: dict[str, Any]) -> int:
    # Filled by token snapshots in future schema revisions; one is the honest minimum when tokens moved.
    token = summary.get("token") or {}
    return 1 if int(token.get("total_tokens") or 0) > int(turn.get("baseline_total") or 0) else 0


def _fmt(value: Any, estimated: bool = False) -> str:
    if value is None:
        return "N/A"
    prefix = "≈" if estimated else ""
    return f"{prefix}{float(value):.1f}"
