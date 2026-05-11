# CodeMesh 学习路径

> 把你从"听说过 Agent"带到"在面试中能讲清 Harness 架构"的循序渐进指南。
> 每个阶段给你：**要读的文件**、**要搞懂的概念**、**能回答的面试题**。

预计总学习时长：**12.5–16.5 小时**（v4 末，含 dreaming / HTML 工件 / Memory 7 层 / ADR 讲述层进阶）。按阶段来，不要跳。

> 路线总览：v1 主体（阶段 0-9，~8h）→ 进阶（阶段 10 + 末尾"v2/v3/v4 进阶"，~4.5-8.5h，含 ADR 讲述层 30min）。
> 想拿这个项目去面试，**v1 主体 + 至少一段 v3 或 v4 进阶 + 进阶 F（ADR 讲述层）**是最低门槛。

---

## 阶段 0 · 搞清楚概念基础（1 小时）

在读任何代码前，请用自己的话回答下面几个问题。不会就先 Google / 问 Claude Code：

1. LLM 本身是无状态的，为什么我们还能"和它对话"？
2. 什么是 **tool use / function calling**？为什么它让 LLM 能跑命令？
3. 什么是 **Agent Loop**？它和普通 Chat API 区别在哪？
4. **OpenAI API 协议**的 messages 格式长什么样？（`role: user/assistant/system/tool`）
5. **异步编程**（async/await）基本语法 —— 不会 async 读这个项目会卡住。

**通过标志**：你能画出"用户 → 模型 → 工具 → 结果 → 模型 → 最终回复"这个闭环。

---

## 阶段 1 · 读项目总览（30 分钟）

按顺序读：

1. `README.md` —— 看 Harness 四层图、文件地图、面试速记
2. `pyproject.toml` —— 看一眼依赖，每个库是干什么的心里有数

**你要能回答**：

- 这个项目解决什么真实问题？
- 四层分别叫什么？顺序从下到上？
- DeepSeek / Qwen / Doubao 分别擅长什么？

---

## 阶段 2 · 记忆层（最简单，先读建立信心）（1 小时）

**读的顺序**：

1. `memory/short_term.py` —— 最简单，理解"消息队列 + 滑动窗口"
2. `memory/working.py` —— dataclass，看"工作记忆 vs 对话记忆"的差异
3. `memory/long_term.py` —— aiosqlite 异步 KV 存储
4. `tests/test_short_term.py` —— 看单测怎么写

**核心概念**：

- 为什么 LLM 是无状态的，但 Agent 能记住东西？→ 每轮把完整历史再传进去
- **滑动窗口** vs **记忆压缩**：两种避免爆 context 的策略
- 短期记忆（messages）vs 工作记忆（结构化状态）vs 长期记忆（持久化）

**面试题自测**：

- Q: context 快满了怎么办？
- Q: 为什么 system message 要特殊对待？
- Q: 为什么选 SQLite 做长期记忆？
- Q: 滑动窗口按消息数切和按 token 数切的取舍？

---

## 阶段 3 · 适配器层（理解适配器模式）（1 小时）

**读的顺序**：

1. `orchestration/adapters/base.py` —— 先看 Protocol 定义，理解"接口是什么"
2. `orchestration/adapters/deepseek.py` —— 最标准的实现
3. `orchestration/adapters/dashscope.py` —— 注意和 DeepSeek 几乎一样（抹平差异的结果）
4. `orchestration/adapters/volcengine.py` —— 注意 endpoint_id 这个坑
5. `tests/test_adapters.py` —— 看怎么验证三家接口一致

**核心概念**：

- **适配器模式**（Adapter Pattern）—— 设计模式里的"万能胶"
- **Protocol vs ABC** —— Python 结构化子类型
- **OpenAI 兼容端点** —— 国内厂商普遍提供的"最大公约数"协议
- **为什么要 async** —— 并发调多家模型时节省 2/3 时间

**动手**：

- 想想如果要加 GLM（智谱）怎么扩展？答：新建 `glm.py`，实现 `complete` 即可，其他层完全不动。这就是适配器模式的威力。

**面试题自测**：

- Q: 为什么用 Protocol 不用 ABC？
- Q: 三家 API 差异很大，你怎么抹平的？
- Q: `complete` 为什么只返回 `str`，不返回完整 response？

