# CodeMesh Multi-Model Workflow 设计方案 v1（draft）

> **Status**: Draft — 等用户 review 拍板后落地
>
> **作者**: Brandoo110 + Claude Code 协作
> **日期**: 2026-05-14
> **关联**: ADR-0007（待写）/ ADR-0005（多模型差异化）/ ADR-0001（agentic search）/ ADR-0006（Web UI 栈）
> **代码基础**: 完全复用 `harness.py` / `execution/tools.py` / `web/` 现有能力；本方案只新增 `web/workflows_store.py` / `web/workflow_orchestrator.py` / `web/routes/workflows.py` 后端三件套，前端新增 `WorkflowsView` 一套。

---

## 0. 定位（已拍板）

### 0.1 Slogan

> **Dify-style workflow for Claude Code-style coding agents.**

### 0.2 不是什么（划清边界）

| 不做 | 原因 |
|------|------|
| 通用 AI 应用编排（聊天机器人 / RAG / 客服） | Dify v1.5+ / Coze / Flowise 已经做透——Variable Inspect / Last Run / Human Input 暂停 / 断点重跑都齐全。我们硬刚等于死 |
| 节点式 DAG（n8n / Flowise 的拖拽流程图） | 引入 React Flow / xyflow 复杂度，且大部分代码工作流是线性的 |
| 多用户协作 / 团队管理 / 鉴权 | MVP 单用户 localhost，对齐 Web UI MVP |
| 工作流 marketplace / 分享导出 | 后续考虑，MVP 砍掉 |
| 变量 / 占位符 / 条件分支 | MVP 不做，v5.1 加 |

### 0.3 是什么

**Coding Agent 的工作流编辑器**。每个"步骤"是一个挂着代码工具（grep/read/write/edit/glob/bash）的 mini Claude Code，可独立选模型、可勾选启用哪些工具、步骤间数据流可见（含 file diff）。

### 0.4 目标用户

1. **对 Claude Code 上瘾的开发者**：想要 Claude Code 体验，又想用 DeepSeek/Qwen 省钱
2. **多模型分工探索者**：相信"架构师用强模型 + 编码用廉价模型"的成本结构（Aider architect/editor 的可视化升级）
3. **AI 工程面试者**：拿这个项目讲"多模型协作"的故事

---

## 1. 市场背景与差异化

### 1.1 调研结论（2026-05-14 市场扫描）

调研对象：Dify / Coze / Flowise / Langflow / n8n / Wordware / Vellum / LangGraph / CrewAI / OpenAI Agents SDK / Cursor 3.0 / Cline / Aider / Devin / GitHub Copilot Workspace / GitHub Copilot CLI（subagent 架构）/ OpenRouter / LiteLLM / Portkey。

| 类别 | 工作流 + 多模型 + 信息流可见 | 代码工具语义（grep/edit/exec） |
|------|------|------|
| 通用 AI 工作流（Dify / Coze / Flowise / Vellum） | ✅ 全做齐 | ❌ 只有 Code 沙箱节点 |
| 多 Agent 框架（LangGraph / CrewAI / AutoGen） | ✅ 编排能力强 | ❌ 无 UI，需自写工具 |
| Coding Agent（Cursor / Cline / Aider） | ❌ 单循环 Agent | ✅ 全套 |
| GitHub Copilot CLI（2026 新出） | ⚠️ subagent 多模型并行 | ✅ | **最接近的对手，闭源** |

### 1.2 真正的空白窗口

**Dify 的可视化工作流 × Claude Code 的代码工具语义** = 没人占的格子。

- Dify 给你"调用 API"的能力，不给你"在仓库里 grep 然后改文件"的能力
- Cursor/Claude Code/Cline 有完整 coding 工具，但没有可视化工作流
- Aider architect/editor 有多模型分工，但只有两步固定模板

### 1.3 3 个差异化护城河（必须做到位）

| 护城河 | 别人为什么做不了 |
|------|---------------|
| **工具白名单 per step** | Dify 的工具是 HTTP/Plugin，没有 grep/read/write 的语义；做范围限制（reviewer 只能 read+grep，coder 全开）是代码 Agent 特有需求 |
| **3 个预置 Coding 模板** | 通用工作流平台不会预置"Architect→Coder→Reviewer"这种代码场景模板 |
| **Diff-aware 数据流** | 步骤间流转的不是泛型变量（Dify 的 Variable Inspect），而是 **file diff**——UI 做 side-by-side 呈现、可一键 revert 某节点的修改。这是代码工作流独有的可视化 |

### 1.4 karpathy 4 条对照

| 原则 | Workflow 设计应用 |
|------|------------------|
| Think Before Coding | 这份方案 + ADR-0007 + 市场调研先写完再开 Phase 6.1 |
| Simplicity First | MVP 砍掉变量 / 条件分支 / DAG / marketplace；线性卡片堆叠先跑通 |
| Surgical Changes | 复用 `harness.run_stream_full`（Phase 3 已建），不改 `execution/loop.py`；前端复用 ModelSelector / ToolCallCard / MessageBubble |
| Goal-Driven Execution | 每个 Phase 有可验证 demo（见 §10）；MVP 收尾必须能演示 Aider 流水线模板 |

---

## 2. 核心概念

### 2.1 概念模型

```
Workflow（工作流模板）
  ├── Step 1 (name / model / system_prompt / user_prompt / enable_tools)
  ├── Step 2 ...
  └── Step N
        ↓ 执行（点 ▶ Run）
WorkflowRun（一次执行实例）
  ├── StepResult 1 (status / output / cost / duration)
  ├── StepResult 2 ...
  └── StepResult N
```

### 2.2 关键术语

| 术语 | 定义 |
|------|------|
| **Workflow** | 用户保存的工作流模板（含 N 个 Step 定义），可反复执行 |
| **Step** | 工作流中的单个步骤，绑定 1 个模型 + 1 套工具白名单 + system/user prompt |
| **Run** | 一次完整执行实例（Workflow → Run），含每步的 StepResult |
| **StepResult** | 单步执行结果（status / output / model_used / cost / duration） |
| **Tool Allowlist** | 该步骤启用的工具集合（如 `["grep", "read"]` 或 `["*"]` 全开） |
| **Carryover Context** | 上一步 output 自动拼入下一步 user_prompt 前的隐式数据流 |
| **Diff Slice** | 从 StepResult 中提取的 file diff 片段，供下一步引用或 UI 高亮 |

