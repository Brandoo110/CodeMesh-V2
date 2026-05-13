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
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

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


# ─────────────── Phase 3: SSE streaming ───────────────


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    harness: Harness = Depends(get_harness),
):
    """
    SSE 流式对话。

    协议（详见 harness.run_stream_full docstring）：
        event: token       data: {"delta": "..."}
        event: tool_start  data: {"name": "...", "args": {...}}
        event: tool_end    data: {"name": "...", "result": "...", "ok": true}
        event: usage       data: {"prompt": int, "completion": int, "cost_rmb": float, "model": str}
        event: done        data: {}
        event: error       data: {"message": "..."}

    为啥 POST 不 GET？
        - GET 走 URL query 拿 task，长 task 容易超 URL 长度限制
        - 浏览器原生 EventSource 只支持 GET，所以前端必须用 fetch + ReadableStream
          手动解析 SSE 帧（lib/sse.ts 里做）

    错误处理：
        - harness 抛异常 → run_stream_full 内部 yield error event
        - HTTP status 永远 200（SSE 流一旦开始就回不到非 200），错误用 event 传

    Phase 3 不做 disconnect cancel —— 客户端关 tab 时 harness 继续跑完
    （单用户场景多按一下退出无伤大雅；Phase 8 部署时再加 request.is_disconnected）
    """
    async def event_generator():
        async for event in harness.run_stream_full(req.task):
            yield {
                "event": event.get("type", "token"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
