"use client";

/**
 * 模型选择器：dropdown 列已配置模型 + 品牌色小圆点
 *
 * 顶栏的全局选择器。Workflows view 里每个 step 用独立的 ModelInlineSelector
 * （在 StepCard.tsx 内）。
 *
 * "自动选择（router 决策）" 选项已移除——router 当前 Literal 只输出
 * deepseek/qwen/doubao，对已配 Gemini / MiniMax 等模型的用户不友好。
 * 强制让用户显式挑一个模型，避免误用。
 */

import { Check, ChevronDown } from "lucide-react";
import { useState } from "react";
import { useStore } from "@/lib/store";

export function ModelSelector() {
  const models = useStore((s) => s.models);
  const selectedModel = useStore((s) => s.selectedModel);
  const setSelectedModel = useStore((s) => s.setSelectedModel);
  const [open, setOpen] = useState(false);

  const current = models.find((m) => m.id === selectedModel);
  const label = current?.name || "选择模型";

  return (
    <div className="relative">
      <button
        className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-surface hover:bg-surface-hover text-sm text-fg transition-colors"
        onClick={() => setOpen((v) => !v)}
      >
        {current && (
          <span
            className="w-2 h-2 rounded-full"
            style={{ background: current.color }}
          />
        )}
        <span>{label}</span>
        <ChevronDown size={14} className="text-fg-muted" />
      </button>

      {open && (
        <>
          {/* 点击外部关闭 */}
          <div
            className="fixed inset-0 z-10"
            onClick={() => setOpen(false)}
          />
          <div className="absolute top-full mt-1 right-0 z-20 min-w-[240px] rounded-md bg-surface border border-border shadow-lg py-1">
            {models.map((m) => (
              <button
                key={m.id}
                className={`w-full flex items-center justify-between px-3 py-2 hover:bg-surface-hover text-sm ${
                  selectedModel === m.id ? "bg-surface-hover" : ""
                }`}
                onClick={() => {
                  setSelectedModel(m.id);
                  setOpen(false);
                }}
              >
                <div className="flex items-center gap-2">
                  <span
                    className="w-2 h-2 rounded-full"
                    style={{ background: m.color }}
                  />
                  <span className="text-fg">{m.name}</span>
                </div>
                {selectedModel === m.id && (
                  <Check size={14} className="text-accent" />
                )}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
