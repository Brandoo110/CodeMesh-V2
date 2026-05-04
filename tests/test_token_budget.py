"""
Token budget 单元测试
=======================

跑法：
    python -m tests.test_token_budget

策略：
  本测试不强求 tiktoken 联网下载成功——两条路径（tiktoken / 启发式）
  都应保持稳定行为：
    - count_tokens 单调
    - 空串返回 0
    - truncate_to_budget 不超 budget（在启发式下允许 ±15% 误差）
"""

from feedback.token_budget import (
    count_tokens,
    truncate_to_budget,
    using_tiktoken,
    _heuristic_count,
)


# ────────────────────────── count_tokens ──────────────────────────


def test_count_empty_string():
    assert count_tokens("") == 0


def test_count_short_text_is_positive():
    n = count_tokens("hello")
    assert n >= 1


def test_count_monotonic():
    """更长的文本必须 >= 短文本的 token 数。"""
    short = count_tokens("hello")
    long_ = count_tokens("hello hello hello hello")
    assert long_ >= short


def test_heuristic_chinese_dense():
    """中文每字算约 1 token，10 字大概 10 token（启发式）。"""
    n = _heuristic_count("你好世界你好世界你好世界")  # 12 个中文字
    # 启发式 = 12 个 cjk → 12 token
    assert 10 <= n <= 14


def test_heuristic_english_sparse():
    """英文 4 字符 ≈ 1 token，启发式给 0.25 比例。"""
    text = "abcdefgh" * 8  # 64 chars 英文
    n = _heuristic_count(text)
    # 64 * 0.25 = 16
    assert 14 <= n <= 18


# ────────────────────────── truncate_to_budget ──────────────────────────


def test_truncate_below_budget_no_change():
    text = "hello"
    out = truncate_to_budget(text, max_tokens=100)
    assert out == text


def test_truncate_above_budget_shrinks():
    text = "abcdefgh" * 200  # 1600 chars
    out = truncate_to_budget(text, max_tokens=10)
    # 应该被截短了
    assert len(out) < len(text)
    # 截后再 count，不超 budget（启发式允许 ±15%，给个宽松上限）
    n = count_tokens(out)
    assert n <= 12


def test_truncate_zero_budget_returns_empty():
    out = truncate_to_budget("anything", max_tokens=0)
    assert out == ""


def test_truncate_chinese():
    text = "你好" * 500   # 1000 个中文字
    out = truncate_to_budget(text, max_tokens=20)
    assert len(out) < len(text)
    # 启发式：中文 1 token/char 左右；20 budget → 至多 ~24 字符
    n = count_tokens(out)
    assert n <= 24


# ────────────────────────── format_context 集成 ──────────────────────────


def test_format_context_max_tokens_path():
    """max_tokens 预算下，超出预算的 hit 会被截断或丢弃。"""
    from rag.retriever import Hit, format_context

    hits = [
        Hit(path="a.py", start_line=1, end_line=10, text="x" * 200, score=0.1),
        Hit(path="b.py", start_line=1, end_line=10, text="y" * 200, score=0.2),
        Hit(path="c.py", start_line=1, end_line=10, text="z" * 200, score=0.3),
    ]
    out = format_context(hits, max_tokens=20)   # 极小 budget
    # 应该只放进很小一段
    assert len(out) < 600   # 三个 hit 文本累计 600 char，极小 budget 下应远低于此
    assert "<CODEBASE CONTEXT>" in out
    assert "</CODEBASE CONTEXT>" in out


def test_format_context_max_chars_back_compat():
    """显式给 max_chars 仍走老路径。"""
    from rag.retriever import Hit, format_context

    hits = [
        Hit(path="a.py", start_line=1, end_line=10, text="x" * 100, score=0.1),
        Hit(path="b.py", start_line=1, end_line=10, text="y" * 100, score=0.2),
    ]
    out = format_context(hits, max_chars=80)   # 极小 char budget
    # 应该只塞进 ~80 字符的内容
    assert len(out) < 200


def test_format_context_empty_hits():
    from rag.retriever import format_context
    assert format_context([], max_tokens=1000) == ""


# ────────────────────────── flag ──────────────────────────


def test_using_tiktoken_returns_bool():
    assert isinstance(using_tiktoken(), bool)


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_")
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} token_budget tests passed.")
    if failed:
        raise SystemExit(1)
