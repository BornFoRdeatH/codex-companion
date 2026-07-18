"use strict";

const assert = require("node:assert/strict");
const {clampCount, planTurns} = require("../ui/history_virtualization.js");

const turns = Array.from({length: 25}, (_, index) => ({
  turnId: `turn-${index + 1}`,
  completed: true,
}));

let plan = planTurns(turns, 10, "");
assert.equal(plan.total, 25);
assert.equal(plan.visible.length, 10);
assert.equal(plan.hiddenCount, 15);
assert.deepEqual(plan.visible, turns.slice(15).map(turn => turn.turnId));

plan = planTurns(turns, 20, "");
assert.equal(plan.visible.length, 20);
assert.equal(plan.hiddenCount, 5);

plan = planTurns(turns, 30, "");
assert.equal(plan.visible.length, 25);
assert.equal(plan.hiddenCount, 0);

const streaming = [...turns, {turnId: "streaming", completed: false}];
plan = planTurns(streaming, 10, "streaming");
assert.ok(plan.visible.includes("streaming"));
assert.equal(plan.hiddenCount, 15);

assert.equal(clampCount(4, 10), 10);
assert.equal(clampCount(101, 10), 10);
assert.equal(clampCount(5, 10), 5);
assert.equal(clampCount(100, 10), 100);

const source = require("node:fs").readFileSync(require.resolve("../ui/history_virtualization.js"), "utf8");
for (const forbidden of ["prompt", "assistant", "tool contents", "textContent"])
  assert.equal(source.includes(forbidden), false, `privacy-sensitive term: ${forbidden}`);

const longChat = Array.from({length: 200}, (_, index) => ({turnId: `long-${index}`, completed: true}));
const started = performance.now();
for (let index = 0; index < 500; index++) {
  const longPlan = planTurns(longChat, 10, "");
  assert.equal(longPlan.hiddenCount, 190);
}
assert.ok(performance.now() - started < 250, "200-turn planning should stay well within one frame per update");

console.log("history virtualization fixture passed");
