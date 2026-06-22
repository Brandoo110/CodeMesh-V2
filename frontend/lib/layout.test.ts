import assert from "node:assert/strict";
import test from "node:test";

import {
  mainViewHostClassName,
  shouldKeepViewMounted,
  viewUsesChatSidebar,
} from "./layout.ts";

test("only chat view uses the chat session sidebar", () => {
  assert.equal(viewUsesChatSidebar("chat"), true);
  assert.equal(viewUsesChatSidebar("stats"), false);
  assert.equal(viewUsesChatSidebar("workflows"), false);
});

test("workflow view stays mounted while switching to other views", () => {
  assert.equal(shouldKeepViewMounted("workflows"), true);
  assert.equal(shouldKeepViewMounted("chat"), false);
  assert.equal(shouldKeepViewMounted("stats"), false);
  assert.equal(shouldKeepViewMounted("memory"), false);
});

test("main view host keeps flex children full height", () => {
  assert.match(mainViewHostClassName, /\bflex-1\b/);
  assert.match(mainViewHostClassName, /\bflex\b/);
  assert.match(mainViewHostClassName, /\bflex-col\b/);
  assert.match(mainViewHostClassName, /\bmin-h-0\b/);
});
