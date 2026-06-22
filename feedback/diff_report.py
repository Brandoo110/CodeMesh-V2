"""
Edit diff HTML 渲染（Harness 反馈层）
========================================

【这模块解决什么】
模型每次调 `edit_file` 工具，原本只返回一行 "OK: edited path (+12 bytes)"。
对人类来说，这一行远远不够——尤其demo 时，"我让 agent 改了什么？"
需要打开能看的 diff。

这模块做"在 edit_file 成功后顺手把 diff 落盘成 HTML"，开关由 env var
`CODEMESH_HTML_DIFF=1` 控制（默认不开，避免无意义的写盘）。

【为什么不直接用 difflib.HtmlDiff】
`difflib.HtmlDiff` 自带渲染能力，但默认样式是 1990s 的 nostalgia——
浅蓝表格 + 紫色字。CodeMesh 主题是暗色 + emerald/red，要重套 CSS。

我们的做法：用 `difflib.unified_diff` 拿语义差异，自己渲染 side-by-side 的
HTML（行号 + 颜色块 + sticky 头部）。可控、零依赖、和 `render_html.py`
主题完全一致。

【设计要点】
"Q: 为啥不直接 git diff？"
→ Agent 修改的文件可能不在 git 仓库里（脚本、临时配置）；而且 git diff
  需要先 commit/stage，不适合"工具执行后立刻给人看"这个场景。

【边界】
- 不渲染二进制文件（utf-8 decode 失败的直接跳过）
- 单文件 > 200KB 渲染会很慢，截到前 5000 行
"""

from __future__ import annotations

import difflib
import os
import re
import time
from pathlib import Path
from typing import Optional

from .render_html import HtmlDoc, escape, write_artifact


# 渲染上限：行数 / 字节
_MAX_LINES = 5000
_MAX_BYTES = 200_000


_DIFF_CSS = """
.diff-table {
  width: 100%;
  border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  table-layout: fixed;
}
.diff-table th, .diff-table td {
  padding: 1px 8px;
  vertical-align: top;
  white-space: pre-wrap;
  word-break: break-all;
}
.diff-table th {
  position: sticky;
  top: 0;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--dim);
  padding: 8px;
  font-weight: 500;
}
.diff-table td.ln {
  width: 50px;
  text-align: right;
  color: var(--dim);
  user-select: none;
  border-right: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
}
.diff-table tr.ctx td.code { background: transparent; color: var(--text); }
.diff-table tr.add td.code { background: rgba(16, 185, 129, 0.12); color: #d1fae5; }
.diff-table tr.del td.code { background: rgba(239, 68, 68, 0.12); color: #fee2e2; }
.diff-table tr.hdr td {
  background: rgba(91, 141, 239, 0.10);
  color: #93b8ff;
  font-style: italic;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.diff-summary {
  display: flex;
  gap: 18px;
  font-size: 12px;
  color: var(--dim);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  padding-bottom: 12px;
}
.diff-summary .add { color: var(--good); }
.diff-summary .del { color: var(--bad); }
"""


def render_edit_diff(
    *,
    path: str,
    before: str,
    after: str,
    context_lines: int = 3,
) -> str:
    """
    渲染单文件 edit 的 unified diff HTML。

    Args:
        path           : 文件路径（用于标题展示）
        before / after : 编辑前后的全文
        context_lines  : unified diff 上下文行数

    Returns:
        完整 HTML 字符串。
    """
    # 行截断（超大文件）
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if len(before_lines) > _MAX_LINES or len(after_lines) > _MAX_LINES:
        before_lines = before_lines[:_MAX_LINES]
        after_lines = after_lines[:_MAX_LINES]
        truncated = True
    else:
        truncated = False

    diff_iter = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="before",
        tofile="after",
        lineterm="",
        n=context_lines,
    )
    rows, n_add, n_del = _build_rows(list(diff_iter))

    summary = (
        f'<div class="diff-summary">'
        f'<span><span class="add">+{n_add}</span> additions</span>'
        f'<span><span class="del">-{n_del}</span> deletions</span>'
    )
    if truncated:
        summary += '<span style="color:var(--warn)">[truncated to first 5000 lines]</span>'
    summary += '</div>'

    body = f"""
    <div class="panel">
      <h2>diff: {escape(path)}</h2>
      {summary}
      <table class="diff-table">
        <thead>
          <tr>
            <th style="width:50px"></th>
            <th>before / after (unified, {context_lines} lines context)</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows) if rows else '<tr><td></td><td><em style="color:var(--dim)">(no textual changes)</em></td></tr>'}
        </tbody>
      </table>
    </div>
    """
    title = f"edit diff · {Path(path).name}"
    return HtmlDoc(title=title, body=body, extra_css=_DIFF_CSS).to_string()


