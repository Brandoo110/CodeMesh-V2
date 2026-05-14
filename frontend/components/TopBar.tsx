"use client";

/**
 * 顶栏：当前对话标题 + 模型选择 + 操作按钮
 *
 * Phase 2 只有模型选择；Phase 4 加 Stats 按钮；Phase 7 加主题切换 + 设置图标。
 */

import { Menu, BarChart3, MessageSquare } from "lucide-react";
import { useStore } from "@/lib/store";
import { ModelSelector } from "./ModelSelector";

export function TopBar() {
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);

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

      {/* 右：ModelSelector（仅 chat view）+ view 切换 */}
      <div className="flex items-center gap-2">
        {view === "chat" && <ModelSelector />}
        <button
          className={`p-1.5 rounded-md transition-colors ${
            view === "stats"
              ? "bg-surface text-accent"
              : "hover:bg-surface text-fg-muted"
          }`}
          onClick={() => setView(view === "stats" ? "chat" : "stats")}
          title={view === "stats" ? "返回对话" : "查看 Stats"}
        >
          {view === "stats" ? <MessageSquare size={18} /> : <BarChart3 size={18} />}
        </button>
      </div>
    </header>
  );
}
