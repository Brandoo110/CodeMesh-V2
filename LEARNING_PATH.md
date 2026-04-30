# CodeMesh 学习路径

> 把你从"听说过 Agent"带到"在面试中能讲清 Harness 架构"的循序渐进指南。
> 每个阶段给你：**要读的文件**、**要搞懂的概念**、**能回答的面试题**。

预计总学习时长：**8–12 小时**。按阶段来，不要跳。

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
- Q: Hybrid search 是什么？怎么做？（向量 + BM25 融合，RRF 加权）
- Q: 如果代码库改了怎么更新索引？（增量 vs 全量重建的取舍）

---

## 阶段 10 · 延伸与扩展（可选）

学到这里你已经能应对绝大多数 Agent 面试。想更深可以：

1. **加一个工具**：`grep_file(pattern, path)` —— 只改 `tools.py`
2. **加一个模型**：接入智谱 GLM 或月之暗面 Kimi
3. **换成 LangGraph 重写 Planner**，亲身感受状态图的威力
4. **实现 `stats` 命令**：查询 Langfuse API 统计今日 token 消耗
5. **容器化沙箱**：把 `bash_exec` 放到 Docker 里跑
6. **记忆压缩**：`short_term.py` 快满时让 DeepSeek 先总结再继续
7. **Hybrid RAG**：在 retriever 里加 BM25 过滤再用 RRF 融合
8. **AST 切 chunk**：用 tree-sitter 替换按行切

---

## 学完你会什么

✅ 能在白板上画出 Harness 四层架构图
✅ 能讲清 PydanticAI vs LangGraph 的选型 tradeoff
✅ 能描述 Agent Loop 的每一步 + 防死循环方案
✅ 能讲清适配器模式在多模型调度中的应用
✅ 能讲清为什么 LLM 可观测性是一等公民
✅ 理解国内模型生态：DeepSeek / Qwen / Doubao 各自的定位
✅ 面试时能信手拿代码举例，而不是空谈概念

---

## 附：遇到问题怎么办

- 卡在某个文件 → 把这个文件的 docstring 粘给 Claude Code，让它再用另一个角度给你讲一遍
- 代码跑不起来 → 先看 `.env` 是否配对，再看是不是依赖没装全（`pip install -e .`）
- 概念不懂 → 回阶段 0 补基础；去看 LangChain / LlamaIndex 的 introduction 文档对照看

祝面试顺利。学完回来再看一遍这份路径，会有新的感受。
