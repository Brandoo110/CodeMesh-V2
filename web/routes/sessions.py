"""
Sessions CRUD —— 会话级元数据管理（Phase 1 内存占位）。

----------------------------------------------------------------------
Phase 1 vs Phase 5
----------------------------------------------------------------------
Phase 1（现在）：纯内存 dict，进程重启就丢
Phase 5（计划）：换 SQLite，复用 memory/long_term.py 的存储层

这种"先占位后落库"的渐进策略让前端可以现在就开发 sidebar 历史列表，
不卡在后端持久化设计上。

----------------------------------------------------------------------
SessionInfo 字段含义
----------------------------------------------------------------------
- id: uuid4 字符串
- title: 用户给的对话标题（侧栏显示，可后续 inline 编辑）
- created_at / updated_at: ISO datetime
- model: 主要使用的模型（最近一次 chat 的 model）
- message_count: Phase 5 会接到真实计数
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from web.schemas import SessionCreateRequest, SessionInfo

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Phase 1 临时存储：进程内 dict
# Phase 5 改 SQLite + 复用 memory/long_term.py 存储层
_SESSIONS: dict[str, SessionInfo] = {}


def _now() -> datetime:
    """UTC datetime（timezone-aware，避免 deprecation warning）。"""
    return datetime.now(timezone.utc)


@router.post("", response_model=SessionInfo)
async def create_session(req: SessionCreateRequest) -> SessionInfo:
    """新建会话，返回 uuid4 字符串作为 id。"""
    sid = str(uuid.uuid4())
    now = _now()
    session = SessionInfo(
        id=sid,
        title=req.title or "新对话",
        created_at=now,
        updated_at=now,
        message_count=0,
    )
    _SESSIONS[sid] = session
    return session


@router.get("", response_model=list[SessionInfo])
async def list_sessions() -> list[SessionInfo]:
    """列出所有会话，按 updated_at 倒序（最新在上）。"""
    return sorted(
        _SESSIONS.values(),
        key=lambda s: s.updated_at,
        reverse=True,
    )


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str) -> SessionInfo:
    """拿单个会话详情。404 if 不存在。"""
    if session_id not in _SESSIONS:
        raise HTTPException(404, f"session {session_id} not found")
    return _SESSIONS[session_id]


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """删除会话。404 if 不存在。"""
    if session_id not in _SESSIONS:
        raise HTTPException(404, f"session {session_id} not found")
    del _SESSIONS[session_id]
    return {"deleted": session_id}
