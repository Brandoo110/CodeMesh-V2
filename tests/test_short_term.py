"""
短期记忆滑动窗口单元测试（Session 2 验收要求）
===============================================

跑法：
    python -m tests.test_short_term
或
    pytest tests/test_short_term.py
"""

from memory import ShortTermMemory


def test_sliding_window_drops_oldest():
    mem = ShortTermMemory(max_messages=3)
    mem.set_system("sys")
    mem.add("user", "1")
    mem.add("assistant", "1")
    mem.add("user", "2")
    mem.add("assistant", "2")   # 加入第 4 条，最早那条应该被挤掉

    msgs = mem.get_messages()
    # system + 3 条保留的
    assert len(msgs) == 4
    assert msgs[0]["role"] == "system"
    # 第一条 user "1" 应该被挤掉，剩下的最早是 assistant "1"
    assert msgs[1]["content"] == "1" and msgs[1]["role"] == "assistant"
    print("OK sliding window")


def test_system_always_first():
    mem = ShortTermMemory()
    mem.add("user", "hi")
    mem.set_system("sys v2")
    msgs = mem.get_messages()
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "sys v2"
    print("OK system always first")


if __name__ == "__main__":
    test_sliding_window_drops_oldest()
    test_system_always_first()
    print("\nAll tests passed.")
