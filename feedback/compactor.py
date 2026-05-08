"""
Compactor：上下文压缩（Harness 反馈层 / 记忆层升级）
========================================================

【背景】
2026-03 Anthropic 误把 Claude Code source map 一起发了，全网拿到 ~51 万行 TS 真源码。
之后港大 HKUDS/OpenHarness 4-9 发了 Python 忠实复刻（v0.1.2），其中
`services/compact/__init__.py` 完整翻译了 CC 的两套压缩机制：micro + full。
本模块是对照 OH 的简化教学版（~200 行 vs OH 600+ 行）。

【两层压缩】
1. **微压缩 (Microcompact)** —— 每轮 API 前的"日常保洁"：清掉旧工具结果（grep / read 等
   "可压缩工具"的输出），保留最近 N 条。**几乎不花成本**：纯 Python 字符串替换，不调 LLM。

2. **全压缩 (Full Compact)** —— 紧急刹车：上下文快爆时调便宜模型生成 9 段结构化摘要。
   触发阈值：有效窗口 - 13K（CC / OH 的字面常量）；预留 13K 给后续输出。

【AutoCompactState 状态机】
跟踪 `consecutive_failures`：连续 3 次失败后**停止自动压缩**，避免坏 LLM 反复浪费钱。
这是 OH 直接抄 CC 的设计——失败时模型可能进入坏循环（输出 truncated / 格式错），
重试只会更贵。

【9 段摘要模板】
照抄 CC / OH 的字面值，每段都精心设计过：
  1. Primary Request and Intent
  2. Key Technical Concepts
  3. Files and Code Sections (with code snippets)
  4. Errors and Fixes
  5. Problem Solving
  6. All User Messages (verbatim — critical for intent tracking)  ← 用户原话不能丢
  7. Pending Tasks
  8. Current Work
  9. Optional Next Step

第 6 段"verbatim"是关键——意图追踪不能依赖摘要，必须保留原话。

【tradeoff vs OH/CC】
- 同：常量值（13_000 / 20_000 / 5 / 60 / 3）、9 段模板、AutoCompactState 字段
- 减：cache_edits API 调用（OpenAI 兼容客户端不支持，国内厂商也基本没有）
- 减：context_management API 参数（同上）
- 减：preCompactDiscoveredTools / SystemCompactBoundaryMessage 元数据（教学项目用不到）
- 加：教学注释密度（每段都讲 why）

【面试故事】
"我对照 HKUDS/OpenHarness 的 services/compact 实现，做了 200 行 Python 简化版。
OH 完整翻译了 CC 的 microcompact + autoCompact，但有些只在 Anthropic 服务端可用的
原语（cache_edits）我没抄，因为 CodeMesh 走 OpenAI 兼容协议跨国内厂商。
9 段摘要模板和 13K buffer / 3 次失败上限这些核心常量保留了——它们和源码 grep 的结果一致。"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

# ─────────────────────────── 常量（对齐 CC / OH） ───────────────────────────

# 自动压缩阈值预留：有效窗口 - 13K = 触发线
# 13K 是 CC cli.js 里的字面值，作用是给"压缩完后还要继续生成"留 buffer
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# 给摘要 LLM 调用预留的最大 output tokens
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# 微压缩：保留最近 N 条工具结果
DEFAULT_KEEP_RECENT = 5

# 微压缩：距上次助手消息超过多少分钟才清旧（缓存 TTL ≈ 1h）
DEFAULT_GAP_THRESHOLD_MINUTES = 60

# AutoCompactState：连续失败上限（再失败就停止自动压缩）
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

# 默认上下文窗口（保守）
DEFAULT_CONTEXT_WINDOW = 200_000

# 微压缩的占位符
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# "可压缩"工具白名单：调用频次高、单次结果体量大的几个
# 这些工具的旧结果丢失了模型可以重新调，不会丢关键信息
COMPACTABLE_TOOL_NAMES: frozenset[str] = frozenset({
    "bash_exec",
    "read_file",
    "grep_text",
    "glob_files",
    "web_fetch",
    "web_search",
    "edit_file",
    "write_file",
})


# ─────────────────────────── 状态机 ───────────────────────────


@dataclass
class AutoCompactState:
    """
    跨 query loop 持久化的状态。Harness 持有一个实例。

    consecutive_failures:
      连续失败次数。≥ MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES 后 should_autocompact
      永远返回 False，避免坏 LLM 反复浪费钱。
      成功一次就清零。
    """
    compacted: bool = False
    turn_counter: int = 0
    consecutive_failures: int = 0


# Summarizer 类型：吃 prompt（构造好的整段文本），返回摘要文本
Summarizer = Callable[[str], Awaitable[str]]


# ─────────────────────────── Token 估算 ───────────────────────────


def estimate_tokens(text: str) -> int:
    """
    粗估文本 token 数。和 OH 的 token_estimation 同思路：
      - 复用 feedback.token_budget 的 count_tokens（tiktoken 优先）
      - 失败回退到 chars/4（CC / OH 的常用默认）
    """
    if not text:
        return 0
    try:
        from feedback.token_budget import count_tokens
        return count_tokens(text)
    except Exception:
        return len(text) // 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算 messages 列表总 token。包含 4/3 padding（保守，对齐 OH）。"""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("role", ""))
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # OpenAI 风格 multi-content
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(block.get("content", "") or block.get("text", ""))
    # 4/3 保守 padding
    return int(total * 4 / 3)


