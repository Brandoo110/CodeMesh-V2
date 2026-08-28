# CodeMesh V2 持久 Agent 团队

本团队服务于“本地产品收口 / 可用 MVP 集成”阶段。总控先冻结一页任务包（目标、HEAD、worktree、allowlist、不变量、验证命令、禁止事项和停止条件），再按职责启用最少成员；一个代码原子只能有一个 writer。

角色配置位于本目录：

- `assurance_runtime_writer.toml`：Assurance runtime 原子唯一 writer，Luna Max。
- `product_slice_writer.toml`：产品切片原子唯一 writer，Luna High。
- `risk_reviewer.toml`：高风险原子的独立只读 reviewer，Luna Max；不编辑、不测试、不提交、不启动服务。
- `canonical_recorder.toml`：canonical 与治理记录 writer，Luna Medium；不碰业务代码。

默认并发上限由 `.codex/config.toml` 的 `[agents] max_concurrent_threads_per_session = 3` 约束，并启用 `interrupt_message = true`。DeepSeek 不在默认团队中；仅当用户当次明确指定时，另行启用隔离 worker，并由总控验收。未通过总控 ACCEPT 的候选不得集成、启动或发布。
