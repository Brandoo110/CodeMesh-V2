"use client";

/**
 * 主页：Layout (Sidebar + TopBar + ChatView)
 *
 * 启动时拉模型列表填充 store。
 */

import { useEffect } from "react";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { ChatView } from "@/components/ChatView";
import { StatsView } from "@/components/StatsView";
import { WorkflowsView } from "@/components/WorkflowsView";
import { useStore } from "@/lib/store";
import { fetchModels } from "@/lib/api";

export default function Home() {
  const setModels = useStore((s) => s.setModels);
  const selectedModel = useStore((s) => s.selectedModel);
  const setSelectedModel = useStore((s) => s.setSelectedModel);
  const view = useStore((s) => s.view);

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

  // workflows view 占满整个区域（无外部 Sidebar），保持 chat / stats 现有结构
  if (view === "workflows") {
    return (
      <div className="flex h-screen w-screen overflow-hidden bg-canvas flex-col">
        <TopBar />
        <WorkflowsView />
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
        {view === "chat" ? <ChatView /> : <StatsView />}
      </div>
    </div>
  );
}
