# CodeMesh

> 国内多模型 Code Agent，基于 **Harness 四层架构** 的实践项目。
> 定位：**面向国内合规场景**（金融/政务/医疗 —— 不能用 OpenAI/Anthropic）的 Claude Code 类 Agent。

**特性速览（v4，2026-05-10）**：
- 🎯 **智能路由**：PydanticAI 强类型路由到 DeepSeek / Qwen / Doubao
- 🧠 **Planner-Executor 双 Agent**：复杂任务自动拆步骤，简单任务单轮秒回
- ⚡ **流式输出**：逐 token 实时吐字
- 💰 **真实成本追踪**：每次调用算到 0.0001 元；`codemesh stats` 聚合本地日志（无需 Langfuse）；`--compare` 一眼看出哪家性价比高
- 🛠️ **Claude Code 同款工具集**：`bash_exec / read_file / write_file / glob_files / grep_text / edit_file / lsp_code`，ripgrep 优先 + Python fallback
- 🧭 **AST-based 轻量 LSP**：5 个操作（document/workspace symbol、go-to-definition、find references、hover）—— 代码搜索靠 agentic search，不靠向量
- 🔁 **跨会话记忆**：`remember_fact` / `recall_facts` 工具 + 自动注入 system prompt（SQLite 本地）
- 🪝 **标准 Hook 系统**：`PreToolUse / PostToolUse / SessionStart / ... / Stop`，PreToolUse 可 `block(reason)`
- 📦 **Skills 加载**：`.claude/skills/<name>/SKILL.md`（兼容 Anthropic 格式），自动注入索引
- 🔍 **代码库 RAG**（保留作非代码场景）：`codemesh index .` → `--rag` 语义检索；AST 切 chunk + token-aware 截断
- 🛡️ **Adapter retry**：429 / 5xx 指数退避自动重试
- 📊 **Langfuse 可观测**：trace / token / 成本全链路追踪（可选）
- 🧩 **Plugins 加载**（v4 增）：`.claude/plugins/<name>/plugin.py` 自动 import + `register(harness)`，可叠加 hooks / tools / skills / permissions
- 🛡️ **Permissions 多级**（v4 增）：ALLOW / DENY / ASK 三级规则集，PreToolUse hook 拦截危险动作
- 🔐 **Memory 7 层对齐**（v4 增）：`compactor` 二级压缩（micro + full，9 段模板）+ `auto_extract` 4 类型抽取（user / feedback / project / reference）+ `session_journal` 每会话叙事 L5 + `dreamer` 4 阶段 L6 巩固
- 🎨 **HTML 工件渲染**（v4 增）：`codemesh stats --html` 可视化 dashboard / `CODEMESH_HTML_DIFF=1` 落盘 edit diff / `CODEMESH_HTML_PLAN=1` 落盘 planner timeline / `docs/architecture.html` 交互式架构图。手写 SVG，零新 PyPI 依赖

---

## 1. 项目为什么存在

Claude Code、Cursor、Devin 都很好用，但它们都依赖境外 API。
国内有大量企业出于合规要求不能出境调模型，但同样需要"多模型调度 + 可观测性 + 沙箱安全"。
这是一个真实且还没被完全填上的市场缺口。

CodeMesh 不是要替代 Claude Code，而是演示：**在境内模型（DeepSeek / Qwen / Doubao）上如何复刻一个能用的 coding agent**，
并把 Harness 四层架构讲清楚。

**面试叙事**：当被问"你怎么理解 Agent 架构"、"为什么选 PydanticAI 不是 LangGraph"、
"你怎么做 Agent 可观测性"时，这个项目给了你具体的、能上手讲 tradeoff 的例子。

---

## 2. Harness 四层架构

