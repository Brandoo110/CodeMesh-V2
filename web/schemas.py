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


# ─────────────── Workflows (v5) ───────────────

class WorkflowInfo(BaseModel):
    """工作流元数据。GET /api/workflows 返回 list of this。"""
    id: str
    name: str
    description: str = ""
    is_template: bool = False
    created_at: datetime
    updated_at: datetime
    step_count: int = 0


class WorkflowCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)


class StepInfo(BaseModel):
    """单个步骤定义。"""
    id: str
    workflow_id: str
    step_order: int
    name: str
    model: Optional[str] = None
    system_prompt: str = ""
    user_prompt: str = ""
    enable_tools: list[str] = Field(default_factory=lambda: ["*"])


class StepCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    model: Optional[str] = None
    system_prompt: str = ""
    user_prompt: str = ""
    enable_tools: list[str] = Field(default_factory=lambda: ["*"])


class StepUpdateRequest(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    enable_tools: Optional[list[str]] = None


class StepReorderRequest(BaseModel):
    step_ids: list[str] = Field(..., description="按新顺序排列的 step id 列表")


class WorkflowDetail(WorkflowInfo):
    """GET /api/workflows/{id} 返回，含 steps。"""
    steps: list[StepInfo] = Field(default_factory=list)


# ─────────────── Workflow Runs (v5) ───────────────

class RunInfo(BaseModel):
    """单次执行元数据。"""
    id: str
    workflow_id: str
    status: str  # running / done / error / cancelled
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_cost_rmb: float = 0.0
    error: Optional[str] = None


class StepResultInfo(BaseModel):
    """单步执行结果。"""
    id: int
    run_id: str
    step_id: str
    step_order: int
    status: str
    output: Optional[str] = None
    error: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    file_diffs: Optional[list[dict]] = None
    model_used: Optional[str] = None
    cost_rmb: Optional[float] = None
    duration_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RunDetail(RunInfo):
    """GET /api/workflows/runs/{id} 返回，含 step_results。"""
    step_results: list[StepResultInfo] = Field(default_factory=list)