### 2.3 步骤间数据流（MVP 用隐式）

```
Step 1 output → 自动拼接到 Step 2 prompt 前：

  "上一步输出：
   {step1.output}
   
   {step2.user_prompt}"
```

**v5.1 升级方向**：显式占位符 `{{step1.output}}` / `{{step1.diff}}` / `{{step1.tool_calls.0.result}}`，允许跳步引用 / 拼接多步。

### 2.4 工具白名单语义（核心差异化）

每个 Step 的 `enable_tools` 字段是一个工具名数组：

```python
# 全开（默认）
enable_tools = ["*"]

# 只读模式（reviewer 步骤）
enable_tools = ["grep_text", "read_file", "glob_files", "lsp_code"]

# 完全禁用（纯文本生成步骤）
enable_tools = []

# 自定义（coder 步骤可写不可 exec）
enable_tools = ["grep_text", "read_file", "edit_file", "write_file"]
```

后端在创建 step 用的临时 harness 时，从 `execution/tools.py` 的 registry 中**按白名单 filter** 注册——模型 list_tools 时就只能看见允许的工具，达到能力隔离。

---

## 3. 用户视角（界面）

### 3.1 顶栏新增 Workflows tab

```
┌────────────────────────────────────────────────────────────────────────┐
│ CodeMesh   [ Chat ]  [ Stats ]  [ Workflows ● ]      [Model: 自动选]   │
└────────────────────────────────────────────────────────────────────────┘
```

- 顶栏第 3 个 tab，复用现有 segmented control 样式
- view === "workflows" 时隐藏右侧 ModelSelector（模型在每个 step 内选）

### 3.2 Workflows View 整体布局

```
┌──────────────────────────────────────────────────────────────────────────┐
│  WorkflowsView                                                          │
│  ┌──────────────┐ ┌────────────────────────────────┐ ┌──────────────┐   │
│  │              │ │                                │ │              │   │
│  │  工作流列表  │ │       工作流编辑器             │ │  运行日志    │   │
│  │  (240px)     │ │       (flex 1)                 │ │  (320px)     │   │
│  │              │ │                                │ │              │   │
│  │  + 新建      │ │  ◀ 名字（可编辑）   [▶ 执行]   │ │  ● Step 1    │   │
│  │  ────────    │ │  ─────────────────────────     │ │  运行中...    │   │
│  │  ●Aider 流水 │ │  ┌─ Step 1 ─────────────────┐  │ │              │   │
│  │  ○三角审查   │ │  │ ...                       │  │ │  ✓ Step 2    │   │
│  │  ○多模型对比 │ │  ├─ Step 2 ─────────────────┤  │ │  done (8s)    │   │
│  │  ○我的工作流1│ │  │ ...                       │  │ │              │   │
│  │              │ │  ├─ Step 3 ─────────────────┤  │ │  Diff Preview │   │
│  │              │ │  └───────────────────────────┘  │ │  [side-by-side]│  │
│  │              │ │  [ + 添加步骤 ]                │ │              │   │
│  └──────────────┘ └────────────────────────────────┘ └──────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

三栏布局：

| 栏目 | 宽度 | 用途 |
|------|------|------|
| 左：工作流列表 | 240px | 列出所有保存的工作流模板，hover trash 删除，"+ 新建" 按钮 |
| 中：编辑器 | flex 1 | 步骤卡片垂直堆叠 + "添加步骤" + "执行" 按钮 |
| 右：运行日志 / Diff 预览 | 320px（可折叠） | 实时执行进度 + 各步 diff 呈现；空闲时收起 |

### 3.3 步骤卡片（核心组件 StepCard）

```
┌─ Step 2：编写代码 ────────────────── [DeepSeek-V3 ▾]  [▶ 单独运行] ─┐
│                                                                     │
│  System Prompt:                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 你是一个编码助手。根据上一步的架构设计完成实现，           │   │
│  │ 优先写小而清晰的函数，每个文件不超过 200 行。              │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  User Prompt:                                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ （留空 = 隐式继承上一步输出）                                │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Tools:                                                             │
│  ☑ grep_text   ☑ read_file   ☑ edit_file   ☑ write_file            │
│  ☑ glob_files  ☐ run_bash    ☑ lsp_code    ☐ delete_file           │
│  [全选] [全清] [Reviewer 预设] [Coder 预设]                         │
│                                                                     │
│  ─────────── 上次输出（折叠展开）───────────                        │
│  > 修改了 3 个文件，新增 2 个函数...                                │
│  > [查看完整 diff →]                                                │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**字段**：

| 字段 | UI 控件 | 说明 |
|------|--------|------|
| Step Name | text input | 顶部标题，可 inline 编辑 |
| Model | ModelSelector dropdown | 复用现有组件，4 个 provider |
| System Prompt | textarea (auto-grow, 4-8 行) | 必填 |
| User Prompt | textarea (auto-grow, 2-6 行) | 可空（隐式继承上一步） |
| Tool Allowlist | checkbox grid | 8 个工具的勾选 + 3 个快捷预设 |
| Run Single | button | 单独跑这步（输入 = 上一步最近一次 run 的 output） |
| Last Output | collapsible section | 显示上一次该步骤的执行结果 |

**交互**：

- 卡片可拖拽重排序（HTML5 drag API，不引外部库）
- 右上角 `×` 删除（confirm 后）
- 点击 Last Output "查看完整 diff" 打开右侧 Diff 面板
- 步骤间用 `↓` 箭头连接（CSS 伪元素，不需要 SVG）

### 3.4 工具快捷预设

3 个预设按钮（点击直接勾选对应工具组）：

| 预设 | 工具集 | 用途 |
|------|------|------|
| **Reviewer** | grep_text / read_file / glob_files / lsp_code | 只读审查 |
| **Coder** | + edit_file / write_file | 写入 + 修改 |
| **Full** | + run_bash / delete_file | 完全自主 |

