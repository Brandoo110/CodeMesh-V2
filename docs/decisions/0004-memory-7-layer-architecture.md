# ADR-0004：Memory 7 层架构

## Status

Accepted — 2026-05（v4 memory 模块完工）。源自 Claude Code 内部源码分析后的复刻。

## Context

v1 / v2 阶段，CodeMesh 的 memory 是**朴素两层**：

- 当前 session 的 message 历史（in-memory list）
- 跨 session 的 SQLite 持久化（用户偏好、long-term facts）

这个模型够用但有几个问题：

| 问题 | 原因 |
|------|------|
| 当前 session 内部信息混乱 | 系统提示 / 用户消息 / tool returns / 临时 scratchpad 都混在同一个 list |
| 项目特定知识没法隔离 | 切换项目时上下文污染（在 A 项目记的事进了 B 项目） |
| 缺"已抽象的长期智慧" | facts 永远是 raw text，没有抽象层 |
| 跨 session 的"工作记忆缓存"没地方放 | 多次提到的概念应该被 cache，但只有 session-内 / 永久 两档 |

调研 Claude Code 内部 memory 实现（leaked 源码 + cli.js 反编译），发现 Anthropic 用的是 **7 层**架构，不是简单 short/long-term。每层有明确语义和生命周期。

## Decision

复刻 Claude Code 的 7 层 memory，每层**独立模块 + 明确读写规则**：

| 层 | 名字 | 生命周期 | 干啥 | 实现 |
|---|------|---------|------|------|
| **L0** | Immediate | 当前 turn | 当前 turn 的 message + tool calls | `harness.py` 内 |
| **L1** | Short-term Working | 当前 session | session 全程对话历史 | `memory/working.py` |
| **L2** | Thread Cache | 7 天 | 跨 session 但短期的工作集（最近聊过的话题、人名、项目） | `memory/thread_cache.py` |
| **L3** | User Preferences | 永久 | 用户偏好（语言 / 风格 / 工具偏好） | `memory/user_prefs.py` |
| **L4** | Project Context | 永久（按 project） | 项目级别的 README、CLAUDE.md、约定 | `memory/project_context.py` |
| **L5** | Facts (auto-extract) | 永久 | 从 session 自动抽取的 facts（"今天用户提到 X 项目用 React"） | `feedback/auto_extract.py` |
| **L6** | Dreams (consolidated) | 永久 | dreaming 巩固后的抽象记忆（见 ADR-0003） | `feedback/dreamer.py` |

**关键边界**：

- **L0–L1**：内存，不持久化
- **L2–L3**：跨 session 持久化（SQLite），用户级
- **L4**：跨 session 持久化，**项目级**（用 cwd 做 key）
- **L5**：**记新事**——抓取 raw facts，不做抽象
- **L6**：**整理已记的事**——L5 通过 dreaming 流水线产出

**最容易混的两层**：

L5 vs L6 这条线**不能混**：

- L5 写：`SessionJournal.append_fact("user uses React for project X")`
- L6 写：**只能由 dreamer 写**，把多条 L5 fact 合并抽象成 dream

如果用户代码不小心绕过 dreamer 直接写 L6，L6 会失去"抽象"语义，变成另一个 raw facts 仓库。

## Consequences

### 好处

1. **每层职责清晰**：debug 时知道去哪查
2. **生命周期可控**：L1 不持久化、L2 自动过期、L4 按项目隔离
3. **路线对齐 Anthropic**：和 Claude Code 内部一致，面试讲清楚有竞争力
4. **L5/L6 分离支持 dreaming**：见 ADR-0003

### 坏处

1. **复杂度高**：7 层 vs 2 层，新人理解曲线陡
2. **代码量增加**：每层一个模块 ~50-150 行
3. **测试矩阵大**：每层独立测 + 跨层交互测

### Mitigation

- `Wiki/Claude Code/Claude-Code-7层记忆-学习笔记.md` 详细解释每层（教学文档）
- 每个 module 顶部写"教学语气"注释（这一层的语义、生命周期、读写谁）
- L5 / L6 分离用代码强约束（`Layer6` API 只有 dreamer 能调）

## 参考

- Claude Code 内部 memory 源码（leaked + cli.js 反编译）
- `Wiki/Claude Code/Claude-Code-7层记忆-学习笔记.md`
- `memory/` 目录下各层实现
- `feedback/auto_extract.py`（L5 写入）
- `feedback/dreamer.py`（L6 写入，见 ADR-0003）

## 相关 ADR

- ADR-0003（Dreaming）—— L5 → L6 数据流的下半场
- ADR-0001（agentic search）—— memory 层和检索层的语义不同（memory 存事实，检索拿代码）
