"""
SessionJournal：会话结束后写一条叙事复盘（CodeMesh 独有的 L5 叙事变体）
==========================================================================

【为什么改名（2026-05-09 晚）】
最初这文件叫 `dreamer.py`，受 Anthropic Dreaming research preview 启发。
但读 troyhua 公众号 + grep cli.js 后发现一个**实现破绽**：
  CC 的 Dreaming 干的是 **consolidation（回去整理已有记忆）**，不是
  "per-session 写新复盘" —— 后者其实是 L5 (Auto Memory Extraction) 的活。

所以改名 `session_journal.py`（叙事日志）。
**真正的 L6 dreaming 在 `feedback/dreamer.py`**——做 4 阶段：
orientation / gather / consolidate / prune。

【SessionJournal 干什么】
  1. session 结束时调便宜模型（doubao），写一条 4 段式 markdown 复盘
  2. 写到 `~/.codemesh/journal/<timestamp>-<slug>.md`，每条 ≤ 100 行
  3. 下次 `Harness.run(task)` 启动时，关键词 grep 出最相关的 top-K 条，
     拼到 system prompt 里当"过往经验"
  4. **L5 的叙事变体** —— 与 `auto_extract.py`（L5 结构化）并存：
     - auto_extract = "结构化事实"（4 类型 + Why/How）
     - session_journal = "叙事复盘"（任务/决策/踩坑/可复用）
     互补：结构化精确召回，叙事铺垫上下文。

【为什么不合并到 auto_extract】
两种召回需求不同：
  - 模型问"用户偏好" → 看 4 类型 frontmatter 一眼就知
  - 模型问"上次类似任务怎么做的" → 看叙事更直观
合并的话 prompt 复杂，frontmatter 索引意义大减。分开更清晰。

【设计选择】
  - 写文件而不是 SQLite：人能 cat 直观看
  - 关键词 grep 不向量检索：零依赖，量小够用
  - 同步 await：CLI 单进程，asyncio.run 退出取消 task
  - 5 门门控避免每会话写一条爆炸（见下）

【5 门触发（与真 dreamer 共享同一套门控约定）】
  Gate 1: Enabled         enabled=True 才放行
  Gate 2: Time            距上次 ≥ min_hours
  Gate 3: Scan throttle   距上次 scan ≥ min_scan_minutes
  Gate 4: Session count   pending ≥ min_sessions
  Gate 5: Lock            `.consolidate-lock` 不被持有

注：Gate 5 锁文件名 `.consolidate-lock` 与真 dreamer 共享——同时跑只能一个进入
临界区，避免抢着改 journal/ 和 auto_memory/ 的混乱。session_journal 默认门控
比 dreamer 宽松（min_hours=0、min_sessions=1）让每会话都能写。

【兼容性】
为不破坏调用方，类名仍是 `Dreamer`、`DreamSummarizer`、`DreamHit`，
但默认目录从 `~/.codemesh/dreams/` 改到 `~/.codemesh/journal/`。
新代码请用 `from feedback.session_journal import SessionJournal`（alias 提供）。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# summarizer 签名：吃 prompt（已经构造好的中文长文本），返回 markdown 摘要。
# 类型和 ShortTermMemory 的 summarizer 略不同（那个吃 messages 列表）。
DreamSummarizer = Callable[[str], Awaitable[str]]


# 默认存储位置：~/.codemesh/journal/（原 dreams/，2026-05-09 改名）
# 老代码用 DEFAULT_DREAMS_DIR 别名仍然指向新路径，兼容性。
DEFAULT_JOURNAL_DIR = Path.home() / ".codemesh" / "journal"
DEFAULT_DREAMS_DIR = DEFAULT_JOURNAL_DIR  # 旧名 alias，避免破坏导入

# 单条 dream 最多多少行（超过截断）。对齐 Anthropic 的 100 行约束 —— 这是
# memory store 里 "一条记忆 ≤ 100 行" 的设计语言。
MAX_DREAM_LINES = 100

# ─── 5 门触发常量（对照 CC cli.js 字面值） ───
DEFAULT_MIN_HOURS = 24      # Gate 2: 距上次 dream 至少 24h
DEFAULT_MIN_SCAN_MINUTES = 10  # Gate 3: 距上次 scan 至少 10min（throttle）
DEFAULT_MIN_SESSIONS = 5    # Gate 4: 累计 ≥ 5 个 session
LOCK_FILENAME = ".consolidate-lock"
LOCK_STALE_HOURS = 2        # 锁如果 ≥ 2h 没动且 PID 死了，视为 stale 可强夺


# ─────────────── prompt 模板 ───────────────

DREAM_PROMPT = """你刚刚帮助用户完成了一个任务。请回顾整个过程，输出一份**简短的中文复盘 markdown**，
让"未来的你"在遇到相似任务时能快速参考。

