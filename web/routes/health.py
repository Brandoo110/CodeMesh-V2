"""
Health check endpoint —— Phase 0 唯一的 endpoint。

用途：
1. 验证 FastAPI 装好 + uvicorn 跑得起来
2. 前端启动时 ping 一下确认后端在线（v2 可加）
3. 未来部署后 docker healthcheck 用
"""
from __future__ import annotations

from fastapi import APIRouter

from web import __version__

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """
    简单的健康检查。

    返回示例：
        {"status": "ok", "version": "0.1.0", "service": "codemesh-web"}
    """
    return {
        "status": "ok",
        "version": __version__,
        "service": "codemesh-web",
    }
