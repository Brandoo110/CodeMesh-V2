# ADR-0001：代码检索使用 agentic search 而非向量 RAG

## Status

Accepted — 2025 年 v1 → v2 转型阶段。已完整落实并写入项目根 CLAUDE.md "RAG 的真相" 一节。

## Context

v1 设计阶段，最初的代码检索方案是**向量 RAG**：

1. 把整个 codebase 分块（chunk by file / function / class）
2. 用 embedding 模型批量编码、入向量库
3. 用户/模型查询时 → similarity search → top-k chunks 注入上下文

设计时这看起来很自然——RAG 是 LLM 应用的"标准答案"。

但调研主流 Claude Code clone（重点参考 [OpenHarness](https://github.com/HKUDS/OpenHarness) 11.7k 行的港大实现）和 Anthropic 自家 Claude Code 的工具集后，发现整个赛道**不走向量 RAG**：

- Claude Code 内部用 `Grep` / `Glob` / `Read` / `Edit` 让模型自己 agentic search
- OpenHarness 同样用 ripgrep + glob + AST + read_file 组合
- Cursor / Cline / Aider 主流方案也都是 agentic search

进一步分析向量 RAG 在**代码场景**的失败模式：

| 失败模式 | 说明 |
|---------|------|
| Embedding 平均化丢结构 | 一个函数被 mean-pool 成单个向量，调用关系、变量命名、类型签名全丢 |
| 相似 ≠ 相关 | 两个函数 embedding 接近不代表它们是用户想要的 |
| 调用图不在向量空间 | 找 "X 在哪被调用" 这种需求，向量没法表达 |
| 索引滞后 | 文件改了向量没更新就给错的结果，但代码场景每分钟都在改 |
| 解释链断裂 | 模型拿到 top-k chunks 也不知道为什么是这几个，没法基于结果继续追查 |

## Decision

代码检索**全走 agentic search**：

- `grep_text`（ripgrep 后端）—— 关键词 / 正则
- `glob_files`（rg --files 后端）—— 文件名模式
- `lsp_code`（AST-based, services/lsp/）—— 符号 / 调用图 / 跳转
- `read_file` —— 局部展开

让模型自己导航——它先 grep 关键词、看 hits、再 read_file 局部展开。每一步都有 reasoning 链。

向量 RAG 模块**保留**（`rag/` 目录），但**只用于非代码场景**：

- 用户文档库
- 知识库
- 长文本检索（论文、文档、报告）

## Consequences

### 好处

1. **解释链完整**：每次工具调用都有理由，不是黑盒返回 top-k
2. **零索引构建成本**：不用维护向量库，不用 embedding 服务
3. **不怕代码改动**：grep 永远查最新文件
4. **工具复用**：同一套 grep / glob / read 也能查日志、配置、文档
5. **接近行业事实标准**：和 Claude Code、Cursor、OpenHarness 一致，面试好讲

### 坏处

1. **模型有"找不准"风险**——需要好的 tool description + 多轮搜索能力
2. **ToolUse 计数高于 RAG 一次注入**——成本结构不同
3. **大型 codebase 可能 grep 慢**——但 ripgrep 已经够快（10k 文件级别 ms）

### Mitigation

- 在 tool description 里强调"先 grep 后 read"启发式
- 在 system prompt 里给检索策略提示
- LSP 工具补充 AST 级精准跳转
- 保留 `rag/` 模块作 escape hatch（万一需要混合策略时不用从零写）

## 参考

- [OpenHarness](https://github.com/HKUDS/OpenHarness) — 港大 Claude Code 克隆，本项目工具组合的直接参考
- Claude Code 公开工具集：Grep / Glob / Read / Edit
- Anthropic Engineering 博客（2024 11 月）—— "How Claude Code agentically explores codebases"

## 相关

- 项目根 CLAUDE.md "RAG 的真相" 一节
- `execution/tools.py` — 工具注册
- `execution/lsp.py` — AST-based 检索
- `rag/` — 保留作非代码场景 RAG
