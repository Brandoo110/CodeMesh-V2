"""
Reranker 单元测试
==================

跑法：
    python -m tests.test_reranker

策略：
  rerank() 接受 scorer 注入，所以测试用 fake scorer，不调任何外部模型。
  覆盖：
    - 空 candidates
    - 按分数降序
    - 取前 k
    - min_score 过滤
    - scorer 抛错时单条降级（不影响其他）
    - 全部低于 min_score 时回退到原 vector 排序
    - _parse_score 边界
"""

import asyncio

from rag.reranker import rerank, _parse_score
from rag.retriever import Hit


def _run(coro):
    return asyncio.run(coro)


def _hit(path: str, text: str, score: float) -> Hit:
    return Hit(path=path, start_line=1, end_line=10, text=text, score=score)


# ────────────────────────── _parse_score ──────────────────────────


def test_parse_score_extracts_integer():
    assert _parse_score("8") == 8.0


def test_parse_score_extracts_first_number_in_text():
    assert _parse_score("我给 7 分，因为...") == 7.0


def test_parse_score_clamps_to_range():
    assert _parse_score("100") == 10.0
    assert _parse_score("-5") == 5.0   # 注意：正则只抓数字部分，会拿到 5
    # 真负号没法被 \d+ 抓到，所以这里其实是 5.0


def test_parse_score_no_number_returns_zero():
    assert _parse_score("不知道") == 0.0
    assert _parse_score("") == 0.0


def test_parse_score_float_supported():
    assert _parse_score("7.5") == 7.5


# ────────────────────────── rerank ──────────────────────────


def test_rerank_empty_returns_empty():
    out = _run(rerank("query", [], k=5, scorer=lambda q, c: _scoring_for(q, c)))
    assert out == []


async def _scoring_for(query: str, candidate: str) -> float:
    """fake scorer：返回 candidate 里数字字符的数量当作分数。"""
    return float(sum(1 for ch in candidate if ch.isdigit()))


def test_rerank_orders_by_score_desc():
    candidates = [
        _hit("a.py", "no digits", 0.1),         # score 0
        _hit("b.py", "one 1 digit", 0.2),       # score 1
        _hit("c.py", "two 22 digits", 0.3),     # score 2
        _hit("d.py", "three 333 digits", 0.4),  # score 3
    ]
    out = _run(rerank("q", candidates, k=4, scorer=_scoring_for))
    paths = [h.path for h in out]
    assert paths == ["d.py", "c.py", "b.py", "a.py"]


def test_rerank_truncates_to_k():
    candidates = [
        _hit(f"f{i}.py", f"file {i} " + "1" * i, 0.1) for i in range(10)
    ]
    out = _run(rerank("q", candidates, k=3, scorer=_scoring_for))
    assert len(out) == 3


def test_rerank_min_score_filters():
    candidates = [
        _hit("low.py", "no digits", 0.1),    # 0
        _hit("mid.py", "one 1", 0.2),        # 1
        _hit("high.py", "many 12345", 0.3),  # 5
    ]
    out = _run(rerank("q", candidates, k=5, scorer=_scoring_for, min_score=2.0))
    paths = [h.path for h in out]
    assert paths == ["high.py"]


def test_rerank_all_below_min_score_falls_back_to_vector_order():
    """全 0 时回退到 candidates[:k]（保留 vector 顺序），不返回空。"""
    candidates = [
        _hit("a.py", "abc", 0.1),
        _hit("b.py", "def", 0.2),
        _hit("c.py", "ghi", 0.3),
    ]
    # 全部 score 0
    async def all_zero(q, c):
        return 0.0
    out = _run(rerank("q", candidates, k=2, scorer=all_zero, min_score=1.0))
    assert len(out) == 2
    assert out[0].path == "a.py"   # 原顺序
    assert out[1].path == "b.py"


def test_rerank_handles_scorer_exception_per_candidate():
    """单个 candidate 的 scorer 抛错不应让整个 rerank 崩。"""
    candidates = [
        _hit("a.py", "ok", 0.1),
        _hit("b.py", "boom", 0.2),
        _hit("c.py", "fine", 0.3),
    ]

    async def flaky(q, c):
        if c == "boom":
            raise RuntimeError("scorer crashed")
        return 5.0

    out = _run(rerank("q", candidates, k=3, scorer=flaky))
    # 应该 3 条都返回；只是 boom 那条用了 -score 兜底排序
    assert len(out) == 3
    paths = [h.path for h in out]
    # 同分时稳定排序，但失败的 b.py 用 -0.2 当分数 → 排最后
    assert paths[-1] == "b.py"


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
    print(f"\n{len(tests) - failed}/{len(tests)} reranker tests passed.")
    if failed:
        raise SystemExit(1)
