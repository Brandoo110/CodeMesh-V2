<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **CodeMesh** (11444 symbols, 26976 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/CodeMesh/context` | Codebase overview, check index freshness |
| `gitnexus://repo/CodeMesh/clusters` | All functional areas |
| `gitnexus://repo/CodeMesh/processes` | All execution flows |
| `gitnexus://repo/CodeMesh/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

# CodeMesh V2 项目治理

## 当前阶段与职责

- 当前阶段：本地产品收口 / 可用 MVP 集成。目标是把核心工作流稳定收口，形成可复现、可回滚、可核验的本地交付。
- 核心职责：维护 V2 的 Assurance runtime、产品切片、最小必要的授权与路径边界，以及与实现同步的 canonical 记录。
- 当前优先级：围绕 canonical 中的 GP-05C2 推进端到端 MVP；先保证正确、安全、可验证，再做局部体验和工程整理。
- 非目标：不把 V2 当作线上内测或生产准备项目；不扩大到生产多租户/RBAC/灾备、全面性能压测或长期稳定性专项；不修改 V1 `/CodeMesh`。

## 工程、验证与授权边界

- 业务代码改动必须有明确的 allowlist、单一 writer 和 focused 验证；不得借治理任务顺带修改业务路径。
- 必须保留秘密保护、路径边界、startup fail-closed、focused negative tests 与可回滚性；这些不是延后事项。
- 静态检查、focused test、build、真实 API/服务、真机/现场和生产证据分层报告；前一层通过不等于后一层完成。
- 不安装依赖，不访问真实生产系统，不发布、不部署、不发送外部写入；涉及这些动作须取得当次明确授权。
- 修改前保留工作区已有改动；不得使用 reset、checkout、clean 或 stash 清理现场。提交前必须核对范围并运行 GitNexus `detect_changes()`。

## 持久 Agent 团队

- 默认按 `.codex/agents/README.md` 启用最小充分团队，并发上限为 3。总控负责冻结原子、调度、验收和最终风险裁决。
- `assurance_runtime_writer`：GPT-5.6 Luna Max，workspace-write，只写 runtime allowlist。
- `product_slice_writer`：GPT-5.6 Luna High，workspace-write，只写产品切片 allowlist。
- `risk_reviewer`：GPT-5.6 Luna Max，read-only，只读审查，不编辑、不测试、不提交、不启动服务。
- `canonical_recorder`：GPT-5.6 Luna Medium，workspace-write，仅写 canonical 文档与治理记录，不碰业务代码。
- DeepSeek 不作为默认或候选模型；只有用户在当次任务中明确指定时，才可另行启用独立 worker。

## 记录与阶段成熟度

- 产品事实以 `/Users/junjieli/obsidian/Brain/Projects/active/CodeMesh/index.md` 与 `V2/V2-执行规划.md` 为 canonical；代码、测试/构建、运行态和 Git 分别作为独立证据层。
- 形成持续进展、重要决策或交付物变化时，由 `canonical_recorder` 按 Brain 规则记录；普通排查不写日志。
- 当核心 API/算法稳定、端到端 MVP 有真实用户/流量证据，且具备授权、回滚与部署证据时，项目应进入下一阶段，应该更新 agents了。未经用户确认，不自动切换阶段或扩大优先级。
- 阶段成熟度提醒：项目应该进入下一阶段，应该更新 agents了。
