"""
MiniMax 适配器
================

【MiniMax 是什么】
MiniMax（稀宇科技）的大模型，国产模型里偏代码 / agentic 场景能力较强。
API 完全兼容 OpenAI 格式，所以直接用 openai 这个 SDK 就行：
  - base_url: "https://api.minimax.chat/v1"
  - api_key:  自己的 MINIMAX_API_KEY

【模型选择】
  - MiniMax-M2.7 (默认)   ：最新代际，代码 / 推理增强
  - MiniMax-M1, abab6.5s  ：旧代际，已被 M2.x 取代

【为什么独立一个 adapter】
  虽然 API 是 OpenAI 兼容的，但 base_url / API key env / 错误码细节都不同。
  独立 adapter 让上层的 Tool Registry / Hooks 可以基于 self.name 做区分
  （比如 stats 按 model 维度归类、render_html 给每家分一个品牌色）。

【.env 配置】
  必需：MINIMAX_API_KEY=...
  可选：MINIMAX_MODEL=MiniMax-M2.7   # 不配走默认
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import Message, Usage
from .retry import async_retry, async_retry_stream


class MiniMaxAdapter:
    """MiniMax 模型适配器（OpenAI 兼容协议）。"""

    name = "minimax"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://api.minimax.chat/v1",
    ):
        self.api_key = api_key or os.getenv("MINIMAX_API_KEY", "")
        # 优先看 env，方便 .env 切换；默认 M2.7（用户指定的最新代际）
        self.model = model or os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)
        self.last_usage = Usage()

    def _build_messages(self, messages: list[Message], system: str) -> list[Message]:
        full: list[Message] = []
        if system:
            full.append({"role": "system", "content": system})
        full.extend(messages)
        return full

    async def complete(self, messages: list[Message], system: str = "") -> str:
        """非流式调用，async_retry 包装。"""
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
        """流式调用，async_retry_stream 包装。"""
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
