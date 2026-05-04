"""
工具集：Agent 可调用的"手脚"（Harness 执行层）
==================================================

【工具是什么】
在 Agent 语境里，"工具（tool）"就是模型可以调用的函数。
模型生成一段特殊格式的输出（tool_call），运行时解析并执行对应函数，
再把结果回填给模型。这让模型突破"只会吐字"的限制，能读写文件、跑命令、查数据库。

【CodeMesh 提供的工具】
基础三件套：
  - bash_exec  : 跑 shell 命令（带超时和沙箱）
  - read_file  : 读文件
  - write_file : 写文件（覆盖式）

Claude Code 标准三件套（v2 加）：
  - glob_files : 按 shell 通配符匹配文件路径（如 **/*.py）
  - grep_text  : 在文件里搜文本（带可选 file_pattern 过滤）
  - edit_file  : 精确替换文件中的一段字符串（增量编辑，比 write_file 安全）

【工具设计原则】
  1. 输入参数简单（字符串为主），模型才能稳定生成
  2. 返回字符串，便于拼回 messages
  3. 错误要作为正常返回，不要抛异常（模型看不到异常，只看字符串）
     这是和普通 Python 函数最大的差异 —— 一切错误要"可读"

【Tool Registry（v2 重构）】
之前是 TOOL_SCHEMAS / TOOL_IMPL 两个全局字典硬编码 3 个工具。
v2 改成 Registry 模式：

    @registry.register(name="...", description="...", parameters={...})
    def my_tool(...): ...

加新工具只要写一个函数 + 一个装饰器，schemas 自动生成。
设计参考 HKUDS/OpenHarness 的 tools/ 注册表实现。

向后兼容：原 TOOL_SCHEMAS / TOOL_IMPL / dispatch_tool 仍可用，是 registry 的视图。
"""

import asyncio
import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

from .sandbox import check_command, SandboxViolation


# ───────────────── Tool Registry ─────────────────


ToolHandler = Union[
    Callable[..., str],
    Callable[..., Awaitable[str]],
]


class ToolRegistry:
    """
    工具注册表。Agent Loop 唯一接触的工具入口。

    职责：
      1. 收集 (name → handler, schema) 映射
      2. 把所有工具的 OpenAI function schema 一次性导出（给模型看的"菜单"）
      3. dispatch 时统一处理 async/sync、参数错误、未知工具
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """
        装饰器：把一个函数注册成工具。

        Args:
            name        : 工具名，模型调用时引用
            description : 一行说明，告诉模型这工具干啥
            parameters  : OpenAI function calling 风格的 JSON schema

        Returns:
            原函数（不包装），方便 Python 内部直接调用。
        """

        def deco(fn: ToolHandler) -> ToolHandler:
            if name in self._handlers:
                raise ValueError(f"tool {name!r} already registered")
            self._handlers[name] = fn
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
            return fn

        return deco

    @property
    def names(self) -> list[str]:
        return list(self._handlers.keys())

    @property
    def schemas(self) -> list[dict]:
        """传给模型 chat.completions.create(tools=...) 的列表。"""
        return list(self._schemas.values())

    @property
    def handlers(self) -> dict[str, ToolHandler]:
        """name → fn 映射；测试 / 调试用。"""
        return dict(self._handlers)

    async def dispatch(self, name: str, arguments: dict) -> str:
        """
        统一分发。原 dispatch_tool 的实现搬到这里。
          - 未知工具：返回错误字符串（不抛异常）
          - sync / async：自动适配
          - 参数不匹配：返回错误字符串
        """
        fn = self._handlers.get(name)
        if fn is None:
            return f"[ERROR] unknown tool: {name}"
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(**arguments)
            return fn(**arguments)
        except TypeError as e:
            return f"[ERROR] bad arguments for {name}: {e}"


# 模块级单例。所有 @register 装饰器都挂在这上面。
registry = ToolRegistry()


# ───────────────── 工具实现：基础三件套 ─────────────────


@registry.register(
    name="bash_exec",
    description="Execute a shell command. Has 30s timeout and sandbox checks.",
    parameters={
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to run"},
        },
        "required": ["cmd"],
    },
)
async def bash_exec(cmd: str, timeout: float = 30.0) -> str:
    """
    执行 shell 命令，返回 stdout + stderr 合并文本。

    为什么 async？—— 用 asyncio.create_subprocess_shell 可以不阻塞事件循环。
    如果用 subprocess.run（同步），在命令没返回前整个 async 进程都卡住。
    """
    # 先过沙箱。命中危险模式直接返回警告，不执行
    try:
        check_command(cmd)
    except SandboxViolation as e:
        return f"[SANDBOX BLOCKED] {e.reason}\ncommand: {cmd}"

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return f"[TIMEOUT after {timeout}s]\ncommand: {cmd}"

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    result = out
    if err:
        result += f"\n[stderr]\n{err}"
    if proc.returncode != 0:
        result += f"\n[exit code: {proc.returncode}]"
    return result or "(empty output)"


@registry.register(
    name="read_file",
    description="Read a text file from disk.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative path"},
        },
        "required": ["path"],
    },
)
def read_file(path: str) -> str:
    """读文件。错误以字符串返回，模型才看得懂。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"[ERROR] file not found: {path}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


