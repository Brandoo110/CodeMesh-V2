"use client";

/**
 * 侧栏（Phase 5：真接 SQLite 历史）。
 *
 * 启动时拉 /api/sessions，按 updated_at 倒序展示。
 * 点击 session → 切到该会话；点击 "+ 新对话" → POST 创建 + 切到新会话。
 *
 * 删除：hover 项右侧 trash 按钮（防误删用 confirm）。
 */

import { Plus, MessageSquare, Settings, Trash2, Pencil } from "lucide-react";
import { useEffect, useState } from "react";
import {
  listSessions as fetchSessions,
  createSession,
  deleteSession,
  updateSession,
} from "@/lib/api";
import { useStore } from "@/lib/store";
import type { SessionInfo } from "@/lib/types";

function shortTime(iso: string): string {
  // ISO → 月/日 时:分（去年的显示年份）
  const d = new Date(iso);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return sameYear ? `${mo}/${day} ${hh}:${mm}` : `${d.getFullYear()}/${mo}/${day}`;
}

export function Sidebar() {
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const sessions = useStore((s) => s.sessions);
  const setSessions = useStore((s) => s.setSessions);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const setCurrentSessionId = useStore((s) => s.setCurrentSessionId);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  // 启动时拉 sessions
  useEffect(() => {
    fetchSessions()
      .then(setSessions)
      .catch((e) => console.error("listSessions failed:", e));
  }, [setSessions]);

  async function handleNewSession() {
    try {
      const s = await createSession("新对话");
      setSessions([s, ...sessions]);
      setCurrentSessionId(s.id);
    } catch (e) {
      console.error("createSession failed:", e);
    }
  }

  async function handleDelete(e: React.MouseEvent, id: string) {
    e.stopPropagation();
    if (!confirm("确认删除这个对话？历史消息将一并删除")) return;
    try {
      await deleteSession(id);
      setSessions(sessions.filter((s) => s.id !== id));
      if (currentSessionId === id) setCurrentSessionId(null);
    } catch (err) {
      console.error("deleteSession failed:", err);
    }
  }

  function startRename(e: React.MouseEvent, session: SessionInfo) {
    e.stopPropagation();
    setEditingId(session.id);
    setDraftTitle(session.title);
  }

  async function commitRename(id: string) {
    const nextTitle = draftTitle.trim();
    const current = sessions.find((s) => s.id === id);
    if (!current) return;

    if (!nextTitle || nextTitle === current.title) {
      setEditingId(null);
      setDraftTitle("");
      return;
    }

    try {
      const updated = await updateSession(id, { title: nextTitle });
      setSessions(sessions.map((s) => (s.id === id ? updated : s)));
    } catch (err) {
      console.error("updateSession failed:", err);
    } finally {
      setEditingId(null);
      setDraftTitle("");
    }
  }

  function cancelRename() {
    setEditingId(null);
    setDraftTitle("");
  }

  if (!sidebarOpen) return null;

  return (
    <aside className="w-60 flex-shrink-0 border-r border-border bg-surface flex flex-col h-full">
      {/* 顶部：新对话按钮 */}
      <div className="p-3 border-b border-border">
        <button
          className="w-full flex items-center gap-2 px-3 py-2 rounded-md bg-canvas hover:bg-surface-hover text-fg text-sm transition-colors"
          onClick={handleNewSession}
        >
          <Plus size={16} />
          <span>新对话</span>
        </button>
      </div>

      {/* 历史列表 */}
      <div className="flex-1 overflow-y-auto p-2">
        <div className="text-xs text-fg-subtle px-2 py-1.5 select-none">
          最近对话
        </div>
        {sessions.length === 0 ? (
          <div className="text-xs text-fg-subtle px-2 py-3 text-center italic">
            暂无历史
          </div>
        ) : (
          sessions.map((s) => {
            const active = s.id === currentSessionId;
            const editing = s.id === editingId;
            return (
              <div
                key={s.id}
                role="button"
                className={`group flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors ${
                  active
                    ? "bg-surface-hover border-l-2 border-l-accent"
                    : "hover:bg-surface-hover"
                }`}
                onClick={() => {
                  if (!editing) setCurrentSessionId(s.id);
                }}
              >
                <MessageSquare size={14} className="text-fg-muted flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  {editing ? (
                    <input
                      value={draftTitle}
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      onBlur={() => void commitRename(s.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void commitRename(s.id);
                        } else if (e.key === "Escape") {
                          e.preventDefault();
                          cancelRename();
                        }
                      }}
                      className="w-full rounded border border-accent bg-canvas px-1.5 py-0.5 text-sm text-fg outline-none"
                    />
                  ) : (
                    <div className="text-sm text-fg truncate">{s.title}</div>
                  )}
                  <div className="text-xs text-fg-subtle truncate">
                    {shortTime(s.updated_at)}
                    {s.message_count > 0 && ` · ${s.message_count} 条`}
                  </div>
                </div>
                {!editing && (
                  <button
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-canvas text-fg-subtle hover:text-fg transition-opacity"
                    onClick={(e) => startRename(e, s)}
                    title="重命名"
                  >
                    <Pencil size={12} />
                  </button>
                )}
                <button
                  className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-canvas text-fg-subtle hover:text-error transition-opacity"
                  onClick={(e) => handleDelete(e, s.id)}
                  title="删除"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            );
          })
        )}
      </div>

      {/* 底部：设置 */}
      <div className="p-3 border-t border-border">
        <button
          className="w-full flex items-center gap-2 px-3 py-2 rounded-md hover:bg-surface-hover text-fg-muted text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60"
          disabled
          title="暂未启用"
        >
          <Settings size={16} />
          <span>设置</span>
        </button>
      </div>
    </aside>
  );
}
