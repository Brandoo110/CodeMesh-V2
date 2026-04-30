"""
CLI 入口
=========

【命令】
  codemesh run "任务"                        # 普通调用（auto：simple 流式 / complex 走 planner）
  codemesh run "任务" --stream               # 强制流式输出
  codemesh run "任务" --compare              # 三家模型并排 + 成本对比
  codemesh run "任务" --rag                  # 开启 RAG 前置检索
  codemesh index [PATH]                     # 建代码库索引（默认当前目录）

【Typer 子命令】
Typer 子命令用 @app.command() 定义多个函数，每个就是一个子命令。

【错误处理原则】
- 缺 key / 网络 / 认证失败：打一两行人话提示，退出码 1，不抛 traceback
- 真的 bug（代码错）才让它显示 traceback，便于开发修
"""

import asyncio
import os
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from harness import Harness
from rag import build_index


app = typer.Typer(
    help="CodeMesh：国内多模型 Code Agent（Harness 四层架构实践）",
    no_args_is_help=True,
)
console = Console()


# ─────────────── preflight / 错误处理 ───────────────

_FAKE_KEY_MARKERS = ("your-key-here", "sk-fake", "changeme", "xxxx")

_KEY_ENVS = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "VOLC_API_KEY", "GEMINI_API_KEY")


def _preflight() -> None:
    """运行前检查 .env 是否配了至少一个可用 key。不过就打人话提示并退出。"""
    load_dotenv()
    # Google 自己的 SDK 有时用 GOOGLE_API_KEY、有时用 GEMINI_API_KEY，两个都兼容
    if not os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
    env_file = Path.cwd() / ".env"
    if not env_file.exists():
        console.print(
            "[yellow]提示：当前目录没有 .env 文件。[/yellow]\n"
            "请执行 [cyan]cp .env.example .env[/cyan] 后填入至少一个 API key "
            "（GEMINI_API_KEY 最易获取）。"
        )
        raise typer.Exit(code=1)

    valid = []
    for k in _KEY_ENVS:
        v = (os.getenv(k) or "").strip()
        if not v:
            continue
        if any(m in v.lower() for m in _FAKE_KEY_MARKERS):
            continue
        valid.append(k)

    if not valid:
        console.print(
            "[red]没检测到有效的 API key。[/red]\n"
            "请在 [cyan].env[/cyan] 里填入以下任一：\n"
            "  - DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / VOLC_API_KEY（国内合规主力）\n"
            "  - GEMINI_API_KEY（学习/演示用，单 key 即可跑全套）"
        )
        raise typer.Exit(code=1)


def _friendly_error(e: BaseException) -> str | None:
    """
    把常见的底层异常翻译成一句人话。返回 None 表示这不是已知可翻译错误，
    应该让它原样抛出（方便开发定位）。
    """
    name = type(e).__name__
    msg = str(e)

    # openai / pydantic_ai / httpx 常见错误关键字
    lower = msg.lower()
    if "authentication" in lower or "invalid api key" in lower or "401" in lower:
        return "API key 无效或未激活，请检查 .env 里对应的 *_API_KEY。"
    if name == "ModelHTTPError" or "connection" in lower or "timeout" in lower:
        return f"模型服务暂时不可达（{name}）。请检查网络或稍后重试。"
    if "quota" in lower or "rate limit" in lower or "429" in lower:
        return "触发了模型厂商的限额/限流，稍后再试或换一个 key。"
    if name in {"APIConnectionError", "APIStatusError", "APIError"}:
        return f"调用模型 API 失败：{msg[:120]}"
    return None


def _safe_run(coro) -> None:
    """统一跑 async 协程并把已知错误翻成人话。"""
    try:
        asyncio.run(coro)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
        sys.exit(130)
    except BaseException as e:  # noqa: BLE001
        hint = _friendly_error(e)
        if hint:
            console.print(f"[red]✗[/red] {hint}")
            sys.exit(1)
        raise


# ─────────────── run ───────────────


