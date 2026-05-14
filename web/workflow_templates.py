"""
内置工作流模板（v5 Phase 6.7）。

3 个 Coding-First 杀手模板（差异化护城河 #2）：

1. Aider 流水线（Architect + Editor）
   致敬 Aider architect/editor 范式。强模型规划，廉价模型实现，成本降 70%+。

2. 三角审查流水线（Planner → Coder → Reviewer）
   模仿真实 PR 流程：规划 / 实现 / 审查分离。Reviewer 工具白名单只读
   ——这是 Dify 给不了的能力差异化。

3. 多模型对比（同任务 × 3 模型 + 综合点评）
   CodeMesh "多模型对比"原始卖点的工作流化。MVP 串行执行（v5.1 加并行）。

启动时注入（idempotent）：通过 name + is_template=True 匹配；已存在则跳过。
不删旧版（避免用户已经基于模板 fork 出工作流被破坏）。
"""
from __future__ import annotations

from web.workflows_store import WorkflowsStore


# ─────────────── 模板定义 ───────────────

# system prompt 用英文写——所有 LLM 对英文 system prompt 鲁棒性更高
# （design plan §11 Q2 拍板）。UI 显示中文名 / 中文描述。

TEMPLATES: list[dict] = [
    {
        "name": "Aider 流水线（Architect + Editor）",
        "description": (
            "致敬 Aider architect/editor 范式：强模型负责架构设计（只读探查），"
            "另一个模型负责具体实现。两步分工分离思考和动手。"
        ),
        "steps": [
            {
                "name": "1. 架构设计（Architect）",
                "model": "deepseek",
                "enable_tools": ["grep_text", "read_file", "glob_files", "lsp_code"],
                "system_prompt": (
                    "You are a senior software architect. Explore the codebase with "
                    "grep_text / read_file / glob_files / lsp_code. Output a design "
                    "plan covering: (1) which files to modify, (2) function "
                    "signatures with type hints, (3) module boundaries, "
                    "(4) potential pitfalls. DO NOT write implementation code. "
                    "Be specific — name actual files and functions."
                ),
                "user_prompt": "",
            },
            {
                "name": "2. 编写代码（Editor）",
                "model": "gemini",
                "enable_tools": [
                    "grep_text", "read_file", "edit_file", "write_file",
                    "glob_files", "lsp_code",
                ],
                "system_prompt": (
                    "You are a coding assistant. Implement the architecture from "
                    "the previous step. Use edit_file for precise changes, "
                    "write_file only for new files. Keep functions small and add "
                    "docstrings. Add or extend tests under tests/ if appropriate."
                ),
                "user_prompt": "",
            },
        ],
    },
    {
        "name": "三角审查流水线（Planner → Coder → Reviewer）",
        "description": (
            "模仿真实 PR 流程：规划 / 实现 / 审查分离。Reviewer 工具白名单只读，"
            "不能改代码——展示工具白名单 per step 的护城河。"
        ),
        "steps": [
            {
                "name": "1. Planner",
                "model": "deepseek",
                "enable_tools": ["grep_text", "read_file", "glob_files", "lsp_code"],
                "system_prompt": (
                    "You plan the change. Read the task, explore relevant files, "
                    "output a step-by-step plan including affected files. NO "
                    "implementation."
                ),
                "user_prompt": "",
            },
            {
                "name": "2. Coder",
                "model": "gemini",
                "enable_tools": [
                    "grep_text", "read_file", "edit_file", "write_file",
                    "glob_files", "lsp_code",
                ],
                "system_prompt": (
                    "Implement the plan from Step 1. Be precise. Add comments "
                    "only where non-obvious. Run tests if a test runner is "
                    "available."
                ),
                "user_prompt": "",
            },
            {
                "name": "3. Reviewer",
                "model": "deepseek",
                "enable_tools": ["grep_text", "read_file", "glob_files", "lsp_code"],
                "system_prompt": (
                    "Review the code changes from Step 2. Check: "
                    "(1) correctness, (2) readability, (3) edge cases, "
                    "(4) test coverage. Output a list of concerns prefixed with "
                    "'⚠'  — or write 'LGTM' if clean. You CANNOT modify code; "
                    "your tools are read-only."
                ),
                "user_prompt": "",
            },
        ],
    },
    {
        "name": "多模型对比（同任务 × 2 模型 + 综合）",
        "description": (
            "同一任务用 DeepSeek 和 Gemini 分别实现，最后综合点评——"
            "多模型对比卖点的工作流化版本。MVP 串行执行。"
        ),
        "steps": [
            {
                "name": "1a. DeepSeek 实现",
                "model": "deepseek",
                "enable_tools": [
                    "grep_text", "read_file", "edit_file", "write_file",
                    "glob_files", "lsp_code",
                ],
                "system_prompt": (
                    "Implement the user's task using available tools. Be "
                    "self-contained — don't reference 'the other implementations'."
                ),
                "user_prompt": "",
            },
            {
                "name": "1b. Gemini 实现",
                "model": "gemini",
                "enable_tools": [
                    "grep_text", "read_file", "edit_file", "write_file",
                    "glob_files", "lsp_code",
                ],
                "system_prompt": (
                    "Implement the user's task using available tools. Be "
                    "self-contained — don't reference 'the other implementations'."
                ),
                "user_prompt": "",
            },
            {
                "name": "2. 综合点评",
                "model": "deepseek",
                "enable_tools": [],   # 纯文本生成，禁工具
                "system_prompt": (
                    "Compare the two implementations from Steps 1a and 1b. Output "
                    "a structured comparison covering: (1) correctness, "
                    "(2) code style / readability, (3) edge-case handling, "
                    "(4) recommended pick with one-paragraph reason."
                ),
                "user_prompt": "",
            },
        ],
    },
]


# ─────────────── 注入入口（idempotent） ───────────────

async def seed_templates(store: WorkflowsStore) -> int:
    """
    把内置模板写入 DB（如不存在）。返回实际新建的模板数。

    匹配规则：name + is_template=True 已存在则跳过。
    不删旧版——用户基于模板 fork 出来的工作流不应被破坏。
    """
    existing = await store.list_workflows()
    existing_template_names = {
        w["name"] for w in existing if w["is_template"]
    }

    created_count = 0
    for tpl in TEMPLATES:
        if tpl["name"] in existing_template_names:
            continue
        wf = await store.create_workflow(
            tpl["name"], tpl["description"], is_template=True,
        )
        for step_def in tpl["steps"]:
            await store.add_step(
                wf["id"],
                name=step_def["name"],
                model=step_def["model"],
                system_prompt=step_def["system_prompt"],
                user_prompt=step_def["user_prompt"],
                enable_tools=step_def["enable_tools"],
            )
        created_count += 1
    return created_count
