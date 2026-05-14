"use client";

/**
 * 工作流编辑器中栏（v5 Phase 6.3）。
 *
 * 功能：
 * - workflow 名 / 描述：inline 编辑（blur 保存）
 * - 模板：只读 + fork 按钮
 * - step 卡片列表：每个独立 PUT，blur 保存
 * - 添加步骤 / 删除 / 重排序（拖拽留 v5.1，MVP 上下箭头）
 *
 * 数据流：
 * - 父组件传 detail 是 source of truth
 * - 任何编辑操作都先调后端，成功后 onChange 让父组件重新拉 detail
 */

import { useEffect, useState } from "react";
import { Plus, GitFork, ArrowUp, ArrowDown, Play } from "lucide-react";
import {
  addStep,
  deleteStep,
  forkWorkflow,
  reorderSteps,
  updateStep,
  updateWorkflow,
} from "@/lib/workflow-api";
import type { Step, WorkflowDetail } from "@/lib/workflow-types";
import { useStore } from "@/lib/store";
import { StepCard } from "./StepCard";

interface Props {
  detail: WorkflowDetail;
  /** 任何后端写操作完成后回调，父组件应该重新拉 detail。 */
  onChange: () => Promise<void> | void;
  /** 触发执行整个工作流（Phase 6.5）。 */
  onRun?: () => Promise<void> | void;
  isRunning?: boolean;
}

export function WorkflowEditor({ detail, onChange, onRun, isRunning }: Props) {
  const setCurrentWorkflowId = useStore((s) => s.setCurrentWorkflowId);
  const [name, setName] = useState(detail.name);
  const [description, setDescription] = useState(detail.description);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setName(detail.name);
    setDescription(detail.description);
  }, [detail.id, detail.name, detail.description]);

  const readOnly = detail.is_template;

  // ───── workflow 元数据 ─────

  const commitName = async () => {
    if (readOnly || name === detail.name) return;
    await updateWorkflow(detail.id, { name });
    await onChange();
  };

  const commitDescription = async () => {
    if (readOnly || description === detail.description) return;
    await updateWorkflow(detail.id, { description });
    await onChange();
  };

  // ───── steps ─────

  const handleAddStep = async () => {
    setBusy(true);
    try {
      await addStep(detail.id, {
        name: `Step ${detail.steps.length + 1}`,
        model: null,
        system_prompt: "",
        user_prompt: "",
        enable_tools: ["*"],
      });
      await onChange();
    } finally {
      setBusy(false);
    }
  };

  const handleStepUpdate = async (sid: string, patch: Partial<Step>) => {
    await updateStep(detail.id, sid, patch);
    await onChange();
  };

  const handleStepDelete = async (sid: string) => {
    if (!confirm("删除这个步骤？")) return;
    await deleteStep(detail.id, sid);
    await onChange();
  };

  // 上下移动：用 reorderSteps API 一次性提交新顺序
  const move = async (idx: number, dir: -1 | 1) => {
    const target = idx + dir;
    if (target < 0 || target >= detail.steps.length) return;
    const newIds = detail.steps.map((s) => s.id);
    [newIds[idx], newIds[target]] = [newIds[target], newIds[idx]];
    await reorderSteps(detail.id, newIds);
    await onChange();
  };

  // ───── fork（模板专属） ─────

  const handleFork = async () => {
    setBusy(true);
    try {
      const forked = await forkWorkflow(detail.id);
      await onChange();
      setCurrentWorkflowId(forked.id);  // 切到副本
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-4">
      {/* 工作流元数据 */}
      <header className="space-y-2">
        <div className="flex items-center gap-3">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={commitName}
            disabled={readOnly}
            placeholder="工作流名"
            className="flex-1 bg-transparent text-xl font-medium text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
          />

          {readOnly ? (
            <button
              onClick={handleFork}
              disabled={busy}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent hover:bg-accent-hover text-white text-sm font-medium transition-colors disabled:opacity-50"
              title="复制为可编辑副本"
            >
              <GitFork size={14} />
              Fork
            </button>
          ) : (
            <button
              onClick={() => onRun?.()}
              disabled={
                isRunning ||
                detail.steps.length === 0 ||
                !onRun
              }
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent hover:bg-accent-hover text-white text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title={
                detail.steps.length === 0
                  ? "至少添加 1 个步骤"
                  : isRunning
                    ? "正在执行..."
                    : "执行整个工作流"
              }
            >
              <Play size={14} />
              {isRunning ? "执行中" : "执行"}
            </button>
          )}
        </div>

        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          onBlur={commitDescription}
          disabled={readOnly}
          placeholder="一句话描述这个工作流要做什么"
          className="w-full bg-transparent text-sm text-fg-muted outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />

        {readOnly && (
          <div className="text-xs text-accent">
            📦 内置模板（只读）—— 点击 Fork 创建可编辑副本
          </div>
        )}
      </header>

      {/* 步骤列表 */}
      <section className="space-y-3">
        {detail.steps.length === 0 ? (
          <div className="border border-dashed border-border rounded-lg p-12 text-center text-fg-subtle">
            <div className="text-sm mb-2">这个工作流还没有步骤</div>
            {!readOnly && (
              <div className="text-xs">点击下方"添加步骤"开始</div>
            )}
          </div>
        ) : (
          detail.steps.map((step, idx) => (
            <div key={step.id} className="relative">
              {/* 上下移动按钮 */}
              {!readOnly && (
                <div className="absolute -left-8 top-3 flex flex-col gap-1">
                  <button
                    onClick={() => move(idx, -1)}
                    disabled={idx === 0}
                    className="p-1 text-fg-subtle hover:text-fg transition-colors disabled:opacity-30"
                    title="上移"
                  >
                    <ArrowUp size={12} />
                  </button>
                  <button
                    onClick={() => move(idx, 1)}
                    disabled={idx === detail.steps.length - 1}
                    className="p-1 text-fg-subtle hover:text-fg transition-colors disabled:opacity-30"
                    title="下移"
                  >
                    <ArrowDown size={12} />
                  </button>
                </div>
              )}

              <StepCard
                step={step}
                readOnly={readOnly}
                onUpdate={(patch) => handleStepUpdate(step.id, patch)}
                onDelete={() => handleStepDelete(step.id)}
              />

              {/* 步骤连接线（最后一个不画） */}
              {idx < detail.steps.length - 1 && (
                <div className="flex justify-center py-1">
                  <div className="w-px h-4 bg-border" />
                </div>
              )}
            </div>
          ))
        )}

        {/* 添加步骤 */}
        {!readOnly && (
          <button
            onClick={handleAddStep}
            disabled={busy}
            className="w-full flex items-center justify-center gap-2 py-3 rounded-lg border border-dashed border-border hover:border-accent text-sm text-fg-muted hover:text-accent transition-colors disabled:opacity-50"
          >
            <Plus size={14} />
            添加步骤
          </button>
        )}
      </section>
    </div>
  );
}
