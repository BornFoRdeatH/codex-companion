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
    return {
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
