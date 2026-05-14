"""
GET /api/stats — Stats Dashboard HTML（Phase 4）。

直接返回 feedback/stats_report.py 渲染的完整 HTML 字符串，前端用 iframe 嵌入。
零适配——v4 HTML 工件的复用。
"""
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from feedback.call_log import LOG_PATH, aggregate, read_calls
from feedback.stats_report import render_stats_dashboard

router = APIRouter(prefix="/stats", tags=["stats"])


def _parse_range(s: str) -> float | None:
    """7d / 30d / 90d → float；all / "" → None（全部历史）"""
    if not s or s.lower() == "all":
        return None
    s = s.strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse)
async def stats_html(
    time_range: str = Query("30d", alias="range", description="窗口：7d / 30d / 90d / all"),
):
    """返回 stats dashboard HTML（暗色主题，自包含）。"""
    since_days = _parse_range(time_range)
    records = read_calls(since_days=since_days)
    by_model = aggregate(records) if records else {}
    html_text = render_stats_dashboard(
        records=records,
        by_model=by_model,
        days_window=since_days,
        log_path=str(LOG_PATH),
    )
    return HTMLResponse(content=html_text)