def _build_rows(lines: list[str]) -> tuple[list[str], int, int]:
    """
    把 unified_diff 的行切成 (类, 行号, 内容) 行 HTML。
    返回 (rows, n_additions, n_deletions)。
    """
    rows: list[str] = []
    n_add = n_del = 0
    line_no = 0
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            # diff 文件标题，跳过（标题已在面板上方显示）
            continue
        if line.startswith("@@"):
            line_no = _parse_hunk_start(line)
            rows.append(
                f'<tr class="hdr"><td class="ln">…</td>'
                f'<td class="code">{escape(line)}</td></tr>'
            )
            continue
        if line.startswith("+"):
            n_add += 1
            rows.append(
                f'<tr class="add"><td class="ln">{line_no}</td>'
                f'<td class="code">+ {escape(line[1:])}</td></tr>'
            )
            line_no += 1
        elif line.startswith("-"):
            n_del += 1
            rows.append(
                f'<tr class="del"><td class="ln">{line_no}</td>'
                f'<td class="code">- {escape(line[1:])}</td></tr>'
            )
            # 删除行不推进 after 行号
        else:
            rows.append(
                f'<tr class="ctx"><td class="ln">{line_no}</td>'
                f'<td class="code">  {escape(line[1:] if line.startswith(" ") else line)}</td></tr>'
            )
            line_no += 1
    return rows, n_add, n_del


def _parse_hunk_start(hunk: str) -> int:
    """从 '@@ -a,b +c,d @@' 拿 c。"""
    m = re.search(r"\+(\d+)", hunk)
    if not m:
        return 0
    return int(m.group(1))


# ───────────────── 文件名 / 路径 ─────────────────


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_artifact_name(path: str) -> str:
    """把文件路径转成安全的 artifact 文件名片段。"""
    base = Path(path).name or "edit"
    safe = _SAFE_NAME_RE.sub("_", base)
    return safe[:80]  # 防超长


def html_diff_enabled() -> bool:
    """读 env var 判断是否落盘 diff HTML。"""
    val = os.getenv("CODEMESH_HTML_DIFF", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def maybe_write_diff(
    *,
    path: str,
    before: str,
    after: str,
    target_dir: Optional[Path] = None,
    keep: int = 20,
) -> Optional[Path]:
    """
    可选钩子：env CODEMESH_HTML_DIFF=1 时把 diff 落盘到 .codemesh/diffs/。
    返回写入路径或 None。任何错误静默吞掉（不影响 edit_file 主流程）。

    安全约束：
      - before/after 任一超过 _MAX_BYTES 字节就跳过（不渲染巨文件）
      - 文件名 sanitize（防路径穿透）
    """
    if not html_diff_enabled():
        return None
    try:
        if len(before) > _MAX_BYTES or len(after) > _MAX_BYTES:
            return None
        # 二进制内容（含 NUL 字节）跳过
        if "\x00" in before or "\x00" in after:
            return None
        if target_dir is None:
            target_dir = Path.cwd() / ".codemesh" / "diffs"
        ts = time.strftime("%Y%m%d-%H%M%S")
        # 防同秒冲突：附微秒
        ms = f"{int((time.time() % 1) * 1000):03d}"
        filename = f"{ts}-{ms}-{safe_artifact_name(path)}.html"
        html = render_edit_diff(path=path, before=before, after=after)
        return write_artifact(
            target_dir=target_dir,
            filename=filename,
            html_text=html,
            keep=keep,
        )
    except Exception:
        # 锦上添花层失败不打断 edit_file
        return None


__all__ = [
    "render_edit_diff",
    "maybe_write_diff",
    "html_diff_enabled",
    "safe_artifact_name",
]
