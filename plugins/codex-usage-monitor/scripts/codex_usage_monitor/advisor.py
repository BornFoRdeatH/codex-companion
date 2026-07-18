from __future__ import annotations

import math
import re
import statistics
from typing import Any, Iterable

from .config import LoadedConfig


LEVEL_ORDER = {"info": 1, "warning": 2, "critical": 3}
CODE_ORDER = {
    "quota_conservation": 100,
    "start_new_chat": 95,
    "reduce_exploration": 80,
    "narrow_request": 75,
    "avoid_new_scope": 70,
    "consider_lower_effort": 60,
    "split_request": 55,
    "add_target": 45,
    "add_constraints": 40,
    "add_acceptance": 35,
}


def analyze_prompt(prompt: str) -> dict[str, Any]:
    """Return privacy-safe structural features. The input is never retained."""
    text = prompt if isinstance(prompt, str) else ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = sum(bool(re.match(r"^(?:[-*+] |\d+[.)]\s+|#{1,6}\s+)", line)) for line in lines)
    sections = sum(bool(re.match(r"^(?:#{1,6}\s+|[A-ZА-ЯІЇЄҐ][^.!?]{1,40}:$)", line)) for line in lines)
    lowered = text.casefold()
    uk = bool(re.search(r"[іїєґ]", lowered))
    en = bool(re.search(r"\b(?:the|please|with|should|must|when|file|test)\b", lowered))
    target = bool(re.search(
        r"(?:[A-Za-z]:\\|[/\\][\w.-]+|\b\w+\.(?:py|js|ts|tsx|jsx|md|toml|json|yaml|yml|rs|go)\b|"
        r"\b(?:файл|модуль|клас|функц|компонент|repo|repository|file|module|class|function|component)\w*)",
        text, re.IGNORECASE,
    ))
    constraints = bool(re.search(
        r"\b(?:не\s+|без\s+|лише|тільки|обмеж|повинен|має|must|without|only|avoid|limit|do not|don't)\w*",
        lowered,
    ))
    acceptance = bool(re.search(
        r"\b(?:тест|перевір|готово|критері|очікую|результат|test|verify|acceptance|done|expect|result)\w*",
        lowered,
    ))
    action = bool(re.search(r"\b(?:зроб|дод|виправ|реаліз|створ|онов|implement|add|fix|create|update|change|build)\w*", lowered))
    clauses = len(re.findall(r"(?:^|[;.!?]\s+|\n)\s*(?:і\s+)?[\wА-Яа-яІіЇїЄєҐґ]", text))
    language = "uk" if uk else "en" if en else "other"
    codes: list[str] = []
    if bullets >= 3 or clauses >= 4:
        codes.append("split_request")
    if language != "other" and action and not target:
        codes.append("add_target")
    if language != "other" and action and len(text) >= 80 and not constraints:
        codes.append("add_constraints")
    if language != "other" and action and len(text) >= 80 and not acceptance:
        codes.append("add_acceptance")
    return {
        "char_count": len(text),
        "line_count": len(lines),
        "section_count": sections,
        "bullet_count": bullets,
        "clause_count": clauses,
        "language": language,
        "recommendation_codes": codes,
    }


def robust_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return {"count": 0, "median": None, "mad": None}
    median = statistics.median(clean)
    return {"count": len(clean), "median": median, "mad": statistics.median(abs(v - median) for v in clean)}


