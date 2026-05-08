"""
Compactor 单元测试
=====================

跑法：
    python -m tests.test_compactor

不调真实 API；所有 summarizer 都是 fake。
"""

import asyncio

from feedback.compactor import (
    AUTOCOMPACT_BUFFER_TOKENS,
    DEFAULT_KEEP_RECENT,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    AutoCompactState,
    auto_compact_if_needed,
    autocompact_threshold,
    compact_conversation,
    estimate_messages_tokens,
    format_compact_summary,
    microcompact_messages,
    should_autocompact,
)


# ─────────────────────────── helpers ───────────────────────────


def _tool_msg(name: str, body: str = "result body" * 50) -> dict:
    """构造一条"工具结果"消息（CodeMesh fallback 格式 [TOOL <name>] ...）。"""
    return {"role": "user", "content": f"[TOOL {name}] {body}"}


def _user_msg(text: str = "hello") -> dict:
    return {"role": "user", "content": text}


def _assistant_msg(text: str = "hi") -> dict:
    return {"role": "assistant", "content": text}


def _fake_summarizer(reply: str = "<summary>compressed</summary>"):
    calls = []
    async def s(prompt: str) -> str:
        calls.append(prompt)
        return reply
    return s, calls


# ─────────────────────────── microcompact ───────────────────────────


def test_microcompact_no_op_under_threshold():
    """少于 keep_recent 条工具消息时不动。"""
    msgs = [_user_msg(), _tool_msg("read_file")]
    new, freed = microcompact_messages(msgs, keep_recent=5)
    assert freed == 0
    # 内容未变
    assert new[1]["content"].startswith("[TOOL read_file]")
    print("OK microcompact: under threshold no-op")


def test_microcompact_clears_old_tool_results():
    """超过 keep_recent 时旧的被替换成 cleared 占位符。"""
    msgs = [_user_msg()]
    for i in range(8):
        msgs.append(_tool_msg("grep_text", body=f"hit-{i} " * 100))
    new, freed = microcompact_messages(msgs, keep_recent=3)
    # 8 - 3 = 5 条被清
    cleared = [m for m in new if m.get("content") == "[Old tool result content cleared]"]
    assert len(cleared) == 5
    # 最近 3 条保留
    assert "hit-7" in new[-1]["content"]
    assert "hit-6" in new[-2]["content"]
    assert "hit-5" in new[-3]["content"]
    assert freed > 0
    print("OK microcompact: clears old, keeps recent")


def test_microcompact_idempotent():
    """对已清过的消息再跑一次不该重复扣 token。"""
    msgs = [_user_msg()] + [_tool_msg("bash_exec") for _ in range(8)]
    new, _ = microcompact_messages(msgs, keep_recent=3)
    new2, freed2 = microcompact_messages(new, keep_recent=3)
    assert freed2 == 0
    print("OK microcompact: idempotent")


def test_microcompact_only_compactable_tools():
    """非 COMPACTABLE 列表的工具不被压（这里 plot_graph 不在白名单）。"""
    msgs = [_user_msg()]
    msgs += [{"role": "user", "content": "[TOOL plot_graph] huge_data"} for _ in range(10)]
    new, freed = microcompact_messages(msgs, keep_recent=3)
    assert freed == 0   # 都不是可压缩工具
    print("OK microcompact: respects whitelist")


# ─────────────────────────── token 估算 / threshold ───────────────────────────


def test_estimate_messages_tokens_grows():
    base = [_user_msg("a")]
    big = [_user_msg("a" * 1000)]
    assert estimate_messages_tokens(big) > estimate_messages_tokens(base)
    print("OK estimate_messages_tokens monotonic")


def test_autocompact_threshold_subtracts_buffer():
    """200K window 下，threshold = 200K - 20K - 13K = 167K。"""
    t = autocompact_threshold(200_000)
    assert t == 200_000 - 20_000 - AUTOCOMPACT_BUFFER_TOKENS
    print("OK autocompact_threshold formula")


def test_should_autocompact_below_threshold():
    state = AutoCompactState()
    msgs = [_user_msg("short")]
    assert should_autocompact(msgs, state) is False
    print("OK should_autocompact: below threshold returns False")


def test_should_autocompact_blocks_on_consecutive_failures():
    """连续失败上限后永远返回 False，避免坏 LLM 浪费钱。"""
    state = AutoCompactState(consecutive_failures=MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES)
    # 哪怕 token 早已爆
    msgs = [_user_msg("x" * 10_000_000)]
    assert should_autocompact(msgs, state) is False
    print("OK should_autocompact: blocks after MAX_CONSECUTIVE_FAILURES")


