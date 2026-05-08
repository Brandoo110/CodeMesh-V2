"""
Dreamer 单元测试
====================

跑法：
    python -m tests.test_dreamer
或
    pytest tests/test_dreamer.py

不调任何真实 API：summarizer 用注入的 fake async 函数。
每个测试用 tempfile.TemporaryDirectory 隔离 dreams_dir，避免污染 ~/.codemesh/。
"""

import asyncio
import tempfile
from pathlib import Path

from feedback.dreamer import (
    Dreamer,
    DreamHit,
    MAX_DREAM_LINES,
    _extract_keywords,
    _extract_snippet,
    _slug,
    _truncate_lines,
)


# ────────────────────────── helpers ──────────────────────────


def _make_dreamer(tmp: Path, summarizer=None) -> Dreamer:
    """快速构造一个用临时目录的 Dreamer。"""
    return Dreamer(dreams_dir=tmp / "dreams", summarizer=summarizer)


def _fake_summarizer(reply: str = "### 任务\nfoo\n### 关键决策\nbar\n### 踩坑 / 教训\n无\n### 可复用经验\nbaz"):
    """返回 (summarizer_async, calls_log)。"""
    calls: list[str] = []

    async def summarizer(prompt: str) -> str:
        calls.append(prompt)
        return reply

    return summarizer, calls


# ────────────────────────── _slug / _extract_keywords / _truncate_lines ──────────────────────────


def test_slug_basic():
    assert _slug("hello world") == "hello-world"
    assert _slug("重构 auth 模块") == "重构-auth-模块"
    # 仅符号 → 退到 "task"
    assert _slug("///") == "task"
    # 长度封顶
    assert len(_slug("a" * 100)) <= 24
    print("OK _slug basic")


def test_extract_keywords_filters_short():
    """1 字 token 应被过滤（中文单字大概率是停用词）。"""
    kws = _extract_keywords("我 想要 重构 the auth")
    assert "重构" in kws
    assert "auth" in kws
    assert "想要" in kws
    assert "我" not in kws
    assert "the" in kws  # the 是 3 字符，会保留 —— 极简实现的代价
    print("OK _extract_keywords filters singletons")


def test_truncate_lines_under_limit_unchanged():
    text = "a\nb\nc"
    assert _truncate_lines(text, 10) == text
    print("OK _truncate_lines under limit unchanged")


def test_truncate_lines_over_limit_truncates():
    text = "\n".join(str(i) for i in range(150))
    out = _truncate_lines(text, 100)
    lines = out.splitlines()
    # 100 行内容 + 1 空行 + 1 提示行
    assert len(lines) <= 102
    assert "truncated" in out
    print("OK _truncate_lines over limit truncates")


def test_extract_snippet_skips_frontmatter():
    text = "---\ntask: foo\ncreated: now\n---\n\nbody-line-1\nbody-line-2"
    snippet = _extract_snippet(text, max_lines=10)
    assert "task: foo" not in snippet
    assert "body-line-1" in snippet
    print("OK _extract_snippet skips frontmatter")


# ────────────────────────── dream() ──────────────────────────


def test_dream_no_summarizer_returns_none():
    """没配 summarizer → dream 静默 no-op。"""
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        path = asyncio.run(d.dream(task="t", output="o"))
        assert path is None
        # 文件夹建了，但没文件
        assert d.dreams_dir.exists()
        assert list(d.dreams_dir.glob("*.md")) == []
    print("OK dream() no summarizer is no-op")


