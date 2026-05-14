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
import { WorkflowEditor } from "./WorkflowEditor";

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

  const reloadDetail = useCallback(async () => {
    if (!currentId) return;
    try {
      const d = await getWorkflow(currentId);
      setDetail(d);
      // workflow 元数据可能变了（name / step_count），同步列表
      await refresh();
    } catch (e) {
      console.error("Failed to reload workflow detail:", e);
    }
  }, [currentId, refresh]);

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

      {/* 中：编辑器 */}
      <main className="flex-1 overflow-y-auto bg-canvas">
        {!currentId ? (
          <EmptyState />
        ) : loadingDetail ? (
          <LoadingState />
        ) : detail ? (
          <WorkflowEditor detail={detail} onChange={reloadDetail} />
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
