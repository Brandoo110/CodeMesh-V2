"use client";

/**
 * 工具白名单编辑器（v5 Phase 6.3）。
 *
 * 13 个工具 × 4 个预设 = 真正的差异化护城河 #1：
 * 让每个 step 自己决定能用哪些工具（Reviewer 只读 / Coder 写 / Full 全开 / 禁用）。
 *
 * 后端协议：
 *   - ["*"]    = 全开
 *   - []       = 完全禁用（纯文本生成步骤）
 *   - 数组     = 白名单
 */

import { useMemo } from "react";

interface Props {
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

// 对齐 web/execution/tools.py 实际注册的工具名
const TOOLS = [
  "bash_exec",
  "grep_text",
  "read_file",
  "edit_file",
  "write_file",
  "glob_files",
  "lsp_code",
  "web_search",
  "fetch_url",
  "invoke_skill",
  "remember_fact",
  "recall_facts",
  "forget_fact",
];

const PRESETS: Record<string, string[]> = {
  Reviewer: ["grep_text", "read_file", "glob_files", "lsp_code", "web_search", "fetch_url"],
  Coder: [
    "grep_text", "read_file", "edit_file", "write_file", "glob_files",
    "lsp_code", "web_search", "fetch_url",
  ],
  Full: TOOLS,
};

export function ToolAllowlistEditor({ value, onChange, disabled }: Props) {
  const allowAll = value.includes("*");
  const allDisabled = value.length === 0 && !allowAll;

  // O(1) 查找：allowAll 时所有 box 勾上
  const enabled = useMemo(
    () => new Set(allowAll ? TOOLS : value),
    [allowAll, value],
  );

  const toggle = (tool: string) => {
    if (disabled) return;
    if (allowAll) {
      // 从全开降到单独勾选状态：先 expand 再 toggle
      onChange(TOOLS.filter((t) => t !== tool));
      return;
    }
    if (enabled.has(tool)) {
      onChange(value.filter((t) => t !== tool));
    } else {
      onChange([...value, tool]);
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs text-fg-muted">工具白名单</div>

      <div className="grid grid-cols-2 gap-1.5">
        {TOOLS.map((t) => (
          <label
            key={t}
            className={`flex items-center gap-2 px-2 py-1 rounded text-xs cursor-pointer transition-colors ${
              enabled.has(t) ? "bg-canvas text-fg" : "text-fg-muted hover:bg-canvas/50"
            } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
          >
            <input
              type="checkbox"
              checked={enabled.has(t)}
              onChange={() => toggle(t)}
              disabled={disabled}
              className="accent-accent"
            />
            <span className="font-mono">{t}</span>
          </label>
        ))}
      </div>

      <div className="flex flex-wrap gap-1.5 pt-1">
        {Object.entries(PRESETS).map(([name, tools]) => (
          <button
            key={name}
            disabled={disabled}
            onClick={() => onChange(name === "Full" ? ["*"] : tools)}
            className="px-2 py-0.5 text-xs rounded border border-border bg-canvas hover:bg-canvas/70 text-fg-muted hover:text-fg transition-colors disabled:opacity-50"
            title={
              name === "Reviewer"
                ? "只读 + 联网：grep / read / glob / lsp / web"
                : name === "Coder"
                  ? "写入 + 联网：reviewer + edit / write"
                  : "全部工具"
            }
          >
            {name}
          </button>
        ))}
        <button
          disabled={disabled}
          onClick={() => onChange([])}
          className="px-2 py-0.5 text-xs rounded border border-border bg-canvas hover:bg-canvas/70 text-fg-muted hover:text-fg transition-colors disabled:opacity-50"
          title="完全禁用（纯文本生成步骤）"
        >
          禁用
        </button>
      </div>

      <div className="text-xs text-fg-subtle">
        {allowAll
          ? "✓ 全部工具可用"
          : allDisabled
            ? "✗ 完全禁用（纯文本生成）"
            : `✓ ${value.length} 个工具`}
      </div>
    </div>
  );
}
