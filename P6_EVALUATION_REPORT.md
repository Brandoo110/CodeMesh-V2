# CodeMesh V2 Phase 6 评测与决策报告

## 结论

截至 2026-08-25，CodeMesh V2 已完成 `change_assurance_v0` 的 Rules Only、Single Strong Reviewer、Specialized Council 三臂正式运行、隐藏金标准评分、Multi-Agent 晋级门和离线主 Demo。

本轮结论是 **Multi-Agent 不进入默认路径**：Single 与 Council 的 precision / macro recall 都是 `1.0 / 1.0`，Council 没有新增 recall 或 unsupported-claim 收益，却比 Single 多一次 false block；真实模型 token、cost 和 latency 不可得，成本门必须 fail closed。因此默认 topology 保持 `single_strong_reviewer+policy_gate`，Council 只允许在 high-risk 或 cross-role conflict 场景实验使用，并继续经过 Policy Gate 和 Human Review。

P6-06 人工时间实验材料已经冻结，但 `results.csv` 仍只有表头。当前不能声称 CodeMesh 降低了人工理解时间或提高了主观信任，Phase 6 也尚未最终关闭。

## 可审计交付物

| 原子 | 交付物 | 证据 |
|---|---|---|
| P6-01 | 12 个公开测试通过但存在接管/治理缺陷的固定 case | commit `de139fe` |
| P6-02 | Rules / Single / Council 严格 adapter 与 12×3 正式结果 | `change_assurance_v0_luna.json`；commit `6f082d1` |
| P6-03 | hidden-gold evaluator 与 metrics-only score | `change_assurance_v0_luna_scores.json`；commit `525c29f` |
| P6-04 | 固定阈值、可重算的晋级决定 | `change_assurance_v0_luna_promotion.json`；commit `8a8da80` |
| P6-05 | 离线双 Case 主 Demo、五分钟走查 | `P6_DEMO.md`；commit `954e3f0` |
| P6-06A | Diff / Single / Acceptance Case 三种同事实人工实验材料 | `experiments/p6_human_time/`；commit `9d57fa5` |
| P6-06B-E | 三条真实人工记录与探索性总结 | **等待参与者** |

正式 result artifact 保存 12 个 case × 3 个 arm 和 48 条角色原始响应。模型角色是 General、Intent、Architecture、Operability，`model_ref=gpt-5.6-luna`，provider 为 `openai-codex-desktop`。Reviewer 只读取 public payload；hidden gold 只在 scorer 侧加载。

## 三臂结果

| 指标 | Rules Only | Single Strong Reviewer | Specialized Council |
|---|---:|---:|---:|
| Case 数 | 12 | 12 | 12 |
| Precision | 1.00 | 1.00 | 1.00 |
| Macro recall | 0.50 | 1.00 | 1.00 |
| Finding 数 | 6 | 12 | 12 |
| Question 数 | 6 | 5 | 10 |
| False block | 1/3 | 0/3 | 1/3 |
| Missed block | 4/8 | 1/8 | 1/8 |
| Evidence ref 有效 | 6/6 | 39/39 | 35/35 |
| Blocking Evidence location | 4/5 | 12/12 | 12/12 |
| Stale escape | 0/1 | 0/1 | 0/1 |
| Required role execution | 12/12 | 12/12 | 12/12 |
| Conflict retention | N/A | N/A | 12/12 |
| 模型 token / cost / latency | 不适用 / 0 / 未记录 | unavailable | unavailable |

Outcome 分布：

- Rules：5 `BLOCKED`、6 `NEEDS_HUMAN`、1 `STALE`；
- Single：7 `BLOCKED`、4 `NEEDS_HUMAN`、1 `STALE`；
- Council：8 `BLOCKED`、3 `NEEDS_HUMAN`、1 `STALE`。

这些数字来自固定数据集上的本地评测，不代表真实团队、生产变更或外部 benchmark。

## Multi-Agent 晋级门

