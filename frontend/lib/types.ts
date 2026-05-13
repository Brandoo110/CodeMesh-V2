/**
 * 前后端共享类型定义。
 *
 * 这些类型对应 web/schemas.py 的 Pydantic 模型，手动对齐。
 * 后续 Phase 4+ 可以用 openapi-typescript 自动生成。
 */

// ─────────────── Models ───────────────

export interface ModelInfo {
  id: string;          // "deepseek" / "qwen" / "doubao" / "gemini"
  name: string;        // "DeepSeek V4 Pro"
  configured: boolean; // env var key 是否已配
  color: string;       // hex "#5b8def"
}

// ─────────────── Chat ───────────────

export interface ChatRequest {
  task: string;
  model?: string;
  session_id?: string;
}

export interface ChatResponse {
  answer: string;
  model: string;
  duration_ms: number;
  cost_rmb: number;
  session_id?: string | null;
}

// ─────────────── Sessions ───────────────

export interface SessionInfo {
  id: string;
  title: string;
  created_at: string;   // ISO datetime
  updated_at: string;
  model?: string | null;
  message_count: number;
}

// ─────────────── 前端内部 ───────────────

/**
 * 单条消息（前端展示用）
 *
 * role 比后端多 system / error 两类，前端单独渲染。
 */
export interface Message {
  id: string;          // 前端生成的 uuid
  role: "user" | "assistant" | "system" | "error";
  content: string;
  model?: string;      // assistant 消息才有
  cost_rmb?: number;
  duration_ms?: number;
  timestamp: number;   // Date.now()
  pending?: boolean;   // assistant 等待回答时
}
