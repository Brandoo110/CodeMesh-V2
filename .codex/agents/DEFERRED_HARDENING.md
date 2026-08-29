# 延后加固清单

项目当前处于“本地产品收口 / 可用 MVP 集成”，当前主线已从 C3 Workbench Intake + Artifact Reader 转入 C4 自动 freshness + server-side stale/invalidation fence；以下事项暂不抢占产品闭环：

- 全面安全扫描与专项 AppSec 审计。
- 压力、性能、长稳和大规模并发测试。
- 生产多租户、完整 RBAC、灾备与运营级发布加固。

延后不适用于当前原子所必需的秘密保护、路径边界、startup fail-closed、focused negative tests、最小授权校验和可回滚性。阶段成熟度达到真实用户/流量、核心 API/算法稳定并具备部署与回滚证据后，应重新评估并提醒“项目应该进入下一阶段，应该更新 agents了”。

阶段成熟度提醒：项目应该进入下一阶段，应该更新 agents了。

本轮阶段判断：C3 已完成，但证据仍限本地 focused/static/fake；项目未达到真实用户/流量、部署回滚或生产成熟度，不切换阶段、不把长期目标写成 MVP 完成。

本轮新增后续项（C3 产品闭环，需在真实 dogfood 前补）：composition 读取 service 私有 artifact store seam 后续显式化；stale poll error/loading 状态；command IDs 约束；非 Git Artifact route 更广矩阵（依赖既有 Reader 测试）。这些不是 C3 里程碑阻塞。

可延后专项：完整 tsc/eslint、browser/service smoke 之外的全面安全扫描、压力/性能/长稳、大规模并发，以及生产多租户/RBAC/灾备/运营级发布加固。完整 tsc/eslint 与 browser/service smoke 属产品闭环证据，真实 dogfood 前应补；全面安全与性能专项继续按阶段成熟度延后。

本轮阶段判断（2026-08-29）：项目仍处于“本地产品收口 / 可用 MVP 集成”。本轮完成 bounded remediation atomic transaction、preflight/API、scoped agent loop 与 configured remediation runtime；已有 focused/fake 与 independent review 证据，但未取得真实 provider、网络或 dogfood 证据，且未 push。GitNexus 索引 stale 且绑定旧仓；不切换阶段，不把本轮结果表述为线上或生产就绪。

主线阻塞/未完成边界：真实 provider dogfood 与远端 push 仍未完成，属于主线交付边界，不归类为可延后 hardening。

本轮新增后续项：无新增。现有全面安全扫描、压力/性能/长稳、大规模并发及生产多租户/RBAC/灾备/运营级发布加固继续按阶段成熟度延后。

本轮专项记录（2026-08-29）：无新增延后专项。当前仍处于“本地产品收口 / 可用 MVP 集成”；本轮文档改动仅修复 Real local Change Acceptance quickstart 的 intake contract，补充有效 task spec 示例并让 README 的 `task_path` 指向该示例。后续可由真实 dogfood 验证其端到端可用性；本轮未进行、也不提前宣称 provider 调用或 dogfood 成功。

本轮专项记录（2026-08-29，DeepSeek V4 model migration）：真实 run `run_b0a090...` 已证明 4 evidence success、FRESH、Passport 可读，但 reviewer 因 legacy model provider failure BLOCKED；keyed GET `/v1/models` 返回 200 只证明 key/network/鉴权，不证明 reviewer success。本轮迁移 reviewer 示例至 `deepseek-v4-flash`，迁移后仍待一次新 run，未完成 dogfood。无新增 hardening；V1 planner/adapter/pricing 作为单独后续范围处理。

本轮专项记录（2026-08-29，DeepSeek V4 bounded non-thinking）：真实 run `run_9afdd...` 已实际使用 `deepseek-v4-flash`，FRESH 且 4 evidence success；但默认 high thinking 在 4096 output budget 达到 `finish_reason=length`，结果为 BLOCKED。本修复后待新 run，dogfood 尚未完成；无新增 hardening。

本轮专项记录（2026-08-29，DeepSeek V4 JSON mode）：真实 run `run_af01...` 已实际使用 DeepSeek V4/non-thinking，FRESH 且 4 evidence success，但状态为 `invalid_json`。Raw artifact 为 4736 bytes，包含单一 ``` fence；去 fence 后是合法 dict，keys 为 `findings`、`questions`、`rubric_hash`、`schema_version`、`subject_digest`，但严格 parser 不应剥 fence。本修复后待新 run，dogfood 尚未完成；无新增 hardening。
