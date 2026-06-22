"""
MemoryStore for the Web Memory Panel.

This is a read-mostly facade over CodeMesh's existing memory files:
  - ~/.codemesh/memory.db          SQLite facts used by remember_fact
  - ~/.codemesh/auto_memory/*.md   auto-extracted memory cards
  - ~/.codemesh/journal/*.md       session journal entries

The store deliberately does not run LLM extraction or dreaming by itself. Routes
can expose state safely without surprising the user with model calls.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from feedback.dreamer import rebuild_memory_index
from feedback.session_journal import (
    DEFAULT_MIN_HOURS,
    DEFAULT_MIN_SCAN_MINUTES,
    DEFAULT_MIN_SESSIONS,
    LOCK_FILENAME,
    LOCK_STALE_HOURS,
)
from memory.auto_extract import DEFAULT_AUTO_MEMORY_DIR
from memory.long_term import DB_PATH


DEFAULT_CODEMESH_DIR = Path.home() / ".codemesh"
DEFAULT_JOURNAL_DIR = DEFAULT_CODEMESH_DIR / "journal"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Small frontmatter parser for CodeMesh-generated markdown files."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'\"")
    return fields, parts[2].lstrip("\n")


def _preview(text: str, max_chars: int = 500) -> str:
    """Whitespace-normalized preview for list rows."""
    rendered = " ".join(text.strip().split())
    return rendered[:max_chars]


def _iso(ts: float) -> str:
    """UTC-ish ISO string from file mtime. Good enough for local dashboard rows."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _read_timestamp(path: Path) -> float | None:
    """Read a timestamp sidecar without treating bad files as fatal."""
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class MemoryStore:
    """Read/write access for CodeMesh memory dashboard endpoints."""

    def __init__(
        self,
        *,
        db_path: Path = DB_PATH,
        auto_memory_dir: Path = DEFAULT_AUTO_MEMORY_DIR,
        journal_dir: Path = DEFAULT_JOURNAL_DIR,
    ):
        self.db_path = db_path
        self.auto_memory_dir = auto_memory_dir
        self.journal_dir = journal_dir

    async def init(self) -> None:
        """Create storage roots and SQLite table if they do not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.auto_memory_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def list_facts(self) -> list[dict[str, Any]]:
        """Return long-term KV facts sorted by key."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT key, value FROM kv ORDER BY key") as cursor:
                rows = await cursor.fetchall()
        out: list[dict[str, Any]] = []
        for key, raw in rows:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            out.append({"key": key, "value": value})
        return out

    async def save_fact(self, key: str, value: Any) -> dict[str, Any]:
        """Insert or replace one long-term fact."""
        clean_key = key.strip()
        if not clean_key:
            raise ValueError("key cannot be blank")
        serialized = json.dumps(value, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (clean_key, serialized),
            )
            await db.commit()
        return {"key": clean_key, "value": value}

    async def delete_fact(self, key: str) -> bool:
        """Delete a long-term fact by key."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM kv WHERE key = ?", (key,))
            await db.commit()
            return cursor.rowcount > 0

    def list_auto_memories(self, type_filter: Optional[str] = None) -> list[dict[str, Any]]:
        """Parse auto_memory markdown files, newest first."""
        index_text = ""
        index_path = self.auto_memory_dir / "MEMORY.md"
        if index_path.exists():
            try:
                index_text = index_path.read_text(encoding="utf-8")
            except OSError:
                index_text = ""

        rows: list[dict[str, Any]] = []
        for path in self.auto_memory_dir.glob("*.md"):
            if path.name == "MEMORY.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
                stat = path.stat()
            except OSError:
                continue
            fields, body = _parse_frontmatter(text)
            memory_type = fields.get("type", "user")
            if type_filter and memory_type != type_filter:
                continue
            rows.append({
                "name": fields.get("name", path.stem),
                "description": fields.get("description", ""),
                "type": memory_type,
                "path": str(path),
                "updated_at": _iso(stat.st_mtime),
                "preview": _preview(body),
                "indexed": f"({path.name})" in index_text,
            })
        rows.sort(key=lambda r: (r["updated_at"], r["name"]), reverse=True)
        return rows

    def list_journals(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent session journal markdown files, newest first."""
        rows: list[dict[str, Any]] = []
        files = sorted(
            self.journal_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
                stat = path.stat()
            except OSError:
                continue
            fields, body = _parse_frontmatter(text)
            rows.append({
                "name": fields.get("task", path.stem),
                "path": str(path),
                "created_at": _iso(stat.st_mtime),
                "preview": _preview(body or text),
            })
        return rows

    def dream_status(self) -> dict[str, Any]:
        """Expose dreamer gate inputs without triggering an LLM call."""
        memory_entries = len([
            p for p in self.auto_memory_dir.glob("*.md") if p.name != "MEMORY.md"
        ])
        lock_path = self.auto_memory_dir / ".consolidate-lock"
        last_dream = self.auto_memory_dir / ".last_dream"
        last_scan = self.auto_memory_dir / ".last_scan"
        now = time.time()

        can_dream = True
        reason = "all gates passed"

        last_dream_ts = _read_timestamp(last_dream)
        if last_dream_ts is not None:
            hours_since = (now - last_dream_ts) / 3600
            if hours_since < DEFAULT_MIN_HOURS:
                can_dream = False
                reason = f"only {hours_since:.1f}h since last dream"

        if can_dream:
            last_scan_ts = _read_timestamp(last_scan)
            if last_scan_ts is not None:
                minutes_since = (now - last_scan_ts) / 60
                if minutes_since < DEFAULT_MIN_SCAN_MINUTES:
                    can_dream = False
                    reason = f"only {minutes_since:.1f}min since last scan"

        if can_dream and memory_entries < DEFAULT_MIN_SESSIONS:
            can_dream = False
            reason = f"only {memory_entries} memory entries (< {DEFAULT_MIN_SESSIONS})"

        if can_dream and self._lock_exists_and_alive():
            can_dream = False
            reason = "lock held by alive PID"

        return {
            "can_dream": can_dream,
            "reason": reason,
            "memory_entries": memory_entries,
            "lock_present": lock_path.exists(),
            "last_dream_at": _iso(last_dream_ts) if last_dream_ts is not None else None,
        }

    def _lock_exists_and_alive(self) -> bool:
        """Read-only version of Dreamer lock detection."""
        lock = self.auto_memory_dir / LOCK_FILENAME
        if not lock.exists():
            return False
        try:
            mtime = lock.stat().st_mtime
            if (time.time() - mtime) / 3600 > LOCK_STALE_HOURS:
                return False
        except OSError:
            return False
        try:
            content = lock.read_text(encoding="utf-8").strip()
            pid = int(content.split(":", 1)[0])
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, ProcessLookupError):
            return False

    async def summary(self) -> dict[str, Any]:
        """Dashboard counts and paths."""
        facts = await self.list_facts()
        auto_count = len([
            p for p in self.auto_memory_dir.glob("*.md") if p.name != "MEMORY.md"
        ])
        journal_count = len(list(self.journal_dir.glob("*.md")))
        dream = self.dream_status()
        return {
            "facts_count": len(facts),
            "auto_memory_count": auto_count,
            "journal_count": journal_count,
            "memory_db_path": str(self.db_path),
            "auto_memory_dir": str(self.auto_memory_dir),
            "journal_dir": str(self.journal_dir),
            "dream": dream,
        }

    def rebuild_index(self) -> dict[str, str]:
        """Rebuild auto_memory/MEMORY.md from existing markdown cards."""
        path = rebuild_memory_index(self.auto_memory_dir)
        return {"path": str(path)}


_default: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    global _default
    if _default is None:
        _default = MemoryStore()
    return _default
