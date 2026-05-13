"use client";

/**
 * 顶栏：当前对话标题 + 模型选择 + 操作按钮
 *
 * Phase 2 只有模型选择；Phase 4 加 Stats 按钮；Phase 7 加主题切换 + 设置图标。
 */

import { Menu, BarChart3 } from "lucide-react";
import { useStore } from "@/lib/store";
import { ModelSelector } from "./ModelSelector";

export function TopBar() {
  const toggleSidebar = useStore((s) => s.toggleSidebar);

  return (
    <header className="h-14 flex-shrink-0 border-b border-border bg-canvas flex items-center justify-between px-4">
      {/* 左：sidebar toggle + 标题 */}
      <div className="flex items-center gap-3">
        <button
          className="p-1.5 rounded-md hover:bg-surface text-fg-muted transition-colors"
          onClick={toggleSidebar}
          title="切换侧栏 (Cmd+\\)"
        >
          <Menu size={18} />
        </button>
        <h1 className="text-sm font-medium text-fg">CodeMesh</h1>
      </div>

      {/* 右：模型选择 + Stats（占位） */}
      <div className="flex items-center gap-2">
        <ModelSelector />
        <button
          className="p-1.5 rounded-md hover:bg-surface text-fg-muted transition-colors"
          disabled
          title="Phase 4 启用"
        >
          <BarChart3 size={18} />
        </button>
      </div>
    </header>
  );
}
