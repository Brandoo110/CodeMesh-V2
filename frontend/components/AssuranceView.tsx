"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileSearch,
  Plus,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  downloadAssurancePassport,
  createAssuranceRun,
  getAssuranceChange,
  listAssuranceArtifacts,
  listAssuranceChanges,
  readAssuranceArtifact,
  submitAssuranceDecision,
} from "@/lib/api";
import {
  getDecisionOptions,
  getSelectedDecisionAction,
} from "@/lib/assurance-case-view";
import type {
  AssuranceDecisionRequest,
  AssuranceArtifactIndex,
  AssuranceArtifactReference,
  AssuranceEvidence,
  AssuranceFinding,
  AssuranceProjection,
  AssuranceRunRequest,
  AssuranceReceiptStep,
} from "@/lib/types";

type DecisionKind = AssuranceDecisionRequest["decision"];

type RunFormState = Omit<AssuranceRunRequest, "policy_paths" | "adr_paths" | "runbook_paths" | "command_ids" | "changed_lines_total"> & {
  policy_paths: string;
  adr_paths: string;
  runbook_paths: string;
  command_ids: string;
  changed_lines_total: string;
};

const INITIAL_RUN_FORM: RunFormState = {
  repository_path: "",
  repository_identity: "",
  author: "",
  base_ref: "main",
  task_path: "TASK.md",
  policy_paths: "",
  adr_paths: "",
  runbook_paths: "",
  command_ids: "",
  changed_lines_total: "",
  external_side_effects: "unknown",
  provider_boundary: "unknown",
};

function splitRunPaths(value: string): string[] {
  return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}

const DECISION_LABELS: Record<DecisionKind, string> = {
  approve: "Accept",
  reject: "Reject",
  approve_with_conditions: "Accept with Conditions",
  waiver: "Waiver",
};

function shortDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function badgeTone(value: string): string {
  const normalized = value.toUpperCase();
  if (["ACCEPTED", "PASS", "SUCCESS", "VALID", "FRESH"].includes(normalized)) {
    return "border-success/30 bg-success/10 text-success";
  }
  if (["INVALIDATED", "REJECTED", "FAILURE", "ERROR", "CRITICAL", "STALE"].includes(normalized)) {
    return "border-error/30 bg-error/10 text-error";
  }
  if (["NEEDS_HUMAN", "NEEDS_EVIDENCE", "CONFLICTED", "CONDITIONAL", "HIGH", "CANCELLED", "UNAVAILABLE"].includes(normalized)) {
    return "border-warning/30 bg-warning/10 text-warning";
  }
  return "border-border bg-surface text-fg-muted";
}

function freshnessLabel(item: AssuranceProjection): string {
  if (!item.freshness) return "NOT CHECKED";
  return item.freshness.reason_code === "FRESHNESS_NOT_CHECKED"
    ? "NOT CHECKED"
    : item.freshness.status;
}

function liveFreshnessNotice(item: AssuranceProjection): { title: string; reason: string } | null {
  if (item.acceptance_state === "INVALIDATED") {
    return {
      title: "INVALIDATED / Subject 已失效",
      reason: item.case.invalidation_reason || "当前 CaseView 绑定不再有效，禁止签收。",
    };
  }
  if (!item.freshness) {
    return { title: "UNAVAILABLE / Freshness unavailable", reason: "NO_FRESHNESS_CHECKER" };
  }
  if (item.freshness.status === "FRESH") return null;
  return {
    title: item.freshness.status === "STALE"
      ? "STALE / Live baseline changed"
      : "UNAVAILABLE / Freshness could not be checked",
    reason: item.freshness.reason_code,
  };
}

function Badge({ value }: { value: string }) {
  return (
    <span className={`inline-flex items-center border px-2 py-0.5 text-[11px] font-semibold tracking-wide ${badgeTone(value)}`}>
      {value}
    </span>
  );
}

function InfoBlock({ title, value }: { title: string; value?: string | null }) {
  return (
    <div className="border-t border-border pt-3">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-fg-subtle">{title}</div>
      <div className="text-sm leading-6 text-fg">{value?.trim() || "未记录"}</div>
    </div>
  );
}

function AxisBlock({ title, value, note }: { title: string; value: string; note?: string }) {
  return (
    <div className="border border-border bg-surface p-3">
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-fg-subtle">{title}</div>
      <Badge value={value} />
      {note && <div className="mt-2 text-xs leading-5 text-fg-muted">{note}</div>}
    </div>
  );
}

