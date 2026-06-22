import assert from "node:assert/strict";
import test from "node:test";
import { buildRunStatesFromDetail } from "./workflow-history.ts";

test("buildRunStatesFromDetail restores step logs for the run panel", () => {
  const states = buildRunStatesFromDetail({
    id: "run-1",
    workflow_id: "wf-1",
    status: "done",
    started_at: "2026-06-14T10:00:00",
    completed_at: "2026-06-14T10:00:05",
    total_cost_rmb: 0.05,
    error: null,
    final_reply: "最终回复",
    step_results: [
      {
        id: 1,
        run_id: "run-1",
        step_id: "coder",
        step_order: 2,
        status: "done",
        output: "Coder 输出",
        error: null,
        tool_calls: [
          { name: "write_file", args: { path: "artifacts/index.html" }, ok: true },
        ],
        file_diffs: [
          { path: "artifacts/index.html", before: "", after: "<html>", kind: "created" },
        ],
        model_used: "deepseek",
        cost_rmb: 0.02,
        duration_ms: 1200,
        started_at: "2026-06-14T10:00:01",
        completed_at: "2026-06-14T10:00:02",
      },
    ],
  });

  const coder = states.get("coder");
  assert.equal(coder?.status, "done");
  assert.equal(coder?.output, "Coder 输出");
  assert.equal(coder?.toolCalls[0].status, "ok");
  assert.equal(coder?.fileDiffs?.[0].kind, "created");
  assert.equal(coder?.modelUsed, "deepseek");
});
