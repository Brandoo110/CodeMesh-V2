"use client";

/**
 * Memory Inspector.
 *
 * This view exposes existing CodeMesh memory storage. It does not trigger LLM
 * extraction; the only write actions are adding/deleting SQLite facts and
 * rebuilding the auto_memory index.
 */

import {
  Brain,
  Clipboard,
  Database,
  FileText,
  ListRestart,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  createMemoryFact,
  deleteMemoryFact,
  getMemorySummary,
  listAutoMemories,
  listJournals,
  listMemoryFacts,
  rebuildMemoryIndex,
} from "@/lib/api";
import { MemoryMarkdownPreview } from "@/lib/memory-markdown";
import type {
  AutoMemoryInfo,
  JournalInfo,
  LongTermFactInfo,
  MemorySummary,
  MemoryType,
} from "@/lib/types";

type Tab = "facts" | "auto" | "journal" | "dream";
type AutoFilter = MemoryType | "all";

const TABS: { id: Tab; label: string; icon: typeof Database }[] = [
  { id: "facts", label: "Facts", icon: Database },
  { id: "auto", label: "Auto Memory", icon: Brain },
  { id: "journal", label: "Journal", icon: FileText },
  { id: "dream", label: "Dreaming", icon: Sparkles },
];

const AUTO_FILTERS: { id: AutoFilter; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "user", label: "User" },
  { id: "feedback", label: "Feedback" },
  { id: "project", label: "Project" },
  { id: "reference", label: "Reference" },
];

function renderValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function shortTime(iso?: string | null): string {
  if (!iso) return "无";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${mo}/${day} ${hh}:${mm}`;
}

function TypePill({ type }: { type: string }) {
  const cls = type === "user"
    ? "text-model-gemini border-model-gemini/40"
    : type === "feedback"
      ? "text-warning border-warning/40"
      : type === "project"
        ? "text-model-deepseek border-model-deepseek/40"
        : "text-model-qwen border-model-qwen/40";
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] ${cls}`}>
      {type}
    </span>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="border border-border rounded-md bg-surface px-4 py-8 text-center text-sm text-fg-muted">
      {text}
    </div>
  );
}

