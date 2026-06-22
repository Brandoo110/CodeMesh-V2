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

READ_TOOLS = ["grep_text", "read_file", "glob_files", "lsp_code", "web_search", "fetch_url"]
WRITE_TOOLS = [
    "grep_text", "read_file", "edit_file", "write_file",
    "glob_files", "lsp_code", "web_search", "fetch_url",
]

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
                "enable_tools": READ_TOOLS,
                "system_prompt": (
                    "你是一名资深软件架构师。用 grep_text / read_file / glob_files / "
                    "lsp_code 工具探查代码库，输出一份设计方案，包含：(1) 要修改"
                    "哪些文件，(2) 函数签名（含类型注解），(3) 模块边界，"
                    "(4) 可能的坑。"
                    "**不要写实现代码**——只规划。回答尽量具体，要点名真实存在"
                    "的文件和函数。"
                ),
                "user_prompt": "",
            },
            {
                "name": "2. 编写代码（Editor）",
                "model": "gemini",
                "enable_tools": WRITE_TOOLS,
                "system_prompt": (
                    "你是编码助手。根据上一步的架构方案具体实现代码。"
                    "用 edit_file 做精确修改，write_file 仅用于新建文件。"
                    "函数保持短小并加 docstring；如有合适场景，"
                    "在 tests/ 下新增或扩展测试。"
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
                "model": "gemini",
                "enable_tools": READ_TOOLS,
                "system_prompt": (
                    "你负责规划。读懂用户任务，探查相关文件，输出一份分步计划，"
                    "包含每步要改的文件和动作。**不要写实现代码**。"
                ),
                "user_prompt": "",
            },
            {
                "name": "2. Coder",
                "model": "deepseek",
                "enable_tools": WRITE_TOOLS,
                "system_prompt": (
                    "按上一步 Planner 给的计划具体实现代码。要精准——只在不"
                    "显然处加注释。如果有测试运行器，跑一下测试。"
                ),
                "user_prompt": "",
            },
            {
                "name": "3. Reviewer",
                "model": "minimax",
                "enable_tools": READ_TOOLS,
                "system_prompt": (
                    "审查上一步 Coder 改的代码。检查 4 个维度：(1) 正确性、"
                    "(2) 可读性、(3) 边界情况、(4) 测试覆盖。"
                    "不要输出 <think>。请用自然语言说明当前交付是否达标；"
                    "如果有阻塞问题，明确说明为什么还不能交付、缺什么、"
                    "建议 Coder 如何补。"
                    "**你无法改代码**——工具是只读的。"
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
                "enable_tools": WRITE_TOOLS,
                "system_prompt": (
                    "用可用的工具完成用户的任务。要自包含——不要引用"
                    "「另一个实现」。"
                ),
                "user_prompt": "",
            },
            {
                "name": "1b. Gemini 实现",
                "model": "gemini",
                "enable_tools": WRITE_TOOLS,
                "system_prompt": (
                    "用可用的工具完成用户的任务。要自包含——不要引用"
                    "「另一个实现」。"
                ),
                "user_prompt": "",
            },
            {
                "name": "2. 综合点评",
                "model": "deepseek",
                "enable_tools": [],   # 纯文本生成，禁工具
                "system_prompt": (
                    "对比 Step 1a 和 Step 1b 两份实现，输出一个结构化对比，"
                    "覆盖：(1) 正确性、(2) 代码风格 / 可读性、"
                    "(3) 边界处理、(4) 推荐选哪份（用一段话说明理由）。"
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
    existing_templates = {
        w["name"]: w for w in existing if w["is_template"]
    }

    created_count = 0
    for tpl in TEMPLATES:
        if tpl["name"] in existing_templates:
            await _sync_existing_template_defaults(
                store, existing_templates[tpl["name"]]["id"], tpl
            )
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


async def _sync_existing_template_defaults(
    store: WorkflowsStore,
    workflow_id: str,
    tpl: dict,
) -> None:
    """模板已存在时同步默认工具和模型，不碰用户 fork 出来的工作流。"""
    existing_steps = await store.get_steps(workflow_id)
    by_name = {step["name"]: step for step in existing_steps}
    for step_def in tpl["steps"]:
        step = by_name.get(step_def["name"])
        if not step:
            continue
        patch = {}
        if step["enable_tools"] != step_def["enable_tools"]:
            patch["enable_tools"] = step_def["enable_tools"]
        if step["model"] != step_def["model"]:
            patch["model"] = step_def["model"]
        if patch:
            await store.update_step(
                step["id"],
                **patch,
            )
