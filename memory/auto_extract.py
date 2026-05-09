"""
Auto Memory Extraction：会话结束后自动抽取跨会话事实（记忆层升级）
========================================================================

【背景】
v3 时 CodeMesh 已经有 `remember_fact / recall_facts / forget_fact` 工具，
但**靠模型主动调**——模型不调就没记忆，模型调得草率就是垃圾记忆。

CC / OH 的设计是"任务结束自动抽"：
  1. 任务结束时 LLM 复盘整个会话，抽出**可跨会话复用**的事实
  2. 按 4 种类型分类：user / feedback / project / reference
  3. 每条带 frontmatter（name + description + type）+ Why + How 双段正文
  4. 写到 `~/.codemesh/auto_memory/<slug>.md`，更新 MEMORY.md 索引

【4 种类型（照抄 CC / OH 字面值）】
| Type | Description | Example |
|---|---|---|
| user | User's role, goals, preferences | "Senior Go engineer, new to React frontend" |
| feedback | Corrections and validated approaches | "Don't mock the database — real DB tests only" |
| project | Ongoing work, deadlines, decisions | "Auth rewrite driven by legal compliance" |
| reference | Pointers to external resources | "Pipeline bugs in Linear project INGEST" |

设计哲学：**只保存不可从当前项目状态推导的信息**。Git 状态能看到的不记。

【Why + How 双段正文格式】
```
**Why:** 为什么这条事实重要（背景 / 触发场景）
**How to apply:** 下次什么场景下复用、怎么用
```
这强迫模型记"因果"和"应用场景"，而不是干巴巴的结论。

【MEMORY.md 索引】
- ≤ 200 行 / ≤ 25 KB（CC `s56=200, j58=25000` 字面常量）
- 每条一行：`- [name](slug.md)` ← 不超过 ~150 字符
- 是索引不是日志——指向 memory file，不是堆全文

【tradeoff vs OH/CC】
- 同：4 类型 + Why/How 模板 + 200 行 / 25KB 上限 + frontmatter 格式
- 减：OH 的 sha1 路径 hash（每个 cwd 一个独立目录）—— 教学项目单一仓库够用
- 减：复杂的 metadata 解析 / search 评分 —— 复用现有 long_term + dreamer.recall
- 加：抽取触发自动调（OH 的 memory/manager.py 只做存储，没抽取）

【面试故事】
"OpenHarness 提供了记忆存储基础设施（memory/manager.py），但没做'任务结束自动抽取'。
我自己写了 80 行 auto_extract，按 CC 设计的 4 类型分类（user/feedback/project/reference）+
Why/How 双段模板，硬约束在 200 行 / 25KB。这是 OH 没覆盖的层，CodeMesh 这部分**领先**。"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

# 4 种记忆类型（对齐 CC / OH 字面值）
MemoryType = Literal["user", "feedback", "project", "reference"]


# 默认存储位置：和 dreams/ / memory.db 都在 ~/.codemesh/
DEFAULT_AUTO_MEMORY_DIR = Path.home() / ".codemesh" / "auto_memory"

# MEMORY.md 索引文件硬约束（CC cli.js 里 s56=200, j58=25000）
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
MAX_ENTRY_CHARS = 150


# ─────────────────────────── 数据类 ───────────────────────────


@dataclass
class MemoryEntry:
    """单条抽取出的记忆。"""
    name: str               # short slug，会变成文件名
    description: str        # 一句话，进 MEMORY.md 索引
    type: MemoryType
    body: str               # 正文（含 Why + How 双段）

    def to_markdown(self) -> str:
        """渲染成完整 markdown 文件内容（frontmatter + 正文）。"""
        return (
            f"---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"type: {self.type}\n"
            f"---\n\n"
            f"{self.body.strip()}\n"
        )


# Summarizer 类型：吃 prompt（构造好的整段文本），返回 markdown 块（可能含多条记忆）
ExtractSummarizer = Callable[[str], Awaitable[str]]


# ─────────────────────────── 抽取 prompt ───────────────────────────


EXTRACT_PROMPT = """\
你刚刚帮用户完成了一个任务。请回顾整段对话，**只抽取可跨会话复用的事实**——
那些下次相似任务能直接用、不可从当前项目状态推导出来的信息。

铁律：
1. 只输出**真正有跨会话价值**的条目。如果这次会话没有产出值得记的事实，**输出空**（即不输出任何 ENTRY 块）。
2. 不要重复记"git 状态能看到的事"（如"我们用了 React"）——那种事下次自己看代码就知道。
3. 每条记忆按下方格式输出，**多条用 `---ENTRY---` 分隔**。

按 4 种类型之一分类：

