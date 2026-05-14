"use client";

/**
 * 顶栏：sidebar toggle + 标题 + view 切换 segmented control + ModelSelector
 *
 * Phase 2 只有模型选择；Phase 4 加 Stats 切换；Phase 6.2 改成 3-tab segmented control
 * （Chat / Stats / Workflows），ModelSelector 仅在 Chat view 显示
 * （workflow 模型在 step 内选）。
 */

import { Menu, MessageSquare, BarChart3, GitBranch } from "lucide-react";
import { useStore, type View } from "@/lib/store";
import { ModelSelector } from "./ModelSelector";

const VIEWS: { id: View; label: string; icon: typeof MessageSquare }[] = [
  { id: "chat", label: "对话", icon: MessageSquare },
  { id: "workflows", label: "工作流", icon: GitBranch },
  { id: "stats", label: "Stats", icon: BarChart3 },
];

export function TopBar() {
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);

  return (
    <header className="h-14 flex-shrink-0 border-b border-border bg-canvas flex items-center px-4">
      {/* 左：sidebar toggle + 标题（flex-1 占据剩余空间 1/2） */}
      <div className="flex-1 flex items-center gap-3">
        <button
          className="p-1.5 rounded-md hover:bg-surface text-fg-muted transition-colors"
          onClick={toggleSidebar}
          title="切换侧栏 (Cmd+\\)"
        >
          <Menu size={18} />
        </button>
        <h1 className="text-sm font-medium text-fg">CodeMesh</h1>
      </div>

      {/* 中：segmented control 3-tab（不 grow，靠左右 flex-1 居中） */}
      <div className="flex items-center bg-surface rounded-lg p-0.5 border border-border">
        {VIEWS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setView(id)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-medium transition-colors ${
              view === id
                ? "bg-canvas text-accent shadow-sm"
                : "text-fg-muted hover:text-fg"
            }`}
            title={label}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </div>

      {/* 右：ModelSelector（flex-1 让 model 名长短不影响中间居中） */}
      <div className="flex-1 flex items-center gap-2 justify-end">
        {view === "chat" && <ModelSelector />}
      </div>
    </header>
  );
}
