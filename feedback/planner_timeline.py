"""
Planner timeline HTML 渲染（Harness 反馈层）
==============================================

【这模块解决什么】
complex 任务走 planner-executor 时，原本只在终端 print 形如：
    [planner] 重构 auth 模块
      step 1/3: [deepseek] 列出当前 auth 用法
      step 2/3: [qwen] 重写 auth.py
      step 3/3: [doubao] 跑测试

行号打印对面试 / demo 体验很弱：看不到拓扑、看不到每步实际成本、出错也不知道
卡哪一步。这模块基于 `feedback/render_html.py` 把 plan + 执行结果渲染成
单文件 HTML：步骤卡片、模型品牌色、状态徽章、成本 / 时长、输出预览。

【触发】
env `CODEMESH_HTML_PLAN=1` 时，每次跑 complex 任务结束后自动写到
`.codemesh/plans/<ts>.html`，按 mtime 滚动保留 20 个。

【面试点】
"Q: 为什么不直接对接 LangGraph 的可视化？"
→ LangGraph 的图状态在节点间流转，可视化要 D3 / mermaid runtime；本项目的
  planner 是顺序执行 + 共享 short_term，渲染需求是"每步是什么 + 状态 + 成本"，
  一段 HTML 模板就够。和 router/planner 的 PydanticAI 类型化输出搭得上。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .render_html import HtmlDoc, escape, model_color, write_artifact


_TIMELINE_CSS = """
.tl-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--dim);
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 20px;
}
.tl-meta .label { color: var(--dim); margin-right: 4px; }
.tl-meta .value { color: var(--text); }

.tl-bar {
  display: flex;
  height: 28px;
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid var(--border);
  margin: 12px 0 24px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
}
.tl-bar .seg {
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255,255,255,0.85);
  border-right: 1px solid rgba(0,0,0,0.4);
  padding: 0 6px;
  white-space: nowrap;
  overflow: hidden;
}
.tl-bar .seg:last-child { border-right: none; }

.step {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 18px;
  margin: 12px 0;
  position: relative;
  border-left: 4px solid transparent;
}
.step.done { border-left-color: var(--good); }
.step.error { border-left-color: var(--bad); }
.step.pending { border-left-color: var(--dim); }

