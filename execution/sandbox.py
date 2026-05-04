"""
沙箱：危险命令检测（Harness 执行层）
======================================

【为什么需要沙箱】
Agent 能执行 shell，但模型不一定"清楚"什么是危险操作。
比如用户说"清理一下临时文件"，模型可能生成 `rm -rf /tmp/*` —— 但写错成 `rm -rf /`
就炸了。沙箱层就是拦在「工具执行」前的安全网。

【两种策略】
  1. 黑名单：列举危险命令/模式，匹配到就拦
  2. 白名单：只允许明确列出的命令
黑名单好写但可能漏（Agent 总能找到绕过的写法）；
白名单安全但限制大，许多合法操作被误伤。
CodeMesh 先用黑名单（教学版够用），生产环境推荐上容器化沙箱（Docker/Firejail/gVisor）。

【面试点】
"为什么不直接让模型自己判断危不危险？"
→ 模型的「安全感」不可控、不可审计。安全策略必须在代码里写死，
  由工程师 review。把安全交给模型等于没有安全。

"Claude Code 是怎么做的？"
→ 多层防御：permission system（每个工具有权限标签）、pre-tool hook、
  用户确认对话框。CodeMesh 这里的 sandbox 相当于 Claude Code 里的
  permission deny + 二次确认。
"""

import re


# 危险命令模式列表。命中任何一条就认为危险。
# 用 re.compile 预编译，匹配快。
DANGEROUS_PATTERNS = [
    # rm 必须带 -r/-R 才能递归删目录，没有 r 的 rm 只能删单文件，风险低很多。
    # 所以只在出现 r/R 标志且目标是 / / ~ / * 时才拦截。
    re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\*)"),
    re.compile(r"\bsudo\b"),                         # 提权
    re.compile(r"\bmkfs\b"),                         # 格式化磁盘
    re.compile(r"\bdd\s+if="),                        # dd 直接写块设备
    re.compile(r":\(\)\s*\{.*\}\s*;\s*:"),           # fork bomb
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),  # SQL 删表
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\s+/"),           # 危险权限
    re.compile(r">\s*/dev/sd[a-z]"),                  # 直接写物理盘
]


class SandboxViolation(Exception):
    """危险命令被拦截时抛的异常。执行层捕获后返回给模型，让它换个写法。"""

    def __init__(self, command: str, reason: str):
        super().__init__(f"Blocked: {reason} | command={command!r}")
        self.command = command
        self.reason = reason


def check_command(cmd: str) -> None:
    """
    检查命令是否危险。危险则抛 SandboxViolation，安全则无返回值。

    这里用"抛异常"而不是"返回 bool"，因为：
      1. 调用方忘记检查返回值时不会静默放行（异常会中断执行）
      2. 异常自带 reason 信息，方便传给模型做二次尝试
    """
    for pat in DANGEROUS_PATTERNS:
        if pat.search(cmd):
            raise SandboxViolation(
                command=cmd,
                reason=f"matched dangerous pattern: {pat.pattern}",
            )