---

## 阶段 4 · 执行层（项目最核心，重点学）（2 小时）

**读的顺序**：

1. `execution/tools.py` —— 先看具体工具怎么写 + OpenAI tool schema 格式
2. `execution/sandbox.py` —— 看安全检查怎么做
3. `execution/loop.py` —— ⭐ 最重要，**整个 Agent 的心脏**

**核心概念**：

- **tool use 协议** —— 模型怎么声明"我想调工具"，运行时怎么执行
- **Agent Loop** —— 把模型变 Agent 的唯一秘密（其实就是个 while 循环）
- `tool_call_id` 匹配机制 —— 为什么每次 tool 返回要带回 id
- **错误作为返回值** —— 给模型的工具不能抛异常，要把错误文本化
- **沙箱哲学** —— 黑名单 vs 白名单 vs 容器化

**动手**：

- 在 `loop.py` 每一行画注释。理解 `convo.append()` 的每一次为什么
- 想"如果我要加 grep 工具，需要改哪几行？"答：`tools.py` 加函数 + 加 schema + 加 `TOOL_IMPL` 映射，别的都不动

**面试题自测**：

- Q: Agent Loop 怎么防死循环？
- Q: 一轮工具调用有多个，能并行吗？
- Q: 你怎么做安全沙箱？生产环境怎么做？
- Q: 工具函数为什么不能直接抛异常？
- Q: Claude Code 的 tool_use 协议和 OpenAI 的 tool_calls 差异？（Anthropic 用 `stop_reason="tool_use"`，OpenAI 用 `finish_reason="tool_calls"` + `choices[0].message.tool_calls`）

---

## 阶段 5 · 编排层 · Router + Hooks（重点面试点）（1.5 小时）

**读的顺序**：

1. `orchestration/hooks.py` —— 先看 hook 系统（比较简单）
2. `orchestration/router.py` —— ⭐ **PydanticAI 的用法 + 面试必考点**

**核心概念**：

- **Hook 模式** —— 横切关注点（日志、权限、审计）怎么优雅插入
- **PydanticAI** —— 一个专门把"LLM 输出"和"Python 类型"对齐的框架
- **Literal[...] 约束** —— 让模型只能返回枚举值，乱写就自动重试
- **PydanticAI vs LangGraph 的哲学差异** —— ⭐⭐⭐ 面试必背

**PydanticAI vs LangGraph（背下来）**：
| 维度 | PydanticAI | LangGraph |
|------|-----------|-----------|
| 定位 | 结构化输出约束 | 状态图工作流 |
| 适合 | 单轮决策、RAG | 多节点有状态、planner |
| 核心 | BaseModel 输出类型 | Graph + State |
| 成本 | 启动轻，代码少 | 学习曲线陡 |

CodeMesh 路由是"输入任务 → 输出决策"的单轮强结构化，PydanticAI 完美匹配。
如果要做"规划 → 执行 → 反思 → 再规划"就应该用 LangGraph。

**面试题自测**：

- Q: 为什么选 PydanticAI 不是 LangGraph？（最高频）
- Q: Hook 和装饰器的区别？
- Q: 如果 hook 抛异常会怎样？你怎么兜底？
- Q: Literal 的 validator 是怎么工作的？它和普通 Pydantic 字段有什么不同？

---

## 阶段 6 · 反馈层（观测是工程师的 level）（1 小时）

**读的顺序**：

1. `feedback/observer.py` —— Langfuse 埋点 + 优雅降级
2. `feedback/validator.py` —— 两个通用校验函数

**核心概念**：

- **LLM Observability** —— 业界共识：没 obs 等于没 Agent
- **Trace / Span** —— 一次请求一个 trace，内部套多个 span
- **优雅降级**（graceful degradation）—— 没配密钥时静默关闭，不炸
- Langfuse 主要价值：Token 成本追踪、Prompt 版本管理、自动评估

**动手**：

- 跑一遍 `codemesh "写个 hello world"`，然后到 Langfuse 控制台看 trace（如果配了）
- 想："如果模型输出了我的 API Key 怎么办？" 看 `validator.check_no_secrets`

**面试题自测**：

