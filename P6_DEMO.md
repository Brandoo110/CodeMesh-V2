# CodeMesh V2 主 Demo

## Demo 要证明什么

这不是一个“AI 能不能把测试跑绿”的演示。场景中的 Builder 已经完成代码并给出绿色测试，但变更仍不具备可接管性：模型路由跨越 Provider boundary，fallback 被硬编码，副作用重试不幂等，同时缺少成本上限、fallback trace、kill switch、owner/ADR，并出现 scope creep。

CodeMesh 要证明的是：这些问题可以被绑定到精确 subject digest、Evidence、角色评审、Policy 与人工责任；修复产生新 digest 后，旧结论会失效，新变更必须重新审核和签收。

## 一键准备

Demo 完全离线，不调用模型、网络、GitHub、CI 或生产服务。命令只向本机默认 Workbench 数据库追加两个固定 Case；不会删除或覆盖已有 Case，同名但内容不同会拒绝写入。

```bash
python -m web.assurance_demo
```

也可以把数据写入隔离数据库，用于测试或检查 JSON 摘要：

```bash
python -m web.assurance_demo --db /tmp/codemesh-p6-demo.sqlite
```

随后分别启动后端和前端：

```bash
make ui-backend
make ui-frontend
```

打开 `http://localhost:3010`，进入 Assurance Workbench。

## 五分钟演示顺序

### 1. 先看旧 Case

选择 `assurance-demo-old-digest`。

1. Queue 显示最终状态 `INVALIDATED`。
2. Evidence 中可看到 Builder 绿色测试、Diff 和 Author Agent Receipt；绿色测试只证明测试运行成功，不替代治理审查。
3. Findings 展示 Intent、Architecture、Operability 三个角色发现的八类问题。
4. Execution Receipt 显示三个计划角色与实际角色，没有把一个模型调用伪装成三角色执行。
5. Policy 的原始结果是 `BLOCKED`；Adjudicator conflict 被保留在时间线。
6. Repair 产生新 digest 后，旧 Case 收到 `INVALIDATE`，旧结论不能继续签收。

### 2. 再看修复后 Case

选择 `assurance-demo-new-digest`。

1. subject digest 与旧 Case 不同，三项 Evidence 也全部重新绑定。
2. 三角色重新审核，旧 Findings 以 `resolved` 状态保留，而不是从历史中消失。
3. Policy 结果为 `NEEDS_HUMAN`，原因是高风险路由变更仍需 `release_owner` 承担责任。
4. 作者 `demo-author` 与签收人 `release-owner` 分离。
5. 人工签收后 Case 才进入 `ACCEPTED`，并可导出 Change Passport。

### 3. 最后给出产品结论

Demo 中真正的价值不是“比 Codex 更会写代码”，而是把 AI 生成变更从一次会话结果变成可交接的责任对象：

- 测试绿不等于可接管；
- Reviewer 只产生有 Evidence 的发现，不拥有最终发布权；
- Policy 负责确定性约束；
- 人工签收绑定精确 digest；
- 代码变化后旧结论自动失效；
- Multi-Agent 没有通过 P6 晋级门，因此默认路径仍是 Single Strong Reviewer + Policy Gate，Council 只用于高风险或跨角色冲突实验。

## 八类风险与责任角色

| 风险 | 主责角色 | 直接 Evidence |
|---|---|---|
| Provider boundary 越界 | Architecture | Diff |
| fallback 硬编码 | Architecture | Diff |
| 副作用重试不幂等 | Operability | Diff |
| 无成本上限 | Operability | Diff |
| 无 fallback trace | Operability | Author Agent Receipt |
| 无 kill switch | Intent | Diff |
| 无 owner/ADR | Architecture | Author Agent Receipt |
| scope creep | Intent | Diff |

## 证据边界

本 Demo 的 Evidence level 是 `deterministic_offline_demo`，`external_services=false`。它证明本地合同、状态机、SQLite projection、Workbench 展示和 Passport 链路可运行；它不证明真实模型调用质量、真实 token/cost/latency、GitHub Check、CI、部署或生产验收。Receipt 中 token 和 cost 为 0，表示离线确定性 fixture 没有发生模型计费，而不是对真实 Council 成本的测量。
