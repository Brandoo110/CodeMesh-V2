> **参与规则：你只能阅读当前被分配的 A 条件文件；不要打开 `answer_key.md` 或 B/C 条件文件。**

# Condition A — Diff Only

请只阅读本文件，完成末尾八个问题，并记录阅读开始/结束时间、原始 Evidence 打开次数和 trust 1-5。材料是离线固定事实，不需要联网或调用模型。

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

## 参与者问题

1. 路由代码跨过了哪个边界？
2. 变更中出现了什么 fallback 行为？
3. 如果操作被重试，可能发生什么？
4. 对重复尝试或花费，代码中能看到什么上限？
5. 哪条记录可以显示 fallback 被使用？
6. 操作者在哪里可以禁用该功能？
7. 提供了什么 ownership 或 architecture record？
8. 哪个 response 字段超出了只更新 status 的请求？
