"""
FastAPI app 入口。

----------------------------------------------------------------------
怎么跑？
----------------------------------------------------------------------
开发：
    uvicorn web.server:app --reload --port 8000

生产（暂未做）：
    uvicorn web.server:app --host 0.0.0.0 --port 8000 --workers 1

注意 workers=1 —— CodeMesh 的 memory 7 层是单进程内状态，多 worker
会让不同请求看到不同的 memory，破坏 ADR-0004 的 L0-L6 语义。

----------------------------------------------------------------------
CORS 策略
----------------------------------------------------------------------
localhost 单用户场景（ADR-0006 部署目标），允许 Next.js 默认端口 3000。
后续 Phase 8 部署时按域名收紧。

----------------------------------------------------------------------
路由分层
----------------------------------------------------------------------
/api/health   —— 健康检查（Phase 0 唯一 endpoint）
/api/models   —— 模型列表（Phase 1）
/api/chat/*   —— 对话核心（Phase 1-3）
/api/sessions —— 历史会话（Phase 5）
/api/stats    —— Stats dashboard（Phase 4）
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from web import __version__
from web.routes import health


def create_app() -> FastAPI:
    """工厂函数 —— 方便测试时注入 dependencies。"""
    app = FastAPI(
        title="CodeMesh Web API",
        version=__version__,
        description="CodeMesh Web UI 后端 — FastAPI 直接复用 harness（详见 ADR-0006）",
    )

    # CORS：本地开发环境允许 Next.js dev server
    # 生产部署时按域名收紧（见 Phase 8）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由注册 —— Phase 0 只挂 health
    # Phase 1+ 会按需 include_router 加 chat / models / sessions / stats
    app.include_router(health.router, prefix="/api")

    return app


# Uvicorn 入口
app = create_app()
