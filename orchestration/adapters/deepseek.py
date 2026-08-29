"""
DeepSeek 适配器
================

【DeepSeek 是什么】
深度求索（DeepSeek）是国内最强推理模型之一。它的 API 完全兼容 OpenAI 格式，
所以直接用 openai 这个 SDK 调就行，只需要改两个参数：
  - base_url: "https://api.deepseek.com/v1"
  - api_key:  自己的 DEEPSEEK_API_KEY

【模型选择】
  - deepseek-chat     ：通用对话（DeepSeek-V3）
  - deepseek-reasoner ：推理模型（DeepSeek-R1），返回里带 reasoning_content（慢思考）

【为什么要封装一层而不是直接用 openai SDK】
  1. 上层只依赖我们的 ModelAdapter 接口，不直接依赖 openai，换 SDK 不影响业务
  2. 可以在这一层统一处理错误、加超时、加重试
  3. 可以在这一层做 prompt 预处理

【usage 怎么拿】
- 非流式：resp.usage.prompt_tokens / completion_tokens，直接取
- 流式：需要在请求里加 stream_options={"include_usage": True}，最后一个 chunk 带 usage
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import Message, Usage
from .retry import async_retry, async_retry_stream


class DeepSeekAdapter:
    """DeepSeek 模型适配器（OpenAI 兼容协议）。"""

    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.deepseek.com/v1",
        json_mode: bool = False,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        # 优先看 env，方便 .env 切换；默认 V4 Pro
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        self.json_mode = json_mode
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)
        # 初始化为 0，调用后更新
        self.last_usage = Usage()

    def _build_messages(self, messages: list[Message], system: str) -> list[Message]:
        full: list[Message] = []
        if system:
            full.append({"role": "system", "content": system})
        full.extend(messages)
        return full

    async def complete(self, messages: list[Message], system: str = "") -> str:
        """
        非流式调用。包了一层 async_retry：429 / 5xx / 网络错误自动指数退避重试，
        401 / 400 等 deterministic 错误立刻抛。
        """
        full = self._build_messages(messages, system)
        request_kwargs = {
            "model": self.model,
            "messages": full,
            "temperature": 0 if self.json_mode else 0.3,
        }
        if self.json_mode:
            request_kwargs.update(
                {
                    "response_format": {"type": "json_object"},
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            )

        async def _call():
            return await self.client.chat.completions.create(**request_kwargs)

        resp = await async_retry(_call)
        # 记录 token 数，方便成本层算账
        if resp.usage:
            self.last_usage = Usage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )
        return resp.choices[0].message.content or ""

    async def complete_stream(
        self, messages: list[Message], system: str = ""
    ) -> AsyncIterator[str]:
        """
        流式调用。包了一层 async_retry_stream：半流断后重发，
        旧前缀 buffer 起来跳过，对下游消费者保持连续体验。
        """
        full = self._build_messages(messages, system)

        async def _factory() -> AsyncIterator[str]:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=full,  # type: ignore[arg-type]
                temperature=0.3,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content
                if chunk.usage:
                    self.last_usage = Usage(
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                    )

        async for c in async_retry_stream(_factory):
            yield c