export function MemoryView() {
  const [tab, setTab] = useState<Tab>("facts");
  const [summary, setSummary] = useState<MemorySummary | null>(null);
  const [facts, setFacts] = useState<LongTermFactInfo[]>([]);
  const [autoRows, setAutoRows] = useState<AutoMemoryInfo[]>([]);
  const [journals, setJournals] = useState<JournalInfo[]>([]);
  const [autoFilter, setAutoFilter] = useState<AutoFilter>("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [factKey, setFactKey] = useState("");
  const [factValue, setFactValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  async function refresh(nextFilter: AutoFilter = autoFilter) {
    setLoading(true);
    setError(null);
    try {
      const [summaryData, factData, autoData, journalData] = await Promise.all([
        getMemorySummary(),
        listMemoryFacts(),
        listAutoMemories(nextFilter),
        listJournals(),
      ]);
      setSummary(summaryData);
      setFacts(factData);
      setAutoRows(autoData);
      setJournals(journalData);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getMemorySummary(),
      listMemoryFacts(),
      listAutoMemories("all"),
      listJournals(),
    ])
      .then(([summaryData, factData, autoData, journalData]) => {
        if (cancelled) return;
        setSummary(summaryData);
        setFacts(factData);
        setAutoRows(autoData);
        setJournals(journalData);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleFilter(next: AutoFilter) {
    setAutoFilter(next);
    await refresh(next);
  }

  async function handleCreateFact() {
    const key = factKey.trim();
    const value = factValue.trim();
    if (!key || !value) return;
    setNotice(null);
    try {
      await createMemoryFact({ key, value });
      setFactKey("");
      setFactValue("");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDeleteFact(key: string) {
    if (!confirm(`删除 fact: ${key}？`)) return;
    try {
      await deleteMemoryFact(key);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleRebuildIndex() {
    setNotice(null);
    try {
      const result = await rebuildMemoryIndex();
      setNotice(`已重建索引：${result.path}`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNotice("已复制路径");
    } catch {
      setNotice("复制失败");
    }
  }

  const cards = useMemo(() => [
    {
      label: "Long-term Facts",
      value: summary?.facts_count ?? 0,
      sub: summary?.memory_db_path || "~/.codemesh/memory.db",
    },
    {
      label: "Auto Memories",
      value: summary?.auto_memory_count ?? 0,
      sub: summary?.auto_memory_dir || "~/.codemesh/auto_memory",
    },
    {
      label: "Session Journals",
      value: summary?.journal_count ?? 0,
      sub: summary?.journal_dir || "~/.codemesh/journal",
    },
    {
      label: "Dream Status",
      value: summary?.dream.can_dream ? "ready" : "idle",
      sub: summary?.dream.reason || "未加载",
    },
  ], [summary]);

  return (
    <div className="flex-1 min-h-0 bg-canvas flex flex-col">
      <div className="border-b border-border px-6 py-4 flex items-center justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-fg">记忆</h2>
          <p className="text-sm text-fg-muted">
            查看会影响模型的长期事实、自动抽取的 Markdown 记忆和 dreaming 状态。
          </p>
        </div>
        <button
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-sm text-fg hover:bg-surface-hover disabled:opacity-60"
        >
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          刷新
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5 space-y-5">
        {error && (
          <div className="rounded-md border border-error/50 bg-error/10 px-4 py-3 text-sm text-error">
            {error}
          </div>
        )}
        {notice && (
          <div className="rounded-md border border-border bg-surface px-4 py-3 text-sm text-fg-muted">
            {notice}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          {cards.map((card) => (
            <div key={card.label} className="rounded-md border border-border bg-surface p-4">
              <div className="text-xs uppercase tracking-wide text-fg-subtle">{card.label}</div>
              <div className="mt-2 text-2xl font-semibold text-fg">{card.value}</div>
              <div className="mt-1 truncate font-mono text-xs text-fg-subtle" title={card.sub}>
                {card.sub}
              </div>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap gap-2 border-b border-border">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm transition-colors ${
                tab === id
                  ? "border-accent text-accent"
                  : "border-transparent text-fg-muted hover:text-fg"
              }`}
            >
              <Icon size={15} />
              {label}
            </button>
          ))}
        </div>

        {tab === "facts" && (
          <section className="space-y-4">
            <div className="rounded-md border border-border bg-surface p-4">
              <div className="grid grid-cols-1 md:grid-cols-[minmax(180px,260px)_1fr_auto] gap-3">
                <input
                  value={factKey}
                  onChange={(e) => setFactKey(e.target.value)}
                  placeholder="key，例如 reply_language"
                  className="rounded-md border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent"
                />
                <input
                  value={factValue}
                  onChange={(e) => setFactValue(e.target.value)}
                  placeholder="value，例如 中文回复"
                  className="rounded-md border border-border bg-canvas px-3 py-2 text-sm text-fg outline-none focus:border-accent"
                />
                <button
                  onClick={() => void handleCreateFact()}
                  disabled={!factKey.trim() || !factValue.trim()}
                  className="inline-flex items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-canvas hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <Plus size={15} />
                  保存
                </button>
              </div>
            </div>

            {facts.length === 0 ? (
              <EmptyState text="当前没有 SQLite long-term facts。" />
            ) : (
              <div className="overflow-hidden rounded-md border border-border">
                <table className="w-full border-collapse text-sm">
                  <thead className="bg-surface text-left text-fg-muted">
                    <tr>
                      <th className="w-64 px-3 py-2 font-medium">Key</th>
                      <th className="px-3 py-2 font-medium">Value</th>
                      <th className="w-16 px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {facts.map((fact) => (
                      <tr key={fact.key} className="border-t border-border bg-canvas">
                        <td className="px-3 py-3 align-top font-mono text-xs text-fg">
                          {fact.key}
                        </td>
                        <td className="px-3 py-3 align-top">
                          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-fg-muted">
                            {renderValue(fact.value)}
                          </pre>
                        </td>
                        <td className="px-3 py-3 align-top">
                          <button
                            onClick={() => void handleDeleteFact(fact.key)}
                            className="rounded p-1.5 text-fg-subtle hover:bg-surface hover:text-error"
                            title="删除 fact"
                          >
                            <Trash2 size={15} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {tab === "auto" && (
          <section className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {AUTO_FILTERS.map((f) => (
                <button
                  key={f.id}
                  onClick={() => void handleFilter(f.id)}
                  className={`rounded-md border px-3 py-1.5 text-sm ${
                    autoFilter === f.id
                      ? "border-accent bg-accent/10 text-accent"
                      : "border-border bg-surface text-fg-muted hover:text-fg"
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>
            {autoRows.length === 0 ? (
              <EmptyState text="当前没有 auto_memory Markdown 条目。" />
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {autoRows.map((row) => (
                  <article key={row.path} className="rounded-md border border-border bg-surface p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <TypePill type={row.type} />
                          {row.indexed && (
                            <span className="text-[11px] text-success">indexed</span>
                          )}
                        </div>
                        <h3 className="mt-2 truncate text-sm font-semibold text-fg">{row.name}</h3>
                        <p className="mt-1 text-sm text-fg-muted">{row.description}</p>
                      </div>
                      <button
                        onClick={() => void copy(row.path)}
                        className="rounded p-1.5 text-fg-subtle hover:bg-canvas hover:text-fg"
                        title="复制路径"
                      >
                        <Clipboard size={15} />
                      </button>
                    </div>
                    <div className="mt-3">
                      <MemoryMarkdownPreview text={row.preview} />
                    </div>
                    <div className="mt-3 flex items-center justify-between gap-3 text-xs text-fg-subtle">
                      <span>{shortTime(row.updated_at)}</span>
                      <span className="truncate font-mono" title={row.path}>{row.path}</span>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        )}

        {tab === "journal" && (
          <section className="space-y-3">
            {journals.length === 0 ? (
              <EmptyState text="当前没有 session journal 条目。" />
            ) : (
              journals.map((row) => (
                <article key={row.path} className="rounded-md border border-border bg-surface p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-fg">{row.name}</h3>
                      <div className="mt-1 text-xs text-fg-subtle">{shortTime(row.created_at)}</div>
                    </div>
                    <button
                      onClick={() => void copy(row.path)}
                      className="rounded p-1.5 text-fg-subtle hover:bg-canvas hover:text-fg"
                      title="复制路径"
                    >
                      <Clipboard size={15} />
                    </button>
                  </div>
                  <div className="mt-3">
                    <MemoryMarkdownPreview text={row.preview} clamp={false} />
                  </div>
                  <div className="mt-3 truncate font-mono text-xs text-fg-subtle" title={row.path}>
                    {row.path}
                  </div>
                </article>
              ))
            )}
          </section>
        )}

        {tab === "dream" && (
          <section className="space-y-4">
            <div className="rounded-md border border-border bg-surface p-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                <div>
                  <div className="text-fg-subtle">状态</div>
                  <div className="mt-1 text-fg">
                    {summary?.dream.can_dream ? "可触发" : "未满足门控"}
                  </div>
                </div>
                <div>
                  <div className="text-fg-subtle">原因</div>
                  <div className="mt-1 text-fg">{summary?.dream.reason || "未加载"}</div>
                </div>
                <div>
                  <div className="text-fg-subtle">Memory entries</div>
                  <div className="mt-1 text-fg">{summary?.dream.memory_entries ?? 0}</div>
                </div>
                <div>
                  <div className="text-fg-subtle">上次 dream</div>
                  <div className="mt-1 text-fg">{shortTime(summary?.dream.last_dream_at)}</div>
                </div>
                <div>
                  <div className="text-fg-subtle">Lock</div>
                  <div className="mt-1 text-fg">
                    {summary?.dream.lock_present ? "存在 .consolidate-lock" : "无锁"}
                  </div>
                </div>
              </div>
              <div className="mt-4">
                <button
                  onClick={() => void handleRebuildIndex()}
                  className="inline-flex items-center gap-2 rounded-md border border-border bg-canvas px-3 py-2 text-sm text-fg hover:bg-surface-hover"
                >
                  <ListRestart size={15} />
                  重建 MEMORY.md 索引
                </button>
              </div>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
