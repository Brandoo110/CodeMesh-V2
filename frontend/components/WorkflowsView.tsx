"use client";

/**
 * v5 WorkflowsView 三栏主容器（Phase 6.2 骨架）。
 *
 * 布局：
 *   ┌──────────────┬─────────────────────────────┬──────────────┐
 *   │ WorkflowList │     WorkflowEditor          │ WorkflowRun  │
 *   │   240px      │     flex 1                  │   320px      │
 *   └──────────────┴─────────────────────────────┴──────────────┘
 *
 * Phase 6.2：WorkflowList 接通 + 中间编辑器空占位 + 右栏占位
 * Phase 6.3：Editor + StepCard 真正实现
 * Phase 6.5：右栏 RunPanel + DiffViewer
 *
 * 数据加载：挂载时 listWorkflows() 填充 store；新建/删除后 refresh。
 */

import { useCallback, useEffect, useState } from "react";
import { useStore } from "@/lib/store";
import { listWorkflows, getWorkflow } from "@/lib/workflow-api";
import type { WorkflowDetail } from "@/lib/workflow-types";
import { WorkflowList } from "./WorkflowList";

export function WorkflowsView() {
  const workflows = useStore((s) => s.workflows);
  const setWorkflows = useStore((s) => s.setWorkflows);
  const currentId = useStore((s) => s.currentWorkflowId);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const rows = await listWorkflows();
      setWorkflows(rows);
    } catch (e) {
      console.error("Failed to list workflows:", e);
    }
  }, [setWorkflows]);

  // 挂载时拉列表
  useEffect(() => {
    refresh();
  }, [refresh]);

  // 切换当前工作流时拉详情（含 steps）
  useEffect(() => {
    if (!currentId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
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
    return () => {
      cancelled = true;
    };
  }, [currentId]);

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* 左：列表 */}
      <WorkflowList workflows={workflows} onRefresh={refresh} />

      {/* 中：编辑器（Phase 6.3 实现） */}
      <main className="flex-1 overflow-y-auto bg-canvas">
        {!currentId ? (
          <EmptyState />
        ) : loadingDetail ? (
          <LoadingState />
        ) : detail ? (
          <EditorPlaceholder detail={detail} />
        ) : (
          <div className="p-8 text-fg-subtle">工作流不存在</div>
        )}
      </main>

      {/* 右：运行日志（Phase 6.5 实现） */}
      <aside className="w-80 flex-shrink-0 border-l border-border bg-surface p-4">
        <div className="text-xs text-fg-subtle uppercase tracking-wider mb-2">
          运行日志
        </div>
        <div className="text-sm text-fg-subtle">
          {currentId
            ? "点击工作流右上角的 ▶ 执行，这里会显示实时进度。"
            : "选择一个工作流开始。"}
        </div>
      </aside>
    </div>
  );
}

// ─────────────── 占位组件（Phase 6.3 替换） ───────────────

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

function EditorPlaceholder({ detail }: { detail: WorkflowDetail }) {
  return (
    <div className="p-8 max-w-3xl mx-auto">
      <header className="mb-6">
        <h2 className="text-xl font-medium text-fg mb-1">{detail.name}</h2>
        {detail.description && (
          <p className="text-sm text-fg-muted">{detail.description}</p>
        )}
        {detail.is_template && (
          <div className="mt-2 text-xs text-accent">
            内置模板（只读）—— 点击 fork 按钮创建可编辑副本
          </div>
        )}
      </header>

      {detail.steps.length === 0 ? (
        <div className="border border-dashed border-border rounded-lg p-12 text-center text-fg-subtle">
          <div className="text-sm mb-2">还没有步骤</div>
          <div className="text-xs">Phase 6.3 起，这里可以添加并编辑步骤</div>
        </div>
      ) : (
        <div className="space-y-3">
          {detail.steps.map((s) => (
            <div
              key={s.id}
              className="border border-border rounded-lg p-4 bg-surface"
            >
              <div className="flex items-center justify-between mb-2">
                <div className="text-sm font-medium text-fg">
                  Step {s.step_order}: {s.name}
                </div>
                <div className="text-xs text-fg-subtle">
                  {s.model || "未指定模型"}
                </div>
              </div>
              {s.system_prompt && (
                <div className="text-xs text-fg-muted line-clamp-2">
                  {s.system_prompt}
                </div>
              )}
              <div className="mt-2 text-xs text-fg-subtle">
                Tools:{" "}
                {s.enable_tools.includes("*")
                  ? "全开"
                  : s.enable_tools.length === 0
                    ? "全禁"
                    : s.enable_tools.join(" / ")}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="mt-8 text-xs text-fg-subtle text-center">
        Phase 6.3 加 StepCard 编辑器 + 添加步骤按钮 + 工具白名单 UI
      </div>
    </div>
  );
}
