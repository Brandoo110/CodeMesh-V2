"use client";

/**
 * v5 WorkflowsView 三栏主容器（Phase 6.5：接通 SSE 执行）。
 *
 * 布局：
 *   ┌──────────────┬─────────────────────────────┬──────────────┐
 *   │ WorkflowList │     WorkflowEditor          │ WorkflowRun  │
 *   │   240px      │     flex 1                  │ 280-640px    │
 *   └──────────────┴─────────────────────────────┴──────────────┘
 *
 * Phase 6.2：WorkflowList 接通 + 中间编辑器空占位 + 右栏占位
 * Phase 6.3：Editor + StepCard 真正实现
 * Phase 6.5：执行 + SSE 实时高亮 + RunPanel
 * Phase 6.6：DiffViewer 集成（在 RunPanel 内或单独面板）
 *
 * 状态机：
 *   currentRunId: 当前 SSE run 的 id（null = 没在跑）
 *   runStates:    step.id → StepRunState 实时状态
 *   isRunning:    SSE 流尚未 done 之前是 true
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useStore } from "@/lib/store";
import {
  draftWorkflowPromptChanges,
  getRun,
  getWorkflow,
  listRuns,
  listWorkflows,
  updateStep,
} from "@/lib/workflow-api";
import {
  streamWorkflowContinue,
  streamWorkflowRun,
  streamStepRun,
} from "@/lib/workflow-sse";
import { buildRunStatesFromDetail } from "@/lib/workflow-history";
import {
  applyPromptDraftChanges,
  buildWorkflowContinueContext,
  findContinueStartStepId,
} from "@/lib/workflow-continue";
import type {
  Run,
  RunDetail,
  Step,
  WorkflowDetail,
  WorkflowPromptDraftResponse,
} from "@/lib/workflow-types";
import type { StreamEvent } from "@/lib/workflow-sse";
import { WorkflowList } from "./WorkflowList";
import { WorkflowEditor } from "./WorkflowEditor";
import {
  WorkflowRunPanel,
  type ReviewDecisionRunEvent,
  type ReworkRunEvent,
  type StepRunState,
} from "./WorkflowRunPanel";

export function WorkflowsView() {
  const workflows = useStore((s) => s.workflows);
  const setWorkflows = useStore((s) => s.setWorkflows);
  const currentId = useStore((s) => s.currentWorkflowId);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // 执行状态机
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [runStates, setRunStates] = useState<Map<string, StepRunState>>(
    new Map(),
  );
  const [isRunning, setIsRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [totalCost, setTotalCost] = useState(0);
  const [finalReply, setFinalReply] = useState("");
  const [isFinalizingReply, setIsFinalizingReply] = useState(false);
  const [reviewDecisionEvents, setReviewDecisionEvents] = useState<ReviewDecisionRunEvent[]>([]);
  const [reworkEvents, setReworkEvents] = useState<ReworkRunEvent[]>([]);
  const [runHistory, setRunHistory] = useState<Run[]>([]);
  const [loadingRunHistory, setLoadingRunHistory] = useState(false);
  const [loadedHistoryRunId, setLoadedHistoryRunId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await listWorkflows();
      setWorkflows(rows);
    } catch (e) {
      console.error("Failed to list workflows:", e);
    }
  }, [setWorkflows]);

  const reloadDetail = useCallback(async () => {
    if (!currentId) return;
    try {
      const d = await getWorkflow(currentId);
      setDetail(d);
      await refresh();
    } catch (e) {
      console.error("Failed to reload workflow detail:", e);
    }
  }, [currentId, refresh]);

  const reloadRuns = useCallback(async () => {
    if (!currentId) {
      setRunHistory([]);
      return;
    }
    setLoadingRunHistory(true);
    try {
      const rows = await listRuns(currentId, 20);
      setRunHistory(rows);
    } catch (e) {
      console.error("Failed to list workflow runs:", e);
    } finally {
      setLoadingRunHistory(false);
    }
  }, [currentId]);

  // 挂载时拉列表
  useEffect(() => {
    refresh();
  }, [refresh]);

  // 切换当前工作流时拉详情 + 清空运行状态
  useEffect(() => {
    let cancelled = false;
    if (!currentId) {
      queueMicrotask(() => {
        if (cancelled) return;
        setDetail(null);
        setRunStates(new Map());
        setCurrentRunId(null);
        setRunError(null);
        setTotalCost(0);
        setFinalReply("");
        setIsFinalizingReply(false);
        setReviewDecisionEvents([]);
        setReworkEvents([]);
        setRunHistory([]);
        setLoadedHistoryRunId(null);
      });
      return () => {
        cancelled = true;
      };
    }
    queueMicrotask(() => {
      if (cancelled) return;
      setLoadingDetail(true);
      setLoadingRunHistory(true);
      setRunStates(new Map());
      setCurrentRunId(null);
      setRunError(null);
      setTotalCost(0);
      setFinalReply("");
      setIsFinalizingReply(false);
      setReviewDecisionEvents([]);
      setReworkEvents([]);
      setRunHistory([]);
      setLoadedHistoryRunId(null);
    });
    getWorkflow(currentId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        console.error("Failed to load workflow detail:", e);
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });
    listRuns(currentId, 20)
      .then((rows) => {
        if (!cancelled) setRunHistory(rows);
      })
      .catch((e) => {
        console.error("Failed to list workflow runs:", e);
      })
      .finally(() => {
        if (!cancelled) setLoadingRunHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [currentId]);

  // ───── 执行入口 ─────

  /** 公共 SSE 消费器：handleRun 和 handleRunStep 共用。 */
  const consumeEvents = useCallback(
    async (gen: AsyncGenerator<StreamEvent>) => {
      setIsRunning(true);
      setRunError(null);

      try {
        for await (const ev of gen) {
          const data = ev.data as Record<string, unknown>;
          const stepId = data.step_id as string | undefined;

          switch (ev.event) {
            case "run_start":
              setCurrentRunId(data.run_id as string);
              break;

            case "step_start":
              if (stepId) {
                setRunStates((prev) => {
                  const next = new Map(prev);
                  next.set(stepId, {
                    status: "running",
                    output: "",
                    toolCalls: [],
                  });
                  return next;
                });
              }
              break;

            case "token":
              if (stepId) {
                setRunStates((prev) => {
                  const next = new Map(prev);
                  const cur = next.get(stepId);
                  if (cur) {
                    next.set(stepId, {
                      ...cur,
                      output: cur.output + (data.delta as string),
                    });
                  }
                  return next;
                });
              }
              break;

            case "tool_start":
              if (stepId) {
                setRunStates((prev) => {
                  const next = new Map(prev);
                  const cur = next.get(stepId);
                  if (cur) {
                    next.set(stepId, {
                      ...cur,
                      toolCalls: [
                        ...cur.toolCalls,
                        {
                          name: data.name as string,
                          args: data.args as Record<string, unknown> | undefined,
                          status: "pending",
                        },
                      ],
                    });
                  }
                  return next;
                });
              }
              break;

            case "tool_end":
              if (stepId) {
                setRunStates((prev) => {
                  const next = new Map(prev);
                  const cur = next.get(stepId);
                  if (cur) {
                    const updated = [...cur.toolCalls];
                    for (let i = updated.length - 1; i >= 0; i--) {
                      if (
                        updated[i].name === (data.name as string) &&
                        updated[i].status === "pending"
                      ) {
                        updated[i] = {
                          ...updated[i],
                          result: data.result as string,
                          ok: data.ok as boolean,
                          status: data.ok ? "ok" : "error",
                        };
                        break;
                      }
                    }
                    next.set(stepId, { ...cur, toolCalls: updated });
                  }
                  return next;
                });
              }
              break;

            case "diff":
              if (stepId) {
                setRunStates((prev) => {
                  const next = new Map(prev);
                  const cur = next.get(stepId);
                  if (cur) {
                    next.set(stepId, {
                      ...cur,
                      fileDiffs: data.diffs as import("./WorkflowRunPanel").StepRunState["fileDiffs"],
                    });
                  }
                  return next;
                });
              }
              break;

            case "step_end":
              if (stepId) {
                const ok = data.ok as boolean;
                setRunStates((prev) => {
                  const next = new Map(prev);
                  const cur = next.get(stepId);
                  if (cur) {
                    next.set(stepId, {
                      ...cur,
                      status: ok ? "done" : "error",
                      costRmb: data.cost_rmb as number | undefined,
                      durationMs: data.duration_ms as number | undefined,
                      modelUsed: data.model_used as string | undefined,
                      error: data.error as string | undefined,
                    });
                  }
                  return next;
                });
                if (data.cost_rmb) {
                  setTotalCost((c) => c + (data.cost_rmb as number));
                }
              }
              break;

            case "review_decision":
              setReviewDecisionEvents((prev) => [
                ...prev,
                {
                  reviewerStepId: data.reviewer_step_id as string,
                  reviewerName: (data.reviewer_name as string | undefined) || "Reviewer",
                  status: data.status === "needs_rework" ? "needs_rework" : "done",
                  targetStepId: data.target_step_id as string | undefined,
                  targetName: data.target_name as string | undefined,
                  reason: data.reason as string | undefined,
                  model: data.model as string | undefined,
                  costRmb: data.cost_rmb as number | undefined,
                },
              ]);
              if (data.cost_rmb) {
                setTotalCost((c) => c + (data.cost_rmb as number));
              }
              break;

            case "rework_requested":
              setReworkEvents((prev) => [
                ...prev,
                {
                  reviewerStepId: data.reviewer_step_id as string,
                  reviewerName: (data.reviewer_name as string | undefined) || "Reviewer",
                  targetStepId: data.target_step_id as string,
                  targetName: (data.target_name as string | undefined) || "Coder",
                  reason: data.reason as string | undefined,
                },
              ]);
              break;

            case "final_start":
              setIsFinalizingReply(true);
              setFinalReply("");
              break;

            case "final_end":
              setIsFinalizingReply(false);
              setFinalReply((data.reply as string | undefined) || "");
              if (data.cost_rmb) {
                setTotalCost((c) => c + (data.cost_rmb as number));
              }
              break;

            case "done":
              setIsRunning(false);
              setIsFinalizingReply(false);
              if (typeof data.total_cost === "number") {
                setTotalCost(data.total_cost);
              }
              if (typeof data.final_reply === "string") {
                setFinalReply(data.final_reply);
              }
              if (!data.ok) {
                setRunError((data.error as string) || "执行失败");
              }
              reloadDetail();
              reloadRuns();
              break;

            case "error":
              setRunError(data.message as string);
              setIsRunning(false);
              setIsFinalizingReply(false);
              break;

            case "cancelled":
              setIsRunning(false);
              setIsFinalizingReply(false);
              break;
          }
        }
      } catch (e) {
        setRunError((e as Error).message);
      } finally {
        setIsRunning(false);
      }
    },
    [reloadDetail, reloadRuns],
  );

  /** 整体工作流执行：所有 steps 重置为 pending 再开始。 */
  const handleRun = useCallback(async () => {
    if (!detail || detail.steps.length === 0) return;
    const fresh = new Map<string, StepRunState>();
    detail.steps.forEach((s: Step) =>
      fresh.set(s.id, {
        status: "pending",
        output: "",
        toolCalls: [],
      }),
    );
    setRunStates(fresh);
    setTotalCost(0);
    setFinalReply("");
    setIsFinalizingReply(false);
    setReviewDecisionEvents([]);
    setReworkEvents([]);
    await consumeEvents(streamWorkflowRun(detail.id));
  }, [detail, consumeEvents]);

  /** 单步执行：只重置该 step 的状态，其他保留（让用户看历史输出对比）。 */
  const handleRunStep = useCallback(
    async (stepId: string) => {
      if (!detail) return;
      // 找前一步的 output 作为 seed input
      const idx = detail.steps.findIndex((s: Step) => s.id === stepId);
      const seedInput = idx > 0
        ? runStates.get(detail.steps[idx - 1].id)?.output || ""
        : "";

      setRunStates((prev) => {
        const next = new Map(prev);
        next.set(stepId, {
          status: "running",
          output: "",
          toolCalls: [],
        });
        return next;
      });
      setFinalReply("");
      setIsFinalizingReply(false);
      setReviewDecisionEvents([]);
      setReworkEvents([]);

      await consumeEvents(
        streamStepRun(detail.id, stepId, seedInput || undefined),
      );
    },
    [detail, runStates, consumeEvents],
  );

  const handleDraftContinue = useCallback(
    async (userRequest: string) => {
      if (!detail) return;
      const runContext = buildWorkflowContinueContext(
        detail.steps,
        runStates,
        finalReply,
      );

      return draftWorkflowPromptChanges(detail.id, {
        user_request: userRequest,
        run_context: runContext,
      });
    },
    [detail, runStates, finalReply],
  );

  const handleConfirmContinue = useCallback(
    async (draft: WorkflowPromptDraftResponse, userRequest: string) => {
      if (!detail) return;

      for (const change of draft.changes) {
        await updateStep(detail.id, change.step_id, {
          [change.field]: change.new_text,
        });
      }

      const patchedSteps = applyPromptDraftChanges(detail.steps, draft);
      const patchedDetail = { ...detail, steps: patchedSteps };
      setDetail(patchedDetail);
      await refresh();

      const startStepId = draft.start_step_id || findContinueStartStepId(patchedSteps);
      if (!startStepId) return;
      const startIdx = patchedSteps.findIndex((s: Step) => s.id === startStepId);
      if (startIdx < 0) return;
      const runContext = buildWorkflowContinueContext(
        patchedSteps,
        runStates,
        finalReply,
      );

      setRunStates((prev) => {
        const next = new Map(prev);
        patchedSteps.slice(startIdx).forEach((s: Step) => {
          next.set(s.id, {
            status: "pending",
            output: "",
            toolCalls: [],
          });
        });
        return next;
      });
      setTotalCost(0);
      setFinalReply("");
      setIsFinalizingReply(false);
      setReviewDecisionEvents([]);
      setReworkEvents([]);

      await consumeEvents(streamWorkflowContinue(detail.id, {
        user_request: userRequest,
        run_context: runContext,
        start_step_id: startStepId,
      }));
    },
    [detail, runStates, finalReply, consumeEvents, refresh],
  );

  const handleLoadRun = useCallback(
    async (runId: string) => {
      if (!runId) return;
      try {
        const run = await getRun(runId);
        setCurrentRunId(run.id);
        setRunStates(buildRunStatesFromDetail(run));
        setIsRunning(false);
        setIsFinalizingReply(false);
        setTotalCost(run.total_cost_rmb);
        setRunError(run.error);
        setFinalReply(run.final_reply || "");
        setReviewDecisionEvents(buildHistoricalReviewDecisionEvents(run, detail?.steps ?? []));
        setReworkEvents(buildHistoricalReworkEvents(run, detail?.steps ?? []));
        setLoadedHistoryRunId(run.id);
      } catch (e) {
        setRunError((e as Error).message);
      }
    },
    [detail],
  );

  const handleStopLocal = useCallback(() => {
    setIsRunning(false);
  }, []);

  /** 当前正在执行的 step id（任何模式都用同一字段）。 */
  const runningStepId = useMemo(() => {
    for (const [sid, state] of runStates) {
      if (state.status === "running") return sid;
    }
    return null;
  }, [runStates]);

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* 左：列表 */}
      <WorkflowList workflows={workflows} onRefresh={refresh} />

      {/* 中：编辑器 */}
      <main className="min-w-0 flex-1 overflow-y-auto bg-canvas">
        {!currentId ? (
          <EmptyState />
        ) : loadingDetail ? (
          <LoadingState />
        ) : detail ? (
          <WorkflowEditor
            key={`${detail.id}:${detail.name}:${detail.description}`}
            detail={detail}
            onChange={reloadDetail}
            onRun={handleRun}
            onRunStep={handleRunStep}
            isRunning={isRunning}
            runningStepId={runningStepId}
          />
        ) : (
          <div className="p-8 text-fg-subtle">工作流不存在</div>
        )}
      </main>

      {/* 右：运行日志 */}
      <WorkflowRunPanel
        steps={detail?.steps ?? []}
        runStates={runStates}
        reviewDecisionEvents={reviewDecisionEvents}
        reworkEvents={reworkEvents}
        isRunning={isRunning}
        runId={currentRunId}
        totalCost={totalCost}
        runError={runError}
        finalReply={finalReply}
        isFinalizingReply={isFinalizingReply}
        runHistory={runHistory}
        loadingRunHistory={loadingRunHistory}
        loadedHistoryRunId={loadedHistoryRunId}
        onStop={handleStopLocal}
        onPrepareContinue={handleDraftContinue}
        onConfirmContinue={handleConfirmContinue}
        onLoadRun={handleLoadRun}
      />
    </div>
  );
}

