"""
GET /api/models —— 列出所有支持模型 + 是否配齐 key。

前端用这个：
  1. 渲染 ModelSelector dropdown
  2. 显示在线状态小圆点（✓ configured / ✗ 未配）
  3. 按 color 给每个模型分配品牌色
"""
from fastapi import APIRouter

from web.deps import list_models
from web.schemas import ModelInfo

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelInfo])
async def get_models() -> list[ModelInfo]:
    """
    返回 4 个支持的模型 + configured 状态。

    Configured = 对应 env var (DEEPSEEK_API_KEY / DASHSCOPE_API_KEY /
    VOLC_API_KEY / GEMINI_API_KEY) 值长度 >= 20 字符（过滤占位符）。
    """
    return [ModelInfo(**m) for m in list_models()]
