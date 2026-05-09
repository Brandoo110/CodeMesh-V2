"""
Dreamer：真正的 L6 dreaming——4 阶段记忆巩固（CC 同款语义）
==================================================================

【这是什么】
对照 troyhua 公众号 + Claude Code source map（cli.js 里的 `DreamTask` /
`tengu_auto_dream_*`），CC 的 dreaming 干的是：
**回去整理已有记忆**——orientation / gather / consolidate / prune 4 阶段。

之前的 `dreamer.py`（已改名 `session_journal.py`）是 per-session 写新条目，
那是 **L5 的活**，不是 L6。L6 真活在这文件里。

【4 阶段流程】（对照 troyhua 公众号 / cli.js consolidate_lock 段）

  Phase 1 Orientation:
    扫 `~/.codemesh/auto_memory/` 列出当前所有结构化记忆 + 读 MEMORY.md 索引
    为后续阶段提供"现在记了什么"的全景

  Phase 2 Gather:
    grep 最近的 journal/ 条目 + 已写入的 auto_memory/ 找候选信号
    （CC 是 grep 完整 session transcript；CodeMesh 简化为已写入的 markdown）
    输出: 候选条目列表

  Phase 3 Consolidate:
    把所有现有 auto_memory 条目交给 LLM，让它给出"操作计划"：
      - DELETE name: <过时 / 错误 / 矛盾>
      - MERGE source -> target: <合并理由>
      - REWRITE name: <新内容>（去除相对日期、修正措辞）
    然后机械地执行

  Phase 4 Prune & Index:
    重建 MEMORY.md 索引——按 LRU 排序（mtime 最新在上），删除已经 unlink 的条目

【与 session_journal 的协作】
两者通过共享 5 门门控（特别是 Gate 5: `.consolidate-lock`）协调：
  - session_journal 默认 min_hours=0、min_sessions=1（每会话都写）
  - dreamer 默认 min_hours=24、min_sessions=5（一天一次）
  - 两者抢同一把锁——同一时刻只有一个能进入临界区

【与 OH/CC 的差异】
- 同：4 阶段流程、门控阈值（24h / 5 session / 锁文件）
- 减：不读 session transcript（CC 完整，CodeMesh 没 transcript 持久化）
  替代：读已写入的 journal/ 和 auto_memory/——粗粒度但够 demo
- 减：不做 GrowthBook 远程开关（教学项目用不到）
- 加：教学注释密度

【面试故事】
"我做完 dreamer 第一版后发现自己实际写的是 per-session 叙事，不是 CC dreaming
本身——CC dreaming 是 consolidation。所以改名 session_journal，重新写了真 dreamer
做 4 阶段巩固：扫 → grep 信号 → 让 LLM 给操作计划 → 机械执行 + 重建索引。
两者通过共享 5 门门控（同一把锁）协调，session_journal 高频低成本，dreamer 低频高质量。
这种'实现完发现命名错主动改'的迭代是工程师常态，比'装作一开始就对'诚实。"
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# 共享 session_journal 的常量与门控（避免重复定义）
from feedback.session_journal import (
    DEFAULT_MIN_HOURS,
    DEFAULT_MIN_SCAN_MINUTES,
    DEFAULT_MIN_SESSIONS,
    LOCK_FILENAME,
    LOCK_STALE_HOURS,
)
from memory.auto_extract import (
    DEFAULT_AUTO_MEMORY_DIR,
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    MemoryEntry,
    parse_entries,
)


# Summarizer 类型：吃 prompt 返回 LLM 文本
ConsolidateSummarizer = Callable[[str], Awaitable[str]]


# ─────────────────────────── 数据结构 ───────────────────────────


@dataclass
class ExistingMemory:
    """orientation 阶段读出的一条已存在记忆。"""
    path: Path
    name: str
    description: str
    type: str
    body: str
    mtime: float


@dataclass
class ConsolidationPlan:
    """LLM 给出的"操作计划"——机械执行的工单。"""
    deletes: list[str]                # name 列表
    merges: list[tuple[str, str]]     # (source_name, target_name)
    rewrites: dict[str, str]          # name → 新 body


# ─────────────────────────── prompt 模板 ───────────────────────────

CONSOLIDATE_PROMPT = """\
你是记忆库的"整理工"。下面是一个 agent 跨多次会话累积的结构化记忆条目列表，
请审视所有条目，找出：
  - 过时的（如指向已不存在的 issue / 已完成的任务）
  - 矛盾的（同一个事实有两条相互冲突）
  - 重复的（两条说的是同一件事）

