import assert from "node:assert/strict";
import test from "node:test";
import {
  RUN_PANEL_DEFAULT_WIDTH,
  RUN_PANEL_MAX_WIDTH,
  RUN_PANEL_MIN_WIDTH,
  clampRunPanelWidth,
} from "./panel-size.ts";

test("clampRunPanelWidth keeps the run panel readable", () => {
  assert.equal(clampRunPanelWidth(120), RUN_PANEL_MIN_WIDTH);
  assert.equal(clampRunPanelWidth(420), 420);
  assert.equal(clampRunPanelWidth(1200), RUN_PANEL_MAX_WIDTH);
  assert.equal(clampRunPanelWidth(Number.NaN), RUN_PANEL_DEFAULT_WIDTH);
});