def test_dream_empty_task_or_output_returns_none():
    """task / output 都得有，否则跳过。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        assert asyncio.run(d.dream(task="", output="x")) is None
        assert asyncio.run(d.dream(task="x", output="")) is None
    print("OK dream() requires task and output")


def test_dream_writes_file_with_frontmatter():
    """正常路径：写一个 .md 文件，含 frontmatter + summarizer 返回的正文。"""
    summarizer, calls = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        path = asyncio.run(d.dream(
            task="重构 auth 模块",
            output="完成，新增了 Token 类",
        ))
        assert path is not None
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        # frontmatter
        assert text.startswith("---\n")
        assert "task: 重构 auth 模块" in text
        assert "created:" in text
        # 4 段式正文
        assert "### 任务" in text
        assert "### 关键决策" in text
        # summarizer 收到的 prompt 包含原 task 和 output
        assert len(calls) == 1
        assert "重构 auth 模块" in calls[0]
        assert "新增了 Token 类" in calls[0]
    print("OK dream() writes file with frontmatter")


def test_dream_truncates_long_output():
    """summarizer 返回 200 行 → 落盘只保留前 100 行 + 提示。"""
    long_md = "\n".join(f"line-{i}" for i in range(200))
    summarizer, _ = _fake_summarizer(reply=long_md)
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        path = asyncio.run(d.dream(task="t", output="o"))
        text = path.read_text(encoding="utf-8")
        body = text.split("---", 2)[2] if text.startswith("---") else text
        # 正文行数应在 100 左右（含 truncated 提示）
        assert "truncated" in body
        assert MAX_DREAM_LINES <= 100  # 防止常量被误改
    print("OK dream() truncates long output")


def test_dream_summarizer_failure_returns_none():
    """summarizer 抛异常 → dream() 返回 None，不向上抛。"""
    async def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=boom)
        path = asyncio.run(d.dream(task="t", output="o"))
        assert path is None
        assert list(d.dreams_dir.glob("*.md")) == []
    print("OK dream() swallows summarizer exception")


# ────────────────────────── recall() ──────────────────────────


def _seed_dream(dreamer: Dreamer, name: str, body: str) -> Path:
    """直接写一份 dream 文件，跳过 summarizer。"""
    p = dreamer.dreams_dir / f"{name}.md"
    p.write_text(f"---\ntask: {name}\n---\n\n{body}\n", encoding="utf-8")
    return p


def test_recall_returns_empty_when_no_dreams():
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        assert d.recall("anything") == []
    print("OK recall() empty when no dreams")


def test_recall_finds_keyword_matching_dream():
    """种 3 个 dream，query 命中其中 1 个。"""
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        _seed_dream(d, "auth-refactor", "### 任务\n重构 auth 鉴权模块\n用 JWT 替换 session")
        _seed_dream(d, "billing-fix", "### 任务\n修 billing 计算 bug\n小数点错位")
        _seed_dream(d, "schema-migrate", "### 任务\n迁移 DB schema 到 v3")

        hits = d.recall("auth 重构")
        assert len(hits) >= 1
        top = hits[0]
        assert "auth-refactor" in top.path.name
        assert top.score > 0
        # snippet 不应包含 frontmatter
        assert "task: auth-refactor" not in top.snippet
    print("OK recall() finds keyword match")


def test_recall_returns_top_k_only():
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        for i in range(5):
            _seed_dream(d, f"dream-{i}", "### 任务\n关键词 cake 出现 N 次\n" * (i + 1))
        hits = d.recall("cake", top_k=2)
        assert len(hits) == 2
        # 分高的在前
        assert hits[0].score >= hits[1].score
    print("OK recall() respects top_k")


def test_recall_filters_below_min_score():
    """无关键词命中 → 返回空，不会"勉强凑数"返回最差的几条。"""
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        _seed_dream(d, "unrelated", "### 任务\n做了别的事\n")
        hits = d.recall("nothing-matches-here")
        assert hits == []
    print("OK recall() filters by min_score")


def test_recall_head_section_gets_bonus():
    """关键词在头部（前 30 行）应比尾部命中得分高。"""
    head_text = "### 任务\nrefactor token validation\n### 关键决策\n用 JWT" + "\n空行" * 50
    tail_text = "### 任务\n做缓存\n" + "\n空行" * 50 + "\nrefactor token validation"
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td))
        _seed_dream(d, "head-hit", head_text)
        _seed_dream(d, "tail-hit", tail_text)
        hits = d.recall("token validation")
        assert len(hits) == 2
        # head-hit 得分应更高（关键词在前 30 行内有额外 +1 加权）
        head = next(h for h in hits if "head-hit" in h.path.name)
        tail = next(h for h in hits if "tail-hit" in h.path.name)
        assert head.score > tail.score
    print("OK recall() head section bonus")


# ────────────────────────── format_context() ──────────────────────────


def test_format_context_empty():
    assert Dreamer.format_context([]) == ""
    print("OK format_context empty")


def test_format_context_renders_block():
    """有 hits → 用 <related past experience> 包起来。"""
    hits = [
        DreamHit(path=Path("/tmp/d1.md"), score=3, snippet="### 任务\nrefactor"),
        DreamHit(path=Path("/tmp/d2.md"), score=2, snippet="### 任务\nfix bug"),
    ]
    out = Dreamer.format_context(hits)
    assert "<related past experience>" in out
    assert "</related past experience>" in out
    assert "## d1" in out
    assert "## d2" in out
    assert "refactor" in out
    print("OK format_context renders block")


def test_format_context_respects_max_chars():
    big = "x" * 5000
    hits = [DreamHit(path=Path(f"/tmp/d{i}.md"), score=1, snippet=big) for i in range(5)]
    out = Dreamer.format_context(hits, max_chars=3000)
    # 只能塞下 1 条左右，肯定不到 5 条
    assert out.count("## d") < 5
    print("OK format_context respects max_chars")


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    test_slug_basic()
    test_extract_keywords_filters_short()
    test_truncate_lines_under_limit_unchanged()
    test_truncate_lines_over_limit_truncates()
    test_extract_snippet_skips_frontmatter()

    test_dream_no_summarizer_returns_none()
    test_dream_empty_task_or_output_returns_none()
    test_dream_writes_file_with_frontmatter()
    test_dream_truncates_long_output()
    test_dream_summarizer_failure_returns_none()

    test_recall_returns_empty_when_no_dreams()
    test_recall_finds_keyword_matching_dream()
    test_recall_returns_top_k_only()
    test_recall_filters_below_min_score()
    test_recall_head_section_gets_bonus()

    test_format_context_empty()
    test_format_context_renders_block()
    test_format_context_respects_max_chars()

    print("\nAll dreamer tests passed.")
