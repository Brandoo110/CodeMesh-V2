"use client";

/**
 * 右栏运行面板（v5 Phase 6.5）。
 *
 * 实时显示工作流执行进度：
 * - 每个 step：pending / running / done / error 状态卡
 * - 当前 step 展开：token 流 + tool call 实时高亮
 * - 已完成 step 折叠为单行（点击展开看完整 output）
 * - 顶部：⏹ 停止 按钮
 * - 底部：总时长 + 总成本 + 通过率
 *
 * Phase 6.6 在 step 完成后加 "查看 Diff" 按钮联动 DiffViewer。
 */

import { useEffect, useMemo, useState } from "react";
import { Square, Check, X, ChevronDown, ChevronRight, Loader2, GitCompare } from "lucide-react";
import { cancelRun } from "@/lib/workflow-sse";
import type { Step, FileDiff } from "@/lib/workflow-types";
import { DiffViewer } from "./DiffViewer";

export interface StepRunState {
  status: "pending" | "running" | "done" | "error" | "cancelled";
  output: string;
  toolCalls: {
    name: string;
    args?: Record<string, unknown>;
    result?: string;
    ok?: boolean;
    status: "pending" | "ok" | "error";
  }[];
  fileDiffs?: FileDiff[];     // v5 Phase 6.6
  costRmb?: number;
  durationMs?: number;
  modelUsed?: string;
  error?: string;
}

interface Props {
  steps: Step[];                            // 工作流的全部 steps（顺序）
  runStates: Map<string, StepRunState>;     // step.id → 实时状态
  isRunning: boolean;                       // 整体 run 进行中
  runId: string | null;                     // 当前 run id（中断需要）
  totalCost: number;
  runError?: string | null;
  onStop?: () => void;
}

export function WorkflowRunPanel({
  steps,
  runStates,
  isRunning,
  runId,
  totalCost,
  runError,
  onStop,
}: Props) {
  const handleCancel = async () => {
    if (!runId) return;
    await cancelRun(runId);
    onStop?.();
  };

  // 找当前 running step；其他默认折叠
  const activeStepId = useMemo(() => {
    for (const s of steps) {
      const st = runStates.get(s.id);
      if (st?.status === "running") return s.id;
    }
    return null;
  }, [steps, runStates]);

  const completedCount = steps.filter((s) =>
    ["done", "error"].includes(runStates.get(s.id)?.status ?? "pending"),
  ).length;
  const hasAnyRun = runStates.size > 0;

  return (
    <aside className="w-80 flex-shrink-0 border-l border-border bg-surface flex flex-col">
      {/* Header */}
      <header className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div>
          <div className="text-xs text-fg-subtle uppercase tracking-wider">
            运行日志
          </div>
          {hasAnyRun && (
            <div className="text-xs text-fg-muted mt-0.5">
              {completedCount} / {steps.length} 完成
            </div>
          )}
        </div>
        {isRunning && (
          <button
            onClick={handleCancel}
            className="flex items-center gap-1 px-2 py-1 rounded-md bg-error/10 hover:bg-error/20 text-error text-xs transition-colors"
            title="停止当前 run（在 step boundary 退出）"
          >
            <Square size={12} />
            停止
          </button>
        )}
      </header>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {!hasAnyRun ? (
          <div className="text-sm text-fg-subtle p-2">
            点击工作流右上角的 ▶ 执行按钮开始。
          </div>
        ) : (
          steps.map((step) => {
            const state = runStates.get(step.id);
            return (
              <StepRunRow
                key={step.id}
                step={step}
                state={state}
                expanded={step.id === activeStepId}
              />
            );
          })
        )}

        {runError && (
          <div className="mt-3 p-2 rounded-md bg-error/10 text-error text-xs">
            <div className="font-medium mb-1">运行失败</div>
            <div className="font-mono">{runError}</div>
          </div>
        )}
      </div>

      {/* Footer */}
      {hasAnyRun && (
        <footer className="px-4 py-2 border-t border-border text-xs text-fg-muted flex items-center justify-between">
          <span>总成本</span>
          <span className="font-mono tabular-nums">¥{totalCost.toFixed(4)}</span>
        </footer>
      )}
    </aside>
  );
}

