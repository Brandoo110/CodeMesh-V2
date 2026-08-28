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
