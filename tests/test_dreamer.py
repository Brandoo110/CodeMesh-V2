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
import time
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


def _make_dreamer(tmp: Path, summarizer=None, **kwargs) -> Dreamer:
    """快速构造一个用临时目录的 Dreamer。kwargs 透传给 Dreamer 构造器。"""
    return Dreamer(dreams_dir=tmp / "dreams", summarizer=summarizer, **kwargs)


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
        path = asyncio.run(d.dream(task="t", output="o", force=True))
        assert path is None
        assert d.dreams_dir.exists()
        assert list(d.dreams_dir.glob("*.md")) == []
    print("OK dream() no summarizer is no-op")


def test_dream_empty_task_or_output_returns_none():
    """task / output 都得有，否则跳过。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        assert asyncio.run(d.dream(task="", output="x", force=True)) is None
        assert asyncio.run(d.dream(task="x", output="", force=True)) is None
    print("OK dream() requires task and output")


def test_dream_writes_file_with_frontmatter():
    """正常路径：force=True 跳过门控，写一个 .md 文件含 frontmatter + 正文。"""
    summarizer, calls = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        path = asyncio.run(d.dream(
            task="重构 auth 模块",
            output="完成，新增了 Token 类",
            force=True,
        ))
        assert path is not None
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "task: 重构 auth 模块" in text
        assert "created:" in text
        assert "### 任务" in text
        assert "### 关键决策" in text
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
        path = asyncio.run(d.dream(task="t", output="o", force=True))
        text = path.read_text(encoding="utf-8")
        body = text.split("---", 2)[2] if text.startswith("---") else text
        assert "truncated" in body
        assert MAX_DREAM_LINES <= 100
    print("OK dream() truncates long output")


def test_dream_summarizer_failure_returns_none():
    """summarizer 抛异常 → dream() 返回 None，不向上抛。"""
    async def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=boom)
        path = asyncio.run(d.dream(task="t", output="o", force=True))
        assert path is None
        assert list(d.dreams_dir.glob("*.md")) == []
    print("OK dream() swallows summarizer exception")


# ────────────────────────── 5 门触发（v2，2026-05-09）──────────────────────────


def test_should_dream_disabled_gate():
    """Gate 1: enabled=False 应直接 false。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        d.enabled = False
        ok, reason = d.should_dream()
        assert ok is False
        assert "disabled" in reason
    print("OK Gate 1 (Enabled) blocks when disabled")


def test_should_dream_session_count_gate():
    """Gate 4: 累计 session 数 < min_sessions 时不触发。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        # min_scan_minutes=0 关掉 Gate 3 的 rate-limit，否则连续测试会被它挡住
        d = _make_dreamer(Path(td), summarizer=summarizer, min_scan_minutes=0)
        d.min_sessions = 5
        ok, reason = d.should_dream()
        assert ok is False
        assert "pending sessions" in reason
        for _ in range(3):
            d.should_dream()
        # 第 5 次：pending=5, ≥ 5 → 通过 Gate 4，进 Gate 5（无锁）→ true
        ok, _ = d.should_dream()
        assert ok is True
    print("OK Gate 4 (Session count) requires ≥ min_sessions")


def test_should_dream_time_gate():
    """Gate 2: 距上次 dream < min_hours 时不触发。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer, min_hours=24, min_sessions=1)
        # 写入"上次 dream = 1 小时前"
        (d.dreams_dir / ".last_dream").write_text(
            str(time.time() - 3600), encoding="utf-8"
        )
        ok, reason = d.should_dream()
        assert ok is False
        assert "since last dream" in reason
    print("OK Gate 2 (Time) blocks if too soon")


