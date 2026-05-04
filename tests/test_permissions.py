"""
权限系统单元测试
==================

跑法：
    python -m tests.test_permissions

覆盖：
  - Permission enum
  - PermissionRule.matches（正常 / matcher 抛错时不传染）
  - PermissionRegistry：注册顺序、首个命中赢、默认决定
  - 快捷接口 allow / deny / ask
  - make_default_permissions：force-push / pip install / 写 /etc 等
  - make_permission_hook：DENY → block, ASK → block, ALLOW → ok
"""

from orchestration.permissions import (
    Permission,
    PermissionRule,
    PermissionRegistry,
    make_default_permissions,
    make_permission_hook,
)
from orchestration.hooks import HookResult


# ────────────────────────── enum / dataclass ──────────────────────────


def test_permission_enum_values():
    assert Permission.ALLOW.value == "allow"
    assert Permission.ASK.value == "ask"
    assert Permission.DENY.value == "deny"


def test_rule_matches_returns_bool():
    rule = PermissionRule(
        permission=Permission.DENY,
        matcher=lambda t, a: t == "x",
    )
    assert rule.matches("x", {}) is True
    assert rule.matches("y", {}) is False


def test_rule_matcher_swallows_exceptions():
    """matcher 抛错时不应让权限检查崩，按"不命中"处理。"""
    def boom(t, a):
        raise ValueError("oops")
    rule = PermissionRule(permission=Permission.DENY, matcher=boom)
    assert rule.matches("x", {}) is False


# ────────────────────────── PermissionRegistry ──────────────────────────


def test_default_decision_when_no_rules():
    reg = PermissionRegistry()
    d = reg.check("anything", {})
    assert d.permission is Permission.ALLOW
    assert d.allowed is True


def test_default_can_be_overridden():
    reg = PermissionRegistry(default=Permission.DENY)
    d = reg.check("anything", {})
    assert d.permission is Permission.DENY
    assert d.denied is True


def test_first_matching_rule_wins():
    """注册顺序很重要：第一个命中的规则赢。"""
    reg = PermissionRegistry()
    reg.allow("bash_exec")
    reg.deny("bash_exec")  # 这条被前一条遮蔽
    d = reg.check("bash_exec", {})
    assert d.permission is Permission.ALLOW


def test_pattern_matching_with_wildcards():
    reg = PermissionRegistry(default=Permission.DENY)
    reg.allow("read_*")
    assert reg.check("read_file", {}).permission is Permission.ALLOW
    assert reg.check("read_dir", {}).permission is Permission.ALLOW
    assert reg.check("write_file", {}).permission is Permission.DENY


def test_arg_predicate_filtering():
    reg = PermissionRegistry()
    reg.deny("bash_exec",
             lambda a: "rm -rf" in (a.get("cmd") or ""))
    assert reg.check("bash_exec", {"cmd": "ls"}).permission is Permission.ALLOW
    assert reg.check("bash_exec", {"cmd": "rm -rf /tmp"}).permission is Permission.DENY


def test_ask_permission_recognised():
    reg = PermissionRegistry()
    reg.ask("bash_exec",
            lambda a: "git push" in (a.get("cmd") or ""))
    d = reg.check("bash_exec", {"cmd": "git push origin main"})
    assert d.permission is Permission.ASK


def test_check_includes_rule_reference():
    reg = PermissionRegistry()
    reg.deny("bash_exec", reason="too dangerous", name="bash:dangerous")
    d = reg.check("bash_exec", {})
    assert d.rule is not None
    assert d.rule.name == "bash:dangerous"


# ────────────────────────── make_default_permissions ──────────────────────────


def test_default_permissions_blocks_force_push():
    reg = make_default_permissions()
    d = reg.check("bash_exec", {"cmd": "git push --force origin main"})
    assert d.permission is Permission.ASK
    d2 = reg.check("bash_exec", {"cmd": "git push -f origin main"})
    assert d2.permission is Permission.ASK


def test_default_permissions_blocks_pip_install():
    reg = make_default_permissions()
    d = reg.check("bash_exec", {"cmd": "pip install requests"})
    assert d.permission is Permission.ASK


def test_default_permissions_allows_normal_bash():
    reg = make_default_permissions()
    d = reg.check("bash_exec", {"cmd": "ls -la"})
    assert d.permission is Permission.ALLOW
    d2 = reg.check("bash_exec", {"cmd": "git status"})
    assert d2.permission is Permission.ALLOW


def test_default_permissions_blocks_write_to_etc():
    reg = make_default_permissions()
    d = reg.check("write_file", {"path": "/etc/hosts", "content": ""})
    assert d.permission is Permission.DENY


def test_default_permissions_blocks_write_to_ssh_dir():
    reg = make_default_permissions()
    d = reg.check("write_file", {"path": "/home/user/.ssh/id_rsa", "content": ""})
    assert d.permission is Permission.DENY


def test_default_permissions_allows_normal_write():
    reg = make_default_permissions()
    d = reg.check("write_file", {"path": "src/main.py", "content": "x"})
    assert d.permission is Permission.ALLOW


def test_default_permissions_blocks_edit_to_bashrc():
    reg = make_default_permissions()
    d = reg.check("edit_file",
                  {"path": "/home/user/.bashrc", "old_string": "x", "new_string": "y"})
    assert d.permission is Permission.DENY


# ────────────────────────── make_permission_hook ──────────────────────────


def test_hook_allows_safe_call():
    reg = make_default_permissions()
    hook = make_permission_hook(reg)
    out = hook(tool_name="read_file", args={"path": "x.py"})
    assert isinstance(out, HookResult)
    assert out.blocked is False


def test_hook_blocks_denied_call():
    reg = make_default_permissions()
    hook = make_permission_hook(reg)
    out = hook(tool_name="write_file", args={"path": "/etc/passwd", "content": ""})
    assert out.blocked is True
    assert "DENIED" in out.reason


def test_hook_blocks_ask_call_in_cli_mode():
    reg = make_default_permissions()
    hook = make_permission_hook(reg)
    out = hook(tool_name="bash_exec", args={"cmd": "git push --force origin main"})
    assert out.blocked is True
    assert "CONFIRMATION" in out.reason


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
    print(f"\n{len(tests) - failed}/{len(tests)} permissions tests passed.")
    if failed:
        raise SystemExit(1)
