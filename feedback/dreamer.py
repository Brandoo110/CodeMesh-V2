"""
Dreamer：会话结束后离线复盘（受 Anthropic Dreaming 启发）
==============================================================

【这是什么】
2026-05 Anthropic 给 Claude / Claude Code 上线了 "Dreaming"（research preview）：
agent 在每次会话结束后做一次"做梦"——回看刚才的工作过程，提炼出可复用的"记忆"
写成 markdown 存起来，下次相似任务直接召回。
源码侧能在 `cli.js` 里搜到 `DreamTask` / `auto_dream` / `tengu_auto_dream_*`。

CodeMesh 这里做一个**极简复刻**：
  1. session 结束时调一次便宜的模型（doubao），把这次任务压缩成结构化 markdown
  2. 写到 `~/.codemesh/dreams/<timestamp>-<slug>.md`，每条 ≤ 100 行
  3. 下次 `Harness.run(task)` 启动时，关键词 grep 出最相关的 top-K 个 dream，
     拼到 system prompt 里给模型当"过往经验"看

为什么这样做：
  - 写文件而不是塞 SQLite：人能直接 cat 看到，调试 / 演示都直观
  - 关键词 grep 而不是向量检索：零依赖，量小够用；进阶版可以走 rag/ ChromaDB
  - 同步 await 而不是后台 task：CLI 单进程，asyncio.run 退出会取消 task；
    多花几秒给 dream 写完更可靠（Anthropic 的后台 fork 是 daemon 才需要）

【面试故事】
  Q: 你的 agent 怎么"越用越聪明"？
  A: 我看完 Anthropic Dreaming research preview 的发布（2026-05）之后，对照
     Claude Code 的 cli.js 反编译看了下 DreamTask 的实现思路（time-gate、
     session diff、压缩成 memory），自己做了 80 行 Python 复刻。会话结束后
     用 doubao（最便宜）压成 4 段式 markdown：任务 / 关键决策 / 踩坑 /
     可复用经验。下次任务启动时 grep 出关键词重叠最高的 top-3 经验拼进 system。
     比 Anthropic 简化的：单进程同步 await + 关键词 grep；保留的：结构化 schema、
     ≤100 行硬约束、跨会话持久化。

【限制 / 还没做】
  - 没做语义检索 —— 关键词不匹配就召不回（计划：复用 rag/ ChromaDB pipeline）
  - 没做 time-gate —— 每次都写一条，dreams 多了会爆（计划：mtime + 频控）
  - 没去重 —— 相同任务可能写多份（计划：内容 hash 比较 + 替换最旧）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

# summarizer 签名：吃 prompt（已经构造好的中文长文本），返回 markdown 摘要。
# 类型和 ShortTermMemory 的 summarizer 略不同（那个吃 messages 列表）。
DreamSummarizer = Callable[[str], Awaitable[str]]


# 默认存储位置：和 long_term memory 在同一个 ~/.codemesh/ 下。
DEFAULT_DREAMS_DIR = Path.home() / ".codemesh" / "dreams"

# 单条 dream 最多多少行（超过截断）。对齐 Anthropic 的 100 行约束 —— 这是
# memory store 里 "一条记忆 ≤ 100 行" 的设计语言。
MAX_DREAM_LINES = 100


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
    ):
        self.dreams_dir = dreams_dir
        self.summarizer = summarizer
        self.dreams_dir.mkdir(parents=True, exist_ok=True)

    # ─── 写入 ───

    async def dream(self, task: str, output: str) -> Path | None:
        """
        把刚结束的会话压缩成一条 dream 并写盘。
        失败（无 summarizer / 模型异常）静默返回 None —— 复盘不应该影响主流程。

        返回写入的文件路径；失败返回 None。
        """
        if self.summarizer is None:
            return None
        if not (task and output):
            return None

        prompt = DREAM_PROMPT.format(task=task[:2000], output=output[:4000])
        try:
            md = await self.summarizer(prompt)
        except Exception as e:
            # 复盘失败比主任务失败优先级低得多 —— 打个 warning 就放走
            print(f"[dreamer] summarize failed ({type(e).__name__}: {e}); skip")
            return None

        md = _truncate_lines(md, MAX_DREAM_LINES)
        # frontmatter 让人翻文件时也能看到元信息；下次 recall 不用，但调试好用
        frontmatter = (
            f"---\ntask: {_one_line(task)[:80]}\n"
            f"created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n\n"
        )
        path = self.dreams_dir / f"{_timestamp()}-{_slug(task)}.md"
        path.write_text(frontmatter + md.strip() + "\n", encoding="utf-8")
        return path

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
