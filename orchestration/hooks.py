"""
Hook 系统：在关键事件点插入自定义逻辑（Harness 编排层）
===========================================================

【Hook 是什么】
Hook（钩子）= 在系统预定义的执行点注入自定义回调。
Claude Code 就有 Hook 系统：PreToolUse / PostToolUse / SessionStart 等，
用户可以在这些事件上挂 shell 脚本或函数。

【为什么 Agent 需要 Hook】
Agent 的执行过程像个黑盒："模型调了工具 → 工具返回 → 继续"。
但真实场景我们想在这些点插入：
  - 日志：每次工具调用打一条
  - 审计：高危操作前记录谁批准了
  - 权限检查：某些工具只允许特定用户调
  - 成本监控：token 用量累加
  - 注入上下文：前置工具调用结果自动附加到下一轮 prompt

Hook 让这些横切关注点（cross-cutting concerns）能插进来，不污染核心循环代码。

【本项目的 Hook 模型】
最简设计：两个事件点 + 一个全局注册表。

  pre_tool_hook(tool_name, args)      # 工具执行前
  post_tool_hook(tool_name, result)   # 工具执行后

每个事件可以挂多个回调（按注册顺序执行）。

【面试点】
"Hook 和装饰器的区别？"
→ 装饰器是编译时（import 时）绑定，hook 是运行时可动态增删。
  hook 更适合多方扩展的场景（用户 / 插件 / 日志都能挂）。

"如果某个 hook 抛异常怎么办？"
→ 我们默认吞掉（打印警告）——hook 不应该影响主流程。
  生产系统可以改成「可配置：严格模式 / 宽容模式」。
"""

import time
from typing import Callable


# Hook 函数签名：pre = (tool_name, args) -> None, post = (tool_name, result) -> None
PreHook = Callable[[str, dict], None]
PostHook = Callable[[str, str], None]


class HookRegistry:
    """
    全局 Hook 注册表。单例风格使用 —— harness 初始化时建一个，到处传递。
    """

    def __init__(self):
        self._pre: list[PreHook] = []
        self._post: list[PostHook] = []
        # 用来计算工具耗时：工具名 → 开始时间
        self._tool_start: dict[str, float] = {}

    # ─── 注册 ───

    def add_pre(self, hook: PreHook) -> None:
        self._pre.append(hook)

    def add_post(self, hook: PostHook) -> None:
        self._post.append(hook)

    # ─── 触发 ───

    def fire_pre(self, tool_name: str, args: dict) -> None:
        self._tool_start[tool_name] = time.perf_counter()
        for h in self._pre:
            self._safe_call(h, tool_name, args)

    def fire_post(self, tool_name: str, result: str) -> None:
        for h in self._post:
            self._safe_call(h, tool_name, result)

    def elapsed_ms(self, tool_name: str) -> float:
        """取上一次 pre 到现在的耗时（毫秒）。post hook 里会用到。"""
        start = self._tool_start.get(tool_name)
        if start is None:
            return 0.0
        return (time.perf_counter() - start) * 1000

    # ─── 内部：hook 异常不扩散 ───

    @staticmethod
    def _safe_call(hook, *args) -> None:
        try:
            hook(*args)
        except Exception as e:
            # 故意不抛，不让用户的 hook bug 毁了主流程
            print(f"[hook warning] {hook.__name__} failed: {e}")


# ───────────────── 默认 Hook：打印日志 ─────────────────


def make_default_logging_hooks(registry: HookRegistry) -> None:
    """
    给 registry 挂上默认的日志 hook。
    把这部分独立成函数而不是写在 __init__ 里，是为了让用户能选择「不要默认 hook」。
    """

    def log_pre(tool_name: str, args: dict) -> None:
        # 打印时截断太长的参数，避免日志爆炸
        preview = str(args)[:80]
        print(f"[tool→] {tool_name}({preview})")

    def log_post(tool_name: str, result: str) -> None:
        ms = registry.elapsed_ms(tool_name)
        preview = result[:120].replace("\n", " ")
        print(f"[tool←] {tool_name} [{ms:.1f}ms] {preview}")

    registry.add_pre(log_pre)
    registry.add_post(log_post)
