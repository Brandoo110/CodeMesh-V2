"""
Planner timeline 渲染单元测试
================================

跑法：
    python -m tests.test_planner_timeline

覆盖：
  - render_planner_timeline 输出自包含 HTML
  - 多个 status（done / error / pending）渲染对应类
  - 模型品牌色出现
  - 总成本 / 总耗时计算
  - 输出折叠（output 长时也能截断）
  - env 控制 maybe_write_plan
  - HTML escape
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from feedback.planner_timeline import (
    StepRecord,
    render_planner_timeline,
    maybe_write_plan,
    html_plan_enabled,
)


# ────────────────────────── render_planner_timeline ──────────────────────────


def _sample_steps() -> list[StepRecord]:
    return [
        StepRecord(n=1, description="读 auth.py 现有用法",
                   suggested_model="deepseek", needs_tools=True,
                   status="done", output="file content...",
                   duration_ms=820.0, cost_rmb=0.0008),
        StepRecord(n=2, description="重写 auth.py",
                   suggested_model="qwen", needs_tools=True,
                   status="done", output="rewritten code...",
                   duration_ms=2100.0, cost_rmb=0.0042),
        StepRecord(n=3, description="跑测试",
                   suggested_model="doubao", needs_tools=True,
                   status="error", output="[ERROR] test_auth failed",
                   duration_ms=1500.0, cost_rmb=0.0017,
                   error="AssertionError: expected 200 got 401"),
    ]


def test_timeline_self_contained():
    out = render_planner_timeline(
        task="重构 auth 模块", summary="3 步重构", steps=_sample_steps()
    )
    assert out.startswith("<!doctype html>")
    assert "<style>" in out
    # 没有外链
    assert "<link" not in out.lower()
    assert "src=" not in out.lower()


def test_timeline_includes_task_and_summary():
    out = render_planner_timeline(
        task="重构 auth 模块", summary="拆 3 步：读 / 写 / 测", steps=_sample_steps()
    )
    assert "重构 auth 模块" in out
    assert "拆 3 步" in out


def test_timeline_step_status_classes():
    out = render_planner_timeline(
        task="x", summary="y", steps=_sample_steps()
    )
    assert 'class="step done"' in out
    assert 'class="step error"' in out


def test_timeline_model_brand_colors():
    out = render_planner_timeline(
        task="x", summary="y", steps=_sample_steps()
    )
    # deepseek=#5b8def, qwen=#7c3aed, doubao=#ef4444
    assert "#5b8def" in out
    assert "#7c3aed" in out
    assert "#ef4444" in out


def test_timeline_totals():
    out = render_planner_timeline(
        task="x", summary="y", steps=_sample_steps()
    )
    # total cost = 0.0008 + 0.0042 + 0.0017 = 0.0067
    assert "¥0.0067" in out
    # total ms = 820 + 2100 + 1500 = 4420 → 4.4s
    assert "4.4s" in out


def test_timeline_error_message_visible():
    out = render_planner_timeline(
        task="x", summary="y", steps=_sample_steps()
    )
    assert "AssertionError" in out


def test_timeline_truncates_long_output():
    long_out = "x" * 20_000
    steps = [StepRecord(
        n=1, description="d", suggested_model="deepseek", needs_tools=True,
        status="done", output=long_out, duration_ms=100, cost_rmb=0,
    )]
    out = render_planner_timeline(task="x", summary="y", steps=steps)
    # 只截前 5000
    assert "x" * 5000 in out
    assert "x" * 5001 not in out


def test_timeline_empty_steps():
    out = render_planner_timeline(task="x", summary="y", steps=[])
    assert "<!doctype html>" in out
    # 不崩，没 step 卡片
    assert 'class="step done"' not in out


def test_timeline_zero_duration_does_not_crash():
    """所有步骤 duration=0 时，proportion bar 退化为均分。"""
    steps = [StepRecord(
        n=i, description=f"s{i}", suggested_model="deepseek",
        needs_tools=False, status="done", output="", duration_ms=0, cost_rmb=0,
    ) for i in range(1, 4)]
    out = render_planner_timeline(task="x", summary="y", steps=steps)
    assert 'class="tl-bar"' in out


def test_timeline_escapes_evil_inputs():
    steps = [StepRecord(
        n=1, description="<script>x</script>", suggested_model="deepseek",
        needs_tools=False, status="done", output="<img onerror=alert(1)>",
        duration_ms=0, cost_rmb=0,
    )]
    out = render_planner_timeline(
        task="<bad>", summary="<evil>", steps=steps
    )
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    assert "<img onerror" not in out


# ────────────────────────── maybe_write_plan ──────────────────────────


def _set_env(value: str | None):
    old = os.environ.get("CODEMESH_HTML_PLAN")
    if value is None:
        os.environ.pop("CODEMESH_HTML_PLAN", None)
    else:
        os.environ["CODEMESH_HTML_PLAN"] = value
    return old


def test_html_plan_disabled_default():
    old = _set_env(None)
    try:
        assert html_plan_enabled() is False
    finally:
        _set_env(old)


def test_maybe_write_plan_no_op_when_disabled():
    old = _set_env(None)
    try:
        with tempfile.TemporaryDirectory() as td:
            result = maybe_write_plan(
                task="x", summary="y", steps=_sample_steps(),
                target_dir=Path(td),
            )
            assert result is None
            assert list(Path(td).iterdir()) == []
    finally:
        _set_env(old)


def test_maybe_write_plan_writes_when_enabled():
    old = _set_env("1")
    try:
        with tempfile.TemporaryDirectory() as td:
            result = maybe_write_plan(
                task="x", summary="y", steps=_sample_steps(),
                target_dir=Path(td),
            )
            assert result is not None
            content = result.read_text()
            assert "<!doctype html>" in content
            assert "重构 auth 模块" not in content  # task 不是这个
            assert "deepseek" in content
    finally:
        _set_env(old)


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
    print(f"\n{len(tests) - failed}/{len(tests)} planner_timeline tests passed.")
    if failed:
        raise SystemExit(1)
