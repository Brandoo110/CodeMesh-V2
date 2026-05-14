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
import { ApiError, getSessionMessages, listSessions } from "@/lib/api";
import { streamChat } from "@/lib/sse";
import { useStore } from "@/lib/store";
import type { Message, ToolCall, StoredMessage } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";
import { InputBar } from "./InputBar";

function uuid(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** 把后端 StoredMessage 转换成前端 Message 显示格式。 */
function storedToMessage(s: StoredMessage): Message {
  return {
    id: `db-${s.id}`,
    role: s.role,
    content: s.content,
    toolCalls: s.tool_calls || undefined,
    model: s.model || undefined,
    cost_rmb: s.cost_rmb ?? undefined,
    duration_ms: s.duration_ms ?? undefined,
    timestamp: new Date(s.created_at).getTime(),
  };
}

export function ChatView() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [sending, setSending] = useState(false);
  const selectedModel = useStore((s) => s.selectedModel);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const setSessions = useStore((s) => s.setSessions);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新消息时自动滚到底
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  // 切换 session 时加载历史
  useEffect(() => {
    if (!currentSessionId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    getSessionMessages(currentSessionId)
      .then((stored) => {
        if (!cancelled) setMessages(stored.map(storedToMessage));
      })
      .catch((e) => {
        if (!cancelled) {
          console.error("getSessionMessages failed:", e);
          setMessages([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [currentSessionId]);

  async function handleSend(text: string) {
    const userMsg: Message = {
      id: uuid(),
      role: "user",
      content: text,
      timestamp: Date.now(),
    };
    const pendingId = uuid();
    const t0 = Date.now();
    const pendingMsg: Message = {
      id: pendingId,
      role: "assistant",
      content: "",
      toolCalls: [],
      timestamp: t0,
      pending: true,
    };
    setMessages((ms) => [...ms, userMsg, pendingMsg]);
    setSending(true);

    try {
      // SSE 流式消费：每个 event 增量更新 pending 消息
      for await (const event of streamChat(text, {
        model: selectedModel || undefined,
        sessionId: currentSessionId || undefined,
      })) {
        switch (event.event) {
          case "token": {
            const delta = String(event.data.delta || "");
            setMessages((ms) =>
              ms.map((m) =>
                m.id === pendingId ? { ...m, content: m.content + delta } : m,
              ),
            );
            break;
          }
          case "tool_start": {
            const newTool: ToolCall = {
              name: String(event.data.name || "unknown"),
              args: (event.data.args as Record<string, unknown>) || {},
              status: "pending",
            };
            setMessages((ms) =>
              ms.map((m) =>
                m.id === pendingId
                  ? { ...m, toolCalls: [...(m.toolCalls || []), newTool] }
                  : m,
              ),
            );
            break;
          }
          case "tool_end": {
            // FIFO 配对：找最近一个 pending 同名 tool 更新结果
            setMessages((ms) =>
              ms.map((m) => {
                if (m.id !== pendingId) return m;
                const tools = [...(m.toolCalls || [])];
                const idx = tools.findIndex(
                  (t) => t.name === event.data.name && t.status === "pending",
                );
                if (idx < 0) return m;
                tools[idx] = {
                  ...tools[idx],
                  result: String(event.data.result || ""),
                  ok: Boolean(event.data.ok),
                  status: event.data.ok ? "ok" : "error",
                };
                return { ...m, toolCalls: tools };
              }),
            );
            break;
          }
          case "usage": {
            setMessages((ms) =>
              ms.map((m) =>
                m.id === pendingId
                  ? {
                      ...m,
                      model: String(event.data.model || m.model || ""),
                      cost_rmb: Number(event.data.cost_rmb || 0),
                    }
                  : m,
              ),
            );
            break;
          }
          case "done": {
            setMessages((ms) =>
              ms.map((m) =>
                m.id === pendingId
                  ? { ...m, pending: false, duration_ms: Date.now() - t0 }
                  : m,
              ),
            );
            break;
          }
          case "error": {
            const errMsg = String(event.data.message || "未知错误");
            setMessages((ms) =>
              ms.map((m) =>
                m.id === pendingId
                  ? {
                      ...m,
                      role: "error" as const,
                      content: errMsg,
                      pending: false,
                      duration_ms: Date.now() - t0,
                    }
                  : m,
              ),
            );
            break;
          }
        }
      }
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
      // 持久化后刷新 sessions 列表（updated_at + message_count 变了）
      if (currentSessionId) {
        try {
          const fresh = await listSessions();
          setSessions(fresh);
        } catch {}
      }
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
