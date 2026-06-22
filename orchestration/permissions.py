"""
权限系统：ALLOW / DENY / ASK 三级（Harness 编排层）
======================================================

【为什么要做权限分级】
v2 之前 `execution/sandbox.py` 是单层正则黑名单，黑名单命中就拦，否则放行。
两个问题：
  1. **粒度太粗**：要么全允许，要么全拦——没有"危险但偶尔需要"的中间档
  2. **写死在沙箱里**：插件 / 用户没法叠加自定义规则

参考 Claude Code 和 OpenHarness 的多级权限模型，做成三档：
  - ALLOW : 显式放行（白名单优先级最高）
  - ASK   : 需要二次确认才放行（生产 UI 弹框；CLI 默认 deny）
  - DENY  : 显式拒绝

匹配规则按 **顺序** 走：第一个命中的规则决定结果。
默认决定（无规则命中）= ALLOW（保持向后兼容；危险命令仍走老 sandbox 黑名单）。

【接入 Hook 系统】
注册一个 PreToolUse hook：
  - 拿 (tool_name, args) → check_permission(...) 决定
  - DENY  → return HookResult.block(reason)
  - ASK   → 当前 CLI 模式按 deny 处理（生产应该弹 prompt）
  - ALLOW → return HookResult.ok()

这样 sandbox 的正则黑名单依然在工具内部跑（双层防御），权限层在外部统一管。

【设计说明】
"Q: 为什么不直接扩大 sandbox 黑名单？"
→ sandbox 是工具内部的"硬安全"，写死代码里不可绕过。
  权限层是"软策略"，**可被用户配置 / 插件覆盖**——例如 dev 机器允许
  `npm install`，prod 机器禁止。两者职责不同。

"Q: ASK 怎么对接 UI？"
→ 接口里 ASK 转成 HookResult.block + reason='requires confirmation'。
  上层（CLI / IDE / Web UI）拿到 blocked + reason 自行决定弹框 / 拒绝。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Permission(str, Enum):
    """三档权限决定。"""
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# 一个 matcher 是 (tool_name, args) -> bool 函数
Matcher = Callable[[str, dict], bool]


@dataclass
class PermissionRule:
    """一条权限规则。"""
    permission: Permission
    matcher: Matcher
    reason: str = ""
    name: str = ""   # 调试用名字

    def matches(self, tool_name: str, args: dict) -> bool:
        try:
            return self.matcher(tool_name, args)
        except Exception:
            # matcher 抛错就当不命中，别让权限规则把工具调用搞崩
            return False


@dataclass
class PermissionDecision:
    """check_permission 的返回值。"""
    permission: Permission
    rule: Optional[PermissionRule] = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.permission == Permission.ALLOW

    @property
    def denied(self) -> bool:
        return self.permission == Permission.DENY


# ─────────────────────────── 注册表 ───────────────────────────


class PermissionRegistry:
    """
    顺序注册表。check 时第一个命中的规则赢。

    用法：
        reg = PermissionRegistry()
        reg.deny("bash_exec", lambda args: "rm -rf /" in args.get("cmd", ""))
        reg.allow("read_file")
        decision = reg.check("bash_exec", {"cmd": "ls"})
    """

    def __init__(self, default: Permission = Permission.ALLOW) -> None:
        self._rules: list[PermissionRule] = []
        self._default = default

    @property
    def default(self) -> Permission:
        return self._default

    def add(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    def add_rule(
        self,
        permission: Permission,
        tool_name_pattern: str,
        arg_predicate: Optional[Callable[[dict], bool]] = None,
        *,
        reason: str = "",
        name: str = "",
    ) -> None:
        """
        快捷注册：tool_name_pattern 是 fnmatch（'bash_exec' / '*' / 'lsp_*'），
        arg_predicate 是可选的 args 谓词。

        Examples:
            reg.add_rule(Permission.ALLOW, "read_file")
            reg.add_rule(Permission.DENY, "bash_exec",
                         lambda a: a.get("cmd", "").startswith("git push"))
        """
        def _matcher(t: str, a: dict) -> bool:
            if not fnmatch.fnmatch(t, tool_name_pattern):
                return False
            if arg_predicate is not None:
                return bool(arg_predicate(a))
            return True
        self.add(PermissionRule(
            permission=permission,
            matcher=_matcher,
            reason=reason,
            name=name or f"{permission.value}:{tool_name_pattern}",
        ))

    def allow(self, tool_name_pattern: str, arg_predicate=None, **kw) -> None:
        self.add_rule(Permission.ALLOW, tool_name_pattern, arg_predicate, **kw)

    def deny(self, tool_name_pattern: str, arg_predicate=None, **kw) -> None:
        self.add_rule(Permission.DENY, tool_name_pattern, arg_predicate, **kw)

    def ask(self, tool_name_pattern: str, arg_predicate=None, **kw) -> None:
        self.add_rule(Permission.ASK, tool_name_pattern, arg_predicate, **kw)

    def check(self, tool_name: str, args: dict) -> PermissionDecision:
        """按注册顺序找第一个命中的规则；没有命中走默认。"""
        for rule in self._rules:
            if rule.matches(tool_name, args):
                return PermissionDecision(
                    permission=rule.permission,
                    rule=rule,
                    reason=rule.reason or rule.name,
                )
        return PermissionDecision(permission=self._default, reason="default")


# ─────────────────────────── 默认规则集 ───────────────────────────


def make_default_permissions() -> PermissionRegistry:
    """
    一个保守的默认规则集，覆盖明显高危场景。
    用户 / 插件可以在 Harness 启动后再叠加。
    """
    reg = PermissionRegistry(default=Permission.ALLOW)

    # bash_exec 里的远程推送 / 删除分支等危险 git 操作 → ASK
    def _is_force_push(args: dict) -> bool:
        cmd = (args.get("cmd") or "").lower()
        return ("git push" in cmd and "--force" in cmd) or ("git push -f" in cmd)

    def _is_branch_delete(args: dict) -> bool:
        cmd = (args.get("cmd") or "").lower()
        return "git branch -d" in cmd or "git branch --delete" in cmd

    def _is_pip_install(args: dict) -> bool:
        cmd = (args.get("cmd") or "").lower()
        return cmd.startswith("pip install") or " pip install " in cmd

    reg.ask("bash_exec", _is_force_push,
            reason="force push needs confirmation", name="bash:git-force-push")
    reg.ask("bash_exec", _is_branch_delete,
            reason="branch deletion needs confirmation", name="bash:git-branch-delete")
    reg.ask("bash_exec", _is_pip_install,
            reason="installing packages needs confirmation", name="bash:pip-install")

    # write_file / edit_file 写到 /etc / / / ~/.ssh → DENY
    def _is_dangerous_write(args: dict) -> bool:
        path = (args.get("path") or "").lower()
        return (
            path.startswith("/etc/")
            or path.startswith("/boot/")
            or path == "/"
            or "/.ssh/" in path
            or path.endswith("/.bashrc")
            or path.endswith("/.zshrc")
        )

    reg.deny("write_file", _is_dangerous_write,
             reason="writing to system / home dotfiles is forbidden",
             name="write:system-paths")
    reg.deny("edit_file", _is_dangerous_write,
             reason="editing system / home dotfiles is forbidden",
             name="edit:system-paths")

    return reg


# ─────────────────────────── 接 Hook 的桥 ───────────────────────────


def make_permission_hook(registry: PermissionRegistry):
    """
    返回一个挂在 PreToolUse 事件上的 hook callback。
      - DENY : 返回 HookResult.block(...) → loop 不会执行该工具
      - ASK  : 当前 CLI 模式同样按 block 处理（reason 标 'requires confirmation'）
      - ALLOW: 返回 ok()
    """
    # 延迟 import 避免循环依赖
    from .hooks import HookResult

    def _hook(*, tool_name: str, args: dict, **_) -> HookResult:
        decision = registry.check(tool_name, args)
        if decision.permission is Permission.DENY:
            return HookResult.block(
                f"DENIED by permissions ({decision.reason})"
            )
        if decision.permission is Permission.ASK:
            # CLI 默认按 deny 处理；UI 模式可换一个 hook 自己弹框
            return HookResult.block(
                f"REQUIRES CONFIRMATION ({decision.reason}) — "
                "rerun with --yes or update permissions to allow"
            )
        return HookResult.ok()

    _hook.__name__ = "permission_check"
    return _hook


__all__ = [
    "Permission",
    "PermissionRule",
    "PermissionDecision",
    "PermissionRegistry",
    "make_default_permissions",
    "make_permission_hook",
]
