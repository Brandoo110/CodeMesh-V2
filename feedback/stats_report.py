"""
Stats HTML 报告渲染（Harness 反馈层）
========================================

【这模块解决什么】
`codemesh stats` 现在只能在终端里 print Rich Table，但"国内多模型成本对比"是
项目核心卖点之一——给用户和维护者看一张终端表格远远不够。

这模块基于 `feedback/render_html.py` 把 calls.jsonl 渲染成单文件 dashboard：
  - KPI 行：调用数 / 总 token / 总成本 / 窗口大小
  - 横向 bar：各模型成本 / 各模型 token
  - pie：调用次数占比
  - sparkline：每日成本趋势
  - 详细表格：每个模型一行

【设计要点】
"Q: 为什么不直接读 jsonl 写报告？"
→ 数据源（call_log.jsonl）和聚合（aggregate）已经有了，这层只是"把同样数据
  换个展示形态"。零新数据流，纯渲染。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Sequence

from .render_html import (
    BarDatum,
    HtmlDoc,
    PieSlice,
    escape,
    horizontal_bar_chart,
    model_color,
    pie_chart,
    sparkline,
)


# Stats 页专属 CSS（叠加在 BASE_CSS 之上）
_STATS_CSS = """
.stats-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 16px;
}
@media (max-width: 800px) {
  .stats-grid { grid-template-columns: 1fr; }
}
.empty {
  color: var(--dim);
  font-size: 12px;
  padding: 12px 0;
  text-align: center;
}
.daily-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.daily-row:last-child { border-bottom: none; }
.daily-row .date { width: 80px; color: var(--dim); }
.daily-row .spark { flex: 0 0 auto; }
.daily-row .meta { margin-left: auto; color: var(--dim); }
.daily-row .meta .cost { color: var(--text); }
"""


def render_stats_dashboard(
    *,
    records: Sequence[dict[str, Any]],
    by_model: dict[str, dict[str, Any]],
    days_window: float | None,
    log_path: str | None = None,
) -> str:
    """
    渲染 stats dashboard。

    Args:
        records      : 原始 call 记录（read_calls 的输出）
        by_model     : 聚合（aggregate 的输出）
        days_window  : 时间窗口（None=全部历史）
        log_path     : 数据源路径，footer 显示用

    Returns:
        完整 HTML 字符串。
    """
    title = _format_title(records, days_window)
    body = _build_body(records, by_model, days_window, log_path)
    doc = HtmlDoc(
        title=title,
        body=body,
        extra_css=_STATS_CSS,
    )
    return doc.to_string()


def _format_title(records, days_window) -> str:
    n = len(records)
    if days_window:
        return f"CodeMesh stats · last {days_window:g}d · {n} calls"
    return f"CodeMesh stats · all-time · {n} calls"


def _build_body(records, by_model, days_window, log_path) -> str:
    if not records:
        return _empty_body(days_window, log_path)

    parts: list[str] = []

    # 1. KPIs
    parts.append(_kpis(records, by_model, days_window))

    # 2. Bars + Pie
    parts.append(_bars_and_pie_panel(by_model))

    # 3. Daily trend sparklines
    parts.append(_daily_trend_panel(records))

    # 4. Per-model table
    parts.append(_table_panel(by_model))

    if log_path:
        parts.append(
            f'<div style="color:var(--dim);font-size:11px;margin-top:24px;'
            f'font-family:ui-monospace,Menlo,Consolas,monospace">'
            f'source: {escape(log_path)}</div>'
        )

    return "\n".join(parts)


def _empty_body(days_window, log_path) -> str:
    msg = (
        f"最近 {days_window:g} 天没有调用记录。"
        if days_window else "还没有调用记录。"
    )
    src = (
        f'<div style="color:var(--dim);font-size:11px;margin-top:8px;'
        f'font-family:ui-monospace,Menlo,Consolas,monospace">'
        f'source: {escape(log_path)}</div>'
    ) if log_path else ""
    return (
        f'<div class="panel"><div class="empty">{escape(msg)} '
        f'先跑几次 <code>codemesh run</code> 再来看。</div>{src}</div>'
    )


def _kpis(records, by_model, days_window) -> str:
    total_calls = len(records)
    total_in = sum(int(r.get("tokens_in", 0)) for r in records)
    total_out = sum(int(r.get("tokens_out", 0)) for r in records)
    total_cost = sum(float(r.get("cost_rmb", 0.0)) for r in records)
    window_label = f"{days_window:g} days" if days_window else "all-time"
    n_models = len(by_model)

    cards = [
        ("calls", f"{total_calls:,}", f"{n_models} models · {window_label}"),
        ("tokens in", f"{total_in:,}", "prompt tokens"),
        ("tokens out", f"{total_out:,}", "completion tokens"),
        ("cost", f"¥{total_cost:.4f}", "total spend"),
    ]
    items = []
    for label, value, sub in cards:
        items.append(
            f'<div class="kpi">'
            f'<div class="label">{escape(label)}</div>'
            f'<div class="value">{escape(value)}</div>'
            f'<div class="sub">{escape(sub)}</div>'
            f'</div>'
        )
    return f'<div class="kpi-row">{"".join(items)}</div>'


def _bars_and_pie_panel(by_model) -> str:
    # cost bar
    cost_data = [
        BarDatum(
            label=m,
            value=agg["cost_rmb"],
            color=model_color(m),
            sub=f"¥{agg['cost_rmb']:.4f}",
        )
        for m, agg in sorted(by_model.items(), key=lambda kv: -kv[1]["cost_rmb"])
    ]
    # tokens bar (sum in+out)
    tok_data = [
        BarDatum(
            label=m,
            value=agg["tokens_in"] + agg["tokens_out"],
            color=model_color(m),
            sub=f"{agg['tokens_in'] + agg['tokens_out']:,}",
        )
        for m, agg in sorted(
            by_model.items(),
            key=lambda kv: -(kv[1]["tokens_in"] + kv[1]["tokens_out"]),
        )
    ]
    # calls pie
    pie_data = [
        PieSlice(label=m, value=agg["calls"], color=model_color(m))
        for m, agg in by_model.items()
    ]
    return f"""
    <div class="stats-grid">
      <div class="panel">
        <h2>cost by model</h2>
        {horizontal_bar_chart(cost_data)}
        <h3 style="margin-top:18px">tokens (in + out) by model</h3>
        {horizontal_bar_chart(tok_data)}
      </div>
      <div class="panel">
        <h2>calls share</h2>
        {pie_chart(pie_data, size=160)}
      </div>
    </div>
    """


def _daily_trend_panel(records) -> str:
    """
    把记录按天 + 模型分桶，给每个模型画一条 sparkline。
    时间窗口 < 1 天会退化为单点；> 30 天截最近 30 天。
    """
    by_day_model = _bucket_by_day_and_model(records)
    if not by_day_model:
        return ""

    days = sorted(by_day_model.keys())[-30:]
    if not days:
        return ""

    # 各模型每日成本序列
    model_set: set[str] = set()
    for d in days:
        model_set.update(by_day_model[d].keys())

    rows: list[str] = []
    for m in sorted(model_set):
        series = [by_day_model[d].get(m, 0.0) for d in days]
        total = sum(series)
        if total == 0.0:
            continue
        color = model_color(m)
        rows.append(f"""
        <div class="daily-row">
          <span class="date">{escape(m)}</span>
          <span class="spark">{sparkline(series, width=240, height=24, color=color)}</span>
          <span class="meta">{len(days)}d · <span class="cost">¥{total:.4f}</span></span>
        </div>
        """)

    if not rows:
        return ""

    return f"""
    <div class="panel">
      <h2>daily cost trend</h2>
      <h3>last {len(days)} day{'s' if len(days) != 1 else ''} · per-model sparkline</h3>
      {''.join(rows)}
    </div>
    """


def _bucket_by_day_and_model(records) -> dict[str, dict[str, float]]:
    """{ 'YYYY-MM-DD': { 'model': cost_rmb_total } }。"""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        ts = r.get("ts")
        if ts is None:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        m = r.get("model", "unknown")
        out[day][m] += float(r.get("cost_rmb", 0.0))
    return out


def _table_panel(by_model) -> str:
    rows = []
    total_calls = total_in = total_out = 0
    total_cost = 0.0
    for m in sorted(by_model.keys()):
        a = by_model[m]
        total_calls += a["calls"]
        total_in += a["tokens_in"]
        total_out += a["tokens_out"]
        total_cost += a["cost_rmb"]
        lat = a["avg_latency_ms"]
        lat_text = f"{lat:.0f}ms" if lat is not None else "-"
        color = model_color(m)
        rows.append(f"""
        <tr>
          <td><span class="dot" style="background:{color}"></span>{escape(m)}</td>
          <td class="num">{a['calls']:,}</td>
          <td class="num">{a['tokens_in']:,}</td>
          <td class="num">{a['tokens_out']:,}</td>
          <td class="num">¥{a['cost_rmb']:.4f}</td>
          <td class="num">{escape(lat_text)}</td>
        </tr>
        """)
    return f"""
    <div class="panel">
      <h2>per-model breakdown</h2>
      <table class="cm-table">
        <thead>
          <tr>
            <th>model</th>
            <th class="num">calls</th>
            <th class="num">tokens in</th>
            <th class="num">tokens out</th>
            <th class="num">cost</th>
            <th class="num">avg latency</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
        <tfoot>
          <tr>
            <td>— total —</td>
            <td class="num">{total_calls:,}</td>
            <td class="num">{total_in:,}</td>
            <td class="num">{total_out:,}</td>
            <td class="num">¥{total_cost:.4f}</td>
            <td class="num"></td>
          </tr>
        </tfoot>
      </table>
    </div>
    """


__all__ = ["render_stats_dashboard"]
