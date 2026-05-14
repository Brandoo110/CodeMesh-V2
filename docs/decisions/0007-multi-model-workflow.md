# ADR-0007：Multi-Model Workflow Orchestration — From Comparison to Collaboration

## Status

Accepted — 2026-05-14（市场调研完成后拍板，等 Phase 6.1 起开始实现）。

## Context

CodeMesh v1-v4 的差异化卖点是**多模型对比**（同一任务跑 4 个国产模型看代码生成差异，ADR-0005）。
随着 v4 Web UI MVP 收官（2026-05-14，merge `d07793e`），下一步面临两条路：

1. **保守路线**：进入讲述层（录 demo / 写 walkthrough / mock 面试）
2. **激进路线**：再加一个差异化大功能，让讲述层更有内容

用户拍板走激进路线，提出 v5 idea：**让用户定义"工作流"（多步骤），每步选不同模型完成不同任务**，
例如"文献调研 DeepSeek → 整理 Qwen → 写作 Doubao → 复盘 Claude"。

### 市场调研（2026-05-14，扫描 20+ 产品）

调研对象：Dify v1.5+ / Coze / Flowise / Langflow / n8n / Wordware / Vellum / Stack AI /
Buildship / LangGraph / CrewAI / AutoGen / OpenAI Agents SDK / Pydantic AI /
Cursor 3.0 / Cline / Roo Code / Aider / Devin / Replit Agent /
GitHub Copilot Workspace / GitHub Copilot CLI / OpenRouter / LiteLLM / Portkey /
火山引擎方舟 / 智谱 BigModel。

**核心发现**：

| 类别 | 工作流 + 多模型 + 信息流可见 | 代码工具语义（grep/edit/exec） |
|------|----|----|
| 通用 AI 工作流（Dify / Coze / Flowise / Vellum） | ✅ 全做齐 | ❌ 只有 Code 沙箱节点 |
| 多 Agent 框架（LangGraph / CrewAI / AutoGen / Pydantic AI） | ✅ 编排能力强 | ❌ 无 UI |
| Coding Agent（Cursor / Cline / Aider / Claude Code） | ❌ 单循环 Agent | ✅ 全套 |
| GitHub Copilot CLI（2026 新出 subagent 架构） | ⚠️ 多模型并行 | ✅ | **最接近的对手** |

**结论**：
- "通用多模型工作流 + 信息流可见 + 用户可控" 已是红海——Dify v1.5+ 做到节点级单步运行 + Variable Inspect + Human Input 暂停 + 断点重跑
- 但 **Coding 特化的工作流是空白**：所有工作流平台的"工具"都是 HTTP/RAG/Plugin（应用层），没有 grep/read/write/edit（代码层）
- 反过来，所有 Coding Agent（Cursor/Cline/Aider/Claude Code）都是单循环没有可视化工作流
- Aider architect/editor 两步固定 split 是最早证明"多模型分工写代码"需求真实的案例

### 真正的空白窗口

**Dify 的可视化工作流 × Claude Code 的代码工具语义** = 没人占的格子。

### 候选定位（3 选 1）

| 候选 | Slogan | 评价 |
|------|--------|------|
| A（选定） | Dify-style workflow for Claude Code-style coding agents | 收窄到代码 agent，避开 Dify 通用市场 |
| B | Architect/Coder/Reviewer 流水线的可视化版 | 太窄，限制扩展（用户可能想要 5 步） |
| C | 本地优先的多模型 Coding Workflow | 太抽象，不突出工作流概念 |

## Decision

**采用候选 A：把 v5 定位为 "Dify-style workflow for Claude Code-style coding agents"**。

具体落地：

### 1. 不做什么（划清边界，防止变成中文版 Dify）

| 不做 | 原因 |
|------|------|
| 通用 AI 应用编排（聊天机器人 / RAG / 客服） | Dify 已做透；硬刚等于死 |
| 节点式 DAG（拖拽流程图） | 引入 React Flow 复杂度，且代码工作流多为线性 |
| 多用户协作 / 团队管理 / 鉴权 | 对齐 Web UI MVP（localhost 单用户） |
| 工作流 marketplace / 分享导出 | 砍掉，留 v5.1+ |
| 变量 / 占位符 / 条件分支 | MVP 隐式数据流，砍掉显式占位符 |

### 2. 做什么（3 个差异化护城河）

| 护城河 | 实现位置 |
|------|---------|
| **工具白名单 per step** | `web/workflows_store.py` 的 `enable_tools` JSON 字段 + `web/workflow_orchestrator.py` 的 `_make_step_harness` filter |
| **3 个预置 Coding 模板**：Aider 流水线 / 三角审查 / 多模型对比 | `web/workflow_templates.py` seed |
| **Diff-aware 数据流** | Orchestrator 每步 snapshot 工作目录 file digests → diff 出来存 `step_results.file_diffs` JSON → 前端 `react-diff-viewer-continued` side-by-side |

### 3. 技术架构

复用现有能力：

- 后端：`harness.run_stream_full`（Phase 3 已建）+ SSE 协议复用 token / tool_start / tool_end / usage
- 前端：`ModelSelector` / `ToolCallCard` / `MessageBubble` / `lib/sse.ts` 直接复用
- 状态：Zustand store 扩展 `workflows` / `currentWorkflowId` / `stepResults`

新增组件（4 个后端 + 8 个前端）：