- **user**: 用户的角色、目标、偏好（"高级 Go 工程师，对 React 是新手"）
- **feedback**: 用户纠正过你的事 / 验证过的做法（"别 mock 数据库，要真 DB 测试"）
- **project**: 进行中的工作、截止日、决策（"auth 重写是合规驱动，不是技术债"）
- **reference**: 指向外部资源的指针（"pipeline bug 在 Linear 项目 INGEST 跟踪"）

每条**严格按这个格式**：

```
---ENTRY---
name: <短 slug，仅小写字母数字下划线，最多 4 词>
description: <1 句概括，最多 80 字符>
type: <user|feedback|project|reference>

**Why:** <为什么这条重要 / 触发场景>
**How to apply:** <下次什么场景下用 / 怎么用>
```

只输出 ENTRY 块，不要解释，不要客套话。

──── 本次对话 ────
任务：{task}

输出：
{output}
"""


# ─────────────────────────── 解析 LLM 输出 ───────────────────────────

# 匹配单个 entry：从 ---ENTRY--- 到下一个 ---ENTRY--- 或文末
_ENTRY_RE = re.compile(r"---ENTRY---\s*(.*?)(?=---ENTRY---|\Z)", re.DOTALL)
# 匹配 frontmatter 字段
_FIELD_RE = re.compile(r"^(name|description|type):\s*(.+?)\s*$", re.MULTILINE)


def parse_entries(raw: str) -> list[MemoryEntry]:
    """从 LLM 原始输出解析出 MemoryEntry 列表。格式不对的条目静默丢弃。"""
    entries: list[MemoryEntry] = []
    for m in _ENTRY_RE.finditer(raw):
        block = m.group(1).strip()
        if not block:
            continue
        fields = {k: v for k, v in _FIELD_RE.findall(block)}
        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        type_ = fields.get("type", "").strip().lower()
        if not (name and description and type_ in {"user", "feedback", "project", "reference"}):
            continue
        # 正文：去掉 frontmatter 行
        body_lines = []
        for line in block.splitlines():
            if _FIELD_RE.match(line):
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        entries.append(MemoryEntry(
            name=_slug(name),
            description=description[:MAX_ENTRY_CHARS],
            type=type_,  # type: ignore[arg-type]
            body=body,
        ))
    return entries


# ─────────────────────────── 索引文件 ───────────────────────────


def update_memory_index(memory_dir: Path, entry: MemoryEntry) -> None:
    """
    把 entry 加进 MEMORY.md 索引（追加一行 markdown link）。
    超出硬约束（200 行 / 25KB）时按 LRU 删最旧。
    """
    index_path = memory_dir / "MEMORY.md"
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8")
    else:
        text = "# Memory Index\n\n"

    # 去重：同名 entry 不重复加
    line = f"- [{entry.name}]({entry.name}.md) — {entry.description}"
    if line in text:
        return

    text = text.rstrip() + "\n" + line + "\n"

    # 硬约束检查
    lines = text.splitlines()
    while len(lines) > MAX_INDEX_LINES or sum(len(l) + 1 for l in lines) > MAX_INDEX_BYTES:
        # 删除最旧的非 header 条目
        for i, l in enumerate(lines):
            if l.startswith("- ["):
                lines.pop(i)
                break
        else:
            break  # 没有可删的条目了

    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ─────────────────────────── 内部工具 ───────────────────────────


def _slug(s: str, max_len: int = 32) -> str:
    """文件名安全 slug。"""
    out = re.sub(r"[^a-zA-Z0-9_]+", "_", s.strip().lower()).strip("_")
    return (out or "memory")[:max_len]


# ─────────────────────────── 主入口 ───────────────────────────


async def extract_and_save(
    task: str,
    output: str,
    summarizer: ExtractSummarizer,
    *,
    memory_dir: Path = DEFAULT_AUTO_MEMORY_DIR,
) -> list[Path]:
    """
    会话结束钩子：调 LLM 抽取记忆，写盘 + 更新索引。

    Returns:
        写入的 .md 文件路径列表（可能为空——LLM 判断没值得记的就空）。
    """
    if not (task and output):
        return []
    memory_dir.mkdir(parents=True, exist_ok=True)

    prompt = EXTRACT_PROMPT.format(task=task[:2000], output=output[:4000])
    try:
        raw = await summarizer(prompt)
    except Exception as e:
        print(f"[auto_extract] LLM call failed ({type(e).__name__}: {e}); skip")
        return []

    entries = parse_entries(raw)
    written: list[Path] = []
    for entry in entries:
        path = memory_dir / f"{entry.name}.md"
        path.write_text(entry.to_markdown(), encoding="utf-8")
        update_memory_index(memory_dir, entry)
        written.append(path)
    return written
