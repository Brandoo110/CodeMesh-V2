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
import { useStore } from "@/lib/store";
import { fetchModels } from "@/lib/api";

export default function Home() {
  const setModels = useStore((s) => s.setModels);
  const view = useStore((s) => s.view);

  useEffect(() => {
    fetchModels()
      .then(setModels)
      .catch((e) => {
        console.error("Failed to load models:", e);
      });
  }, [setModels]);

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
