"use client";

/**
 * 单步骤卡片（v5 Phase 6.3）。
 *
 * 设计要点：
 * - 顶部：步骤序号 + 名字（inline 编辑）+ 模型选择 + 删除按钮
 * - System / User Prompt：auto-grow textarea，blur 时保存
 * - 工具白名单：8 工具勾选 + 3 预设 + 禁用
 * - Phase 6.5+ 加：单独运行按钮 + 上次输出展开
 *
 * 状态管理策略：
 * - 局部 useState 维护编辑中的值（受控）
 * - blur / dropdown 关闭时 → onUpdate（PUT 后端）
 * - 父组件传 step 是 source of truth，refresh 后会覆盖局部
 */

import { useState } from "react";
import { Trash2, ChevronDown, Play } from "lucide-react";
import { useStore } from "@/lib/store";
import { formatApiErrorDetail } from "@/lib/api";
import { getStepOrderPrefix } from "@/lib/workflow-display";
import type { Step } from "@/lib/workflow-types";
import { ToolAllowlistEditor } from "./ToolAllowlistEditor";

interface Props {
  step: Step;
  readOnly?: boolean;
  onUpdate: (patch: Partial<Step>) => Promise<void> | void;
  onDelete: () => Promise<void> | void;
  /** v5 Phase 6.8：单独运行这一步。父组件接 streamStepRun。 */
  onRunStep?: () => Promise<void> | void;
  isRunningStep?: boolean;
}

type EditableStepPatch = Pick<
  Step,
  "name" | "model" | "system_prompt" | "user_prompt" | "enable_tools"
>;

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : formatApiErrorDetail(error);
}

export function StepCard({ step, readOnly, onUpdate, onDelete, onRunStep, isRunningStep }: Props) {
  const [name, setName] = useState(step.name);
  const [systemPrompt, setSystemPrompt] = useState(step.system_prompt);
  const [userPrompt, setUserPrompt] = useState(step.user_prompt);
  const [saveError, setSaveError] = useState<string | null>(null);

  const resetLocalField = (field: keyof EditableStepPatch) => {
    if (field === "name") setName(step.name);
    if (field === "system_prompt") setSystemPrompt(step.system_prompt);
    if (field === "user_prompt") setUserPrompt(step.user_prompt);
  };

  // 字段失焦保存（避免每次按键都 PUT）
  const commit = async <K extends keyof EditableStepPatch>(
    field: K,
    value: EditableStepPatch[K],
  ) => {
    if (readOnly) return;
    // 与原值相同就跳过
    if (step[field] === value) return;
    setSaveError(null);
    try {
      await onUpdate({ [field]: value });
    } catch (error) {
      console.error(`Failed to update step ${field}:`, error);
      resetLocalField(field);
      setSaveError(`保存步骤失败：${describeError(error)}`);
    }
  };

  const handleDelete = async () => {
    setSaveError(null);
    try {
      await onDelete();
    } catch (error) {
      console.error("Failed to delete step:", error);
      setSaveError(`删除步骤失败：${describeError(error)}`);
    }
  };
  const orderPrefix = getStepOrderPrefix(step.step_order, step.name);

  return (
    <article className="border border-border rounded-lg bg-surface overflow-hidden">
      {/* 头部 */}
      <header className="px-4 py-3 border-b border-border bg-surface-hover/40 flex items-center gap-3">
        {orderPrefix && (
          <span className="text-xs text-fg-subtle font-mono">
            {orderPrefix}
          </span>
        )}
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => {
            void commit("name", name);
          }}
          disabled={readOnly}
          placeholder="步骤名"
          className="flex-1 bg-transparent text-sm font-medium text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />
        <ModelInlineSelector
          value={step.model}
          onChange={(m) => {
            void commit("model", m);
          }}
          disabled={readOnly}
        />
        {!readOnly && onRunStep && (
          <button
            onClick={() => onRunStep()}
            disabled={isRunningStep}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded-md text-fg-muted hover:text-accent hover:bg-canvas transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            title="只运行这一步（输入 = 上一步 output）"
          >
            <Play size={12} />
            单独运行
          </button>
        )}
        {!readOnly && (
          <button
            onClick={() => {
              void handleDelete();
            }}
            className="p-1 text-fg-subtle hover:text-error transition-colors"
            title="删除步骤"
          >
            <Trash2 size={14} />
          </button>
        )}
      </header>

      {/* 内容 */}
      <div className="p-4 space-y-3">
        <PromptField
          label="System Prompt"
          placeholder="这步该做什么、扮演什么角色…"
          value={systemPrompt}
          onChange={setSystemPrompt}
          onBlur={() => {
            void commit("system_prompt", systemPrompt);
          }}
          disabled={readOnly}
          minRows={3}
        />
        <PromptField
          label="User Prompt"
          placeholder="留空则隐式继承上一步输出"
          value={userPrompt}
          onChange={setUserPrompt}
          onBlur={() => {
            void commit("user_prompt", userPrompt);
          }}
          disabled={readOnly}
          minRows={2}
        />
        <ToolAllowlistEditor
          value={step.enable_tools}
          onChange={(t) => {
            void commit("enable_tools", t);
          }}
          disabled={readOnly}
        />
        {saveError && (
          <div className="rounded-md bg-error/10 px-3 py-2 text-xs text-error">
            {saveError}
          </div>
        )}
      </div>
    </article>
  );
}

// ─────────────── 局部组件：内嵌模型选择 ───────────────

interface InlineModelProps {
  value: string | null;
  onChange: (model: string | null) => void;
  disabled?: boolean;
}

/**
 * Step 内嵌模型选择器。
 *
 * 不复用 components/ModelSelector.tsx —— 那个组件绑定全局 store.selectedModel，
 * 这里需要每个 step 独立的受控值。
 */
function ModelInlineSelector({ value, onChange, disabled }: InlineModelProps) {
  const models = useStore((s) => s.models);
  const [open, setOpen] = useState(false);

  const current = models.find((m) => m.id === value);
  const label = current?.name || (value ?? "选择模型");

  return (
    <div className="relative">
      <button
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs bg-canvas hover:bg-canvas/70 text-fg transition-colors disabled:cursor-not-allowed disabled:opacity-50"
      >
        {current && (
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: current.color }}
          />
        )}
        <span>{label}</span>
        <ChevronDown size={10} className="text-fg-muted" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute top-full mt-1 right-0 z-20 min-w-[180px] rounded-md bg-surface border border-border shadow-lg py-1">
            {models.map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  onChange(m.id);
                  setOpen(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs hover:bg-surface-hover ${
                  value === m.id ? "bg-surface-hover" : ""
                }`}
              >
                <span
                  className="w-1.5 h-1.5 rounded-full"
                  style={{ background: m.color }}
                />
                <span className="text-fg">{m.name}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ─────────────── 局部组件：auto-grow prompt 输入 ───────────────

interface PromptFieldProps {
  label: string;
  placeholder?: string;
  value: string;
  onChange: (v: string) => void;
  onBlur?: () => void;
  disabled?: boolean;
  minRows?: number;
}

function PromptField({
  label,
  placeholder,
  value,
  onChange,
  onBlur,
  disabled,
  minRows = 2,
}: PromptFieldProps) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-fg-muted">{label}</label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        placeholder={placeholder}
        disabled={disabled}
        rows={Math.max(minRows, value.split("\n").length)}
        className="w-full px-3 py-2 rounded-md bg-canvas border border-border text-sm text-fg outline-none focus:border-accent transition-colors resize-y placeholder:text-fg-subtle disabled:cursor-not-allowed disabled:opacity-70 font-mono"
      />
    </div>
  );
}
