"""
Gemini 适配器（Google）
========================

【为什么 CodeMesh 支持 Gemini】
CodeMesh 定位是"国内合规场景"，主力是 DeepSeek / Qwen / Doubao。
但在学习和 demo 阶段，开发者可能手上只有一个 Gemini 的 key（免费额度大、容易拿）。
提供 Gemini 适配器的意义：
  1. 学习 / 演示时只需一个 key 就能跑通四层架构
  2. 上层 adapter 接口不变，验证了"OpenAI 兼容协议"的可移植性
  3. 真正上生产时把 Gemini 摘掉即可（不影响境内合规）

【接入方式】
Gemini 官方提供 OpenAI 兼容端点：
  base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
  api_key  = GEMINI_API_KEY
调用方式和 DeepSeek / 其他 OpenAI 兼容家完全一致。

【模型选择】
默认 gemini-2.5-flash（便宜、快、够用）。可以通过 GEMINI_MODEL 环境变量覆盖。
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import Message, Usage


class GeminiAdapter:
    """Google Gemini 适配器（OpenAI 兼容端点）。"""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
    ):
        # GEMINI_API_KEY 和 GOOGLE_API_KEY 两个环境变量官方文档都在用，两个都认
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=full,  # type: ignore[arg-type]
            temperature=0.3,
        )
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
