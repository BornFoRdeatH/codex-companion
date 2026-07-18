from __future__ import annotations

import math
from typing import Any

from .config import LoadedConfig


def evaluate(summary: dict[str, Any], config: LoadedConfig, prompt_features: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an advisory-only, privacy-safe turn and quota forecast."""
    if not config.get("ui.budget.enabled", True):
        return {"enabled": False, "level": "none"}
    baseline = summary.get("budget_baseline") or {}
    tokens = baseline.get("total_tokens") or {}
    quota = baseline.get("quota_delta") or {}
    count = int(tokens.get("count") or 0)
    median = _number(tokens.get("median"))
    mad = _number(tokens.get("mad")) or 0.0
    minimum = int(config.get("ui.budget.min_personal_turns", 10))
    hard = float(config.get("thresholds.expensive_turn_tokens", 100000))
    configured = float(config.get("ui.budget.per_turn_tokens", 0))
    adaptive = max(hard, (median + 3.0 * mad) if median is not None else hard)
    token_budget = configured if configured > 0 else adaptive
    expected = median if median is not None else hard * 0.35
    high = max(median + 3.0 * mad, median * 1.5) if median is not None else hard * 0.65
    features = _safe_features(prompt_features)
    multiplier = 1.0
    reasons: list[str] = []
    if features.get("char_count", 0) >= 4000:
        multiplier *= 1.2
        reasons.append("long_prompt")
    if features.get("multi_task"):
        multiplier *= 1.25
        reasons.append("multi_task")
    expected *= multiplier
    high *= multiplier
    ratio = (high / token_budget * 100.0) if token_budget > 0 and high is not None else None
    warning = float(config.get("ui.budget.warn_at_percent", 80))
    critical = float(config.get("ui.budget.critical_at_percent", 100))
    level = "critical" if ratio is not None and ratio >= critical else "warning" if ratio is not None and ratio >= warning else "info"

    view = summary.get("view") or {}
    primary = view.get("primary") or {}
    remaining = _number(primary.get("remaining_percent"))
    quota_median = _number(quota.get("median"))
    projected_remaining = remaining - quota_median if remaining is not None and quota_median is not None else None
    reserve = float(config.get("ui.budget.weekly_remaining_reserve_percent", 10))
    reserve_risk = projected_remaining is not None and projected_remaining < reserve
    if reserve_risk:
        level = "critical" if projected_remaining < 0 else "warning"
        reasons.append("weekly_reserve")
    confidence = "high" if count >= minimum else "medium" if count >= 3 else "low"
    result = {
        "enabled": True,
        "advisory_only": True,
        "level": level,
        "expected_tokens": round(expected) if expected is not None else None,
        "high_tokens": round(high) if high is not None else None,
        "token_budget": round(token_budget),
        "budget_used_percent": ratio,
        "baseline_samples": count,
        "confidence": confidence,
        "projected_weekly_remaining_percent": projected_remaining,
        "weekly_reserve_percent": reserve,
        "reserve_risk": reserve_risk,
        "reasons": reasons,
        "prompt_features": features,
        "source": "estimated",
    }
    result["context_optimizer"] = context_optimizer(summary, config, features)
    return result


def context_optimizer(summary: dict[str, Any], config: LoadedConfig,
                      prompt_features: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a privacy-safe, advisory-only context-window forecast."""
    enabled = bool(config.get("ui.budget.optimizer_enabled", True))
    if not enabled:
        return {"enabled": False, "status": "unavailable", "level": "info", "advisory_only": True}

    view = summary.get("view") or {}
    context = view.get("context") or {}
    source = str(context.get("source") or "unavailable")
    used_percent = _number(context.get("used_percent"))
    window = _number(context.get("window"))
    trusted = source in {"official", "observed_renderer"}
    if used_percent is None or window is None or window <= 0:
        return {
            "enabled": True, "advisory_only": True, "status": "unavailable", "level": "info",
            "source": source, "confidence": "low", "safe_turns_remaining": None,
            "next_turn_percent": None, "predicted_delta_tokens": None, "reasons": ["context_unavailable"],
            "recommended_action": "none", "compactions": view.get("compactions") or {},
        }

    baseline = summary.get("budget_baseline") or {}
    stats = baseline.get("context_delta") or {}
    samples = int(stats.get("count") or 0)
    median_delta = _number(stats.get("median"))
    mad = _number(stats.get("mad")) or 0.0
    minimum = max(1, int(config.get("ui.budget.minimum_context_samples", 3)))
    features = _safe_features(prompt_features)
    multiplier = 1.0
    reasons: list[str] = []
    if features.get("char_count", 0) >= 4000:
        multiplier *= 1.2
        reasons.append("long_prompt")
    if features.get("multi_task"):
        multiplier *= 1.25
        reasons.append("multi_task")
    delta = (median_delta + mad) if median_delta is not None and median_delta > 0 else _number(
        (summary.get("view") or {}).get("turn", {}).get("input")
    )
    if delta is None or delta <= 0:
        delta = max(1.0, _number((summary.get("budget_baseline") or {}).get("total_tokens", {}).get("median")) or 0.0)
    delta *= multiplier
    next_percent = min(100.0, used_percent + (delta / window * 100.0)) if delta else used_percent
    warning = float(config.get("ui.budget.context_warning_percent", 70))
    checkpoint = float(config.get("ui.budget.context_checkpoint_percent", 80))
    handoff = float(config.get("ui.budget.context_handoff_percent", 88))
    new_task = float(config.get("ui.budget.context_new_task_percent", 93))
    reserve = float(config.get("ui.budget.context_safety_reserve_percent", 5))
    danger = max(warning, new_task - reserve)
    delta_percent = delta / window * 100.0 if delta else None
    safe_turns = None if not delta_percent else max(0, int(math.floor((danger - used_percent) / delta_percent)))
    if source == "estimated":
        reasons.append("estimated_context")
    if samples < minimum:
        reasons.append("limited_baseline")
    compactions = view.get("compactions") or {}
    compaction_count = int(compactions.get("count") or 0)
    if compaction_count >= 1:
        reasons.append("compaction_seen")
    if compaction_count >= 2:
        reasons.append("repeated_compactions")
    projected = max(next_percent, used_percent)
    if trusted and (used_percent >= new_task or projected >= new_task):
        status, action = "new_task_recommended", "new_task"
    elif trusted and (used_percent >= handoff or projected >= handoff):
        status, action = "handoff_recommended", "handoff"
    elif trusted and (used_percent >= checkpoint or projected >= checkpoint):
        status, action = "checkpoint_recommended", "checkpoint"
    elif used_percent >= warning or projected >= warning:
        status, action = "watch", "monitor"
    else:
        status, action = "healthy", "none"
    if not trusted:
        level = "info"
    elif status in {"new_task_recommended", "handoff_recommended"}:
        level = "critical" if status == "new_task_recommended" else "warning"
    elif status == "checkpoint_recommended" or status == "watch":
        level = "warning"
    else:
        level = "info"
    confidence = "high" if trusted and samples >= minimum else "medium" if trusted and samples >= 3 else "low"
    return {
        "enabled": True, "advisory_only": True, "status": status, "level": level,
        "source": source, "confidence": confidence, "context_used_percent": round(used_percent, 2),
        "context_window": round(window), "predicted_delta_tokens": round(delta),
        "next_turn_percent": round(next_percent, 2), "safe_turns_remaining": safe_turns,
        "baseline_samples": samples, "recommended_action": action, "reasons": reasons,
        "compactions": {"count": compaction_count, "last_time": compactions.get("last_time"),
                        "impact": "unavailable" if compaction_count else None},
    }


def transient_features(value: dict[str, Any] | None) -> dict[str, Any]:
    """Validate renderer-derived numeric structure without accepting prompt text."""
    return _safe_features(value)


def _safe_features(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in ("char_count", "line_count", "section_count", "task_count"):
        raw = source.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
            result[key] = max(0, min(int(raw), 1_000_000))
    result["multi_task"] = bool(source.get("multi_task"))
    return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
