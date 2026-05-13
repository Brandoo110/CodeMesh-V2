"use client";

/**
 * Stats Dashboard 嵌入（Phase 4）。
 *
 * 后端 /api/stats 返回完整 HTML（feedback/stats_report.py 渲染）。
 * 前端 iframe 直接嵌入——零适配，HTML 自带暗色主题与项目对齐。
 *
 * 顶部 segmented control 切窗口（7d / 30d / 90d / all）。
 */

import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

const RANGES: { id: string; label: string }[] = [
  { id: "7d", label: "近 7 天" },
  { id: "30d", label: "近 30 天" },
  { id: "90d", label: "近 90 天" },
  { id: "all", label: "全部" },
];

export function StatsView() {
  const [range, setRange] = useState("30d");
  const src = `${API_BASE}/api/stats?range=${range}`;

  return (
    <div className="flex-1 flex flex-col min-w-0 bg-canvas">
      {/* 顶部 range segmented control */}
      <div className="border-b border-border px-6 py-3 flex items-center gap-2">
        <span className="text-sm text-fg-muted mr-2">时间窗口</span>
        <div className="inline-flex bg-surface rounded-md p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.id}
              className={`px-3 py-1.5 text-sm rounded transition-colors ${
                range === r.id
                  ? "bg-canvas text-fg shadow-sm"
                  : "text-fg-muted hover:text-fg"
              }`}
              onClick={() => setRange(r.id)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* iframe 嵌入 stats HTML */}
      <iframe
        key={range}  // range 切换时强制 reload
        src={src}
        className="flex-1 w-full border-0"
        title="CodeMesh Stats Dashboard"
      />
    </div>
  );
}
