"""
VolcEngine（火山引擎豆包）适配器
==================================

【VolcEngine 是什么】
字节跳动旗下的云平台，豆包（Doubao）模型就部署在这里。
响应速度快，成本极低，适合「简单问答 / 文档生成」类任务。

【和 Qwen、DeepSeek 的差异】
  - DeepSeek R1：推理强但慢，适合复杂 debug、架构分析
  - Qwen-Coder ：中等速度，专精代码
  - Doubao     ：最快最便宜，简单任务性价比最高

【接入方式】
VolcEngine 的方舟（Ark）平台提供 OpenAI-compatible 端点：
  base_url = https://ark.cn-beijing.volces.com/api/v3

注意：VolcEngine 的「model」参数要填「接入点 ID」（endpoint id，类似 ep-xxx），
不是模型名本身。需要先在控制台建接入点。
"""

import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from .base import Message, Usage


class VolcEngineAdapter:
    """火山引擎豆包适配器。"""

    name = "doubao"

    def __init__(
        self,
        api_key: str | None = None,
        endpoint_id: str | None = None,
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
    ):
        self.api_key = api_key or os.getenv("VOLC_API_KEY", "")
        self.model = endpoint_id or os.getenv("DOUBAO_ENDPOINT_ID", "")
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
