"""
Hook 系统单元测试
==================

跑法：
    python -m tests.test_hooks

覆盖：
  - HookEvent enum
  - HookResult.ok / block 工厂
  - register / trigger 新 API
  - PreToolUse 拦截短路（blocked=True 的 hook 之后的 hook 不再调）
  - 老 API add_pre/add_post/fire_pre/fire_post 仍可用
  - hook 抛异常不影响后续 hook 和主流程
  - elapsed_ms 工作正常
  - 默认 logging hooks 注册到正确事件
"""

from orchestration.hooks import (
    HookEvent,
    HookResult,
    HookRegistry,
    make_default_logging_hooks,
)


# ────────────────────────── enum / dataclass ──────────────────────────


def test_hook_event_enum_values():
    """枚举值必须和 Claude Code 的事件名一致（外部配置文件靠它对齐）。"""
    assert HookEvent.PRE_TOOL_USE.value == "PreToolUse"
    assert HookEvent.POST_TOOL_USE.value == "PostToolUse"
    assert HookEvent.SESSION_START.value == "SessionStart"
    assert HookEvent.SESSION_END.value == "SessionEnd"
    assert HookEvent.USER_PROMPT_SUBMIT.value == "UserPromptSubmit"
    assert HookEvent.STOP.value == "Stop"


def test_hook_result_factories():
    ok = HookResult.ok()
    assert ok.blocked is False
    blocked = HookResult.block("nope")
    assert blocked.blocked is True
    assert blocked.reason == "nope"


# ────────────────────────── register / trigger ──────────────────────────


def test_register_and_trigger_pre_tool_use():
    reg = HookRegistry()
    seen = []

    def my_hook(*, tool_name, args, **_):
        seen.append((tool_name, args))
        return HookResult.ok()

    reg.register(HookEvent.PRE_TOOL_USE, my_hook)
    out = reg.trigger(HookEvent.PRE_TOOL_USE, tool_name="bash_exec", args={"cmd": "ls"})
    assert out.blocked is False
    assert seen == [("bash_exec", {"cmd": "ls"})]


def test_blocked_hook_short_circuits():
    """前一个 hook 返回 blocked=True，后面的 hook 不该被调用。"""
    reg = HookRegistry()
    seen = []

    def first(*, tool_name, args, **_):
        return HookResult.block("denied by policy")

    def second(*, tool_name, args, **_):
        seen.append("second was called")
        return HookResult.ok()

    reg.register(HookEvent.PRE_TOOL_USE, first)
    reg.register(HookEvent.PRE_TOOL_USE, second)
    out = reg.trigger(HookEvent.PRE_TOOL_USE, tool_name="x", args={})
    assert out.blocked is True
    assert "denied by policy" in out.reason
    assert seen == []  # second 没被调


def test_hook_returning_none_is_ok():
    """callback 返回 None 时不应该崩溃；视为 ok。"""
    reg = HookRegistry()

    def silent(*, tool_name, args, **_):
        return None

    reg.register(HookEvent.PRE_TOOL_USE, silent)
    out = reg.trigger(HookEvent.PRE_TOOL_USE, tool_name="x", args={})
    assert out.blocked is False


def test_hook_exception_is_swallowed():
    """一个 hook 抛错不应该影响其他 hook。"""
    reg = HookRegistry()
    seen = []

    def bad(*, tool_name, args, **_):
        raise RuntimeError("boom")

    def good(*, tool_name, args, **_):
        seen.append("ran")
        return HookResult.ok()

    reg.register(HookEvent.PRE_TOOL_USE, bad)
    reg.register(HookEvent.PRE_TOOL_USE, good)
    out = reg.trigger(HookEvent.PRE_TOOL_USE, tool_name="x", args={})
    assert out.blocked is False
    assert seen == ["ran"]


def test_unknown_event_register_raises():
    reg = HookRegistry()
    try:
        reg.register("not_an_event", lambda **_: None)  # type: ignore[arg-type]
    except (ValueError, KeyError):
        return
    raise AssertionError("expected ValueError/KeyError on unknown event")


# ────────────────────────── 老 API 兼容 ──────────────────────────


def test_add_pre_old_api_still_works():
    reg = HookRegistry()
    seen = []

    def old_pre(tool_name, args):
        seen.append((tool_name, args))

    reg.add_pre(old_pre)
    reg.fire_pre("read_file", {"path": "x"})
    assert seen == [("read_file", {"path": "x"})]


def test_add_post_old_api_still_works():
    reg = HookRegistry()
    seen = []

    def old_post(tool_name, result):
        seen.append((tool_name, result))

    reg.add_post(old_post)
    reg.fire_post("read_file", "OK")
    assert seen == [("read_file", "OK")]


# ────────────────────────── elapsed_ms ──────────────────────────


def test_elapsed_ms_zero_before_pre():
    reg = HookRegistry()
    assert reg.elapsed_ms("never_started") == 0.0


def test_elapsed_ms_grows_after_pre():
    import time
    reg = HookRegistry()
    reg.trigger(HookEvent.PRE_TOOL_USE, tool_name="x", args={})
    time.sleep(0.01)
    elapsed = reg.elapsed_ms("x")
    assert elapsed >= 5  # 至少 5ms（time.sleep 精度问题）


# ────────────────────────── 默认 logging hooks ──────────────────────────


def test_default_logging_hooks_registers_four_events():
    reg = HookRegistry()
    make_default_logging_hooks(reg)
    h = reg.handlers
    # 至少四类事件各注册了一个
    assert len(h[HookEvent.PRE_TOOL_USE]) >= 1
    assert len(h[HookEvent.POST_TOOL_USE]) >= 1
    assert len(h[HookEvent.SESSION_START]) >= 1
    assert len(h[HookEvent.SESSION_END]) >= 1


def test_session_start_hook_invocable(capsys):  # capsys 不强求；如果没 pytest 就不检查输出
    reg = HookRegistry()
    make_default_logging_hooks(reg)
    out = reg.trigger(HookEvent.SESSION_START, task="hello")
    assert out.blocked is False


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_") and k != "test_session_start_hook_invocable"
    ]
    # 这条用例需要 pytest 的 capsys，单独跳一下
    try:
        # 不传 capsys 也能跑，因为里面我们只 assert blocked
        tests.append(globals()["test_session_start_hook_invocable"])
    except KeyError:
        pass
    failed = 0
    for t in tests:
        try:
            # capsys 缺省时给 None
            try:
                t()
            except TypeError:
                # 需要 capsys 的用例兜一下
                t.__call__(None)  # type: ignore[arg-type]
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} hooks tests passed.")
    if failed:
        raise SystemExit(1)
