/**
 * 后端 API client wrapper。
 *
 * 所有 fetch 调用集中在这里，便于：
 *   1. 统一错误处理（throw ApiError with status）
 *   2. base URL 切换（dev: localhost:8000 / prod: 同域）
 *   3. 后续 Phase 3 加 SSE wrapper
 */

import type {
  ChatRequest,
  ChatResponse,
  AutoMemoryInfo,
  DreamStatusInfo,
  JournalInfo,
  LongTermFactCreateRequest,
  LongTermFactInfo,
  MemorySummary,
  MemoryType,
  ModelInfo,
  SessionInfo,
  SessionUpdateRequest,
  StoredMessage,
  AssuranceDecisionRequest,
  AssuranceProjection,
  AssuranceArtifactContent,
  AssuranceArtifactIndex,
  AssuranceRunRequest,
  AssuranceRunResponse,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

type ErrorObject = Record<string, unknown>;

function isErrorObject(value: unknown): value is ErrorObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatValidationItem(item: unknown): string {
  if (isErrorObject(item) && "msg" in item) {
    const msg = String(item.msg || "Invalid value");
    const loc = Array.isArray(item.loc)
      ? item.loc.map(String).join(".")
      : typeof item.loc === "string"
        ? item.loc
        : "";
    return loc ? `${loc}: ${msg}` : msg;
  }
  return formatApiErrorDetail(item);
}

export function formatApiErrorDetail(detail: unknown): string {
  if (typeof detail === "string") return detail || "Request failed";
  if (detail == null) return "Request failed";

  if (Array.isArray(detail)) {
    const rendered = detail.map(formatValidationItem).filter(Boolean);
    return rendered.length > 0 ? rendered.join("; ") : "Request failed";
  }

  if (isErrorObject(detail)) {
    if ("detail" in detail) return formatApiErrorDetail(detail.detail);
    if ("message" in detail) return formatApiErrorDetail(detail.message);
    if ("error" in detail) return formatApiErrorDetail(detail.error);

    try {
      return JSON.stringify(detail);
    } catch {
      return "Request failed";
    }
  }

  return String(detail);
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, detail: unknown) {
    super(formatApiErrorDetail(detail));
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });

  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      // body 不是 JSON，用 statusText 兜底
    }
    throw new ApiError(res.status, detail);
  }

  return res.json();
}

// ─────────────── Endpoints ───────────────

export async function fetchModels(): Promise<ModelInfo[]> {
  return request<ModelInfo[]>("/api/models");
}

export async function sendChat(req: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function listSessions(): Promise<SessionInfo[]> {
  return request<SessionInfo[]>("/api/sessions");
}

export async function createSession(title?: string): Promise<SessionInfo> {
  return request<SessionInfo>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: title || "新对话" }),
  });
}

export async function deleteSession(id: string): Promise<void> {
  await request(`/api/sessions/${id}`, { method: "DELETE" });
}

export async function updateSession(
  id: string,
  req: SessionUpdateRequest,
): Promise<SessionInfo> {
  return request<SessionInfo>(`/api/sessions/${id}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export async function getSessionMessages(id: string): Promise<StoredMessage[]> {
  return request<StoredMessage[]>(`/api/sessions/${id}/messages`);
}

export async function getMemorySummary(): Promise<MemorySummary> {
  return request<MemorySummary>("/api/memory/summary");
}

export async function listMemoryFacts(): Promise<LongTermFactInfo[]> {
  return request<LongTermFactInfo[]>("/api/memory/facts");
}

export async function createMemoryFact(
  req: LongTermFactCreateRequest,
): Promise<LongTermFactInfo> {
  return request<LongTermFactInfo>("/api/memory/facts", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function deleteMemoryFact(key: string): Promise<void> {
  await request(`/api/memory/facts/${encodeURIComponent(key)}`, { method: "DELETE" });
}

export async function listAutoMemories(type?: MemoryType | "all"): Promise<AutoMemoryInfo[]> {
  const q = type && type !== "all" ? `?type=${encodeURIComponent(type)}` : "";
  return request<AutoMemoryInfo[]>(`/api/memory/auto${q}`);
}

export async function listJournals(): Promise<JournalInfo[]> {
  return request<JournalInfo[]>("/api/memory/journal");
}

export async function getDreamStatus(): Promise<DreamStatusInfo> {
  return request<DreamStatusInfo>("/api/memory/dream/status");
}

export async function rebuildMemoryIndex(): Promise<{ path: string }> {
  return request<{ path: string }>("/api/memory/dream/rebuild-index", {
    method: "POST",
  });
}

export async function listAssuranceChanges(): Promise<AssuranceProjection[]> {
  return request<AssuranceProjection[]>("/api/assurance/changes");
}

export async function getAssuranceChange(id: string): Promise<AssuranceProjection> {
  return request<AssuranceProjection>(`/api/assurance/changes/${encodeURIComponent(id)}`);
}

export async function createAssuranceRun(
  payload: AssuranceRunRequest,
  idempotencyKey: string,
): Promise<AssuranceRunResponse> {
  return request<AssuranceRunResponse>("/api/assurance/runs", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
}

export async function listAssuranceArtifacts(
  caseId: string,
  evidenceId: string,
): Promise<AssuranceArtifactIndex> {
  return request<AssuranceArtifactIndex>(
    `/api/assurance/changes/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}/artifacts`,
  );
}

export async function readAssuranceArtifact(
  caseId: string,
  evidenceId: string,
  digest: string,
): Promise<AssuranceArtifactContent> {
  const res = await fetch(
    `${API_BASE}/api/assurance/changes/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}/artifacts/${encodeURIComponent(digest)}`,
    { headers: { Accept: "text/plain" } },
  );
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      // 非 JSON 错误体。
    }
    throw new ApiError(res.status, detail);
  }
  const text = await res.text();
  const rawSize = res.headers.get("X-Artifact-Size");
  const parsedSize = rawSize === null ? null : Number(rawSize);
  return {
    text,
    digest: res.headers.get("X-Artifact-Digest"),
    byte_size: rawSize !== null && Number.isSafeInteger(parsedSize) ? parsedSize : null,
  };
}

export async function submitAssuranceDecision(
  id: string,
  decision: AssuranceDecisionRequest,
  idempotencyKey: string,
): Promise<AssuranceProjection> {
  return request<AssuranceProjection>(
    `/api/assurance/changes/${encodeURIComponent(id)}/decisions`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(decision),
    },
  );
}

export async function downloadAssurancePassport(
  id: string,
  format: "json" | "markdown",
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/assurance/changes/${encodeURIComponent(id)}/passport?format=${format}`,
  );
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      // 非 JSON 错误体。
    }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `codemesh-${id}-passport.${format === "json" ? "json" : "md"}`;
  anchor.click();
  URL.revokeObjectURL(url);
}