- Q: 为什么要可观测性？具体记录哪些字段？
- Q: 为什么选 Langfuse 不自己写日志？
- Q: 如果 Langfuse 挂了会影响主流程吗？你怎么设计降级？

---

## 阶段 7 · 顶层组装（看全局）（1 小时）

**读的顺序**：

1. `harness.py` —— ⭐ 四层怎么串起来，每一层是什么顺序被调用
2. `cli.py` —— Typer + Rich 怎么写漂亮 CLI
3. 回头对照 `README.md` 的架构图，确认每一层你都能在代码里指出来

**核心概念**：

- **依赖注入**（adapter 按需懒加载 + 缓存）
- **async CLI** —— Typer 同步入口 + asyncio.run() 拉异步协程
- **compare 模式怎么利用 asyncio.gather 并发**

**面试题自测**：

- Q: Harness.run() 里四层是什么顺序协作的？（背住这个顺序）
- Q: `_get_adapter` 为什么要缓存？
- Q: `compare` 模式并发调三家总耗时约等于最慢那个，原理？

---

## 阶段 8 · 端到端跑 + Review（1 小时）

1. 配好 `.env`，至少填 `DEEPSEEK_API_KEY`
2. `pip install -e .`
3. 跑几条任务观察输出：
   
   ```bash
   codemesh "列出当前目录所有 Python 文件"
   codemesh "read_file cli.py 然后解释 _run_compare 做了什么"
   codemesh --compare "用 10 个字介绍 Linux"
   ```
4. 观察控制台：`[router]` 日志告诉你路由决策；`[tool→]/[tool←]` 告诉你每次工具调用
5. 如果装了 Langfuse，看 trace

**最后一个练习（面试级）**：
给朋友 5 分钟内讲清楚这个项目。
讲不清说明还有盲区，回头找到对应阶段重读。

---

## 阶段 9 · 四个增强模块（项目的亮点，面试重头戏）（2 小时）

学完前面 8 阶段你已经有一个能跑的 Agent，但下面这 4 个让项目从"能跑"变成"亮眼"。

### 9.1 成本追踪（30 分钟）

**读的顺序**：

1. `feedback/cost.py` —— 价格表 + CallCost dataclass
2. `orchestration/adapters/base.py` —— `Usage` 和 `last_usage` 属性
3. `orchestration/adapters/deepseek.py` —— 看 `resp.usage.prompt_tokens` 怎么取
4. `harness.py::_record_cost` —— 记账逻辑

**核心概念**：

- **API usage 字段** —— OpenAI 风格响应里 `usage.prompt_tokens / completion_tokens`
- **副作用模式暴露状态** —— 为什么用 `last_usage` 属性而不是改 `complete()` 返回值？
  答：兼容性，不破坏已有代码；副作用在"一个 adapter 不会并发调自己"的前提下是安全的
- **输入/输出定价不对称** —— output 通常是 input 的 2–4 倍，因为推理是串行的

**面试题自测**：

- Q: 你怎么知道模型调用的真实成本？
- Q: 为什么 input 和 output 价格不一样？
- Q: 流式输出怎么拿到 token 数？（答：`stream_options={"include_usage": True}`）

### 9.2 Streaming 流式输出（20 分钟）

**读的顺序**：

1. `orchestration/adapters/*.py` 里 `complete_stream` 方法 —— async generator
2. `harness.py::run_stream` —— 对外 streaming 接口
3. `cli.py::_run` 里 `--stream` 分支 —— 实时 print

**核心概念**：

- **async generator** —— `async def ... yield`，配合 `async for` 消费
- **SSE（Server-Sent Events）** —— OpenAI 流式 API 的协议基础
- **Streaming 的 UX 价值** —— 首字延迟 vs 总耗时

**面试题自测**：

- Q: Streaming 怎么实现的？返回类型是什么？
- Q: 流式输出下工具调用怎么处理？（答：tool_call 需要完整累积才能执行，不能边流边调）

### 9.3 Planner-Executor 双 Agent（30 分钟）

**读的顺序**：

1. `orchestration/planner.py` —— `TaskPlan` 和 PydanticAI 第二次出场
2. `harness.py::_run_planned` —— 怎么逐步执行
3. `harness.py::run` 里 `if decision.complexity == "simple"` 的分流

**核心概念**：

