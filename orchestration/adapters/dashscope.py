"""
DashScope（阿里云百炼）适配器
===============================

【DashScope 是什么】
阿里云的模型服务平台。路径上叫「通义千问」。
我们用 `qwen-coder-turbo` —— 专门针对代码任务微调的版本。

【为什么选 Qwen 做代码生成】
  - 针对代码任务有专门微调（qwen-coder 系列）
  - 中文理解好，能准确执行中文 prompt
  - 价格比 GPT-4 便宜一个数量级
  - 数据不出境，国内合规场景必须用

【API 调法】
DashScope 有两种接入方式：原生 SDK / OpenAI-compatible 端点。
为保持代码一致，用 OpenAI-compatible：
  base_url = https://dashscope.aliyuncs.com/compatible-mode/v1

这体现了适配器模式的好处：选了"共同的最小子集"（OpenAI 协议），三家代码几乎相同。
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import Message, Usage
from .retry import async_retry, async_retry_stream


class DashScopeAdapter:
    """阿里 DashScope / Qwen 适配器。"""

    name = "qwen"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "qwen-coder-turbo",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.model = model
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)
        self.last_usage = Usage()

    def _build_messages(self, messages: list[Message], system: str) -> list[Message]:
        full: list[Message] = []
        if system:
            full.append({"role": "system", "content": system})
        full.extend(messages)
        return full

    async def complete(self, messages: list[Message], system: str = "") -> str:
        full = self._build_messages(messages, system)
        async def _call():
            return await self.client.chat.completions.create(
                model=self.model,
                messages=full,  # type: ignore[arg-type]
                temperature=0.3,
            )
        resp = await async_retry(_call)
        if resp.usage:
            self.last_usage = Usage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )
        return resp.choices[0].message.content or ""

    async def complete_stream(
        self, messages: list[Message], system: str = ""
    ) -> AsyncIterator[str]:
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
