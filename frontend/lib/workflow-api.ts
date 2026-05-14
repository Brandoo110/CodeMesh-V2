/**
 * Workflows API client（v5）。
 *
 * 复用 lib/api.ts 的 ApiError + base URL 模式，独立文件避免污染主 api.ts。
 */

import type {
  Run,
  RunDetail,
  Step,
  StepCreateRequest,
  StepUpdateRequest,
  Workflow,
  WorkflowCreateRequest,
  WorkflowDetail,
  WorkflowUpdateRequest,
} from "./workflow-types";
import { ApiError } from "./api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      // fall through
    }
    throw new ApiError(res.status, detail);
  }
  // DELETE 返回 {deleted: id} 也是 JSON，能 parse
  return res.json();
}

// ─────────────── Workflows ───────────────

export async function listWorkflows(): Promise<Workflow[]> {
  return request<Workflow[]>("/api/workflows");
}

export async function getWorkflow(id: string): Promise<WorkflowDetail> {
  return request<WorkflowDetail>(`/api/workflows/${id}`);
}

export async function createWorkflow(req: WorkflowCreateRequest): Promise<Workflow> {
  return request<Workflow>("/api/workflows", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function updateWorkflow(
  id: string,
  req: WorkflowUpdateRequest,
): Promise<Workflow> {
  return request<Workflow>(`/api/workflows/${id}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function deleteWorkflow(id: string): Promise<void> {
  await request(`/api/workflows/${id}`, { method: "DELETE" });
}

export async function forkWorkflow(id: string): Promise<WorkflowDetail> {
  return request<WorkflowDetail>(`/api/workflows/${id}/fork`, { method: "POST" });
}

// ─────────────── Steps ───────────────

export async function addStep(workflowId: string, req: StepCreateRequest): Promise<Step> {
  return request<Step>(`/api/workflows/${workflowId}/steps`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function updateStep(
  workflowId: string,
  stepId: string,
  req: StepUpdateRequest,
): Promise<Step> {
  return request<Step>(`/api/workflows/${workflowId}/steps/${stepId}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function deleteStep(workflowId: string, stepId: string): Promise<void> {
  await request(`/api/workflows/${workflowId}/steps/${stepId}`, { method: "DELETE" });
}

export async function reorderSteps(workflowId: string, stepIds: string[]): Promise<void> {
  await request(`/api/workflows/${workflowId}/steps/reorder`, {
    method: "POST",
    body: JSON.stringify({ step_ids: stepIds }),
  });
}

// ─────────────── Runs (read-only at Phase 6.2) ───────────────

export async function listRuns(workflowId: string, limit = 20): Promise<Run[]> {
  return request<Run[]>(`/api/workflows/${workflowId}/runs?limit=${limit}`);
}

export async function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/api/workflows/runs/${runId}`);
}
