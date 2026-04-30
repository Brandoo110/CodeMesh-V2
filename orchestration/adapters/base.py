"""
模型适配器基础接口（Harness 编排层 - 适配器子模块）
=======================================================

【这一层在 Harness 中的位置】
编排层（Orchestration）负责「把任务分发给合适的模型」。
不同厂商（DeepSeek / DashScope / VolcEngine）的 API 差异很大：
  - 参数名不一样（max_tokens vs max_new_tokens）
  - 返回结构不一样（choices[0].message vs output.text）
  - 鉴权方式不一样（Bearer vs signature）

如果上层代码直接调厂商 SDK，会出现大量 if-else，换模型时改动极大。
所以我们引入「适配器模式」：用一个统一接口 ModelAdapter 把三家抹平。

【为什么用 Protocol 而不是抽象基类 ABC】
Python 的 Protocol（PEP 544）是「结构化子类型」，只要一个类长得像 Protocol
（有相同签名的方法），就自动被视为实现了 Protocol，不需要显式继承。
这样：
  1. 解耦：具体适配器不需要 import 这个文件
  2. 易测：测试时用 MockAdapter 不需要继承任何东西

【接口演进记录】
- v0: complete(messages, system) -> str
- v1: + complete_stream() 异步生成器，支持流式输出
- v1: + last_usage 属性，暴露最近一次调用的 token 统计（给成本追踪用）

这里的设计选择：
- 不把 usage 放进 complete 的返回值里（那样要改 str → 复合类型，破坏已有代码）
- 改成"调完之后通过 last_usage 属性去取"，副作用模式
  优点：兼容旧代码；缺点：非线程安全（同一 adapter 并发调会串）
  CodeMesh 的并发场景是"三个 adapter 各自并发"，同一 adapter 不会并发，所以没问题
"""

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


Message = dict[str, str]


@dataclass
class Usage:
    """一次模型调用的 token 统计。适配器从响应中提取并暴露给反馈层。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class ModelAdapter(Protocol):
    """
    模型适配器统一接口。

    所有厂商适配器都要实现这个接口。
    """

    name: str
    # 最近一次调用的 token 统计（调 complete / complete_stream 后更新）
    last_usage: Usage

    async def complete(
        self,
        messages: list[Message],
        system: str = "",
    ) -> str:
        """
        调用模型生成文本，返回完整字符串。
        调用完成后 self.last_usage 会被更新。
        """
        ...

    async def complete_stream(
        self,
        messages: list[Message],
        system: str = "",
    ) -> AsyncIterator[str]:
        """
        流式生成：逐 token 返回文本片段。

        用 async generator 实现 —— 调用方写:
            async for chunk in adapter.complete_stream(...):
                print(chunk, end="", flush=True)

        流结束后 self.last_usage 会被更新。
        """
        ...