# ─────────────────────────── 微压缩 ───────────────────────────


def microcompact_messages(
    messages: list[dict],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[dict], int]:
    """
    清除旧的"可压缩工具"结果，保留最近 keep_recent 条。
    几乎不花钱：纯 Python 字符串替换，不调 LLM。

    判定一条消息是"可压缩工具结果"的规则：
      - role == "tool" （OpenAI tool_calls 协议）；或
      - role == "user" 且 content 看起来像 [TOOL <name>] result（CodeMesh 早期 fallback）

    Returns:
        (new_messages, tokens_saved)
    """
    keep_recent = max(1, keep_recent)

    # 第一步：标出所有"工具结果消息"的下标
    tool_indices: list[int] = []
    for i, m in enumerate(messages):
        if _is_compactable_tool_result(m):
            tool_indices.append(i)

    if len(tool_indices) <= keep_recent:
        return messages, 0

    # 留最后 keep_recent 个，前面全部 clear
    keep_set = set(tool_indices[-keep_recent:])
    clear_indices = [i for i in tool_indices if i not in keep_set]

    new_messages = list(messages)
    tokens_saved = 0
    for idx in clear_indices:
        old = new_messages[idx]
        old_content = old.get("content", "") or ""
        if isinstance(old_content, str) and old_content == TIME_BASED_MC_CLEARED_MESSAGE:
            continue   # 已清过，跳过
        tokens_saved += estimate_tokens(old_content if isinstance(old_content, str) else "")
        # 替换 content；保留 role / tool_call_id 等元数据
        new_messages[idx] = {**old, "content": TIME_BASED_MC_CLEARED_MESSAGE}

    return new_messages, tokens_saved


def _is_compactable_tool_result(message: dict) -> bool:
    """判断一条消息是不是"可微压缩"的工具结果。"""
    role = message.get("role")
    if role == "tool":
        # OpenAI tool_calls 协议：tool message 都视为可压缩
        return True
    if role == "user":
        content = message.get("content", "")
        if isinstance(content, str):
            # CodeMesh fallback 格式：[TOOL <name>] ...
            m = re.match(r"^\[TOOL\s+(\w+)\]", content.strip())
            if m and m.group(1) in COMPACTABLE_TOOL_NAMES:
                return True
    return False


# ─────────────────────────── 全压缩 ───────────────────────────


# 9 段摘要 prompt：完全照抄 CC / OH 的字面文本（这是 source map 泄漏后的 ground truth）
# 第 6 段 verbatim 是关键设计：意图追踪不能依赖摘要，必须保留用户原话
COMPACT_PROMPT = """\
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. You already have all the
context you need above. Tool calls will be REJECTED.

Your task is to create a detailed summary of the conversation so far. This summary
will replace the earlier messages, so it must capture all important information.

First, draft your analysis inside <analysis> tags. Walk through the conversation
chronologically and extract: every user request, the approach taken, specific files
and code, errors and fixes, user feedback.

Then, produce a structured summary inside <summary> tags with these sections:

1. **Primary Request and Intent**: All user requests in full detail.
2. **Key Technical Concepts**: Technologies, frameworks, patterns discussed.
3. **Files and Code Sections**: Every file examined, with snippets and line numbers.
4. **Errors and Fixes**: Every error encountered and how it was resolved.
5. **Problem Solving**: Approaches that worked vs. didn't work.
6. **All User Messages**: Non-tool user messages (preserve exact wording).
7. **Pending Tasks**: Explicitly requested work that hasn't been completed.
8. **Current Work**: Detailed description of last task before compaction.
9. **Optional Next Step**: Single most logical next step aligned with recent request.

REMINDER: Plain text only — <analysis> block followed by <summary> block.
"""


