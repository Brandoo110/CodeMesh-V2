/**
 * Zustand store — 全局状态。
 *
 * Phase 2 只放：
 *   - 当前模型选择
 *   - 模型列表
 *   - UI 状态（sidebar 展开 / 主题）
 *
 * 消息流不放全局（在 ChatView 组件本地用 useState），因为：
 *   1. 切换 session 时消息会换，本地 state 自然重置
 *   2. 避免每次输入字符触发全局 store update（流式输出场景）
 */

import { create } from "zustand";
import type { ModelInfo } from "./types";
import type { Workflow } from "./workflow-types";

export type View = "chat" | "stats" | "workflows";

interface StoreState {
  // 模型
  models: ModelInfo[];
  selectedModel: string | null;  // null = 让 router 决定
  setModels: (m: ModelInfo[]) => void;
  setSelectedModel: (id: string | null) => void;

  // Sessions（Phase 5）
  sessions: import("./types").SessionInfo[];
  currentSessionId: string | null;
  setSessions: (s: import("./types").SessionInfo[]) => void;
  setCurrentSessionId: (id: string | null) => void;

  // UI
  sidebarOpen: boolean;
  toggleSidebar: () => void;

  // View 切换（Phase 4 → Phase 6.2 加 workflows）
  view: View;
  setView: (v: View) => void;

  // Workflows（v5 Phase 6.2）
  workflows: Workflow[];
  currentWorkflowId: string | null;
  setWorkflows: (w: Workflow[]) => void;
  setCurrentWorkflowId: (id: string | null) => void;
}

export const useStore = create<StoreState>((set) => ({
  models: [],
  selectedModel: null,
  setModels: (m) => set({ models: m }),
  setSelectedModel: (id) => set({ selectedModel: id }),

  sessions: [],
  currentSessionId: null,
  setSessions: (s) => set({ sessions: s }),
  setCurrentSessionId: (id) => set({ currentSessionId: id }),

  sidebarOpen: true,
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),

  view: "chat",
  setView: (v) => set({ view: v }),

  workflows: [],
  currentWorkflowId: null,
  setWorkflows: (w) => set({ workflows: w }),
  setCurrentWorkflowId: (id) => set({ currentWorkflowId: id }),
}));
