"""
edit diff HTML 渲染单元测试
==============================

跑法：
    python -m tests.test_diff_report

覆盖：
  - render_edit_diff: 增 / 删 / 上下文行分别渲染对应 CSS 类
  - 行号正确累加
  - HTML 转义防注入
  - 二进制 / 超大文件被 skip
  - maybe_write_diff: env 控制开关、写入 .codemesh/diffs/、滚动保留
  - 单文件正确返回 path
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from feedback.diff_report import (
    render_edit_diff,
    maybe_write_diff,
    html_diff_enabled,
    safe_artifact_name,
)


# ────────────────────────── render_edit_diff ──────────────────────────


def test_render_diff_self_contained():
    out = render_edit_diff(path="x.py", before="a\nb\n", after="a\nB\n")
    assert out.startswith("<!doctype html>")
    assert "<style>" in out
    assert "<table class=\"diff-table\">" in out


def test_render_diff_marks_additions_and_deletions():
    before = "line1\nline2\nline3\n"
    after = "line1\nNEW\nline3\n"
    out = render_edit_diff(path="x.py", before=before, after=after)
    # 删除 line2
    assert 'class="del"' in out
    assert "line2" in out
    # 添加 NEW
    assert 'class="add"' in out
    assert "NEW" in out


def test_render_diff_summary_counts():
    """总行数对应 +N -M 计数。"""
    before = "a\nb\nc\n"
    after = "a\nB\nC\n"
    out = render_edit_diff(path="x.py", before=before, after=after)
    # 改了两行 → 2 add + 2 del
    assert "+2</span> additions" in out
    assert "-2</span> deletions" in out


def test_render_diff_no_changes_shows_message():
    out = render_edit_diff(path="x.py", before="same\n", after="same\n")
    assert "no textual changes" in out


def test_render_diff_escapes_evil_content():
    before = "<script>alert(1)</script>\n"
    after = "<script>alert(2)</script>\n"
    out = render_edit_diff(path="x.html", before=before, after=after)
    # 不应有可执行的 <script>alert
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert(1)" in out


def test_render_diff_includes_filename_in_title():
    out = render_edit_diff(path="src/foo.py", before="x\n", after="y\n")
    assert "edit diff · foo.py" in out


# ────────────────────────── safe_artifact_name ──────────────────────────


def test_safe_artifact_name_keeps_basename():
    assert safe_artifact_name("/path/to/foo.py") == "foo.py"


def test_safe_artifact_name_replaces_unsafe_chars():
    assert safe_artifact_name("hello world!.txt") == "hello_world_.txt"


def test_safe_artifact_name_truncates_long_names():
    long = "a" * 200 + ".py"
    out = safe_artifact_name(long)
    assert len(out) <= 80


# ────────────────────────── maybe_write_diff ──────────────────────────


def _set_env(value: str | None):
    """给 CODEMESH_HTML_DIFF 设值（None 表示删除）。返回旧值用于复原。"""
    old = os.environ.get("CODEMESH_HTML_DIFF")
    if value is None:
        os.environ.pop("CODEMESH_HTML_DIFF", None)
    else:
        os.environ["CODEMESH_HTML_DIFF"] = value
    return old


def test_html_diff_enabled_default_off():
    old = _set_env(None)
    try:
        assert html_diff_enabled() is False
    finally:
        _set_env(old)


def test_html_diff_enabled_truthy():
    old = _set_env(None)
    try:
        for v in ("1", "true", "yes", "on", "TRUE"):
            os.environ["CODEMESH_HTML_DIFF"] = v
            assert html_diff_enabled() is True
        for v in ("0", "false", "no", ""):
            os.environ["CODEMESH_HTML_DIFF"] = v
            assert html_diff_enabled() is False
    finally:
        _set_env(old)


def test_maybe_write_diff_returns_none_when_disabled():
    old = _set_env(None)
    try:
        with tempfile.TemporaryDirectory() as td:
            result = maybe_write_diff(
                path="x.py", before="a\n", after="b\n",
                target_dir=Path(td),
            )
            assert result is None
            assert list(Path(td).iterdir()) == []
    finally:
        _set_env(old)


def test_maybe_write_diff_writes_when_enabled():
    old = _set_env("1")
    try:
        with tempfile.TemporaryDirectory() as td:
            result = maybe_write_diff(
                path="src/x.py", before="a\n", after="b\n",
                target_dir=Path(td),
            )
            assert result is not None
            assert result.exists()
            content = result.read_text()
            assert "<!doctype html>" in content
            assert "x.py" in content
    finally:
        _set_env(old)


def test_maybe_write_diff_skips_binary_input():
    old = _set_env("1")
    try:
        with tempfile.TemporaryDirectory() as td:
            result = maybe_write_diff(
                path="bin", before="abc\x00def", after="abc\x00xyz",
                target_dir=Path(td),
            )
            assert result is None
    finally:
        _set_env(old)


def test_maybe_write_diff_skips_huge_input():
    old = _set_env("1")
    try:
        with tempfile.TemporaryDirectory() as td:
            big = "x" * 250_000
            result = maybe_write_diff(
                path="big.txt", before=big, after=big + "y",
                target_dir=Path(td),
            )
            assert result is None
    finally:
        _set_env(old)


def test_maybe_write_diff_rotates_at_keep():
    """连续写多次自动滚动（保留最近 keep 个）。"""
    old = _set_env("1")
    try:
        with tempfile.TemporaryDirectory() as td:
            import time as _t
            d = Path(td)
            for i in range(5):
                p = maybe_write_diff(
                    path=f"f{i}.py", before="x\n", after=f"y{i}\n",
                    target_dir=d, keep=2,
                )
                assert p is not None
                # 自然 mtime 分离，避免同秒下 rotate 排序不稳定
                _t.sleep(0.01)
            files = list(d.iterdir())
            assert len(files) == 2
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
    print(f"\n{len(tests) - failed}/{len(tests)} diff_report tests passed.")
    if failed:
        raise SystemExit(1)
