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
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from harness import Harness
from rag import build_index
from feedback import read_calls, aggregate, LOG_PATH, render_stats_dashboard, write_artifact
from assurance.cli import app as assurance_app


app = typer.Typer(
    help="CodeMesh：国内多模型 Code Agent（Harness 四层架构实践）",
    no_args_is_help=True,
)
app.add_typer(assurance_app, name="assurance")
console = Console()


# ─────────────── preflight / 错误处理 ───────────────

_FAKE_KEY_MARKERS = ("your-key-here", "sk-fake", "changeme", "xxxx")

_KEY_ENVS = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "VOLC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MINIMAX_API_KEY",
)

_PUBLISH_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_PUBLISH_REPOSITORY_RE = re.compile(r"^[^/\s?#]+/[^/\s?#]+$")
_PUBLISH_DEFAULT_BRANCH = "codex/authoritative-publication"
_PUBLISH_DEFAULT_BASE = "codex/local-acceptance-vertical"
_PUBLISH_DEFAULT_EVIDENCE_ROOT = "~/.codemesh/codemesh-v2-dogfood"


def _publish_git_value(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Git repository state could not be read") from exc
    if completed.returncode != 0:
        raise ValueError("Git repository state could not be read")
    value = completed.stdout.strip()
    if not value:
        raise ValueError("Git repository state was empty")
    return value


def _publish_require_clean(repository: Path) -> None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Git repository state could not be read") from exc
    if completed.returncode != 0:
        raise ValueError("Git repository state could not be read")
    if completed.stdout.strip():
        raise ValueError("transport worktree must be clean")


def _publish_repository(repository: Path) -> str:
    configured = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if configured and _PUBLISH_REPOSITORY_RE.fullmatch(configured):
        return configured
    remote = _publish_git_value(repository, "config", "--get", "remote.origin.url")
    if remote.startswith("git@github.com:"):
        candidate = remote.removeprefix("git@github.com:").removesuffix(".git")
    else:
        parsed = urlsplit(remote)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("a GitHub origin is required")
        candidate = parsed.path.strip("/").removesuffix(".git")
    if _PUBLISH_REPOSITORY_RE.fullmatch(candidate) is None:
        raise ValueError("GitHub repository identity is invalid")
    return candidate


def _publish_token() -> str:
    for env_name in ("CODEMESH_GITHUB_TOKEN", "GITHUB_TOKEN"):
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("a GitHub token is required") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ValueError("a GitHub token is required")
    return value


def _publish_payload(receipt) -> dict[str, object]:
    payload = asdict(receipt)
    payload.pop("workbench", None)
    return payload


def _preflight() -> None:
    """运行前检查 .env 是否配了至少一个可用 key。不过就打人话提示并退出。"""
    env_file = Path.cwd() / ".env"
    if not env_file.exists():
        console.print(
            "[yellow]提示：当前目录没有 .env 文件。[/yellow]\n"
            "请执行 [cyan]cp .env.example .env[/cyan] 后填入至少一个 API key "
            "（GEMINI_API_KEY 最易获取）。"
        )
        raise typer.Exit(code=1)

    # 只加载当前工作目录的 .env。python-dotenv 默认会按调用栈找文件，
    # 测试切到临时目录时容易误读项目根目录的真实 key。
    load_dotenv(dotenv_path=env_file)
    # Google 自己的 SDK 有时用 GOOGLE_API_KEY、有时用 GEMINI_API_KEY，两个都兼容
    if not os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]

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
            "  - DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / VOLC_API_KEY / MINIMAX_API_KEY\n"
            "  - GEMINI_API_KEY / GOOGLE_API_KEY（学习/演示用，单 key 即可跑全套）"
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


