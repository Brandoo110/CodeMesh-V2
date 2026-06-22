/**
 * v5 工作流类型定义。
 *
 * 对应 web/schemas.py 的 Workflow / Step / Run / StepResult 系列，手动对齐。
 */

export interface Workflow {
  id: string;
  name: string;
  description: string;
  is_template: boolean;
  created_at: string;
  updated_at: string;
  step_count: number;
}

/** 工具白名单——支持 ["*"] 全开 或具体工具名数组。 */
export type ToolAllowlist = string[];

export interface Step {
  id: string;
  workflow_id: string;
  step_order: number;
  name: string;
  model: string | null;
  system_prompt: string;
  user_prompt: string;
  enable_tools: ToolAllowlist;
}

/** GET /api/workflows/{id} 返回的详情。 */
export interface WorkflowDetail extends Workflow {
  steps: Step[];
}

export interface Run {
  id: string;
  workflow_id: string;
  status: "running" | "done" | "error" | "cancelled";
  started_at: string;
  completed_at: string | null;
  total_cost_rmb: number;
  error: string | null;
  final_reply: string | null;
}

export interface FileDiff {
  path: string;
  before: string;
  after: string;
  kind: "modified" | "created" | "deleted";
}

export interface StepResult {
  id: number;
  run_id: string;
  step_id: string;
  step_order: number;
  status: "pending" | "running" | "done" | "error";
  output: string | null;
  error: string | null;
  tool_calls: Array<{
    name: string;
    args?: Record<string, unknown>;
    result?: string;
    ok?: boolean;
  }> | null;
  file_diffs: FileDiff[] | null;
  model_used: string | null;
  cost_rmb: number | null;
  duration_ms: number | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface RunDetail extends Run {
  step_results: StepResult[];
}

// ─────────────── Request bodies ───────────────

export interface WorkflowCreateRequest {
  name: string;
  description?: string;
}

export interface WorkflowUpdateRequest {
  name?: string;
  description?: string;
}

export interface WorkflowContinueRequest {
  user_request: string;
  run_context?: string;
  start_step_id?: string | null;
}

export interface WorkflowPromptDraftRequest {
  user_request: string;
  run_context?: string;
}

export type WorkflowPromptField = "system_prompt" | "user_prompt";

export interface WorkflowPromptChange {
  step_id: string;
  step_name: string;
  field: WorkflowPromptField;
  old_text: string;
  new_text: string;
  reason: string;
}

export interface WorkflowPromptDraftResponse {
  summary: string;
  start_step_id: string;
  changes: WorkflowPromptChange[];
}

export interface StepCreateRequest {
  name: string;
  model?: string | null;
  system_prompt?: string;
  user_prompt?: string;
  enable_tools?: ToolAllowlist;
}

export type StepUpdateRequest = Partial<StepCreateRequest>;
