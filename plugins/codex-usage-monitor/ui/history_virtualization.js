(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.__CodexUsageHistoryVirtualization = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function clampCount(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 5 && number <= 100 ? Math.trunc(number) : fallback;
  }

  function planTurns(turns, visibleTurns, activeTurnId) {
    const limit = clampCount(visibleTurns, 10);
    const ordered = Array.isArray(turns) ? turns.filter(turn => turn && turn.turnId) : [];
    const completed = ordered.filter(turn => turn.completed && turn.turnId !== activeTurnId);
    const visibleCompleted = new Set(completed.slice(-limit).map(turn => String(turn.turnId)));
    const visible = [], hidden = [];
    for (const turn of ordered) {
      const id = String(turn.turnId);
      if (!turn.completed || id === activeTurnId || visibleCompleted.has(id)) visible.push(id);
      else hidden.push(id);
    }
    return {visible, hidden, total: ordered.length, hiddenCount: hidden.length, limit};
  }

  return {clampCount, planTurns};
});
