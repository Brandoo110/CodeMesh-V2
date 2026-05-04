"""
Hook 系统：在关键事件点插入自定义逻辑（Harness 编排层）
===========================================================

【Hook 是什么】
Hook（钩子）= 在系统预定义的执行点注入自定义回调。
Claude Code 和 HKUDS/OpenHarness 都有这套：PreToolUse / PostToolUse /
SessionStart / SessionEnd / UserPromptSubmit / Stop 等，用户可以在这些
事件上挂 shell 脚本或函数。

【为什么 Agent 需要 Hook】
Agent 的执行过程像个黑盒："模型调了工具 → 工具返回 → 继续"。
但真实场景我们想在这些点插入：
  - 日志：每次工具调用打一条
  - 审计：高危操作前记录谁批准了
  - 权限检查：某些工具只允许特定用户调
  - 成本监控：token 用量累加
  - 注入上下文：前置工具调用结果自动附加到下一轮 prompt
  - **拦截**：pre 钩子返回 blocked=True 阻止本次工具执行

Hook 让这些横切关注点（cross-cutting concerns）能插进来，不污染核心循环代码。

【本项目的 Hook 模型（v2，对齐 Claude Code / OpenHarness）】
事件枚举 HookEvent：
  PreToolUse        - 工具执行前；可返回 blocked=True 拦截
  PostToolUse       - 工具执行后
  SessionStart      - Harness.run() / run_stream() 进入时
  SessionEnd        - Harness.run() / run_stream() 退出时
  UserPromptSubmit  - 用户输入到达时（router 之前）
  Stop              - 单轮迭代终止时（agent loop 结束）

HookResult 是结构化结果：
  blocked   - 仅 PreToolUse 有意义，True 表示拦截
  reason    - 为什么拦截 / 为什么放行
  metadata  - 自由扩展字段

【面试点】
"Hook 和装饰器的区别？"
→ 装饰器是编译时（import 时）绑定，hook 是运行时可动态增删。
  hook 更适合多方扩展的场景（用户 / 插件 / 日志都能挂）。

"如果某个 hook 抛异常怎么办？"
→ 我们默认吞掉（打印警告）——hook 不应该影响主流程。
  生产系统可以改成「可配置：严格模式 / 宽容模式」。

"为什么搞 enum 而不是字符串？"
→ Claude Code 的 hook 配置就是这几个固定字符串；用 Enum 让 IDE 能补全
  + 拼错事件名时提前报错（mypy 也会抓）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ─────────────────────────── 事件枚举 ───────────────────────────


class HookEvent(str, Enum):
    """所有可挂钩的事件名。值是给配置文件写的字符串。"""
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"


# ─────────────────────────── 结果对象 ───────────────────────────


@dataclass
class HookResult:
    """
    单个 hook 调用的结果。
      blocked   : 仅 PreToolUse 有效。True 表示拦下这次工具执行。
      reason    : 为什么 blocked / 为什么放行（写日志好看）。
      metadata  : 扩展字段（成本累加器、token 统计、自定义都行）。
    """
    blocked: bool = False
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls) -> "HookResult":
        return cls(blocked=False)

    @classmethod
    def block(cls, reason: str) -> "HookResult":
        return cls(blocked=True, reason=reason)


# Hook 回调函数签名：吃任意 kwargs，返回 HookResult 或 None（None 视为 ok）。
HookCallback = Callable[..., Any]


# ─────────────────────────── 注册表 ───────────────────────────


class HookRegistry:
    """
    Hook 注册表。Harness 持有一个实例；用户 / 插件 / 默认日志都注册到这里。

    新 API（推荐）：
        registry.register(HookEvent.PRE_TOOL_USE, my_callback)
        result = registry.trigger(HookEvent.PRE_TOOL_USE, tool_name=..., args=...)
        if result.blocked: ...

    老 API（保留向后兼容）：
        registry.add_pre(fn)   等价于 register(PRE_TOOL_USE, ...)
        registry.fire_pre(...) 等价于 trigger(PRE_TOOL_USE, ...)（忽略 blocked 返回）
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookCallback]] = {
            event: [] for event in HookEvent
        }
        # 用来计算工具耗时：工具名 → 开始时间
        self._tool_start: dict[str, float] = {}

    # ─── 新 API ───

    def register(self, event: HookEvent, callback: HookCallback) -> None:
        """挂一个 callback 到指定事件。"""
        if event not in self._handlers:
            raise ValueError(f"unknown hook event: {event!r}")
        self._handlers[event].append(callback)

    def trigger(self, event: HookEvent, **kwargs: Any) -> HookResult:
        """
        触发事件。多个 hook 按注册顺序执行。
        任意一个返回 blocked=True 就立即返回（短路，后续 hook 不再调）。

        没人挂或全部 ok → 返回 HookResult.ok()
        """
        # PreToolUse 触发时记一下时间，PostToolUse 算耗时用
        if event is HookEvent.PRE_TOOL_USE:
            tool_name = kwargs.get("tool_name", "")
            if tool_name:
                self._tool_start[tool_name] = time.perf_counter()

        for handler in self._handlers.get(event, []):
            try:
                result = handler(**kwargs)
            except Exception as e:
                print(f"[hook warning] {getattr(handler, '__name__', handler)} on {event.value} failed: {e}")
                continue
            if isinstance(result, HookResult) and result.blocked:
                return result
        return HookResult.ok()

    def elapsed_ms(self, tool_name: str) -> float:
        """取上一次 PreToolUse 到现在的耗时（毫秒）。"""
        start = self._tool_start.get(tool_name)
        if start is None:
            return 0.0
        return (time.perf_counter() - start) * 1000

    @property
    def handlers(self) -> dict[HookEvent, list[HookCallback]]:
        """名->列表 视图，调试用。"""
        return {k: list(v) for k, v in self._handlers.items()}

    # ─── 老 API（向后兼容；harness.py 现在依然在用） ───

    def add_pre(self, hook: Callable[[str, dict], None]) -> None:
        """老 API：等价于 register(PRE_TOOL_USE, _wrap_pre(hook))。"""
        def _adapter(*, tool_name: str, args: dict, **_) -> HookResult:
            hook(tool_name, args)
            return HookResult.ok()
        _adapter.__name__ = getattr(hook, "__name__", "pre_hook")
        self.register(HookEvent.PRE_TOOL_USE, _adapter)

    def add_post(self, hook: Callable[[str, str], None]) -> None:
        """老 API：等价于 register(POST_TOOL_USE, _wrap_post(hook))。"""
        def _adapter(*, tool_name: str, result: str, **_) -> HookResult:
            hook(tool_name, result)
            return HookResult.ok()
        _adapter.__name__ = getattr(hook, "__name__", "post_hook")
        self.register(HookEvent.POST_TOOL_USE, _adapter)

    def fire_pre(self, tool_name: str, args: dict) -> None:
        """老 API：忽略 blocked 返回。新代码请用 trigger() 检查 blocked。"""
        self.trigger(HookEvent.PRE_TOOL_USE, tool_name=tool_name, args=args)

    def fire_post(self, tool_name: str, result: str) -> None:
        self.trigger(HookEvent.POST_TOOL_USE, tool_name=tool_name, result=result)


