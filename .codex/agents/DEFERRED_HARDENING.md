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

本轮专项记录（2026-08-29，真实 reviewer dogfood operability closeout）：在 commit `6fc42037d75600907eef5ed0820ca3a5309c81b2`，真实本地 run `run_d73c...` / case `case_4665...` 使用 DeepSeek V4 Flash，取得 4 evidence success、`FRESH`、reviewer success、schema valid、receipt success，记录 6 findings、2 questions、0 blocking findings；Gate 仍为 `NEEDS_EVIDENCE/NEEDS_HUMAN`。这证明当前 reviewer 成功边界，不是生产证据或最终人批准。

本轮待回答的两个 operability questions 是：如何保证每次 run 不因 reviewer 重试而产生额外 provider 请求，以及服务停止/回滚时有哪些真实控制面。当前边界为 `max_retries=0`、每次 run 一次 provider request，Case `Idempotency-Key` 负责 replay/conflict，失败 fail-closed；没有独立 runtime kill-switch，安全停止/回滚依靠停止本地服务或回滚 provider option。无新增 hardening。

后续 operability gap：建立 dedicated provider status taxonomy，并补充 usage/cost capture。两项均未完成，不阻塞当前已记录的真实 reviewer success 事实。

本轮专项记录（2026-08-30，P-B 真实入口与自动化）：新增 loopback CLI、GitHub Check transport/readback 与同一合同的 CI workflow；focused tests 使用 MockTransport 仅证明实现边界，不替代真实 provider/GitHub 写入证据。真实 GitHub 写入因本轮未获 exact authorization 未执行；全面安全扫描、压力/性能/长稳及生产加固仍按当前阶段延后。

本轮授权纠偏（2026-08-30，P-B）：当前冻结任务已明确授权本仓库、分支、PR 与 Workflow/Check 的真实 dogfood 外部写入；上述“未获 exact authorization”仅适用于此前运行窗口。新的候选仍须以 fresh PR、Workflow、Case/Passport、Check 与 artifact lineage 权威读回为准，未完成前不宣称 dogfood 或生产就绪；全面安全扫描、压力/性能/长稳及生产加固继续延后。

本轮专项记录（2026-08-30，P-C 团队接管与产品体验）：新增 Queue → Change Passport → Findings/Evidence/Freshness/Lineage → owner decision 的 CI-only 隔离走查；验证素材为临时 SQLite、TestClient、Next production build 与 headless Chromium，未触碰真实 Case、生产数据库、Provider 或部署。当前仍未达到真实用户/流量、部署回滚或生产成熟度；全面安全扫描、压力/性能/长稳、大规模并发及生产多租户/RBAC/灾备/运营级发布加固无新增，继续按阶段延后。