输出"操作计划"用以下格式（**只输出 PLAN 块，不要解释**）：

---PLAN---
DELETE name1
DELETE name2
MERGE source_name -> target_name
REWRITE name3
<name3 的新 body 正文，包含 **Why:** 和 **How to apply:** 双段，写完接下一行 ---END--->
---END---
REWRITE name4
<name4 的新 body 正文>
---END---
---PLAN---

如果没有要做的事，输出空 ---PLAN---/---PLAN--- 块即可。

铁律：
1. **保守**：拿不准就别动，宁可保留也不要误删
2. 只动**明显**的过时 / 矛盾 / 重复
3. REWRITE 时务必保留 Why + How 双段，用绝对日期（"2026-04-15" 不是"上周"）

──── 当前记忆条目（共 {n} 条）────

{entries}
"""


# ─────────────────────────── 主类 ───────────────────────────


class Dreamer:
    """
    真正的 L6 dreaming——4 阶段记忆巩固。

    用法：
        d = Dreamer(summarizer=my_async_fn)
        result = await d.dream()    # 返回 dict: {"deleted": [...], "merged": [...], ...}
    """

    def __init__(
        self,
        memory_dir: Path = DEFAULT_AUTO_MEMORY_DIR,
        journal_dir: Path | None = None,
        summarizer: ConsolidateSummarizer | None = None,
        *,
        enabled: bool = True,
        min_hours: float = DEFAULT_MIN_HOURS,
        min_scan_minutes: float = DEFAULT_MIN_SCAN_MINUTES,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
    ):
        self.memory_dir = memory_dir
        # journal_dir 可选——dreamer 主要操作 auto_memory；journal 仅作为辅助信号源
        self.journal_dir = journal_dir or (Path.home() / ".codemesh" / "journal")
        self.summarizer = summarizer
        self.enabled = enabled
        self.min_hours = min_hours
        self.min_scan_minutes = min_scan_minutes
        self.min_sessions = min_sessions
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ─── 5 门触发判断（与 session_journal 共享同一套约定） ───

    def should_dream(self, *, force: bool = False) -> tuple[bool, str]:
        """5 门门控；与 SessionJournal 共享 .consolidate-lock 文件。"""
        if not self.enabled:
            return False, "disabled"
        if force:
            if self._lock_exists_and_alive():
                return False, "lock held by alive PID"
            return True, "forced"

        last_dream_path = self.memory_dir / ".last_dream"
        last_scan_path = self.memory_dir / ".last_scan"
        now = time.time()

        # Gate 2: Time
        if last_dream_path.exists():
            try:
                last = float(last_dream_path.read_text(encoding="utf-8").strip())
                hours_since = (now - last) / 3600
                if hours_since < self.min_hours:
                    return False, f"only {hours_since:.1f}h since last dream"
            except (OSError, ValueError):
                pass

        # Gate 3: Scan throttle
        if last_scan_path.exists():
            try:
                last = float(last_scan_path.read_text(encoding="utf-8").strip())
                minutes_since = (now - last) / 60
                if minutes_since < self.min_scan_minutes:
                    return False, f"only {minutes_since:.1f}min since last scan"
            except (OSError, ValueError):
                pass
        try:
            last_scan_path.write_text(str(now), encoding="utf-8")
        except OSError:
            pass

        # Gate 4: Session count（按 auto_memory/ 目录里 .md 文件总数当 session 粗估）
        memory_files = list(self.memory_dir.glob("*.md"))
        # MEMORY.md 是索引不算
        session_count = len([p for p in memory_files if p.name != "MEMORY.md"])
        if session_count < self.min_sessions:
            return False, f"only {session_count} memory entries (< {self.min_sessions})"

        # Gate 5: Lock
        if self._lock_exists_and_alive():
            return False, "lock held by alive PID"
        return True, "all gates passed"

    def _lock_path(self) -> Path:
        return self.memory_dir / LOCK_FILENAME

    def _lock_exists_and_alive(self) -> bool:
        lock = self._lock_path()
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

    def _acquire_lock(self) -> bool:
        if self._lock_exists_and_alive():
            return False
        try:
            self._lock_path().write_text(f"{os.getpid()}:{time.time()}", encoding="utf-8")
            return True
        except OSError:
            return False

    def _release_lock(self) -> None:
        try:
            self._lock_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ─── Phase 1: Orientation ───

    def orient(self) -> list[ExistingMemory]:
        """扫 memory_dir 下所有 .md（除 MEMORY.md），解析 frontmatter。"""
        result: list[ExistingMemory] = []
        for p in self.memory_dir.glob("*.md"):
            if p.name == "MEMORY.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
                mtime = p.stat().st_mtime
            except OSError:
                continue
            fields, body = _parse_frontmatter(text)
            if not fields.get("name"):
                continue
            result.append(ExistingMemory(
                path=p,
                name=fields.get("name", p.stem),
                description=fields.get("description", ""),
                type=fields.get("type", "user"),
                body=body,
                mtime=mtime,
            ))
        # 新文件在前，便于 LLM 看到时间序
        result.sort(key=lambda m: m.mtime, reverse=True)
        return result

    # ─── Phase 2: Gather ───

    def gather_signals(self, memories: list[ExistingMemory]) -> str:
        """
        收集"额外信号"——主要从 journal/ 里翻最近条目作为 LLM 的上下文。
        简化版：直接拼最近 5 条 journal 摘要。
        CC 完整版会 grep session transcript 找用户纠正、显式让记的事。
        """
        if not self.journal_dir.exists():
            return ""
        journals = sorted(
            (p for p in self.journal_dir.glob("*.md")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        if not journals:
            return ""
        snippets = []
        for p in journals:
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            snippets.append(f"## {p.stem}\n{text[:500]}")
        return "\n\n".join(snippets)

    # ─── Phase 3: Consolidate ───

    async def consolidate(self, memories: list[ExistingMemory]) -> ConsolidationPlan:
        """让 LLM 给出操作计划。失败时返回空计划。"""
        if self.summarizer is None or not memories:
            return ConsolidationPlan([], [], {})

        entries_text = "\n\n".join(
            f"### {m.name} ({m.type})\n描述: {m.description}\n更新: {time.strftime('%Y-%m-%d', time.localtime(m.mtime))}\n正文:\n{m.body[:600]}"
            for m in memories
        )
        prompt = CONSOLIDATE_PROMPT.format(n=len(memories), entries=entries_text)

        try:
            raw = await self.summarizer(prompt)
        except Exception as e:
            print(f"[dreamer] consolidate LLM failed ({type(e).__name__}: {e}); skip")
            return ConsolidationPlan([], [], {})

        return parse_consolidation_plan(raw)

    # ─── Phase 4: Prune & Index ───

    def apply_plan(self, plan: ConsolidationPlan, memories: list[ExistingMemory]) -> dict:
        """
        机械地执行 plan：删除 / 合并 / 重写。然后重建 MEMORY.md 索引。
        返回操作摘要 dict 给调用方看。
        """
        by_name = {m.name: m for m in memories}
        deleted: list[str] = []
        merged: list[tuple[str, str]] = []
        rewritten: list[str] = []

        # 1. DELETE
        for name in plan.deletes:
            if name in by_name:
                try:
                    by_name[name].path.unlink(missing_ok=True)
                    deleted.append(name)
                except OSError:
                    pass

        # 2. MERGE source -> target
        for src, dst in plan.merges:
            if src in by_name and dst in by_name and src != dst:
                # 简化：只删 source；不合并 body（避免 LLM 没给完就乱合）
                try:
                    by_name[src].path.unlink(missing_ok=True)
                    merged.append((src, dst))
                except OSError:
                    pass

        # 3. REWRITE
        for name, new_body in plan.rewrites.items():
            if name in by_name:
                m = by_name[name]
                try:
                    new_text = (
                        f"---\n"
                        f"name: {m.name}\n"
                        f"description: {m.description}\n"
                        f"type: {m.type}\n"
                        f"---\n\n"
                        f"{new_body.strip()}\n"
                    )
                    m.path.write_text(new_text, encoding="utf-8")
                    rewritten.append(name)
                except OSError:
                    pass

        # 4. 重建 MEMORY.md
        rebuild_memory_index(self.memory_dir)

        return {
            "deleted": deleted,
            "merged": merged,
            "rewritten": rewritten,
        }

    # ─── 主入口 ───

    async def dream(self, *, force: bool = False) -> dict | None:
        """
        4 阶段巩固主入口。失败 / 门控不通过返回 None。
        成功返回 dict({deleted, merged, rewritten})。
        """
        ok, reason = self.should_dream(force=force)
        if not ok:
            print(f"[dreamer] skip: {reason}")
            return None
        if not self._acquire_lock():
            print("[dreamer] skip: failed to acquire lock")
            return None

        try:
            # Phase 1
            memories = self.orient()
            if len(memories) < 2:
                # 少于 2 条没什么可整理
                print(f"[dreamer] only {len(memories)} memories; nothing to consolidate")
                return {"deleted": [], "merged": [], "rewritten": []}

            # Phase 2 (signals 进 prompt 略；本简化版没用上)
            _ = self.gather_signals(memories)

            # Phase 3
            plan = await self.consolidate(memories)

            # Phase 4
            summary = self.apply_plan(plan, memories)

            # 更新 .last_dream 时间戳
            (self.memory_dir / ".last_dream").write_text(str(time.time()), encoding="utf-8")

            print(
                f"[dreamer] consolidated: "
                f"{len(summary['deleted'])} deleted, "
                f"{len(summary['merged'])} merged, "
                f"{len(summary['rewritten'])} rewritten"
            )
            return summary
        finally:
            self._release_lock()


# ─────────────────────────── 工具函数 ───────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """从 markdown 文件 text 拆出 frontmatter dict + body。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fields = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip().strip("'\"")
    return fields, parts[2].lstrip("\n")


