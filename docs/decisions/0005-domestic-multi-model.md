# ADR-0005：国内多模型差异化策略

## Status

Accepted — v1 起持续至今。是项目最初的差异化定位。

## Context

Claude Code 默认绑定 Anthropic API。这个组合对**国内开发者**有 3 个硬阻塞：

| 阻塞 | 说明 |
|------|------|
| 合规 | Anthropic API 在中国大陆未提供官方服务 |
| 网络 | 直连 api.anthropic.com 不稳定 |
| 成本 | Anthropic API 单价高于国内主流模型 5-10× |

但 Claude Code 这种 agentic harness 的能力很有用——多步规划 / 工具调用 / 长上下文。CodeMesh 就是想给国内开发者一套**等价能力但绑国内模型**的方案。

直接选项有：

1. **DeepSeek only**：单模型，成本最低，但锁死风险
2. **OpenAI 兼容协议任意切换**：通用，但失去针对性优化
3. **多模型 + 主备策略**：选一个主路 + 几个备份，按场景切换

选项 3 更符合"差异化产品"定位——可以打"在国内多模型间智能切换"这张牌。

## Decision

**主备多模型策略**，按角色和场景分配：

| 角色 | 主路 | 备路 | 选择理由 |
|------|------|------|---------|
| 主对话 / planner | **DeepSeek V3** | Qwen 2.5 / Doubao | 成本最低、长上下文好、reasoning 强 |
| 代码生成 | **DeepSeek V3** | Qwen 2.5 Coder | 同上 |
| 视觉 / 多模态 | **Doubao 1.5 Vision** | Gemini 2.0 Flash | 中文 OCR 强、字节官方稳 |
| 海外 fallback | **Gemini 2.0 Flash** | — | 当国内全挂时的最后兜底 |

**实现层面**：

- Adapter 层（`adapters/`）：每个模型一个 adapter，统一 OpenAI 兼容接口
- Router（`models/router.py`）：根据 task type / context length / 成本预算选 adapter
- Fallback chain：主路失败 → 备路 → Gemini → 抛错
- Cost tracking：每次调用记成本到 `.codemesh/calls.jsonl`，`stats` 命令可视化（见 ADR-0002）

## Consequences

### 好处

1. **真实国内可用**：不用代理 / 不需要 Anthropic key
2. **成本对比可讲**：4 模型实际跑同一任务的成本/延迟差异是讲述层素材
3. **健壮性**：一家挂了不影响其他
4. **贴合岗位需求**：国内 AI 应用开发岗 90% 是基于国内模型，对齐市场

### 坏处（要诚实）

1. **合规边界要诚实** —— DeepSeek / Qwen / Doubao 是**境外公网调用境内服务器**，不是真正的私有部署。被懂行的人追问"如何处理数据出境"会卡。**不能装作"国内合规方案"**——本质是"个人开发者用境内模型 API 的便利封装"
2. **能力天花板** —— 目前国内主流模型在 agent 能力（多步工具调用、长上下文 tool use 稳定性）整体仍不及 Claude Sonnet / Opus。要诚实承认部分场景 fallback Gemini 才稳
3. **API 不一致** —— 各家 OpenAI 兼容程度不同，特别是 tool use schema 偶尔有差异，需要 adapter 层吃下来
4. **维护成本** —— 每家模型升级时要测全套 adapter

### Mitigation

- README 在 v4 状态段明确写"国内 API 调用便利封装"而非"合规方案"
- 各 adapter 有独立单测覆盖兼容性差异
- Router 决策逻辑写在 module 头部教学注释里
- 长期方案：观察国内模型 agent 能力进展，DeepSeek v4+ 可能进一步收敛

## 参考

- DeepSeek API：https://platform.deepseek.com
- Qwen API：https://dashscope.aliyun.com
- Doubao API：https://www.volcengine.com/product/doubao
- Gemini API：https://ai.google.dev
- `adapters/` 目录实现
- `models/router.py` 路由决策

## 相关 ADR

- ADR-0001（agentic search）—— 工具组合不依赖特定模型，适合多模型切换
- ADR-0002（HTML 给人看）—— stats dashboard 可视化多模型成本对比
