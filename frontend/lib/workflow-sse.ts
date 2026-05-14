/**
 * Workflow SSE 消费器（v5 Phase 6.5）。
 *
 * 与 lib/sse.ts 完全相同的 SSE 协议（sse-starlette 输出），但是端点不同：
 *   /api/workflows/{id}/run
 *   /api/workflows/{id}/steps/{sid}/run
 *
 * Event 类型：
 *   run_start    {run_id, workflow_id, total_steps}
 *   step_start   {step_id, name, model, step_order}
 *   token        {delta, step_id}
 *   tool_start   {name, args, step_id}
 *   tool_end     {name, result, ok, step_id}
 *   usage        {cost_rmb, model, prompt, completion, step_id}
 *   step_end     {step_id, step_order, ok, cost_rmb, duration_ms, model_used}
 *   done         {ok, total_cost, run_id}
 *   error        {message}
 *   cancelled    {run_id}
 */

import { ApiError } from "./api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}

async function* consumeSSE(res: Response): AsyncGenerator<StreamEvent> {
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.text()) || res.statusText;
    } catch {
      // ignore
    }
    throw new ApiError(res.status, detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseSSEFrame(frame);
        if (ev) yield ev;
      }
    }
    if (buffer.trim()) {
      const ev = parseSSEFrame(buffer);
      if (ev) yield ev;
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSSEFrame(frame: string): StreamEvent | null {
  const lines = frame.split("\n");
  let eventName = "message";
  let data = "";
  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data += line.slice(5).trim();
    }
  }
  if (!data) return null;
  try {
    return { event: eventName, data: JSON.parse(data) };
  } catch {
    return { event: eventName, data: { raw: data } };
  }
}

/** 整体工作流执行 SSE。 */
export async function* streamWorkflowRun(
  workflowId: string,
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${API_BASE}/api/workflows/${workflowId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  yield* consumeSSE(res);
}

/** 单步执行 SSE（Phase 6.8 启用 UI）。 */
export async function* streamStepRun(
  workflowId: string,
  stepId: string,
  seedInput?: string,
): AsyncGenerator<StreamEvent> {
  const url = new URL(
    `${API_BASE}/api/workflows/${workflowId}/steps/${stepId}/run`,
  );
  if (seedInput) url.searchParams.set("seed_input", seedInput);
  const res = await fetch(url.toString(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  yield* consumeSSE(res);
}

/** 中断当前 run。返回 ok=true 即使 run 不存在（幂等）。 */
export async function cancelRun(runId: string): Promise<void> {
  await fetch(`${API_BASE}/api/workflows/runs/${runId}/cancel`, {
    method: "POST",
  });
}
