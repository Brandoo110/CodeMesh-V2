# P-C 团队接管与产品走查

本走查只在 GitHub Actions 执行，不启动本机前端服务器，也不触碰 V1、真实 Case、生产数据库或外部 Provider。

## Python authoritative walkthrough

`scripts/p_c_handover_walkthrough.py` 在隔离临时 Git workspace 中通过真实 server-owned composition 生成两个持久 Case：

- `LiveFreshnessChecker(workspace_root=...)`；
- `AssuranceWebRepository(..., live_required=True)`；
- `AssuranceRunService(..., committer=repository)`。

reviewer 与命令仅是脚本内的确定性协议适配器；Case、Bundle、Evidence、Finding、Policy 和事件均由 Run Service 组合并由同一 Repository 持久化。脚本不 import 测试 helper，也不手工 append policy/event 或触碰 web projection 私有写入口。

流程和断言覆盖：

- Queue 识别两个 Case；Queue 未实时扫描时允许 `FRESHNESS_NOT_CHECKED`，detail/passport 再由 authoritative GET 检查；
- 决策前 authoritative GET 必须为 `FRESH` / `FRESHNESS_MATCH` / `live_required`，并从 `allowed_actions` 读取 `approve` 与 `required_human_role`；
- 合格 owner 提交后再次 authoritative GET，与 POST 业务投影精确匹配并进入 `ACCEPTED`；
- 独立未决 Case 的真实 freshness checker 返回 `UNAVAILABLE`，只保留 `download_passport`；当前 digest 的决策 POST 返回 `409 ACTION_NOT_ALLOWED`，前后 GET 的 human refs、revision、acceptance state、policy gate 和业务投影无漂移；
- 真实源文件变化触发 `STALE`，由服务端持久化 `INVALIDATED`；随后精确恢复 durable run 的 `TASK.md` 与 `changed.txt`，authoritative GET 必须回到 `FRESH/FRESHNESS_MATCH` 但保持 `INVALIDATED`，再验证 INVALIDATED、旧 digest 和未授权 `waiver` 分别 fail closed 且不新增 human decision；
- `walkthrough.json` 保存各场景的 authoritative pre/post readback 和明确布尔结果。

## Headless browser journey

CI 串行运行 `scripts/p_c_browser_walkthrough.mjs`：目标用户从 Change Queue 进入同一 Case，打开 Passport、Findings、Evidence、Freshness、Lineage，截图决策前的 `FRESH`，从 authoritative `allowed_actions` 使用准确 owner role 完成 decision，再截图 `ACCEPTED` readback；随后打开 `UNAVAILABLE` Case，确认 UI 只显示 Passport 下载，尝试当前 digest POST 并以 authoritative GET 证明 no drift。

产物至少包括：`desktop-fresh-before-decision.png`、`desktop-readback-accepted.png`、`desktop-unavailable-no-drift.png`、`mobile-case-passport.png`、`browser-walkthrough.json` 和 `browser-walkthrough.log`。浏览器与 build 只在 CI 执行，任务结束关闭 browser。

所有结果明确标注为隔离 synthetic/CI walkthrough；它们不等同于 CodeMesh 自身真实 Reviewer、Case、官方 authoritative Bundle 或生产验收证据。

现有 workflow 会上传 `p-c-handover-walkthrough-<run_id>` artifact，保留 7 天。
