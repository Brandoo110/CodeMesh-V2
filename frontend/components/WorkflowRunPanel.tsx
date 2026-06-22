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

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Square,
  Check,
  X,
  ChevronDown,
  ChevronRight,
  Loader2,
  GitCompare,
  Send,
} from "lucide-react";
import { cancelRun } from "@/lib/workflow-sse";
import {
  RUN_PANEL_DEFAULT_WIDTH,
  RUN_PANEL_MAX_WIDTH,
  RUN_PANEL_MIN_WIDTH,
  clampRunPanelWidth,
} from "@/lib/panel-size";
import { formatStepTitle } from "@/lib/workflow-display";
import type { Step, FileDiff } from "@/lib/workflow-types";
import type {
  Run,
  WorkflowPromptChange,
  WorkflowPromptDraftResponse,
} from "@/lib/workflow-types";
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

export interface ReworkRunEvent {
  reviewerStepId: string;
  reviewerName: string;
  targetStepId: string;
  targetName: string;
  reason?: string;
}

export interface ReviewDecisionRunEvent {
  reviewerStepId: string;
  reviewerName: string;
  status: "done" | "needs_rework";
  targetStepId?: string;
  targetName?: string;
  reason?: string;
  model?: string;
  costRmb?: number;
}

interface Props {
  steps: Step[];                            // 工作流的全部 steps（顺序）
  runStates: Map<string, StepRunState>;     // step.id → 实时状态
  reviewDecisionEvents?: ReviewDecisionRunEvent[];
  reworkEvents?: ReworkRunEvent[];
  isRunning: boolean;                       // 整体 run 进行中
  runId: string | null;                     // 当前 run id（中断需要）
  totalCost: number;
  runError?: string | null;
  finalReply?: string;
  isFinalizingReply?: boolean;
  runHistory?: Run[];
  loadingRunHistory?: boolean;
  loadedHistoryRunId?: string | null;
  onStop?: () => void;
  onPrepareContinue?: (request: string) => Promise<WorkflowPromptDraftResponse | undefined>;
  onConfirmContinue?: (
    draft: WorkflowPromptDraftResponse,
    request: string,
  ) => Promise<void>;
  onLoadRun?: (runId: string) => Promise<void>;
}

export function WorkflowRunPanel({
  steps,
  runStates,
  reviewDecisionEvents = [],
  reworkEvents = [],
  isRunning,
  runId,
  totalCost,
  runError,
  finalReply,
  isFinalizingReply,
  runHistory = [],
  loadingRunHistory,
  loadedHistoryRunId,
  onStop,
  onPrepareContinue,
  onConfirmContinue,
  onLoadRun,
}: Props) {
  const [panelWidth, setPanelWidth] = useState(RUN_PANEL_DEFAULT_WIDTH);
  const [isResizing, setIsResizing] = useState(false);
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);

  const handleCancel = async () => {
    if (!runId) return;
    await cancelRun(runId);
    onStop?.();
  };

  const startResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    dragState.current = {
      startX: event.clientX,
      startWidth: panelWidth,
    };
    setIsResizing(true);
  };

  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setPanelWidth((w) => clampRunPanelWidth(w + 40));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setPanelWidth((w) => clampRunPanelWidth(w - 40));
    } else if (event.key === "Home") {
      event.preventDefault();
      setPanelWidth(RUN_PANEL_MIN_WIDTH);
    } else if (event.key === "End") {
      event.preventDefault();
      setPanelWidth(RUN_PANEL_MAX_WIDTH);
    }
  };

  useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      if (!dragState.current) return;
      const nextWidth =
        dragState.current.startWidth + dragState.current.startX - event.clientX;
      setPanelWidth(clampRunPanelWidth(nextWidth));
    };

    const stopResize = () => {
      dragState.current = null;
      setIsResizing(false);
    };

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize, { once: true });
    window.addEventListener("pointercancel", stopResize, { once: true });

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
    };
  }, [isResizing]);

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
    <aside
      className="relative flex-shrink-0 border-l border-border bg-surface flex flex-col"
      style={{ width: panelWidth }}
    >
      <button
        type="button"
        role="separator"
        aria-label="调整运行日志宽度"
        aria-orientation="vertical"
        aria-valuemin={RUN_PANEL_MIN_WIDTH}
        aria-valuemax={RUN_PANEL_MAX_WIDTH}
        aria-valuenow={panelWidth}
        title="拖动调整运行日志宽度"
        onPointerDown={startResize}
        onKeyDown={handleResizeKeyDown}
        className="group absolute left-0 top-0 z-20 h-full w-2 -translate-x-1 cursor-col-resize touch-none outline-none"
      >
        <span
          className={`mx-auto block h-full w-px transition-colors ${
            isResizing
              ? "bg-accent"
              : "bg-border group-hover:bg-accent group-focus-visible:bg-accent"
          }`}
        />
      </button>

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
        <div className="flex items-center gap-2">
          {runHistory.length > 0 && (
            <select
              value={loadedHistoryRunId || ""}
              disabled={isRunning || loadingRunHistory || !onLoadRun}
              onChange={(event) => {
                const runId = event.target.value;
                if (runId) void onLoadRun?.(runId);
              }}
              className="max-w-32 rounded-md border border-border bg-canvas px-2 py-1 text-xs text-fg-muted outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
              title="查看历史运行日志"
            >
              <option value="">历史记录</option>
              {runHistory.map((run) => (
                <option key={run.id} value={run.id}>
                  {formatRunOption(run)}
                </option>
              ))}
            </select>
          )}
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
        </div>
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

        {reviewDecisionEvents.length > 0 && (
          <ReviewDecisionEventList events={reviewDecisionEvents} />
        )}

        {reworkEvents.length > 0 && (
          <ReworkEventList events={reworkEvents} />
        )}

        {(finalReply || isFinalizingReply) && (
          <FinalReplyText
            reply={finalReply}
            isFinalizing={Boolean(isFinalizingReply)}
          />
        )}

        {runError && (
          <div className="mt-3 p-2 rounded-md bg-error/10 text-error text-xs">
            <div className="font-medium mb-1">运行失败</div>
            <div className="font-mono">{runError}</div>
          </div>
        )}
      </div>

      {hasAnyRun && (
        <ContinueComposer
          disabled={isRunning || !onPrepareContinue || !onConfirmContinue}
          onPrepareContinue={onPrepareContinue}
          onConfirmContinue={onConfirmContinue}
        />
      )}

      {/* Footer */}
      {hasAnyRun && (
        <footer className="px-4 py-2 border-t border-border text-xs text-fg-muted flex items-center justify-between">
          <span>本次总成本</span>
          <span className="font-mono tabular-nums">¥{totalCost.toFixed(4)}</span>
        </footer>
      )}
    </aside>
  );
}

