"""
ShortTermMemory 记忆压缩单元测试
====================================

跑法：
    python -m tests.test_memory_compression
或
    pytest tests/test_memory_compression.py

这套测试不调任何真实 API：所有 summarizer 都是注入的 fake async 函数，
所以本地无网络也能跑。
"""

import asyncio

from memory import ShortTermMemory


# ────────────────────────── helpers ──────────────────────────


def _fake_summarizer_factory():
    """
    返回 (summarizer, calls)：
      - summarizer 是 async 函数，可注入到 ShortTermMemory
      - calls 是 list[list[dict]]，每次调用收到的 messages 都被记下来
    """
    calls: list[list[dict]] = []

    async def summarizer(messages: list[dict]) -> str:
        calls.append(list(messages))
        # 用 role+前 4 字 拼一个稳定字符串，断言时好看
        return "SUM(" + "|".join(
            f"{m.get('role')}:{(m.get('content') or '')[:4]}" for m in messages
        ) + ")"

    return summarizer, calls


def _fill(mem: ShortTermMemory, n: int) -> None:
    """塞 n 条交替 user/assistant 消息。"""
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add(role, f"msg-{i}")


# ────────────────────────── tests ──────────────────────────


def test_below_threshold_no_compress():
    """阈值未到时 maybe_compress 直接返回 False，summarizer 不被调。"""
    summarizer, calls = _fake_summarizer_factory()
    mem = ShortTermMemory(
        max_messages=20, compress_threshold=10, summarizer=summarizer
    )
    mem.set_system("sys")
    _fill(mem, 5)

    triggered = asyncio.run(mem.maybe_compress())
    assert triggered is False
    assert calls == []
    assert mem.summary is None
    # 消息一条都没丢
    assert len(mem) == 5
    print("OK below threshold: no compression triggered")


def test_above_threshold_triggers():
    """达到阈值时 summarizer 被调用，最旧一半消息从队列中消失。"""
    summarizer, calls = _fake_summarizer_factory()
    mem = ShortTermMemory(
        max_messages=20, compress_threshold=10, summarizer=summarizer
    )
    mem.set_system("sys")
    _fill(mem, 10)  # 刚好到阈值

    triggered = asyncio.run(mem.maybe_compress())
    assert triggered is True
    assert len(calls) == 1
    # 半数 = 5 条被压缩
    assert len(calls[0]) == 5
    # 队列剩 10 - 5 = 5
    assert len(mem) == 5
    # 剩下的应该是后 5 条（msg-5..msg-9）
    contents = [m["content"] for m in mem.get_messages() if m["role"] != "system"]
    assert contents == [f"msg-{i}" for i in range(5, 10)]
    assert mem.summary is not None and mem.summary.startswith("SUM(")
    print("OK above threshold: oldest half compressed")


def test_summary_injected_into_messages():
    """压缩后 get_messages 顺序：原 system → summary system → 剩余消息。"""
    summarizer, _ = _fake_summarizer_factory()
    mem = ShortTermMemory(
        max_messages=20, compress_threshold=4, summarizer=summarizer
    )
    mem.set_system("ROLE")
    _fill(mem, 4)

    asyncio.run(mem.maybe_compress())
    msgs = mem.get_messages()

    # [原 system, summary system, 2 条剩余]
    assert len(msgs) == 4
    assert msgs[0] == {"role": "system", "content": "ROLE"}
    assert msgs[1]["role"] == "system"
    assert "previous conversation summary" in msgs[1]["content"]
    assert msgs[2]["content"] == "msg-2"
    assert msgs[3]["content"] == "msg-3"
    print("OK summary injected at correct position")


def test_system_never_compressed():
    """system 消息不会被传入 summarizer（它本来就不在 _messages 里）。"""
    summarizer, calls = _fake_summarizer_factory()
    mem = ShortTermMemory(
        max_messages=20, compress_threshold=4, summarizer=summarizer
    )
    mem.set_system("SECRET-SYSTEM-PROMPT")
    _fill(mem, 4)

    asyncio.run(mem.maybe_compress())
    # summarizer 收到的所有消息内容里都不应该出现 system 文案
    seen = [m for batch in calls for m in batch]
    assert all(m["role"] != "system" for m in seen)
    assert all("SECRET-SYSTEM-PROMPT" not in (m.get("content") or "") for m in seen)
    print("OK system message never compressed")


def test_repeated_compression_accumulates():
    """两次触发时第二次的 summary 包含第一次的 summary（拼接保留）。"""
    summarizer, calls = _fake_summarizer_factory()
    mem = ShortTermMemory(
        max_messages=20, compress_threshold=4, summarizer=summarizer
    )
    mem.set_system("sys")

    # 第一轮：4 条 → 压缩 → 剩 2 条
    _fill(mem, 4)
    triggered_1 = asyncio.run(mem.maybe_compress())
    first_summary = mem.summary
    assert triggered_1 is True
    assert first_summary is not None

    # 再加 2 条凑回 4 条 → 第二次压缩
    mem.add("user", "later-A")
    mem.add("assistant", "later-B")
    assert len(mem) == 4
    triggered_2 = asyncio.run(mem.maybe_compress())
    assert triggered_2 is True

    # summary 应包含两段（用 \n\n 分隔）
    assert mem.summary is not None
    assert mem.summary.count("SUM(") >= 2
    assert first_summary in mem.summary
    print("OK repeated compression accumulates summaries")


def test_no_summarizer_means_no_compression():
    """compress_threshold 设了但 summarizer 没设 → 永不触发。"""
    mem = ShortTermMemory(max_messages=20, compress_threshold=2, summarizer=None)
    mem.set_system("sys")
    _fill(mem, 10)

    triggered = asyncio.run(mem.maybe_compress())
    assert triggered is False
    assert mem.summary is None
    assert len(mem) == 10
    print("OK no summarizer: compression disabled")


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    test_below_threshold_no_compress()
    test_above_threshold_triggers()
    test_summary_injected_into_messages()
    test_system_never_compressed()
    test_repeated_compression_accumulates()
    test_no_summarizer_means_no_compression()
    print("\nAll memory compression tests passed.")