严格按以下 4 段式结构输出（只输出 markdown 正文，不要任何解释）：

### 任务
<用 1-2 句概括用户原始诉求 + 输入的上下文>

### 关键决策
<这次过程中做了什么关键判断 / 选择？为什么？最多 3 条要点>

### 踩坑 / 教训
<有没有走过弯路、误判、被工具/环境坑过？最多 3 条；没有就写"无">

### 可复用经验
<这次的哪些做法、模板、命令、参数下次可以直接用？写得越具体越好>

⚠️ 总长度控制在 60 行以内。重点是"未来能用"而不是"完整记录"。
不要写敬语、客套话、总结陈词；像在写工程笔记。

──────── 本次任务原始信息 ────────
任务：{task}

输出（agent 最终回复）：
{output}
"""


# ─────────────── 数据类 ───────────────


@dataclass
class DreamHit:
    """召回结果：一条匹配上的 dream 片段。"""
    path: Path
    score: int
    snippet: str

    def render(self) -> str:
        """拼到 system prompt 里时的展示形式。"""
        return f"## {self.path.stem}\n{self.snippet.strip()}"


# ─────────────── 主类 ───────────────


class Dreamer:
    """
    会话结束写 dream + 启动召回 dream。

    用法：
        dreamer = Dreamer(summarizer=my_async_fn)
        await dreamer.dream(task="...", output="...")        # session end
        hits = dreamer.recall("user query")                   # next session start
        ctx  = dreamer.format_context(hits)                   # → system prompt
    """

    def __init__(
        self,
        dreams_dir: Path = DEFAULT_DREAMS_DIR,
        summarizer: DreamSummarizer | None = None,
        *,
        enabled: bool = True,
        min_hours: float = DEFAULT_MIN_HOURS,
        min_scan_minutes: float = DEFAULT_MIN_SCAN_MINUTES,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
    ):
        self.dreams_dir = dreams_dir
        self.summarizer = summarizer
        self.enabled = enabled
        self.min_hours = min_hours
        self.min_scan_minutes = min_scan_minutes
        self.min_sessions = min_sessions
        self.dreams_dir.mkdir(parents=True, exist_ok=True)

    # ─── 5 门触发判断（按成本递增排序）───

    def should_dream(self, *, force: bool = False) -> tuple[bool, str]:
        """
        判断这次 session 结束是否触发 dream。
        返回 (是否触发, 不触发的原因)。force=True 跳过 1-4 门，仍走 Gate 5（锁）。

        门控按"廉价 → 昂贵"排序，99% 调用在前几门就 false 退出，对应 CC 设计意图。
        """
        # Gate 1: Enabled —— 最便宜的检查（一个布尔判断）
        if not self.enabled:
            return False, "disabled"

        # Force 模式：跳过 time / throttle / count，仍要锁
        if force:
            if self._lock_exists_and_alive():
                return False, "lock held by alive PID"
            return True, "forced"

        # 用 timestamp 文件存"上次 dream 时间"和"上次 scan 时间"
        last_dream_path = self.dreams_dir / ".last_dream"
        last_scan_path = self.dreams_dir / ".last_scan"
        now = time.time()

        # Gate 2: Time —— 距上次 dream ≥ min_hours
        if last_dream_path.exists():
            try:
                last = float(last_dream_path.read_text(encoding="utf-8").strip())
                hours_since = (now - last) / 3600
                if hours_since < self.min_hours:
                    return False, f"only {hours_since:.1f}h since last dream (< {self.min_hours}h)"
            except (OSError, ValueError):
                pass  # 文件坏了，当作没有

        # Gate 3: Scan throttle —— 距上次 scan ≥ min_scan_minutes
        if last_scan_path.exists():
            try:
                last = float(last_scan_path.read_text(encoding="utf-8").strip())
                minutes_since = (now - last) / 60
                if minutes_since < self.min_scan_minutes:
                    return False, f"only {minutes_since:.1f}min since last scan (< {self.min_scan_minutes}min)"
            except (OSError, ValueError):
                pass

        # 写下这次 scan 时间（无论后续是否真触发，throttle 都生效）
        try:
            last_scan_path.write_text(str(now), encoding="utf-8")
        except OSError:
            pass

        # Gate 4: Session count —— 累计 dream 数（≈ session 数粗估，每个 session 至多 1 dream）
        # 这里简化：用 dreams_dir 里 .md 文件数当 session 计数，加未触发的次数（用 .pending 计数文件）
        pending_path = self.dreams_dir / ".pending_sessions"
        try:
            pending = int(pending_path.read_text(encoding="utf-8").strip()) if pending_path.exists() else 0
        except (OSError, ValueError):
            pending = 0
        pending += 1
        if pending < self.min_sessions:
            try:
                pending_path.write_text(str(pending), encoding="utf-8")
            except OSError:
                pass
            return False, f"only {pending} pending sessions (< {self.min_sessions})"

        # Gate 5: Lock —— 互斥
        if self._lock_exists_and_alive():
            return False, "lock held by alive PID"

        return True, "all gates passed"

    def _lock_path(self) -> Path:
        return self.dreams_dir / LOCK_FILENAME

    def _lock_exists_and_alive(self) -> bool:
        """锁存在 + 锁里的 PID 还活着 + 锁不老于 stale 阈值。"""
        lock = self._lock_path()
        if not lock.exists():
            return False
        # 时间太老 → stale，可强夺
        try:
            mtime = lock.stat().st_mtime
            if (time.time() - mtime) / 3600 > LOCK_STALE_HOURS:
                return False
        except OSError:
            return False
        # PID 死了也视为可夺
        try:
            content = lock.read_text(encoding="utf-8").strip()
            pid = int(content.split(":", 1)[0])
            os.kill(pid, 0)  # 不发信号，只检查存在性
            return True
        except (OSError, ValueError, ProcessLookupError):
            return False

    def _acquire_lock(self) -> bool:
        """尝试获取锁。成功返回 True。"""
        if self._lock_exists_and_alive():
            return False
        try:
            self._lock_path().write_text(
                f"{os.getpid()}:{time.time()}", encoding="utf-8"
            )
            return True
        except OSError:
            return False

    def _release_lock(self) -> None:
        """释放锁。失败静默（崩溃恢复时 stale 检测会兜底）。"""
        try:
            self._lock_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ─── 写入 ───

    async def dream(self, task: str, output: str, *, force: bool = False) -> Path | None:
        """
        把刚结束的会话压缩成一条 dream 并写盘。
        失败 / 5 门未通过 / 抢锁失败都静默返回 None —— 复盘不应该影响主流程。

        force=True 跳过 1-4 门（仍走锁），用于测试 / 用户显式触发。
        返回写入的文件路径；失败返回 None。
        """
        if self.summarizer is None:
            return None
        if not (task and output):
            return None

        # 5 门触发判断
        ok, reason = self.should_dream(force=force)
        if not ok:
            print(f"[dreamer] skip: {reason}")
            return None

        # 抢锁（Gate 5 已检查过，但为了避免 TOCTOU race，这里再 acquire 一次）
        if not self._acquire_lock():
            print("[dreamer] skip: failed to acquire lock")
            return None

        try:
            prompt = DREAM_PROMPT.format(task=task[:2000], output=output[:4000])
            try:
                md = await self.summarizer(prompt)
            except Exception as e:
                print(f"[dreamer] summarize failed ({type(e).__name__}: {e}); skip")
                return None

            md = _truncate_lines(md, MAX_DREAM_LINES)
            frontmatter = (
                f"---\ntask: {_one_line(task)[:80]}\n"
                f"created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n\n"
            )
            path = self.dreams_dir / f"{_timestamp()}-{_slug(task)}.md"
            path.write_text(frontmatter + md.strip() + "\n", encoding="utf-8")

            # 触发成功：更新 last_dream 时间戳，重置 pending 计数
            now = time.time()
            (self.dreams_dir / ".last_dream").write_text(str(now), encoding="utf-8")
            (self.dreams_dir / ".pending_sessions").write_text("0", encoding="utf-8")

            return path
        finally:
            self._release_lock()

    # ─── 召回 ───

    def recall(self, query: str, top_k: int = 3, min_score: int = 1) -> list[DreamHit]:
        """
        关键词 grep 找最相关的 top_k 条 dream。

        打分规则（粗暴但够用）：
          1. 把 query 按非字母数字 / 中文标点切成关键词
          2. 每个关键词在 dream 文本里出现一次 +1 分
          3. 出现在前 30 行 (任务+关键决策段) 额外 +1 分
          4. 分数 < min_score 直接丢

        Returns: 按分数降序，最多 top_k 条
        """
        if not self.dreams_dir.exists():
            return []
        keywords = _extract_keywords(query)
        if not keywords:
            return []

        scored: list[DreamHit] = []
        for path in sorted(self.dreams_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            score = _score(text, keywords)
            if score < min_score:
                continue
            scored.append(DreamHit(
                path=path,
                score=score,
                snippet=_extract_snippet(text, max_lines=40),
            ))

        # 高分在前；同分时新文件在前（按 mtime 倒序）
        scored.sort(key=lambda h: (h.score, h.path.stat().st_mtime), reverse=True)
        return scored[:top_k]

    # ─── 拼 system prompt ───

    @staticmethod
    def format_context(hits: list[DreamHit], max_chars: int = 3000) -> str:
        """
        把召回结果拼成一段塞进 system prompt 的文本。空列表返回 ""。

        max_chars 是字符数粗截断，避免 dream 太多撑爆 system。
        """
        if not hits:
            return ""
        parts = ["<related past experience>"]
        used = 0
        for h in hits:
            block = h.render()
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        parts.append("</related past experience>")
        return "\n\n".join(parts)


# ─────────────── 内部工具函数 ───────────────


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _slug(task: str, max_len: int = 24) -> str:
    """
    把任务字符串变成文件名安全的短 slug。
    保留中英文数字，其余替换成 -；长度封顶。
    """
    s = re.sub(r"[^\w一-鿿]+", "-", task, flags=re.UNICODE).strip("-")
    return (s or "task")[:max_len]


def _truncate_lines(text: str, max_lines: int) -> str:
    """把 markdown 限制在 max_lines 行内（超过末尾加省略提示）。"""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    kept.append(f"\n<!-- truncated: {len(lines) - max_lines} more lines -->")
    return "\n".join(kept)


# 中文/英文/数字保留为 token 边界；其余视为分隔
_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _extract_keywords(query: str) -> list[str]:
    """
    极简关键词抽取：分词 + 去 1 字停用词。
    没用 jieba/分词库是为了零依赖；后续可换。
    """
    tokens = _TOKEN_RE.findall(query.lower())
    # 中文单字大概率是停用词（"的"、"了"、"是"），过滤；英文 1 字母同理
    return [t for t in tokens if len(t) >= 2]


def _score(text: str, keywords: list[str]) -> int:
    """文本对关键词集合的总命中分。"""
    lower = text.lower()
    head = "\n".join(lower.splitlines()[:30])  # 前 30 行：任务 + 关键决策
    score = 0
    for kw in keywords:
        score += lower.count(kw)
        # 出现在头部加权（更可能是真主题，不是顺带提到）
        if kw in head:
            score += 1
    return score


def _extract_snippet(text: str, max_lines: int = 40) -> str:
    """
    去掉 frontmatter，取正文前 max_lines 行作为 snippet。
    召回展示给模型时不需要每条 dream 全文；省 token。
    """
    if text.startswith("---"):
        # 跳过 frontmatter
        parts = text.split("---", 2)
        body = parts[2] if len(parts) >= 3 else text
    else:
        body = text
    lines = body.strip().splitlines()
    return "\n".join(lines[:max_lines])


# ─── 兼容性别名 ───
# 改名前类叫 Dreamer；为保持向后兼容（harness / tests 旧导入），加 SessionJournal 别名。
# 新代码请用 SessionJournal；旧代码用 Dreamer 仍可工作。
SessionJournal = Dreamer
