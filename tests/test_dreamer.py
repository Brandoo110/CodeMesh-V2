"""
真 L6 Dreamer 单元测试（4 阶段巩固）
=========================================

跑法：
    python -m tests.test_dreamer

不调真实 API；summarizer 用 fake async fn。
"""

import asyncio
import tempfile
from pathlib import Path

from feedback.dreamer import (
    ConsolidationPlan,
    Dreamer,
    parse_consolidation_plan,
    rebuild_memory_index,
)


# ─────────────────────────── helpers ───────────────────────────


def _fake_summarizer(reply: str):
    calls = []
    async def s(prompt: str) -> str:
        calls.append(prompt)
        return reply
    return s, calls


def _seed_memory(memory_dir: Path, name: str, type_: str = "user", body: str = "default body") -> Path:
    """直接写一条 memory file，跳过 LLM 抽取。"""
    path = memory_dir / f"{name}.md"
    text = (
        f"---\n"
        f"name: {name}\n"
        f"description: {name} description\n"
        f"type: {type_}\n"
        f"---\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


# ─────────────────────────── parse_consolidation_plan ───────────────────────────


def test_parse_empty_plan():
    """LLM 输出空 PLAN 块 → 空计划。"""
    raw = "---PLAN---\n---PLAN---"
    plan = parse_consolidation_plan(raw)
    assert plan.deletes == []
    assert plan.merges == []
    assert plan.rewrites == {}
    print("OK parse: empty PLAN block")


def test_parse_no_plan_block():
    """LLM 没按格式输出 → 空计划（保守）。"""
    raw = "I think we should delete some entries"
    plan = parse_consolidation_plan(raw)
    assert plan.deletes == []
    assert plan.merges == []
    assert plan.rewrites == {}
    print("OK parse: no PLAN block returns empty")


def test_parse_delete_commands():
    raw = "---PLAN---\nDELETE foo\nDELETE bar\n---PLAN---"
    plan = parse_consolidation_plan(raw)
    assert plan.deletes == ["foo", "bar"]
    print("OK parse: DELETE commands")


def test_parse_merge_commands():
    raw = "---PLAN---\nMERGE old_pref -> new_pref\n---PLAN---"
    plan = parse_consolidation_plan(raw)
    assert plan.merges == [("old_pref", "new_pref")]
    print("OK parse: MERGE source -> target")


def test_parse_rewrite_with_body():
    raw = """---PLAN---
REWRITE my_entry
**Why:** updated reason here
**How to apply:** new instruction
---END---
---PLAN---"""
    plan = parse_consolidation_plan(raw)
    assert "my_entry" in plan.rewrites
    body = plan.rewrites["my_entry"]
    assert "**Why:**" in body
    assert "**How to apply:**" in body
    print("OK parse: REWRITE with body")


def test_parse_mixed_commands():
    raw = """---PLAN---
DELETE old_thing
MERGE a -> b
REWRITE c
**Why:** new
**How to apply:** apply
---END---
---PLAN---"""
    plan = parse_consolidation_plan(raw)
    assert plan.deletes == ["old_thing"]
    assert plan.merges == [("a", "b")]
    assert "c" in plan.rewrites
    print("OK parse: mixed DELETE + MERGE + REWRITE")


# ─────────────────────────── orient (Phase 1) ───────────────────────────


def test_orient_reads_all_memory_files():
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "alpha", "user")
        _seed_memory(memory_dir, "beta", "feedback")
        _seed_memory(memory_dir, "gamma", "project")
        # MEMORY.md 不应被列为 entry
        (memory_dir / "MEMORY.md").write_text("# index", encoding="utf-8")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        names = {m.name for m in memories}
        assert names == {"alpha", "beta", "gamma"}
        # MEMORY.md 不在 list
        assert "MEMORY" not in names
    print("OK orient: reads all memories, skips MEMORY.md")


def test_orient_skips_files_without_name():
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "good", "user")
        # 写一个没有 frontmatter 的脏文件
        (memory_dir / "broken.md").write_text("just random body without frontmatter", encoding="utf-8")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        assert len(memories) == 1
        assert memories[0].name == "good"
    print("OK orient: skips files without frontmatter name")


# ─────────────────────────── apply_plan (Phase 4) ───────────────────────────


def test_apply_plan_deletes():
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "to_delete")
        _seed_memory(memory_dir, "to_keep")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        plan = ConsolidationPlan(deletes=["to_delete"], merges=[], rewrites={})
        result = d.apply_plan(plan, memories)
        assert "to_delete" in result["deleted"]
        assert not (memory_dir / "to_delete.md").exists()
        assert (memory_dir / "to_keep.md").exists()
    print("OK apply_plan: DELETE removes file")


def test_apply_plan_merges_drops_source():
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "src")
        _seed_memory(memory_dir, "dst")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        plan = ConsolidationPlan(deletes=[], merges=[("src", "dst")], rewrites={})
        result = d.apply_plan(plan, memories)
        assert ("src", "dst") in result["merged"]
        assert not (memory_dir / "src.md").exists()
        assert (memory_dir / "dst.md").exists()  # target 保留
    print("OK apply_plan: MERGE drops source, keeps target")


def test_apply_plan_rewrites_body():
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "entry", body="OLD body")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        plan = ConsolidationPlan(
            deletes=[], merges=[],
            rewrites={"entry": "**Why:** new reason\n**How to apply:** new way"},
        )
        result = d.apply_plan(plan, memories)
        assert "entry" in result["rewritten"]
        text = (memory_dir / "entry.md").read_text(encoding="utf-8")
        assert "OLD body" not in text
        assert "new reason" in text
        # frontmatter 仍在
        assert "name: entry" in text
    print("OK apply_plan: REWRITE replaces body, keeps frontmatter")


