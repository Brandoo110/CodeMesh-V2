"use client";

/**
 * WorkflowsView 左栏 240px：工作流列表 + 新建按钮。
 *
 * 设计要点：
 * - 模板和用户工作流混排，但模板顶端有 "📦 模板" 标签
 * - 选中项左侧加 2px accent 边（复用 Sidebar 的高亮样式）
 * - hover trash 删除（模板禁用）
 * - 顶部 "+ 新建" 按钮新建一个空工作流
 */

import { useState } from "react";
import { Plus, Trash2, Sparkles } from "lucide-react";
import { useStore } from "@/lib/store";
import { createWorkflow, deleteWorkflow } from "@/lib/workflow-api";
import type { Workflow } from "@/lib/workflow-types";

interface Props {
  workflows: Workflow[];
  onRefresh: () => Promise<void>;
}

export function WorkflowList({ workflows, onRefresh }: Props) {
  const currentId = useStore((s) => s.currentWorkflowId);
  const setCurrent = useStore((s) => s.setCurrentWorkflowId);
  const [creating, setCreating] = useState(false);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const wf = await createWorkflow({
        name: "新工作流",
        description: "",
      });
      await onRefresh();
      setCurrent(wf.id);
    } catch (e) {
      console.error("Failed to create workflow:", e);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (e: React.MouseEvent, wf: Workflow) => {
    e.stopPropagation();
    if (wf.is_template) return;
    if (!confirm(`确定删除"${wf.name}"？所有执行历史也会被清除。`)) return;
    try {
      await deleteWorkflow(wf.id);
      if (currentId === wf.id) setCurrent(null);
      await onRefresh();
    } catch (err) {
      console.error("Failed to delete workflow:", err);
    }
  };

  // 拆开模板和用户工作流（后端已经把模板排在前，这里只是分组渲染）
  const templates = workflows.filter((w) => w.is_template);
  const userWorkflows = workflows.filter((w) => !w.is_template);

  return (
    <aside className="w-60 flex-shrink-0 border-r border-border bg-surface flex flex-col">
      {/* 顶部：新建 */}
      <div className="p-3 border-b border-border">
        <button
          onClick={handleCreate}
          disabled={creating}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-md bg-accent hover:bg-accent-hover text-white text-sm font-medium transition-colors disabled:opacity-50"
        >
          <Plus size={14} />
          新建工作流
        </button>
      </div>

      {/* 列表 */}
      <div className="flex-1 overflow-y-auto py-2">
        {templates.length > 0 && (
          <>
            <div className="px-3 py-1 text-xs text-fg-subtle uppercase tracking-wider flex items-center gap-1">
              <Sparkles size={10} />
              内置模板
            </div>
            {templates.map((wf) => (
              <WorkflowItem
                key={wf.id}
                wf={wf}
                isActive={wf.id === currentId}
                onClick={() => setCurrent(wf.id)}
                onDelete={(e) => handleDelete(e, wf)}
              />
            ))}
          </>
        )}

        {userWorkflows.length > 0 && (
          <>
            <div className="px-3 py-1 mt-2 text-xs text-fg-subtle uppercase tracking-wider">
              我的工作流
            </div>
            {userWorkflows.map((wf) => (
              <WorkflowItem
                key={wf.id}
                wf={wf}
                isActive={wf.id === currentId}
                onClick={() => setCurrent(wf.id)}
                onDelete={(e) => handleDelete(e, wf)}
              />
            ))}
          </>
        )}

        {workflows.length === 0 && (
          <div className="px-3 py-8 text-xs text-fg-subtle text-center">
            还没有工作流
            <br />
            点击上方新建一个
          </div>
        )}
      </div>
    </aside>
  );
}

// ─────────────── 列表行 ───────────────

interface ItemProps {
  wf: Workflow;
  isActive: boolean;
  onClick: () => void;
  onDelete: (e: React.MouseEvent) => void;
}

function WorkflowItem({ wf, isActive, onClick, onDelete }: ItemProps) {
  return (
    <div
      onClick={onClick}
      className={`group px-3 py-2 cursor-pointer flex items-center justify-between border-l-2 transition-colors ${
        isActive
          ? "bg-canvas border-l-accent"
          : "border-l-transparent hover:bg-canvas/50"
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-sm text-fg truncate">{wf.name}</div>
        <div className="text-xs text-fg-subtle">
          {wf.step_count} 步
          {wf.is_template && <span className="ml-2 text-accent">· 模板</span>}
        </div>
      </div>
      {!wf.is_template && (
        <button
          onClick={onDelete}
          className="opacity-0 group-hover:opacity-100 p-1 text-fg-subtle hover:text-error transition-opacity"
          title="删除"
        >
          <Trash2 size={12} />
        </button>
      )}
    </div>
  );
}
