"""
适配器冒烟测试（手动跑，需要 .env 配好真 API Key）
====================================================

跑法：
    python -m tests.test_adapters

这不是单元测试（没 mock），是"真打一下 API 看能不能通"的冒烟测试。
面试时常问："你怎么验证多个厂商适配器接口一致？"
答：写一个参数化的循环，三家都跑同一个 prompt，都返回 str 即通过。
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from orchestration.adapters import (
    DeepSeekAdapter,
    DashScopeAdapter,
    VolcEngineAdapter,
    ModelAdapter,
)


PROMPT = "用一句话介绍你自己，10 字以内。"


async def probe(adapter: ModelAdapter) -> None:
    print(f"\n--- {adapter.name} ---")
    try:
        text = await adapter.complete(
            messages=[{"role": "user", "content": PROMPT}],
            system="你是简洁的助手。",
        )
        print(f"OK: {text!r}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")


async def main() -> None:
    adapters: list[ModelAdapter] = []
    if os.getenv("DEEPSEEK_API_KEY"):
        adapters.append(DeepSeekAdapter())
    if os.getenv("DASHSCOPE_API_KEY"):
        adapters.append(DashScopeAdapter())
    if os.getenv("VOLC_API_KEY") and os.getenv("DOUBAO_ENDPOINT_ID"):
        adapters.append(VolcEngineAdapter())

    if not adapters:
        print("没有配置任何 API Key，跳过。请先填 .env。")
        return

    for a in adapters:
        await probe(a)


if __name__ == "__main__":
    asyncio.run(main())
