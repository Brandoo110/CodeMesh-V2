import type { Step, WorkflowPromptDraftResponse } from "./workflow-types";

const WRITE_TOOLS = new Set(["edit_file", "write_file", "delete_file"]);
const DEFAULT_OUTPUT_LIMIT = 1200;

type ContinueStep = Pick<Step, "id" | "step_order" | "name" | "enable_tools">;

export interface ContinueRunState {
  status?: string;
  output?: string;
}

export function findContinueStartStepId(steps: ContinueStep[]): string | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (stepCanWrite(steps[i])) return steps[i].id;
  }
  return steps[0]?.id ?? null;
}

export function buildWorkflowContinueContext(
  steps: ContinueStep[],
  runStates: Map<string, ContinueRunState>,
  finalReply: string,
): string {
  const parts = ["上一次 workflow 执行摘要："];

  for (const step of steps) {
    const state = runStates.get(step.id);
    if (!state) continue;

    const output = truncateForPrompt(state.output || "", DEFAULT_OUTPUT_LIMIT);
    parts.push([
      `#${step.step_order} ${step.name}`,
      `状态：${state.status || "unknown"}`,
      output ? `输出：\n${output}` : "",
    ].filter(Boolean).join("\n"));
  }

  const reply = truncateForPrompt(finalReply, DEFAULT_OUTPUT_LIMIT);
  if (reply) {
    parts.push(`最终回复：\n${reply}`);
  }

  return parts.join("\n\n");
}

export function applyPromptDraftChanges<T extends Pick<Step,
  "id" | "system_prompt" | "user_prompt"
>>(
  steps: T[],
  draft: WorkflowPromptDraftResponse,
): T[] {
  const changesByStep = new Map<string, WorkflowPromptDraftResponse["changes"]>();
  for (const change of draft.changes) {
    const changes = changesByStep.get(change.step_id) || [];
    changes.push(change);
    changesByStep.set(change.step_id, changes);
  }

  return steps.map((step) => {
    const changes = changesByStep.get(step.id);
    if (!changes) return step;

    let next = { ...step };
    for (const change of changes) {
      next = {
        ...next,
        [change.field]: change.new_text,
      };
    }
    return next;
  });
}

function stepCanWrite(step: ContinueStep): boolean {
  const tools = new Set(step.enable_tools || []);
  if (tools.has("*")) return true;
  for (const tool of WRITE_TOOLS) {
    if (tools.has(tool)) return true;
  }
  return false;
}

function truncateForPrompt(text: string, limit: number): string {
  const trimmed = text.trim();
  if (trimmed.length <= limit) return trimmed;
  return `${trimmed.slice(0, limit)}\n...[已截断]`;
}
