"""
LSP（AST-based）单元测试
==========================

跑法：
    python -m tests.test_lsp

覆盖：
  - SymbolLocation / iter_python_files
  - list_document_symbols：函数 / 类 / 嵌套方法 / 顶层变量
  - workspace_symbol_search：跨文件子串模糊匹配
  - go_to_definition：按 symbol 名 / 按 line:col 位置
  - find_references：词边界正则
  - hover：返回首个匹配
  - extract_symbol_at_position：边界条件
  - lsp_code 工具入口的格式化输出 + 错误分支
"""

import tempfile
from pathlib import Path

from execution.lsp import (
    SymbolLocation,
    iter_python_files,
    list_document_symbols,
    workspace_symbol_search,
    go_to_definition,
    find_references,
    hover,
    extract_symbol_at_position,
)
from execution.tools import lsp_code


# ────────────────────────── helpers ──────────────────────────


def _mkroot() -> Path:
    """造一个含 a.py / b.py / sub/c.py 的临时仓库。"""
    base = Path(tempfile.mkdtemp(prefix="lsp-test-"))
    (base / "a.py").write_text(
        '"""mod a."""\n'
        "TOP_CONST = 42\n"
        "\n"
        "def hello(name):\n"
        '    """Say hi."""\n'
        '    return f"hi {name}"\n'
        "\n"
        "class Greeter:\n"
        '    """A greeter."""\n'
        "    salutation = 'hello'\n"
        "    def greet(self, name):\n"
        '        """Greet someone."""\n'
        "        return hello(name)\n"
    )
    (base / "b.py").write_text(
        "from a import hello\n"
        "\n"
        "def shout(name):\n"
        "    return hello(name).upper()\n"
        "\n"
        "def hello():\n"
        "    return 'shadowed'\n"
    )
    sub = base / "sub"
    sub.mkdir()
    (sub / "c.py").write_text(
        "def deeply():\n"
        "    pass\n"
    )
    # 噪音目录：__pycache__ 应被跳过
    (base / "__pycache__").mkdir()
    (base / "__pycache__" / "junk.py").write_text("def should_be_skipped(): ...\n")
    return base


# ────────────────────────── iter_python_files ──────────────────────────


def test_iter_python_files_skips_pycache():
    root = _mkroot()
    files = iter_python_files(root)
    names = [f.name for f in files]
    assert "a.py" in names
    assert "b.py" in names
    assert "c.py" in names
    assert "junk.py" not in names


def test_iter_python_files_stable_order():
    root = _mkroot()
    files = iter_python_files(root)
    assert files == sorted(files)


# ────────────────────────── list_document_symbols ──────────────────────────


def test_list_document_symbols_finds_function():
    root = _mkroot()
    syms = list_document_symbols(root / "a.py")
    names = {s.name for s in syms}
    assert "hello" in names
    assert "Greeter" in names
    assert "Greeter.greet" in names
    assert "TOP_CONST" in names


def test_list_document_symbols_signature_and_docstring():
    root = _mkroot()
    syms = list_document_symbols(root / "a.py")
    hello = next(s for s in syms if s.name == "hello")
    assert hello.kind == "function"
    assert hello.signature == "def hello(name)"
    assert "Say hi" in hello.docstring


def test_list_document_symbols_handles_syntax_error():
    base = Path(tempfile.mkdtemp(prefix="lsp-bad-"))
    (base / "broken.py").write_text("def x( :::\n")
    syms = list_document_symbols(base / "broken.py")
    assert syms == []


# ────────────────────────── workspace_symbol_search ──────────────────────────


def test_workspace_symbol_substring_match():
    root = _mkroot()
    matches = workspace_symbol_search(root, "hello")
    names = [m.name for m in matches]
    # a.py 的 hello + b.py 的 hello 都该命中
    assert names.count("hello") >= 2


def test_workspace_symbol_case_insensitive():
    root = _mkroot()
    matches = workspace_symbol_search(root, "GREETER")
    assert any(m.name == "Greeter" for m in matches)


def test_workspace_symbol_empty_query():
    root = _mkroot()
    assert workspace_symbol_search(root, "") == []
    assert workspace_symbol_search(root, "   ") == []


# ────────────────────────── go_to_definition ──────────────────────────


def test_go_to_definition_by_symbol_name():
    root = _mkroot()
    matches = go_to_definition(root=root, file_path=root / "b.py", symbol="hello")
    names = [m.name for m in matches]
    # a.py 和 b.py 都有 hello，应都返回
    assert names.count("hello") >= 2