def test_apply_plan_rebuilds_memory_index():
    """apply_plan 末尾会调 rebuild_memory_index，MEMORY.md 应反映最新状态。"""
    summarizer, _ = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "alive")
        _seed_memory(memory_dir, "doomed")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        memories = d.orient()
        plan = ConsolidationPlan(deletes=["doomed"], merges=[], rewrites={})
        d.apply_plan(plan, memories)

        idx_text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "alive" in idx_text
        assert "doomed" not in idx_text
    print("OK apply_plan: rebuilds MEMORY.md after operations")


# ─────────────────────────── rebuild_memory_index ───────────────────────────


def test_rebuild_index_sorts_by_mtime():
    import time
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td)
        old = _seed_memory(memory_dir, "old_one")
        # 延迟一点，让 mtime 不同
        time.sleep(0.05)
        new = _seed_memory(memory_dir, "new_one")

        rebuild_memory_index(memory_dir)
        idx = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
        # new 在前
        assert idx.index("new_one") < idx.index("old_one")
    print("OK rebuild_index: newest first")


def test_rebuild_index_excludes_self():
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td)
        _seed_memory(memory_dir, "x")
        # 即使 MEMORY.md 已存在，也不该把自己列进去
        (memory_dir / "MEMORY.md").write_text("# old\n", encoding="utf-8")

        rebuild_memory_index(memory_dir)
        idx = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "MEMORY.md" not in idx
        assert "x" in idx
    print("OK rebuild_index: excludes MEMORY.md itself")


# ─────────────────────────── 完整 4 阶段流程 ───────────────────────────


def test_dream_full_flow_with_force():
    """end-to-end：种 3 条 → LLM 给 plan 删 1 条 → 验证文件状态。"""
    plan_reply = """---PLAN---
DELETE outdated
---PLAN---"""
    summarizer, calls = _fake_summarizer(plan_reply)
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "outdated")
        _seed_memory(memory_dir, "current_a")
        _seed_memory(memory_dir, "current_b")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer, min_sessions=2)
        result = asyncio.run(d.dream(force=True))
        assert result is not None
        assert "outdated" in result["deleted"]
        assert not (memory_dir / "outdated.md").exists()
        assert (memory_dir / "current_a.md").exists()
        assert (memory_dir / "current_b.md").exists()
        # MEMORY.md 应不含 outdated
        idx = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "outdated" not in idx
        # summarizer 被调一次
        assert len(calls) == 1
    print("OK dream(): full 4-phase flow with force")


def test_dream_skips_when_too_few_memories():
    """少于 2 条记忆时不动 LLM，直接返回空结果。"""
    summarizer, calls = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "only_one")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer)
        result = asyncio.run(d.dream(force=True))
        assert result == {"deleted": [], "merged": [], "rewritten": []}
        assert calls == []  # LLM 没被调
    print("OK dream(): skips when fewer than 2 memories")


def test_dream_respects_session_count_gate():
    """session count gate 默认 5；只有 2 条 memory 应被挡。"""
    summarizer, calls = _fake_summarizer("---PLAN------PLAN---")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        _seed_memory(memory_dir, "a")
        _seed_memory(memory_dir, "b")

        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer, min_scan_minutes=0)
        # 不 force，依赖正常门控
        ok, reason = d.should_dream()
        assert ok is False
        assert "memory entries" in reason
        # dream() 也应跳过
        result = asyncio.run(d.dream())
        assert result is None
    print("OK dream(): session count gate blocks insufficient memories")


def test_dream_disabled_returns_none():
    summarizer, calls = _fake_summarizer("")
    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        for i in range(6):
            _seed_memory(memory_dir, f"e{i}")
        d = Dreamer(memory_dir=memory_dir, summarizer=summarizer, enabled=False)
        result = asyncio.run(d.dream(force=True))
        assert result is None
        assert calls == []
    print("OK dream(): disabled returns None even with force")


def test_dream_summarizer_failure_returns_empty_plan():
    """summarizer 抛异常 → 不 propagate，按空 plan 处理（保守）。"""
    async def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    with tempfile.TemporaryDirectory() as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir()
        for i in range(3):
            _seed_memory(memory_dir, f"e{i}")

        d = Dreamer(memory_dir=memory_dir, summarizer=boom, min_sessions=2)
        result = asyncio.run(d.dream(force=True))
        # 失败时返回空操作摘要而非 None
        assert result == {"deleted": [], "merged": [], "rewritten": []}
        # 文件都还在
        assert (memory_dir / "e0.md").exists()
    print("OK dream(): swallows summarizer exception, returns empty result")


# ─────────────────────────── runner ───────────────────────────


if __name__ == "__main__":
    test_parse_empty_plan()
    test_parse_no_plan_block()
    test_parse_delete_commands()
    test_parse_merge_commands()
    test_parse_rewrite_with_body()
    test_parse_mixed_commands()

    test_orient_reads_all_memory_files()
    test_orient_skips_files_without_name()

    test_apply_plan_deletes()
    test_apply_plan_merges_drops_source()
    test_apply_plan_rewrites_body()
    test_apply_plan_rebuilds_memory_index()

    test_rebuild_index_sorts_by_mtime()
    test_rebuild_index_excludes_self()

    test_dream_full_flow_with_force()
    test_dream_skips_when_too_few_memories()
    test_dream_respects_session_count_gate()
    test_dream_disabled_returns_none()
    test_dream_summarizer_failure_returns_empty_plan()

    print("\nAll real-dreamer tests passed.")