@app.command()
def run(
    task: str = typer.Argument(..., help="任务描述"),
    compare: bool = typer.Option(False, "--compare", "-c", help="并排展示三家模型输出"),
    stream: bool = typer.Option(False, "--stream", help="强制流式输出"),
    rag: bool = typer.Option(False, "--rag", help="启用 RAG 前置检索（需先 index）"),
):
    """跑一次 CodeMesh 任务。"""
    _preflight()
    _safe_run(_run(task, compare, stream, rag))


async def _run(task: str, compare: bool, stream: bool, rag: bool) -> None:
    harness = Harness(use_rag=rag)

    if compare:
        await _run_compare(harness, task)
        return

    console.print(Panel(task, title="任务", border_style="cyan"))

    if stream:
        # 强制流式：实时打印
        console.print("[bold green]回复:[/bold green] ", end="")
        async for chunk in harness.run_stream(task):
            console.print(chunk, end="", highlight=False)
        console.print()
        _print_costs(harness)
        return

    # 默认：自动分流
    answer = await harness.run(task)
    console.print(Panel(answer, title="回复", border_style="green"))
    _print_costs(harness)


async def _run_compare(harness: Harness, task: str) -> None:
    console.print(Panel(task, title="对比任务", border_style="cyan"))
    console.print("[yellow]并发调用 DeepSeek / Qwen / Doubao ...[/yellow]")
    results = await harness.compare(task)

    table = Table(title="三家模型对比", show_lines=True)
    table.add_column("模型", style="bold cyan")
    table.add_column("延迟", style="yellow")
    table.add_column("token (in/out)", style="magenta")
    table.add_column("成本", style="green")
    table.add_column("输出", style="white", overflow="fold", max_width=60)
    for name, r in results.items():
        text = r["text"]
        is_err = isinstance(text, str) and text.startswith("[ERROR]")
        c = r["cost"]
        if is_err:
            # 失败行：不要显示 0ms / ¥0.0000 这种误导数字
            table.add_row(name, "—", "—", "—", text)
        else:
            table.add_row(
                name,
                f"{r['latency_ms']:.0f}ms",
                f"{c.tokens_in}/{c.tokens_out}",
                f"¥{c.cost_rmb:.4f}",
                text,
            )
    console.print(table)


def _print_costs(harness: Harness) -> None:
    """把本轮累计成本打成表。"""
    if not harness.last_costs:
        return
    table = Table(title="本次调用成本", show_header=True, header_style="bold")
    table.add_column("模型")
    table.add_column("tokens in")
    table.add_column("tokens out")
    table.add_column("¥")
    total = 0.0
    for c in harness.last_costs:
        table.add_row(
            c.model, str(c.tokens_in), str(c.tokens_out), f"{c.cost_rmb:.4f}"
        )
        total += c.cost_rmb
    table.add_row("— 合计 —", "", "", f"[bold]¥{total:.4f}[/bold]")
    console.print(table)


# ─────────────── index ───────────────


@app.command()
def index(
    path: Path = typer.Argument(Path.cwd(), help="要索引的代码目录，默认当前目录"),
):
    """
    为代码目录建 RAG 索引。以后用 --rag 时会检索这里的内容。
    """
    _preflight()
    console.print(f"[cyan]正在扫描 {path.resolve()} ...[/cyan]")
    _safe_run(_index(path))


async def _index(path: Path) -> None:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} files | {task.fields[chunks]} chunks"),
    ) as progress:
        task_id = progress.add_task("扫描", total=None, chunks=0)

        def on_progress(done: int, total: int, chunks: int) -> None:
            progress.update(task_id, completed=done, total=total, chunks=chunks)

        count = await build_index(path, on_progress=on_progress)
    console.print(f"[green]✓ 索引完成，共 {count} 个 chunk。[/green]")
    console.print(f"使用方式: [cyan]codemesh run '任务' --rag[/cyan]")


# ─────────────── stats（隐藏：尚未实现）───────────────


@app.command(hidden=True)
def stats():
    """Langfuse 统计入口（未实现，请直接用 Langfuse 控制台）。"""
    console.print(
        Panel(
            "[yellow]stats 子命令尚未实现（路线图项）。[/yellow]\n"
            "实时统计请到 [link]https://cloud.langfuse.com[/link] 控制台查看。",
            title="Stats",
            border_style="magenta",
        )
    )


if __name__ == "__main__":
    app()