- **单 Agent vs 多 Agent** —— 什么时候该拆？
- **Planner 的模型选择** —— 推理好的模型（DeepSeek）；Executor 工具稳的即可
- **同样是 PydanticAI，router 和 planner 的区别** —— 输出 schema 复杂度不同

**面试题自测**：

- Q: 为什么要拆 Planner 和 Executor？
- Q: 如果某一步失败了，整个计划怎么办？（答：CodeMesh MVP 没做 retry/回滚；生产要设计）
- Q: 这像不像 LangGraph？为什么不直接用 LangGraph？（YAGNI 渐进引入）

### 9.4 RAG 代码库检索（40 分钟，**最值得讲**）

**读的顺序**：

1. `rag/embedder.py` —— 调 embedding 的最小封装
2. `rag/indexer.py` —— chunk 策略 + ChromaDB 用法
3. `rag/retriever.py` —— query → Hit 列表 → format_context
4. `harness.py::_build_system_with_context` —— 把检索结果塞进 system prompt

**核心概念**：

- **Embedding** —— 文本 → 向量 → 语义相似度
- **Chunk 策略**：按行 / 按 AST / 按标题，各自适合什么场景
- **向量数据库** —— ChromaDB / Qdrant / Milvus / FAISS 对比
- **RAG 的完整管道**：scan → chunk → embed → store → retrieve → augment
- **Context 窗口预算** —— 检索结果 4000 字符 ≈ 2k token，不能塞爆

**面试题自测**：

- Q: RAG 是什么？为什么需要？
- Q: 代码文件怎么 chunk？按行切和按 AST 切的取舍？
- Q: 为什么选 ChromaDB？和 FAISS / Qdrant 区别？
- Q: topK 选多少？怎么动态调？
- Q: 如果代码库改了怎么更新索引？（增量 vs 全量重建的取舍）

> **v3 后的关键修订（必看）**：读了 OpenHarness 源码后发现 Coding Agent 业界事实标准
> **不是**向量 RAG（BM25+向量+RRF 那套），而是 `grep + glob + AST-LSP + read` 的 agentic search。
> `rag/` 模块在 v3 后**保留作非代码场景**（文档库、知识库），代码搜索完全走 agentic 路径。
> 这是面试里很值得讲的"读源码后调整方向"故事——见末尾"进阶 B"。

---

## 阶段 10 · 延伸与扩展（v1 设想 vs 实际去向）

> 这段原本是 v1 末写的"还能做什么"清单。v2-v4 把其中 5 项做掉了，下面用对照表保留——
> 看自己的扩展直觉是不是和后续迭代方向对得上，比再写一遍 todo 有信息量。

| 项 | v1 时设想 | 实际去向 |
|---|---|---|
| 加一个工具（grep_file） | 只改 tools.py | ✅ v2: glob_files / grep_text / edit_file / lsp_code 全都加了 |
| 加一个模型 | 智谱 GLM / Kimi | ✅ v3: 加 Gemini fallback（单 key 跑通四层）|
| 换 LangGraph 重写 Planner | 体验状态图 | ⬜ 没做。YAGNI 渐进引入；当前 PydanticAI 单轮决策够用 |
| 实现 stats 命令 | 查 Langfuse API | ✅ v3: 改本地 jsonl，不依赖外网；v4 追加 `--html` dashboard |
| 容器化沙箱 | bash_exec 放 Docker | ⬜ 没做。v4 加了 Permissions 多级缓解，Docker 仍是更硬的方案 |
| 记忆压缩 | short_term 快满时摘要 | ✅ v2: ShortTermMemory.maybe_compress（doubao 摘要器）；v4 进阶到 compactor 二级压缩 + AutoCompactState |
| Hybrid RAG | BM25 + 向量 + RRF | ⬜ **故意不做**。v3 调研后定位 `rag/` 为非代码场景；代码搜索走 agentic search |
| AST 切 chunk | tree-sitter 替换按行切 | ✅ v3: rag/ast_chunker.py（stdlib `ast`，Python-only 场景够用）|
| 架构决策档案化 | v1 没规划 | ✅ v4 末: `docs/decisions/` 5 份 ADR；讲述层第一手素材（详见进阶 F）|

读完这张表你已经隐约感觉到"读源码 → 调整方向"是这个项目的工作模式。**真正还想动手做的事**看末尾"进阶 D"末段的清单。

