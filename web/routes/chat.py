"""
POST /api/chat —— 非流式对话入口（Phase 1）。

----------------------------------------------------------------------
为什么非流式？
----------------------------------------------------------------------
Phase 1 先把"调通 harness.run + 返回完整答案"这条最短路跑通。
Phase 3 会加 /api/chat/stream 走 SSE（async generator + EventSourceResponse）。

----------------------------------------------------------------------
Harness.run 的 contract
----------------------------------------------------------------------
- 接受单个 task: str（不是 messages 数组）
- Harness 维护自己的 short_term，多轮历史在实例内部累加
- 返回完整答案字符串
- 副作用：harness.last_costs 累加这次 run 的所有 CallCost

Phase 1 单 Harness 单例 = 单用户 localhost 场景，所有 chat 调用共享
同一份对话历史（符合 ADR-0006 部署约束）。
"""
import time

from fastapi import APIRouter, Depends, HTTPException

from harness import Harness
from web.deps import get_harness
from web.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    harness: Harness = Depends(get_harness),
) -> ChatResponse:
    """
    主对话入口。直接 await harness.run(task)，等完整答案。

    错误处理：
      - 422: task 为空（Pydantic min_length=1 自动）
      - 500: harness 跑挂了（model API 超时 / key 未配 / 工具异常）
    """
    t0 = time.perf_counter()
    try:
        answer = await harness.run(req.task)
    except Exception as e:
        # 错误文本化（harness 内部的工具异常已经被 ToolRegistry 转字符串，
        # 这里捕获的是路由 / model API / adapter 层的 fatal error）
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    duration_ms = int((time.perf_counter() - t0) * 1000)

    # 从 last_costs 推断实际跑的模型 + 累加成本
    # last_costs 是 list[CallCost]，每次 LLM call 一条
    costs = getattr(harness, "last_costs", []) or []
    total_cost = sum(getattr(c, "cost_rmb", 0.0) for c in costs)
    actual_model = (
        getattr(costs[0], "model", None) if costs else None
    ) or req.model or "auto"

    return ChatResponse(
        answer=answer,
        model=actual_model,
        duration_ms=duration_ms,
        cost_rmb=round(total_cost, 4),
        session_id=req.session_id,
    )