预先固定的收益条件要求至少满足一项：macro recall 相对 Single 提升 10 个百分点、unsupported rate 下降 25%，或人工审查时间下降 25%。同时要求 false-block 增量不超过 3 个百分点、stale escape 为 0、阻塞 Finding 的 Evidence location 为 100%，并满足 2.5× 成本上限或只限 high-risk。

| Check | 结果 | 当前事实 |
|---|---|---|
| Benefit | FAIL | recall gain=0；unsupported 两边均为 0；human time unavailable |
| False block | FAIL | Council - Single = 1/3，超过 0.03 |
| Cost | UNAVAILABLE_FAIL_CLOSED | Single/Council cost 均不可得 |
| Stale escape | PASS | 0 |
| Blocking Evidence | PASS | 1.0 |
| Required roles | PASS | Single/Council 均 1.0 |
| Conflict retention | PASS | Council 1.0 |

最终决定：

```text
NOT_PROMOTED
default = single_strong_reviewer + policy_gate
council = experimental_high_risk_or_conflict_only
requires = policy_gate + human_review
can_override_stale_or_evidence_gate = false
```

该决定由 check status 动态派生，不是永久硬编码。未来只有在新鲜结果使全部门槛通过后才能重新晋级。

## 主 Demo

主场景是一项“测试全绿”的模型路由与 fallback 变更，但仍存在 Provider boundary 越界、fallback 硬编码、副作用重试不幂等、无 cost cap、无 fallback trace、无 kill switch、无 owner/ADR 和 scope creep。

离线 seed 生成两条 Case：

1. 旧 digest Case 保留三类 Evidence、三角色 Receipt、8 个 Finding、`BLOCKED` Policy 和 Adjudicator conflict；repair 后进入 `INVALIDATED`。
2. 新 digest Case 重新绑定 Evidence 并重审，Policy 为 `NEEDS_HUMAN`；非作者 release owner 签收后才进入 `ACCEPTED`，Passport 可导出。

Demo 的 `evidence_level=deterministic_offline_demo`、`external_services=false`。它不依赖真实模型、网络、GitHub、CI 或生产服务。

## P6-06 人工时间实验

三种条件使用字节完全一致的 task brief、Diff 和 green-test packet：

- A：Diff Only；
- B：Diff + flat Single Reviewer；
- C：Structured Acceptance Case。

记录字段包括理解耗时、8 题正确率、原始 Evidence 打开次数、trust 1-5 和遗漏条件。推荐 3 位未接触本项目的参与者各只看一个条件；同一人重复会产生学习效应。`n=3` 只允许描述观察，不做显著性、因果或泛化结论。

当前状态：**材料完成、人工观测为 0**。Agent dry-run、自动答案或研究者代答不计入 `human_reported`。

## 简历与面试陈述边界

当前可以陈述：

- 设计并实现本地优先的 AI 代码变更接管与验收工作台，用 subject digest 将 Evidence、角色评审、Policy、人工签收和失效重验绑定为 Acceptance Case；
- 构建 12-case、三臂、public-input / hidden-gold 隔离的 Multi-Agent 评测，并用预设门槛否定一次不具备增益的 Council 默认晋级；
- 实现按角色的模型路由、fallback 事实、Execution Receipt、冲突保留与 fail-closed Gate；
- 将“测试全绿但不可接管”的治理风险做成完全离线、可重放的主 Demo。

当前不能陈述：

- Council 比 Single 更准确、更快或更省钱；
- CodeMesh 已降低人工审查时间或提高信任；
- 真实 token/cost/latency 已测量；
- 已完成 GitHub/CI、真实团队、部署、生产或签名 attestation 验收。

## Phase 6 收口状态

- [x] 12-case 本地集；
- [x] 三组结果可离线重放；
- [x] Multi 按既定阈值被否定默认晋级；
- [x] 主 Demo 无隐藏真实服务依赖；
- [x] 指标、决策和证据边界已写入报告与 canonical devlog；
- [x] 简历陈述限制已固定；
- [ ] 三条真实 `human_reported` 走查完成；
- [ ] P6-06 与 Phase 6 Gate 最终关闭。
