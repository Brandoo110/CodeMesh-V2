import assert from "node:assert/strict";
import test from "node:test";

import { formatStepTitle, getStepOrderPrefix } from "./workflow-display.ts";

test("step title does not duplicate matching numeric prefixes", () => {
  assert.equal(getStepOrderPrefix(1, "1. Planner"), null);
  assert.equal(getStepOrderPrefix(2, "2. Coder"), null);
  assert.equal(getStepOrderPrefix(3, "#3 Reviewer"), null);

  assert.equal(formatStepTitle(1, "1. Planner"), "1. Planner");
  assert.equal(formatStepTitle(2, "2. Coder"), "2. Coder");
});

test("step title keeps generated prefix for unnumbered names", () => {
  assert.equal(getStepOrderPrefix(1, "Planner"), "#1");
  assert.equal(formatStepTitle(1, "Planner"), "#1 Planner");
});

test("step title only suppresses the matching order number", () => {
  assert.equal(getStepOrderPrefix(2, "1. Planner"), "#2");
  assert.equal(formatStepTitle(2, "1. Planner"), "#2 1. Planner");
});
