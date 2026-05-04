"""
Skills 加载机制单元测试
=========================

跑法：
    python -m tests.test_skills

覆盖：
  - SkillDefinition / SkillRegistry 基础 (register / get / all / __contains__)
  - render_index 渲染格式
  - _parse_skill_markdown：YAML frontmatter / 标题 fallback / 兜底
  - _load_dir：扫一个目录
  - load_skill_registry：双路径 + project 覆盖 user
  - invoke_skill 工具的正反路径
"""

import tempfile
from pathlib import Path

from orchestration.skills import (
    SkillDefinition,
    SkillRegistry,
    load_skill_registry,
    _parse_skill_markdown,
    _load_dir,
)
from execution.tools import invoke_skill, set_skill_registry


# ────────────────────────── helpers ──────────────────────────


def _mkroot() -> Path:
    return Path(tempfile.mkdtemp(prefix="skills-test-"))


def _make_skill(root: Path, name: str, content: str) -> Path:
    """在 root/<name>/SKILL.md 写一个 skill。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(content)
    return p


# ────────────────────────── SkillRegistry ──────────────────────────


def test_registry_register_and_get():
    reg = SkillRegistry()
    s = SkillDefinition(name="a", description="d", content="c", source="user")
    reg.register(s)
    assert reg.get("a") is s
    assert reg.get("missing") is None


def test_registry_overwrites_same_name():
    reg = SkillRegistry()
    reg.register(SkillDefinition(name="x", description="v1", content="", source="user"))
    reg.register(SkillDefinition(name="x", description="v2", content="", source="project"))
    assert reg.get("x").description == "v2"
    assert reg.get("x").source == "project"


def test_registry_all_returns_list():
    reg = SkillRegistry()
    reg.register(SkillDefinition(name="b", description="", content="", source="user"))
    reg.register(SkillDefinition(name="a", description="", content="", source="user"))
    names = {s.name for s in reg.all()}
    assert names == {"a", "b"}


def test_registry_contains_and_len():
    reg = SkillRegistry()
    reg.register(SkillDefinition(name="x", description="", content="", source="user"))
    assert "x" in reg
    assert "y" not in reg
    assert len(reg) == 1


def test_render_index_empty_returns_empty():
    assert SkillRegistry().render_index() == ""


def test_render_index_format():
    reg = SkillRegistry()
    reg.register(SkillDefinition(name="git-commit", description="规范的 commit", content="", source="user"))
    reg.register(SkillDefinition(name="abc", description="alphabet", content="", source="user"))
    out = reg.render_index()
    assert "<available skills>" in out
    assert "</available skills>" in out
    # 按字母排序，所以 abc 在前
    assert out.index("abc") < out.index("git-commit")
    assert "规范的 commit" in out


# ────────────────────────── _parse_skill_markdown ──────────────────────────


def test_parse_with_yaml_frontmatter():
    content = (
        "---\n"
        "name: my-skill\n"
        "description: do something useful\n"
        "---\n"
        "# 标题\n"
        "正文\n"
    )
    name, desc = _parse_skill_markdown("default", content)
    assert name == "my-skill"
    assert desc == "do something useful"


def test_parse_falls_back_to_heading():
    content = "# how-to-write-tests\n这是一个 skill\n"
    name, desc = _parse_skill_markdown("default", content)
    assert name == "how-to-write-tests"
    assert "skill" in desc


def test_parse_falls_back_to_default():
    content = ""   # 啥也没有
    name, desc = _parse_skill_markdown("fallback", content)
    assert name == "fallback"
    assert "fallback" in desc


def test_parse_handles_bad_yaml():
    content = (
        "---\n"
        "name: : : invalid\n"
        "---\n"
        "# my-title\n"
        "正文很短\n"
    )
    name, desc = _parse_skill_markdown("def", content)
    # bad yaml 被吞掉，回退到 heading
    assert name == "my-title" or name == "def"


# ────────────────────────── _load_dir ──────────────────────────


def test_load_dir_skips_files_without_skill_md():
    root = _mkroot()
    # 一个 dir 没有 SKILL.md
    (root / "noskill").mkdir()
    (root / "noskill" / "other.md").write_text("# x")
    # 一个 dir 有
    _make_skill(root, "good", "# good\n描述\n")
    out = _load_dir(root, source="user")
    names = [s.name for s in out]
    assert "good" in names
    assert "noskill" not in names


def test_load_dir_handles_missing_root():
    out = _load_dir(Path("/no/such/path/__skills_xyz"), source="user")
    assert out == []


# ────────────────────────── load_skill_registry ──────────────────────────


def test_load_skill_registry_extra_dirs_wins():
    """
    load_skill_registry 注册顺序：user → project → extra_dirs。
    后写胜出，所以 extra_dirs 里的同名 skill 覆盖前两个。
    （生产场景里 extra_dirs 是测试 / 临时插件目录，应当能覆盖默认目录。）
    """
    # 用 YAML frontmatter 显式钉住 name，避免被 # 标题改写
    project_root = _mkroot()
    proj_skills = project_root / ".claude" / "skills"
    proj_skills.mkdir(parents=True)
    _make_skill(
        proj_skills, "deploy",
        "---\nname: deploy\ndescription: from project\n---\n项目级版本\n",
    )

    user_root = _mkroot()
    user_skills = user_root / "skills"
    user_skills.mkdir(parents=True)
    _make_skill(
        user_skills, "deploy",
        "---\nname: deploy\ndescription: from user\n---\n用户级版本\n",
    )

    reg = load_skill_registry(project_root=project_root, extra_dirs=[user_skills])
    deploy = reg.get("deploy")
    assert deploy is not None
    # extra_dirs 是最后写的，它胜出（"from user"）
    assert deploy.description == "from user"
    assert "用户级版本" in deploy.content


def test_load_skill_registry_project_beats_user_via_default_paths():
    """
    在没有 extra_dirs 时：user_skills_dir → project_skills_dir，
    项目级（后注册）覆盖用户级。
    用 monkeypatch 重写 user_skills_dir() 才能在测试里隔离 ~/.codemesh/skills。
    """
    import orchestration.skills as skills_mod

    project_root = _mkroot()
    proj_skills = project_root / ".claude" / "skills"
    proj_skills.mkdir(parents=True)
    _make_skill(
        proj_skills, "policy",
        "---\nname: policy\ndescription: project version\n---\n项目级\n",
    )

    user_root = _mkroot() / "skills"
    user_root.mkdir(parents=True)
    _make_skill(
        user_root, "policy",
        "---\nname: policy\ndescription: user version\n---\n用户级\n",
    )

    original = skills_mod.user_skills_dir
    skills_mod.user_skills_dir = lambda: user_root  # type: ignore[assignment]
    try:
        reg = load_skill_registry(project_root=project_root)
    finally:
        skills_mod.user_skills_dir = original  # type: ignore[assignment]

    p = reg.get("policy")
    assert p is not None
    assert p.description == "project version"  # 项目级胜出
    assert "项目级" in p.content


def test_load_skill_registry_no_skills_dir():
    """完全没 .claude/skills 也不应崩。"""
    project_root = _mkroot()
    reg = load_skill_registry(project_root=project_root)
    # 可能 ~/.codemesh/skills 里有，所以不严格断言为空
    assert isinstance(reg, SkillRegistry)


# ────────────────────────── invoke_skill 工具 ──────────────────────────


def test_invoke_skill_returns_full_content():
    reg = SkillRegistry()
    reg.register(SkillDefinition(
        name="my-test-skill",
        description="d",
        content="full SKILL.md text here",
        source="user",
    ))
    set_skill_registry(reg)
    out = invoke_skill("my-test-skill")
    assert "full SKILL.md text" in out


def test_invoke_skill_unknown_returns_error():
    reg = SkillRegistry()
    set_skill_registry(reg)
    out = invoke_skill("ghost")
    assert "[ERROR]" in out and "not found" in out


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
    print(f"\n{len(tests) - failed}/{len(tests)} skills tests passed.")
    if failed:
        raise SystemExit(1)
