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

// ─────────────── Assurance Workbench (V2 P5) ───────────────

export interface AssuranceCaseState {
  case_id: string;
  subject_digest: string;
  state: string;
  evidence_refs: string[];
  finding_refs: string[];
  execution_receipt_refs: string[];
  policy_decision_refs: string[];
  human_decision_refs: string[];
  conditions: string[];
  conflicts: string[];
  missing_evidence: string[];
  invalidation_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssuranceMetadata {
  change_id: string;
  title: string;
  summary: string;
  owner: string;
  owner_role: string;
  author: string;
  risk: "low" | "medium" | "high" | "critical";
  priority: number;
  value: number;
  release_status: string;
  intent_coverage?: string;
  architecture_impact?: string;
  operational_readiness?: string;
  knowledge_notes?: string;
  ownership_notes?: string;
}

export interface AssuranceEvidence {
  evidence_id: string;
  subject_digest: string;
  kind: string;
  producer: string;
  artifact_digest: string;
  source_ref: string;
  trace_id: string | null;
  status: string;
  trust_level: string;
  collected_at: string;
}

export interface AssuranceFinding {
  finding_id: string;
  subject_digest: string;
  reviewer_role: "intent" | "architecture" | "operability";
  claim: string;
  evidence_refs: string[];
  basis: string;
  severity: string;
  confidence: number;
  rubric_hash: string;
  model_ref: string;
  status: string;
  evidence_status: "backed" | "missing";
}

export interface AssuranceReceiptStep {
  sequence: number;
  planned_role: string;
  actual_role: string | null;
  model_ref: string | null;
  provider: string | null;
  tool_grants: string[];
  routing_rule: string;
  fallback_reason: string | null;
  token_budget: number | null;
  timeout_seconds: number;
  result: string;
  schema_status: string;
}

export interface AssuranceReceipt {
  receipt_id: string;
  run_id: string;
  overall_result: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  started_at: string;
  completed_at: string;
  steps: AssuranceReceiptStep[];
}

export interface AssuranceTimelineItem {
  type: "event" | "decision" | "receipt_step" | string;
  id: string;
  sequence: number;
  at: string;
  kind?: string;
  outcome?: string;
  routing_rule?: string;
  result?: string;
  reason?: string | null;
}

export type AssuranceActionCode =
  | "approve"
  | "reject"
  | "approve_with_conditions"
  | "waiver"
  | "download_passport"
  | (string & {});

export interface AssuranceAction {
  code: AssuranceActionCode;
  required_human_role: string | null;
  self_approval_forbidden: boolean;
  high_risk_confirmation_required: boolean;
}

export interface AssurancePolicyGate {
  status: string;
  decision_id: string | null;
  reason_codes: string[];
  required_human_role: string | null;
  waiver_ref: string | null;
  evaluated_at: string | null;
}

export interface AssuranceReleaseState {
  status: string;
  observation_id: string | null;
  environment: string | null;
  deployment_id: string | null;
  source: string | null;
  trust_level: string | null;
  recorded_at: string | null;
}

export type AssuranceFreshnessStatus = "FRESH" | "STALE" | "UNAVAILABLE";

export interface AssuranceFreshness {
  status: AssuranceFreshnessStatus;
  reason_code: string;
  checked_at: string;
  expected_subject_digest?: string | null;
  observed_subject_digest?: string | null;
}

export interface AssuranceProjection {
  case: AssuranceCaseState;
  binding: {
    policy_version: string;
    rubric_version: string;
    waiver_id: string | null;
    waiver_expires_at: string | null;
  };
  metadata: AssuranceMetadata | null;
  evidence: AssuranceEvidence[];
  findings: AssuranceFinding[];
  receipt: AssuranceReceipt | null;
  decisions: Array<Record<string, unknown> & { kind: "policy" | "human" }>;
  timeline: AssuranceTimelineItem[];
  revision: number;
  schema_version: "v1";
  case_id: string;
  subject_digest: string;
  policy_gate: AssurancePolicyGate;
  acceptance_state: string;
  release_state: AssuranceReleaseState;
  allowed_actions: AssuranceAction[];
  gate: string;
  digest_freshness: boolean;
  freshness?: AssuranceFreshness | null;
  attention_reason: string | null;
}

export interface AssuranceDecisionRequest {
  decision_id: string;
  subject_digest: string;
  owner: string;
  owner_role: string;
  decision: "approve" | "reject" | "approve_with_conditions" | "waiver";
  reason: string;
  conditions: string[];
  waiver_id: string | null;
  expires_at: string | null;
  decided_at: string;
  high_risk_confirmed: boolean;
}

export interface AssuranceRunRequest {
  repository_path: string;
  repository_identity: string;
  author: string;
  base_ref: string;
  task_path: string;
  policy_paths: string[];
  adr_paths: string[];
  runbook_paths: string[];
  command_ids: string[];
  changed_lines_total: number | null;
  external_side_effects: "none_declared" | "present_declared" | "unknown";
  provider_boundary:
    | "within_declared_boundary"
    | "crosses_declared_boundary"
    | "unknown";
}

export interface AssuranceRunResponse {
  schema_version: "v1";
  run_id: string;
  request_digest: string;
  cached: boolean;
  case_id: string;
  case_view: AssuranceProjection;
}

export interface AssuranceArtifactReference {
  schema_version: "v1";
  digest: string;
  kind: string;
  label: string;
  byte_size: number;
  media_type: "text/plain";
  integrity_status: "SHA-256 integrity verified";
  role: "top_level" | "document" | "stdout" | "stderr";
  path: string | null;
  command_id: string | null;
  stream: "stdout" | "stderr" | null;
}

export interface AssuranceArtifactIndex {
  schema_version: "v1";
  case_id: string;
  evidence_id: string;
  evidence_kind: string;
  artifacts: AssuranceArtifactReference[];
}

export interface AssuranceArtifactContent {
  text: string;
  digest: string | null;
  byte_size: number | null;
}
