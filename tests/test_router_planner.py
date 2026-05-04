"""
Router 与 Planner 单元测试（不联网）
=====================================

跑法：
    python -m tests.test_router_planner

策略：
  router / planner 各自维护一个 PydanticAI Agent，正常会调真实模型。
  测试时用 PydanticAI 的 TestModel 注入预设输出 + agent.override，
  全程零网络。

覆盖：
  - RouteDecision / Step / TaskPlan 的 Pydantic 校验（Literal 强类型）
  - 用 TestModel override 后 route() / plan() 返回预设结果
"""

import asyncio

import pydantic
from pydantic_ai.models.test import TestModel

import orchestration.router as router_mod
from orchestration.router import RouteDecision, route, _build_router_agent
import orchestration.planner as planner_mod
from orchestration.planner import Step, TaskPlan, plan, _build_planner_agent


def _run(coro):
    return asyncio.run(coro)


# ────────────────────────── Pydantic schema 校验 ──────────────────────────


def test_route_decision_accepts_valid_models():
    for m in ("deepseek", "qwen", "doubao"):
        d = RouteDecision(model=m, complexity="simple", reason="test")
        assert d.model == m


def test_route_decision_rejects_invalid_model():
    try:
        RouteDecision(model="chatgpt", complexity="simple", reason="x")
    except pydantic.ValidationError:
        return
    raise AssertionError("expected ValidationError for invalid model")


def test_route_decision_rejects_invalid_complexity():
    try:
        RouteDecision(model="deepseek", complexity="medium", reason="x")
    except pydantic.ValidationError:
        return
    raise AssertionError("expected ValidationError for invalid complexity")


def test_route_decision_requires_reason():
    try:
        RouteDecision(model="deepseek", complexity="simple")  # type: ignore
    except pydantic.ValidationError:
        return
    raise AssertionError("expected ValidationError when reason missing")


def test_step_validates_suggested_model():
    s = Step(description="do x", suggested_model="qwen", needs_tools=True)
    assert s.needs_tools is True
    try:
        Step(description="do x", suggested_model="claude", needs_tools=False)
    except pydantic.ValidationError:
        return
    raise AssertionError("expected ValidationError for invalid suggested_model")


def test_task_plan_holds_steps():
    p = TaskPlan(
        summary="refactor auth",
        steps=[
            Step(description="read files", suggested_model="qwen", needs_tools=True),
            Step(description="write new", suggested_model="deepseek", needs_tools=True),
        ],
    )
    assert p.summary == "refactor auth"
    assert len(p.steps) == 2


# ────────────────────────── route() 端到端，用 TestModel ──────────────────────────


def test_route_returns_overridden_decision():
    """用 TestModel 灌一个 RouteDecision，确认 route() 把它原样吐出来。"""
    # 建一个 agent（会读 env，但因为我们 override 了 model，不会真发请求）
    agent = _build_router_agent()
    canned = TestModel(custom_output_args={
        "model": "qwen",
        "complexity": "complex",
        "reason": "code generation task",
    })
    # 把模块级 _router_agent 替换成我们手里这个 agent
    router_mod._router_agent = agent
    try:
        with agent.override(model=canned):
            decision = _run(route("写个递归函数"))
        assert decision.model == "qwen"
        assert decision.complexity == "complex"
        assert "code generation" in decision.reason
    finally:
        router_mod._router_agent = None  # 清掉，不污染其他测试


def test_route_decision_each_model_value():
    """三家模型都能从 TestModel 吐回来。"""
    agent = _build_router_agent()
    router_mod._router_agent = agent
    try:
        for m in ("deepseek", "qwen", "doubao"):
            tm = TestModel(custom_output_args={
                "model": m, "complexity": "simple", "reason": "r",
            })
            with agent.override(model=tm):
                d = _run(route("anything"))
            assert d.model == m
    finally:
        router_mod._router_agent = None


# ────────────────────────── plan() 端到端，用 TestModel ──────────────────────────


def test_plan_returns_overridden_plan():
    agent = _build_planner_agent()
    canned = TestModel(custom_output_args={
        "summary": "two-step refactor",
        "steps": [
            {"description": "read all auth files", "suggested_model": "qwen", "needs_tools": True},
            {"description": "rewrite hash function", "suggested_model": "deepseek", "needs_tools": True},
        ],
    })
    planner_mod._planner = agent
    try:
        with agent.override(model=canned):
            tp = _run(plan("重构 auth 模块"))
        assert tp.summary == "two-step refactor"
        assert len(tp.steps) == 2
        assert tp.steps[0].suggested_model == "qwen"
        assert tp.steps[1].needs_tools is True
    finally:
        planner_mod._planner = None


def test_plan_step_count_can_be_one():
    """边界：一步计划也合法。"""
    agent = _build_planner_agent()
    canned = TestModel(custom_output_args={
        "summary": "trivial",
        "steps": [
            {"description": "answer directly", "suggested_model": "doubao", "needs_tools": False},
        ],
    })
    planner_mod._planner = agent
    try:
        with agent.override(model=canned):
            tp = _run(plan("hi"))
        assert len(tp.steps) == 1
        assert tp.steps[0].needs_tools is False
    finally:
        planner_mod._planner = None


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
    print(f"\n{len(tests) - failed}/{len(tests)} router/planner tests passed.")
    if failed:
        raise SystemExit(1)
