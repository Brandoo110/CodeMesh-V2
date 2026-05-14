"""
Sessions CRUD + Messages 读取（Phase 5：SQLite 真接）。

Phase 1 用内存 dict，Phase 5 替换为 SessionsStore（aiosqlite）。
存储路径：~/.codemesh/web_sessions.db
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from web.schemas import SessionCreateRequest, SessionInfo
from web.sessions_store import SessionsStore, get_sessions_store

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 单例 init 标记，避免每次请求重复建表
_initialized = False


async def _ensure_init(store: SessionsStore) -> None:
    global _initialized
    if not _initialized:
        await store.init()
        _initialized = True


@router.post("", response_model=SessionInfo)
async def create_session(
    req: SessionCreateRequest,
    store: SessionsStore = Depends(get_sessions_store),
) -> SessionInfo:
    """新建会话。"""
    await _ensure_init(store)
    data = await store.create_session(req.title or "新对话")
    return SessionInfo(**data)


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    store: SessionsStore = Depends(get_sessions_store),
) -> list[SessionInfo]:
    """列出全部会话，按 updated_at desc。"""
    await _ensure_init(store)
    rows = await store.list_sessions()
    return [SessionInfo(**r) for r in rows]


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(
    session_id: str,
    store: SessionsStore = Depends(get_sessions_store),
) -> SessionInfo:
    """单个会话详情。"""
    await _ensure_init(store)
    data = await store.get_session(session_id)
    if not data:
        raise HTTPException(404, f"session {session_id} not found")
    return SessionInfo(**data)


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    store: SessionsStore = Depends(get_sessions_store),
) -> dict[str, str]:
    """删除会话 + 关联消息。"""
    await _ensure_init(store)
    ok = await store.delete_session(session_id)
    if not ok:
        raise HTTPException(404, f"session {session_id} not found")
    return {"deleted": session_id}


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str,
    store: SessionsStore = Depends(get_sessions_store),
) -> list[dict[str, Any]]:
    """拿 session 全部消息（包含 toolCalls JSON 反序列化）。"""
    await _ensure_init(store)
    sess = await store.get_session(session_id)
    if not sess:
        raise HTTPException(404, f"session {session_id} not found")
    return await store.get_messages(session_id)
