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
    """
    for p in root.rglob("*"):
        if any(part in _IGNORED_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if name_filter and not fnmatch.fnmatch(p.name, name_filter):
            continue
        yield p


@registry.register(
    name="glob_files",
    description=(
        "List files matching a glob pattern (e.g. '**/*.py', 'src/*.ts'). "
        "Searches under the given root (default '.'). "
        "Skips node_modules / .git / venv / __pycache__ etc. "
        "Returns at most 100 paths, newline-separated."
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
def glob_files(pattern: str, root: str = _DEFAULT_ROOT) -> str:
    """
    按 glob pattern 列文件。返回相对路径，每行一个。

    设计要点：
      - 用 Path.rglob 而不是 glob.glob：原生支持 **/ 递归
      - 命中数上限 100，避免一次性返回几千个把上下文塞爆
      - 跳过 _IGNORED_DIRS，结果更干净
    """
    try:
        base = Path(root)
        if not base.exists():
            return f"[ERROR] root not found: {root}"

        matches: list[Path] = []
        for p in base.rglob(pattern):
            if any(part in _IGNORED_DIRS for part in p.parts):
                continue
            if p.is_file():
                matches.append(p)
            if len(matches) >= 100:
                break

        if not matches:
            return f"(no files matched {pattern!r} under {root!r})"
        rel = [str(p.relative_to(base)) if p.is_relative_to(base) else str(p) for p in matches]
        return "\n".join(sorted(rel))
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


@registry.register(
    name="grep_text",
    description=(
        "Search files for a regex pattern. Returns matching lines as "
        "'path:line:content'. Optional file_pattern filters by filename glob "
        "(e.g. '*.py'). Skips noisy directories. "
        "Returns at most 200 hits."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python regex pattern (use \\b for word boundaries)",
            },
            "root": {
                "type": "string",
                "description": "Root directory to search under, default '.'",
            },
            "file_pattern": {
                "type": "string",
                "description": "fnmatch-style filename filter, e.g. '*.py'. Optional.",
            },
        },
        "required": ["pattern"],
    },
)
def grep_text(
    pattern: str,
    root: str = _DEFAULT_ROOT,
    file_pattern: Optional[str] = None,
) -> str:
    """
    在文件内容里 grep。返回 path:line:content 行。

    设计要点：
      - 用 Python re 而不是调 ripgrep：少一个外部依赖
      - 命中数上限 200，单行截断到 200 字符（防止一行 minified JS 把窗口塞爆）
      - 编码问题用 errors='replace'，不让单个二进制混入文件让整次搜索崩
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR] bad regex: {e}"

    base = Path(root)
    if not base.exists():
        return f"[ERROR] root not found: {root}"

    hits: list[str] = []
    for f in _iter_filtered_files(base, name_filter=file_pattern):
        # 跳过明显二进制（>500KB 或非 utf-8）
        try:
            if f.stat().st_size > 500_000:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
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