def test_should_dream_scan_throttle_gate():
    """Gate 3: 距上次 scan < min_scan_minutes 时不触发。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer, min_scan_minutes=10, min_sessions=1)
        # 上次 scan = 5 分钟前
        (d.dreams_dir / ".last_scan").write_text(
            str(time.time() - 300), encoding="utf-8"
        )
        ok, reason = d.should_dream()
        assert ok is False
        assert "since last scan" in reason
    print("OK Gate 3 (Scan throttle) blocks if scanned too recently")


def test_should_dream_lock_gate():
    """Gate 5: 锁被活进程持有时不触发。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer, min_sessions=1)
        # 写自己的 PID 进锁文件 —— 自己活着，所以视为锁被占用
        import os as _os
        (d.dreams_dir / ".consolidate-lock").write_text(
            f"{_os.getpid()}:{time.time()}", encoding="utf-8"
        )
        ok, reason = d.should_dream()
        assert ok is False
        assert "lock" in reason.lower()
    print("OK Gate 5 (Lock) blocks when held by alive PID")


def test_should_dream_lock_gate_stale_pid():
    """Gate 5: 锁被死 PID 持有时应可强夺。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer, min_sessions=1)
        # PID=1 通常在 macOS/Linux 是 launchd/init，但写一个明显不存在的 PID
        # （非常大的数字）当作 stale
        (d.dreams_dir / ".consolidate-lock").write_text(
            f"99999999:{time.time()}", encoding="utf-8"
        )
        ok, reason = d.should_dream()
        # PID 死了，前面 4 门都过 → 应该 true（如果 pending_sessions 也够）
        # 第一次调 should_dream，pending=1 ≥ 1 → 过
        assert ok is True
    print("OK Gate 5 (Lock) allows steal from dead PID")


def test_dream_skips_when_gates_block():
    """无 force 时 dream() 应被 Gate 4（pending=1<5）挡住，不调 summarizer。"""
    summarizer, calls = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        # 默认 min_sessions=5，第一次调用 pending=1 应被挡
        path = asyncio.run(d.dream(task="t", output="o"))
        assert path is None
        assert calls == []   # summarizer 没被调
        assert list(d.dreams_dir.glob("*.md")) == []
    print("OK dream() respects gates when force=False")


def test_dream_force_overrides_throttle_and_count_gates():
    """force=True 跳过 Gate 2-4（time / throttle / count），但仍走 Gate 1（enabled）和 Gate 5（锁）。

    enabled=False 是用户显式关掉，force 不应跨越；锁是并发安全保障，force 也不能破。
    """
    summarizer, calls = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        # Gate 4 默认会挡（pending=1<5），但 force 跳过
        path = asyncio.run(d.dream(task="t", output="o", force=True))
        assert path is not None
        assert len(calls) == 1

    # 验证 force 仍尊重 enabled=False
    summarizer2, calls2 = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d2 = _make_dreamer(Path(td), summarizer=summarizer2)
        d2.enabled = False
        path = asyncio.run(d2.dream(task="t", output="o", force=True))
        assert path is None  # enabled=False 即使 force 也挡
        assert calls2 == []
    print("OK force=True overrides Gates 2-4 but not Gate 1 (enabled)")


def test_dream_resets_pending_count_on_success():
    """成功 dream 后 .pending_sessions 应重置为 0。"""
    summarizer, _ = _fake_summarizer()
    with tempfile.TemporaryDirectory() as td:
        d = _make_dreamer(Path(td), summarizer=summarizer)
        asyncio.run(d.dream(task="t", output="o", force=True))
        pending_path = d.dreams_dir / ".pending_sessions"
        if pending_path.exists():
            assert pending_path.read_text().strip() == "0"
        # last_dream 也应被更新
        assert (d.dreams_dir / ".last_dream").exists()
    print("OK successful dream resets pending_sessions and updates last_dream")


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

    # 5 门触发（v2 升级）
    test_should_dream_disabled_gate()
    test_should_dream_session_count_gate()
    test_should_dream_time_gate()
    test_should_dream_scan_throttle_gate()
    test_should_dream_lock_gate()
    test_should_dream_lock_gate_stale_pid()
    test_dream_skips_when_gates_block()
    test_dream_force_overrides_throttle_and_count_gates()
    test_dream_resets_pending_count_on_success()

    print("\nAll dreamer tests passed.")