```
┌──────────────────────────────────────────────────────────────────┐
│ 编排层 Orchestration                                              │
│   router.py     —— PydanticAI 强类型路由                         │
│   planner.py    —— 复杂任务拆步骤                                │
│   hooks.py      —— HookEvent 标准事件 + HookResult.block         │
│   skills.py     —— SKILL.md 加载（项目级 + 用户级）              │
│   plugins.py    —— v4 增：插件加载 + register(harness)            │
│   permissions.py—— v4 增：ALLOW/DENY/ASK 三级规则                 │
│   adapters/     —— DeepSeek / DashScope / VolcEngine / Gemini    │
│   adapters/retry.py —— async 指数退避重试（429 / 5xx）           │
├──────────────────────────────────────────────────────────────────┤
│ 执行层 Execution                                                  │
│   loop.py       —— Agent Loop（模型↔工具循环）                   │
│   tools.py      —— Tool Registry + 11 个工具                     │
│   lsp.py        —— AST-based 5 个代码导航操作                    │
│   sandbox.py    —— 危险命令拦截                                  │
├──────────────────────────────────────────────────────────────────┤
│ 反馈层 Feedback                                                   │
│   observer.py        —— Langfuse 埋点                            │
│   validator.py       —— 输出校验（路径逃逸、密钥泄露）           │
│   cost.py            —— 真实人民币成本计算                       │
│   call_log.py        —— 本地 jsonl 调用日志（stats 数据源）      │
│   token_budget.py    —— tiktoken + CJK 启发式 token 计数         │
│   compactor.py       —— v4 增：micro/full 二级压缩（9 段模板）   │
│   session_journal.py —— v4 增：每会话叙事 L5（5 门触发）         │
│   dreamer.py         —— v4 增：4 阶段 L6 巩固（24h / 5 sessions）│
│   render_html.py     —— v4 增：HTML 工件共享渲染基建（SVG 原语） │
│   stats_report.py    —— v4 增：stats --html dashboard 渲染       │
│   diff_report.py     —— v4 增：edit_file 落盘 unified diff HTML  │
│   planner_timeline.py—— v4 增：planner 时间线 HTML               │
├──────────────────────────────────────────────────────────────────┤
│ 记忆层 Memory                                                     │
│   short_term.py —— 对话历史 + 滑动窗口 + LLM 摘要压缩            │
│   working.py    —— 当前任务结构化状态                            │
│   long_term.py  —— SQLite 跨会话持久化（remember_fact 工具）     │
│   auto_extract.py —— v4 增：会话末抽取 4 类型事实（user/feedback/│
│                       project/reference + Why/How 双段）         │
└──────────────────────────────────────────────────────────────────┘
             ▲
             │ 顶层组装
        harness.py  ——  cli.py
```

> 完整交互式架构图见 [`docs/architecture.html`](docs/architecture.html)（v4 新增），点击层标题可展开下属文件，hover 看一句话职责。

**核心思想：关注点分离。** 改模型厂商不影响 Agent Loop；加新工具不影响记忆；
加 Langfuse 不影响路由。每一层只做一件事。

---

## 3. 文件地图（逐个讲）

### 📁 `memory/` — 记忆层

Agent 的"大脑海马"。把无状态的 LLM 变成有连续性的助手。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `short_term.py` | 存 messages 队列（本次对话） | `deque(maxlen=N)` 自动滑动窗口；system 消息永不淘汰 |
| `working.py`    | 存当前任务状态（正在改哪个文件、第几步） | dataclass，类型清晰；`snapshot()` 用于日志/调试 |
| `long_term.py`  | SQLite KV 存储（跨会话） | aiosqlite 异步；JSON 序列化；`~/.codemesh/memory.db` |
| `auto_extract.py` (v4) | 会话末 LLM 抽取跨会话事实 | 4 类型 user/feedback/project/reference + Why/How 双段；写到 `~/.codemesh/auto_memory/` + MEMORY.md 索引 |

### 📁 `execution/` — 执行层

Agent 的"手脚"。调模型、解析 tool_call、执行工具、循环。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `loop.py`    | Agent Loop 核心 | while 没 tool_call：调模型 → 跑工具 → 结果回填；`max_iterations` 防死循环 |
| `tools.py`   | 三个工具 + OpenAI 风格 schema | 错误以文本返回（不抛异常），模型才看得懂 |
| `sandbox.py` | 危险命令拦截 | 正则黑名单；命中抛 `SandboxViolation` |

### 📁 `feedback/` — 反馈层

