"""
HTML 渲染基建单元测试
=========================

跑法：
    python -m tests.test_render_html

覆盖：
  - HtmlDoc.to_string 自包含（DOCTYPE / inline CSS / 没外链 src）
  - escape 防 XSS
  - bar / sparkline / pie 三个原语：空数据不崩、单值不崩、正常情况输出 SVG
  - rotate_dir 滚动保留最近 N 个
  - write_artifact 写入 + 触发滚动
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

from feedback.render_html import (
    HtmlDoc,
    BarDatum,
    PieSlice,
    escape,
    model_color,
    horizontal_bar_chart,
    sparkline,
    pie_chart,
    write_artifact,
    rotate_dir,
)


# ────────────────────────── HtmlDoc / escape ──────────────────────────


def test_doc_is_self_contained():
    doc = HtmlDoc(title="hello", body="<p>x</p>")
    out = doc.to_string()
    assert out.startswith("<!doctype html>")
    # CSS 必须 inline
    assert "<style>" in out
    # 不允许 <link rel=stylesheet ...> 或 <script src=...>
    assert "<link" not in out.lower()
    assert "src=" not in out.lower() or "src=\"" not in out.lower()
    # body 内容嵌入
    assert "<p>x</p>" in out
    # title escape
    assert "<title>hello</title>" in out


def test_doc_escapes_title():
    doc = HtmlDoc(title="<script>", body="")
    out = doc.to_string()
    assert "<script>" not in out.split("<style>")[0]  # head 区不能裸 <script>
    assert "&lt;script&gt;" in out


def test_escape_basic():
    assert escape("<a>&\"") == "&lt;a&gt;&amp;&quot;"
    assert escape("") == ""


def test_model_color_known_unknown():
    assert model_color("deepseek").startswith("#")
    assert model_color("nonexistent-model") == "#7f8aa3"  # unknown sentinel


# ────────────────────────── horizontal_bar_chart ──────────────────────────


def test_bar_chart_empty_data_returns_placeholder():
    out = horizontal_bar_chart([])
    assert "no data" in out
    assert "<svg" not in out


def test_bar_chart_renders_svg_with_rect_per_row():
    data = [
        BarDatum(label="deepseek", value=10, sub="¥0.10", color="#5b8def"),
        BarDatum(label="qwen", value=5, sub="¥0.05", color="#7c3aed"),
    ]
    out = horizontal_bar_chart(data)
    assert out.startswith("<svg")
    # 两行 → 两个 rect
    assert out.count("<rect") == 2
    # 标签出现
    assert "deepseek" in out
    assert "qwen" in out
    assert "¥0.10" in out


def test_bar_chart_handles_all_zero_values():
    """全 0 不应除零崩。"""
    data = [
        BarDatum(label="a", value=0, sub=""),
        BarDatum(label="b", value=0, sub=""),
    ]
    out = horizontal_bar_chart(data)
    # 不抛、有 svg
    assert "<svg" in out


def test_bar_chart_label_escaped():
    data = [BarDatum(label="<script>", value=1, sub="")]
    out = horizontal_bar_chart(data)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ────────────────────────── sparkline ──────────────────────────


def test_sparkline_empty():
    out = sparkline([])
    assert out.startswith("<svg")
    # 空数据：没有 polyline
    assert "polyline" not in out


def test_sparkline_single_value():
    out = sparkline([5.0])
    assert "<line" in out  # 单点画一根横线


def test_sparkline_multiple_points_makes_polyline():
    out = sparkline([1.0, 2.0, 3.0, 4.0])
    assert "polyline" in out
    # 抓真正的 polyline（fill 模式下还有一个 polygon 在前面，要锁定标签名）
    m = re.search(r'<polyline[^>]+points="([^"]+)"', out)
    assert m is not None
    coords = m.group(1).split()
    assert len(coords) == 4


def test_sparkline_constant_values_does_not_div_zero():
    """所有值相同 → vmax==vmin，不要除零崩。"""
    out = sparkline([3.0, 3.0, 3.0])
    assert "<svg" in out


# ────────────────────────── pie_chart ──────────────────────────


def test_pie_empty_total_returns_circle_outline():
    out = pie_chart([PieSlice("a", 0, "#fff")])
    assert "<circle" in out
    assert "<path" not in out


def test_pie_single_slice_full_circle():
    out = pie_chart([PieSlice("only", 5, "#10b981")])
    # 全占据一个 slice → 用 <circle> 而不是 path（避免起止点重合）
    assert "<circle" in out
    assert "100.0%" in out


def test_pie_multiple_slices_paths():
    out = pie_chart([
        PieSlice("a", 1, "#ef4444"),
        PieSlice("b", 2, "#10b981"),
        PieSlice("c", 1, "#5b8def"),
    ])
    # 多个 slice → path
    assert out.count("<path") == 3
    # legend 显示百分比
    assert "25.0%" in out
    assert "50.0%" in out


# ────────────────────────── rotate_dir / write_artifact ──────────────────────────


def test_rotate_dir_keeps_n_most_recent():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 造 5 个不同 mtime 的 html
        for i in range(5):
            p = d / f"file_{i}.html"
            p.write_text(f"<html>{i}</html>")
            # 强制不同 mtime（向前推秒）
            os.utime(p, (time.time() - (5 - i) * 10, time.time() - (5 - i) * 10))
        deleted = rotate_dir(d, keep=3)
        assert deleted == 2
        remaining = sorted(p.name for p in d.iterdir())
        assert remaining == ["file_2.html", "file_3.html", "file_4.html"]


def test_rotate_dir_only_targets_matching_suffix():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "a.html").write_text("a")
        (d / "b.txt").write_text("b")
        (d / "c.html").write_text("c")
        rotate_dir(d, keep=1, suffix=".html")
        names = sorted(p.name for p in d.iterdir())
        # .txt 必须保留
        assert "b.txt" in names


def test_rotate_dir_no_op_on_missing_dir():
    """目录不存在不应抛。"""
    rotate_dir(Path("/nonexistent/path/xyz"), keep=5)


def test_write_artifact_creates_file_and_rotates():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "reports"
        # 先写 4 个文件（用大 keep 让 rotate 不动它们），手动设 mtime 制造顺序，
        # 最后再单独 rotate，避免"先 rotate 再 utime"导致的 FileNotFound
        for i in range(4):
            p = write_artifact(
                target_dir=d,
                filename=f"r_{i}.html",
                html_text=f"<html>{i}</html>",
                keep=99,
            )
            os.utime(p, (time.time() + i * 10, time.time() + i * 10))
        # 最后 rotate 到 2 个
        rotate_dir(d, keep=2)
        remaining = sorted(p.name for p in d.iterdir())
        assert remaining == ["r_2.html", "r_3.html"]


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
    print(f"\n{len(tests) - failed}/{len(tests)} render_html tests passed.")
    if failed:
        raise SystemExit(1)