@registry.register(
    name="write_file",
    description="Overwrite a text file with given content. Creates parent dirs.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)
def write_file(path: str, content: str) -> str:
    """覆盖式写文件，自动建父目录。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


# ───────────────── Claude Code 标准三件套 ─────────────────


# 路径白名单：搜索 / glob 默认在当前目录树内，避免模型瞎指根目录
_DEFAULT_ROOT = "."

# 跟 RAG indexer 保持一致的忽略清单
_IGNORED_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".next", ".cache",
}


def _iter_filtered_files(root: Path, name_filter: Optional[str] = None) -> Iterable[Path]:
    """
    遍历 root 下所有文件，跳过已知噪音目录。可选 fnmatch 名字过滤。
    Python fallback 使用，rg 路径会自己尊重 .gitignore。
    """
    for p in root.rglob("*"):
        if any(part in _IGNORED_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if name_filter and not fnmatch.fnmatch(p.name, name_filter):
            continue
        yield p


def _looks_like_git_repo(path: Path, max_depth: int = 6) -> bool:
    """
    向上找最多 max_depth 层，看有没有 .git。
    用于 glob：在 git 仓库里搜隐藏目录（.github 等）有意义；在用户家目录搜会爆。
    """
    cur = path
    for _ in range(max_depth):
        if (cur / ".git").exists():
            return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


# rg 退出码：0=有匹配，1=无匹配，-15=SIGTERM（我们超时杀的），-9=SIGKILL
_RG_OK_RETURNCODES = {0, 1, -15, -9}
# 单行最大缓冲：8MB，避免 minified js 触发 LimitOverrunError
_RG_LINE_LIMIT = 8 * 1024 * 1024


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """优雅终止子进程：SIGTERM → 等 2s → SIGKILL。"""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _rg_grep(
    *,
    root: Path,
    pattern: str,
    file_glob: Optional[str],
    case_sensitive: bool,
    limit: int,
    timeout_seconds: int,
) -> Optional[list[str]]:
    """
    用 ripgrep 搜内容。
    返回 None 表示 rg 不可用 / 出错（让上层回退到 Python 实现）。
    返回 list[str] 表示 rg 成功，每条形如 'path:line:content'。
    """
    rg = shutil.which("rg")
    if not rg:
        return None

    include_hidden = (root / ".git").exists() or (root / ".gitignore").exists()
    cmd: list[str] = [rg, "--no-heading", "--line-number", "--color", "never"]
    if include_hidden:
        cmd.append("--hidden")
    if not case_sensitive:
        cmd.append("-i")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    # `--` 让 pattern 即使以 `-` 开头也不被当成 flag
    cmd.extend(["--", pattern, "."])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_RG_LINE_LIMIT,
        )
    except (OSError, ValueError):
        return None

    matches: list[str] = []
    timed_out = False
    try:
        async def _collect() -> None:
            assert process.stdout is not None
            while len(matches) < limit:
                try:
                    raw = await process.stdout.readline()
                except ValueError:
                    # 单行超过 buffer：跳过该行继续
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    matches.append(line)

        await asyncio.wait_for(_collect(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        await _terminate(process)
        if process.returncode is None:
            await process.wait()

    if not timed_out and process.returncode not in _RG_OK_RETURNCODES:
        return None

    if timed_out:
        matches.append(f"[grep timed out after {timeout_seconds}s, partial results above]")
    return matches


async def _rg_glob_files(
    *,
    root: Path,
    pattern: str,
    limit: int,
    timeout_seconds: int = 30,
) -> Optional[list[str]]:
    """
    用 ripgrep 的 file walker 列文件（速度比 Path.rglob 快很多 + 自动尊重 .gitignore）。
    返回 None 表示 rg 不可用 / 出错。
    """
    rg = shutil.which("rg")
    if not rg:
        return None

    cmd = [rg, "--files"]
    if _looks_like_git_repo(root):
        cmd.append("--hidden")
    cmd.extend(["--glob", pattern, "."])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_RG_LINE_LIMIT,
        )
    except (OSError, ValueError):
        return None

    lines: list[str] = []
    try:
        async def _collect() -> None:
            assert process.stdout is not None
            while len(lines) < limit:
                try:
                    raw = await process.stdout.readline()
                except ValueError:
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)

        await asyncio.wait_for(_collect(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        await _terminate(process)
        if process.returncode is None:
            await process.wait()

    if process.returncode not in _RG_OK_RETURNCODES:
        return None
    lines.sort()
    return lines


@registry.register(
    name="glob_files",
    description=(
        "List files matching a glob pattern (e.g. '**/*.py', 'src/*.ts'). "
        "Uses ripgrep when available (respects .gitignore, fast) with a "
        "Python fallback. Returns at most 200 paths, newline-separated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'tests/test_*.py'",
            },
            "root": {
                "type": "string",
                "description": "Root directory to search under, default '.'",
            },
        },
        "required": ["pattern"],
    },
)
async def glob_files(pattern: str, root: str = _DEFAULT_ROOT) -> str:
    """
    列文件。优先 ripgrep（速度 + .gitignore 自动尊重），失败回 Path.rglob。

    设计参考 HKUDS/OpenHarness glob_tool.py：
      - rg 路径：`rg --files --glob <p> .`，比 Path.rglob 快几倍且不进入 .gitignore 目录
      - rg 不可用 / 出错时回退到 Python，跳过 _IGNORED_DIRS
      - 命中数上限 200（OpenHarness 默认 200，原 Codemesh 是 100，提到 200 一致）
    """
    try:
        base = Path(root).resolve() if root else Path(".").resolve()
        if not base.exists():
            return f"[ERROR] root not found: {root}"

        # 优先 ripgrep
        rg_lines = await _rg_glob_files(root=base, pattern=pattern, limit=200)
        if rg_lines is not None:
            if not rg_lines:
                return f"(no files matched {pattern!r} under {root!r})"
            return "\n".join(rg_lines)

        # Python fallback
        matches: list[Path] = []
        for p in base.rglob(pattern):
            if any(part in _IGNORED_DIRS for part in p.parts):
                continue
            if p.is_file():
                matches.append(p)
            if len(matches) >= 200:
                break
        if not matches:
            return f"(no files matched {pattern!r} under {root!r})"
        rel = [
            str(p.relative_to(base)) if p.is_relative_to(base) else str(p)
            for p in matches
        ]
        return "\n".join(sorted(rel))
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


@registry.register(
    name="grep_text",
    description=(
        "Search files for a regex pattern. Returns matching lines as "
        "'path:line:content'. Uses ripgrep when available (fast, "
        "respects .gitignore, streamed) with a Python fallback. "
        "Optional file_pattern filters by filename glob (e.g. '*.py'). "
        "Returns at most 200 hits, with timeout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern (rg syntax when available, Python re otherwise)",
            },
            "root": {
                "type": "string",
                "description": "Root directory to search under, default '.'",
            },
            "file_pattern": {
                "type": "string",
                "description": "Glob filename filter (passed to rg --glob), e.g. '*.py'. Optional.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case sensitive. Default true.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Timeout for ripgrep run. Default 20.",
            },
        },
        "required": ["pattern"],
    },
)
async def grep_text(
    pattern: str,
    root: str = _DEFAULT_ROOT,
    file_pattern: Optional[str] = None,
    case_sensitive: bool = True,
    timeout_seconds: int = 20,
) -> str:
    """
    内容 grep。优先 ripgrep（流式 + 超时 + .gitignore + 二进制跳过），
    否则回 Python re。

    设计参考 HKUDS/OpenHarness grep_tool.py：
      - rg 流式读 stdout，达到 limit 立刻杀进程（不会等全文搜完）
      - 8MB 单行 buffer 限制，遇到超长 minified 行不崩溃
      - 进程退出码 0 / 1 / -15 / -9 都视为成功（rg 无匹配返回 1）
      - rg 不可用：回退到 Python re，自己跳过 binary（含 \\x00）和 >500KB 文件
    """
    base = Path(root).resolve() if root else Path(".").resolve()
    if not base.exists():
        return f"[ERROR] root not found: {root}"

    # 优先 ripgrep
    rg_hits = await _rg_grep(
        root=base,
        pattern=pattern,
        file_glob=file_pattern,
        case_sensitive=case_sensitive,
        limit=200,
        timeout_seconds=timeout_seconds,
    )
    if rg_hits is not None:
        if not rg_hits:
            filt = f" with file_pattern={file_pattern!r}" if file_pattern else ""
            return f"(no matches for {pattern!r}{filt})"
        return "\n".join(rg_hits)

    # Python fallback
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"[ERROR] bad regex: {e}"

    hits: list[str] = []
    for f in _iter_filtered_files(base, name_filter=file_pattern):
        try:
            if f.stat().st_size > 500_000:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            # 二进制文件跳过
            continue
        text = raw.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                rel = str(f.relative_to(base)) if f.is_relative_to(base) else str(f)
                snippet = line if len(line) <= 200 else line[:200] + "..."
                hits.append(f"{rel}:{i}:{snippet}")
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n[truncated at 200 hits]"

    if not hits:
        filt = f" with file_pattern={file_pattern!r}" if file_pattern else ""
        return f"(no matches for {pattern!r}{filt})"
    return "\n".join(hits)


@registry.register(
    name="edit_file",
    description=(
        "Edit an existing file by replacing exactly one occurrence of "
        "old_string with new_string. The old_string MUST be unique in the file "
        "— if it appears 0 or 2+ times, the call fails and asks the model to "
        "include more surrounding context. Safer than write_file because it "
        "doesn't overwrite other content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {
                "type": "string",
                "description": "Exact text to find. Must be unique in the file.",
            },
            "new_string": {
                "type": "string",
                "description": "Text to replace old_string with.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """
    精确替换。和 Claude Code 的 Edit 工具同语义：
      - old_string 必须在文件中**恰好出现 1 次**，否则报错
      - 不创建文件（用 write_file 创建新文件）
      - 替换后写回原文件
    """
    p = Path(path)
    if not p.exists():
        return f"[ERROR] file not found: {path}. Use write_file to create new files."
    try:
        original = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"[ERROR] read failed: {type(e).__name__}: {e}"

    count = original.count(old_string)
    if count == 0:
        return (
            f"[ERROR] old_string not found in {path}. "
            "Check whitespace and indentation, or include more surrounding context."
        )
    if count > 1:
        return (
            f"[ERROR] old_string appears {count} times in {path}; "
            "must be unique. Add more surrounding context to disambiguate."
        )

    updated = original.replace(old_string, new_string, 1)
    try:
        p.write_text(updated, encoding="utf-8")
    except Exception as e:
        return f"[ERROR] write failed: {type(e).__name__}: {e}"
    delta = len(new_string) - len(old_string)
    sign = "+" if delta >= 0 else ""
    return f"OK: edited {path} ({sign}{delta} bytes)"


# ───────────────── 向后兼容导出 ─────────────────
# 保留原有 TOOL_SCHEMAS / TOOL_IMPL / dispatch_tool 三个名字，
# 这样 execution/loop.py 和外部依赖不需要改动。

TOOL_SCHEMAS: list[dict] = registry.schemas
TOOL_IMPL: dict[str, Any] = registry.handlers


async def dispatch_tool(name: str, arguments: dict) -> str:
    """向后兼容包装：直接转发到 registry.dispatch。"""
    return await registry.dispatch(name, arguments)
