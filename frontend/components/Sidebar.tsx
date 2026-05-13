"use client";

/**
 * 侧栏：新对话按钮 + 历史会话列表（Phase 2 占位）+ 设置入口
 *
 * Phase 5 会接 SQLite 真实历史；现在显示"暂无历史"。
 */

import { Plus, MessageSquare, Settings } from "lucide-react";
import { useStore } from "@/lib/store";

export function Sidebar() {
  const sidebarOpen = useStore((s) => s.sidebarOpen);

  if (!sidebarOpen) return null;

  return (
    <aside className="w-60 flex-shrink-0 border-r border-border bg-surface flex flex-col h-full">
      {/* 顶部：新对话按钮 */}
      <div className="p-3 border-b border-border">
        <button
          className="w-full flex items-center gap-2 px-3 py-2 rounded-md bg-canvas hover:bg-surface-hover text-fg text-sm transition-colors"
          onClick={() => window.location.reload()}
        >
          <Plus size={16} />
          <span>新对话</span>
        </button>
      </div>

      {/* 历史列表（Phase 5 完善） */}
      <div className="flex-1 overflow-y-auto p-2">
        <div className="text-xs text-fg-subtle px-2 py-1.5 select-none">
          最近对话
        </div>
        <div className="text-xs text-fg-subtle px-2 py-3 text-center italic">
          暂无历史
          <br />
          <span className="text-fg-subtle">(Phase 5 启用)</span>
        </div>
      </div>

      {/* 底部：设置 */}
      <div className="p-3 border-t border-border">
        <button
          className="w-full flex items-center gap-2 px-3 py-2 rounded-md hover:bg-surface-hover text-fg-muted text-sm transition-colors"
          disabled
          title="Phase 7 启用"
        >
          <Settings size={16} />
          <span>设置</span>
        </button>
      </div>
    </aside>
  );
}