def test_go_to_definition_unknown_symbol():
    root = _mkroot()
    matches = go_to_definition(
        root=root, file_path=root / "a.py", symbol="nonexistent_xyz"
    )
    assert matches == []


def test_go_to_definition_by_position():
    root = _mkroot()
    # b.py 第 4 行: `    return hello(name).upper()`
    # 列 12 应落在 "hello" 上
    matches = go_to_definition(root=root, file_path=root / "b.py", line=4, character=12)
    assert any(m.name == "hello" for m in matches)


# ────────────────────────── find_references ──────────────────────────


def test_find_references_word_boundary():
    root = _mkroot()
    refs = find_references(root=root, file_path=root / "a.py", symbol="hello")
    # 至少有：a.py 的 def, b.py 的 import, b.py 的 调用 hello(name), b.py 的 def hello
    assert len(refs) >= 4
    paths = {p.name for p, _, _ in refs}
    assert "a.py" in paths
    assert "b.py" in paths


def test_find_references_unknown_symbol():
    root = _mkroot()
    refs = find_references(root=root, file_path=root / "a.py", symbol="nonexistent_xyz")
    assert refs == []


# ────────────────────────── hover ──────────────────────────


def test_hover_returns_first_match():
    root = _mkroot()
    h = hover(root=root, file_path=root / "a.py", symbol="hello")
    assert h is not None
    assert h.name == "hello"
    assert "Say hi" in h.docstring


def test_hover_none_when_missing():
    root = _mkroot()
    h = hover(root=root, file_path=root / "a.py", symbol="ghost")
    assert h is None


# ────────────────────────── extract_symbol_at_position ──────────────────────────


def test_extract_symbol_finds_identifier_at_col():
    root = _mkroot()
    sym = extract_symbol_at_position(root / "a.py", line=4, character=5)  # "def hello"
    # line 4: `def hello(name):`  col 5 落在 "hello"
    assert sym == "hello"


def test_extract_symbol_returns_none_for_invalid_line():
    root = _mkroot()
    assert extract_symbol_at_position(root / "a.py", line=9999, character=1) is None
    assert extract_symbol_at_position(root / "a.py", line=None, character=1) is None


# ────────────────────────── lsp_code 工具入口 ──────────────────────────


def test_tool_document_symbol_formats_output():
    root = _mkroot()
    out = lsp_code("document_symbol", file_path=str(root / "a.py"), root=str(root))
    assert "function hello" in out
    assert "class Greeter" in out
    assert "signature:" in out


def test_tool_workspace_symbol_returns_results():
    root = _mkroot()
    out = lsp_code("workspace_symbol", query="greet", root=str(root))
    assert "Greeter" in out


def test_tool_workspace_symbol_requires_query():
    out = lsp_code("workspace_symbol", root=".")
    assert "[ERROR]" in out and "requires query" in out


def test_tool_go_to_definition():
    root = _mkroot()
    out = lsp_code(
        "go_to_definition",
        file_path=str(root / "b.py"),
        symbol="hello",
        root=str(root),
    )
    assert "function hello" in out


def test_tool_find_references():
    root = _mkroot()
    out = lsp_code(
        "find_references",
        file_path=str(root / "a.py"),
        symbol="hello",
        root=str(root),
    )
    # 至少含 b.py 引用
    assert "b.py:" in out


def test_tool_hover():
    root = _mkroot()
    out = lsp_code(
        "hover",
        file_path=str(root / "a.py"),
        symbol="Greeter",
        root=str(root),
    )
    assert "class Greeter" in out


def test_tool_unknown_operation():
    root = _mkroot()
    out = lsp_code("teleport", file_path=str(root / "a.py"), root=str(root))
    assert "[ERROR]" in out and "unknown operation" in out


def test_tool_non_python_file_rejected():
    root = _mkroot()
    (root / "x.js").write_text("var x = 1;\n")
    out = lsp_code("document_symbol", file_path=str(root / "x.js"), root=str(root))
    assert "[ERROR]" in out and "Python (.py) files only" in out


def test_tool_missing_file_path():
    out = lsp_code("document_symbol", root=".")
    assert "[ERROR]" in out and "requires file_path" in out


def test_tool_bad_root():
    out = lsp_code("document_symbol", file_path="x.py", root="/no/such/place_xyz")
    assert "[ERROR]" in out and "root not found" in out


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
    print(f"\n{len(tests) - failed}/{len(tests)} lsp tests passed.")
    if failed:
        raise SystemExit(1)
