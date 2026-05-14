"""
Pydantic schemas for web API.

----------------------------------------------------------------------
为什么独立成文件？
----------------------------------------------------------------------
FastAPI 用 Pydantic 类型做：
  1. 请求体自动验证（非法 payload → 422）
  2. 响应自动序列化（dataclass-like dict → JSON）
  3. OpenAPI doc 自动生成（http://localhost:8000/docs）

把所有 schema 放一处避免在 routes/*.py 里散落，方便前端
对照（前端 TS 类型可以从 OpenAPI 反向生成）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ─────────────── Models ───────────────

class ModelInfo(BaseModel):
    """单个模型 metadata。GET /api/models 返回 list of this。"""
    id: str             # "deepseek" / "qwen" / "doubao" / "gemini"
    name: str           # 显示名 "DeepSeek V4 Pro"
    configured: bool    # 对应 API key 是否已配（启发式：env var 长度 >= 20）
    color: str          # 品牌色 hex，前端按这个画头像 / 工具卡左边线


# ─────────────── Chat ───────────────

class ChatRequest(BaseModel):
    """POST /api/chat 请求体。"""
    task: str = Field(..., min_length=1, description="用户问题（一句话即可，Harness 内部维护历史）")
    model: Optional[str] = Field(None, description="可选指定模型 id；不传则走 router 决策")
    session_id: Optional[str] = Field(None, description="会话 id；Phase 1 占位，Phase 5 真接 SQLite")


class ChatResponse(BaseModel):
    """POST /api/chat 响应体。"""
    answer: str
    model: str          # 实际跑的模型 id（router 决策后的结果）
    duration_ms: int
    cost_rmb: float     # 这一次 run 的总成本（last_costs 累加）
    session_id: Optional[str] = None


# ─────────────── Sessions ───────────────

class SessionInfo(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    model: Optional[str] = None
    message_count: int = 0


class SessionCreateRequest(BaseModel):
    title: Optional[str] = Field(default="新对话", description="标题；不传默认'新对话'")
