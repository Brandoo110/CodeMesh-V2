"""
Plugins 加载机制（Harness 编排层）
=====================================

【Plugin 是什么】
一个目录里塞一个 plugin.py，里面定义一个 register() 函数。Harness 启动时
扫两条路径自动加载，让第三方 / 用户能在不动核心代码的前提下叠加：
  - 新工具（往 execution.registry 里 @register）
  - 新 hook（往 harness.hooks 里 register）
  - 新 skills（往 harness.skills 里 register）
  - 新 permission rules（往 harness.permissions 里 add）

【目录约定】
  <root>/.claude/plugins/<name>/plugin.py    项目级（跟仓库走）
  ~/.codemesh/plugins/<name>/plugin.py        用户级（跨项目共享）

每个 plugin.py 至少要有一个 register(harness) 函数：

    # .claude/plugins/my-plugin/plugin.py
    from execution import registry as tools

    @tools.register(name="my_tool", description="...", parameters={...})
    def my_tool(arg: str) -> str:
        return "ok"

    def register(harness):
        # 可选：拿到 harness 实例做更多事
        # harness.permissions.deny("bash_exec", lambda a: ...)
        # harness.hooks.register(HookEvent.SESSION_START, ...)
        pass

【为什么用 importlib 而不是 pkgutil / entry_points】
  - importlib.util.spec_from_file_location 直接给路径就能 import
  - 不要求 plugin 是合法 Python 包（不需要 __init__.py / setup.py）
  - 用户体验：把一个 .py 文件丢进去就生效

【失败容错】
  单个 plugin 加载失败（语法错 / register 抛错）不应该让整个 Harness 起不来。
  失败的 plugin 打印 warning 跳过；其他 plugin 继续。

【面试讲法】
"Q: 为什么不用 setuptools entry_points？"
→ entry_points 要求 plugin 是 pip 装包；本项目目标是"丢一个 .py 进 .claude/plugins/
  就能用"。entry_points 适合发到 PyPI 的成熟生态，不适合个人配置 / 团队约定。

"Q: 安全？"
→ Plugin 是任意 Python 代码，相当于 import。生产场景应该走签名 / 沙箱（OpenHarness
  permissions/ + sandbox/ 那一套）；本项目教学版假设用户信任自己 commit 进仓库的代码。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


logger = logging.getLogger(__name__)


@dataclass
class LoadedPlugin:
    """一个加载成功的 plugin 的元数据。"""
    name: str
    path: Path
    source: str        # 'project' / 'user'
    module: Any        # 已 import 进来的 module 对象
    registered: bool   # register(harness) 是否成功调过


def project_plugins_dir(project_root: Path = Path(".")) -> Path:
    return project_root / ".claude" / "plugins"


def user_plugins_dir() -> Path:
    return Path.home() / ".codemesh" / "plugins"


def load_plugins(
    harness: Any,
    project_root: Path = Path("."),
    extra_dirs: Optional[Iterable[Path]] = None,
) -> list[LoadedPlugin]:
    """
    扫两条标准路径 + 可选额外路径，import 每个 plugin.py 并调它的 register(harness)。

    Args:
        harness     : Harness 实例，传给 register(harness)
        project_root: 项目根，默认 cwd
        extra_dirs  : 额外要扫的目录（测试 / 自定义场景）

    Returns:
        加载成功的 LoadedPlugin 列表（失败的不在列表里，会打 warning）。
    """
    out: list[LoadedPlugin] = []
    seen: set[Path] = set()

    dirs: list[tuple[Path, str]] = [
        (user_plugins_dir(), "user"),
        (project_plugins_dir(project_root), "project"),
    ]
    if extra_dirs:
        for d in extra_dirs:
            dirs.append((Path(d), "user"))

    for directory, source in dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            plugin_file = child / "plugin.py"
            if not plugin_file.exists():
                continue
            if plugin_file.resolve() in seen:
                continue
            seen.add(plugin_file.resolve())
            loaded = _load_one(plugin_file, name=child.name, source=source, harness=harness)
            if loaded is not None:
                out.append(loaded)
    return out


def _load_one(
    plugin_file: Path,
    *,
    name: str,
    source: str,
    harness: Any,
) -> Optional[LoadedPlugin]:
    """加载单个 plugin.py。失败返回 None 并打 warning。"""
    try:
        spec = importlib.util.spec_from_file_location(
            f"codemesh_plugin_{name}", plugin_file,
        )
        if spec is None or spec.loader is None:
            logger.warning("plugin %s: invalid spec", name)
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("plugin %s: import failed: %s", name, exc)
        return None

    registered = False
    register_fn: Optional[Callable[[Any], None]] = getattr(module, "register", None)
    if callable(register_fn):
        try:
            register_fn(harness)
            registered = True
        except Exception as exc:
            logger.warning("plugin %s: register(harness) failed: %s", name, exc)

    return LoadedPlugin(
        name=name,
        path=plugin_file,
        source=source,
        module=module,
        registered=registered,
    )


__all__ = [
    "LoadedPlugin",
    "load_plugins",
    "project_plugins_dir",
    "user_plugins_dir",
]
