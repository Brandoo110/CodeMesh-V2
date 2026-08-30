# P-C 团队接管与产品走查

本目录中的 P-C 走查只在 GitHub Actions 执行，不启动本机前端服务器，也不触碰真实 Case、生产数据库或外部 Provider。

CI 会在隔离 SQLite/TestClient 中生成一个 `NEEDS_HUMAN` Case，验证：

- Queue 识别目标 Case，并读取 Passport、Findings、Evidence、Freshness 与 Lineage；
- 合格 `release_owner` 执行一次服务端允许的 owner decision；
- Decision POST 后再 GET authoritative CaseView，业务字段精确匹配后才算确认；
- stale digest 与 INVALIDATED Case 的请求均 fail closed，且不新增 human decision；
- 构建后的 UI 通过 headless Chromium 完成 desktop/mobile 走查，输出截图、日志和 JSON 结果 artifact。

结果 artifact 保留 7 天，名称为 `p-c-handover-walkthrough-<run_id>`。其中 API 结果标注 `production_evidence=false`，浏览器结果只证明 CI synthetic UI journey，不代表真实 API、团队或生产验收。