也支持完全禁用（"☐ 全清"），用于纯文本生成步骤（如"总结"）。

### 3.5 运行视图（执行中实时高亮）

执行中状态：

```
┌──────────────────────────────────────────────────────────────────┐
│  ◀ Aider 流水线                                  ⏸ 暂停  ⏹ 停止 │
│  ─────────────────────────────────────────────────────────────── │
│                                                                  │
│  ✓ Step 1: 架构设计                                              │
│  ├─ Claude Opus, 4.2s, ¥0.018, 2 tool calls                      │
│  └─ 输出: 设计了 3 个模块... [展开 / Diff →]                     │
│                                                                  │
│  ● Step 2: 编写代码 [运行中]                                     │
│  ├─ DeepSeek-V3, 6.8s, ¥0.003 [实时累计]                         │
│  ├─ 🔧 grep_text "Harness"...                                    │
│  ├─ 🔧 read_file harness.py...                                   │
│  └─ 输出（流式）: 创建了 web/workflow_orchestrator.py，包含...   │
│                                                                  │
│  ⏸ Step 3: Review（等待中）                                      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

- 已完成步骤折叠为单行（点击展开看完整 output）
- 当前步骤展开，显示 tool calls + 流式 token
- 未运行步骤灰色
- 顶栏暂停（在步骤间隙挂起）/ 停止（cancel 当前 run）
- 每完成一步刷新左侧步骤卡的 Last Output

### 3.6 Diff Preview 面板（差异化护城河）

右侧 320px 面板，运行后激活：

```
┌─ Step 2 输出 Diff ─────────────────────────────────────┐
│  [全部] [Step 1→2] [Step 2→3]                          │
│  ─────────────────────────────────────────────────────│
│                                                       │
│   web/workflow_orchestrator.py                        │
│   ┌──────────────────┬──────────────────┐             │
│   │  Before (empty)  │  After           │             │
│   │                  │  + class Workflow│             │
│   │                  │  +     Orchestra │             │
│   │                  │  + ...           │             │
│   └──────────────────┴──────────────────┘             │
│   [回滚此步骤]                                        │
│                                                       │
│   harness.py                                          │
│   ┌──────────────────┬──────────────────┐             │
│   │  Before          │  After           │             │
│   │  def run()       │  def run()       │             │
│   │  - return result │  + log_step(...)│              │
│   │                  │  + return result│              │
│   └──────────────────┴──────────────────┘             │
│                                                       │
└───────────────────────────────────────────────────────┘
```

**实现路径**：

- 后端：编排器在每步执行前 snapshot 工作目录的 file digests（git diff 风格的轻量 hash 对比），执行后 diff 出来作为 StepResult.diff 字段保存
- 前端：用 `react-diff-viewer-continued` 组件渲染 side-by-side
- "回滚此步骤"：调用后端 `POST /api/workflows/runs/{run_id}/steps/{sid}/rollback`，把该步骤改动的文件 reset 回 snapshot

**MVP 砍 vs 留**：

- ✅ 留：text-based diff 提取 + side-by-side 展示
- ⬜ 砍（v5.1 加）：rollback 按钮、跨步骤 diff 对比、binary file diff

### 3.7 3 个预置模板（杀手锏，登场即满血）

#### 3.7.1 模板 1：Aider 流水线（Architect + Editor）

| Step | 模型 | System Prompt（节选） | Tools |
|------|------|---------------------|-------|
| 1. 架构设计 | Claude Opus 4.7 | "你是架构师。只输出设计方案、模块划分、接口签名。不写实现代码。" | Reviewer 预设（只读） |
| 2. 编写代码 | DeepSeek-V3 | "你是编码助手。根据架构设计实现。每个函数加 docstring。" | Coder 预设 |

**讲述价值**：直接致敬 Aider [architect/editor](https://aider.chat/2024/09/26/architect.html) 范式——把强模型用于"思考"、廉价模型用于"动手"，成本可降 70%+。

#### 3.7.2 模板 2：三角审查流水线（Planner → Coder → Reviewer）

| Step | 模型 | Tools |
|------|------|-------|
| 1. Planner | Claude Opus 4.7 | Reviewer 预设（只读） |
| 2. Coder | DeepSeek-V3 | Coder 预设 |
| 3. Reviewer | Qwen-Max | Reviewer 预设（只能看 step 2 改了啥，不能再改） |

**讲述价值**：模仿"代码 PR 三方流程"——规划 / 实现 / 审查分离，每方权限不同。Reviewer 只能 read+grep 是工具白名单的最佳用例。

#### 3.7.3 模板 3：多模型对比（同一任务并行 3 模型）

| Step | 模型 | Tools |
|------|------|-------|
| 1a. DeepSeek 实现 | DeepSeek-V3 | Coder 预设 |
| 1b. Qwen 实现 | Qwen-Max | Coder 预设 |
| 1c. Doubao 实现 | Doubao-Pro | Coder 预设 |
| 2. 对比总结 | Claude Opus 4.7 | 工具全禁（纯文本） |

**讲述价值**：CodeMesh "多模型对比"原始卖点的工作流化——一次 prompt 看三家国产模型在同一代码任务上的差异。MVP 用串行实现（v5.1 真并行）。

---

## 4. 数据模型

### 4.1 SQLite 表结构

独立数据库 `~/.codemesh/workflows.db`（不污染 `web_sessions.db` / `memory.db`）。

```sql
-- 工作流模板
CREATE TABLE workflows (
    id           TEXT PRIMARY KEY,         -- uuid4
    name         TEXT NOT NULL,
    description  TEXT,
    is_template  INTEGER DEFAULT 0,        -- 1 = 内置模板，不可删除
    created_at   TEXT NOT NULL,            -- ISO8601 UTC
    updated_at   TEXT NOT NULL
);

