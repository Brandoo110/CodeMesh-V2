"use client";

/**
 * 消息气泡：根据 role 渲染不同样式
 *
 * - user: 右侧，圆角背景灰
 * - assistant: 左侧，无背景，前置头像（24px 圆形 model 色）
 * - system: 居中灰色斜体小字
 * - error: 红边卡片
 *
 * Markdown 用 react-markdown 渲染；代码高亮 Phase 3 加 Shiki。
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useStore } from "@/lib/store";
import type { Message } from "@/lib/types";
import { ToolCallCard } from "./ToolCallCard";

interface Props {
  message: Message;
}

export function MessageBubble({ message }: Props) {
  const models = useStore((s) => s.models);
  const model = message.model ? models.find((m) => m.id === message.model) : null;
  const modelColor = model?.color || "#a0a0a0";

  // ─── User ───
  if (message.role === "user") {
    return (
      <div className="flex justify-end mb-6">
        <div className="max-w-[720px] rounded-xl bg-surface-hover px-4 py-2.5 text-fg">
          <div className="prose-msg whitespace-pre-wrap">{message.content}</div>
        </div>
      </div>
    );
  }

  // ─── System (居中小字) ───
  if (message.role === "system") {
    return (
      <div className="flex justify-center mb-4">
        <div className="text-xs text-fg-subtle italic">{message.content}</div>
      </div>
    );
  }

  // ─── Error (红边卡片) ───
  if (message.role === "error") {
    return (
      <div className="flex justify-start mb-6">
        <div className="max-w-[720px] rounded-xl border border-error/40 bg-error/10 px-4 py-2.5 text-error">
          <div className="text-xs font-semibold mb-1">⚠ 出错了</div>
          <div className="prose-msg whitespace-pre-wrap text-fg">{message.content}</div>
        </div>
      </div>
    );
  }

  // ─── Assistant ───
  return (
    <div className="flex gap-3 mb-6 max-w-[720px]">
      <div
        className="w-6 h-6 rounded-full flex-shrink-0 mt-1"
        style={{ background: modelColor }}
        title={model?.name || message.model || "auto"}
      />
      <div className="flex-1 min-w-0">
        {/* 工具调用卡片在文本之前显示（先调用工具再回答）*/}
        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mb-3">
            {message.toolCalls.map((t, i) => (
              <ToolCallCard key={i} tool={t} />
            ))}
          </div>
        )}

        {/* 内容 + pending 状态 */}
        {message.pending && !message.content && (!message.toolCalls || message.toolCalls.length === 0) ? (
          <div className="text-fg-muted text-sm italic flex items-center gap-2">
            <span className="inline-block w-2 h-2 rounded-full bg-fg-muted animate-pulse" />
            <span>思考中...</span>
          </div>
        ) : (
          <>
            {message.content && (
              <div className="prose-msg text-fg">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.content}
                </ReactMarkdown>
                {message.pending && (
                  <span className="inline-block w-2 h-4 ml-0.5 bg-fg animate-pulse align-middle" />
                )}
              </div>
            )}
            {!message.pending && (message.duration_ms !== undefined || message.cost_rmb !== undefined) && (
              <div className="mt-2 text-xs text-fg-subtle flex items-center gap-3">
                {message.duration_ms !== undefined && (
                  <span>{(message.duration_ms / 1000).toFixed(2)}s</span>
                )}
                {message.cost_rmb !== undefined && message.cost_rmb > 0 && (
                  <span>¥{message.cost_rmb.toFixed(4)}</span>
                )}
                {model && <span style={{ color: modelColor }}>{model.name}</span>}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
