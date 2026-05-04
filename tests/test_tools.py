"""
工具与 Tool Registry 单元测试
================================

跑法：
    python -m tests.test_tools

覆盖：
  - Registry 注册 / dispatch / async-sync 适配 / 错误处理
  - read_file / write_file / edit_file 三个文件类工具
  - glob_files / grep_text 两个搜索工具
  - 不测 bash_exec 的真实命令（沙箱已在 test_sandbox 覆盖）
"""

import asyncio
import tempfile
from pathlib import Path

from execution.tools import (
    ToolRegistry,
    registry,
    TOOL_SCHEMAS,
    dispatch_tool,
    read_file,
    write_file,
    glob_files,
    grep_text,
    edit_file,
)


# ────────────────────────── helpers ──────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _make_workspace() -> Path:
    """生成一个含若干文件的临时目录，返回其 Path。"""
    base = Path(tempfile.mkdtemp(prefix="codemesh-test-"))
    # rg 只在认出是 git 仓库时才应用 .gitignore；造一个空 .git 目录当 marker
    # （Python fallback 自带硬编码 _IGNORED_DIRS，但 rg 路径必须靠 .gitignore）
    (base / ".git").mkdir()
    (base / ".gitignore").write_text("node_modules/\n")
    (base / "a.py").write_text("def hello():\n    return 'hi'\n")
    (base / "b.py").write_text("def world():\n    return 'world'\n")
    (base / "data.txt").write_text("apple\nbanana\ncherry\n")
    sub = base / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("def deep():\n    pass\nDROP TABLE foo\n")
    # 噪音目录应该被忽略
    noise = base / "node_modules"
    noise.mkdir()
    (noise / "should_be_ignored.js").write_text("noise")
    return base


# ────────────────────────── registry 基础 ──────────────────────────


def test_registry_has_eleven_tools():
    assert len(registry.names) == 11
    assert set(registry.names) == {
        "bash_exec", "read_file", "write_file",
        "glob_files", "grep_text", "edit_file",
        "lsp_code",
        "remember_fact", "recall_facts", "forget_fact",
        "invoke_skill",
    }


def test_schemas_match_openai_format():
    """每个 schema 必须有 type=function 和 function.name/description/parameters。"""
    for s in TOOL_SCHEMAS:
        assert s["type"] == "function"
        fn = s["function"]
        assert "name" in fn and "description" in fn and "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_registry_dispatch_unknown_tool():
    out = _run(dispatch_tool("does_not_exist", {}))
    assert "[ERROR]" in out and "unknown tool" in out


def test_registry_dispatch_bad_args():
    """缺必传参数应返回错误字符串，不抛异常。"""
    out = _run(dispatch_tool("read_file", {}))   # 缺 path
    assert "[ERROR]" in out and "bad arguments" in out


def test_registry_register_async_and_sync():
    """新建一个 registry，注册一个 async 一个 sync，dispatch 都能工作。"""
    r = ToolRegistry()

    @r.register(name="echo", description="echo back", parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    })
    def echo_sync(text: str) -> str:
        return f"echoed: {text}"

    @r.register(name="echo_async", description="async echo", parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    })
    async def echo_async(text: str) -> str:
        return f"async-echoed: {text}"

    assert _run(r.dispatch("echo", {"text": "hi"})) == "echoed: hi"
    assert _run(r.dispatch("echo_async", {"text": "ya"})) == "async-echoed: ya"


def test_registry_duplicate_name_raises():
    r = ToolRegistry()

    @r.register(name="x", description="d", parameters={"type": "object", "properties": {}})
    def fn1() -> str:
        return ""

    try:
        @r.register(name="x", description="d", parameters={"type": "object", "properties": {}})
        def fn2() -> str:
            return ""
    except ValueError as e:
        assert "already registered" in str(e)
        return
    raise AssertionError("expected ValueError on duplicate registration")


# ────────────────────────── read / write ──────────────────────────


def test_read_file_ok():
    base = _make_workspace()
    out = read_file(str(base / "a.py"))
    assert "def hello" in out


def test_read_file_missing():
    out = read_file("/nope/nonexistent_xyz")
    assert "[ERROR]" in out and "file not found" in out


