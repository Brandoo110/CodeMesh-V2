# P6-06A 研究者评分键

> **研究者文件：参与者不得打开本文件，也不得把本文件提供给参与者。**

八题固定各 1 分，满分 8 分。答案只要明确包含对应接受要素即可得分；不得因为参与者使用了不同但等价的措辞而扣分。`missed_conditions` 使用下面的稳定名称并以 `|` 分隔。

## 八题与接受要素

1. `provider_boundary_breach`（1 分）：指出路由/domain 代码直接 import 或构造 provider adapter/provider-specific endpoint；provider adapter 应位于外层边界。
2. `hardcoded_fallback`（1 分）：指出 fallback provider 或 fallback 控制被具体字符串/常量硬编码在变更中。
3. `retry_duplicate_side_effect`（1 分）：指出 timeout/retry 路径在重试时调用 `charge` 等副作用，且未展示幂等保护。
4. `unbounded_retries_or_cost`（1 分）：指出 `while True` 或等价的无限/无上限重复路径，且没有 max attempts 或 spend/cost budget。
5. `missing_fallback_trace`（1 分）：指出 fallback 使用没有 event、trace 或 metric 记录，无法从材料看到 fallback 发生。
6. `missing_kill_switch`（1 分）：指出没有 operator disable/kill-switch path；`enabled = True` 本身不是可操作的禁用路径。
7. `missing_owner_or_adr`（1 分）：指出 metadata 没有 owner/runbook，且没有 architecture decision record/ADR 材料。
8. `scope_creep_customer_data`（1 分）：指出 status response 额外返回 `customer_email`/customer data，超出只更新 status 的任务范围。

## Expected conditions

`provider_boundary_breach|hardcoded_fallback|retry_duplicate_side_effect|unbounded_retries_or_cost|missing_fallback_trace|missing_kill_switch|missing_owner_or_adr|scope_creep_customer_data`
