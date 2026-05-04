"""
CLI 单元测试（不真调模型 / 不读真实 .env）
==============================================

跑法：
    python -m tests.test_cli

策略：
  用 typer.testing.CliRunner 跑命令；
  - stats 走完整代码路径（读本地 jsonl 或显示 empty 提示）
  - run / compare / index 用 monkeypatch 替换 Harness 和 build_index，
    避免真调 API
  - _preflight / _friendly_error 是模块级纯函数，单独测
"""

import json
import os
import tempfile
from pathlib import Path

from typer.testing import CliRunner

import cli as cli_mod
from feedback import call_log


runner = CliRunner()


def _isolate_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造一个干净的 env，只保留必要变量，确保 _preflight 决策可控。"""
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    if extra:
        base.update(extra)
    return base


# ────────────────────────── _friendly_error ──────────────────────────


def test_friendly_error_translates_auth():
    out = cli_mod._friendly_error(Exception("Invalid API key"))
    assert out is not None and "API key" in out


def test_friendly_error_translates_rate_limit():
    out = cli_mod._friendly_error(Exception("Rate limit exceeded (429)"))
    assert out is not None and ("限额" in out or "限流" in out)


def test_friendly_error_translates_timeout():
    class FakeErr(Exception):
        pass
    FakeErr.__name__ = "ModelHTTPError"
    out = cli_mod._friendly_error(FakeErr("connection timeout"))
    assert out is not None and "不可达" in out


def test_friendly_error_returns_none_for_unknown():
    """不能识别的错应返回 None 让上层照常抛。"""
    out = cli_mod._friendly_error(ValueError("unrelated thing"))
    assert out is None


# ────────────────────────── _preflight ──────────────────────────


def test_preflight_exits_when_no_env_file(tmp_path_factory=None):
    """没 .env 文件时 _preflight 应该 typer.Exit(1)。"""
    import typer
    base = Path(tempfile.mkdtemp(prefix="cli-pre-"))
    cwd_was = Path.cwd()
    try:
        os.chdir(base)
        try:
            cli_mod._preflight()
        except typer.Exit as e:
            assert e.exit_code == 1
            return
        raise AssertionError("expected typer.Exit")
    finally:
        os.chdir(cwd_was)


def test_preflight_passes_with_real_key():
    """有 .env 且至少一个 key 看起来真 → 通过。"""
    import typer
    base = Path(tempfile.mkdtemp(prefix="cli-pre-ok-"))
    (base / ".env").write_text("")
    cwd_was = Path.cwd()
    saved = os.environ.get("GEMINI_API_KEY")
    try:
        os.chdir(base)
        os.environ["GEMINI_API_KEY"] = "AIz" + "a" * 35  # 看起来像真 key
        cli_mod._preflight()  # 不该抛
    finally:
        os.chdir(cwd_was)
        if saved is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = saved


def test_preflight_rejects_placeholder_key():
    import typer
    base = Path(tempfile.mkdtemp(prefix="cli-pre-fake-"))
    (base / ".env").write_text("")
    cwd_was = Path.cwd()
    saved = os.environ.get("DEEPSEEK_API_KEY")
    try:
        os.chdir(base)
        os.environ["DEEPSEEK_API_KEY"] = "your-key-here"
        # 临时把其他真 key 全清掉
        cleared = {}
        for k in ("GEMINI_API_KEY", "DASHSCOPE_API_KEY", "VOLC_API_KEY", "GOOGLE_API_KEY"):
            if k in os.environ:
                cleared[k] = os.environ.pop(k)
        try:
            cli_mod._preflight()
        except typer.Exit as e:
            assert e.exit_code == 1
            return
        finally:
            for k, v in cleared.items():
                os.environ[k] = v
        raise AssertionError("expected typer.Exit")
    finally:
        os.chdir(cwd_was)
        if saved is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = saved


# ────────────────────────── stats 命令 ──────────────────────────


def _patch_read_calls(log_path: Path):
    """
    替换 cli.read_calls 让它从指定 log_path 读。
    单纯改 LOG_PATH 不够 —— read_calls 的默认参数在 def 时就 bind 了。
    返回一个 (saved, restore) 对，restore() 还原。
    """
    saved = cli_mod.read_calls
    real = call_log.read_calls

    def _patched(*, since_days=None, log_path=log_path):  # type: ignore[no-redef]
        return real(since_days=since_days, log_path=log_path)

    cli_mod.read_calls = _patched   # type: ignore[assignment]
    cli_mod.LOG_PATH = log_path     # type: ignore[assignment]
    return saved


def _restore_read_calls(saved):
    cli_mod.read_calls = saved  # type: ignore[assignment]


def test_stats_with_no_log_shows_empty_message():
    """日志文件不存在时 stats 应输出"没有调用记录"提示。"""
    fresh = Path(tempfile.mkdtemp(prefix="cli-stats-")) / "calls.jsonl"
    saved = _patch_read_calls(fresh)
    try:
        result = runner.invoke(cli_mod.app, ["stats"])
        assert result.exit_code == 0
        assert "没有调用记录" in result.stdout
    finally:
        _restore_read_calls(saved)


def test_stats_aggregates_records():
    """日志里塞两条记录，stats 输出应包含模型名 + 成本。"""
    log = Path(tempfile.mkdtemp(prefix="cli-stats-2-")) / "calls.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    import time
    now = time.time()
    log.write_text(
        json.dumps({"ts": now, "model": "qwen", "tokens_in": 10,
                    "tokens_out": 20, "cost_rmb": 0.001, "latency_ms": 200}) + "\n"
        + json.dumps({"ts": now, "model": "deepseek", "tokens_in": 100,
                      "tokens_out": 50, "cost_rmb": 0.01, "latency_ms": 800}) + "\n"
    )
    saved = _patch_read_calls(log)
    try:
        result = runner.invoke(cli_mod.app, ["stats", "--days", "1"])
        assert result.exit_code == 0
        assert "qwen" in result.stdout
        assert "deepseek" in result.stdout
        assert "total" in result.stdout
    finally:
        _restore_read_calls(saved)


def test_stats_window_filters_old_records():
    log = Path(tempfile.mkdtemp(prefix="cli-stats-3-")) / "calls.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    import time
    old = time.time() - 30 * 86400  # 30 天前
    log.write_text(
        json.dumps({"ts": old, "model": "old", "tokens_in": 1,
                    "tokens_out": 1, "cost_rmb": 0.0001}) + "\n"
    )
    saved = _patch_read_calls(log)
    try:
        result = runner.invoke(cli_mod.app, ["stats", "--days", "7"])
        assert result.exit_code == 0
        assert "没有调用记录" in result.stdout
    finally:
        _restore_read_calls(saved)


# ────────────────────────── run 命令（mock Harness）──────────────────────────


def test_run_invokes_harness_run(monkeypatch=None):
    """run 命令应该实例化 Harness 并 await harness.run(task)。"""
    calls = {"task": None, "ran": False}

    class _FakeHarness:
        def __init__(self, **kwargs):
            self.last_costs = []

        async def run(self, task):
            calls["task"] = task
            calls["ran"] = True
            return f"answer for {task}"

    # monkeypatch Harness, _preflight
    saved_h = cli_mod.Harness
    saved_pre = cli_mod._preflight
    cli_mod.Harness = _FakeHarness  # type: ignore[assignment]
    cli_mod._preflight = lambda: None  # type: ignore[assignment]
    try:
        result = runner.invoke(cli_mod.app, ["run", "hello task"])
        assert result.exit_code == 0
        assert calls["ran"] is True
        assert calls["task"] == "hello task"
        assert "answer for hello task" in result.stdout
    finally:
        cli_mod.Harness = saved_h  # type: ignore[assignment]
        cli_mod._preflight = saved_pre  # type: ignore[assignment]


def test_run_compare_flag_calls_compare(monkeypatch=None):
    """--compare 应该调 harness.compare，不调 run。"""
    state = {"compare": False, "run": False}

    class _FakeHarness:
        def __init__(self, **kwargs):
            self.last_costs = []

        async def run(self, task):
            state["run"] = True
            return ""

        async def compare(self, task):
            state["compare"] = True
            from feedback import compute_cost
            return {
                "deepseek": {"text": "ds answer", "cost": compute_cost("deepseek", 10, 5),
                             "latency_ms": 100},
                "qwen":     {"text": "qw answer", "cost": compute_cost("qwen", 10, 5),
                             "latency_ms": 200},
                "doubao":   {"text": "db answer", "cost": compute_cost("doubao", 10, 5),
                             "latency_ms": 150},
            }

    saved_h = cli_mod.Harness
    saved_pre = cli_mod._preflight
    cli_mod.Harness = _FakeHarness  # type: ignore[assignment]
    cli_mod._preflight = lambda: None  # type: ignore[assignment]
    try:
        result = runner.invoke(cli_mod.app, ["run", "task", "--compare"])
        assert result.exit_code == 0
        assert state["compare"] is True
        assert state["run"] is False
        assert "ds answer" in result.stdout
        assert "qw answer" in result.stdout
    finally:
        cli_mod.Harness = saved_h  # type: ignore[assignment]
        cli_mod._preflight = saved_pre  # type: ignore[assignment]


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
    print(f"\n{len(tests) - failed}/{len(tests)} cli tests passed.")
    if failed:
        raise SystemExit(1)
