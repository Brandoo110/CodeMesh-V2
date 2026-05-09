"""反馈层聚合出口。"""
from .observer import Observer
from .validator import check_no_secrets, check_path_safe
from .cost import compute_cost, CallCost, ModelPricing, PRICING
from .call_log import log_call, read_calls, aggregate, LOG_PATH
from .token_budget import count_tokens, truncate_to_budget, using_tiktoken
# 旧名 dreamer 改名 session_journal（per-session 叙事日志，CodeMesh 独有的 L5 变体）。
# 新 dreamer.py 是真 L6 dreaming（4 阶段巩固，CC 同款语义）。
# 兼容老导入：feedback.Dreamer 仍可用（指向 SessionJournal）；新代码用 RealDreamer。
from .session_journal import (
    Dreamer,                # alias 旧名，等价 SessionJournal
    SessionJournal,
    DreamHit,
    DEFAULT_DREAMS_DIR,     # alias 旧名
    DEFAULT_JOURNAL_DIR,
)
# 真 L6 dreamer（4 阶段巩固）
from .dreamer import (
    Dreamer as RealDreamer,
    ConsolidationPlan,
    ExistingMemory,
    parse_consolidation_plan,
    rebuild_memory_index,
)
from .compactor import (
    AutoCompactState,
    auto_compact_if_needed,
    compact_conversation,
    microcompact_messages,
    should_autocompact,
    estimate_messages_tokens,
)
# HTML 工件渲染基建（2026-05-10）：把已有数据换个展示形态——给人看的工件。
from .render_html import (
    HtmlDoc,
    BarDatum,
    PieSlice,
    horizontal_bar_chart,
    sparkline,
    pie_chart,
    write_artifact,
    rotate_dir,
    model_color,
)
from .stats_report import render_stats_dashboard
from .diff_report import (
    render_edit_diff,
    maybe_write_diff,
    html_diff_enabled,
)
from .planner_timeline import (
    StepRecord,
    render_planner_timeline,
    maybe_write_plan,
    html_plan_enabled,
)

__all__ = [
    "Observer",
    "check_no_secrets",
    "check_path_safe",
    "compute_cost",
    "CallCost",
    "ModelPricing",
    "PRICING",
    "log_call",
    "read_calls",
    "aggregate",
    "LOG_PATH",
    "count_tokens",
    "truncate_to_budget",
    "using_tiktoken",
    "Dreamer",
    "SessionJournal",
    "DreamHit",
    "DEFAULT_DREAMS_DIR",
    "DEFAULT_JOURNAL_DIR",
    "RealDreamer",
    "ConsolidationPlan",
    "ExistingMemory",
    "parse_consolidation_plan",
    "rebuild_memory_index",
    "AutoCompactState",
    "auto_compact_if_needed",
    "compact_conversation",
    "microcompact_messages",
    "should_autocompact",
    "estimate_messages_tokens",
    "HtmlDoc",
    "BarDatum",
    "PieSlice",
    "horizontal_bar_chart",
    "sparkline",
    "pie_chart",
    "write_artifact",
    "rotate_dir",
    "model_color",
    "render_stats_dashboard",
    "render_edit_diff",
    "maybe_write_diff",
    "html_diff_enabled",
    "StepRecord",
    "render_planner_timeline",
    "maybe_write_plan",
    "html_plan_enabled",
]
