"""
HTML 工件渲染基建（Harness 反馈层）
====================================

【这模块解决什么】
CodeMesh 的所有产物——stats、planner 输出、edit diff、架构图——之前都是文本。
但 diff、调用图、并排比较本质上是**空间信息**，markdown 是线性的会丢一个维度
（参考 thariqs.github.io/html-effectiveness）。

这模块提供"输出 HTML 工件"的共享基建：
  - 统一的暗色主题 CSS（emerald/red、等宽字体、sticky 表头）
  - SVG 原语（bar / sparkline / pie），无 PyPI 依赖
  - 自包含 HTML 文档 wrapper
  - 文件落盘 + 滚动清理

【为什么手写 SVG 而不是 matplotlib / plotly】
1. 零 PyPI 依赖：面试项目少一行 requirements.txt 就少一个解释成本
2. 单文件自包含：HTML 工件能直接发简历 / 公众号，不需要 server / runtime
3. 数据量小（几十条 calls）：matplotlib 杀鸡用牛刀，plotly 启动 ~MB JS
4. 可控样式：暗色主题、emerald 色一致

【边界（重要，别越界）】
**不**把 tool returns 改成 HTML——那是给 agent 吃的中间态，HTML 标签会污染
token 经济。HTML 工件是给**人**看的最终产物（dashboard / report / diagram）。

【面试点】
"Q: 你为什么不用 matplotlib？"
→ 单文件自包含 + 零依赖。SVG 原生支持 CSS 主题，导出简历也不会因缺 PIL 跑不出图。
"Q: 模板字符串拼 SVG 不会失控？"
→ 集中在这一个模块，路径是 escape() + 数值 clamp，不接受用户文本插值。
   类型化 dataclass 描述图表数据，不让调用方拼字符串。
"""

from __future__ import annotations

import html
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


# ───────────────── 主题常量（统一色板）─────────────────

# 暗色基调，对照 emerald/red 高对比度
COLOR_BG = "#0f1419"
COLOR_PANEL = "#1a1f2e"
COLOR_TEXT = "#e6e1cf"
COLOR_DIM = "#7f8aa3"
COLOR_BORDER = "#2a3142"

# 模型品牌色（保持各家品牌识别度，但都是 600 级深色）
MODEL_COLORS = {
    "deepseek": "#5b8def",   # 蓝
    "qwen": "#7c3aed",       # 紫
    "doubao": "#ef4444",     # 红
    "gemini": "#10b981",     # 绿
    "unknown": "#7f8aa3",
}

# 状态色
COLOR_GOOD = "#10b981"
COLOR_BAD = "#ef4444"
COLOR_WARN = "#f59e0b"


# ───────────────── 共享 CSS ─────────────────

BASE_CSS = """
:root {
  color-scheme: dark;
  --bg: #0f1419;
  --panel: #1a1f2e;
  --text: #e6e1cf;
  --dim: #7f8aa3;
  --border: #2a3142;
  --good: #10b981;
  --bad: #ef4444;
  --warn: #f59e0b;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
.codemesh-shell {
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}
.codemesh-shell > header {
  border-bottom: 1px solid var(--border);
  margin-bottom: 24px;
  padding-bottom: 16px;
}
.codemesh-shell > header h1 {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.codemesh-shell > header .meta {
  color: var(--dim);
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.codemesh-shell > footer {
  margin-top: 48px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  color: var(--dim);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px 20px;
  margin: 16px 0;
}
.panel h2 {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.panel h3 {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
table.cm-table {
  width: 100%;
  border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
}
table.cm-table th, table.cm-table td {
  text-align: left;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
}
table.cm-table th {
  position: sticky;
  top: 0;
  background: var(--panel);
  color: var(--dim);
  font-weight: 500;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.04em;
}
table.cm-table tr:hover td {
  background: rgba(255, 255, 255, 0.02);
}
table.cm-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
table.cm-table tfoot td {
  font-weight: 600;
  border-top: 1px solid var(--border);
  border-bottom: none;
  color: var(--text);
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 16px 0 24px;
}
.kpi {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 18px;
}
.kpi .label {
  color: var(--dim);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 4px;
}
.kpi .value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 22px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.kpi .sub {
  color: var(--dim);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  margin-top: 2px;
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: rgba(255, 255, 255, 0.06);
  color: var(--text);
}
a { color: #5b8def; text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
"""


