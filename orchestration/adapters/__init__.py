"""
适配器聚合出口：方便上层写 `from orchestration.adapters import DeepSeekAdapter`。
"""
from .base import ModelAdapter, Message, Usage
from .deepseek import DeepSeekAdapter
from .dashscope import DashScopeAdapter
from .volcengine import VolcEngineAdapter
from .gemini import GeminiAdapter
from .minimax import MiniMaxAdapter

__all__ = [
    "ModelAdapter",
    "Message",
    "Usage",
    "DeepSeekAdapter",
    "DashScopeAdapter",
    "VolcEngineAdapter",
    "GeminiAdapter",
    "MiniMaxAdapter",
]