---

## 学完你会什么

**v1 主体**：

✅ 能在白板上画出 Harness 四层架构图
✅ 能讲清 PydanticAI vs LangGraph 的选型 tradeoff
✅ 能描述 Agent Loop 的每一步 + 防死循环方案
✅ 能讲清适配器模式在多模型调度中的应用
✅ 能讲清为什么 LLM 可观测性是一等公民
✅ 理解国内模型生态：DeepSeek / Qwen / Doubao 各自的定位
✅ 面试时能信手拿代码举例，而不是空谈概念

**v2-v4 进阶（看完末尾进阶段后追加）**：

✅ 能讲"代码搜索为啥不靠向量 RAG，靠 agentic search"（v3）
✅ 能讲 OpenHarness 8 个核心子系统跟 CodeMesh 的对位（v3-v4）
✅ 能讲 Memory 7 层架构 + L5 / L6 边界（v4）
✅ 能讲 HTML 工件的"给人看 vs 给 agent 吃"边界（v4）
✅ 能讲"thesis-driven 迭代"和"用户痛点驱动"的差异（v4 自我反思）
✅ 能用 5 份 ADR 系统化讲述每个架构决策 + "诚实坏处"模板主动 disarm 面试反问（v4 末）

---

## v2 / v3 / v4 进阶（在 v1 全部读完之后）

> v1 是骨架；v2-v4 把它做成"真能用"的肌肉。整体 90-150 分钟。
> 读完你能讲完整的"我做了四轮迭代 + 一次自我修正"故事，比"我跑通了一个 Agent demo"信息量大 10 倍。
>
> 子标题用"进阶 A/B/C/D"避免和阶段 0-9 编号冲突——它们和阶段 0-9 是平行段，不是延续。

### 进阶 A · v2 工程化补齐（30 分钟）

**目标**：理解为什么"能跑通"和"工程化"之间还差很远。

**读什么**：
1. `memory/short_term.py` 末尾的 `maybe_compress` —— 滑动窗口之外加一层 LLM 摘要压缩
2. `execution/tools.py` 的 `ToolRegistry` —— 怎么从 3 个硬编码工具升级到注册表模式
3. `execution/tools.py` 的 `glob_files / grep_text / edit_file` —— Claude Code 标准三件套
4. `execution/sandbox.py` 顶部的 rm 正则（commit `9f74e59`）—— 一个写测试时挖出的真 false-positive bug

**面试故事**：
> "v1 跑通整套四层后做 v2：补单测、加 Tool Registry、加 Glob/Grep/Edit。
>  写测试时挖出一个生产代码里跑了几个月的 sandbox 误伤 bug —— 这就是写测试的价值。"

### 进阶 B · v3 对齐 OpenHarness / Claude Code（45 分钟）

**目标**：理解 Coding Agent 的工业事实标准——**为什么不用向量 RAG**、AST-LSP 怎么做、Skill 是什么。

**读什么**（按顺序）：

1. **`execution/tools.py` 的 `_rg_grep` / `_rg_glob_files`**
   流式读 stdout、超时控制、SIGTERM→2s→SIGKILL、退出码白名单 {0, 1, -15, -9}。
   对照原型：HKUDS/OpenHarness `src/openharness/tools/grep_tool.py`（363 行）。
2. **`execution/lsp.py` 完整通读**
   stdlib `ast` 替代 pyright daemon。理解为什么单次任务 1-2 次查询不值得起 daemon。
3. **`memory/long_term.py` 末尾单例 + `execution/tools.py` 的 `remember_fact / recall_facts / forget_fact`**
   dead code 怎么变成跨会话记忆能力。
4. **`feedback/call_log.py` + `cli.py` 的 stats 命令**
   本地 JSONL 替代外网 Langfuse 的兜底设计。
5. **`orchestration/hooks.py` 的 `HookEvent` 枚举 + `HookResult.block`**
   Claude Code 标准事件命名 + PreToolUse 短路拦截语义。
6. **`feedback/token_budget.py`** —— tiktoken + CJK 启发式 fallback。中英 4 倍 token 差的故事。
7. **`rag/ast_chunker.py`** —— 为什么 stdlib `ast` 在 Python-only 场景下能替代 tree-sitter。
8. **`orchestration/skills.py` + `.claude/skills/<name>/SKILL.md`** —— Anthropic skill 格式怎么注入 Agent system prompt。
9. **`orchestration/adapters/retry.py`** —— 20 行手写指数退避 + jitter；为什么不引 tenacity。

