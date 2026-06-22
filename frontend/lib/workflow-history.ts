import type { FileDiff, RunDetail, StepResult } from "./workflow-types";

export interface HistoricalToolCall {
  name: string;
  args?: Record<string, unknown>;
  result?: string;
  ok?: boolean;
  status: "pending" | "ok" | "error";
}

export interface HistoricalStepRunState {
  status: "pending" | "running" | "done" | "error" | "cancelled";
  output: string;
  toolCalls: HistoricalToolCall[];
  fileDiffs?: FileDiff[];
  costRmb?: number;
  durationMs?: number;
  modelUsed?: string;
  error?: string;
}

export function buildRunStatesFromDetail(
  run: RunDetail,
): Map<string, HistoricalStepRunState> {
  const states = new Map<string, HistoricalStepRunState>();
  for (const result of run.step_results) {
    states.set(result.step_id, buildStepState(result));
  }
  return states;
}

function buildStepState(result: StepResult): HistoricalStepRunState {
  return {
    status: normalizeStatus(result.status),
    output: result.output || "",
    toolCalls: (result.tool_calls || []).map(normalizeToolCall),
    fileDiffs: result.file_diffs || undefined,
    costRmb: result.cost_rmb ?? undefined,
    durationMs: result.duration_ms ?? undefined,
    modelUsed: result.model_used ?? undefined,
    error: result.error ?? undefined,
  };
}

function normalizeStatus(
  status: StepResult["status"],
): HistoricalStepRunState["status"] {
  if (status === "running" || status === "pending" || status === "error") {
    return status;
  }
  return "done";
}

function normalizeToolCall(
  call: NonNullable<StepResult["tool_calls"]>[number],
): HistoricalToolCall {
  const ok = call.ok !== false;
  return {
    ...call,
    ok,
    status: ok ? "ok" : "error",
  };
}