# ─────────────────────────── format_compact_summary ───────────────────────────


def test_format_strips_analysis_keeps_summary():
    raw = "<analysis>internal scratch</analysis><summary>final result</summary>"
    out = format_compact_summary(raw)
    assert "scratch" not in out
    assert "final result" in out
    print("OK format strips <analysis>")


def test_format_falls_back_when_no_summary_tag():
    """模型没按格式回 → 整段当 summary（兜底，不丢内容）。"""
    raw = "no tags here"
    out = format_compact_summary(raw)
    assert "no tags here" in out
    print("OK format falls back when no summary tag")


# ─────────────────────────── compact_conversation ───────────────────────────


def test_compact_conversation_short_unchanged():
    """少于 preserve_recent 条时直接返回。"""
    summarizer, calls = _fake_summarizer()
    msgs = [_user_msg(), _assistant_msg()]
    out = asyncio.run(compact_conversation(msgs, summarizer, preserve_recent=6))
    assert out == msgs
    assert calls == []
    print("OK compact_conversation: short list unchanged")


def test_compact_conversation_replaces_older():
    """长对话被压：older → 摘要消息，newer 保留。"""
    summarizer, calls = _fake_summarizer(reply="<summary>OK</summary>")
    msgs = [_user_msg(f"old-{i}") for i in range(20)]
    out = asyncio.run(compact_conversation(msgs, summarizer, preserve_recent=4))
    # [摘要消息] + 4 条 newer
    assert len(out) == 5
    assert "summary" in out[0]["content"].lower()
    # 最后 4 条还在
    for i, m in enumerate(out[1:], start=16):
        assert f"old-{i}" in m["content"]
    assert len(calls) == 1
    print("OK compact_conversation: older replaced, newer preserved")


# ─────────────────────────── auto_compact_if_needed ───────────────────────────


def test_auto_compact_no_op_when_below_threshold():
    summarizer, calls = _fake_summarizer()
    state = AutoCompactState()
    msgs = [_user_msg("short")]
    out, was = asyncio.run(auto_compact_if_needed(msgs, summarizer, state))
    assert was is False
    assert out == msgs
    assert calls == []
    print("OK auto_compact: no-op below threshold")


def test_auto_compact_microcompact_alone_suffices():
    """token 微爆 → microcompact 清掉旧工具结果就够。"""
    summarizer, calls = _fake_summarizer()
    state = AutoCompactState()
    # 故意做超大消息触发阈值
    msgs = [_user_msg()]
    # 8 条很长的工具结果 → 总 token 爆
    for i in range(8):
        msgs.append(_tool_msg("read_file", body=("x" * 50_000)))
    # 用极小 window 强制触发
    out, was = asyncio.run(auto_compact_if_needed(msgs, summarizer, state, context_window=50_000))
    assert was is True
    print("OK auto_compact: microcompact handles surge alone")


def test_auto_compact_failure_increments_state():
    """summarizer 抛异常 → consecutive_failures += 1，不向上抛。"""
    async def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    state = AutoCompactState()
    # 构造一个真要 full compact 的场景：消息数多 + 不是工具结果（microcompact 救不了）
    msgs = [_user_msg("a" * 10_000) for _ in range(50)]
    out, was = asyncio.run(auto_compact_if_needed(msgs, boom, state, context_window=1000))
    assert was is False
    assert state.consecutive_failures == 1
    print("OK auto_compact: failure increments state.consecutive_failures")


# ─────────────────────────── runner ───────────────────────────


if __name__ == "__main__":
    test_microcompact_no_op_under_threshold()
    test_microcompact_clears_old_tool_results()
    test_microcompact_idempotent()
    test_microcompact_only_compactable_tools()

    test_estimate_messages_tokens_grows()
    test_autocompact_threshold_subtracts_buffer()
    test_should_autocompact_below_threshold()
    test_should_autocompact_blocks_on_consecutive_failures()

    test_format_strips_analysis_keeps_summary()
    test_format_falls_back_when_no_summary_tag()

    test_compact_conversation_short_unchanged()
    test_compact_conversation_replaces_older()

    test_auto_compact_no_op_when_below_threshold()
    test_auto_compact_microcompact_alone_suffices()
    test_auto_compact_failure_increments_state()

    print("\nAll compactor tests passed.")
