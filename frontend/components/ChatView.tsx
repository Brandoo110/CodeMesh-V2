"use client";

/**
 * 对话主视图：消息列表 + 输入栏
 *
 * 状态：messages 本地 useState，不入全局 store（流式场景避免重渲染）。
 *
 * 流程：
 *   1. 用户输入 → onSend → 加 user 消息 + pending assistant
 *   2. 调 /api/chat → 等响应 → 替换 pending 为真实回答
 *   3. 报错 → 把 pending 换成 error 消息
 *
 * Phase 3 会把 fetch 换成 SSE，pending 内容流式追加。
 */

import { useState, useEffect, useRef } from "react";
import { sendChat, ApiError } from "@/lib/api";
import { useStore } from "@/lib/store";
import type { Message } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";
import { InputBar } from "./InputBar";

function uuid(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export function ChatView() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [sending, setSending] = useState(false);
  const selectedModel = useStore((s) => s.selectedModel);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新消息时自动滚到底
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  async function handleSend(text: string) {
    const userMsg: Message = {
      id: uuid(),
      role: "user",
      content: text,
      timestamp: Date.now(),
    };
    const pendingId = uuid();
    const pendingMsg: Message = {
      id: pendingId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      pending: true,
    };
    setMessages((ms) => [...ms, userMsg, pendingMsg]);
    setSending(true);

    try {
      const res = await sendChat({
        task: text,
        model: selectedModel || undefined,
      });
      setMessages((ms) =>
        ms.map((m) =>
          m.id === pendingId
            ? {
                ...m,
                content: res.answer,
                model: res.model,
                duration_ms: res.duration_ms,
                cost_rmb: res.cost_rmb,
                pending: false,
              }
            : m,
        ),
      );
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `${e.status}: ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e);
      setMessages((ms) =>
        ms.map((m) =>
          m.id === pendingId
            ? { ...m, role: "error" as const, content: msg, pending: false }
            : m,
        ),
      );
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0">
      {/* 消息列表（滚动区） */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-8">
        <div className="max-w-[820px] mx-auto">
          {messages.length === 0 ? (
            <EmptyState />
          ) : (
            messages.map((m) => <MessageBubble key={m.id} message={m} />)
          )}
        </div>
      </div>

      {/* 输入栏 */}
      <InputBar onSend={handleSend} disabled={sending} />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[400px] text-center select-none">
      <div className="text-2xl font-medium text-fg mb-3">CodeMesh</div>
      <div className="text-sm text-fg-muted max-w-[480px]">
        国内多模型 Code Agent。问点代码问题、文件操作、或者就聊聊。
      </div>
      <div className="mt-6 text-xs text-fg-subtle">
        当前 Phase 2：非流式对话已通；流式 + 工具调用可视化在 Phase 3
      </div>
    </div>
  );
}