function ReviewDecisionEventList({
  events,
}: {
  events: ReviewDecisionRunEvent[];
}) {
  return (
    <section className="mt-3 border-t border-border pt-3">
      <div className="mb-2 text-xs font-medium text-fg-muted">
        Review Decision
      </div>
      <div className="space-y-2">
        {events.map((event, idx) => {
          const needsRework = event.status === "needs_rework";
          return (
            <div
              key={`${event.reviewerStepId}-${idx}`}
              className={`rounded-md border px-3 py-2 text-xs ${
                needsRework
                  ? "border-accent/40 bg-accent/5"
                  : "border-success/30 bg-success/5"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-fg">
                  {event.reviewerName} 判定：{needsRework ? "需要返工" : "通过"}
                </span>
                {event.model && (
                  <span className="font-mono text-[11px] text-fg-subtle">
                    {event.model}
                  </span>
                )}
              </div>
              {event.targetName && (
                <div className="mt-1 text-fg-muted">
                  目标：{event.targetName}
                </div>
              )}
              {event.reason && (
                <div className="mt-1 whitespace-pre-wrap text-fg-muted">
                  {event.reason}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ReworkEventList({ events }: { events: ReworkRunEvent[] }) {
  return (
    <section className="mt-3 border-t border-border pt-3">
      <div className="mb-2 text-xs font-medium text-accent">
        Review 打回记录
      </div>
      <div className="space-y-2">
        {events.map((event, idx) => (
          <div
            key={`${event.reviewerStepId}-${event.targetStepId}-${idx}`}
            className="rounded-md border border-accent/40 bg-accent/5 px-3 py-2 text-xs"
          >
            <div className="text-fg">
              {event.reviewerName} 打回 {event.targetName} 返工
            </div>
            {event.reason && (
              <div className="mt-1 whitespace-pre-wrap text-fg-muted">
                {event.reason}
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function ContinueComposer({
  disabled,
  onPrepareContinue,
  onConfirmContinue,
}: {
  disabled: boolean;
  onPrepareContinue?: (request: string) => Promise<WorkflowPromptDraftResponse | undefined>;
  onConfirmContinue?: (
    draft: WorkflowPromptDraftResponse,
    request: string,
  ) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [draft, setDraft] = useState<WorkflowPromptDraftResponse | null>(null);
  const [draftRequest, setDraftRequest] = useState("");

  const submit = async () => {
    const request = text.trim();
    if (!request || !onPrepareContinue || disabled || submitting) return;

    setError("");
    setSubmitting(true);
    try {
      const nextDraft = await onPrepareContinue(request);
      if (!nextDraft || nextDraft.changes.length === 0) {
        setError("没有生成可确认的 Prompt 修改。");
        return;
      }
      setDraft(nextDraft);
      setDraftRequest(request);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const confirm = async () => {
    if (!draft || !draftRequest || !onConfirmContinue || disabled || submitting) {
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      await onConfirmContinue(draft, draftRequest);
      setText("");
      setDraft(null);
      setDraftRequest("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      className="border-t border-border p-3 space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <textarea
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          if (draft) {
            setDraft(null);
            setDraftRequest("");
          }
        }}
        disabled={disabled || submitting}
        rows={3}
        placeholder="告诉我想新增需求、修 bug、或改哪一步 Prompt..."
        className="w-full resize-none rounded-md border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none transition-colors placeholder:text-fg-subtle focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
      />
      {draft && <PromptDraftPreview draft={draft} />}
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 text-xs text-error truncate">{error}</div>
        <div className="flex flex-shrink-0 items-center gap-2">
          {draft && (
            <button
              type="button"
              disabled={disabled || submitting}
              onClick={() => {
                setDraft(null);
                setDraftRequest("");
              }}
              className="rounded-md border border-border px-2.5 py-1.5 text-xs text-fg-muted transition-colors hover:bg-canvas disabled:cursor-not-allowed disabled:opacity-50"
            >
              重新编辑
            </button>
          )}
          <button
            type={draft ? "button" : "submit"}
            onClick={draft ? () => void confirm() : undefined}
            disabled={disabled || submitting || text.trim().length === 0}
            className="inline-flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <Send size={13} />
            )}
            {draft ? "确认并执行" : "生成修改草案"}
          </button>
        </div>
      </div>
    </form>
  );
}

function PromptDraftPreview({ draft }: { draft: WorkflowPromptDraftResponse }) {
  return (
    <section className="rounded-md border border-accent/60 bg-accent/5 p-2 text-xs">
      <div className="mb-2 text-fg">{draft.summary}</div>
      <div className="space-y-2">
        {draft.changes.map((change) => (
          <PromptChangePreview
            key={`${change.step_id}-${change.field}`}
            change={change}
          />
        ))}
      </div>
    </section>
  );
}

function PromptChangePreview({ change }: { change: WorkflowPromptChange }) {
  return (
    <details className="rounded border border-border/70 bg-canvas/70 p-2">
      <summary className="cursor-pointer text-fg-muted">
        {change.step_name} · {formatPromptField(change.field)}
      </summary>
      <div className="mt-2 text-fg-subtle">{change.reason}</div>
      <div className="mt-2 space-y-2">
        <PromptTextBlock label="修改前" text={change.old_text} />
        <PromptTextBlock label="修改后" text={change.new_text} />
      </div>
    </details>
  );
}

function PromptTextBlock({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <div className="mb-1 text-fg-subtle">{label}</div>
      <pre className="max-h-28 overflow-y-auto whitespace-pre-wrap rounded bg-surface px-2 py-1 font-mono text-[11px] leading-relaxed text-fg-muted">
        {text || "（空）"}
      </pre>
    </div>
  );
}

function formatPromptField(field: WorkflowPromptChange["field"]): string {
  return field === "system_prompt" ? "System Prompt" : "User Prompt";
}

function formatRunOption(run: Run): string {
  const date = new Date(run.started_at);
  const time = Number.isNaN(date.getTime())
    ? run.started_at
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return `${time} · ${run.status}`;
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
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) setExpanded(defaultExpanded);
    });
    return () => {
      cancelled = true;
    };
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
            {formatStepTitle(step.step_order, step.name)}
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

function FinalReplyText({
  reply,
  isFinalizing,
}: {
  reply?: string;
  isFinalizing: boolean;
}) {
  return (
    <section className="mt-4 border-t border-border/60 pt-3">
      <div className="text-xs uppercase tracking-wider text-fg-subtle">
        回复用户
      </div>
      <div className="prose-msg mt-2 max-h-80 overflow-y-auto text-sm leading-relaxed text-fg-muted">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {reply || "正在整理最终回复..."}
        </ReactMarkdown>
        {isFinalizing && (
          <span className="inline-block w-1.5 h-3 bg-accent animate-pulse ml-1" />
        )}
      </div>
    </section>
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