Agent 的"后视镜"。没有 observability 的 Agent 等于没有 Agent。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `observer.py`  | Langfuse trace 记录 | 未配密钥时静默降级为 no-op；延迟 import |
| `validator.py` | 输出校验工具 | `check_no_secrets`、`check_path_safe` |
| `cost.py`      | 人民币成本计算 | 各家价格表 `PRICING`；`compute_cost()` 返回 `CallCost` |
| `call_log.py`  | 本地 jsonl 调用日志 | `~/.codemesh/calls.jsonl`，append-only；`stats` 子命令的数据源 |
| `token_budget.py` | tokens 计数 / 截断 | tiktoken + CJK 启发式 fallback |
| `compactor.py` (v4) | 二级会话压缩 | micro（5 条工具结果合并）+ full（9 段模板，CC 同款）；`AutoCompactState` 跟踪连续失败 |
| `session_journal.py` (v4) | L5 每会话叙事日志 | 4 段 markdown 写到 `~/.codemesh/journal/`；5 门触发（enabled / time / scan / sessions / lock）；下次相似任务自动召回 |
| `dreamer.py` (v4) | L6 4 阶段记忆巩固 | orientation / gather / consolidate / prune & index；24h / 5 sessions 触发；与 session_journal 共用 `.consolidate-lock` |
| `render_html.py` (v4) | HTML 工件共享基建 | `HtmlDoc` wrapper + 暗色主题 CSS + SVG 原语（horizontal_bar / sparkline / pie） + `write_artifact` / `rotate_dir`；零 PyPI 依赖 |
| `stats_report.py` (v4) | `codemesh stats --html` 渲染器 | KPI / 各模型成本横条 / pie / 按天 sparkline / 详细表 |
| `diff_report.py` (v4) | edit_file unified diff HTML | env `CODEMESH_HTML_DIFF=1` 触发；写到 `.codemesh/diffs/`；rotate keep 20 |
| `planner_timeline.py` (v4) | planner 时间线 HTML | env `CODEMESH_HTML_PLAN=1` 触发；按耗时占比横条 + 步骤卡片 |

### 📁 `orchestration/` — 编排层

Agent 的"调度中枢"。决定任务给谁、在关键点插入逻辑。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `router.py`        | PydanticAI 路由 | `RouteDecision` 强类型；`Literal[...]` 限定返回值 |
| `planner.py`       | 任务拆分 Agent | `TaskPlan(steps=[Step])`；complex 任务才触发 |
| `hooks.py`         | Pre/Post hook 注册表 | 支持多回调；hook 异常不毁主流程 |
| `skills.py`        | SKILL.md 加载 | 扫 `.claude/skills/` + `~/.codemesh/skills/`；自动注入索引 |
| `plugins.py` (v4)  | 插件加载 | `.claude/plugins/<name>/plugin.py` + `register(harness)`；可叠加 hooks/tools/skills/permissions |
| `permissions.py` (v4) | ALLOW/DENY/ASK 三级 | 默认规则集（force-push / pip install / 系统路径写入...）；通过 PreToolUse hook 拦截 |
| `adapters/base.py` | 统一接口 Protocol | `ModelAdapter` 协议；暴露 `last_usage` 给成本追踪 |
| `adapters/deepseek.py`   | DeepSeek（推理强） | OpenAI 兼容；支持 streaming |
| `adapters/dashscope.py`  | 阿里 Qwen（代码强） | 同上 |
| `adapters/volcengine.py` | 字节 Doubao（快而廉） | 同上；用 endpoint ID 调模型 |

### 📁 `rag/` — 代码库检索（可选）

让 Agent 在回答前先"看一眼"整个 codebase 相关代码。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `embedder.py`  | 文本 → 向量 | 调 DashScope text-embedding-v3；批量处理 |
| `indexer.py`   | 扫描代码 → 切 chunk → 建索引 | 40 行/chunk，10 行 overlap；忽略 node_modules 等；存 ChromaDB |
| `retriever.py` | 查询 → topK 片段 | 返回带文件路径+行号的 `Hit`；`format_context` 拼给模型 |

### 🔧 顶层

| 文件 | 职责 |
|------|------|
| `harness.py` | 四层组装，暴露 `async Harness.run(task)` 和 `.compare(task)` |
| `cli.py`     | Typer + Rich，`codemesh "任务"` / `--compare` / `--stats` |
| `tests/`     | 冒烟测试（适配器）+ 单测（滑动窗口）|

---

## 4. 快速开始

**环境要求**：Python ≥ 3.10。macOS / 新版 Linux 会因为 PEP 668 阻止直接 `pip install`，请用 venv（下面会写）。

