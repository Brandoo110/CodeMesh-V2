# P7-03 Assurance 生命周期边界合同

本文是 P7-03 的边界合同，范围只包括 assurance lifecycle 的 release observation
模型、本地生命周期存储、Web repository 和
`web.routes.assurance_lifecycle.router`。它不把验收结果、声明式观测或本地
持久化扩展成生产操作能力。

## A. 不自动部署或回滚

- 创建、评审、接受或持久化一个 assurance case 都不会触发部署、回滚、监控
  查询或云端写入。
- `RollbackRecord` 只是调用方在 observation 中提供的事实声明；它描述
  `not_executed`、`executed` 或 `unknown`，不是回滚命令，也不执行回滚。
- P7-03 的 observation importer 只校验调用方给出的 JSON，并把原始 import
  bytes 保存到本地 content-addressed `ArtifactStore`。它没有监控、云、网络、
  subprocess 或 deployment client。

## B. pre-merge PASS / ACCEPTED 不等于 production success

`PASS`、`ACCEPTED` 和 assurance passport 表示变更验收合同满足了当前 case 的
证据、策略和人工决策要求。它们不是生产发布成功、真实流量健康或生产回滚
完成的证明。

接受一个 case 不会隐式创建 `ReleaseObservation`，也不会填充任何生产结果。
对一个刚变为 `ACCEPTED` 的 case，
`list_release_observations(case_id)` 的初始结果必须仍为空；后续 observation
只能由明确的 manual/import 写入产生。

## C. 没有真实监控时，observation 只允许 manual/import 且 trust=declared

`ReleaseObservation` 的输入类型是封闭的：

- `source` 只能是 `manual` 或 `import`；
- `trust_level` 只能是 `declared`。

因此，调用方提供的生产窗口、SLO、告警和回滚字段仍是声明事实。没有真实监控
或可信 ingress 时，系统不会把它们升级为 `observed`、`deterministic` 或其它
更高信任等级。import 还必须绑定 case subject digest，并保留可重放的原始
payload；这不改变其 `declared` trust。

## D. 生产操作需要独立授权；当前 API 没有生产操作写入口

部署、回滚和其它 production operation 必须由独立的生产操作授权、执行系统
和运行态证据负责；P7-03 不代替这些授权或执行系统。

当前 `assurance_lifecycle.router` 公开的路径只有：

| Method | Path | 语义 |
| --- | --- | --- |
| `POST` | `/assurance/changes/{case_id}/release-observations/manual` | 写入调用方声明的 manual observation |
| `POST` | `/assurance/changes/{case_id}/release-observations/import` | 写入经校验的 import payload |
| `GET` | `/assurance/changes/{case_id}/release-observations` | 读取 observation |
| `GET` | `/assurance/changes/{case_id}/remediations` | 读取 remediation lineage |

当前 API 没有 `deploy`、`rollback` 或 `production-operation` 的写路由。上述
两个 `POST` 只写入声明式 observation，不是生产操作授权，也不是部署/回滚
执行入口。

## 证据边界

本合同和 focused architecture tests 证明的是代码、模型、路由和本地 SQLite/
artifact seam 的边界。它们不证明真实监控接入、真实部署、真实回滚、生产成功、
现场设备状态或云端运行结果。