# ───────────────── 文档 wrapper ─────────────────


@dataclass
class HtmlDoc:
    """一个完整 HTML 文档的描述。to_string() 输出自包含 HTML。"""

    title: str
    body: str
    extra_css: str = ""
    generator: str = "codemesh"
    timestamp: float = field(default_factory=time.time)

    def to_string(self) -> str:
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))
        css = BASE_CSS + self.extra_css
        return _DOC_TEMPLATE.format(
            title=html.escape(self.title),
            css=css,
            body=self.body,
            ts=ts_str,
            generator=html.escape(self.generator),
        )


_DOC_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="codemesh-shell">
<header>
  <h1>{title}</h1>
  <div class="meta">{generator} · {ts}</div>
</header>
{body}
<footer>generated by {generator} · {ts}</footer>
</div>
</body>
</html>
"""


def escape(text: str) -> str:
    """HTML escape 短包装；不接 None。"""
    return html.escape(text if text is not None else "", quote=True)


def model_color(name: str) -> str:
    """按模型名拿品牌色。未知名走 dim。"""
    return MODEL_COLORS.get(name, MODEL_COLORS["unknown"])


# ───────────────── SVG 原语 ─────────────────


@dataclass
class BarDatum:
    label: str
    value: float
    color: str = "#5b8def"
    sub: str = ""  # 副标签（成本 / token 之类的右侧文本）


def horizontal_bar_chart(
    data: Sequence[BarDatum],
    *,
    width: int = 520,
    row_height: int = 28,
    label_width: int = 90,
    sub_width: int = 110,
    pad: int = 8,
) -> str:
    """
    渲染一组横向 bar。每行：[label] [bar....] [sub]
    数据为空 → 返回友好占位。
    """
    if not data:
        return '<div class="empty">(no data)</div>'

    max_val = max((d.value for d in data), default=0.0) or 1.0
    bar_area = width - label_width - sub_width - pad * 2
    if bar_area < 80:
        bar_area = 80
    height = row_height * len(data) + pad * 2

    rows: list[str] = []
    for i, d in enumerate(data):
        y = pad + i * row_height
        bar_y = y + 6
        # bar 宽度按比例，最小 1 像素让"几乎为 0"也可见
        ratio = (d.value / max_val) if max_val > 0 else 0.0
        bw = max(1.0, ratio * bar_area)
        rows.append(
            f'<text x="{pad}" y="{y + row_height / 2 + 4}" '
            f'fill="{COLOR_TEXT}" font-size="12" '
            f'font-family="ui-monospace, Menlo, Consolas, monospace">'
            f'{escape(d.label)}</text>'
        )
        rows.append(
            f'<rect x="{pad + label_width}" y="{bar_y}" '
            f'width="{bw:.1f}" height="{row_height - 12}" '
            f'fill="{d.color}" rx="2" />'
        )
        rows.append(
            f'<text x="{width - pad}" y="{y + row_height / 2 + 4}" '
            f'fill="{COLOR_DIM}" font-size="11" text-anchor="end" '
            f'font-family="ui-monospace, Menlo, Consolas, monospace">'
            f'{escape(d.sub)}</text>'
        )
    inner = "\n".join(rows)
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">\n'
        f'{inner}\n</svg>'
    )


def sparkline(
    values: Sequence[float],
    *,
    width: int = 140,
    height: int = 28,
    color: str = "#5b8def",
    fill: bool = True,
) -> str:
    """
    简单 sparkline：折线 + 可选填充。
    点数 < 2 时返回横线占位。
    """
    n = len(values)
    if n == 0:
        return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}"></svg>'
    if n == 1:
        # 单点：画一根横线
        y = height / 2
        return (
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" '
            f'stroke="{color}" stroke-width="1.5" />'
            f'</svg>'
        )

    vmin = min(values)
    vmax = max(values)
    rng = vmax - vmin if vmax != vmin else 1.0
    pad_top = 3
    pad_bot = 3
    inner_h = height - pad_top - pad_bot

    pts: list[str] = []
    for i, v in enumerate(values):
        x = i * (width / (n - 1))
        y = pad_top + (1.0 - (v - vmin) / rng) * inner_h
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)

    fill_path = ""
    if fill:
        fill_path = (
            f'<polygon points="0,{height} {poly} {width},{height}" '
            f'fill="{color}" fill-opacity="0.15" />'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f'{fill_path}'
        f'<polyline points="{poly}" fill="none" '
        f'stroke="{color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />'
        f'</svg>'
    )


@dataclass
class PieSlice:
    label: str
    value: float
    color: str


def pie_chart(slices: Sequence[PieSlice], *, size: int = 180) -> str:
    """
    极简 pie。零值 / 全零 → 空圆。
    """
    total = sum(max(0.0, s.value) for s in slices)
    cx = cy = size / 2
    r = size / 2 - 4

    if total <= 0:
        return (
            f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{COLOR_BORDER}" stroke-width="1" />'
            f'</svg>'
        )

    def polar(angle_rad: float) -> tuple[float, float]:
        return cx + r * math.cos(angle_rad), cy + r * math.sin(angle_rad)

    paths: list[str] = []
    legend: list[str] = []
    start = -math.pi / 2  # 12 点钟方向开始
    for s in slices:
        if s.value <= 0:
            continue
        portion = s.value / total
        sweep = portion * 2 * math.pi
        end = start + sweep
        large = 1 if sweep > math.pi else 0
        x1, y1 = polar(start)
        x2, y2 = polar(end)
        # 单一切片占满 100% 时 SVG 起止点重合，用 circle 兜底
        if abs(portion - 1.0) < 1e-9:
            paths.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{s.color}" />'
            )
        else:
            paths.append(
                f'<path d="M {cx:.2f} {cy:.2f} '
                f'L {x1:.2f} {y1:.2f} '
                f'A {r:.2f} {r:.2f} 0 {large} 1 {x2:.2f} {y2:.2f} Z" '
                f'fill="{s.color}" />'
            )
        legend.append(
            f'<div class="legend-row">'
            f'<span class="dot" style="background:{s.color}"></span>'
            f'{escape(s.label)} '
            f'<span style="color:var(--dim)">{portion * 100:.1f}%</span>'
            f'</div>'
        )
        start = end

    svg = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f'{"".join(paths)}'
        f'</svg>'
    )
    legend_html = '<div class="pie-legend">' + "".join(legend) + '</div>'
    return (
        f'<div class="pie-block" style="display:flex;gap:18px;align-items:center;">'
        f'{svg}{legend_html}</div>'
    )


# ───────────────── 文件落盘 + 滚动清理 ─────────────────


def write_artifact(
    *,
    target_dir: Path,
    filename: str,
    html_text: str,
    keep: int = 30,
) -> Path:
    """
    写一个 HTML 工件到 target_dir/filename。
    自动建父目录、滚动保留最近 keep 个 .html（按 mtime 排序）。
    返回写入路径。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / filename
    path.write_text(html_text, encoding="utf-8")
    rotate_dir(target_dir, keep=keep)
    return path


def rotate_dir(target_dir: Path, *, keep: int = 30, suffix: str = ".html") -> int:
    """
    保留 target_dir 下最近 keep 个匹配 suffix 的文件，删掉更老的。
    返回删除数量。
    """
    if not target_dir.exists() or keep <= 0:
        return 0
    files = sorted(
        [p for p in target_dir.iterdir() if p.is_file() and p.suffix == suffix],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in files[keep:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


__all__ = [
    "BASE_CSS",
    "COLOR_BG",
    "COLOR_PANEL",
    "COLOR_TEXT",
    "COLOR_DIM",
    "COLOR_BORDER",
    "COLOR_GOOD",
    "COLOR_BAD",
    "COLOR_WARN",
    "MODEL_COLORS",
    "HtmlDoc",
    "BarDatum",
    "PieSlice",
    "escape",
    "model_color",
    "horizontal_bar_chart",
    "sparkline",
    "pie_chart",
    "write_artifact",
    "rotate_dir",
]