function ArrayBlock({ title, values, danger = false }: { title: string; values: string[]; danger?: boolean }) {
  return (
    <div className="border-t border-border pt-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-fg-subtle">{title}</span>
        <span className="text-xs text-fg-subtle">{values.length}</span>
      </div>
      {values.length === 0 ? (
        <span className="text-sm text-fg-subtle">无</span>
      ) : (
        <ul className="space-y-1.5">
          {values.map((value) => (
            <li key={value} className={`border-l-2 pl-3 text-sm ${danger ? "border-error text-error" : "border-border text-fg-muted"}`}>
              {value}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EvidenceDrawer({ caseId, evidence, missingRef, onClose }: { caseId: string | null; evidence: AssuranceEvidence | null; missingRef: string | null; onClose: () => void }) {
  const [artifactIndex, setArtifactIndex] = useState<AssuranceArtifactIndex | null>(null);
  const [artifact, setArtifact] = useState<AssuranceArtifactReference | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loadingArtifacts, setLoadingArtifacts] = useState(false);
  const [loadingContent, setLoadingContent] = useState(false);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const artifactSequence = useRef(0);

  useEffect(() => {
    const sequence = ++artifactSequence.current;
    if (!caseId || !evidence) {
      setArtifactIndex(null);
      setArtifact(null);
      setContent(null);
      setArtifactError(null);
      return () => {
        if (artifactSequence.current === sequence) artifactSequence.current += 1;
      };
    }
    setLoadingArtifacts(true);
    setArtifactIndex(null);
    setArtifact(null);
    setContent(null);
    setArtifactError(null);
    listAssuranceArtifacts(caseId, evidence.evidence_id)
      .then((value) => { if (artifactSequence.current === sequence) setArtifactIndex(value); })
      .catch((cause) => { if (artifactSequence.current === sequence) setArtifactError(cause instanceof Error ? cause.message : String(cause)); })
      .finally(() => { if (artifactSequence.current === sequence) setLoadingArtifacts(false); });
    return () => {
      if (artifactSequence.current === sequence) artifactSequence.current += 1;
    };
  }, [caseId, evidence]);

  async function readArtifact(reference: AssuranceArtifactReference) {
    if (!caseId || !evidence) return;
    const sequence = ++artifactSequence.current;
    setArtifact(reference);
    setContent(null);
    setArtifactError(null);
    setLoadingContent(true);
    try {
      const value = await readAssuranceArtifact(caseId, evidence.evidence_id, reference.digest);
      if (artifactSequence.current === sequence) setContent(value.text);
    } catch (cause) {
      if (artifactSequence.current === sequence) setArtifactError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (artifactSequence.current === sequence) setLoadingContent(false);
    }
  }

  return (
    <aside className="flex h-full w-[370px] flex-shrink-0 flex-col border-l border-border bg-surface">
      <div className="flex h-14 items-center justify-between border-b border-border px-5">
        <div>
          <div className="text-xs uppercase tracking-[0.14em] text-fg-subtle">Evidence</div>
          <div className="max-w-[280px] truncate text-sm font-medium text-fg">{evidence?.evidence_id || missingRef}</div>
        </div>
        <button onClick={onClose} className="p-1.5 text-fg-muted hover:bg-surface-hover hover:text-fg" aria-label="关闭证据面板">
          <X size={17} />
        </button>
      </div>
      <div className="flex-1 space-y-5 overflow-y-auto p-5">
        {!evidence ? (
          <div className="border border-error/30 bg-error/10 p-4 text-sm text-error">
            Finding 引用了 Evidence，但当前 Case 中没有对应的可复查载荷。
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-2"><Badge value={evidence.kind} /><Badge value={evidence.trust_level} /><Badge value={evidence.status} /></div>
            <InfoBlock title="Collector / Producer" value={evidence.producer} />
            <InfoBlock title="Source" value={evidence.source_ref} />
            <InfoBlock title="Artifact Digest" value={evidence.artifact_digest} />
            <InfoBlock title="Trace" value={evidence.trace_id} />
            <InfoBlock title="Collected At" value={new Date(evidence.collected_at).toLocaleString("zh-CN")} />
            <InfoBlock title="Redaction" value="未记录" />
            <InfoBlock title="Truncation" value={evidence.status === "truncated" ? "是" : "未记录"} />
            <div className="border-t border-border pt-3">
              <div className="mb-2 flex items-center justify-between"><div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-fg-subtle">Authorized Artifacts</div>{artifactIndex && <span className="text-xs text-fg-subtle">{artifactIndex.artifacts.length}</span>}</div>
              {loadingArtifacts && <div className="text-sm text-fg-muted">正在读取服务端授权索引…</div>}
              {artifactError && <div className="border border-error/30 bg-error/10 p-3 text-sm text-error">{artifactError}</div>}
              {artifactIndex && <div className="space-y-2">{artifactIndex.artifacts.map((reference) => <button key={reference.digest} onClick={() => void readArtifact(reference)} className={`w-full border p-3 text-left ${artifact?.digest === reference.digest ? "border-accent bg-accent/5" : "border-border bg-canvas hover:bg-surface-hover"}`}><div className="flex items-start justify-between gap-3"><span className="text-sm font-medium text-fg">{reference.label}</span><Badge value={reference.role} /></div><div className="mt-1 break-all font-mono text-[11px] text-fg-muted">{reference.digest}</div><div className="mt-1 text-xs text-fg-subtle">{reference.byte_size} bytes · {reference.kind}</div></button>)}</div>}
            </div>
            {artifact && <div className="border-t border-border pt-3"><div className="mb-2 flex items-center justify-between"><div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-fg-subtle">{artifact.label}</div>{loadingContent && <RefreshCw size={14} className="animate-spin text-fg-subtle" />}</div>{content !== null && <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap break-words border border-border bg-canvas p-3 font-mono text-xs leading-5 text-fg">{content}</pre>}</div>}
          </>
        )}
      </div>
    </aside>
  );
}

export function AssuranceView() {
  const [cases, setCases] = useState<AssuranceProjection[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);
  const composerOpenRef = useRef(false);
  const loadSequence = useRef(0);
  const [detail, setDetail] = useState<AssuranceProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drawerEvidence, setDrawerEvidence] = useState<AssuranceEvidence | null>(null);
  const [drawerMissingRef, setDrawerMissingRef] = useState<string | null>(null);
  const [decision, setDecision] = useState<DecisionKind>("approve");
  const [owner, setOwner] = useState("");
  const [ownerRole, setOwnerRole] = useState("");
  const [reason, setReason] = useState("");
  const [conditions, setConditions] = useState("");
  const [waiverId, setWaiverId] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [highRiskConfirmed, setHighRiskConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [runForm, setRunForm] = useState<RunFormState>(INITIAL_RUN_FORM);
  const runIdempotencyKey = useRef<string | null>(null);
  const [runSubmitting, setRunSubmitting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const load = useCallback(async (quiet = false, preferredId?: string | null) => {
    const sequence = ++loadSequence.current;
    if (quiet) setRefreshing(true); else setLoading(true);
    try {
      const rows = await listAssuranceChanges();
      if (sequence !== loadSequence.current) return;
      if (composerOpenRef.current && preferredId === undefined) {
        setCases(rows);
        setError(null);
        return;
      }
      const currentId = preferredId ?? selectedIdRef.current;
      const nextId = currentId && rows.some((row) => row.case_id === currentId)
        ? currentId
        : rows[0]?.case_id ?? null;
      setCases(rows);
      selectedIdRef.current = nextId;
      setSelectedId(nextId);
      const nextDetail = nextId ? await getAssuranceChange(nextId) : null;
      if (sequence !== loadSequence.current) return;
      if (nextDetail) {
        setCases((current) => current.map((row) => row.case_id === nextDetail.case_id ? nextDetail : row));
      }
      setDetail(nextDetail);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 8000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    getAssuranceChange(selectedId)
      .then((value) => {
        if (!cancelled) {
          setDetail(value);
          setCases((rows) => rows.map((row) => row.case_id === value.case_id ? value : row));
          setError(null);
        }
      })
      .catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { cancelled = true; };
  }, [selectedId]);

  useEffect(() => {
    setOwner(detail?.metadata?.owner || "");
    setOwnerRole(detail?.metadata?.owner_role || "");
    setReason(""); setConditions(""); setWaiverId(""); setExpiresAt("");
    setHighRiskConfirmed(false);
  }, [detail?.case_id]);

  const evidenceById = useMemo(
    () => new Map((detail?.evidence || []).map((item) => [item.evidence_id, item])),
    [detail?.evidence],
  );

  const decisionOptions = useMemo(
    () => getDecisionOptions(detail?.allowed_actions || []),
    [detail?.allowed_actions],
  );
  const effectiveDecision = (
    decisionOptions.some((action) => action.code === decision)
      ? decision
      : decisionOptions[0]?.code as DecisionKind | undefined
  ) || null;
  const selectedAction = useMemo(
    () => getSelectedDecisionAction(detail?.allowed_actions || [], effectiveDecision),
    [detail?.allowed_actions, effectiveDecision],
  );
  const requiredRole = selectedAction?.required_human_role || null;
  const ownerRoleMatches = !requiredRole || ownerRole.trim() === requiredRole;
  const selfApprovalBlocked = Boolean(
    selectedAction?.self_approval_forbidden
      && owner.trim()
      && owner.trim() === detail?.metadata?.author,
  );
  const runCommands = splitRunPaths(runForm.command_ids);
  const runFormReady = Boolean(
    runForm.repository_path.trim()
    && runForm.repository_identity.trim()
    && runForm.author.trim()
    && runForm.base_ref.trim()
    && runForm.task_path.trim()
    && runCommands.length > 0
    && (runForm.changed_lines_total.trim() === "" || /^\d+$/.test(runForm.changed_lines_total.trim()))
  );
  const freshnessNotice = detail ? liveFreshnessNotice(detail) : null;

  function openEvidence(ref: string) {
    const evidence = evidenceById.get(ref) || null;
    setDrawerEvidence(evidence);
    setDrawerMissingRef(evidence ? null : ref);
  }

  function selectCase(caseId: string | null) {
    loadSequence.current += 1;
    const openingComposer = caseId === null;
    composerOpenRef.current = openingComposer;
    setComposerOpen(openingComposer);
    selectedIdRef.current = caseId;
    setSelectedId(caseId);
    if (openingComposer) {
      setDetail(null);
      setDrawerEvidence(null);
      setDrawerMissingRef(null);
      setRunError(null);
    }
  }

  function updateRunForm<K extends keyof RunFormState>(key: K, value: RunFormState[K]) {
    runIdempotencyKey.current = null;
    setRunForm((current) => ({ ...current, [key]: value }));
  }

  async function submitRun() {
    const commandIds = splitRunPaths(runForm.command_ids);
    if (!runForm.repository_path.trim() || !runForm.repository_identity.trim() || !runForm.author.trim() || !runForm.base_ref.trim() || !runForm.task_path.trim() || commandIds.length === 0) return;
    const request: AssuranceRunRequest = {
      repository_path: runForm.repository_path.trim(),
      repository_identity: runForm.repository_identity.trim(),
      author: runForm.author.trim(),
      base_ref: runForm.base_ref.trim(),
      task_path: runForm.task_path.trim(),
      policy_paths: splitRunPaths(runForm.policy_paths),
      adr_paths: splitRunPaths(runForm.adr_paths),
      runbook_paths: splitRunPaths(runForm.runbook_paths),
      command_ids: commandIds,
      changed_lines_total: runForm.changed_lines_total.trim() ? Number(runForm.changed_lines_total) : null,
      external_side_effects: runForm.external_side_effects,
      provider_boundary: runForm.provider_boundary,
    };
    if (request.changed_lines_total !== null && (!Number.isInteger(request.changed_lines_total) || request.changed_lines_total < 0)) return;
    setRunSubmitting(true);
    setRunError(null);
    const idempotencyKey = runIdempotencyKey.current || `web-run-${Date.now()}-${crypto.randomUUID()}`;
    runIdempotencyKey.current = idempotencyKey;
    try {
      const result = await createAssuranceRun(request, idempotencyKey);
      runIdempotencyKey.current = null;
      composerOpenRef.current = false;
      setComposerOpen(false);
      await load(true, result.case_id);
    } catch (cause) {
      setRunError(cause instanceof ApiError ? `服务端 ${cause.status}：${cause.message}` : cause instanceof Error ? cause.message : String(cause));
    } finally {
      setRunSubmitting(false);
    }
  }

  async function submitDecision() {
    if (
      !detail
      || !selectedAction
      || !effectiveDecision
      || !reason.trim()
      || !owner.trim()
      || !ownerRole.trim()
      || !ownerRoleMatches
      || selfApprovalBlocked
      || (selectedAction.high_risk_confirmation_required && !highRiskConfirmed)
    ) return;
    const parsedConditions = (
      effectiveDecision === "approve_with_conditions" || effectiveDecision === "waiver"
        ? conditions.split(/[\n,]/).map((item) => item.trim()).filter(Boolean)
        : []
    );
    setSubmitting(true);
    try {
      const decidedAt = new Date().toISOString();
      const request: AssuranceDecisionRequest = {
        decision_id: `human-${Date.now()}`,
        subject_digest: detail.subject_digest,
        owner: owner.trim(), owner_role: ownerRole.trim(), decision: effectiveDecision,
        reason: reason.trim(), conditions: parsedConditions,
        waiver_id: effectiveDecision === "waiver" ? waiverId.trim() || null : null,
        expires_at: effectiveDecision === "waiver" && expiresAt ? new Date(expiresAt).toISOString() : null,
        decided_at: decidedAt, high_risk_confirmed: highRiskConfirmed,
      };
      const updated = await submitAssuranceDecision(
        detail.case_id,
        request,
        `web-${detail.case_id}-${crypto.randomUUID()}`,
      );
      setDetail(updated);
      setCases((rows) => rows.map((row) => row.case_id === updated.case_id ? updated : row));
      setReason(""); setConditions("");
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <div className="flex flex-1 items-center justify-center text-sm text-fg-muted">正在加载 Acceptance Cases…</div>;
  }

  return (
    <div className="relative flex min-h-0 flex-1 overflow-hidden bg-canvas">
      <aside className="flex w-[310px] flex-shrink-0 flex-col border-r border-border bg-surface">
        <div className="flex h-14 items-center justify-between border-b border-border px-4">
          <div><div className="text-sm font-semibold text-fg">Change Queue</div><div className="text-xs text-fg-subtle">{cases.length} 个验收对象</div></div>
          <div className="flex items-center gap-1"><button onClick={() => selectCase(null)} className="p-2 text-fg-muted hover:bg-surface-hover hover:text-fg" title="新建本地 Run"><Plus size={16} /></button><button onClick={() => void load(true)} className="p-2 text-fg-muted hover:bg-surface-hover hover:text-fg" title="刷新"><RefreshCw size={16} className={refreshing ? "animate-spin" : ""} /></button></div>
        </div>
        {error && <div className="border-b border-error/30 bg-error/10 px-4 py-3 text-xs text-error">{error}</div>}
        <div className="flex-1 overflow-y-auto">
          {cases.length === 0 ? <div className="p-6 text-sm text-fg-muted">暂无 Acceptance Case。点击右上角＋提交本地 Run。</div> : cases.map((item) => {
            const active = item.case_id === selectedId;
            const meta = item.metadata;
            return (
              <button key={item.case_id} onClick={() => selectCase(item.case_id)} className={`w-full border-b border-border px-4 py-4 text-left transition-colors ${active ? "bg-canvas" : "hover:bg-surface-hover"}`}>
                <div className="mb-2 flex items-start justify-between gap-2"><span className="line-clamp-2 text-sm font-medium text-fg">{meta?.title || item.case_id}</span><div className="flex flex-wrap justify-end gap-1.5"><Badge value={item.policy_gate.status} /><Badge value={item.acceptance_state} /></div></div>
                <div className="mb-2 flex flex-wrap gap-1.5"><Badge value={meta?.risk || "unknown"} /><Badge value={freshnessLabel(item)} /></div>
                <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-fg-muted"><span>Owner {meta?.owner || "未记录"}</span><span>V/P {meta?.value ?? "-"}/{meta?.priority ?? "-"}</span><span>缺证 {item.case.missing_evidence.length}</span><span>{shortDate(item.case.updated_at)}</span></div>
                {item.attention_reason && <div className="mt-2 line-clamp-2 text-xs text-warning">{item.attention_reason}</div>}
              </button>
            );
          })}
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-y-auto">
        {composerOpen ? <div className="mx-auto max-w-[900px] px-7 py-8">
          <section className="border border-border bg-surface p-6">
            <div className="mb-6 flex items-start justify-between gap-4 border-b border-border pb-5"><div><div className="text-xs font-semibold uppercase tracking-[0.14em] text-fg-subtle">Local Assurance Run</div><h2 className="mt-2 text-2xl font-semibold tracking-tight text-fg">提交一次本地 Assurance Run</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-fg-muted">只提交调用方原始输入；Case、Evidence、Receipt 和 Policy 由服务端生成。Artifact 读取也会继续沿用服务端授权边界。</p></div><ShieldCheck size={22} className="mt-1 flex-shrink-0 text-accent" /></div>
            {runError && <div className="mb-4 border border-error/30 bg-error/10 p-3 text-sm text-error">{runError}</div>}
            <form onSubmit={(event) => { event.preventDefault(); void submitRun(); }} className="space-y-5">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-xs text-fg-muted">Repository path<span className="ml-1 text-error">*</span><input value={runForm.repository_path} onChange={(event) => updateRunForm("repository_path", event.target.value)} placeholder="/absolute/path/to/repository" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Repository identity<span className="ml-1 text-error">*</span><input value={runForm.repository_identity} onChange={(event) => updateRunForm("repository_identity", event.target.value)} placeholder="github.com/org/repository" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Author<span className="ml-1 text-error">*</span><input value={runForm.author} onChange={(event) => updateRunForm("author", event.target.value)} placeholder="调用方姓名或 Agent 标识" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Base ref<span className="ml-1 text-error">*</span><input value={runForm.base_ref} onChange={(event) => updateRunForm("base_ref", event.target.value)} placeholder="main" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Task path<span className="ml-1 text-error">*</span><input value={runForm.task_path} onChange={(event) => updateRunForm("task_path", event.target.value)} placeholder="TASK.md" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Changed lines total<input inputMode="numeric" value={runForm.changed_lines_total} onChange={(event) => updateRunForm("changed_lines_total", event.target.value)} placeholder="可选" className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-xs text-fg-muted">Command IDs<span className="ml-1 text-error">*</span><textarea value={runForm.command_ids} onChange={(event) => updateRunForm("command_ids", event.target.value)} placeholder="每行或逗号分隔，例如 check" rows={2} className="mt-1 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Policy paths（可选）<textarea value={runForm.policy_paths} onChange={(event) => updateRunForm("policy_paths", event.target.value)} placeholder="每行一个 .md 路径" rows={2} className="mt-1 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">ADR paths（可选）<textarea value={runForm.adr_paths} onChange={(event) => updateRunForm("adr_paths", event.target.value)} placeholder="每行一个 .md 路径" rows={2} className="mt-1 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
                <label className="text-xs text-fg-muted">Runbook paths（可选）<textarea value={runForm.runbook_paths} onChange={(event) => updateRunForm("runbook_paths", event.target.value)} placeholder="每行一个 .md 路径" rows={2} className="mt-1 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></label>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-xs text-fg-muted">External side effects<select value={runForm.external_side_effects} onChange={(event) => updateRunForm("external_side_effects", event.target.value as RunFormState["external_side_effects"])} className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent"><option value="unknown">unknown</option><option value="none_declared">none_declared</option><option value="present_declared">present_declared</option></select></label>
                <label className="text-xs text-fg-muted">Provider boundary<span className="ml-1 text-error">*</span><select value={runForm.provider_boundary} onChange={(event) => updateRunForm("provider_boundary", event.target.value as RunFormState["provider_boundary"])} className="mt-1 w-full border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent"><option value="unknown">unknown（服务端会返回 412）</option><option value="within_declared_boundary">within_declared_boundary</option><option value="crosses_declared_boundary">crosses_declared_boundary</option></select></label>
              </div>
              <div className="flex items-center justify-between gap-4 border-t border-border pt-5"><div className="text-xs leading-5 text-fg-subtle">失败或网络不确定时，保持表单不变即可复用同一 Idempotency-Key；修改任一字段后会生成新 Key。成功后自动刷新并选中新 Case。</div><button type="submit" disabled={!runFormReady || runSubmitting} className="flex items-center gap-2 bg-accent px-4 py-2 text-sm font-semibold text-canvas disabled:cursor-not-allowed disabled:opacity-40">{runSubmitting ? <RefreshCw size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}提交 Run</button></div>
            </form>
          </section>
        </div> : !detail ? <div className="flex h-full items-center justify-center text-sm text-fg-muted">从左侧选择一个 Case</div> : (
          <div className="mx-auto max-w-[1120px] space-y-6 px-7 py-6">
            {freshnessNotice && (
              <div className="flex items-start gap-3 border border-error/40 bg-error/10 p-4 text-error"><AlertTriangle className="mt-0.5 flex-shrink-0" size={18} /><div><div className="font-semibold">{freshnessNotice.title}</div><div className="mt-1 text-sm">{freshnessNotice.reason}</div></div></div>
            )}
            <header className="border-b border-border pb-5">
              <div className="mb-3 flex items-center justify-between gap-3"><span className="text-xs font-semibold uppercase tracking-[0.12em] text-fg-subtle">CaseView v1</span><span className="text-xs text-fg-subtle">rev {detail.revision}</span></div>
              <div className="flex items-start justify-between gap-6"><div><h2 className="text-2xl font-semibold tracking-tight text-fg">{detail.metadata?.title || detail.case_id}</h2><p className="mt-2 max-w-3xl text-sm leading-6 text-fg-muted">{detail.metadata?.summary || "未记录变更摘要"}</p></div><div className="flex flex-shrink-0 gap-2"><button onClick={() => void downloadAssurancePassport(detail.case_id, "json")} className="flex items-center gap-1.5 border border-border px-3 py-2 text-xs text-fg-muted hover:bg-surface"><Download size={14} />JSON</button><button onClick={() => void downloadAssurancePassport(detail.case_id, "markdown")} className="flex items-center gap-1.5 border border-border px-3 py-2 text-xs text-fg-muted hover:bg-surface"><Download size={14} />Markdown</button></div></div>
              <div className="mt-4 break-all font-mono text-xs text-fg-subtle">{detail.subject_digest}</div>
            </header>

            <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <AxisBlock title="Policy" value={detail.policy_gate.status} />
              <AxisBlock title="Acceptance" value={detail.acceptance_state} />
              <AxisBlock title="Release" value={detail.release_state.status} note={detail.release_state.trust_level ? `trust: ${detail.release_state.trust_level}` : undefined} />
              <AxisBlock title="Freshness" value={detail.freshness?.status || "UNAVAILABLE"} note={detail.freshness?.reason_code || "NO_FRESHNESS_CHECKER"} />
            </section>

            <section className="grid grid-cols-1 gap-5 lg:grid-cols-3"><InfoBlock title="Intent Coverage" value={detail.metadata?.intent_coverage} /><InfoBlock title="Architecture Impact" value={detail.metadata?.architecture_impact} /><InfoBlock title="Operational Readiness" value={detail.metadata?.operational_readiness} /><InfoBlock title="Knowledge" value={detail.metadata?.knowledge_notes} /><InfoBlock title="Ownership" value={detail.metadata?.ownership_notes} /><InfoBlock title="Policy / Rubric" value={`${detail.binding.policy_version} / ${detail.binding.rubric_version}`} /></section>

            <section><div className="mb-3 flex items-center justify-between"><h3 className="text-sm font-semibold uppercase tracking-[0.12em] text-fg-muted">Findings</h3><span className="text-xs text-fg-subtle">{detail.findings.length}</span></div><div className="space-y-2">{detail.findings.length === 0 ? <div className="border border-border p-4 text-sm text-fg-subtle">无 Finding</div> : detail.findings.map((finding: AssuranceFinding) => <button key={finding.finding_id} onClick={() => openEvidence(finding.evidence_refs[0])} className={`w-full border p-4 text-left ${finding.evidence_status === "missing" ? "border-error/40 bg-error/5" : "border-border bg-surface hover:bg-surface-hover"}`}><div className="flex items-start justify-between gap-4"><div><div className="mb-2 flex flex-wrap gap-2"><Badge value={finding.reviewer_role} /><Badge value={finding.severity} /><Badge value={finding.status} />{finding.evidence_status === "missing" && <Badge value="NO EVIDENCE" />}</div><p className="text-sm leading-6 text-fg">{finding.claim}</p><div className="mt-2 text-xs text-fg-subtle">{finding.model_ref} · confidence {Math.round(finding.confidence * 100)}%</div></div><FileSearch size={17} className="mt-1 flex-shrink-0 text-fg-subtle" /></div><div className="mt-3 flex flex-wrap gap-1.5">{finding.evidence_refs.map((ref) => <span key={ref} onClick={(event) => { event.stopPropagation(); openEvidence(ref); }} className="border border-border px-2 py-1 font-mono text-[11px] text-fg-muted hover:text-accent">{ref}</span>)}</div></button>)}</div></section>

            <section className="grid grid-cols-1 gap-5 lg:grid-cols-3"><ArrayBlock title="Missing Evidence" values={detail.case.missing_evidence} danger /><ArrayBlock title="Conflicts" values={detail.case.conflicts} danger /><ArrayBlock title="Conditions" values={detail.case.conditions} /></section>

            <section><h3 className="mb-3 text-sm font-semibold uppercase tracking-[0.12em] text-fg-muted">Execution Timeline</h3><div className="border border-border bg-surface">{detail.timeline.length === 0 ? <div className="p-4 text-sm text-fg-subtle">暂无执行记录</div> : detail.timeline.map((item, index) => { const step: AssuranceReceiptStep | undefined = item.type === "receipt_step" ? detail.receipt?.steps.find((value) => value.sequence === item.sequence) : undefined; return <div key={`${item.type}-${item.id}-${index}`} className="grid grid-cols-[76px_1fr] gap-4 border-b border-border p-4 last:border-b-0"><div className="text-xs text-fg-subtle">#{index + 1}<br />{shortDate(item.at)}</div><div><div className="mb-1 flex flex-wrap items-center gap-2"><Badge value={item.type} /><span className="text-sm font-medium text-fg">{item.kind || item.outcome || item.result || item.id}</span></div>{step && <div className="text-xs leading-5 text-fg-muted">{step.planned_role} → {step.actual_role || "未执行"} · {step.provider || "provider 未记录"}/{step.model_ref || "model 未记录"} · {step.routing_rule}{step.fallback_reason ? ` · fallback: ${step.fallback_reason}` : ""} · {step.result}</div>}{item.reason && <div className="text-xs text-warning">{item.reason}</div>}</div></div>; })}</div>{detail.receipt?.overall_result === "cancelled" && <div className="mt-2 border border-warning/30 bg-warning/10 p-3 text-sm text-warning">本次执行已取消，不视为完成或通过。</div>}</section>

            <section className="border border-border bg-surface p-5">
              <div className="mb-4 flex items-center gap-2">
                <ShieldCheck size={18} className="text-accent" />
                <div>
                  <h3 className="font-semibold text-fg">Human Decision</h3>
                  {requiredRole && <div className="mt-1 text-xs text-fg-subtle">Required role: {requiredRole}</div>}
                </div>
              </div>
              {decisionOptions.length === 0 ? (
                <div className="border border-border bg-canvas p-4 text-sm text-fg-muted">
                  当前 Case 没有可执行的 Decision action；只可导出 Passport。
                </div>
              ) : (
                <>
                  <div className="mb-4 grid grid-cols-2 gap-2 lg:grid-cols-4">
                    {decisionOptions.map((action) => {
                      const value = action.code as DecisionKind;
                      return (
                        <button key={value} onClick={() => setDecision(value)} className={`border px-3 py-2 text-xs font-medium ${effectiveDecision === value ? "border-accent bg-accent/10 text-accent" : "border-border text-fg-muted hover:bg-surface-hover"}`}>
                          {DECISION_LABELS[value]}
                        </button>
                      );
                    })}
                  </div>
                  {selectedAction?.self_approval_forbidden && (
                    <div className="mb-3 border-l-2 border-warning pl-3 text-xs text-warning">
                      {selfApprovalBlocked ? "当前身份与作者相同，禁止自批。" : "该 action 禁止作者自批。"}
                    </div>
                  )}
                  {requiredRole && !ownerRoleMatches && ownerRole.trim() && (
                    <div className="mb-3 border-l-2 border-warning pl-3 text-xs text-warning">owner_role 必须为 {requiredRole}。</div>
                  )}
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="Owner" className="border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" />
                    <input value={ownerRole} onChange={(e) => setOwnerRole(e.target.value)} placeholder={requiredRole ? `Role（需为 ${requiredRole}）` : "Role"} className="border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" />
                  </div>
                  <textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="签收理由（必填）" rows={3} className="mt-3 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" />
                  {(effectiveDecision === "approve_with_conditions" || effectiveDecision === "waiver") && <textarea value={conditions} onChange={(e) => setConditions(e.target.value)} placeholder="Conditions，逗号或换行分隔" rows={2} className="mt-3 w-full resize-y border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" />}
                  {effectiveDecision === "waiver" && <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2"><input value={waiverId} onChange={(e) => setWaiverId(e.target.value)} placeholder="Waiver ID" className="border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /><input type="datetime-local" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} className="border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent" /></div>}
                  {selectedAction?.high_risk_confirmation_required && <label className="mt-3 flex items-center gap-2 text-sm text-warning"><input type="checkbox" checked={highRiskConfirmed} onChange={(e) => setHighRiskConfirmed(e.target.checked)} />我已复核高风险变更并进行二次确认</label>}
                  <div className="mt-4 flex items-center justify-between gap-4">
                    <div className="text-xs text-fg-subtle">动作来自当前 CaseView allowed_actions；服务端会再次校验。</div>
                    <button onClick={() => void submitDecision()} disabled={submitting || !selectedAction || !reason.trim() || !owner.trim() || !ownerRole.trim() || !ownerRoleMatches || selfApprovalBlocked || Boolean(selectedAction?.high_risk_confirmation_required && !highRiskConfirmed)} className="flex items-center gap-2 bg-accent px-4 py-2 text-sm font-semibold text-canvas disabled:cursor-not-allowed disabled:opacity-40">
                      {submitting ? <RefreshCw size={15} className="animate-spin" /> : effectiveDecision === "reject" ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}提交签收
                    </button>
                  </div>
                </>
              )}
            </section>
          </div>
        )}
      </main>
      {(drawerEvidence || drawerMissingRef) && <EvidenceDrawer caseId={detail?.case_id || selectedId} evidence={drawerEvidence} missingRef={drawerMissingRef} onClose={() => { setDrawerEvidence(null); setDrawerMissingRef(null); }} />}
    </div>
  );
}
