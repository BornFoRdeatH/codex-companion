"use strict";

const assert = require("node:assert/strict");
const {
  boundedCount, planWindow, validateMountedRange, signedBoundaryScrollTop, compensatedScrollTop, shouldClampScroll,
} = require("../ui/history_focus.js");

const mounted = Array.from({length: 5}, (_, index) => ({
  turnKey: `turn-${index + 30}`,
  turnNumber: index + 30,
  totalTurnCount: 34,
  parentKey: "conversation",
}));

let range = validateMountedRange(mounted);
assert.equal(range.compatible, true);
assert.equal(range.totalTurnCount, 34);
let plan = planWindow(range.totalTurnCount, 10);
assert.deepEqual(plan, {total: 34, size: 10, windowStart: 25, hiddenLogicalTurns: 24});

plan = planWindow(range.totalTurnCount, 20);
assert.equal(plan.windowStart, 15);
assert.equal(plan.hiddenLogicalTurns, 14);

assert.equal(validateMountedRange(mounted.slice(0, 2)).compatible, false);
assert.equal(validateMountedRange(mounted.map((row, index) => ({...row, parentKey: index ? "a" : "b"}))).reason, "mixed_containers");
assert.equal(validateMountedRange(mounted.map((row, index) => ({...row, turnNumber: index + 30 + (index > 2 ? 1 : 0)}))).reason, "non_contiguous_range");
assert.equal(validateMountedRange(mounted.map((row, index) => ({...row, totalTurnCount: 34 + (index === 4 ? 1 : 0)}))).reason, "unstable_total");

assert.equal(boundedCount(2), 3);
assert.equal(boundedCount(101), 3);
assert.equal(boundedCount(5), 5);
assert.equal(boundedCount(3), 3);
assert.equal(boundedCount(100), 100);

assert.equal(signedBoundaryScrollTop(-8000, 100, 83, 30), -8013);
assert.equal(signedBoundaryScrollTop(8000, 100, 83, 30), 7987);
assert.equal(signedBoundaryScrollTop(0, Number.NaN, 0, 20), null);
assert.equal(shouldClampScroll(-8100, -8013), true);
assert.equal(shouldClampScroll(-8014, -8013), false);
assert.equal(shouldClampScroll(7900, 7987), true);
assert.equal(compensatedScrollTop(-8013, 120, 150), -7983);
assert.equal(compensatedScrollTop(500, 150, 120), 470);

const longRange = Array.from({length: 5}, (_, index) => ({turnKey: `long-${196 + index}`, turnNumber: 196 + index, totalTurnCount: 200, parentKey: "root"}));
const started = performance.now();
for (let index = 0; index < 1000; index++) {
  assert.equal(validateMountedRange(longRange).compatible, true);
  assert.equal(planWindow(200, 10).hiddenLogicalTurns, 190);
}
assert.ok(performance.now() - started < 250, "native range planning must stay sub-frame per update");

const source = require("node:fs").readFileSync(require.resolve("../ui/history_focus.js"), "utf8");
for (const forbidden of ["prompt", "assistant", "tool contents", "textContent"])
  assert.equal(source.includes(forbidden), false, `privacy-sensitive term: ${forbidden}`);

console.log("history focus fixture passed");