-- 步骤定义
CREATE TABLE workflow_steps (
    id              TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    step_order      INTEGER NOT NULL,      -- 1, 2, 3...
    name            TEXT NOT NULL,
    model           TEXT,                  -- "deepseek-chat" / "qwen-plus" / etc
    system_prompt   TEXT,
    user_prompt     TEXT,                  -- 可空（隐式继承上一步）
    enable_tools    TEXT,                  -- JSON array: ["grep_text", "read_file"] 或 ["*"]
    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

-- 一次执行
CREATE TABLE workflow_runs (
    id              TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    status          TEXT NOT NULL,         -- pending / running / done / error / cancelled
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    total_cost_rmb  REAL DEFAULT 0,
    error           TEXT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

-- 单步结果
CREATE TABLE workflow_step_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    step_id      TEXT NOT NULL,
    step_order   INTEGER NOT NULL,         -- 冗余，方便排序
    status       TEXT NOT NULL,            -- pending / running / done / error
    output       TEXT,
    error        TEXT,
    tool_calls   TEXT,                     -- JSON array
    file_diffs   TEXT,                     -- JSON: {"path": "harness.py", "before": "...", "after": "..."}[]
    model_used   TEXT,
    cost_rmb     REAL,
    duration_ms  INTEGER,
    started_at   TEXT,
    completed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);

CREATE INDEX idx_steps_wid ON workflow_steps(workflow_id, step_order);
CREATE INDEX idx_runs_wid ON workflow_runs(workflow_id);
CREATE INDEX idx_step_results_run ON workflow_step_results(run_id, step_order);
```

### 4.2 设计取舍

| 决策 | 选择 | 理由 |
|------|------|------|
| 数据库分离 | 独立 workflows.db | 工作流与 chat sessions 是不同领域，分库便于备份/重置 |
| enable_tools 存 JSON | TEXT 字段 | SQLite 无原生 array，JSON 简单灵活 |
| file_diffs 存 JSON | TEXT 字段 | 同上；diff 内容平均 < 10KB，不必另起 BLOB 表 |
| step_order 冗余在 result | 重复存 | 避免每次查询 JOIN steps 表 |
| ON DELETE CASCADE | 手动级联 | SQLite 默认不启用 FK，手动 DELETE 即可（参考 sessions_store.py） |

---

## 5. 后端架构

### 5.1 文件结构

```
web/
├── workflows_store.py          # SQLite CRUD（仿 sessions_store.py）
├── workflow_orchestrator.py    # 编排器（核心 ~200 行）
├── workflow_templates.py       # 3 个内置模板的 seed 函数
└── routes/
    └── workflows.py            # FastAPI 路由
```

### 5.2 WorkflowsStore（sessions_store.py 风格）

```python
# web/workflows_store.py
"""
Workflows / Steps / Runs / StepResults 持久化（v5）。

设计沿用 sessions_store.py：aiosqlite + ~/.codemesh/ 隐藏目录 +
模块级 lazy 单例。独立 workflows.db 与 web_sessions.db 分离。
"""

class WorkflowsStore:
    async def init(self) -> None: ...
    
    # workflows CRUD
    async def create_workflow(self, name: str, description: str = "") -> dict: ...
    async def list_workflows(self) -> list[dict]: ...
    async def get_workflow(self, wid: str) -> Optional[dict]: ...
    async def update_workflow(self, wid: str, **fields) -> None: ...
    async def delete_workflow(self, wid: str) -> bool: ...
    
    # steps CRUD
    async def add_step(self, wid: str, *, name: str, model: str,
                       system_prompt: str = "", user_prompt: str = "",
                       enable_tools: list[str] = ["*"]) -> dict: ...
    async def get_steps(self, wid: str) -> list[dict]: ...
    async def update_step(self, sid: str, **fields) -> None: ...
    async def delete_step(self, sid: str) -> bool: ...
    async def reorder_steps(self, wid: str, step_ids: list[str]) -> None: ...
    
    # runs CRUD
    async def create_run(self, wid: str) -> dict: ...
    async def update_run(self, run_id: str, *, status: str = None,
                         total_cost: float = None, error: str = None) -> None: ...
    async def list_runs(self, wid: str, limit: int = 20) -> list[dict]: ...
    async def get_run(self, run_id: str) -> Optional[dict]: ...
    
    # step results
    async def save_step_result(self, run_id: str, step: dict, **fields) -> int: ...
    async def get_step_results(self, run_id: str) -> list[dict]: ...
```

### 5.3 WorkflowOrchestrator（核心 ~200 行）

```python
# web/workflow_orchestrator.py
"""
多模型工作流编排器（v5 核心）。

设计要点（面试讲述用）：
- 每个 step 创建独立的临时 harness，按 step.model 选 adapter、按
  step.enable_tools filter 工具白名单
- step 间隐式数据流：上一步 output 作为 context 拼到下一步 user_prompt 前
- SSE 事件序列：step_start → token* → tool_start/tool_end* → step_end → ...
- 工作目录快照：每 step 执行前后取一次 file digests，diff 出来存 file_diffs
- 中断处理：cancel API 时清理 run_task，标记 run = cancelled，剩余 steps 不执行
"""

from harness import Harness
from execution.tools import registry

class WorkflowOrchestrator:
    def __init__(self, store: WorkflowsStore, work_dir: Path = Path.cwd()):
        self.store = store
        self.work_dir = work_dir
        self._cancel_flags: dict[str, bool] = {}

    async def run(self, workflow_id: str, run_id: str):
        """async generator 推 SSE 事件。"""
        steps = await self.store.get_steps(workflow_id)
        prev_output = ""
        total_cost = 0.0
        
        for step in steps:
            if self._cancel_flags.get(run_id):
                await self.store.update_run(run_id, status="cancelled")
                yield {"type": "cancelled", "data": {}}
                return
            
            yield {"type": "step_start", "data": {
                "step_id": step["id"], "name": step["name"],
                "model": step["model"], "step_order": step["step_order"],
            }}
            
            # 拼 prompt：隐式继承上一步 output
            user_input = step["user_prompt"] or ""
            if prev_output:
                user_input = f"上一步输出：\n{prev_output}\n\n{user_input}".strip()
            
            # 创建临时 harness（按 step 配置）
            harness = self._make_step_harness(step)
            
            # 工作目录 snapshot before
            before_digests = self._snapshot_dir()
            
            full_answer = ""
            tool_calls = []
            cost_rmb = 0.0
            start_ts = time.time()
            
            try:
                async for ev in harness.run_stream_full(user_input):
                    yield {"type": ev["type"], "data": {**ev["data"], "step_id": step["id"]}}
                    if ev["type"] == "token":
                        full_answer += ev["data"]["delta"]
                    elif ev["type"] == "tool_end":
                        tool_calls.append(ev["data"])
                    elif ev["type"] == "usage":
                        cost_rmb = ev["data"].get("cost_rmb", 0.0)
                
                # snapshot after + diff
                after_digests = self._snapshot_dir()
                file_diffs = self._compute_diffs(before_digests, after_digests)
                
                # 持久化 step_result
                await self.store.save_step_result(
                    run_id, step,
                    status="done", output=full_answer,
                    tool_calls=tool_calls, file_diffs=file_diffs,
                    model_used=step["model"], cost_rmb=cost_rmb,
                    duration_ms=int((time.time() - start_ts) * 1000),
                )
                total_cost += cost_rmb
                yield {"type": "step_end", "data": {"step_id": step["id"], "ok": True}}
                prev_output = full_answer
                
            except Exception as e:
                await self.store.save_step_result(
                    run_id, step, status="error", error=str(e),
                )
                yield {"type": "step_end", "data": {
                    "step_id": step["id"], "ok": False, "error": str(e),
                }}
                await self.store.update_run(run_id, status="error", error=str(e))
                yield {"type": "done", "data": {"ok": False}}
                return
        
        await self.store.update_run(run_id, status="done", total_cost=total_cost)
        yield {"type": "done", "data": {"ok": True, "total_cost": total_cost}}

    def cancel(self, run_id: str):
        self._cancel_flags[run_id] = True

    def _make_step_harness(self, step: dict) -> Harness:
        """按 step.model + enable_tools 创建临时 harness。"""
        h = Harness(model_name=step["model"])
        if step["system_prompt"]:
            h.short_term.set_system(step["system_prompt"])
        # 工具白名单 filter
        allowlist = json.loads(step["enable_tools"])
        if allowlist != ["*"]:
            h.tools = {n: t for n, t in registry.all().items() if n in allowlist}
        return h

    def _snapshot_dir(self) -> dict[str, str]:
        """返回 cwd 下所有文件的 path → sha256 mapping（跳过 .git / node_modules / .venv）。"""
        ...
    
    def _compute_diffs(self, before: dict, after: dict) -> list[dict]:
        """对比 digests，返回变化的文件列表（含 before/after 内容）。"""
        ...
```

### 5.4 API 端点

```
# Workflow CRUD
GET    /api/workflows                            列表
POST   /api/workflows                            创建 (body: name, description)
GET    /api/workflows/{id}                       详情（含 steps）
PUT    /api/workflows/{id}                       更新元数据 (name, description)
DELETE /api/workflows/{id}                       删除（级联 steps + runs + results）

# Steps CRUD
POST   /api/workflows/{id}/steps                 添加步骤
PUT    /api/workflows/{id}/steps/{sid}           更新步骤
DELETE /api/workflows/{id}/steps/{sid}           删除步骤
POST   /api/workflows/{id}/steps/reorder         重排序 (body: [step_id, ...])

# Run
POST   /api/workflows/{id}/run                   整个工作流执行 (SSE)
POST   /api/workflows/{id}/steps/{sid}/run       单步执行 (SSE)
POST   /api/workflows/runs/{run_id}/cancel       中断
GET    /api/workflows/runs/{run_id}              运行详情（含 step_results）
GET    /api/workflows/{id}/runs                  运行历史
GET    /api/workflows/runs/{run_id}/diff         所有 file diffs（用于 Diff 面板）

# Templates
GET    /api/workflows/templates                  列出内置模板（is_template=1）
POST   /api/workflows/templates/{tid}/fork       基于模板创建用户工作流
```

### 5.5 SSE 事件序列（执行整个工作流时）

```
event: run_start
data: {"run_id": "...", "workflow_id": "..."}

event: step_start
data: {"step_id": "s1", "name": "架构设计", "model": "claude-opus", "step_order": 1}

event: token
data: {"delta": "好的", "step_id": "s1"}

event: token
data: {"delta": "，我", "step_id": "s1"}

event: tool_start
data: {"name": "read_file", "args": {...}, "step_id": "s1"}

event: tool_end
data: {"name": "read_file", "result": "...", "ok": true, "step_id": "s1"}

event: usage
data: {"cost_rmb": 0.018, "model": "claude-opus", "duration_ms": 4200, "step_id": "s1"}

event: step_end
data: {"step_id": "s1", "ok": true}

event: step_start
data: {"step_id": "s2", ...}
... (Step 2 序列)

event: done
data: {"ok": true, "total_cost": 0.025}
```

事件类型扩展：复用 Phase 3 的 token / tool_start / tool_end / usage，新增 run_start / step_start / step_end / done / error / cancelled。

---

## 6. 前端架构

### 6.1 文件结构

```
frontend/
├── components/
│   ├── WorkflowsView.tsx           # 三栏主容器
│   ├── WorkflowList.tsx            # 左：工作流列表
│   ├── WorkflowEditor.tsx          # 中：编辑器
│   ├── StepCard.tsx                # 单步卡片（含 ModelSelector / ToolAllowlist）
│   ├── ToolAllowlistEditor.tsx     # 工具勾选 grid + 3 个预设
│   ├── WorkflowRunPanel.tsx        # 右：执行日志
│   ├── DiffViewer.tsx              # 右：Diff side-by-side
│   └── TemplateGallery.tsx         # "新建" 时的模板选择弹窗
├── lib/
│   ├── workflow-api.ts             # API client（仿 api.ts）
│   ├── workflow-sse.ts             # SSE consumer（复用 lib/sse.ts 模式）
│   ├── workflow-types.ts           # Workflow / Step / Run / StepResult / FileDiff
│   └── store.ts                    # 扩展：currentWorkflowId / workflows / currentRun
└── app/
    └── page.tsx                     # 顶栏加 view === "workflows"
```

### 6.2 状态管理扩展（store.ts）

```ts
type View = "chat" | "stats" | "workflows";

interface WorkflowState {
  workflows: Workflow[];                   // 全部模板（含内置 + 用户）
  currentWorkflowId: string | null;        // 当前编辑的工作流
  currentRun: WorkflowRun | null;          // 实时运行状态
  stepResults: Map<string, StepResult>;    // step_id → 实时结果（流式更新）
  
  setWorkflows: (w: Workflow[]) => void;
  setCurrentWorkflow: (id: string | null) => void;
  updateStepResult: (sid: string, r: Partial<StepResult>) => void;
  clearRun: () => void;
}
```

### 6.3 关键组件设计

#### 6.3.1 StepCard.tsx

```tsx
function StepCard({ step, isLast, onUpdate, onDelete, onRun }: Props) {
  return (
    <div className="border border-border-subtle rounded-lg p-4 bg-surface mb-3">
      {/* Header: 序号 + 名字 + 模型 + 单独运行 */}
      <header className="flex items-center justify-between mb-3">
        <input value={step.name} onChange={...} />
        <ModelSelector value={step.model} onChange={...} />
        <button onClick={() => onRun(step.id)}>▶ 单独运行</button>
      </header>
      
      {/* System / User Prompt */}
      <PromptField label="System" value={step.systemPrompt} onChange={...} />
      <PromptField label="User" value={step.userPrompt} onChange={...} />
      
      {/* Tool Allowlist */}
      <ToolAllowlistEditor value={step.enableTools} onChange={...} />
      
      {/* Last Output（折叠） */}
      {lastResult && <LastOutputCollapse result={lastResult} />}
      
      {!isLast && <ConnectorArrow />}
    </div>
  );
}
```

#### 6.3.2 ToolAllowlistEditor.tsx

```tsx
const TOOLS = [
  "grep_text", "read_file", "edit_file", "write_file",
  "glob_files", "run_bash", "lsp_code", "delete_file",
];

const PRESETS = {
  Reviewer: ["grep_text", "read_file", "glob_files", "lsp_code"],
  Coder:    ["grep_text", "read_file", "edit_file", "write_file", "glob_files", "lsp_code"],
  Full:     TOOLS,
};

function ToolAllowlistEditor({ value, onChange }: Props) {
  const allowAll = value.includes("*");
  return (
    <div>
      <div className="grid grid-cols-4 gap-2">
        {TOOLS.map(t => (
          <label key={t}>
            <input type="checkbox" checked={allowAll || value.includes(t)}
                   onChange={() => toggle(t)} />
            {t}
          </label>
        ))}
      </div>
      <div className="flex gap-2 mt-2">
        {Object.entries(PRESETS).map(([name, tools]) => (
          <button onClick={() => onChange(tools)}>{name} 预设</button>
        ))}
        <button onClick={() => onChange(["*"])}>全开</button>
        <button onClick={() => onChange([])}>全清</button>
      </div>
    </div>
  );
}
```

#### 6.3.3 DiffViewer.tsx（差异化护城河）

```tsx
import ReactDiffViewer from "react-diff-viewer-continued";

function DiffViewer({ diffs }: { diffs: FileDiff[] }) {
  return (
    <div className="space-y-4">
      {diffs.map(d => (
        <div key={d.path} className="border rounded">
          <header className="px-3 py-2 bg-surface-hover">{d.path}</header>
          <ReactDiffViewer
            oldValue={d.before || ""}
            newValue={d.after || ""}
            splitView={true}
            useDarkTheme={true}
          />
        </div>
      ))}
    </div>
  );
}
```

依赖新增：`pnpm add react-diff-viewer-continued`

### 6.4 复用现有组件

| 现有组件 | 复用位置 |
|---------|--------|
| `ModelSelector` | 每个 StepCard 上一个 |
| `ToolCallCard` | RunPanel 中显示步骤的工具调用 |
| `MessageBubble` | 步骤 output 流式显示 |
| `lib/sse.ts` 的 `parseSSEFrame` | workflow-sse.ts 直接 import 复用 |
| `lib/api.ts` 的 `ApiError` | workflow-api.ts 复用 |

---

## 7. 工具白名单实现细节

### 7.1 后端 filter 路径

```python
# orchestrator._make_step_harness
allowlist = json.loads(step["enable_tools"])

if allowlist == ["*"]:
    # 全开，不动 harness.tools
    pass
elif allowlist == []:
    # 完全禁用，清空 tools dict
    h.tools = {}
else:
    # 白名单 filter
    h.tools = {name: tool for name, tool in registry.all().items() if name in allowlist}
```

**注意**：`Harness` 当前是从 `registry` 全量加载，需要在 `harness.py` 暴露 `self.tools` 字段或加 `replace_tools(names)` 方法。这是本方案对 harness 唯一的小改动（Phase 6.4 完成）。

### 7.2 模型 list_tools 时的可见性

模型走 OpenAI tool calling 协议时，list_tools 返回的就是 `h.tools.values()` 序列化后的 JSON Schema。filter 之后模型就**看不见**被禁用的工具——这是"软隔离"的核心。

不需要在 tool 实现里加权限检查（避免污染 Tool Registry 的简洁度）。

### 7.3 验证场景

| 场景 | enable_tools | 预期 |
|------|-------------|------|
| Reviewer 步骤试图 write_file | `["grep_text", "read_file"]` | 模型 list_tools 看不到 write_file，不会调用 |
| Coder 步骤试图 run_bash | `["grep_text", "read_file", "edit_file", "write_file"]` | 同上 |
| Full 步骤 | `["*"]` | 全部工具可用 |
| 纯文本步骤 | `[]` | h.tools 为空 dict，模型只能纯文本回答 |

---

## 8. Diff-Aware 数据流

### 8.1 文件 digest 算法

```python
def _snapshot_dir(self) -> dict[str, tuple[str, str]]:
    """返回 path → (sha256, content) mapping。"""
    snap = {}
    for path in self.work_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(p in path.parts for p in (".git", "node_modules", ".venv", "__pycache__", ".next")):
            continue
        if path.stat().st_size > 100_000:  # 跳过大文件（图片 / 数据）
            continue
        content = path.read_text(errors="ignore")
        digest = hashlib.sha256(content.encode()).hexdigest()
        snap[str(path.relative_to(self.work_dir))] = (digest, content)
    return snap
```

### 8.2 diff 提取

```python
def _compute_diffs(self, before: dict, after: dict) -> list[dict]:
    diffs = []
    all_paths = set(before) | set(after)
    for path in all_paths:
        b_digest, b_content = before.get(path, ("", ""))
        a_digest, a_content = after.get(path, ("", ""))
        if b_digest != a_digest:
            diffs.append({
                "path": path,
                "before": b_content,
                "after": a_content,
                "kind": "modified" if path in before and path in after
                        else ("created" if path in after else "deleted"),
            })
    return diffs
```

### 8.3 性能保护

| 风险 | 缓解 |
|------|------|
| 大仓库 rglob 慢 | 跳过 .git / node_modules / .venv / 大文件；warn if > 500ms |
| diff 内容膨胀 DB | 每个 step_result 的 file_diffs JSON 上限 100KB，超出截断 + warn |
| 频繁 IO | snapshot 用 memcache 配 mtime 判断，未变文件复用上次 digest |

---

## 9. 实施阶段（Phase 6.0-6.8）

| Phase | 工作 | 估时 | 交付物 |
|------|------|------|-------|
| **6.0** | 设计方案 + ADR-0007 + CLAUDE.md ADR 列表更新 | 已 done | 本文档 + ADR-0007 |
| **6.1** | 后端：workflows_store.py + workflows CRUD 路由 + 8 测试 | 0.5 天 | commit + backend-notes Section §14 |
| **6.2** | 前端：WorkflowsView 三栏骨架 + WorkflowList 接 6.1 API + 顶栏 tab | 0.5 天 | commit + frontend-notes Section §45 |
| **6.3** | 前端：WorkflowEditor + StepCard + ToolAllowlistEditor（不接执行） | 1 天 | commit |
| **6.4** | 后端：WorkflowOrchestrator + Harness `replace_tools` + 整体 run SSE 端点 + 6 测试 | 1 天 | commit |
| **6.5** | 前端：WorkflowRunPanel + SSE 消费 + 步骤实时高亮 + 进度 | 1 天 | commit |
| **6.6** | 后端：snapshot + diff 实现 + DiffViewer 前端集成 | 1 天 | commit（**护城河实现**） |
| **6.7** | 后端：3 个 templates seed + 前端 TemplateGallery 选模板 fork | 0.5 天 | commit |
| **6.8** | 单步运行 + 中断 + 运行历史 + ADR-0007 收尾 + 4 个 notes 文件同步 + merge to main | 0.5 天 | merge commit |

**总预估 5.5 天**。按 Web UI MVP 经验（1 Phase ≈ 1 天实际），约 1 周可完成。

### 9.1 每个 Phase 的可验证 demo

| Phase | demo 标准 |
|------|---------|
| 6.1 | `curl POST /api/workflows + GET /api/workflows` 走通；测试全过 |
| 6.2 | 浏览器进 Workflows tab 能看见空列表；点 "+ 新建" 能创建一个空工作流 |
| 6.3 | 能在编辑器里加 3 个 step、改名、改 prompt、勾工具、保存 |
| 6.4 | 命令行 `curl -N POST /run` 能看到 SSE 流，token + tool 事件齐全 |
| 6.5 | 浏览器点 ▶ 执行，步骤卡实时高亮，输出流式显示 |
| 6.6 | 跑完一步打开 Diff 面板能看到 side-by-side 改动 |
| 6.7 | 点 "新建" 看到 3 个内置模板可 fork，fork 后能直接跑 |
| 6.8 | 运行历史列表能翻看过去的 runs；merge 到 main 完成 v5 |

---

## 10. 风险与 Mitigation

| 风险 | 影响 | Mitigation |
|------|------|-----------|
| **通用工作流红海**（Dify v1.5+ 已成熟） | 用户问"这不就是中文版 Dify？" | 定位收窄到 Coding-First；README 顶部明写"不做通用应用编排"；ADR-0007 §Decision 段写清边界 |
| **GitHub Copilot CLI 直接对手**（2026 新出 subagent 架构） | 闭源企业版抢占心智 | 反击点：开源 + 本地优先 + 国产模型成本优势 + 可视化编排（CLI 无 UI）|
| **多次 harness 实例化的 token / 性能浪费** | 每步新 harness，无法跨步分享 short_term | 性能瓶颈在 LLM call 而非进程开销；MVP 接受；v5.1 考虑 step 间 short_term 接力 |
| **diff 提取性能问题** | 大仓库每步 snapshot 慢 | §8.3 三条保护：跳过 .git/node_modules，大文件跳过，mtime 判断复用 |
| **工具白名单破坏 Tool Registry 简洁度** | harness.py 需新增 replace_tools 方法 | 该方法只 15 行，对 registry 模式无侵入；可作为 ADR-0007 附带的小重构 |
| **3 个模板的 prompt 质量决定首印象** | 模板写得不好 demo 翻车 | Phase 6.7 单独花时间打磨 prompt；用真实代码任务做端到端测试 |
| **diff 面板用户找不到** | 护城河功能被埋没 | 6.6 阶段在 RunPanel 顶栏加显眼按钮 "查看 Diff"；执行完成 toast 提示 |

---

## 11. 未决问题（等用户回）

| Q | 选项 | 建议 |
|---|------|------|
| **Q1**：工作流执行时是否允许跨步骤共享 short_term？ | (a) 每步全新 harness 隔离 / (b) 步骤间继承 short_term | 建议 a，理由：每步独立模型，short_term 跨模型混乱；上一步 output 已通过隐式 context 传递 |
| **Q2**：3 个模板的 system_prompt 用中文还是英文？ | (a) 中文（用户母语）/ (b) 英文（模型理解更稳） | 建议 b，理由：所有 LLM 对英文 system prompt 鲁棒性更高；UI 显示可中文 |
| **Q3**：Phase 6.6 的 react-diff-viewer-continued 是否值得引入？ | (a) 引入 / (b) 自写简易 diff（unified diff 文本形式） | 建议 a，理由：3 KB gzip，护城河功能值得；自写工作量大 |
| **Q4**：cancel API 是否支持"完成当前 step 后停"？ | (a) 立即 cancel（terminate task）/ (b) 设 flag，step 间隙退出 | 建议 b，理由：避免 step 中途 cancel 留下半成品文件 |
| **Q5**：内置模板是否允许用户编辑后保存为新工作流？ | (a) 只能 fork 后改 / (b) 内置可改 | 建议 a，理由：保护模板完整性，fork 是显式动作 |
| **Q6**：MVP 是否做"运行历史导出 JSON"？ | (a) 做 / (b) 不做 | 建议 b，砍掉留 v5.1 |
| **Q7**：右侧 DiffViewer 面板默认折叠还是展开？ | (a) 默认折叠（点开看）/ (b) 执行中自动展开 | 建议 b，执行中自动展开，结束 30s 后自动收起 |

---

## 12. 验证 checklist（merge to main 前必须全过）

### 12.1 功能验证

- [ ] 创建 / 编辑 / 删除工作流 + 步骤 CRUD 全通
- [ ] 步骤拖拽重排序生效
- [ ] 工具白名单：Reviewer 步骤试图 write_file 失败（模型拿不到工具）
- [ ] 整体执行：3 步工作流端到端跑通，SSE 实时显示
- [ ] 单步执行：基于上一步输出独立运行某 step
- [ ] Diff 面板：跑完一个写文件的 step 能看到 side-by-side diff
- [ ] 中断：执行中点 ⏹ 能立即停止
- [ ] 3 个内置模板可 fork + 跑通

### 12.2 测试验证

- [ ] 后端测试 ≥ 20 个：workflows_store 8 + routes 6 + orchestrator 6
- [ ] 前端 `pnpm tsc --noEmit` 零错误
- [ ] 后端 `python -m tests.test_web.test_workflows_*` 全过
- [ ] 所有现有测试（36 个 web 测试）继续过

### 12.3 讲述层验证

- [ ] backend-notes 加 Section §14（v5 工作流编排器）≥ 300 行
- [ ] frontend-notes 加 Section §45（v5 三栏 UI）≥ 250 行
- [ ] devlog.md 顶部加 v5 大段（含市场调研对照）
- [ ] ADR-0007 完整（Context / Decision / Consequences / Mitigation）
- [ ] README 不动（用户自己写 v5 章节）

---

## 附录 A：与 Dify 的功能对照

| 维度 | Dify v1.5+ | CodeMesh v5 | 评价 |
|------|----------|-----------|------|
| 工作流形态 | DAG 节点 + 拖拽 | 线性卡片堆叠 | Dify 强；我们故意收窄 |
| 每节点选模型 | ✅ | ✅ | 持平 |
| 信息流可见 | Variable Inspect | Last Output + Diff Panel | 我们 diff 维度强 |
| 用户可控 | Human Input 暂停 + 断点重跑 | 单步运行 + 中断 + step 间 cancel | 持平 |
| 工具语义 | HTTP / RAG / Plugin / Code 沙箱 | grep/read/write/edit/glob/bash/lsp/delete | **我们独有** |
| 工具白名单 per step | ❌ 全开 | ✅ 8 工具勾选 + 3 预设 | **我们独有** |
| Diff-aware 数据流 | ❌ 泛型变量 | ✅ side-by-side file diff | **我们独有** |
| 模板 | 应用市场 | 3 个内置 Coding 模板 | 互补 |

**结论**：Dify 是面向"AI 应用构建者"，CodeMesh v5 是面向"代码 Agent 重度用户"。两者不竞争同一市场。

---

## 附录 B：3 个模板的完整 system prompt（草稿）

### B.1 Aider 流水线

```
[Step 1 - Architect, Claude Opus 4.7]
You are a senior software architect. Read the codebase carefully (use grep_text, 
read_file, glob_files). Output a design plan with: (1) which files to modify, 
(2) function signatures, (3) module boundaries, (4) potential pitfalls. DO NOT 
write implementation code. Be specific, name actual files and functions.

[Step 2 - Editor, DeepSeek-V3]
You are a coding assistant. Implement the architecture from the previous step.
Write small, clear functions with docstrings. Edit existing files when possible
rather than creating new ones. Add tests in tests/. Use edit_file for precise
changes, write_file for new files.
```

### B.2 三角审查流水线

```
[Step 1 - Planner, Claude Opus 4.7]
You plan the change. Read the task carefully, explore the codebase, output a
plan with steps and affected files. NO implementation.

[Step 2 - Coder, DeepSeek-V3]
Implement the plan from Step 1. Be precise. Add comments only where non-obvious.

[Step 3 - Reviewer, Qwen-Max]
Review the code changes from Step 2. Check: (1) correctness, (2) readability,
(3) edge cases, (4) test coverage. Output a list of concerns (or "LGTM"). You
CANNOT modify code—you only have read tools.
```

### B.3 多模型对比

```
[Step 1a/1b/1c - parallel implementations, DeepSeek/Qwen/Doubao]
Implement the user's task using grep/read/edit/write tools. Be self-contained.

[Step 2 - Comparator, Claude Opus 4.7, tools=[]]
Compare the three implementations from Steps 1a, 1b, 1c. Output a structured
comparison: (1) correctness, (2) code style, (3) edge case handling, 
(4) recommended pick with reason.
```

---

## 参考

- [Aider Architect/Editor Split](https://aider.chat/2024/09/26/architect.html) — 多模型分工写代码的开山案例
- [Dify Workflow LLM Node](https://docs.dify.ai/en/guides/workflow/node/llm) — 节点级模型选择
- [Dify 1.5.0 Real-Time Debugging](https://dify.ai/blog/dify-1-5-0-real-time-workflow-debugging-that-actually-works) — Variable Inspect / Last Run / 单步运行
- [GitHub Copilot CLI Subagent](https://fbakkensen.github.io/ai/copilot/productivity/2026/04/10/github-copilot-cli-multi-model-subagents.html) — 最直接的对手
- [react-diff-viewer-continued](https://github.com/aeolun/react-diff-viewer-continued) — 前端 diff 渲染
- ADR-0001 — Agentic Search over RAG（Python-first 一致原则）
- ADR-0005 — 国内多模型差异化（v5 是其延伸）
- ADR-0006 — Web UI 栈 FastAPI + Next.js（v5 复用）