// ─────────────── 单步行 ───────────────

interface RowProps {
  step: Step;
  state?: StepRunState;
  expanded: boolean;
}

function StepRunRow({ step, state, expanded: defaultExpanded }: RowProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  // 父组件改 active step 时同步展开（用户仍可手动 toggle）
  useEffect(() => {
    setExpanded(defaultExpanded);
  }, [defaultExpanded]);

  const status = state?.status ?? "pending";
  const ok = status === "done";
  const fail = status === "error";
  const running = status === "running";

  return (
    <div
      className={`rounded-md border transition-colors ${
        running
          ? "border-accent bg-accent/5"
          : fail
            ? "border-error/40 bg-error/5"
            : ok
              ? "border-success/30 bg-canvas"
              : "border-border bg-canvas/40"
      }`}
    >
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-3 py-2 flex items-center gap-2 text-left"
      >
        <StatusIcon status={status} />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-fg truncate">
            #{step.step_order} {step.name}
          </div>
          {state && (state.costRmb !== undefined || state.modelUsed) && (
            <div className="text-xs text-fg-subtle truncate">
              {state.modelUsed || step.model || "—"}
              {state.durationMs ? ` · ${(state.durationMs / 1000).toFixed(1)}s` : ""}
              {state.costRmb ? ` · ¥${state.costRmb.toFixed(4)}` : ""}
            </div>
          )}
        </div>
        {expanded ? (
          <ChevronDown size={14} className="text-fg-subtle" />
        ) : (
          <ChevronRight size={14} className="text-fg-subtle" />
        )}
      </button>

      {expanded && state && (
        <div className="px-3 pb-3 space-y-2 border-t border-border/50 pt-2">
          {/* tool calls */}
          {state.toolCalls.length > 0 && (
            <div className="space-y-1">
              {state.toolCalls.map((tc, i) => (
                <div
                  key={i}
                  className={`text-xs font-mono px-2 py-1 rounded border-l-2 ${
                    tc.status === "pending"
                      ? "border-l-warning bg-warning/5 text-fg-muted"
                      : tc.status === "error"
                        ? "border-l-error bg-error/5"
                        : "border-l-success bg-canvas/40"
                  }`}
                >
                  <span className="text-fg-muted">🔧 </span>
                  <span className="text-fg">{tc.name}</span>
                  {tc.args && (
                    <span className="text-fg-subtle">
                      {" "}
                      {JSON.stringify(tc.args).slice(0, 60)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* output (流式 token 累计) */}
          {state.output && (
            <div className="text-xs text-fg-muted whitespace-pre-wrap font-mono leading-relaxed max-h-48 overflow-y-auto">
              {state.output}
              {running && (
                <span className="inline-block w-1.5 h-3 bg-accent animate-pulse ml-0.5" />
              )}
            </div>
          )}

          {fail && state.error && (
            <div className="text-xs text-error font-mono">{state.error}</div>
          )}

          {/* v5 Phase 6.6 护城河 #3：file diffs */}
          {state.fileDiffs && state.fileDiffs.length > 0 && (
            <div className="pt-2 border-t border-border/30 space-y-2">
              <div className="flex items-center gap-1.5 text-xs text-fg-muted">
                <GitCompare size={11} />
                文件变更 ({state.fileDiffs.length})
              </div>
              <DiffViewer diffs={state.fileDiffs} collapsed={true} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: StepRunState["status"] }) {
  switch (status) {
    case "running":
      return <Loader2 size={14} className="text-accent animate-spin" />;
    case "done":
      return <Check size={14} className="text-success" />;
    case "error":
    case "cancelled":
      return <X size={14} className="text-error" />;
    default:
      return (
        <div className="w-3.5 h-3.5 rounded-full border border-border bg-canvas" />
      );
  }
}