@app.command("publish-case")
def publish_case(
    case_id: str = typer.Option(..., "--case-id", help="要发布的本地权威 Case ID"),
    target_pr: int = typer.Option(..., "--pr", min=1, help="绑定的 producer PR 编号"),
    producer_head: str = typer.Option(..., "--producer-head", help="producer PR 的精确 HEAD SHA"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 receipt"),
) -> None:
    """将一个本地权威 Case 通过 CI Bundle 发布并完成远端读回。"""

    try:
        # Keep the existing top-level CLI import graph light: publishing is a
        # deliberate external operation and loads its adapter only on demand.
        from assurance.case_publication import CasePublication
        from assurance.integrations.github_actions import GitHubActionsTransport

        repository_root = Path.cwd().resolve(strict=True)
        if not repository_root.is_dir():
            raise ValueError("current directory is not a repository")
        _publish_require_clean(repository_root)
        transport_branch = os.getenv(
            "CODEMESH_TRANSPORT_BRANCH", _PUBLISH_DEFAULT_BRANCH
        )
        base_branch = os.getenv("CODEMESH_TRANSPORT_BASE", _PUBLISH_DEFAULT_BASE)
        actual_branch = _publish_git_value(
            repository_root, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if actual_branch != transport_branch:
            raise ValueError("publish-case must run from the transport branch")
        transport_head = _publish_git_value(repository_root, "rev-parse", "HEAD")
        if _PUBLISH_SHA1_RE.fullmatch(transport_head) is None:
            raise ValueError("transport HEAD is invalid")
        if _PUBLISH_SHA1_RE.fullmatch(producer_head) is None:
            raise ValueError("producer HEAD is invalid")
        repository = _publish_repository(repository_root)
        token = _publish_token()
        evidence_root = Path(
            os.getenv("CODEMESH_EVIDENCE_ROOT", _PUBLISH_DEFAULT_EVIDENCE_ROOT)
        ).expanduser()
        with GitHubActionsTransport(
            token=token,
            repository=repository,
            transport_branch=transport_branch,
            base_branch=base_branch,
        ) as transport:
            receipt = CasePublication(
                evidence_root=evidence_root,
                repository=repository,
                transport_head=transport_head,
                remote=transport,
            ).publish(
                case_id=case_id,
                target_pr=target_pr,
                producer_head=producer_head,
            )
        payload = _publish_payload(receipt)
    except Exception as exc:
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": "v1",
                        "confirmed": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            typer.echo(
                f"publish-case failed; authoritative result was not confirmed ({type(exc).__name__}: {exc})",
                err=True,
            )
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for field in (
        "case_id",
        "run_id",
        "subject_digest",
        "bundle_digest",
        "passport_digest",
        "producer_head",
        "transport_head",
        "transport_ref",
        "transport_ref_commit",
        "ci_run_id",
        "ci_job_id",
        "run_attempt",
        "artifact_id",
        "check_id",
        "check_url",
        "conclusion",
    ):
        typer.echo(f"{field}: {payload[field]}")


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


# ─────────────── stats（本地 JSONL 聚合）───────────────


@app.command()
def stats(
    days: float = typer.Option(7.0, "--days", "-d", help="窗口大小（天）。0 表示全部历史。"),
    html: bool = typer.Option(False, "--html", help="渲染交互式 dashboard 到 .codemesh/reports/"),
    output: Path = typer.Option(None, "--output", "-o", help="自定义 HTML 输出路径（仅 --html 时生效）"),
):
    """显示最近 N 天的本地调用统计：调用数 / token / 成本 / 平均延迟。

    数据源是 ~/.codemesh/calls.jsonl（每次 run 自动追加）。
    比 Langfuse 简单：不需要外网账号。

    `--html` 把同样数据渲染成单文件 dashboard（KPI / bar / pie / sparkline / 表）。
    数据源、聚合逻辑都是同一套，HTML 只是换个给人看的形态。
    """
    since = days if days > 0 else None
    records = read_calls(since_days=since)

    if html:
        _stats_html(records, since, output)
        return

    if not records:
        msg = (
            f"[yellow]最近 {days} 天没有调用记录。[/yellow]"
            if since
            else "[yellow]还没有调用记录。先跑几次 codemesh run 再来。[/yellow]"
        )
        console.print(msg)
        console.print(f"日志位置: [dim]{LOG_PATH}[/dim]")
        return

    by_model = aggregate(records)

    table = Table(title=f"CodeMesh stats (last {days}d, {len(records)} calls)" if since else f"CodeMesh stats (all-time, {len(records)} calls)")
    table.add_column("model", style="cyan")
    table.add_column("calls", justify="right")
    table.add_column("tokens_in", justify="right")
    table.add_column("tokens_out", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("avg_latency", justify="right")

    total_calls = total_in = total_out = 0
    total_cost = 0.0
    for model in sorted(by_model.keys()):
        agg = by_model[model]
        total_calls += agg["calls"]
        total_in += agg["tokens_in"]
        total_out += agg["tokens_out"]
        total_cost += agg["cost_rmb"]
        lat = agg["avg_latency_ms"]
        lat_text = f"{lat:.0f}ms" if lat is not None else "-"
        table.add_row(
            model,
            f"{agg['calls']:,}",
            f"{agg['tokens_in']:,}",
            f"{agg['tokens_out']:,}",
            f"¥{agg['cost_rmb']:.4f}",
            lat_text,
        )
    table.add_row(
        "[bold]— total —[/bold]",
        f"[bold]{total_calls:,}[/bold]",
        f"[bold]{total_in:,}[/bold]",
        f"[bold]{total_out:,}[/bold]",
        f"[bold]¥{total_cost:.4f}[/bold]",
        "",
    )
    console.print(table)
    console.print(f"[dim]source: {LOG_PATH}[/dim]")


def _stats_html(records, since, output: Path | None) -> None:
    """渲染 stats dashboard 到 HTML 文件。空数据也写一份"暂无记录"的页面。"""
    import time

    by_model = aggregate(records) if records else {}
    html_text = render_stats_dashboard(
        records=records,
        by_model=by_model,
        days_window=since,
        log_path=str(LOG_PATH),
    )

    if output is not None:
        target = Path(output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_text, encoding="utf-8")
        path = target
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = write_artifact(
            target_dir=Path.cwd() / ".codemesh" / "reports",
            filename=f"stats-{ts}.html",
            html_text=html_text,
            keep=20,
        )
    console.print(f"[green]✓[/green] HTML dashboard 已写入 [cyan]{path}[/cyan]")
    console.print(f"[dim]浏览器打开：[/dim] file://{path}")


if __name__ == "__main__":
    app()
