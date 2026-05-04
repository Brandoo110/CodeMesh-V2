"""
async_retry 单元测试
======================

跑法：
    python -m tests.test_retry

策略：
  注入 fake sleep 让测试不真等。验证：
    - 成功首次返回，不重试
    - 可重试错误退避后重试
    - 不可重试错误立即抛
    - 超过 max_retries 仍失败 → 抛最后一个错
    - 退避序列接近指数（带 ±25% jitter 容差）
"""

import asyncio

from orchestration.adapters.retry import async_retry


def _run(coro):
    return asyncio.run(coro)


# ────────────────────────── helpers ──────────────────────────


class _SleepRecorder:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, t: float) -> None:
        self.delays.append(t)


def _retryable_error():
    """可重试的错（asyncio.TimeoutError 在 _is_retryable 里默认 True）。"""
    return asyncio.TimeoutError("timeout")


def _non_retryable_error():
    """不可重试的错。"""
    return ValueError("bad input")


# ────────────────────────── tests ──────────────────────────


def test_first_attempt_succeeds_no_sleep():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    out = _run(async_retry(factory, sleep=sleep))
    assert out == "ok"
    assert calls["n"] == 1
    assert sleep.delays == []


def test_retryable_error_then_success():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _retryable_error()
        return "finally"

    out = _run(async_retry(factory, max_retries=3, sleep=sleep))
    assert out == "finally"
    assert calls["n"] == 3
    # 两次重试 → 两次 sleep
    assert len(sleep.delays) == 2


def test_non_retryable_immediately_raises():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _non_retryable_error()

    try:
        _run(async_retry(factory, max_retries=5, sleep=sleep))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert calls["n"] == 1
    assert sleep.delays == []


def test_exceeds_max_retries_raises_last():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _retryable_error()

    try:
        _run(async_retry(factory, max_retries=2, sleep=sleep))
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("expected TimeoutError")
    # 1 次首次 + 2 次重试 = 3 次调用
    assert calls["n"] == 3
    # 2 次重试 → 2 次 sleep
    assert len(sleep.delays) == 2


def test_backoff_grows_exponentially():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _retryable_error()

    try:
        _run(async_retry(
            factory, max_retries=3, base_delay=1.0, max_delay=100.0, sleep=sleep,
        ))
    except asyncio.TimeoutError:
        pass

    # base=1, attempt 0/1/2 → 期望约 1 / 2 / 4，含 ±25% jitter
    assert len(sleep.delays) == 3
    d0, d1, d2 = sleep.delays
    assert 0.7 <= d0 <= 1.4
    assert 1.4 <= d1 <= 2.7
    assert 2.9 <= d2 <= 5.2


def test_max_delay_caps():
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _retryable_error()

    try:
        _run(async_retry(
            factory, max_retries=5, base_delay=10.0, max_delay=2.0, sleep=sleep,
        ))
    except asyncio.TimeoutError:
        pass
    # 所有 sleep 都被 max_delay=2.0 截顶（含 ±25% jitter，最大 2.5）
    assert all(d <= 2.6 for d in sleep.delays)


def test_custom_is_retryable():
    """自定义谓词：把 ValueError 视为可重试。"""
    sleep = _SleepRecorder()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("transient")
        return "done"

    out = _run(async_retry(
        factory,
        is_retryable=lambda e: isinstance(e, ValueError),
        sleep=sleep,
    ))
    assert out == "done"
    assert calls["n"] == 2


def test_factory_called_fresh_each_attempt():
    """每次重试都应该重新调 factory()，而不是复用同一个 awaitable。"""
    sleep = _SleepRecorder()
    factory_calls = {"n": 0}
    inner_calls = {"n": 0}

    async def inner():
        inner_calls["n"] += 1
        if inner_calls["n"] < 2:
            raise _retryable_error()
        return "v"

    def factory():
        factory_calls["n"] += 1
        return inner()   # 每次都生成新 coroutine

    out = _run(async_retry(factory, sleep=sleep))
    assert out == "v"
    assert factory_calls["n"] == 2  # 真的被调了两次


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_")
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} retry tests passed.")
    if failed:
        raise SystemExit(1)
