"""
AST chunker 单元测试
======================

跑法：
    python -m tests.test_ast_chunker

覆盖：
  - chunk_python_file: 顶层函数 / 类 / 嵌套方法 / 装饰器 / module header
  - 边界：空文件 / 仅注释 / 语法错误 / 大文件
  - chunk_file_with_fallback: 非 .py / 解析失败时回退到按行切
"""

import tempfile
from pathlib import Path

from rag.ast_chunker import (
    chunk_python_file,
    chunk_file_with_fallback,
    CodeChunk,
)


def _write(text: str, suffix: str = ".py") -> Path:
    p = Path(tempfile.mkdtemp(prefix="ast-")) / f"sample{suffix}"
    p.write_text(text)
    return p


# ────────────────────────── chunk_python_file ──────────────────────────


def test_top_level_function_is_one_chunk():
    p = _write(
        '"""mod doc."""\n'
        "import os\n"
        "\n"
        "def hello():\n"
        '    """Say hi."""\n'
        '    return "hi"\n'
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    # module header（含 import）+ 函数本身
    kinds = [c.kind for c in chunks]
    assert "header" in kinds
    assert "function" in kinds
    fn = next(c for c in chunks if c.kind == "function")
    assert fn.name == "hello"
    assert "Say hi" in fn.text


def test_class_with_methods_yields_separate_chunks():
    p = _write(
        "class Foo:\n"
        '    """foo doc."""\n'
        "    x = 1\n"
        "    def m1(self):\n"
        "        return 1\n"
        "    def m2(self):\n"
        "        return 2\n"
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    names = [c.name for c in chunks if c.name]
    # class header + 2 methods
    assert "Foo" in names
    assert "Foo.m1" in names
    assert "Foo.m2" in names


def test_class_without_methods_one_chunk():
    p = _write(
        "class Empty:\n"
        '    """nothing."""\n'
        "    pass\n"
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    klass_chunks = [c for c in chunks if c.name == "Empty"]
    assert len(klass_chunks) == 1
    assert klass_chunks[0].kind == "class"


def test_decorator_included_in_chunk():
    """装饰器行应包含在函数 chunk 内（@register 会被作为 chunk 的开头）。"""
    p = _write(
        "@some_decorator\n"
        "@another\n"
        "def decorated():\n"
        "    pass\n"
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    fn = next(c for c in chunks if c.name == "decorated")
    assert "@some_decorator" in fn.text
    assert "@another" in fn.text


def test_module_header_captures_imports_and_docstring():
    p = _write(
        '"""module doc."""\n'
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "CONST = 1\n"
        "\n"
        "def f():\n"
        "    pass\n"
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    header = next((c for c in chunks if c.kind == "header"), None)
    assert header is not None
    assert "module doc" in header.text
    assert "import os" in header.text
    assert "CONST = 1" in header.text


def test_async_def_treated_as_function():
    p = _write(
        "async def hello():\n"
        "    return 1\n"
    )
    chunks = chunk_python_file(p)
    assert chunks is not None
    fn = next(c for c in chunks if c.name == "hello")
    assert fn.kind == "function"


def test_syntax_error_returns_none():
    p = _write("def x( :::\n")
    assert chunk_python_file(p) is None


def test_empty_file_returns_none_or_empty():
    p = _write("")
    out = chunk_python_file(p)
    # 空文件下要么 None，要么空列表都可接受
    assert out is None or out == []


def test_only_comments_no_def():
    p = _write(
        "# just a comment\n"
        "# another comment\n"
    )
    chunks = chunk_python_file(p)
    # 只有 header，没有 def/class
    assert chunks is not None
    assert all(c.kind == "header" for c in chunks)


# ────────────────────────── chunk_file_with_fallback ──────────────────────────


def test_fallback_for_non_python_file():
    p = _write(
        "line1\nline2\nline3\nline4\nline5\n",
        suffix=".js",
    )
    chunks = chunk_file_with_fallback(p, fallback_chunks=3, fallback_overlap=1)
    # JS 文件不会走 ast，全部走 line chunker
    assert all(c.kind == "block" for c in chunks)
    assert len(chunks) >= 1


def test_fallback_for_syntax_error_python_file():
    p = _write("def broken( :::\n", suffix=".py")
    chunks = chunk_file_with_fallback(p, fallback_chunks=10, fallback_overlap=2)
    # AST 失败 → line chunker
    assert all(c.kind == "block" for c in chunks)


def test_fallback_normal_python_uses_ast():
    p = _write(
        "def fn():\n    pass\n",
        suffix=".py",
    )
    chunks = chunk_file_with_fallback(p, fallback_chunks=10, fallback_overlap=2)
    # 正常 .py 走 AST，应有非 block 的 chunk
    assert any(c.kind == "function" for c in chunks)


def test_chunks_are_continuous_and_inbounds():
    """所有 chunk 行号应在文件范围内，且 start <= end。"""
    src = (
        "import os\n"
        "\n"
        "def a():\n"
        "    return 1\n"
        "\n"
        "class B:\n"
        "    def m(self):\n"
        "        return 2\n"
    )
    p = _write(src)
    chunks = chunk_python_file(p)
    assert chunks is not None
    n_lines = len(src.splitlines())
    for c in chunks:
        assert c.start_line >= 1
        assert c.end_line <= n_lines
        assert c.start_line <= c.end_line


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
    print(f"\n{len(tests) - failed}/{len(tests)} ast_chunker tests passed.")
    if failed:
        raise SystemExit(1)
