"use client";

/**
 * 输入栏：多行 textarea + Cmd+Enter 发送
 *
 * Phase 2 简版：纯文本 + 发送按钮。
 * Phase 3+ 加：工具开关 / token 估算 / 文件上传等。
 */

import { Send } from "lucide-react";
import { useRef, useState, useEffect } from "react";

interface Props {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export function InputBar({ onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // Auto-grow textarea
  useEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [text]);

  function handleSend() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter 直接发送；Shift+Enter（或输入法 composing 中）换行
    if (
      e.key === "Enter" &&
      !e.shiftKey &&
      !e.nativeEvent.isComposing  // 中文输入法选词时回车不要触发发送
    ) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="border-t border-border bg-canvas px-6 py-4">
      <div className="max-w-[820px] mx-auto">
        <div className="flex items-end gap-2 rounded-xl bg-surface border border-border focus-within:border-accent/50 transition-colors p-2">
          <textarea
            ref={ref}
            className="flex-1 bg-transparent text-fg placeholder:text-fg-subtle resize-none outline-none px-2 py-1.5 text-[15px] leading-relaxed"
            placeholder="问点什么...（Enter 发送，Shift+Enter 换行）"
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
          />
          <button
            className="flex items-center justify-center w-8 h-8 rounded-md bg-accent hover:bg-accent-hover disabled:bg-surface-hover disabled:text-fg-subtle text-canvas transition-colors flex-shrink-0"
            onClick={handleSend}
            disabled={disabled || !text.trim()}
            title="发送 (Enter)"
          >
            <Send size={16} />
          </button>
        </div>
        <div className="mt-2 text-xs text-fg-subtle text-center">
          模型可能会出错。复杂任务会调工具，首次响应需几秒。
        </div>
      </div>
    </div>
  );
}
