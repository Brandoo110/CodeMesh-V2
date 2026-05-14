/**
 * SSE 消费 hook（async generator 版）。
 *
 * ----------------------------------------------------------------------
 * 为什么不用浏览器原生 EventSource？
 * ----------------------------------------------------------------------
 * EventSource 只支持 GET 请求，不能带 POST body。我们的 task 可能很长
 * （多行 markdown / 长代码片段），URL query 会超长。所以用 fetch +
 * ReadableStream 手动解析 SSE 帧。
 *
 * 现代浏览器（Chrome 92+, Safari 14+, Firefox 102+）都支持 streaming fetch。
 *
 * ----------------------------------------------------------------------
 * SSE 帧格式（sse-starlette 输出）
 * ----------------------------------------------------------------------
 *     event: token
 *     data: {"delta":"hello"}
 *
 *     event: tool_start
 *     data: {"name":"grep_text","args":{...}}
 *
 * 帧之间用空行（\n\n）分隔。每帧可有多行 event:/data:。
 */

import { ApiError } from "./api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * 主入口：调 /api/chat/stream，yield 每个 SSE event。
 *
 * 用法：
 *   for await (const event of streamChat(task, { model: "deepseek" })) {
 *     switch (event.event) { case "token": ...; ... }
 *   }
 */
export async function* streamChat(
  task: string,
  options?: { model?: string; sessionId?: string },
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task,
      model: options?.model,
      session_id: options?.sessionId,
    }),
  });

  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const body = await res.text();
      detail = body || res.statusText;
    } catch {}
    throw new ApiError(res.status, detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // normalize CRLF → LF: sse-starlette 在 chunked transfer 下发的是 \r\n
      // 而 indexOf("\n\n") 找不到 \r\n\r\n 中的连续 \n，所以必须先 normalize
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      // SSE 用 \n\n 分帧
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseSSEFrame(frame);
        if (ev) yield ev;
      }
    }
    // 处理结尾残留帧（最后一帧后没 \n\n 的情况）
    if (buffer.trim()) {
      const ev = parseSSEFrame(buffer);
      if (ev) yield ev;
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSSEFrame(frame: string): StreamEvent | null {
  const lines = frame.split("\n");
  let eventName = "message";
  let data = "";

  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data += line.slice(5).trim();
    }
    // 忽略 id: / retry: / 注释行（以 : 开头）
  }

  if (!data) return null;

  try {
    return { event: eventName, data: JSON.parse(data) };
  } catch {
    // 后端理论上永远发合法 JSON，但兜底
    return { event: eventName, data: { raw: data } };
  }
}
