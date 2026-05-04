"""
适配器重试 + 速率限制兜底
============================

【为什么需要】
README 里的 "常见坑" 提到：`--compare` 三家并发时单 key 容易撞 503 / 429。
这个模块给所有适配器加一层"指数退避重试"，让偶发的 5xx / 429 不会直接挂掉。

【哪些错可以重试】
✅ 应重试：
  - HTTP 429 Too Many Requests（限流）
  - HTTP 500/502/503/504（服务端瞬时故障）
  - 网络层错误：APIConnectionError / 超时

❌ 不应重试（重试只会浪费 token / 钱）：
  - 401 Unauthorized（key 不对）
  - 403 Forbidden
  - 400 Bad Request（参数错）
  - 404 Not Found

【退避策略】
经典做法：base * 2^attempt + jitter。
  attempt 0: 1s
  attempt 1: 2s ± 0.3
  attempt 2: 4s ± 0.7

总等待最多 1+2+4 = 7s（默认 3 次重试），不至于阻塞太久。

【面试讲法】
"Q: 为什么不用 tenacity？"
→ tenacity 是好工具，但本项目目标是面试 demo，多一个依赖就少一分清晰。
  20 行手写指数退避 + jitter 已经覆盖 95% 用例，且把"为什么这么做"写进注释。

"Q: 流式调用怎么重试？"
→ 半流断了的话不能简单重试（已经向用户输出过的内容会重复）。
  本模块只对"非流式 complete"做重试；流式只做开流前的失败重试，
  开流之后的中断按当前 chunk 之前已 yield 的部分计数。
"""

from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator, Awaitable, Callable, TypeVar


T = TypeVar("T")


# 默认参数
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0   # 秒
DEFAULT_MAX_DELAY = 30.0


def _is_retryable(exc: BaseException) -> bool:
    """
    决定一个异常是否可重试。
      - openai.APIStatusError: 看 status_code 在 {429, 500, 502, 503, 504}
      - openai.APIConnectionError / APITimeoutError: 直接重试
      - asyncio.TimeoutError: 重试
      - 其他：不重试（避免把 401 / 400 这种 deterministic 错也卷进来）
    """
    # 延迟 import 避免硬依赖 openai；如果没装，下面 isinstance 都返回 False
    try:
        from openai import APIConnectionError, APITimeoutError, APIStatusError, RateLimitError
    except Exception:
        APIConnectionError = APITimeoutError = APIStatusError = RateLimitError = ()  # type: ignore[assignment,misc]

    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):  # type: ignore[arg-type]
        return True
    if isinstance(exc, APIStatusError):  # type: ignore[arg-type]
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        return status in {429, 500, 502, 503, 504}
    return False


def _backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """指数退避 + jitter。attempt 从 0 起算。"""
    raw = base * (2 ** attempt)
    delay = min(raw, cap)
    # ±25% 抖动，错峰重试避免 thundering herd
    jitter = delay * 0.25 * (random.random() * 2 - 1)
    return max(0.0, delay + jitter)


async def async_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    is_retryable: Callable[[BaseException], bool] = _is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """
    通用 async 重试。

    Args:
        factory     : 无参数 async 工厂函数，返回 awaitable。每次重试都重新调一次
                      factory()，因此 factory 内部应能从头开始（别在外面预 await）。
        max_retries : 最多重试次数（不含首次调用）
        base_delay  : 基础延迟秒数（指数底）
        max_delay   : 单次延迟上限
        is_retryable: 自定义"哪些错该重试"
        sleep       : 注入睡眠函数，方便测试不真等

    Returns:
        factory() 成功时的返回值。

    Raises:
        最后一次重试还失败时，原样抛出。
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await factory()
        except BaseException as exc:   # 包括 KeyboardInterrupt 也想 break 的话改成 Exception
            last_exc = exc
            if attempt >= max_retries:
                break
            if not is_retryable(exc):
                raise
            wait = _backoff_seconds(attempt, base_delay, max_delay)
            await sleep(wait)
    assert last_exc is not None
    raise last_exc


async def async_retry_stream(
    factory: Callable[[], "AsyncIterator[str]"],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    is_retryable: Callable[[BaseException], bool] = _is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
):
    """
    流式 retry。难点：半流断了不能简单重新跑（会让用户看到重复内容）。
    解决方案：**buffer-prefix replay**——
      1. 失败时把已 yield 的所有 chunk 都记录在 buffer 里
      2. 重新调 factory() 拿新流，**跳过新流前 buffer 长度的字符**再开始 yield
      3. 这样下游消费者看到的输出是连续的：旧前缀 + 新尾部

    缺点：模型每次重试会重新生成（消耗 token），但保证用户体验连续。
    生产可加 partial=True 只发剩余部分给模型，但这要厂商 SDK 支持。

    Args:
        factory: 无参 async-gen 工厂；每次重试都重新调一次。
                 调用 factory() 应该返回一个 AsyncIterator[str]。
        其余参数同 async_retry。

    Yields:
        chunk 字符串。

    Raises:
        所有重试都失败时抛最后一个错。
    """
    buffer = ""
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            new_text = ""
            async for chunk in factory():
                new_text += chunk
                # 跳过和 buffer 重叠的前缀
                if len(new_text) <= len(buffer):
                    continue
                if attempt > 0 and len(new_text) - len(chunk) < len(buffer):
                    # chunk 跨越了 buffer 边界：只 yield 越界部分
                    skip = len(buffer) - (len(new_text) - len(chunk))
                    yield chunk[skip:]
                else:
                    yield chunk
            return
        except BaseException as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            if not is_retryable(exc):
                raise
            # 把这次成功 yield 出去的内容全部记进 buffer
            buffer = new_text   # type: ignore[possibly-undefined]
            wait = _backoff_seconds(attempt, base_delay, max_delay)
            await sleep(wait)
    assert last_exc is not None
    raise last_exc


__all__ = [
    "async_retry",
    "async_retry_stream",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
]
