"""
依赖注入工厂。

----------------------------------------------------------------------
为什么不直接 `from harness import Harness; harness = Harness()`？
----------------------------------------------------------------------
FastAPI 的 `Depends` 模式让测试时可以用 `app.dependency_overrides`
覆盖单例，不污染全局状态。这是 FastAPI 官方推荐的可测试性模式。

----------------------------------------------------------------------
为什么是单例（lru_cache）？
----------------------------------------------------------------------
Phase 1 是 localhost 单用户场景（ADR-0006）。Harness 内部维护 memory
7 层 + hooks + permissions + plugins，每次新建代价高。lru_cache 让整个
进程只 init 一次，符合 ADR-0004 的 "memory 7 层是进程内状态" 边界。

多用户场景需要按 session 隔离（Phase 5+ 重构）。
"""
from __future__ import annotations

import os
from functools import lru_cache

from harness import Harness


@lru_cache(maxsize=1)
def get_harness() -> Harness:
    """全局 Harness 单例。FastAPI Depends 入口。"""
    return Harness()


# Model 配置侦测：复用 harness._get_adapter 里的 key 校验逻辑
# 这里独立一份方便 /api/models 端 expose 状态（不调用 _get_adapter 私有方法）
_NATIVE_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen":     "DASHSCOPE_API_KEY",
    "doubao":   "VOLC_API_KEY",
    "gemini":   "GEMINI_API_KEY",
}

# 模型展示 metadata（颜色和 feedback/render_html.py 的 MODEL_COLORS 对齐）
_MODEL_META = {
    "deepseek": {"name": "DeepSeek V4 Pro", "color": "#5b8def"},
    "qwen":     {"name": "Qwen 3 Max",      "color": "#7c3aed"},
    "doubao":   {"name": "Doubao Pro",      "color": "#ef4444"},
    "gemini":   {"name": "Gemini 2.5 Pro",  "color": "#10b981"},
}


def is_configured(model_id: str) -> bool:
    """
    判断模型的 API key 是否已配。

    启发式：env var >= 20 字符。和 harness._get_adapter 的 `_valid()` 一致，
    过滤常见占位符（"YOUR_KEY_HERE" 这种）。
    """
    env_name = _NATIVE_KEY_ENV.get(model_id)
    if not env_name:
        return False
    return len(os.getenv(env_name, "").strip()) >= 20


def list_models() -> list[dict]:
    """返回所有支持模型的 metadata（含 configured 状态）。"""
    return [
        {
            "id": model_id,
            "name": meta["name"],
            "configured": is_configured(model_id),
            "color": meta["color"],
        }
        for model_id, meta in _MODEL_META.items()
    ]
