"use client";

/**
 * 主页：Layout (Sidebar + TopBar + 主视图)
 *
 * 启动时拉模型列表填充 store。
 */

import { useEffect, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { ChatView } from "@/components/ChatView";
import { StatsView } from "@/components/StatsView";
import { WorkflowsView } from "@/components/WorkflowsView";
import { MemoryView } from "@/components/MemoryView";
import { useStore } from "@/lib/store";
import { fetchModels } from "@/lib/api";
import {
  mainViewHostClassName,
  shouldKeepViewMounted,
  viewUsesChatSidebar,
} from "@/lib/layout";

export default function Home() {
  const setModels = useStore((s) => s.setModels);
  const selectedModel = useStore((s) => s.selectedModel);
  const setSelectedModel = useStore((s) => s.setSelectedModel);
  const view = useStore((s) => s.view);
  const [mountedWorkflow, setMountedWorkflow] = useState(view === "workflows");

  useEffect(() => {
    fetchModels()
      .then((rows) => {
        setModels(rows);
        // 没有"自动选择"了，所以默认就选第一个已配置的，避免 chat 空模型 fail
        if (rows.length > 0 && !selectedModel) {
          setSelectedModel(rows[0].id);
        }
      })
      .catch((e) => {
        console.error("Failed to load models:", e);
      });
    // selectedModel 不能进依赖，否则换模型时会重新拉接口
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setModels, setSelectedModel]);

  useEffect(() => {
    let cancelled = false;
    if (shouldKeepViewMounted(view) && view === "workflows" && !mountedWorkflow) {
      queueMicrotask(() => {
        if (!cancelled) setMountedWorkflow(true);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [mountedWorkflow, view]);

  const hasChatSidebar = viewUsesChatSidebar(view);
  const shouldRenderWorkflow = mountedWorkflow || view === "workflows";

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas">
      {hasChatSidebar && <Sidebar />}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <TopBar />
        <div className={mainViewHostClassName}>
          {view === "chat" && <ChatView />}
          {view === "stats" && <StatsView />}
          {view === "memory" && <MemoryView />}
          {shouldRenderWorkflow && (
            <div
              className={
                view === "workflows"
                  ? "flex h-full min-w-0 min-h-0"
                  : "hidden"
              }
            >
              <WorkflowsView />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
