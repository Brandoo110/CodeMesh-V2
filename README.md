# CodeMesh

> 国内多模型 Code Agent，基于 **Harness 四层架构** 的实践项目。
> 定位：**面向国内合规场景**（金融/政务/医疗 —— 不能用 OpenAI/Anthropic）的 Claude Code 类 Agent。

**特性速览**：
- 🎯 **智能路由**：PydanticAI 强类型路由到 DeepSeek / Qwen / Doubao
- 🧠 **Planner-Executor 双 Agent**：复杂任务自动拆步骤，简单任务单轮秒回
- ⚡ **流式输出**：逐 token 实时吐字
- 💰 **真实成本追踪**：每次调用算到 0.0001 元，`--compare` 一眼看出哪家性价比高
- 🔍 **代码库 RAG**：`codemesh index .` 建索引 → `--rag` 语义检索相关代码
- 📊 **Langfuse 可观测**：trace / token / 成本全链路追踪

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
┌─────────────────────────────────────────────────────────────┐
│ 编排层 Orchestration                                         │
│   router.py   —— PydanticAI 强类型路由                      │
│   hooks.py    —— Pre/Post 工具调用钩子                      │
│   adapters/   —— DeepSeek / DashScope / VolcEngine          │
├─────────────────────────────────────────────────────────────┤
│ 执行层 Execution                                             │
│   loop.py     —— Agent Loop（模型↔工具循环）                │
│   tools.py    —— bash_exec / read_file / write_file         │
│   sandbox.py  —— 危险命令拦截                               │
├─────────────────────────────────────────────────────────────┤
│ 反馈层 Feedback                                              │
│   observer.py —— Langfuse 埋点（trace、token、成本）        │
│   validator.py —— 输出校验（路径逃逸、密钥泄露）            │
├─────────────────────────────────────────────────────────────┤
│ 记忆层 Memory                                                │
│   short_term.py —— 对话历史 + 滑动窗口                      │
│   working.py    —— 当前任务结构化状态                       │
│   long_term.py  —— SQLite 跨会话持久化                      │
└─────────────────────────────────────────────────────────────┘
             ▲
             │ 顶层组装
        harness.py  ——  cli.py
```

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

### 📁 `orchestration/` — 编排层

Agent 的"调度中枢"。决定任务给谁、在关键点插入逻辑。

| 文件 | 职责 | 关键设计 |
|------|------|----------|
| `router.py`        | PydanticAI 路由 | `RouteDecision` 强类型；`Literal[...]` 限定返回值 |
| `planner.py`       | 任务拆分 Agent | `TaskPlan(steps=[Step])`；complex 任务才触发 |
| `hooks.py`         | Pre/Post hook 注册表 | 支持多回调；hook 异常不毁主流程 |
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

# 5. 跑测试
python -m tests.test_short_term      # 单元测试
python -m tests.test_adapters        # 冒烟测试（消耗少量 API 配额）
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

## 7. 项目状态

- ✅ 全部四层代码到位，可端到端跑通
- ✅ 成本追踪 / 流式输出 / Planner-Executor / RAG 四大增强已集成
- ⚠️ 未覆盖全面的单测（只有关键滑动窗口单测 + 适配器冒烟）
- ⚠️ Langfuse 统计聚合（`stats` 子命令）未实现，请直接看 Langfuse 控制台
- 💡 扩展方向：
  - 记忆压缩（让模型总结老对话再丢）
  - Docker 沙箱（真正隔离 bash_exec）
  - Hybrid search（RAG 向量 + BM25 融合）
  - AST 切 chunk（tree-sitter 代替按行切）
  - 更多工具（grep / git / diff）