def evaluate(summary: dict[str, Any], view: dict[str, Any], config: LoadedConfig) -> dict[str, Any]:
    if not config.get("ui.advisor.enabled", True):
        return {"items": [], "highest": None, "baseline": summary.get("advisor_baseline") or {}}
    baseline = summary.get("advisor_baseline") or {}
    prompt = summary.get("prompt_features") or {}
    turn = view.get("turn") or {}
    tools = view.get("tools") or {}
    context = view.get("context") or {}
    primary = view.get("primary") or {}
    compactions = view.get("compactions") or {}
    items: list[dict[str, Any]] = []

    def add(code: str, level: str, confidence: str, source: str, evidence: dict[str, Any], global_value: bool = False) -> None:
        numeric = {key: value for key, value in evidence.items() if value is None or isinstance(value, (bool, int, float))}
        items.append({"code": code, "level": level, "confidence": confidence, "source": source,
                      "evidence": numeric, "global": global_value, "estimated": source == "estimated"})

    context_used = _number(context.get("used_percent"))
    context_source = str(context.get("source") or "unavailable")
    trusted_context = context_source in {"official", "observed_renderer"}
    if context_used is not None:
        if trusted_context and context_used >= 85:
            add("start_new_chat", "critical" if context_used >= 92 else "warning", "high", context_source,
                {"context_used_percent": context_used})
        elif context_used >= 70:
            add("avoid_new_scope", "info" if context_source == "estimated" else "warning",
                "low" if context_source == "estimated" else "high", context_source,
                {"context_used_percent": context_used})
    compaction_count = int(compactions.get("count") or 0)
    if compaction_count >= 2 and not any(item["code"] == "avoid_new_scope" for item in items):
        add("avoid_new_scope", "warning", "medium", "observed", {"compactions": compaction_count})

    min_turns = int(config.get("ui.advisor.min_personal_turns", 10))
    total = _number(turn.get("total"))
    quota = _number(turn.get("quota_primary_delta"))
    total_stats = baseline.get("total_tokens") or {}
    quota_stats = baseline.get("quota_delta") or {}
    hard_tokens = float(config.get("thresholds.expensive_turn_tokens", 100000))
    expensive = total is not None and total >= hard_tokens
    evidence = {"turn_tokens": total, "fixed_threshold": hard_tokens}
    confidence = "medium"
    if int(total_stats.get("count") or 0) >= min_turns and total_stats.get("median") is not None:
        threshold = _personal_threshold(total_stats)
        expensive = total is not None and (total >= hard_tokens or total > threshold)
        evidence.update({"baseline_median": total_stats.get("median"), "baseline_mad": total_stats.get("mad"),
                         "personal_threshold": threshold, "baseline_samples": total_stats.get("count")})
        confidence = "high"
    quota_expensive = False
    if quota is not None and int(quota_stats.get("count") or 0) >= min_turns and quota_stats.get("median"):
        quota_threshold = _personal_threshold(quota_stats)
        quota_expensive = quota > quota_threshold
        evidence.update({"turn_quota_delta": quota, "quota_baseline_median": quota_stats.get("median"),
                         "quota_personal_threshold": quota_threshold})
    if expensive or quota_expensive:
        add("narrow_request", "warning", confidence, "observed", evidence)

    calls = _number(tools.get("total_calls"))
    failed = _number(tools.get("failed_calls"))
    tool_seconds = _number(tools.get("tool_seconds"))
    failed_ratio = failed / calls if calls and failed is not None else None
    call_stats, time_stats = baseline.get("tool_calls") or {}, baseline.get("tool_seconds") or {}
    exploration = bool(calls and calls >= 5 and failed_ratio is not None and failed_ratio >= .20)
    explore_evidence = {"tool_calls": calls, "failed_tool_calls": failed, "failed_tool_ratio": failed_ratio,
                        "tool_seconds": tool_seconds}
    personalized = False
    if int(call_stats.get("count") or 0) >= min_turns and call_stats.get("median") is not None:
        call_threshold = _personal_threshold(call_stats)
        time_threshold = _personal_threshold(time_stats) if time_stats.get("median") is not None else None
        exploration = exploration or (calls is not None and calls > call_threshold) or (
            tool_seconds is not None and time_threshold is not None and tool_seconds > time_threshold)
        explore_evidence.update({"tool_calls_baseline_median": call_stats.get("median"),
                                 "tool_calls_threshold": call_threshold, "tool_seconds_threshold": time_threshold})
        personalized = True
    if exploration:
        add("reduce_exploration", "warning", "high" if personalized else "medium", "observed", explore_evidence)

    reasoning = _number(turn.get("reasoning"))
    reasoning_stats = baseline.get("reasoning_tokens") or {}
    if (reasoning is not None and int(reasoning_stats.get("count") or 0) >= min_turns
            and reasoning_stats.get("median") is not None and reasoning > _personal_threshold(reasoning_stats)
            and (calls or 0) <= max(2, float((call_stats or {}).get("median") or 0))):
        add("consider_lower_effort", "info", "medium", "observed",
            {"reasoning_tokens": reasoning, "baseline_median": reasoning_stats.get("median"),
             "tool_calls": calls})

    quota_used = _number(primary.get("used_percent"))
    quota_critical = float(config.get("thresholds.quota_critical_percent", 90))
    if quota_used is not None and quota_used >= quota_critical:
        add("quota_conservation", "critical", "high", str(primary.get("source") or "official"),
            {"quota_used_percent": quota_used, "critical_threshold": quota_critical}, True)

    if config.get("ui.advisor.prompt_coach.enabled", False):
        for code in prompt.get("recommendation_codes") or []:
            if code in {"split_request", "add_target", "add_constraints", "add_acceptance"}:
                add(code, "info", "medium", "observed_local", {
                    "char_count": prompt.get("char_count"), "line_count": prompt.get("line_count"),
                    "section_count": prompt.get("section_count"), "bullet_count": prompt.get("bullet_count"),
                    "clause_count": prompt.get("clause_count"),
                })

    items.sort(key=lambda item: (LEVEL_ORDER[item["level"]], CODE_ORDER.get(item["code"], 0)), reverse=True)
    maximum = max(1, int(config.get("ui.advisor.max_visible", 1)))
    return {"items": items[:maximum], "all_items": items, "highest": items[0]["level"] if items else None,
            "baseline": baseline}


def _personal_threshold(stats: dict[str, Any]) -> float:
    median = float(stats.get("median") or 0)
    mad = float(stats.get("mad") or 0)
    return max(median * 2.5, median + 3.0 * mad)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
