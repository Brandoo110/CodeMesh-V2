"""
工具集：Agent 可调用的"手脚"（Harness 执行层）
==================================================

【工具是什么】
在 Agent 语境里，"工具（tool）"就是模型可以调用的函数。
模型生成一段特殊格式的输出（tool_call），运行时解析并执行对应函数，
再把结果回填给模型。这让模型突破"只会吐字"的限制，能读写文件、跑命令、查数据库。

【CodeMesh 提供的三个基础工具】
  - bash_exec : 跑 shell 命令（带超时和沙箱）
  - read_file : 读文件
  - write_file: 写文件

这三个已经能支撑大部分代码类任务（看代码、改代码、跑测试）。
真实产品里还会加 grep、git、HTTP 请求等，原理一样。

【工具设计原则】
  1. 输入参数简单（字符串为主），模型才能稳定生成
  2. 返回字符串，便于拼回 messages
  3. 错误要作为正常返回，不要抛异常（模型看不到异常，只看字符串）
     这是和普通 Python 函数最大的差异 —— 一切错误要"可读"

【TOOL_SCHEMAS】
下面的 OpenAI 风格 function schema 会传给模型，告诉它"有哪些工具、参数怎么填"。
这就是 tool use / function calling 的核心：提前声明接口。
"""

import asyncio
from pathlib import Path

from .sandbox import check_command, SandboxViolation


# ───────────────── 工具实现 ─────────────────


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

    # 启动子进程。stdout/stderr 走 pipe，方便我们读
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        # wait_for 给它设个超时上限，防止模型生成死循环卡死 Agent
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return f"[TIMEOUT after {timeout}s]\ncommand: {cmd}"

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")

    # 合并输出：正常情况只有 stdout，有 stderr 时拼上去
    result = out
    if err:
        result += f"\n[stderr]\n{err}"
    if proc.returncode != 0:
        result += f"\n[exit code: {proc.returncode}]"
    return result or "(empty output)"


def read_file(path: str) -> str:
    """
    读取文件内容。同步即可（本地 IO 很快）。
    错误不抛异常，返回文字方便模型理解后调整。
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"[ERROR] file not found: {path}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


def write_file(path: str, content: str) -> str:
    """
    写入文件。自动创建父目录。覆盖式写入（全量替换）。
    返回简短确认信息 —— 模型据此决定下一步。
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


# ───────────────── 工具元数据（给模型看的 schema） ─────────────────
# 这份 schema 会在 Agent Loop 里传给模型的 tools 参数，
# 模型会按这个格式生成 tool_call。
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": "Execute a shell command. Has 30s timeout and sandbox checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite a text file with given content. Creates parent dirs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


# 工具名到实现的映射（Agent Loop 里用 name 查找对应函数）
TOOL_IMPL = {
    "bash_exec": bash_exec,
    "read_file": read_file,
    "write_file": write_file,
}


async def dispatch_tool(name: str, arguments: dict) -> str:
    """
    统一的工具分发入口。

    Agent Loop 拿到模型的 tool_call 后调这个函数。好处：
      1. 一处写错误兜底（某个工具名不存在时返回提示，而不是 KeyError）
      2. 自动处理 async/sync 差异（bash_exec 是 async，其他是 sync）
    """
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return f"[ERROR] unknown tool: {name}"
    try:
        # asyncio.iscoroutinefunction 判断原始函数是不是协程
        if asyncio.iscoroutinefunction(fn):
            return await fn(**arguments)
        return fn(**arguments)
    except TypeError as e:
        # 参数不匹配（模型填错了 argument 名）
        return f"[ERROR] bad arguments for {name}: {e}"
