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
import { useStore } from "@/lib/store";
import { fetchModels } from "@/lib/api";

export default function Home() {
  const setModels = useStore((s) => s.setModels);

  useEffect(() => {
    fetchModels()
      .then(setModels)
      .catch((e) => {
        console.error("Failed to load models:", e);
        // 后端未启 → 留空 selector 显示"自动选择"也能用
      });
  }, [setModels]);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
        <ChatView />
      </div>
    </div>
  );
}
