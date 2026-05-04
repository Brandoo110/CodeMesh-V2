"""
Skills 加载机制（Harness 编排层）
====================================

【Skill 是什么】
Anthropic 在 Claude Code / Claude API 推出的"按需加载的小专家文档"。
每个 skill 是一个目录，里面有一个 `SKILL.md`：

    .claude/skills/git-commit/
      ├─ SKILL.md          ← 用 markdown 写的"什么时候用、怎么做"
      └─ scripts/...       ← 可选的辅助文件

SKILL.md 头部可以有 YAML frontmatter：

    ---
    name: git-commit
    description: 写规范的 commit message
    ---
    # 怎么写好的 commit message
    ...

【Trigger 模型】
Anthropic 的设计：把所有 skill 的 (name, description) 放进 system prompt 当
"目录"，模型看到任务相关时主动调 invoke_skill 工具拿全文。这样：
  - 默认只占 ~50 字 / skill 的 token
  - 真用的时候才把整篇 SKILL.md（可能几千字）拉进来

【目录约定（参考 HKUDS/OpenHarness skills/loader.py）】
  ./.claude/skills/<name>/SKILL.md     ← 项目级（跟仓库走，commit 上去）
  ~/.codemesh/skills/<name>/SKILL.md   ← 用户级（家目录，跨项目共享）

项目级在用户级之前注册；同名时项目级覆盖用户级（项目意图优先）。

【面试讲法】
"Q: skill 和 prompt 有什么区别？"
→ prompt 是死的文字，每次都全量发；skill 是带 metadata 的可寻址条目，
  模型按需调用。模型本身决定"现在要不要拉 X skill"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillDefinition:
    """一个加载好的 skill。"""
    name: str
    description: str
    content: str        # 完整 SKILL.md 文本（含 frontmatter）
    source: str         # 'project' / 'user'
    path: Optional[str] = None


class SkillRegistry:
    """skill 注册表。同名后注册的覆盖前者（让 project 覆盖 user）。"""

    def __init__(self) -> None:
        self._items: dict[str, SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        self._items[skill.name] = skill

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._items.get(name)

    def all(self) -> list[SkillDefinition]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def render_index(self) -> str:
        """
        把 skill 目录渲染成短列表，塞 system prompt 里当 "可用 skill 索引"。
        每行一个 skill，模型看到了就知道"我可以调 invoke_skill('git-commit')"。
        """
        if not self._items:
            return ""
        lines = ["<available skills>"]
        for s in sorted(self._items.values(), key=lambda x: x.name):
            lines.append(f"- {s.name}: {s.description}")
        lines.append("</available skills>")
        return "\n".join(lines)


# ─────────────────────────── 默认目录 ───────────────────────────


def project_skills_dir(project_root: Path = Path(".")) -> Path:
    return project_root / ".claude" / "skills"


def user_skills_dir() -> Path:
    return Path.home() / ".codemesh" / "skills"


# ─────────────────────────── 加载 ───────────────────────────


def load_skill_registry(
    project_root: Path = Path("."),
    extra_dirs: Optional[Iterable[Path]] = None,
) -> SkillRegistry:
    """
    扫两条标准路径 + 可选额外路径，把 SKILL.md 都加载进 registry。
      1. 用户级: ~/.codemesh/skills/<name>/SKILL.md   (source='user')
      2. 项目级: <root>/.claude/skills/<name>/SKILL.md  (source='project')
      3. extra_dirs：测试 / 自定义场景注入
    后注册的覆盖前注册的，所以 project > user。
    """
    registry = SkillRegistry()

    for d in [user_skills_dir(), project_skills_dir(project_root)]:
        for s in _load_dir(d, source=("project" if "project" in str(d) or ".claude" in str(d) else "user")):
            registry.register(s)

    if extra_dirs:
        for d in extra_dirs:
            for s in _load_dir(Path(d), source="user"):
                registry.register(s)

    return registry


def _load_dir(directory: Path, *, source: str) -> list[SkillDefinition]:
    """
    一个 root 下，所有 <name>/SKILL.md 各加载成一个 SkillDefinition。
    root 不存在 → 返回 []。
    """
    if not directory.exists() or not directory.is_dir():
        return []
    out: list[SkillDefinition] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        skill_path = child / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            content = skill_path.read_text(encoding="utf-8")
        except OSError:
            continue
        name, description = _parse_skill_markdown(child.name, content)
        out.append(SkillDefinition(
            name=name,
            description=description,
            content=content,
            source=source,
            path=str(skill_path),
        ))
    return out


def _parse_skill_markdown(default_name: str, content: str) -> tuple[str, str]:
    """
    从 SKILL.md 抽 (name, description)：
      1. 先试 YAML frontmatter (--- ... ---)
      2. 否则用第一个 # 标题做 name，第一段非空非 # 行做 description
      3. 都不行就 (default_name, "Skill: <default_name>")
    """
    name = default_name
    description = ""

    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            try:
                import yaml  # 延迟 import，没装也不挂掉
                meta = yaml.safe_load(content[4:end])
                if isinstance(meta, dict):
                    if isinstance(meta.get("name"), str) and meta["name"].strip():
                        name = meta["name"].strip()
                    if isinstance(meta.get("description"), str) and meta["description"].strip():
                        description = meta["description"].strip()
            except Exception:
                logger.debug("YAML frontmatter parse failed for %s", default_name)

    if not description:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                if not name or name == default_name:
                    name = stripped[2:].strip() or default_name
                continue
            if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                description = stripped[:200]
                break

    if not description:
        description = f"Skill: {name}"
    return name, description


__all__ = [
    "SkillDefinition",
    "SkillRegistry",
    "load_skill_registry",
    "project_skills_dir",
    "user_skills_dir",
]