**关键认知（v3 必背）**：
> "我**之前以为**做 RAG 就要 BM25 + 向量 + RRF（LangChain 那套）。读了 OpenHarness 源码才发现
>  Coding Agent 业界事实标准不是这个——是 `grep + glob + AST-LSP + read` 的 agentic search，
>  让模型自己决定查什么。原因：向量会陈旧、对函数名 / 错误码精确匹配差、要花钱建索引。
>  我把 `rag/` 模块**保留作非代码场景**（文档、知识库），代码搜索完全走 agentic 路径。"

### 进阶 C · v4 收尾批 - 编排层完结（30 分钟）

**目标**：理解为什么 Plugins / Permissions / Reranker LLM 版 / 流式 retry 是"补完整套 Coding Agent"的必经之路。

**读什么**：

1. **`orchestration/permissions.py`** —— ALLOW / DENY / ASK 三级规则集 + 默认拦截 force-push / pip install / 系统路径写入
2. **`orchestration/plugins.py`** —— `.claude/plugins/<name>/plugin.py` + `register(harness)`，可叠加 hooks/tools/skills/permissions
3. **`orchestration/adapters/`** 流式 retry 的 buffer-prefix 思路
4. **`rag/reranker.py`** —— 用 LLM 当 reranker 的 0 依赖方案
5. **`tests/test_permissions.py` / `tests/test_plugins.py`** —— 看怎么测插件加载和权限拦截

**关键认知（v4 中段）**：
> "v3 末我列了'后续扩展方向'，包括 Permissions 和 Plugins。v4 把它们做了——这两块都是
>  '把项目从单人工具变成可被插件扩展的 framework'的必经之路。Plugins 让用户能往项目里
>  注入自定义逻辑而不改源码，是开源项目的标配。"

### 进阶 D · v4 末段 - Memory 7 层 + dreaming + HTML 工件（60 分钟）

**目标**：理解 Claude Code 7 层记忆架构、弄清 **L5 ≠ L6** 的边界，以及"thesis-driven 迭代"和"用户痛点驱动"的差异。

**读什么**（按顺序）：

1. **`feedback/compactor.py`** —— L2 microcompact + L4 full compaction
   - 9 段模板 verbatim 抄自 Claude Code source map（2026-03-31 npm 误打包暴露）
   - `AutoCompactState.consecutive_failures=3` 状态机：连续失败防止坏 LLM 反复浪费钱
   - 字面常量对齐 OH / CC：`AUTOCOMPACT_BUFFER_TOKENS=13_000` 等

2. **`memory/auto_extract.py`** —— L5 自动事实抽取
   - 4 种类型：user / feedback / project / reference
   - **Why / How 双段**强迫模型记因果 + 应用场景，不只是结论
   - MEMORY.md 索引硬约束（s56=200 行 / j58=25000 字节，CC 同款）

3. **`feedback/session_journal.py`** —— L5 叙事变体（旧名 dreamer）
   - **5 门触发**：Enabled → Time(24h) → Scan(10min) → Sessions(≥5) → Lock；按成本递增 99% 调用早退出
   - `.consolidate-lock` 文件 PID:timestamp 崩溃恢复

4. **`feedback/dreamer.py`** —— L6 真 dreaming（重要：和 session_journal 不是同一回事）
   - **4 阶段**：orientation → gather → consolidate → prune & index
   - **L5 是"记新事"，L6 是"整理已记的事"** —— 这是 2026-05-09 晚发现命名错误后修正的 self-correction 故事

5. **`feedback/render_html.py`** —— HTML 工件共享基建
   - HtmlDoc wrapper + 暗色 CSS + 手写 SVG 原语（horizontal_bar / sparkline / pie）+ 文件滚动
   - 零新 PyPI 依赖（全靠字符串模板和 inline SVG）

6. **`feedback/stats_report.py` + `cli.py` 的 `--html` 分支** —— stats dashboard
   - 跑 `codemesh stats --html` 看实际效果，比读代码直观

