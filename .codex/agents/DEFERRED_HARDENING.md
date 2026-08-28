# 延后加固清单

项目当前处于“本地产品收口 / 可用 MVP 集成”，当前主线已从 GP-05C2 转入 C3 Workbench Intake + Artifact Reader；以下事项暂不抢占产品闭环：

- 全面安全扫描与专项 AppSec 审计。
- 压力、性能、长稳和大规模并发测试。
- 生产多租户、完整 RBAC、灾备与运营级发布加固。

延后不适用于当前原子所必需的秘密保护、路径边界、startup fail-closed、focused negative tests、最小授权校验和可回滚性。阶段成熟度达到真实用户/流量、核心 API/算法稳定并具备部署与回滚证据后，应重新评估并提醒“项目应该进入下一阶段，应该更新 agents了”。

阶段成熟度提醒：项目应该进入下一阶段，应该更新 agents了。

本轮阶段判断：GP-05C2 已完成，但证据仍仅为本地 fake transport/runtime/HTTP；项目未达到真实用户/流量、部署回滚或生产成熟度，不切换阶段、不把长期目标写成 MVP 完成。

本轮新增后续项：runtime loader + fake reviewer 5xx/timeout → BLOCKED 的完整集成覆盖尚未补。该项属于当前产品闭环的完整性缺口，不是本轮 C2 里程碑阻塞，但必须在真实 Reviewer smoke 前补齐。
