"""
输出验证（Harness 反馈层）
=============================

【为什么需要单独的验证层】
模型输出可能：
  - 违反结构（路由决策该返回 JSON，结果返回了自然语言解释）
  - 违反内容（代码里混入了泄露密码的 print）
  - 违反业务规则（生成了不该写的文件路径）

路由器用 PydanticAI 已经做了一部分结构验证，但 Agent Loop 里
模型的自由输出还需要额外校验。这里提供一些通用 validator 工具函数。

【和 PydanticAI 的关系】
PydanticAI 是"主动验证"：模型必须按你的 Pydantic 模型输出。
这里的 validator 更像"被动验证"：模型说啥就接收啥，再跑一些 check。

两者配合使用：结构化决策用 PydanticAI，自由文本用这里的 validator。

【这个模块目前提供】
  - check_no_secrets   : 检查输出中是否泄露常见密钥格式
  - check_path_safe    : 检查写文件路径是否逃出项目目录
这些都是非常"小而实用"的守门员函数。
"""

import re
from pathlib import Path


# 常见密钥格式（简化版，真实生产环境请上 gitleaks / trufflehog）
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),          # OpenAI / DeepSeek 风格
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS Access Key
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"(?i)password\s*=\s*['\"][^'\"]{6,}['\"]"),
]


def check_no_secrets(text: str) -> list[str]:
    """
    返回匹配到的密钥列表。空列表表示安全。
    """
    hits: list[str] = []
    for pat in SECRET_PATTERNS:
        for m in pat.findall(text):
            hits.append(m if isinstance(m, str) else str(m))
    return hits


def check_path_safe(path: str, root: Path | None = None) -> bool:
    """
    检查 path 是不是逃出了 root（默认当前工作目录）。
    防止 Agent 误把文件写到 ~/.ssh/ 或 /etc/ 这种地方。

    实现思路：把 path 解析成绝对路径，看它是不是以 root 开头。
    用 Path.resolve() 会处理 ../ 这种相对路径逃逸。
    """
    root = (root or Path.cwd()).resolve()
    try:
        target = Path(path).resolve()
    except Exception:
        return False
    # Python 3.9+ 的 is_relative_to 方法，判断 target 是否在 root 内
    try:
        return target.is_relative_to(root)
    except AttributeError:
        # 兼容老版本
        return str(target).startswith(str(root))