def format_compact_summary(raw: str) -> str:
    """
    剥离 <analysis> 草稿，提取 <summary> 正文。
    草稿不进入最终历史——节省 token，对齐 CC 的"思考剥离"设计。
    """
    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw)
    m = re.search(r"<summary>([\s\S]*?)</summary>", text)
    if m:
        return m.group(1).strip()
    # 兜底：模型没按格式输出，整段当 summary
    return text.strip()


def build_compact_replacement(summary: str) -> dict:
    """
    构造一条替换旧消息的 user message。
    用 user role 而不是 system，是因为 system 容易被某些 API 压缩前清掉。
    """
    body = (
        "This session is being continued from a previous conversation that ran out "
        "of context. The summary below covers the earlier portion:\n\n"
        f"{summary}\n\n"
        "Continue from where it left off without asking clarifying questions. "
        "Do not recap the summary; pick up the last task as if no break occurred."
    )
    return {"role": "user", "content": body}


# ─────────────────────────── 触发判断 ───────────────────────────


def autocompact_threshold(context_window: int = DEFAULT_CONTEXT_WINDOW) -> int:
    """
    返回触发自动压缩的 token 阈值。
    = 上下文窗口 - 输出预留(20K, 取保守值) - buffer(13K)
    """
    output_reserve = min(MAX_OUTPUT_TOKENS_FOR_SUMMARY, 20_000)
    return context_window - output_reserve - AUTOCOMPACT_BUFFER_TOKENS


def should_autocompact(
    messages: list[dict],
    state: AutoCompactState,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> bool:
    """是否该触发自动全压缩。"""
    if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return False
    return estimate_messages_tokens(messages) >= autocompact_threshold(context_window)


# ─────────────────────────── 全压缩主流程 ───────────────────────────


async def compact_conversation(
    messages: list[dict],
    summarizer: Summarizer,
    *,
    preserve_recent: int = 6,
) -> list[dict]:
    """
    全压缩主流程：
      1. 先做一次微压缩（cheap）
      2. 把 messages 切成 older（要压）+ newer（保留）两段
      3. older 喂给 summarizer 拿摘要
      4. 新 messages = [摘要消息] + newer

    summarizer 失败时上层应该 catch，写到 state.consecutive_failures。
    """
    if len(messages) <= preserve_recent:
        return list(messages)

    # 1. microcompact 先做一次（很可能就够了）
    messages, _ = microcompact_messages(messages, keep_recent=DEFAULT_KEEP_RECENT)

    # 2. 切片
    older = messages[:-preserve_recent]
    newer = messages[-preserve_recent:]
    if not older:
        return list(messages)

    # 3. 调 summarizer
    older_text = "\n\n".join(
        f"{m.get('role', '?')}: {(m.get('content') or '')[:2000]}"
        for m in older if isinstance(m.get("content"), str)
    )
    prompt = COMPACT_PROMPT + "\n\n──── 待压缩的对话 ────\n\n" + older_text
    raw = await summarizer(prompt)
    summary = format_compact_summary(raw)

    # 4. 拼新 messages
    return [build_compact_replacement(summary), *newer]


async def auto_compact_if_needed(
    messages: list[dict],
    summarizer: Summarizer,
    state: AutoCompactState,
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    preserve_recent: int = 6,
) -> tuple[list[dict], bool]:
    """
    Harness 在每轮 query loop 开头调一次。

    流程：
      1. 不该压 → 直接返回
      2. 试 microcompact，如果省下的 token 够把阈值降下来就停
      3. 还不够才 full compact，更新 state

    Returns:
        (new_messages, was_compacted)
    """
    if not should_autocompact(messages, state, context_window):
        return messages, False

    # 第一刀：microcompact
    new_messages, freed = microcompact_messages(messages)
    if freed > 0 and not should_autocompact(new_messages, state, context_window):
        return new_messages, True

    # 第二刀：full compact
    try:
        result = await compact_conversation(
            new_messages, summarizer, preserve_recent=preserve_recent
        )
        state.compacted = True
        state.turn_counter += 1
        state.consecutive_failures = 0
        return result, True
    except Exception as e:
        state.consecutive_failures += 1
        print(
            f"[compactor] auto-compact failed "
            f"({state.consecutive_failures}/{MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES}): "
            f"{type(e).__name__}: {e}"
        )
        return new_messages, False
