"use client";

/**
 * 工具调用卡片：默认折叠显示一行，点击展开看 args + output。
 *
 * 视觉对齐 ui-design-plan.md §3.4.1：
 *   - 左侧 4px 模型色边
 *   - 折叠时一行 summary："🔧 grep_text [3 hits, 400ms ✓]"
 *   - 展开后 args (JSON) + output (限制 50 行预览)
 *
 * Phase 3 简版，args 直接 JSON.stringify；
 * Phase 4+ 可改 args 表格 / 高亮 / 重点字段 pretty-print。
 */

import { useState } from "react";
import { ChevronRight, ChevronDown, Wrench } from "lucide-react";
import type { ToolCall } from "@/lib/types";

interface Props {
  tool: ToolCall;
}

export function ToolCallCard({ tool }: Props) {
  const [open, setOpen] = useState(false);

  const statusBadge =
    tool.status === "pending" ? (
      <span className="inline-flex items-center gap-1 text-xs text-fg-muted">
        <span className="w-1.5 h-1.5 rounded-full bg-warning animate-pulse" />
        running
      </span>
    ) : tool.status === "ok" ? (
      <span className="text-xs text-success">✓</span>
    ) : (
      <span className="text-xs text-error">✗</span>
    );

  // args 短摘要（折叠时显示）
  const argsSummary = (() => {
    const keys = Object.keys(tool.args || {});
    if (keys.length === 0) return "";
    const first = keys[0];
    const val = String(tool.args[first] ?? "");
    const shown = val.length > 30 ? val.slice(0, 30) + "…" : val;
    return ` "${shown}"`;
  })();

  const resultLines = tool.result?.split("\n") || [];
  const resultPreview = resultLines.slice(0, 50).join("\n");
  const truncated = resultLines.length > 50;

  return (
    <div className="my-2 rounded-md bg-surface border-l-4 border-l-fg-muted overflow-hidden">
      <button
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-surface-hover transition-colors text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="flex items-center gap-2 min-w-0 flex-1">
          {open ? (
            <ChevronDown size={14} className="text-fg-muted flex-shrink-0" />
          ) : (
            <ChevronRight size={14} className="text-fg-muted flex-shrink-0" />
          )}
          <Wrench size={14} className="text-fg-muted flex-shrink-0" />
          <span className="text-sm text-fg font-mono truncate">
            {tool.name}
            <span className="text-fg-muted">{argsSummary}</span>
          </span>
        </div>
        <div className="flex-shrink-0 ml-2">{statusBadge}</div>
      </button>

      {open && (
        <div className="px-3 pb-3 pt-1 border-t border-border space-y-2">
          {/* args */}
          {Object.keys(tool.args || {}).length > 0 && (
            <div>
              <div className="text-xs text-fg-muted mb-1">args</div>
              <pre className="text-xs bg-canvas rounded p-2 overflow-x-auto font-mono text-fg">
                {JSON.stringify(tool.args, null, 2)}
              </pre>
            </div>
          )}
          {/* result */}
          {tool.result !== undefined && (
            <div>
              <div className="text-xs text-fg-muted mb-1">
                output{truncated && ` (前 50 / ${resultLines.length} 行)`}
              </div>
              <pre className="text-xs bg-canvas rounded p-2 overflow-x-auto font-mono text-fg whitespace-pre-wrap break-all max-h-[400px] overflow-y-auto">
                {resultPreview}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
