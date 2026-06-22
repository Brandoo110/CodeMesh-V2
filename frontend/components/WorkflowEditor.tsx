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

import { useRef, useState } from "react";
import type { FocusEvent, MouseEvent } from "react";
import { Plus, GitFork, ArrowUp, ArrowDown, Play } from "lucide-react";
import {
  addStep,
  deleteStep,
  forkWorkflow,
  reorderSteps,
  updateStep,
  updateWorkflow,
} from "@/lib/workflow-api";
import { formatApiErrorDetail } from "@/lib/api";
import type { Step, WorkflowDetail } from "@/lib/workflow-types";
import { useStore } from "@/lib/store";
import { StepCard } from "./StepCard";

interface Props {
  detail: WorkflowDetail;
  /** 任何后端写操作完成后回调，父组件应该重新拉 detail。 */
  onChange: () => Promise<void> | void;
  /** 触发执行整个工作流（Phase 6.5）。 */
  onRun?: () => Promise<void> | void;
  /** 触发单步执行（Phase 6.8）。 */
  onRunStep?: (stepId: string) => Promise<void> | void;
  isRunning?: boolean;
  /** 当前正在跑的 step id（单步或整体都可能填）。 */
  runningStepId?: string | null;
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : formatApiErrorDetail(error);
}

type MetaField = "name" | "description";

export function WorkflowEditor({
  detail,
  onChange,
  onRun,
  onRunStep,
  isRunning,
  runningStepId,
}: Props) {
  const setCurrentWorkflowId = useStore((s) => s.setCurrentWorkflowId);
  const [name, setName] = useState(detail.name);
  const [description, setDescription] = useState(detail.description);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const autoSelectedFields = useRef<Set<string>>(new Set());
  const mouseUpSelectField = useRef<string | null>(null);

  const readOnly = detail.is_template;

  // ───── workflow 元数据 ─────

  const selectMetaFieldOnce = (
    field: MetaField,
    event: FocusEvent<HTMLInputElement>,
  ) => {
    if (readOnly) return;
    const key = `${detail.id}:${field}`;
    if (autoSelectedFields.current.has(key)) return;

    const input = event.currentTarget;
    autoSelectedFields.current.add(key);
    mouseUpSelectField.current = key;
    requestAnimationFrame(() => input.select());
  };

  const keepAutoSelectionOnMouseUp = (
    field: MetaField,
    event: MouseEvent<HTMLInputElement>,
  ) => {
    const key = `${detail.id}:${field}`;
    if (mouseUpSelectField.current !== key) return;

    event.preventDefault();
    mouseUpSelectField.current = null;
  };

  const commitName = async () => {
    if (readOnly || name === detail.name) return;
    setSaveError(null);
    try {
      await updateWorkflow(detail.id, { name });
      await onChange();
    } catch (error) {
      console.error("Failed to update workflow name:", error);
      setName(detail.name);
      setSaveError(`保存工作流名失败：${describeError(error)}`);
    }
  };

  const commitDescription = async () => {
    if (readOnly || description === detail.description) return;
    setSaveError(null);
    try {
      await updateWorkflow(detail.id, { description });
      await onChange();
    } catch (error) {
      console.error("Failed to update workflow description:", error);
      setDescription(detail.description);
      setSaveError(`保存工作流描述失败：${describeError(error)}`);
    }
  };

  // ───── steps ─────

  const handleAddStep = async () => {
    setBusy(true);
    setSaveError(null);
    try {
      await addStep(detail.id, {
        name: `Step ${detail.steps.length + 1}`,
        model: null,
        system_prompt: "",
        user_prompt: "",
        enable_tools: ["*"],
      });
      await onChange();
    } catch (error) {
      console.error("Failed to add workflow step:", error);
      setSaveError(`添加步骤失败：${describeError(error)}`);
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
    setSaveError(null);
    try {
      await reorderSteps(detail.id, newIds);
      await onChange();
    } catch (error) {
      console.error("Failed to reorder workflow steps:", error);
      setSaveError(`调整步骤顺序失败：${describeError(error)}`);
    }
  };

  // ───── fork（模板专属） ─────

  const handleFork = async () => {
    setBusy(true);
    setSaveError(null);
    try {
      const forked = await forkWorkflow(detail.id);
      await onChange();
      setCurrentWorkflowId(forked.id);  // 切到副本
    } catch (error) {
      console.error("Failed to fork workflow:", error);
      setSaveError(`Fork 失败：${describeError(error)}`);
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
            onFocus={(e) => selectMetaFieldOnce("name", e)}
            onMouseUp={(e) => keepAutoSelectionOnMouseUp("name", e)}
            onBlur={() => {
              void commitName();
            }}
            disabled={readOnly}
            placeholder="工作流名"
            className="flex-1 bg-transparent text-xl font-medium text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
          />

          {readOnly ? (
            <button
              onClick={() => {
                void handleFork();
              }}
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
          onFocus={(e) => selectMetaFieldOnce("description", e)}
          onMouseUp={(e) => keepAutoSelectionOnMouseUp("description", e)}
          onBlur={() => {
            void commitDescription();
          }}
          disabled={readOnly}
          placeholder="一句话描述这个工作流要做什么"
          className="w-full bg-transparent text-sm text-fg-muted outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />

        {saveError && (
          <div className="rounded-md bg-error/10 px-3 py-2 text-xs text-error">
            {saveError}
          </div>
        )}

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
              <div className="text-xs">点击下方「添加步骤」开始</div>
            )}
          </div>
        ) : (
          detail.steps.map((step, idx) => (
            <div key={step.id} className="relative">
              {/* 上下移动按钮 */}
              {!readOnly && (
                <div className="absolute -left-8 top-3 flex flex-col gap-1">
                  <button
                    onClick={() => {
                      void move(idx, -1);
                    }}
                    disabled={idx === 0}
                    className="p-1 text-fg-subtle hover:text-fg transition-colors disabled:opacity-30"
                    title="上移"
                  >
                    <ArrowUp size={12} />
                  </button>
                  <button
                    onClick={() => {
                      void move(idx, 1);
                    }}
                    disabled={idx === detail.steps.length - 1}
                    className="p-1 text-fg-subtle hover:text-fg transition-colors disabled:opacity-30"
                    title="下移"
                  >
                    <ArrowDown size={12} />
                  </button>
                </div>
              )}

              <StepCard
                key={`${step.id}:${step.name}:${step.system_prompt}:${step.user_prompt}`}
                step={step}
                readOnly={readOnly}
                onUpdate={(patch) => handleStepUpdate(step.id, patch)}
                onDelete={() => handleStepDelete(step.id)}
                onRunStep={onRunStep ? () => onRunStep(step.id) : undefined}
                isRunningStep={runningStepId === step.id}
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
            onClick={() => {
              void handleAddStep();
            }}
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
