"""
Plugins 加载机制单元测试
==========================

跑法：
    python -m tests.test_plugins

策略：
  在临时目录里造 .claude/plugins/<name>/plugin.py，调用 load_plugins，
  断言 register(harness) 被调过、模块对象被加载。
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from orchestration.plugins import load_plugins, LoadedPlugin


def _mkroot() -> Path:
    return Path(tempfile.mkdtemp(prefix="plugins-test-"))


def _write_plugin(plugins_dir: Path, name: str, body: str) -> Path:
    d = plugins_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "plugin.py"
    p.write_text(body)
    return p


# ────────────────────────── happy path ──────────────────────────


def test_load_plugin_calls_register_with_harness():
    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    _write_plugin(plugins, "p1",
                  "REGISTERED_WITH = []\n"
                  "def register(harness):\n"
                  "    REGISTERED_WITH.append(harness)\n")

    fake_harness = SimpleNamespace(name="harness-instance")
    out = load_plugins(harness=fake_harness, project_root=root)

    assert len(out) == 1
    p = out[0]
    assert isinstance(p, LoadedPlugin)
    assert p.name == "p1"
    assert p.source == "project"
    assert p.registered is True
    # 模块的全局 REGISTERED_WITH 应该收到了 harness
    assert p.module.REGISTERED_WITH == [fake_harness]


def test_load_plugin_without_register_function():
    """plugin.py 没 register() 也算加载成功，registered=False。"""
    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    _write_plugin(plugins, "p_no_register", "x = 1\n")

    out = load_plugins(harness=SimpleNamespace(), project_root=root)
    assert len(out) == 1
    assert out[0].registered is False


# ────────────────────────── 容错 ──────────────────────────


def test_plugin_import_error_is_swallowed():
    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    # 故意写一个语法错的 plugin
    _write_plugin(plugins, "bad", "this is not python\ndef ::: \n")
    # 同时写一个好的，证明 bad 不影响 good
    _write_plugin(plugins, "good",
                  "def register(harness):\n    pass\n")

    out = load_plugins(harness=SimpleNamespace(), project_root=root)
    names = [p.name for p in out]
    assert "good" in names
    assert "bad" not in names


def test_plugin_register_exception_is_swallowed():
    """register(harness) 抛错时 LoadedPlugin 仍被记录，registered=False。"""
    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    _write_plugin(plugins, "throws",
                  "def register(harness):\n    raise RuntimeError('boom')\n")

    out = load_plugins(harness=SimpleNamespace(), project_root=root)
    assert len(out) == 1
    assert out[0].name == "throws"
    assert out[0].registered is False
    # 模块仍然被 import 成功了（exception 是 register 内部的）
    assert hasattr(out[0].module, "register")


# ────────────────────────── 路径 / 目录 ──────────────────────────


def test_no_plugins_dir_returns_empty():
    root = _mkroot()  # 不创建 .claude/plugins
    out = load_plugins(harness=SimpleNamespace(), project_root=root)
    # 可能 ~/.codemesh/plugins 里有，但临时 root 没有
    # 至少不抛异常
    assert isinstance(out, list)


def test_dir_without_plugin_py_skipped():
    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    (plugins / "no-plugin-file").mkdir(parents=True)
    (plugins / "no-plugin-file" / "other.py").write_text("x = 1")
    out = load_plugins(harness=SimpleNamespace(), project_root=root)
    assert all(p.name != "no-plugin-file" for p in out)


def test_extra_dirs_are_scanned():
    """extra_dirs 里的 plugin 也应被加载。"""
    extra = _mkroot() / "my-plugins"
    extra.mkdir()
    _write_plugin(extra, "extra1",
                  "REG = []\n"
                  "def register(harness):\n    REG.append('called')\n")

    fake_root = _mkroot()  # 项目级没东西
    out = load_plugins(
        harness=SimpleNamespace(),
        project_root=fake_root,
        extra_dirs=[extra],
    )
    names = [p.name for p in out]
    assert "extra1" in names


def test_duplicate_plugin_path_loaded_once():
    """同一个 plugin.py 路径出现在多个 dir 里只 import 一次。"""
    extra = _mkroot() / "shared"
    extra.mkdir()
    _write_plugin(extra, "dup",
                  "COUNT = 0\n"
                  "def register(harness):\n"
                  "    global COUNT\n    COUNT += 1\n")

    out = load_plugins(
        harness=SimpleNamespace(),
        project_root=_mkroot(),
        extra_dirs=[extra, extra],   # 同一个目录两次
    )
    dup_plugins = [p for p in out if p.name == "dup"]
    assert len(dup_plugins) == 1
    assert dup_plugins[0].module.COUNT == 1


# ────────────────────────── plugin 真实场景：注入工具 ──────────────────────────


def test_plugin_can_register_tool_via_decorator():
    """
    真实场景：plugin 在 module-level 调 @registry.register 注册一个新工具。
    加载后这工具应该出现在全局 registry 里。
    """
    from execution import registry as _global_registry

    root = _mkroot()
    plugins = root / ".claude" / "plugins"
    _write_plugin(plugins, "ttool",
                  "from execution import registry\n"
                  "@registry.register(name='_test_plugin_tool', "
                  "description='test', "
                  "parameters={'type': 'object', 'properties': {}})\n"
                  "def _ttool() -> str:\n    return 'ok'\n"
                  "def register(harness):\n    pass\n")

    try:
        load_plugins(harness=SimpleNamespace(), project_root=root)
        assert "_test_plugin_tool" in _global_registry.names
    finally:
        # 清理：把测试加进去的工具从全局注册表里移除，避免污染其他测试
        _global_registry._handlers.pop("_test_plugin_tool", None)
        _global_registry._schemas.pop("_test_plugin_tool", None)


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
    print(f"\n{len(tests) - failed}/{len(tests)} plugins tests passed.")
    if failed:
        raise SystemExit(1)
