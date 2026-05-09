"""
stats HTML 报告渲染单元测试
=============================

跑法：
    python -m tests.test_stats_report

覆盖：
  - 空记录 → 不崩，给出"暂无记录"页面
  - 典型记录 → 4 个面板 (KPI / bar+pie / daily / table) 都渲染
  - title 区分 windowed vs all-time
  - cost / token 数值出现在输出里
  - daily trend 按天分桶
  - 输出是自包含 HTML（DOCTYPE + inline style）
"""

from __future__ import annotations

import re
import time

from feedback.call_log import aggregate
from feedback.stats_report import render_stats_dashboard


def _make_records(now: float | None = None) -> list[dict]:
    """造一批典型记录：3 模型，跨 3 天。"""
    if now is None:
        now = time.time()
    day = 86400.0
    return [
        {"ts": now - 0 * day, "model": "deepseek", "tokens_in": 100, "tokens_out": 50, "cost_rmb": 0.0002, "latency_ms": 800},
        {"ts": now - 0 * day, "model": "deepseek", "tokens_in": 200, "tokens_out": 80, "cost_rmb": 0.0004, "latency_ms": 1200},
        {"ts": now - 1 * day, "model": "qwen", "tokens_in": 50, "tokens_out": 30, "cost_rmb": 0.0008, "latency_ms": 600},
        {"ts": now - 2 * day, "model": "doubao", "tokens_in": 1000, "tokens_out": 200, "cost_rmb": 0.0014, "latency_ms": 1500},
    ]


# ────────────────────────── 空数据路径 ──────────────────────────


def test_empty_records_renders_placeholder():
    out = render_stats_dashboard(records=[], by_model={}, days_window=7.0)
    assert "<!doctype html>" in out
    assert "还没有调用记录" in out or "没有调用记录" in out
    # 不应有 KPI / 表格元素（CSS 类名出现在 <style> 里没关系，关心的是 DOM）
    assert '<div class="kpi-row">' not in out
    assert '<table class="cm-table">' not in out


def test_empty_records_with_log_path_shows_source():
    out = render_stats_dashboard(
        records=[], by_model={}, days_window=None, log_path="/tmp/calls.jsonl"
    )
    assert "/tmp/calls.jsonl" in out


# ────────────────────────── 典型数据 ──────────────────────────


def test_full_dashboard_has_all_panels():
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    # 自包含
    assert out.startswith("<!doctype html>")
    assert "<style>" in out
    # 4 个面板
    assert "kpi-row" in out
    assert "cost by model" in out
    assert "calls share" in out
    assert "daily cost trend" in out
    assert "per-model breakdown" in out
    # 模型名都在
    assert "deepseek" in out
    assert "qwen" in out
    assert "doubao" in out


def test_kpi_totals_correct():
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    # total calls = 4
    assert re.search(r'>4<', out) or "4 calls" in out
    # total cost ≈ 0.0028
    assert "¥0.0028" in out
    # total tokens_in = 100+200+50+1000 = 1350
    assert "1,350" in out


def test_title_uses_window_label():
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    assert "last 7d" in out
    assert "4 calls" in out


def test_title_all_time_when_window_none():
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=None)
    assert "all-time" in out


def test_per_model_table_rows():
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    # 表 + 3 个数据 row + 1 个总计 row
    table_section = out[out.find("per-model breakdown"):]
    # tbody 行数 = 3
    assert table_section.count("<tr>") >= 4  # header + 3 model rows
    # 总计 row
    assert "— total —" in out


def test_model_brand_color_appears_in_dots():
    """暗色 dot 应该用模型品牌色。"""
    records = _make_records()
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    # deepseek = #5b8def
    assert "#5b8def" in out
    # doubao = #ef4444
    assert "#ef4444" in out


# ────────────────────────── 边界 ──────────────────────────


def test_all_zero_cost_does_not_crash():
    records = [
        {"ts": time.time(), "model": "x", "tokens_in": 0, "tokens_out": 0, "cost_rmb": 0.0},
    ]
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=1.0)
    assert "<!doctype html>" in out


def test_single_call_renders():
    records = [
        {"ts": time.time(), "model": "deepseek", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.0001},
    ]
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=7.0)
    # 单点 sparkline 不应崩
    assert "<svg" in out
    assert "deepseek" in out


def test_records_without_ts_skipped_in_daily_trend():
    """没 ts 的记录不参与 daily 桶但不能让函数崩。"""
    records = [
        {"ts": time.time(), "model": "a", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.001},
        {"model": "b", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.002},  # 没 ts
    ]
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=None)
    assert "<!doctype html>" in out


def test_html_escapes_unknown_model_names():
    records = [
        {"ts": time.time(), "model": "<evil>", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.0},
    ]
    by_model = aggregate(records)
    out = render_stats_dashboard(records=records, by_model=by_model, days_window=None)
    # 不应有裸 <evil>
    assert "<evil>" not in out
    assert "&lt;evil&gt;" in out


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
    print(f"\n{len(tests) - failed}/{len(tests)} stats_report tests passed.")
    if failed:
        raise SystemExit(1)
