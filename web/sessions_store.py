"""
Sessions / Messages 持久化存储（Phase 5）。

SQLite 风格沿用 memory/long_term.py：aiosqlite + ~/.codemesh/ 隐藏目录。
独立文件 web_sessions.db 避免污染 long_term memory.db。

两张表：
  sessions          会话 metadata
  session_messages  消息（user / assistant，含 toolCalls JSON）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite


DB_DIR = Path.home() / ".codemesh"
DB_PATH = DB_DIR / "web_sessions.db"


def _now_iso() -> str:
    """ISO datetime UTC，给 SQLite TEXT 列存。"""
    return datetime.now(timezone.utc).isoformat()


class SessionsStore:
    """异步 SQLite CRUD。每个方法一次性 connect+commit，简化错误处理。"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        """建表（IF NOT EXISTS 幂等）。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id         TEXT PRIMARY KEY,
                    title      TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    model      TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    tool_calls  TEXT,
                    model       TEXT,
                    cost_rmb    REAL,
                    duration_ms INTEGER,
                    created_at  TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_msgs_sid "
                "ON session_messages(session_id)"
            )
            await db.commit()

    # ─────────────── Sessions CRUD ───────────────

    async def create_session(self, title: str) -> dict[str, Any]:
        """新建会话返回完整 metadata dict。"""
        sid = str(uuid.uuid4())
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, title, now, now),
            )
            await db.commit()
        return {
            "id": sid,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "model": None,
            "message_count": 0,
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        """全部 sessions 按 updated_at desc 排序，附带 message_count。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at, s.model,
                       (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.id) AS msg_count
                FROM sessions s
                ORDER BY s.updated_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "model": r["model"],
                "message_count": r["msg_count"],
            }
            for r in rows
        ]

    async def get_session(self, sid: str) -> Optional[dict[str, Any]]:
        """单个 session 详情；不存在返回 None。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, title, created_at, updated_at, model FROM sessions WHERE id = ?",
                (sid,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            count_cur = await db.execute(
                "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
                (sid,),
            )
            count = (await count_cur.fetchone())[0]
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "model": row["model"],
            "message_count": count,
        }

    async def delete_session(self, sid: str) -> bool:
        """删除 session + 关联 messages（手动 cascade）。返回是否删到。"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
            cursor2 = await db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            await db.commit()
            return cursor2.rowcount > 0

    async def update_session(self, sid: str, *, title: Optional[str] = None, model: Optional[str] = None) -> None:
        """更新 title / model + 永远刷新 updated_at。空更新也会刷时间戳。"""
        fields = ["updated_at = ?"]
        values: list[Any] = [_now_iso()]
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if model is not None:
            fields.append("model = ?")
            values.append(model)
        values.append(sid)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )
            await db.commit()

    # ─────────────── Messages CRUD ───────────────

    async def append_message(
        self,
        sid: str,
        role: str,
        content: str,
        *,
        tool_calls: Optional[list[dict]] = None,
        model: Optional[str] = None,
        cost_rmb: Optional[float] = None,
        duration_ms: Optional[int] = None,
    ) -> int:
        """追加一条消息，返回 messages.id（自增主键）。"""
        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO session_messages
                    (session_id, role, content, tool_calls, model, cost_rmb, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, role, content, tc_json, model, cost_rmb, duration_ms, now),
            )
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, sid),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_messages(self, sid: str) -> list[dict[str, Any]]:
        """按 created_at asc 拿 session 的全部 messages。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, role, content, tool_calls, model, cost_rmb, duration_ms, created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (sid,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else None,
                "model": r["model"],
                "cost_rmb": r["cost_rmb"],
                "duration_ms": r["duration_ms"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


# 模块级单例（lazy init）
_store: Optional[SessionsStore] = None


def get_sessions_store() -> SessionsStore:
    """FastAPI Depends 入口。"""
    global _store
    if _store is None:
        _store = SessionsStore()
    return _store
