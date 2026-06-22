"""
Planner：任务拆分（Harness 编排层）
=====================================

【Planner-Executor 模式】
Agent 社区里一个成熟的模式：
  - Planner Agent  ：只负责"拆任务"—— 把"重构这个模块"拆成 ["读文件", "找用法", "写新版本", "跑测试"]
  - Executor Agent ：每一步拿对应的工具实际执行

为什么要分两个角色？
  1. 专注度不同：Planner 要推理能力强（选 DeepSeek R1），
     Executor 要工具调用稳（选 Qwen-Coder 就够）
  2. 失败隔离：某步 Executor 失败不影响 Planner 的整体规划
  3. 可见性：用户能看到"计划长什么样"，不是黑盒黑跑到底

【和 LangGraph 的关系】
这就是 LangGraph 擅长的场景 —— 但我们用 PydanticAI 也能搭出一个简化版。
差异：LangGraph 的状态在图节点间流转，每步都能回看全部状态；
我们的简化版是顺序执行 + 共享 short_term memory，够用不够优雅。

进阶设计说明：
"Q: 为什么不直接上 LangGraph？"
→ 渐进式引入。先用 PydanticAI 把 planner 做 minimum viable，
  跑起来发现"状态回看"不够用、需要"条件分支"了，再引 LangGraph 不迟。
  这叫 YAGNI —— 不提前为想象中的需求付出复杂度。

【触发时机】
router 返回 complexity="complex" 时，harness 先跑 planner，再按 plan 顺序执行。
simple 任务跳过 planner，直接一把梭。
"""

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider


class Step(BaseModel):
    """一步子任务。"""

    description: str = Field(..., description="这一步要做什么（一句话，动作明确）")
    suggested_model: Literal["deepseek", "qwen", "doubao"] = Field(
        ..., description="建议哪家模型做（和 router 策略保持一致）"
    )
    needs_tools: bool = Field(
        ..., description="这一步是否需要调工具（读文件 / 跑命令），False 表示纯思考输出"
    )


class TaskPlan(BaseModel):
    """完整执行计划。"""

    summary: str = Field(..., description="一句话总结这个计划要做什么")
    steps: list[Step] = Field(..., description="有序子步骤列表")


def _build_planner_agent() -> Agent[None, TaskPlan]:
    # 优先 DeepSeek；没有就用 Gemini（学习场景单 key 可跑）
    ds_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if len(ds_key) >= 20:
        provider = OpenAIProvider(
            api_key=ds_key,
            base_url="https://api.deepseek.com/v1",
        )
        # Planner 用 deepseek-chat（推理好 + 便宜），R1 太慢没必要
        model = OpenAIModel("deepseek-chat", provider=provider)
    elif os.getenv("GEMINI_API_KEY"):
        provider = OpenAIProvider(
            api_key=os.getenv("GEMINI_API_KEY", ""),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        model = OpenAIModel(
            os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), provider=provider
        )
    else:
        provider = OpenAIProvider(api_key="", base_url="https://api.deepseek.com/v1")
        model = OpenAIModel("deepseek-chat", provider=provider)
    return Agent(
        model=model,
        output_type=TaskPlan,
        system_prompt=(
            "你是一个任务规划器。接收一个复杂编程任务，拆成 2–5 个有序子步骤。\n"
            "原则：\n"
            "1. 每步要足够小，能在单轮 LLM 调用内完成\n"
            "2. 步骤之间有明显先后依赖\n"
            "3. needs_tools=True 表示该步要读写文件或跑命令\n"
            "4. suggested_model: 推理/debug→deepseek, 代码生成→qwen, 简单→doubao\n"
            "只返回 JSON。"
        ),
    )


_planner: Agent[None, TaskPlan] | None = None


async def plan(task: str) -> TaskPlan:
    """输入复杂任务描述，返回结构化执行计划。"""
    global _planner
    if _planner is None:
        _planner = _build_planner_agent()
    result = await _planner.run(task)
    return result.output
