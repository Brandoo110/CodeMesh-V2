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

export interface SessionUpdateRequest {
  title?: string;
}

/**
 * GET /api/sessions/{id}/messages 返回的消息（Phase 5）
 *
 * 字段对应 web/sessions_store.py 的 session_messages 表。
 * tool_calls 已经反序列化为 list[ToolCall] | null。
 */
export interface StoredMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  tool_calls: ToolCall[] | null;
  model: string | null;
  cost_rmb: number | null;
  duration_ms: number | null;
  created_at: string;
}

// ─────────────── Memory Panel ───────────────

export type MemoryType = "user" | "feedback" | "project" | "reference";

export interface LongTermFactInfo {
  key: string;
  value: unknown;
}

export interface LongTermFactCreateRequest {
  key: string;
  value: unknown;
}

export interface DreamStatusInfo {
  can_dream: boolean;
  reason: string;
  memory_entries: number;
  lock_present: boolean;
  last_dream_at: string | null;
}

export interface MemorySummary {
  facts_count: number;
  auto_memory_count: number;
  journal_count: number;
  memory_db_path: string;
  auto_memory_dir: string;
  journal_dir: string;
  dream: DreamStatusInfo;
}

export interface AutoMemoryInfo {
  name: string;
  description: string;
  type: MemoryType | string;
  path: string;
  updated_at: string;
  preview: string;
  indexed: boolean;
}

export interface JournalInfo {
  name: string;
  path: string;
  created_at: string;
  preview: string;
}

// ─────────────── 前端内部 ───────────────

/**
 * 工具调用（Phase 3）
 *
 * 后端 SSE 顺序：tool_start → ... → tool_end。
 * 前端按 FIFO 配对（CodeMesh 不并发同名工具）。
 */
export interface ToolCall {
  name: string;                        // "grep_text" / "read_file" / ...
  args: Record<string, unknown>;
  result?: string;                     // pending 时为 undefined
  ok?: boolean;
  status: "pending" | "ok" | "error";
}

/**
 * 单条消息（前端展示用）
 *
 * role 比后端多 system / error 两类，前端单独渲染。
 */
export interface Message {
  id: string;          // 前端生成的 uuid
  role: "user" | "assistant" | "system" | "error";
  content: string;
  toolCalls?: ToolCall[];  // assistant 走 agent loop 时累加
  model?: string;      // assistant 消息才有
  cost_rmb?: number;
  duration_ms?: number;
  timestamp: number;   // Date.now()
  pending?: boolean;   // assistant 等待回答时
}