```bash
# 1. 创建虚拟环境（macOS 上必须，否则 PEP 668 报错）
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. 安装依赖
pip install -e .                   # 基础版
pip install -e ".[rag]"            # 带 RAG 的版本（多装 chromadb）

# 3. 配置 API Key
cp .env.example .env
# 生产 / 合规场景：至少填 DEEPSEEK_API_KEY
# 只想学习 / 跑通 demo：只填 GEMINI_API_KEY 即可（router/planner/adapter 会自动走 Gemini）

# 4. 常用命令
codemesh run "帮我写个脚本读所有 .md 文件"           # 自动分流
codemesh run "重构 auth 模块提高可测试性" --rag     # 复杂任务 + RAG
codemesh run "一句话解释 Harness" --compare        # 三家对比 + 成本表
codemesh run "什么是 Protocol" --stream            # 流式输出
codemesh index .                                   # 建 RAG 索引
codemesh stats                                     # 终端聚合表
codemesh stats --html                              # v4 增：HTML dashboard

# 5. 跑测试
python -m tests.test_short_term      # 单元测试
python -m tests.test_adapters        # 冒烟测试（消耗少量 API 配额）

# 6. v4 可选环境变量
# export CODEMESH_HTML_DIFF=1   # edit_file 时落盘 unified diff HTML
# export CODEMESH_HTML_PLAN=1   # complex 任务跑完落盘 planner timeline HTML
# 两者都默认关；落到 .codemesh/diffs/ 和 .codemesh/plans/，按 mtime 滚动 20 个
```

