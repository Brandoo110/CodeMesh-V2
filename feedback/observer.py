"""
可观测性：Langfuse 埋点（Harness 反馈层）
==============================================

【为什么反馈层是 Harness 的一等公民】
业界有个共识：没有 observability 的 Agent 等于没有 Agent。
因为 LLM 行为是非确定的 —— 同一输入可能不同输出。出问题时，
如果没有每次调用的完整 trace（prompt、参数、输出、耗时、成本），
根本没法 debug。

反馈层做两件事：
  1. 观察（observer.py）: 记录每次调用的 trace，方便事后分析
  2. 验证（validator.py）: 对输出做结构化校验，不合规就拒绝

【Langfuse 是什么】
一个开源的 LLM 可观测性平台，类似 Datadog 之于服务端。
功能：trace 记录、token 成本计算、对话重放、评估打分、提示词版本管理。
支持自托管（本地 docker 起）或用云服务（cloud.langfuse.com）。

【为什么选 Langfuse 而不是自己写日志】
  1. 可视化免费：控制台直接看 trace 树、成本趋势
  2. 生态兼容：支持 OpenAI、Anthropic、LangChain 等主流框架
  3. 社区活跃：开源，有人维护

【优雅降级】
未配置 Langfuse 时（环境变量为空），所有方法静默变成 no-op。
这样教学/演示场景不需要注册 Langfuse 也能跑通项目。
"""

import os
import uuid
from typing import Any


class Observer:
    """
    Langfuse 埋点封装。

    API 设计：
      start_trace(task)   -> trace_id
      log_llm_call(...)
      end_trace(success=True)
    """

    def __init__(self):
        # 尝试初始化 Langfuse 客户端。失败则进入 "no-op 模式"
        self.client = self._try_init()
        self._current_trace: Any = None

    @staticmethod
    def _try_init() -> Any | None:
        """
        只有公钥/私钥都配了才真正连 Langfuse。
        这种设计叫 feature flag：未配置就静默关闭该能力，不影响其他功能。
        """
        pub = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        sec = os.getenv("LANGFUSE_SECRET_KEY", "")
        if not (pub and sec):
            return None

        try:
            # 延迟 import：没装 langfuse 时整个模块仍能加载
            from langfuse import Langfuse
            return Langfuse(
                public_key=pub,
                secret_key=sec,
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception as e:
            print(f"[observer] Langfuse init failed: {e}. Switching to no-op.")
            return None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ─────────────── trace 生命周期 ───────────────

    def start_trace(self, task_desc: str) -> str:
        """
        开始一次追踪。一次用户请求 = 一个 trace，内部可嵌套多次 LLM 调用。
        """
        trace_id = str(uuid.uuid4())
        if not self.enabled:
            return trace_id

        # 不同 Langfuse 版本 API 略有差异，用 getattr 兼容
        try:
            self._current_trace = self.client.trace(  # type: ignore[union-attr]
                id=trace_id,
                name="codemesh.run",
                input={"task": task_desc},
            )
        except Exception as e:
            print(f"[observer] start_trace failed: {e}")
        return trace_id

    def log_llm_call(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        route_reason: str = "",
    ) -> None:
        """
        记录一次模型调用。
        """
        if not self.enabled or self._current_trace is None:
            return
        try:
            self._current_trace.generation(
                name=f"llm.{model}",
                model=model,
                usage={"input": tokens_in, "output": tokens_out},
                metadata={"latency_ms": latency_ms, "route_reason": route_reason},
            )
        except Exception as e:
            print(f"[observer] log_llm_call failed: {e}")

    def end_trace(self, success: bool = True, output: str = "") -> None:
        """结束 trace。Langfuse 要 flush 才能真正上报。"""
        if not self.enabled or self._current_trace is None:
            return
        try:
            self._current_trace.update(
                output={"success": success, "text": output[:500]}
            )
            self.client.flush()  # type: ignore[union-attr]
        except Exception as e:
            print(f"[observer] end_trace failed: {e}")
        finally:
            self._current_trace = None