function buildHistoricalReworkEvents(
  run: RunDetail,
  steps: Step[],
): ReworkRunEvent[] {
  const stepById = new Map(steps.map((step) => [step.id, step]));
  const events: ReworkRunEvent[] = [];

  for (let idx = 1; idx < run.step_results.length; idx += 1) {
    const prev = run.step_results[idx - 1];
    const cur = run.step_results[idx];
    if (cur.step_order >= prev.step_order) continue;

    const reviewer = stepById.get(prev.step_id);
    const target = stepById.get(cur.step_id);
    events.push({
      reviewerStepId: prev.step_id,
      reviewerName: reviewer?.name || `Step ${prev.step_order}`,
      targetStepId: cur.step_id,
      targetName: target?.name || `Step ${cur.step_order}`,
      reason: summarizeReworkReason(prev.output || ""),
    });
  }

  return events;
}

function buildHistoricalReviewDecisionEvents(
  run: RunDetail,
  steps: Step[],
): ReviewDecisionRunEvent[] {
  const stepById = new Map(steps.map((step) => [step.id, step]));
  const events: ReviewDecisionRunEvent[] = [];

  for (let idx = 0; idx < run.step_results.length; idx += 1) {
    const result = run.step_results[idx];
    const step = stepById.get(result.step_id);
    const isReviewer = step?.name.toLowerCase().includes("reviewer")
      || step?.name.toLowerCase().includes("review")
      || step?.name.includes("审查");
    if (!isReviewer) continue;

    const next = run.step_results[idx + 1];
    const loopsBack = Boolean(next && next.step_order < result.step_order);
    const target = loopsBack ? stepById.get(next.step_id) : undefined;
    events.push({
      reviewerStepId: result.step_id,
      reviewerName: step?.name || `Step ${result.step_order}`,
      status: loopsBack ? "needs_rework" : "done",
      targetStepId: target?.id,
      targetName: target?.name,
      reason: loopsBack
        ? summarizeReworkReason(result.output || "")
        : "历史运行未保存隐藏决策详情，按步骤顺序推断为通过。",
    });
  }

  return events;
}

function summarizeReworkReason(output: string): string {
  const lines = output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const issueLines = lines.filter((line) =>
    ["⚠", "缺失", "缺少", "仍需", "需补", "需要补", "返工"].some((marker) =>
      line.includes(marker),
    ),
  );
  return (issueLines.length > 0 ? issueLines : lines).slice(0, 3).join("\n");
}

// ─────────────── 占位组件 ───────────────

function EmptyState() {
  return (
    <div className="h-full flex flex-col items-center justify-center text-fg-muted">
      <div className="text-2xl mb-2">🔗</div>
      <div className="text-base mb-1">多模型工作流</div>
      <div className="text-sm text-fg-subtle">
        左侧选择一个内置模板，或新建你自己的工作流。
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="p-8 text-fg-subtle text-sm">加载中...</div>
  );
}