```
web/
├── workflows_store.py          # SQLite CRUD
├── workflow_orchestrator.py    # 编排器
├── workflow_templates.py       # 3 个内置模板 seed
└── routes/workflows.py         # FastAPI 路由

frontend/components/
├── WorkflowsView.tsx           # 三栏主容器
├── WorkflowList.tsx            # 左
├── WorkflowEditor.tsx          # 中
├── StepCard.tsx                # 单步卡片
├── ToolAllowlistEditor.tsx     # 工具勾选 + 3 预设
├── WorkflowRunPanel.tsx        # 右上：日志
├── DiffViewer.tsx              # 右下：diff
└── TemplateGallery.tsx         # 模板选择弹窗
```

### 4. 数据库分离

独立 `~/.codemesh/workflows.db`（不污染 `web_sessions.db` / `memory.db`），4 张表：
`workflows / workflow_steps / workflow_runs / workflow_step_results`。

### 5. 实施阶段

Phase 6.0（本 ADR + 设计方案）→ 6.1（后端 CRUD）→ 6.2（前端骨架）→ 6.3（编辑器 UI）→
6.4（编排器 + SSE）→ 6.5（运行 UI）→ 6.6（diff 实现，护城河）→ 6.7（3 模板）→ 6.8（收尾 + merge）

总预估 5.5 天（按 Web UI MVP 经验 1 Phase ≈ 1 天实际）。

完整设计见 [`docs/workflow-design-plan.md`](../workflow-design-plan.md)。

## Consequences

### 好处

1. **唯一占位"代码工作流"空白市场** —— 通用工作流红海中找到独有切入点
2. **讲述层 10 倍价值放大** —— "演示 Aider 流水线 5 分钟"比"演示 Chat + Stats"故事强得多
3. **复用 v1-v4 全部基建** —— harness / SSE / Zustand / SQLite 都不变；新增是纯叠加，无重构
4. **3 个内置模板降低空白页焦虑** —— 用户进入 Workflows 立即看到杀手锏
5. **diff-aware 是真正护城河** —— Dify 给不了，Coding Agent 也没有；技术实现门槛中等但产品价值高
6. **多模型协作故事完整闭环** —— 从 v1 "对比"升级到 v5 "协作"，ADR-0005 的天然延伸

### 坏处（要诚实）

1. **推迟讲述层 1.5-2 周** —— 用户原本"v4 冻结进入讲述层"被打破；但故事更值钱，权衡值得
2. **GitHub Copilot CLI 是直接对手** —— 闭源企业版若强势推广，会抢占心智
3. **工具白名单需小改 harness.py** —— 加 `replace_tools(names)` 方法（15 行，不破坏 registry 模式）
4. **diff 提取在大仓库有性能风险** —— 用 mtime + size 阈值 + skip dirs 三层防护
5. **多次创建 harness 实例的内存占用** —— 每步新 harness，跨步无 short_term 接力（v5.1 优化）
6. **3 模板的 prompt 质量决定首印象** —— 写得差 demo 翻车，Phase 6.7 必须打磨

### Mitigation

- **关于推迟讲述层**：v5 完成时本身就是讲述层素材（demo 内容更丰富、ADR-0007 直接成讲述章节）
- **关于 Copilot CLI 对手**：开源 + 本地优先 + 国产模型成本优势 + 可视化编排（CLI 无 UI）四点反击
- **关于 harness 改动**：`replace_tools` 是纯加法，不动 registry 注册机制；附加在 ADR-0007 不另开新 ADR
- **关于 diff 性能**：[`workflow-design-plan.md`](../workflow-design-plan.md) §8.3 三层保护方案
- **关于 harness 多实例**：MVP 接受性能浪费；v5.1 考虑 short_term 接力或 lazy harness pool
- **关于模板质量**：Phase 6.7 专门花半天打磨，端到端测试每个模板都能跑通 1 个真实任务

### 不做的事（明确写下）

- ❌ 不实现 DAG / 条件分支 / 循环（线性堆叠先跑通）
- ❌ 不实现工作流 marketplace / 分享 / 导出 JSON（v5.1+）
- ❌ 不实现多用户 / 鉴权 / 协作（对齐 Web UI MVP）
- ❌ 不实现工作流模板的二次编辑保存为新模板（fork 是唯一路径）
- ❌ 不修改 `execution/loop.py`（surgical changes 原则，复用 run_stream_full）
- ❌ 不引入新的状态管理库（继续用 Zustand）
- ❌ 不引入 React Flow / xyflow（垂直卡片堆叠用 HTML5 drag 实现）

## 参考

- [`docs/workflow-design-plan.md`](../workflow-design-plan.md) — 完整 v5 设计方案（含 ASCII UI 图 / 数据模型 / API spec / Phase 拆分 / 风险 mitigation / 3 模板 prompt 草稿）
- ADR-0001（agentic-search-over-rag） — Python-first 一致原则
- ADR-0005（domestic-multi-model） — 多模型差异化 v1 起的起点；v5 是其延伸（对比 → 协作）
- ADR-0006（web-ui-stack-fastapi-nextjs） — v5 复用整套 Web UI 栈
- [Aider Architect/Editor Split](https://aider.chat/2024/09/26/architect.html) — Template 1 致敬
- [Dify Workflow Docs](https://docs.dify.ai/en/guides/workflow/node/llm) — 调研对照样本
- [GitHub Copilot CLI Subagent](https://fbakkensen.github.io/ai/copilot/productivity/2026/04/10/github-copilot-cli-multi-model-subagents.html) — 最直接对手
- [react-diff-viewer-continued](https://github.com/aeolun/react-diff-viewer-continued) — 前端 diff 渲染依赖

## 相关 ADR

- ADR-0001（agentic search） — 同 Python-first 决策
- ADR-0002（HTML for humans） — diff side-by-side 是 HTML 给人看原则的延伸
- ADR-0005（domestic multi-model） — v5 的直接前置
- ADR-0006（web-ui stack） — v5 复用全部 Web UI 基建