.step-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 4px;
}
.step-head .n {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--dim);
}
.step-head .desc {
  flex: 1;
  font-size: 14px;
  color: var(--text);
}
.step-head .status {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 3px;
  background: rgba(255,255,255,0.06);
}
.step.done .status { background: rgba(16,185,129,0.18); color: #d1fae5; }
.step.error .status { background: rgba(239,68,68,0.18); color: #fee2e2; }

.step-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--dim);
  margin-top: 8px;
}
.step-meta .model-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(255,255,255,0.04);
  color: var(--text);
}
.step-meta .model-tag .swatch {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.step-output {
  margin-top: 10px;
}
.step-output summary {
  cursor: pointer;
  color: var(--dim);
  font-size: 11px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  padding: 4px 0;
  user-select: none;
}
.step-output summary:hover { color: var(--text); }
.step-output pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 10px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 6px 0 0;
  max-height: 360px;
  overflow: auto;
}
"""


@dataclass
class StepRecord:
    """一步执行的全部观察数据。给 timeline 渲染。"""

    n: int
    description: str
    suggested_model: str
    needs_tools: bool
    status: str = "pending"  # done / error / pending
    output: str = ""
    duration_ms: float = 0.0
    cost_rmb: float = 0.0
    error: str = ""


def render_planner_timeline(
    *,
    task: str,
    summary: str,
    steps: Sequence[StepRecord],
) -> str:
    """渲染 planner 时间线 HTML。"""
    total_cost = sum(s.cost_rmb for s in steps)
    total_ms = sum(s.duration_ms for s in steps)
    n_done = sum(1 for s in steps if s.status == "done")
    n_err = sum(1 for s in steps if s.status == "error")

    parts: list[str] = []
    parts.append(_meta_row(task, summary, len(steps), n_done, n_err, total_cost, total_ms))
    parts.append(_proportion_bar(steps, total_ms))
    for s in steps:
        parts.append(_step_card(s))

    title = f"plan · {summary[:60]}" if summary else "plan timeline"
    return HtmlDoc(
        title=title,
        body='<div class="panel">' + "\n".join(parts) + "</div>",
        extra_css=_TIMELINE_CSS,
    ).to_string()


def _meta_row(task, summary, n_steps, n_done, n_err, cost, ms) -> str:
    bits = [
        ("task", escape(task[:200])),
        ("plan summary", escape(summary[:200])),
        ("steps", f"{n_done}/{n_steps} done · {n_err} error"),
        ("cost", f"¥{cost:.4f}"),
        ("duration", f"{ms / 1000:.1f}s"),
    ]
    items = []
    for label, value in bits:
        items.append(
            f'<div><span class="label">{escape(label)}</span>'
            f'<span class="value">{value}</span></div>'
        )
    return f'<div class="tl-meta">{"".join(items)}</div>'


def _proportion_bar(steps: Sequence[StepRecord], total_ms: float) -> str:
    """按耗时占比的横条，每段一个步骤；total=0 时退化为均分。"""
    if not steps:
        return ""
    if total_ms <= 0:
        # 平均分
        share = 100.0 / len(steps)
        segments = [(s, share) for s in steps]
    else:
        segments = [(s, max(2.0, s.duration_ms / total_ms * 100)) for s in steps]
    bits = []
    for s, w in segments:
        c = model_color(s.suggested_model)
        sub = f"step {s.n}"
        if s.duration_ms > 0:
            sub += f" · {s.duration_ms / 1000:.1f}s"
        bits.append(
            f'<div class="seg" style="background:{c}; flex-basis:{w:.1f}%; flex-grow:0;">'
            f'{escape(sub)}</div>'
        )
    return f'<div class="tl-bar">{"".join(bits)}</div>'


def _step_card(s: StepRecord) -> str:
    """单步卡片：标题 + meta + 输出折叠。"""
    klass = "step "
    klass += s.status if s.status in ("done", "error", "pending") else "pending"
    color = model_color(s.suggested_model)
    tools_label = "tools" if s.needs_tools else "no-tools"

    meta_bits = [
        f'<span class="model-tag"><span class="swatch" style="background:{color}"></span>{escape(s.suggested_model)}</span>',
        f'<span>{escape(tools_label)}</span>',
    ]
    if s.duration_ms > 0:
        meta_bits.append(f'<span>{s.duration_ms / 1000:.2f}s</span>')
    if s.cost_rmb > 0:
        meta_bits.append(f'<span>¥{s.cost_rmb:.4f}</span>')
    if s.error:
        meta_bits.append(
            f'<span style="color:var(--bad)">err: {escape(s.error[:120])}</span>'
        )

    body_extra = ""
    if s.output:
        body_extra = f"""
        <details class="step-output">
          <summary>output ({len(s.output):,} chars) ▾</summary>
          <pre>{escape(s.output[:5000])}</pre>
        </details>
        """

    status_label = s.status if s.status in ("done", "error", "pending") else "pending"

    return f"""
    <div class="{klass}">
      <div class="step-head">
        <span class="n">step {s.n}</span>
        <span class="desc">{escape(s.description)}</span>
        <span class="status">{escape(status_label)}</span>
      </div>
      <div class="step-meta">{''.join(meta_bits)}</div>
      {body_extra}
    </div>
    """


# ───────────────── env 控制 + 落盘 ─────────────────


def html_plan_enabled() -> bool:
    """读 env 判断是否落盘 timeline HTML。"""
    val = os.getenv("CODEMESH_HTML_PLAN", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def maybe_write_plan(
    *,
    task: str,
    summary: str,
    steps: Sequence[StepRecord],
    target_dir: Optional[Path] = None,
    keep: int = 20,
) -> Optional[Path]:
    """
    可选钩子：env 开时把 plan timeline 落盘。任何错误静默吞掉。
    返回写入路径或 None。
    """
    if not html_plan_enabled():
        return None
    try:
        if target_dir is None:
            target_dir = Path.cwd() / ".codemesh" / "plans"
        ts = time.strftime("%Y%m%d-%H%M%S")
        ms = f"{int((time.time() % 1) * 1000):03d}"
        filename = f"{ts}-{ms}-plan.html"
        html = render_planner_timeline(task=task, summary=summary, steps=steps)
        return write_artifact(
            target_dir=target_dir,
            filename=filename,
            html_text=html,
            keep=keep,
        )
    except Exception:
        return None


__all__ = [
    "StepRecord",
    "render_planner_timeline",
    "html_plan_enabled",
    "maybe_write_plan",
]
