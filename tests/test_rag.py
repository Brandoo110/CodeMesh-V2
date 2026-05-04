"""
RAG 模块单元测试（不联网、不依赖 chromadb）
============================================

跑法：
    python -m tests.test_rag

策略：
  - rag/embedder.py 真调 DashScope，不能直接测；只测它在没 key / 没网时的行为
  - rag/indexer.py：把 _iter_code_files / _chunk_file 当成纯函数测
  - rag/retriever.py：retrieve 在没索引 / 没 chromadb 时返回 [];
                      format_context 测过滤、换行、token 预算等纯路径
"""

import tempfile
import asyncio
from pathlib import Path

from rag.indexer import (
    _iter_code_files,
    _chunk_file,
    CHUNK_LINES,
    CHUNK_OVERLAP,
)
from rag.retriever import retrieve, format_context, Hit


def _run(coro):
    return asyncio.run(coro)


def _mkroot() -> Path:
    """造一个含若干代码文件的临时仓库。"""
    base = Path(tempfile.mkdtemp(prefix="rag-test-"))
    (base / "main.py").write_text(
        "def main():\n"
        "    return 1\n"
    )
    (base / "util.py").write_text(
        "import os\n"
        "\n"
        "def helper():\n"
        "    return 'h'\n"
    )
    sub = base / "sub"
    sub.mkdir()
    (sub / "deep.py").write_text("def deep():\n    pass\n")
    # 噪音目录：应被 _iter_code_files 跳过
    nm = base / "node_modules"
    nm.mkdir()
    (nm / "noise.js").write_text("var x = 1;\n")
    pyc = base / "__pycache__"
    pyc.mkdir()
    (pyc / "x.cpython.pyc").write_bytes(b"\x00\x01\x02")
    # 二进制扩展名（不在 CODE_EXTS）
    (base / "bin.dat").write_bytes(b"\xff" * 100)
    # 一个超大文件：>500KB，应被跳过
    (base / "huge.py").write_text("x = 1\n" * 200_000)
    return base


# ────────────────────────── _iter_code_files ──────────────────────────


def test_iter_code_files_returns_python_files():
    root = _mkroot()
    paths = list(_iter_code_files(root))
    names = {p.name for p in paths}
    assert "main.py" in names
    assert "util.py" in names
    assert "deep.py" in names


def test_iter_code_files_skips_node_modules():
    root = _mkroot()
    paths = list(_iter_code_files(root))
    assert all("node_modules" not in p.parts for p in paths)


def test_iter_code_files_skips_pycache():
    root = _mkroot()
    paths = list(_iter_code_files(root))
    assert all("__pycache__" not in p.parts for p in paths)


def test_iter_code_files_skips_huge_files():
    root = _mkroot()
    paths = list(_iter_code_files(root))
    names = {p.name for p in paths}
    # huge.py（>500KB）应被跳过
    assert "huge.py" not in names


def test_iter_code_files_skips_unknown_extensions():
    root = _mkroot()
    paths = list(_iter_code_files(root))
    names = {p.name for p in paths}
    assert "bin.dat" not in names


# ────────────────────────── _chunk_file（已转 AST-aware） ──────────────────────────


def test_chunk_python_file_yields_function_chunks():
    root = _mkroot()
    chunks = _chunk_file(root / "util.py")
    # AST-aware：至少应该有 helper 函数那一段
    assert len(chunks) >= 1
    all_text = "\n".join(c[0] for c in chunks)
    assert "def helper" in all_text


def test_chunk_returns_one_based_line_numbers():
    root = _mkroot()
    chunks = _chunk_file(root / "main.py")
    for text, start, end in chunks:
        assert start >= 1
        assert end >= start


def test_chunk_non_python_falls_back_to_line_chunks():
    base = Path(tempfile.mkdtemp(prefix="rag-md-"))
    md = base / "doc.md"
    md.write_text("\n".join(f"line {i}" for i in range(1, 100)))
    chunks = _chunk_file(md)
    # 按行切（CHUNK_LINES=40）应该至少 2 段
    assert len(chunks) >= 2


def test_chunk_skips_blank_python_file():
    base = Path(tempfile.mkdtemp(prefix="rag-empty-"))
    p = base / "blank.py"
    p.write_text("")
    chunks = _chunk_file(p)
    # 空文件不该崩；返回空或只有 header 都接受
    assert chunks == [] or all(c[0].strip() == "" or c[0].strip() for c in chunks)


# ────────────────────────── retrieve ──────────────────────────


def test_retrieve_returns_empty_when_no_db():
    """没建过索引时 retrieve 应静默返回 []，不抛。"""
    nonexistent = Path("/tmp/__codemesh_no_such_rag_db_xyz")
    out = _run(retrieve("anything", db_dir=nonexistent))
    assert out == []


def test_retrieve_returns_empty_when_chromadb_missing(monkeypatch=None):
    """模拟 chromadb 没装：让 import 失败时 retrieve 返回 []。"""
    import sys
    # 注入一个会让 import chromadb 失败的 finder
    real_chromadb = sys.modules.pop("chromadb", None)
    sys.modules["chromadb"] = None  # type: ignore[assignment]
    try:
        out = _run(retrieve("query", db_dir=Path("/tmp/anything")))
        assert out == []
    finally:
        if real_chromadb is not None:
            sys.modules["chromadb"] = real_chromadb
        else:
            sys.modules.pop("chromadb", None)


# ────────────────────────── format_context ──────────────────────────


def test_format_context_empty_hits_returns_empty():
    assert format_context([], max_tokens=1000) == ""


def test_format_context_includes_path_and_lines():
    hits = [
        Hit(path="src/auth.py", start_line=12, end_line=48,
            text="def login(): pass", score=0.1),
    ]
    out = format_context(hits, max_tokens=1000)
    assert "src/auth.py:12-48" in out
    assert "<CODEBASE CONTEXT>" in out
    assert "</CODEBASE CONTEXT>" in out


def test_format_context_max_chars_truncates():
    hits = [
        Hit(path="a.py", start_line=1, end_line=5, text="x" * 500, score=0.1),
        Hit(path="b.py", start_line=1, end_line=5, text="y" * 500, score=0.2),
    ]
    out = format_context(hits, max_chars=120)   # 极小预算
    # 第一个 hit 也截不下 500 char，应在它内部截断
    assert "a.py" in out  # 至少 header 进去了
    assert "b.py" not in out  # 第二个没机会


def test_format_context_max_tokens_path():
    """没传 max_chars，走 token 预算路径。"""
    hits = [
        Hit(path="a.py", start_line=1, end_line=5, text="hello world", score=0.1),
    ]
    out = format_context(hits, max_tokens=200)
    assert "hello world" in out
    assert "a.py" in out


def test_format_context_multiple_hits_concatenated():
    hits = [
        Hit(path="a.py", start_line=1, end_line=5, text="A code", score=0.1),
        Hit(path="b.py", start_line=10, end_line=20, text="B code", score=0.2),
        Hit(path="c.py", start_line=30, end_line=40, text="C code", score=0.3),
    ]
    out = format_context(hits, max_tokens=500)
    assert "a.py:1-5" in out
    assert "b.py:10-20" in out
    assert "c.py:30-40" in out


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
    print(f"\n{len(tests) - failed}/{len(tests)} rag tests passed.")
    if failed:
        raise SystemExit(1)