7. **`feedback/diff_report.py` / `feedback/planner_timeline.py`**（选学）
   - env 控制的可选钩子（`CODEMESH_HTML_DIFF` / `CODEMESH_HTML_PLAN`），默认关

8. **`docs/architecture.html` + `docs/index.html`** —— 在浏览器里打开看
   - 作为 README ASCII 架构图的对照——结构是不是真长这样

**关键认知（v4 末必背）**：

> **L5 ≠ L6**：L5（auto_extract / session_journal）是"记新事"——会话末把这次的事抽成结构化条目；
> L6（dreamer）是"整理已记的事"——稀疏触发，4 阶段 consolidation 把零散记忆合并去重重建索引。
> 我做这块时第一版把 L5 当成 dreaming 做了，发现错了之后改名 + 写真 L6——这个 self-correction
> 故事比单纯说"我做了 dreaming"更可信。

> **HTML 给"人"看，不给"agent"吃**：tool returns 必须保持字符串，否则会污染 token 经济、模型也消化不了。
> 这是 thariqs 的 thesis 最容易被搞混的一条边界。

**面试题自测（v4 版）**：

- Q: 你说做了 dreaming，跑过几次实际生效？（陷阱题：诚实答"24h / 5 sessions 触发条件下，开发期没真触发；测试覆盖了所有路径，但生产数据缺失"）
- Q: L5 和 L6 区别？为什么需要分两层？（背：记新事 vs 整理已记的事；分两层是为了不在每次 session 都跑昂贵的 consolidation）
- Q: HTML 工件这块有什么是不能 HTML 化的？（agent 自己吃的 tool returns）
- Q: AutoCompactState 的 `consecutive_failures=3` 是为啥？（坏 LLM 反复浪费钱；CC 同款值）
- Q: 你怎么知道 9 段模板是 CC 同款？（2026-03-31 source map 泄漏后的 ground truth）

### 进阶 E · v4 末段，怎么继续（且看且做）

> **第一手素材已经在手**：5 份 ADR（`docs/decisions/0001-0005`）覆盖了项目所有重大架构决策——
> Mock 面试时不要硬背阶段题，而是按 ADR 的 Context / Decision / Consequences 顺序讲故事。详见 **进阶 F**。

**v4 末没做的事**（按 ROI 排）：

| 项 | 工程量 | 故事价值 | 建议 |
|---|---|---|---|
| MCP client minimal | 大 | 强 | 想做 demo / 上简历可加 |
| Docker 沙箱 | 中 | 中（替代 Permissions 短板） | 有时间再做 |
| 真正端到端 E2E 测试 | 中 | 中 | 推荐——补单元测试缺口 |
| iframe srcdoc 嵌入真工件预览 | 小 | 小 | 可不做 |
| LangGraph 重写 planner | 大 | 高（对照体验值） | 时间多再做 |

**比加代码更重要的非代码项**（v4 末以后真正该做的）：

- 录 5 分钟 demo 视频
- 写公众号 / 知乎 walkthrough
- README 头部加 `docs/architecture.html` 截图
- 拿这份 LEARNING_PATH 做 mock 面试，把每个"为啥这么做"练熟

每做一个，DEVLOG 顶部加一段，commit + push。**这个项目的叙事骨干是 DEVLOG，不是 README。**

> ⚠ 重要：v4 后再加技术 feature 的边际收益已接近零——讲述能力（5 分钟讲清）才是当前真正的瓶颈。
> 看到这条还想"再加一个 feature 让项目更牛"的话，停一下，去录视频。

### 进阶 F · ADR 作为讲述层引擎（30 分钟）

**目标**：把 5 份 ADR（Architecture Decision Records，`docs/decisions/`）当作讲述层第一手素材。Mock 面试时不要硬背阶段题，按 ADR 模板讲故事。

**为什么 ADR 是讲述层引擎**：

ADR 不是文档，是**项目的化石层**——每一份记录一个"为什么这样选"的决策时刻，按时间编号永不修改。
DEVLOG 是流水账（5000 行），ADR 是脉络（5 份各 100-150 行）。
面试时翻 DEVLOG 找重点 vs 直接打开 ADR 讲故事，差距巨大。

**读什么**（按讲述价值降序）：

