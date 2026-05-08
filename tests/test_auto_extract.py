"""
Auto Memory Extraction 单元测试
==================================

跑法：
    python -m tests.test_auto_extract

不调真实 API；summarizer 用 fake async fn。
"""

import asyncio
import tempfile
from pathlib import Path

from memory.auto_extract import (
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    MemoryEntry,
    extract_and_save,
    parse_entries,
    update_memory_index,
)


# ─────────────────────────── helpers ───────────────────────────


def _fake_summarizer(reply: str):
    calls = []
    async def s(prompt: str) -> str:
        calls.append(prompt)
        return reply
    return s, calls


def _good_entry_text(name: str = "go_engineer", type_: str = "user") -> str:
    return f"""---ENTRY---
name: {name}
description: User is a senior Go engineer
type: {type_}

**Why:** They mentioned 10 years of Go experience.
**How to apply:** Lean on Go idioms first, explain comparisons sparingly.
"""


# ─────────────────────────── parse_entries ───────────────────────────


def test_parse_single_valid_entry():
    raw = _good_entry_text()
    entries = parse_entries(raw)
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "go_engineer"
    assert e.type == "user"
    assert "Senior" in e.description or "senior" in e.description
    assert "Why:" in e.body
    assert "How to apply:" in e.body
    print("OK parse: single valid entry")


def test_parse_multiple_entries():
    raw = _good_entry_text("e1", "user") + "\n" + _good_entry_text("e2", "feedback")
    entries = parse_entries(raw)
    assert len(entries) == 2
    types = {e.type for e in entries}
    assert types == {"user", "feedback"}
    print("OK parse: multiple entries split by ---ENTRY---")


def test_parse_drops_invalid_type():
    """type 不在 4 种枚举里 → 静默丢弃。"""
    raw = _good_entry_text("bad", "wrongtype")
    entries = parse_entries(raw)
    assert entries == []
    print("OK parse: drops invalid type")


def test_parse_drops_missing_fields():
    raw = "---ENTRY---\nname: foo\ntype: user\n\nbody"  # 缺 description
    entries = parse_entries(raw)
    assert entries == []
    print("OK parse: drops entries with missing fields")


def test_parse_empty_output():
    """LLM 判断没值得记的就空 → parse 返回 []。"""
    assert parse_entries("") == []
    assert parse_entries("just some explanation, no entries") == []
    print("OK parse: empty output yields []")


# ─────────────────────────── MemoryEntry.to_markdown ───────────────────────────


def test_to_markdown_has_frontmatter_and_body():
    e = MemoryEntry(
        name="test", description="d", type="user", body="**Why:** x\n**How to apply:** y"
    )
    md = e.to_markdown()
    assert md.startswith("---\n")
    assert "name: test" in md
    assert "type: user" in md
    assert "Why:" in md
    print("OK to_markdown structure")


# ─────────────────────────── update_memory_index ───────────────────────────


def test_index_creates_file_on_first_write():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        e = MemoryEntry(name="x", description="desc", type="user", body="body")
        update_memory_index(d, e)
        idx = d / "MEMORY.md"
        assert idx.exists()
        text = idx.read_text(encoding="utf-8")
        assert "[x](x.md)" in text
        assert "desc" in text
    print("OK index: created on first write")


def test_index_dedupes():
    """同名条目不重复加。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        e = MemoryEntry(name="dup", description="d", type="user", body="b")
        update_memory_index(d, e)
        update_memory_index(d, e)
        text = (d / "MEMORY.md").read_text(encoding="utf-8")
        assert text.count("[dup](dup.md)") == 1
    print("OK index: dedupes same entry")


def test_index_enforces_line_limit():
    """超过 200 行硬上限时按 LRU 删最旧。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 塞 250 条
        for i in range(250):
            e = MemoryEntry(name=f"e{i}", description="d", type="user", body="b")
            update_memory_index(d, e)
        text = (d / "MEMORY.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        # 不超过 MAX_INDEX_LINES
        assert len(lines) <= MAX_INDEX_LINES
        # 最早的几条应该被删
        assert "[e0](e0.md)" not in text
        # 最新的还在
        assert "[e249](e249.md)" in text
    print("OK index: enforces 200-line cap (LRU)")


def test_index_enforces_byte_limit():
    """超过 25 KB 时也会按 LRU 缩。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 用很长的 description 撑大每行
        for i in range(50):
            e = MemoryEntry(
                name=f"e{i}",
                description="x" * 1000,  # 每行 ~1KB
                type="user",
                body="b",
            )
            update_memory_index(d, e)
        text = (d / "MEMORY.md").read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) <= MAX_INDEX_BYTES + 1000  # 留 1K 容差
    print("OK index: enforces 25KB cap")


# ─────────────────────────── extract_and_save 端到端 ───────────────────────────


def test_extract_and_save_writes_files_and_index():
    raw = _good_entry_text("user_role", "user") + "\n" + _good_entry_text("test_pref", "feedback")
    summarizer, _ = _fake_summarizer(raw)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        paths = asyncio.run(extract_and_save(
            task="工作完成",
            output="完成 auth 重构",
            summarizer=summarizer,
            memory_dir=d,
        ))
        assert len(paths) == 2
        assert all(p.exists() for p in paths)
        # MEMORY.md 索引含两条
        idx = (d / "MEMORY.md").read_text(encoding="utf-8")
        assert "user_role" in idx
        assert "test_pref" in idx
    print("OK extract_and_save: writes files + index")


def test_extract_and_save_empty_output_no_op():
    """task / output 为空时跳过。"""
    summarizer, calls = _fake_summarizer("")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        paths = asyncio.run(extract_and_save(
            task="", output="x", summarizer=summarizer, memory_dir=d
        ))
        assert paths == []
        assert calls == []
    print("OK extract_and_save: empty input no-op")


def test_extract_and_save_swallows_summarizer_exception():
    async def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        paths = asyncio.run(extract_and_save(
            task="t", output="o", summarizer=boom, memory_dir=d
        ))
        assert paths == []
    print("OK extract_and_save: swallows summarizer exception")


def test_extract_and_save_returns_empty_when_llm_finds_nothing():
    """LLM 判断本次没事可记 → 返回空 list，不写任何文件。"""
    summarizer, _ = _fake_summarizer("This task had no learnings worth saving.")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        paths = asyncio.run(extract_and_save(
            task="t", output="o", summarizer=summarizer, memory_dir=d
        ))
        assert paths == []
        # MEMORY.md 也不该被建（没条目）
        assert not (d / "MEMORY.md").exists()
    print("OK extract_and_save: empty when LLM finds nothing")


# ─────────────────────────── runner ───────────────────────────


if __name__ == "__main__":
    test_parse_single_valid_entry()
    test_parse_multiple_entries()
    test_parse_drops_invalid_type()
    test_parse_drops_missing_fields()
    test_parse_empty_output()

    test_to_markdown_has_frontmatter_and_body()

    test_index_creates_file_on_first_write()
    test_index_dedupes()
    test_index_enforces_line_limit()
    test_index_enforces_byte_limit()

    test_extract_and_save_writes_files_and_index()
    test_extract_and_save_empty_output_no_op()
    test_extract_and_save_swallows_summarizer_exception()
    test_extract_and_save_returns_empty_when_llm_finds_nothing()

    print("\nAll auto_extract tests passed.")