# 解析 LLM 输出的 PLAN 块
_PLAN_RE = re.compile(r"---PLAN---\s*(.*?)\s*---PLAN---", re.DOTALL)
_REWRITE_RE = re.compile(r"REWRITE\s+(\S+)\s*\n(.*?)\s*---END---", re.DOTALL)


def parse_consolidation_plan(raw: str) -> ConsolidationPlan:
    """把 LLM 输出的字符串 PLAN 块解析成结构化对象。"""
    deletes: list[str] = []
    merges: list[tuple[str, str]] = []
    rewrites: dict[str, str] = {}

    m = _PLAN_RE.search(raw)
    if not m:
        # LLM 没按格式输出 → 按最严格"啥都不做"处理
        return ConsolidationPlan([], [], {})

    block = m.group(1)
    # 先抠 REWRITE （它会跨多行）
    for rm in _REWRITE_RE.finditer(block):
        rewrites[rm.group(1).strip()] = rm.group(2).strip()
    # 单行命令：DELETE / MERGE
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("DELETE "):
            name = line[len("DELETE "):].strip()
            if name:
                deletes.append(name)
        elif line.startswith("MERGE "):
            # 格式: MERGE source -> target
            rest = line[len("MERGE "):].strip()
            if "->" in rest:
                src, _, dst = rest.partition("->")
                src, dst = src.strip(), dst.strip()
                if src and dst:
                    merges.append((src, dst))
    return ConsolidationPlan(deletes, merges, rewrites)


def rebuild_memory_index(memory_dir: Path) -> Path:
    """
    扫所有 memory_dir/*.md（除 MEMORY.md），重建 MEMORY.md 索引。
    按 mtime 倒序（最新在前）；遵守 200 行 / 25KB 上限。
    """
    index_path = memory_dir / "MEMORY.md"
    files = sorted(
        (p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    lines = ["# Memory Index", ""]
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fields, _ = _parse_frontmatter(text)
        name = fields.get("name", p.stem)
        desc = fields.get("description", "")
        line = f"- [{name}]({p.name}) — {desc}"
        # 检查上限
        candidate_total = sum(len(l) + 1 for l in lines) + len(line) + 1
        if len(lines) >= MAX_INDEX_LINES or candidate_total > MAX_INDEX_BYTES:
            break
        lines.append(line)
    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return index_path
