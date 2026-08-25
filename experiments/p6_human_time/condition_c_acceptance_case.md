> **参与规则：你只能阅读当前被分配的 C 条件文件；不要打开 `answer_key.md` 或 A/B 条件文件。**

# Condition C — Acceptance Case

请只阅读本文件，完成末尾八个问题，并记录阅读开始/结束时间、原始 Evidence 打开次数和 trust 1-5。以下结构化 Acceptance Case 复用同一离线事实，提供交接和状态链条；它不表示 Council 已晋级默认路径。

<!-- FIXED_PACKET_BEGIN -->
## Fixed offline packet

### Task brief

A release engineer asks for a small Python change to the routing service. The change adds a status response and uses the repository's normal model-routing path. Provider adapters belong at the outer boundary; the change must not add customer data. The review brief asks for bounded retries, an operator disable path, ownership/runbook material, and an architecture record. The supplied green test covers only the normal provider-success path.

### Unified diff

```diff
diff --git a/service/route.py b/service/route.py
index 1111111..2222222 100644
--- a/service/route.py
+++ b/service/route.py
@@ -1,4 +1,24 @@
 from typing import Callable
-
+
+from provider_adapter import ProviderAdapter
+
+DEFAULT_PROVIDER = "openai-codex-desktop"
+FALLBACK_PROVIDER = "deepseek-local"
+SERVICE_METADATA = {"name": "status-route", "release": "v2"}
+
+
 def run_status(task: dict, charge: Callable[[str], None]) -> dict:
-    return {"status": "ok"}
+    enabled = True
+    provider = DEFAULT_PROVIDER
+    attempts = 0
+    while True:
+        try:
+            result = ProviderAdapter(provider).send(task)
+            charge(result)
+            return {
+                "status": result,
+                "customer_email": task.get("customer_email"),
+            }
+        except TimeoutError:
+            attempts += 1
+            provider = FALLBACK_PROVIDER
```

### Green test output

```text
$ python -m unittest -q tests/test_status.py
----------------------------------------------------------------------
Ran 1 test in 0.01s

OK
```
<!-- FIXED_PACKET_END -->

## Structured Acceptance Case

### Old digest

- `case_id`: `assurance-demo-old-digest`
- `subject_digest`: `sha256:b937189ff110571133b908a5d02c824701838b65ccb012042a0b65c7957891a1`
- state/gate: `INVALIDATED` / `INVALIDATED`
- Evidence:
  - `assurance-demo-old-digest:evidence:builder-green` — builder green test output only
  - `assurance-demo-old-digest:evidence:diff` — unified diff
  - `assurance-demo-old-digest:evidence:author-receipt` — author-agent receipt
- Role review:
  - Intent: no operator disable path; response scope includes customer data.
  - Architecture: provider adapter boundary and provider-specific fallback; owner/architecture record material is absent.
  - Operability: the same `try/except` covers provider send and `charge`; a timeout advances to the fallback and loops without an idempotency key, while `attempts` has no upper bound or spend budget and the provider switch has no event/trace/metric.
- Policy: `BLOCKED`, because the governance findings require repair before acceptance.
- Adjudicator conflict: intent scope boundary and architecture migration prerequisite were recorded as conflicting views.
- Timeline: `collect → review → conflict → invalidate`.

### Repair and new digest

下列生命周期说明的是另一个、已修复的 subject；它的 Diff 是新的 Evidence artifact，不在本次八题材料中。八题只针对上方 old-digest Diff，不能把旧 Diff 当成最终被签收的代码。

- `case_id`: `assurance-demo-new-digest`
- `subject_digest`: `sha256:1abe85611db8645a02b90dadcdf265894aead41d3aca12cf8ee12c6ffb80247f`
- Evidence classes are retained with new content and summaries:
  - `assurance-demo-new-digest:evidence:builder-green`
  - `assurance-demo-new-digest:evidence:diff`
  - `assurance-demo-new-digest:evidence:author-receipt`
- The repaired subject receives Intent, Architecture, and Operability re-review; the earlier findings are marked resolved.
- Policy: `NEEDS_HUMAN`; the high-risk routing change requires the `release_owner` role.
- The author is `demo-author`; the release owner is `release-owner`; they are different people/roles.
- HumanDecision: `approve`; final state/gate: `ACCEPTED` / `ACCEPTED`; digest freshness is true; a passport is available.

当前默认仍是 Single Strong Reviewer + Policy Gate；Council 没有晋级默认路径，只在 high-risk 或 cross-role conflict 的实验 allowlist 中使用，并仍需现有 Gate/HITL。

## 参与者问题

1. 路由代码跨过了哪个边界？
2. 变更中出现了什么 fallback 行为？
3. 如果操作被重试，可能发生什么？
4. 对重复尝试或花费，代码中能看到什么上限？
5. 哪条记录可以显示 fallback 被使用？
6. 操作者在哪里可以禁用该功能？
7. 提供了什么 ownership 或 architecture record？
8. 哪个 response 字段超出了只更新 status 的请求？
