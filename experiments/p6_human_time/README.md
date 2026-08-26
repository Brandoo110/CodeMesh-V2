# P6-06A 人工时间实验材料

> **状态：已取消，不属于 CodeMesh V2 MVP Gate。** 2026-08-26 的产品决策认为当前无需为“结构化 Acceptance Case 比分散材料更容易接管”这一方向性前提组织三人实验。材料保留供未来需要验证具体 UI 或量化商业指标时复用；`results.csv` 保持空表，human review time 记为 `not_measured`。

这是一个离线、固定事实的内部走查材料包，不包含任何伪造的人类观测。三次走查分别使用 A、B、C 条件；建议招募 3 位参与者，每位参与者随机只看一个 condition。不要让同一人重复多个 condition；若因资源限制重复，必须标记为较弱的探索性观察，因为会有学习效应。

## 分配与边界

参与者只打开被分配的 `condition_a_diff_only.md`、`condition_b_single_reviewer.md` 或 `condition_c_acceptance_case.md`。参与者不得打开其他 condition 或 `answer_key.md`。`answer_key.md` 仅供研究者评分。

三个 condition 使用完全相同的离线模型路由/fallback 变更、任务 brief、统一 diff 和 green test output；差别只在附加审查材料的结构化程度，不改变题目事实或难度。没有网络、模型调用、生产服务或真实成本/延迟观测。

## 走查流程

1. 研究者随机分配一个 condition，给参与者一个不存真实姓名的 `participant_alias`。alias 不得包含姓名、邮箱、手机号或其他可识别信息。
2. `started_at` 是参与者第一次打开被分配文件并开始阅读时记录的 ISO-8601 时间。
3. `completed_at` 是参与者提交八题答案和信任评分后记录的 ISO-8601 时间。
4. `comprehension_seconds` 是上述两时刻的非负整数秒差；不要用估计值填充。
5. `raw_evidence_opens` 是参与者实际打开原始 Evidence/source artifact 的次数：每次打开计 1 次，重新打开同一 artifact 也计 1 次；只在已打开的条件文件中再次阅读渲染文本不计一次。打开 `answer_key.md` 或其他 condition 不计入有效数据，并应终止该次独立走查。
6. 参与者回答固定八题，并给出 trust 1-5。研究者只按 `answer_key.md` 的接受要素评分，不向参与者提示答案。

### 固定八题

1. 路由代码跨过了哪个边界？
2. 变更中出现了什么 fallback 行为？
3. 如果操作被重试，可能发生什么？
4. 对重复尝试或花费，代码中能看到什么上限？
5. 哪条记录可以显示 fallback 被使用？
6. 操作者在哪里可以禁用该功能？
7. 提供了什么 ownership 或 architecture record？
8. 哪个 response 字段超出了只更新 status 的请求？

### 信任评分

`trust_1_to_5` 只能填整数 1-5：1=完全不信任材料，2=较不信任，3=中立，4=较信任，5=完全信任。它是参与者的主观报告，不是正确率的替代物。

### 评分与结果记录

八题每题 1 分，只有答案包含 `answer_key.md` 对应的接受要素才记 1 分，满分 8 分。未覆盖的条件用 `missed_conditions` 记录，使用 answer key 中稳定的 condition 名称，并以 `|` 分隔；没有遗漏时留空。`total_questions` 固定为 8，`evidence_level` 固定为 `human_reported`。`results.csv` 的时间必须是 ISO-8601，秒数和 Evidence 打开次数必须为非负整数。

`n=3` 只描述三次内部走查中的观察，不计算显著性，不写统计结论、因果结论或泛化结论。Agent dry-run、自动生成答案或研究者代答都不是 human_reported，不得写入完成记录。

## 完成标准

若未来重新启动实验，只有在 `results.csv` 中有恰好 3 条真实的 `human_reported` 记录、每条来自不同 participant_alias 且各自只看一个随机 condition 时才算完成。当前实验已取消，空表是刻意保留的 `not_measured` 状态；不要为了填表伪造数据。