**单 key happy path（最快上手）**：
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# 在 .env 里只填 GEMINI_API_KEY=<your_key>（GOOGLE_API_KEY 也认）
codemesh run "用一句话解释 Harness 架构" --stream
```

**常见坑**
- `ModuleNotFoundError: No module named 'cli'`（Python ≥ 3.14）：
  3.14 把以 `_` 开头的 `.pth` 当隐藏文件跳过，老 setuptools 生成的
  `__editable__.*.pth` 会静默失效。执行 `pip install -U setuptools`
  再 `pip install -e . --force-reinstall --no-deps` 即可。
- `--compare` 只填了一个 key 时会撞厂商并发限流（Gemini 单 key 三路撞 503
  UNAVAILABLE 是正常的）。想看真对比请把三家国内 key 都填上。

---

## 5. 如何学习这个项目

> **新手请先打开 `LEARNING_PATH.md`** —— 里面有从零起步的学习路径，
> 按顺序读文件，每一步都告诉你要理解什么、会问什么面试题。

---

## 6. 面试速记

| 问题 | 30 秒答案 |
|------|-----------|
| Harness 四层是什么？| Memory / Execution / Feedback / Orchestration。从底到顶依次是状态、行动、观测、调度。 |
| 为什么 PydanticAI 不是 LangGraph？| 路由是单轮结构化决策，不需要状态机；PydanticAI 的 Literal 约束 + 自动重试完美匹配。LangGraph 适合多节点有状态编排。 |
| Agent Loop 怎么防死循环？| `max_iterations` 硬上限；生产再加"监工" Agent 检测重复调用。 |
| 三个模型怎么选？| DeepSeek=复杂推理，Qwen=代码生成，Doubao=简单快问答。按复杂度和成本路由。 |
| 沙箱怎么做？| 正则黑名单拦截 `rm -rf /`、`sudo`、`DROP TABLE` 等；生产上容器化（Docker / Firejail）。 |
| 可观测性？| Langfuse trace 每次调用的 prompt、模型、token、延迟、路由理由。未配置密钥时静默降级。 |

---

## 7. 项目状态（v3，2026-05-04）

- ✅ 全部四层代码到位，可端到端跑通
- ✅ 成本追踪 / 流式输出 / Planner-Executor / RAG 四大增强已集成
- ✅ **记忆压缩**：超阈值后用 doubao 摘要老对话（v2 增）
- ✅ **stats 子命令**：本地 jsonl 聚合 token / 成本 / 延迟，不依赖 Langfuse（v3 增）
- ✅ **测试覆盖**：14 个测试文件、200 + 用例，全部纯单测无网络（v2/v3 加）
- ✅ **Tool Registry**：`@registry.register` 模式加新工具（v2 增）
- ✅ **Glob / Grep / Edit / LSP**：Claude Code 标准检索三件套 + AST-based 代码导航；ripgrep 优先 + Python fallback（v2/v3 增）
- ✅ **Hooks 标准事件**：`PreToolUse / PostToolUse / SessionStart / SessionEnd / UserPromptSubmit / Stop` + `HookResult.block(reason)` 拦截（v3 增）
- ✅ **跨会话长期记忆**：`remember_fact / recall_facts / forget_fact` 三个工具 + 自动注入 system prompt（v3 增）
- ✅ **Token-aware context budget**：tiktoken + CJK 启发式 fallback（v3 增）
- ✅ **AST chunking**：Python 文件按 def/class 切 chunk（v3 增）
- ✅ **Skills 加载**：兼容 Anthropic SKILL.md 格式，自动扫 `.claude/skills/` 和 `~/.codemesh/skills/`（v3 增）
- ✅ **Adapter retry**：所有适配器 `complete()` 加指数退避，处理 429 / 5xx（v3 增）
- 💡 v3 当时写的"后续扩展方向"——除流式 retry / MCP / Reranker / Docker 沙箱外，**Permissions 多级 / Plugins 机制都已在 v4 兑现**

## 8. 项目状态（v4，2026-05-10）

v3 → v4 一句话总结：**编排 / 记忆 / 反馈三层都补齐了 OpenHarness 已有但本项目缺的能力**，并基于 thariqs 的 thesis 把"给人看"的产物从 markdown 升级为自包含 HTML。

- ✅ **Plugins 机制**：`.claude/plugins/<name>/plugin.py` + `register(harness)` 自动加载，可叠加 hooks / tools / skills / permissions（v3 后续扩展计划兑现）
- ✅ **Permissions 多级**：ALLOW / DENY / ASK 三级规则集，PreToolUse hook 拦截危险动作（同上）
- ✅ **Memory 7 层对齐 OpenHarness / Claude Code**：
  - `compactor.py` —— L2 micro + L4 full 二级压缩，9 段 CC 同款模板，`AutoCompactState` 跟踪连续失败防止反复浪费 token
  - `auto_extract.py` —— L5 会话末抽取 4 类型（user / feedback / project / reference）+ Why/How 双段，写到 `~/.codemesh/auto_memory/` + MEMORY.md 索引
  - `session_journal.py` —— L5 叙事变体，每会话末写 4 段 markdown 到 `~/.codemesh/journal/`，5 门触发，下次相似任务自动召回
  - `dreamer.py` —— L6 真 dreaming，4 阶段巩固（orientation / gather / consolidate / prune & index），24h / 5 sessions 触发（CC `DreamTask` 同款语义）
  - 关键认知：**L5 ≠ L6**。L5 是"记新事"，L6 是"整理已记的事"，两者不是同一个东西
- ✅ **HTML 工件渲染**（灵感：[thariqs · The Unreasonable Effectiveness of HTML](https://thariqs.github.io/html-effectiveness/)）：
  - `feedback/render_html.py` —— 共享基建：暗色主题 CSS + 手写 SVG 原语 + 文件滚动；零新 PyPI 依赖
  - `codemesh stats --html` —— dashboard：KPI / 各模型成本横条 / pie / 按天 sparkline / 详细表
  - `CODEMESH_HTML_DIFF=1` —— `edit_file` 后落盘 unified diff HTML（绿/红块、行号、上下文）
  - `CODEMESH_HTML_PLAN=1` —— complex 任务后落盘 planner timeline（耗时占比横条 + 步骤卡片 + 状态色）
  - `docs/architecture.html` —— 交互式 4 层架构图（点 ▶ 折叠每层，hover 看一句话职责）
  - `docs/index.html` —— 工件 showcase 主页
  - 关键边界：**HTML 给"人"看，不给"agent"吃**。tool returns 仍是字符串，否则会污染 token 经济、模型也消化不了
- ✅ **测试覆盖**：v4 新增 4 个 HTML 渲染测试模块（render_html / stats_report / diff_report / planner_timeline，60 个 case）+ 4 个记忆层测试模块（compactor / auto_extract / session_journal / dreamer，70+ case），全部走"纯 Python + `if __name__ == '__main__'` runner"，无 pytest 依赖、无网络
- 💡 后续可做：
  - **iframe srcdoc 嵌入真实工件预览**：`docs/index.html` 当前用手画 SVG 缩略图，可换成真实生成的工件
  - **stats sparkline hover tooltip**：当前只有静态轨迹，加 `<title>` / mousemove 能看每天具体调用列表
  - **planner timeline 实时刷新**：当前是任务结束后渲染，加 long-poll / SSE 能边跑边看
  - **diff render syntax highlight**：当前纯白文本，可写 100 行纯 Python tokenizer 给 .py 文件染色（保持零依赖）
  - **流式 retry**：buffer 前 N 个 chunk 才能在断流后从头重发
  - **MCP client**：Anthropic 生态接入（filesystem / github / brave 等）
  - **Reranker**：非代码 RAG 的 cross-encoder 重排
  - **Docker 沙箱**：真正隔离 bash_exec
- 📖 完整改动叙事看 `DEVLOG.md`（每个 commit 都有"背景 / 改动 / tradeoff / 面试故事"段）；工作守则看 `CLAUDE.md`
