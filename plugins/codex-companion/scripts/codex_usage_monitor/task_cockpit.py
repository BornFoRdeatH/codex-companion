from __future__ import annotations

import time
from typing import Any


LEVEL_ORDER = {"info": 1, "warning": 2, "critical": 3}
ACTION_ORDER = {"review": 10, "checkpoint": 30, "handoff": 40, "new_task": 50}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _recommendation(code: str, level: str, action: str, confidence: str, source: str,
                    reason: str, impact: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    numeric = {key: value for key, value in (evidence or {}).items()
               if value is None or isinstance(value, (bool, int, float))}
    return {"code": code, "level": level, "action": action, "confidence": confidence,
            "source": source, "reason_code": reason, "impact_code": impact, "evidence": numeric}


def build(summary: dict[str, Any], view: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Build current-task-only health, recommendations, and an aggregate activity timeline."""
    now = time.time() if now is None else float(now)
    turn = view.get("turn") or {}
    tools = view.get("tools") or {}
    context = view.get("context") or {}
    optimizer = (view.get("budget") or {}).get("context_optimizer") or {}
    advisor = view.get("advisor") or {}
    compactions = view.get("compactions") or {}
    calls = _number(tools.get("total_calls")) or 0
    failures = _number(tools.get("failed_calls")) or 0
    failure_rate = failures / calls if calls else 0.0
    turn_total = _number(turn.get("total"))
    duration = _number(turn.get("duration"))
    context_used = _number(context.get("used_percent"))
    context_source = str(context.get("source") or "unavailable")
    budget_status = str(optimizer.get("status") or "unavailable")
    trusted_context = context_source in {"official", "observed_renderer"}
    recommendations: list[dict[str, Any]] = []

    if trusted_context and budget_status == "new_task_recommended":
        recommendations.append(_recommendation("context_exhaustion", "critical", "new_task", "high", context_source,
                                               "context_pressure", "new_task_safety", {"context_used_percent": context_used}))
    elif trusted_context and budget_status == "handoff_recommended":
        recommendations.append(_recommendation("context_handoff", "warning", "handoff", "high", context_source,
                                               "context_pressure", "preserve_continuity", {"context_used_percent": context_used}))
    elif trusted_context and budget_status == "checkpoint_recommended":
        recommendations.append(_recommendation("context_checkpoint", "warning", "checkpoint", "high", context_source,
                                               "context_pressure", "reduce_recovery_risk", {"context_used_percent": context_used}))

    if calls >= 3 and failure_rate >= 0.5:
        recommendations.append(_recommendation("tool_retry_loop", "warning", "review", "medium", "observed",
                                               "repeated_tool_failures", "narrow_next_action",
                                               {"tool_calls": calls, "failed_tool_calls": failures, "failure_rate": failure_rate}))
    if (_number(compactions.get("count")) or 0) >= 2:
        recommendations.append(_recommendation("compaction_pressure", "warning", "checkpoint", "medium", "observed",
                                               "repeated_compactions", "preserve_continuity", {"compactions": compactions.get("count")}))
    for item in advisor.get("all_items") or advisor.get("items") or []:
        code = str(item.get("code") or "");
        if code in {"split_request", "avoid_new_scope", "narrow_request", "reduce_exploration", "quota_conservation"}:
            action = "review" if code != "quota_conservation" else "review"
            recommendations.append(_recommendation(code, str(item.get("level") or "info"), action,
                                                   str(item.get("confidence") or "low"), str(item.get("source") or "observed"),
                                                   code, "reduce_risk", item.get("evidence")))

    if not turn:
        state = "unavailable"
    elif trusted_context and budget_status in {"new_task_recommended", "handoff_recommended", "checkpoint_recommended"}:
        state = "context_risk"
    elif not turn.get("ended_at"):
        state = "working"
    elif calls >= 3 and failure_rate >= 0.5:
        state = "blocked"
    else:
        state = "ready_for_review"

    recommendations.sort(key=lambda item: (LEVEL_ORDER.get(item["level"], 0), ACTION_ORDER.get(item["action"], 0)), reverse=True)
    primary = recommendations[0] if recommendations else _recommendation(
        "review_when_ready", "info", "review", "medium" if turn else "low", context_source,
        "continue_current_task", "maintain_progress", {"turn_tokens": turn_total})
    confidence = "high" if primary["confidence"] == "high" else "medium" if recommendations else "low"
    events: list[dict[str, Any]] = []
    if turn.get("started_at"):
        events.append({"type": "turn_started", "at": turn.get("started_at")})
    if turn.get("ended_at"):
        events.append({"type": "turn_completed", "at": turn.get("ended_at")})
    if compactions.get("last_time"):
        events.append({"type": "compaction", "at": compactions.get("last_time"), "count": compactions.get("count")})
    if primary["code"] != "review_when_ready":
        events.append({"type": "recommendation", "code": primary["code"]})
    events.sort(key=lambda item: _number(item.get("at")) or now)
    activity = {
        "turns": 1 if turn else 0,
        "tool_calls": int(calls),
        "failures": int(failures),
        "failure_rate": round(failure_rate, 3),
        "compactions": int(_number(compactions.get("count")) or 0),
        "edits": int(_number(tools.get("file_edits")) or 0),
        "last_event": events[-1]["type"] if events else "unavailable",
        "events": events[-20:],
    }
    return {
        "state": state,
        "confidence": confidence,
        "source": context_source,
        "risk_codes": [item["code"] for item in recommendations[:5] if item["level"] != "info"],
        "recommended_action": primary["action"],
        "primary_recommendation": primary,
        "recommendations": recommendations[:8],
        "activity": activity,
        "progress": {"turn_tokens": turn_total, "duration_seconds": duration,
                     "velocity_tokens_per_minute": round(turn_total / (duration / 60), 1) if turn_total and duration and duration > 0 else None},
        "last_updated_at": now,
        "advisory_only": True,
    }
