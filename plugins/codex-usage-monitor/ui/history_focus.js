(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.__CodexCompanionHistoryFocus = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function boundedCount(value, fallback = 10) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 5 && number <= 100 ? Math.trunc(number) : fallback;
  }

  function planWindow(totalTurnCount, visibleTurns) {
    const total = Math.max(0, Math.trunc(Number(totalTurnCount) || 0));
    const size = boundedCount(visibleTurns, 10);
    const windowStart = total ? Math.max(1, total - size + 1) : 1;
    return {total, size, windowStart, hiddenLogicalTurns: Math.max(0, windowStart - 1)};
  }

  function validateMountedRange(records) {
    if (!Array.isArray(records)) return {compatible: false, reason: "missing_records"};
    const unique = new Map();
    for (const record of records) {
      const turnNumber = Number(record?.turnNumber), totalTurnCount = Number(record?.totalTurnCount);
      if (!record?.turnKey || !Number.isInteger(turnNumber) || !Number.isInteger(totalTurnCount)) continue;
      if (turnNumber < 1 || totalTurnCount < turnNumber || !record.parentKey) continue;
      unique.set(String(record.turnKey), {...record, turnNumber, totalTurnCount});
    }
    const mounted = [...unique.values()].sort((a, b) => a.turnNumber - b.turnNumber);
    if (mounted.length < 3) return {compatible: false, reason: "too_few_mounted_turns", mounted};
    if (new Set(mounted.map(record => record.parentKey)).size !== 1)
      return {compatible: false, reason: "mixed_containers", mounted};
    if (new Set(mounted.map(record => record.totalTurnCount)).size !== 1)
      return {compatible: false, reason: "unstable_total", mounted};
    for (let index = 1; index < mounted.length; index++)
      if (mounted[index].turnNumber !== mounted[index - 1].turnNumber + 1)
        return {compatible: false, reason: "non_contiguous_range", mounted};
    return {compatible: true, reason: "native_range", mounted, totalTurnCount: mounted[0].totalTurnCount};
  }

  return {boundedCount, planWindow, validateMountedRange};
});
