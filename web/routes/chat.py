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
from web.sessions_store import SessionsStore, get_sessions_store

router = APIRouter(prefix="/chat", tags=["chat"])

# 单例 init 标记
_store_initialized = False


async def _ensure_store_init(store: SessionsStore) -> None:
    global _store_initialized
    if not _store_initialized:
        await store.init()
        _store_initialized = True


async def _load_history_to_harness(harness: Harness, store: SessionsStore, sid: str) -> None:
    """从 DB 加载 session 历史灌 harness.short_term（清空再 add）。"""
    harness.short_term.clear()  # 保留 system message
    messages = await store.get_messages(sid)
    for m in messages:
        role = m["role"]
        # 只灌 user / assistant；前端的 error / system 不进 LLM context
        if role in ("user", "assistant") and m["content"]:
            harness.short_term.add(role, m["content"])


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    harness: Harness = Depends(get_harness),
    store: SessionsStore = Depends(get_sessions_store),
) -> ChatResponse:
    """
    主对话入口。

    Phase 5 加 session 持久化：
      - 有 session_id → 从 DB 加载历史灌 short_term，回答后保存新消息
      - 无 session_id → ephemeral，不持久化（保持 Phase 1 行为兼容）

    错误处理：
      - 422: task 为空（Pydantic min_length=1）
      - 404: session_id 不存在
      - 500: harness 跑挂（model API 超时 / key 未配）
    """
    # 1. 如果给了 session_id，校验存在 + 加载历史
    if req.session_id:
        await _ensure_store_init(store)
        sess = await store.get_session(req.session_id)
        if not sess:
            raise HTTPException(404, f"session {req.session_id} not found")
        await _load_history_to_harness(harness, store, req.session_id)
    else:
        # ephemeral chat：清空 short_term 但保留 system
        harness.short_term.clear()

    # 2. 跑 harness（如果前端选了模型，绕开 router，强制用选定模型）
    t0 = time.perf_counter()
    prev_pref = harness.preferred_model
    try:
        if req.model:
            harness.set_preferred_model(req.model)
        answer = await harness.run(req.task)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    finally:
        harness.set_preferred_model(prev_pref)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    costs = getattr(harness, "last_costs", []) or []
    total_cost = sum(getattr(c, "cost_rmb", 0.0) for c in costs)
    actual_model = (
        getattr(costs[0], "model", None) if costs else None
    ) or req.model or "auto"

    # 3. 持久化新消息
    if req.session_id:
        await store.append_message(req.session_id, "user", req.task)
        await store.append_message(
            req.session_id, "assistant", answer,
            model=actual_model,
            cost_rmb=round(total_cost, 4),
            duration_ms=duration_ms,
        )
        await store.update_session(req.session_id, model=actual_model)

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
    store: SessionsStore = Depends(get_sessions_store),
):
    """
    SSE 流式对话。Phase 5 加 session 加载/保存。

    协议（详见 harness.run_stream_full docstring）：
        event: token / tool_start / tool_end / usage / done / error

    Session 处理：
        - 有 session_id → 校验存在 + 加载历史；流末尾把答案 + tool calls 写回 DB
        - 无 session_id → ephemeral，不持久化
    """
    # 1. 加载历史 / 清空
    if req.session_id:
        await _ensure_store_init(store)
        sess = await store.get_session(req.session_id)
        if not sess:
            raise HTTPException(404, f"session {req.session_id} not found")
        await _load_history_to_harness(harness, store, req.session_id)
    else:
        harness.short_term.clear()

    # 2. 流式生成 + 同时累积答案/工具/用量用于落库
    # 如果前端选了模型，临时设 preferred_model 绕开 router；流结束 reset。
    prev_pref = harness.preferred_model
    if req.model:
        harness.set_preferred_model(req.model)

    async def event_generator():
        full_answer: list[str] = []
        tool_calls: list[dict] = []
        usage_data: dict = {}
        error_msg: str | None = None
        t0 = time.monotonic()

        try:
            async for event in harness.run_stream_full(req.task):
                etype = event.get("type", "token")
                edata = event.get("data", {})

                # 累积用于持久化
                if etype == "token":
                    full_answer.append(str(edata.get("delta", "")))
                elif etype == "tool_start":
                    tool_calls.append({
                        "name": edata.get("name", ""),
                        "args": edata.get("args", {}),
                        "status": "pending",
                    })
                elif etype == "tool_end":
                    # FIFO 配对 pending 同名工具更新结果
                    for tc in tool_calls:
                        if tc.get("name") == edata.get("name") and tc.get("status") == "pending":
                            tc["result"] = edata.get("result", "")
                            tc["ok"] = bool(edata.get("ok", True))
                            tc["status"] = "ok" if tc["ok"] else "error"
                            break
                elif etype == "usage":
                    usage_data = edata
                elif etype == "error":
                    error_msg = str(edata.get("message", ""))

                yield {
                    "event": etype,
                    "data": json.dumps(edata, ensure_ascii=False),
                }
        finally:
            # 流结束（含异常）reset preferred_model，避免污染下一个 chat
            harness.set_preferred_model(prev_pref)

        # 3. 持久化（流结束）
        if req.session_id and not error_msg:
            answer = "".join(full_answer)
            duration_ms = int((time.monotonic() - t0) * 1000)
            await store.append_message(req.session_id, "user", req.task)
            await store.append_message(
                req.session_id, "assistant", answer,
                tool_calls=tool_calls if tool_calls else None,
                model=str(usage_data.get("model", "")) or None,
                cost_rmb=float(usage_data.get("cost_rmb", 0)) or None,
                duration_ms=duration_ms,
            )
            await store.update_session(
                req.session_id,
                model=str(usage_data.get("model", "")) or None,
            )

    return EventSourceResponse(event_generator())
