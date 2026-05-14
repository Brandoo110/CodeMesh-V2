"use client";

/**
 * 模型选择器：dropdown 列 4 个模型 + configured 状态小圆点
 *
 * Phase 2 单选；Phase 6 加 compare 多选。
 */

import { Check, X, ChevronDown } from "lucide-react";
import { useState } from "react";
import { useStore } from "@/lib/store";

export function ModelSelector() {
  const models = useStore((s) => s.models);
  const selectedModel = useStore((s) => s.selectedModel);
  const setSelectedModel = useStore((s) => s.setSelectedModel);
  const [open, setOpen] = useState(false);

  const current = models.find((m) => m.id === selectedModel);
  const label = current?.name || "自动选择（router 决策）";

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
            {/* "自动" 选项 */}
            <button
              className={`w-full flex items-center justify-between px-3 py-2 hover:bg-surface-hover text-sm text-fg ${
                selectedModel === null ? "bg-surface-hover" : ""
              }`}
              onClick={() => {
                setSelectedModel(null);
                setOpen(false);
              }}
            >
              <span>自动选择（router 决策）</span>
              {selectedModel === null && <Check size={14} className="text-accent" />}
            </button>
            <div className="border-t border-border my-1" />
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
                <div className="flex items-center gap-2">
                  {m.configured ? (
                    <Check size={14} className="text-success" />
                  ) : (
                    <X size={14} className="text-fg-subtle" />
                  )}
                  {selectedModel === m.id && (
                    <Check size={14} className="text-accent" />
                  )}
                </div>
              </button>
            ))}
            <div className="border-t border-border my-1" />
            <div className="px-3 py-1.5 text-xs text-fg-subtle">
              ✓ 已配 key &nbsp; ✗ 未配 key
            </div>
          </div>
        </>
      )}
    </div>
  );
}