1. **`docs/decisions/0001-agentic-search-over-rag.md`** —— v1→v2 最大架构 pivot
   - Context 段把"为什么 v1 想用 RAG"写活
   - Decision 段说清楚行业事实标准（OpenHarness / Cursor / Claude Code）
   - Consequences 列了 **5 类向量 RAG 在代码场景的失败模式**——可背
2. **`docs/decisions/0003-dreaming-4-stage-consolidation.md`** —— **诚实段的标杆**
   - 主动写"开发期没真触发 / 18 单测全 mock / 生产价值未验证"
   - 主动 disarm "你跑过几次"这道陷阱题
3. **`docs/decisions/0005-domestic-multi-model.md`** —— **诚实段标杆 2**
   - 主动写"不是合规方案，是个人开发者便利封装；境外公网调境内服务器，非私有部署"
   - 主动 disarm "如何合规"这道追问题
4. **`docs/decisions/0002-html-for-humans-not-agents.md`** —— v4 边界设计
   - "tool returns 必须保持字符串" 这条铁律的完整论证
5. **`docs/decisions/0004-memory-7-layer-architecture.md`** —— L5/L6 分离写在代码强约束里

**ADR 模板 5 个 section（必背）**：

| Section | 干啥 |
|---|---|
| Status | Proposed / Accepted / Deprecated / Superseded by NNNN |
| Context | 当时面临什么情况——不讲技术讲约束 |
| Decision | 最终选了什么 |
| Consequences | 好处 + **坏处（必须有）** + Mitigation |
| 参考 | 链接 / 相关 ADR / 源码位置 |

**"诚实坏处"是 ADR 的灵魂**——marketing 文档只列好处，ADR 必须写坏处 + mitigation。这是面试时"主动 disarm 反问"的关键技术。

**怎么用 ADR 做 mock 面试**（5 套 talking points）：

| 面试问题 | 对应 ADR | 讲法 |
|---|---|---|
| "为什么不用 RAG？" | 0001 | Context → Decision → Consequences 一气呵成 |
| "dreaming 跑过几次？" | 0003 | 直接念"诚实坏处"段；"开发期没真触发，逻辑用 mock 验证" |
| "国内多模型合规吗？" | 0005 | 念"不是合规方案，是便利封装"诚实段 |
| "HTML 工件不会污染 agent 吗？" | 0002 | 念"tool returns 永远是字符串"铁律 |
| "Memory 为什么 7 层？" | 0004 | 念 L5/L6 分离 + 复刻 Claude Code 内部 |

**关键认知（必背）**：

> "ADR 是讲述层引擎——每份是一个独立故事的脚本。面试官问 X，我打开 docs/decisions/NNNN 念。
>  特别是 0003 / 0005 我主动写了'诚实坏处'段——把面试官最可能挖的坑自己挖了埋了。
>  这是工程纪律：marketing 文档堆好处，ADR 必须有坏处 + mitigation。"

**面试题自测（ADR 版）**：

- Q: 你这个项目有架构决策记录吗？（答"5 份在 `docs/decisions/`"，比说"在 DEVLOG 里"专业 10 倍）
- Q: ADR 和普通文档区别？（ADR 不修改，反悔写新的 Superseded 旧的；编号永不重用）
- Q: 选 ADR 体系是出于什么考虑？（项目跑久了"为什么这样选"会忘——ADR 把决策时刻凝固成档案）
- Q: 给我讲个你最得意的架构决策？（直接打开 0001 念 Context 段）

**下一步可做的 ADR**（按讲述价值排，**讲述层先做完再考虑**）：

- ADR-0006 候选：edit_file 工具的"字符串替换 over diff"选择
- ADR-0007 候选：测试不依赖 pytest 用裸 Python `if __name__ == "__main__"` 跑法
- ADR-0008 候选：commit author override 而不是改全局 .gitconfig

---

## 附：遇到问题怎么办

- 卡在某个文件 → 把这个文件的 docstring 粘给 Claude Code，让它再用另一个角度给你讲一遍
- 代码跑不起来 → 先看 `.env` 是否配对，再看是不是依赖没装全（`pip install -e .`）
- 概念不懂 → 回阶段 0 补基础；去看 LangChain / LlamaIndex 的 introduction 文档对照看

祝面试顺利。学完回来再看一遍这份路径，会有新的感受。