# ───────────────── 默认 Hook：打印日志 ─────────────────


def make_default_logging_hooks(registry: HookRegistry) -> None:
    """
    给 registry 挂上默认的日志 hook。
    把这部分独立成函数而不是写在 __init__ 里，是为了让用户能选择「不要默认 hook」。
    """

    def log_pre_tool(*, tool_name: str, args: dict, **_) -> HookResult:
        preview = str(args)[:80]
        print(f"[tool→] {tool_name}({preview})")
        return HookResult.ok()

    def log_post_tool(*, tool_name: str, result: str, **_) -> HookResult:
        ms = registry.elapsed_ms(tool_name)
        preview = result[:120].replace("\n", " ")
        print(f"[tool←] {tool_name} [{ms:.1f}ms] {preview}")
        return HookResult.ok()

    def log_session_start(*, task: str = "", **_) -> HookResult:
        snippet = (task or "")[:60].replace("\n", " ")
        print(f"[session] start — {snippet!r}")
        return HookResult.ok()

    def log_session_end(*, success: bool = True, **_) -> HookResult:
        flag = "ok" if success else "fail"
        print(f"[session] end — {flag}")
        return HookResult.ok()

    registry.register(HookEvent.PRE_TOOL_USE, log_pre_tool)
    registry.register(HookEvent.POST_TOOL_USE, log_post_tool)
    registry.register(HookEvent.SESSION_START, log_session_start)
    registry.register(HookEvent.SESSION_END, log_session_end)
