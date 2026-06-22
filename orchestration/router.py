"""
路由器：用 PydanticAI 做强类型任务分流（Harness 编排层）
============================================================

【路由器做什么】
接收用户任务描述，返回一个结构化的决策：
  - 用哪个模型（deepseek / qwen / doubao）
  - 任务复杂度（simple / complex）
  - 选择理由（给日志用）

这个决策直接决定接下来 Agent Loop 用哪个适配器。

【为什么用 PydanticAI 而不是 LangGraph】
这是本项目最核心的设计选择，核心设计点。

PydanticAI 的定位：给 LLM 输出套上 Pydantic 强类型约束。
  → 你定义一个 output_type（这里是 RouteDecision），PydanticAI 帮你：
    1. 把类型 schema 转成 prompt 描述给模型
    2. 解析模型输出，验证字段类型
    3. 失败自动重试（让模型按格式重写）

LangGraph 的定位：构建有状态的多节点工作流（图）。
  → 适合「规划 → 执行 → 反思 → 再规划」这种有状态机的复杂 Agent。

CodeMesh 的路由是「单轮决策」：
  输入 = 任务字符串
  输出 = RouteDecision（结构化）
  不需要状态、不需要多节点协作。
  → PydanticAI 是天然匹配，启动开销小、代码量少。

简言之：
  - 单轮 + 强结构化 = PydanticAI
  - 多轮 + 有状态 + 图结构 = LangGraph
这就是两者设计哲学的核心差异，能讲清这一点就说明你真正理解这两个框架。

【为什么强类型重要】
路由决策进入下游代码后，下游会根据 decision.model 选适配器。
如果模型返回了 "chatgpt"（不在预设值里），或者返回了 "一个更智能的模型"
（自由文本），程序就 crash 或逻辑错乱。
强类型（Literal[...]）让这种错误在解析阶段就被拦截并自动重试。
"""

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider


# ───────────────── 输出 Schema ─────────────────


class RouteDecision(BaseModel):
    """
    路由决策的结构化输出。

    字段用 Literal[...] 约束 —— 模型只能从枚举中选一个。
    如果模型乱写，Pydantic 会在校验阶段报错，PydanticAI 自动让模型重试。
    """

    model: Literal["deepseek", "qwen", "doubao"] = Field(
        ...,
        description=(
            "选哪个模型: "
            "deepseek=复杂推理/debug/架构, "
            "qwen=代码生成/重构, "
            "doubao=简单问答/文档/解释"
        ),
    )
    complexity: Literal["simple", "complex"] = Field(
        ..., description="任务复杂度"
    )
    reason: str = Field(..., description="一句话说明为什么这么选")


# ───────────────── PydanticAI Agent ─────────────────

# 路由器自己也需要一个模型来做决策。用 DeepSeek 做路由（便宜 + 推理好）。
# 这里用 pydantic-ai 的 OpenAIModel，base_url 指向 DeepSeek 兼容端点。
def _build_router_agent() -> Agent[None, RouteDecision]:
    """
    构建 PydanticAI Agent。
    放到函数里是为了惰性初始化（import 时不立刻联网）。

    优先用 DeepSeek（国内合规默认）；没有 DEEPSEEK_API_KEY 就退到 Gemini，
    让只配了 Gemini key 的学习场景也能跑通。
    """
    ds_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    # 真实 key 远超 20 字符；低于这个长度一律当占位符看待
    if len(ds_key) >= 20:
        provider = OpenAIProvider(
            api_key=ds_key,
            base_url="https://api.deepseek.com/v1",
        )
        # 读 env 让 .env 切模型生效，和 adapter 一致；默认 V4 Pro
        model = OpenAIModel(
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"), provider=provider
        )
    elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        provider = OpenAIProvider(
            api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        model = OpenAIModel(
            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"), provider=provider
        )
    else:
        # 都没配，保底起一个 DeepSeek provider，在真正调用时会失败，由上层友好提示
        provider = OpenAIProvider(api_key="", base_url="https://api.deepseek.com/v1")
        model = OpenAIModel("deepseek-v4-pro", provider=provider)

    return Agent(
        model=model,
        output_type=RouteDecision,  # 关键：声明输出类型
        system_prompt=(
            "你是一个任务路由器。用户会给你一段任务描述，"
            "你需要判断用哪个国内模型处理最合适，并返回结构化 JSON。\n"
            "规则：\n"
            "- 涉及 debug、架构分析、逻辑推理 → deepseek（推理最强）\n"
            "- 涉及代码生成、重构、补全 → qwen（代码专精）\n"
            "- 简单问答、文档生成、概念解释 → doubao（快且便宜）\n"
            "只输出 JSON，不要解释。"
        ),
    )


# 模块级缓存，避免每次调用都重建
_router_agent: Agent[None, RouteDecision] | None = None


async def route(task: str) -> RouteDecision:
    """
    对外接口：输入任务描述，返回路由决策。
    """
    global _router_agent
    if _router_agent is None:
        _router_agent = _build_router_agent()

    # PydanticAI 的 run() 返回 AgentRunResult，output 字段是强类型对象
    result = await _router_agent.run(task)
    return result.output
