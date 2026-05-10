# ADR-0002：HTML 给人看，不给 agent 吃

## Status

Accepted — 2026-05-10（v4 HTML 工件批的核心边界设计）。

## Context

2026-05 看到 Thariq Shihipar 的文章 ["HTML 输出的不合理有效性"](https://thariq.io)，文中六个用例展示 HTML 作为 LLM 输出格式的优势：

- 视觉信息密度高（一张图胜过 markdown 表）
- 交互性（hover / 折叠 / 链接）
- 可定制（CSS / SVG）
- 自包含（一个 HTML 文件即一份完整产物）

读完想给 CodeMesh 也加 HTML 工件——dashboard、diff、timeline、architecture。

但**直接全面 HTML 化会出问题**：

如果 agent 的 tool returns 也变成 HTML，会发生：

| 问题 | 后果 |
|------|------|
| Token 浪费 | HTML 标签 + CSS + SVG 把 agent context 撑爆 |
| 模型语义被污染 | 模型本来读 `OK: edited file at line 42`，现在读到一坨 `<div class="diff-add">` |
| 工具链耦合 UI | 工具应该返回事实，不应该返回展示 |
| 测试失控 | 工具单测断言 HTML 结构会脆弱 |

Thariq 文章本身关注的是 **"模型给人最终输出"** 的形态，不是工具内部通信。

## Decision

**严格分两类输出，互不串味**：

| 输出类型 | 受众 | 格式 | 例子 |
|---------|------|------|------|
| Tool returns（agent 内部） | 模型自己读 | **纯字符串** | `"OK: wrote 32 lines to foo.py"` |
| Final artifacts（最终产物） | 用户看 | **HTML（自包含）** | `.codemesh/reports/stats-<ts>.html` |

**实现层面**：

1. `feedback/render_html.py`：HTML 渲染基建（HtmlDoc + 暗色 CSS + 手写 SVG 原语 + 文件滚动），**只在 final artifact 路径调用**
2. `execution/tools.py` 里的 `edit_file()`：环境变量 `CODEMESH_HTML_DIFF=1` 控制是否额外**落盘** HTML diff 文件，**返回值仍然是字符串**（追加一行 `[diff html: <path>]` 提示用户）
3. `harness.py` `_run_planned()`：环境变量 `CODEMESH_HTML_PLAN=1` 控制是否落盘 planner timeline，同样**不影响 step 间通信**
4. `cli.py stats --html`：dashboard 直接给用户跑的命令，HTML 是终态产物
5. `docs/architecture.html` / `docs/index.html`：直接面向访问者的静态产物，零 agent 介入

**铁律**：HTML 永远是**额外的产物**（落盘到 `.codemesh/reports/` 或 `docs/`），永远不进 agent 的 context window。

## Consequences

### 好处

1. **Token 不浪费**：agent context 永远只装事实文本
2. **工具语义干净**：tool 返回 `"OK: ..."` / `"[ERROR] ..."` 模型熟悉的格式
3. **HTML 可以肆意做厚**：因为不影响 agent，可以加交互、加 SVG、加 CSS 动画
4. **测试可分层**：工具测试只断言字符串；HTML 测试只断言渲染
5. **可选启用**：env var 控制开关，CI / 测试场景默认关闭，演示场景再开

### 坏处

1. **双轨设计** —— 同一份信息可能存两次（diff 字符串 + diff HTML 文件），冗余
2. **磁盘开销** —— HTML 文件累积，需要 rotate（已实现 `rotate_dir`，默认 keep=20）
3. **环境变量分散** —— `CODEMESH_HTML_DIFF` / `CODEMESH_HTML_PLAN` 等多个开关需要文档化

### Mitigation

- `feedback/render_html.py` 里 `write_artifact` + `rotate_dir` 自动管理磁盘
- `CLAUDE.md` 和 `README.md` 集中文档化 env var
- 测试覆盖率 100%（19 + 12 + 16 + 13 = 60 测试针对 HTML 工件）

## 参考

- Thariq Shihipar "HTML 输出的不合理有效性"（原文已 ingest 到 Wiki/Claude Code/）
- `feedback/render_html.py` 实现
- `docs/index.html` 中 6 条设计原则的第 1 条 "HTML for humans, not for agents"

## 相关 ADR

- ADR-0003（Dreaming）—— 同样遵循"对内字符串、对外可视化"原则