def test_write_file_creates_parent_dirs():
    base = _make_workspace()
    target = base / "deep" / "nested" / "x.txt"
    out = write_file(str(target), "hello")
    assert out.startswith("OK:")
    assert target.read_text() == "hello"


# ────────────────────────── edit_file ──────────────────────────


def test_edit_file_unique_replace():
    base = _make_workspace()
    f = base / "a.py"
    out = edit_file(str(f), "return 'hi'", "return 'updated'")
    assert out.startswith("OK: edited")
    assert "return 'updated'" in f.read_text()


def test_edit_file_old_string_not_found():
    base = _make_workspace()
    f = base / "a.py"
    out = edit_file(str(f), "this string does not exist", "x")
    assert "[ERROR]" in out and "not found" in out


def test_edit_file_old_string_not_unique():
    base = _make_workspace()
    f = base / "dup.py"
    f.write_text("foo\nfoo\nfoo\n")
    out = edit_file(str(f), "foo", "bar")
    assert "[ERROR]" in out and "appears 3 times" in out
    # 文件未被修改
    assert f.read_text() == "foo\nfoo\nfoo\n"


def test_edit_file_does_not_create_new_file():
    out = edit_file("/tmp/__codemesh_definitely_not_exist.txt", "a", "b")
    assert "[ERROR]" in out and "file not found" in out


# ────────────────────────── glob_files ──────────────────────────


def test_glob_finds_python_files():
    base = _make_workspace()
    out = _run(glob_files("**/*.py", root=str(base)))
    lines = out.splitlines()
    # rg / Python fallback 都用相对路径；可能含 ./ 前缀
    assert any(l.endswith("a.py") for l in lines)
    assert any(l.endswith("b.py") for l in lines)
    assert any(l.endswith("c.py") for l in lines)


def test_glob_skips_node_modules():
    base = _make_workspace()
    out = _run(glob_files("**/*.js", root=str(base)))
    # rg --files 不进 node_modules（默认 .gitignore 行为，但是临时目录不一定有 .git）
    # python fallback 走 _IGNORED_DIRS 显式跳过
    # 两条路径都应该不返回 noise 文件
    assert "should_be_ignored.js" not in out


def test_glob_no_matches():
    base = _make_workspace()
    out = _run(glob_files("**/*.rs", root=str(base)))
    assert "no files matched" in out


def test_glob_bad_root():
    out = _run(glob_files("*.py", root="/nope/__codemesh_no_such_dir"))
    assert "[ERROR]" in out and "root not found" in out


# ────────────────────────── grep_text ──────────────────────────


def test_grep_finds_function_def():
    base = _make_workspace()
    out = _run(grep_text(r"def hello", root=str(base)))
    assert "a.py" in out and "def hello" in out


def test_grep_with_file_pattern():
    base = _make_workspace()
    # data.txt 含 banana，但 file_pattern=*.py 应过滤掉
    out = _run(grep_text(r"banana", root=str(base), file_pattern="*.py"))
    assert "no matches" in out


def test_grep_returns_path_line_format():
    base = _make_workspace()
    out = _run(grep_text(r"DROP TABLE", root=str(base)))
    # 期望格式：path:line:content。rg 路径下可能是 sub/c.py:3:DROP TABLE foo
    # python 路径下也是 sub/c.py:3:DROP TABLE foo
    assert "c.py" in out
    parts = out.split(":")
    assert len(parts) >= 3
    line_no = parts[1].strip()
    assert line_no.isdigit()


def test_grep_bad_regex():
    """
    在 Python fallback 路径下 bad regex 会被本地 re.compile 截获并返回 [ERROR]。
    在 rg 路径下，rg 自己会拒绝并退出码 != 0/1/-15/-9 → 我们返回 None → 落到
    Python fallback 再次报错。两种路径最终都得到 [ERROR] bad regex。
    """
    out = _run(grep_text(r"(unbalanced", root="."))
    # rg 拒绝时它的退出码不是 0/1，所以会回退到 Python，由 Python re 报错
    assert "[ERROR]" in out and "bad regex" in out


def test_grep_no_match():
    base = _make_workspace()
    out = _run(grep_text(r"this_string_does_not_appear_anywhere_xyz", root=str(base)))
    assert "no matches" in out


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
    print(f"\n{len(tests) - failed}/{len(tests)} tools tests passed.")
    if failed:
        raise SystemExit(1)
