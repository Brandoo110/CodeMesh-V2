import assert from "node:assert/strict";
import test from "node:test";
import {
  applyPromptDraftChanges,
  buildWorkflowContinueContext,
  findContinueStartStepId,
} from "./workflow-continue.ts";

const steps = [
  {
    id: "planner",
    step_order: 1,
    name: "1. Planner",
    enable_tools: ["grep_text", "read_file"],
  },
  {
    id: "coder",
    step_order: 2,
    name: "2. Coder",
    enable_tools: ["grep_text", "read_file", "edit_file", "write_file"],
  },
  {
    id: "reviewer",
    step_order: 3,
    name: "3. Reviewer",
    enable_tools: ["grep_text", "read_file"],
  },
];

test("findContinueStartStepId chooses the last writable step", () => {
  assert.equal(findContinueStartStepId(steps), "coder");
});

test("buildWorkflowContinueContext includes prior output and final reply", () => {
  const context = buildWorkflowContinueContext(
    steps,
    new Map([
      ["planner", { status: "done", output: "实现计划" }],
      ["coder", { status: "done", output: "已经写出 index.html" }],
      ["reviewer", { status: "done", output: "LGTM" }],
    ]),
    "最终回复：页面已经完成。",
  );

  assert.match(context, /1\. Planner/);
  assert.match(context, /已经写出 index\.html/);
  assert.match(context, /最终回复：页面已经完成。/);
});

test("applyPromptDraftChanges patches only proposed prompt fields", () => {
  const patched = applyPromptDraftChanges(
    [
      {
        id: "planner",
        step_order: 1,
        name: "1. Planner",
        system_prompt: "plan system",
        user_prompt: "old plan",
        enable_tools: ["read_file"],
      },
      {
        id: "coder",
        step_order: 2,
        name: "2. Coder",
        system_prompt: "code system",
        user_prompt: "old code",
        enable_tools: ["write_file"],
      },
    ],
    {
      summary: "draft",
      start_step_id: "planner",
      changes: [
        {
          step_id: "planner",
          step_name: "1. Planner",
          field: "user_prompt",
          old_text: "old plan",
          new_text: "new plan",
          reason: "需求变更",
        },
      ],
    },
  );

  assert.equal(patched[0].user_prompt, "new plan");
  assert.equal(patched[0].system_prompt, "plan system");
  assert.equal(patched[1].user_prompt, "old code");
});
