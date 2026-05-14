"""
WorkflowsStore 直接单测（v5 Phase 6.1）。

不走 FastAPI，直接测 SQLite 层。覆盖：
- workflows CRUD（含模板 / step_count）
- steps CRUD（含 step_order 维护 / 重排序）
- runs + step_results（含 JSON 字段 roundtrip）
- 级联删除
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from web.workflows_store import WorkflowsStore


def run(coro):
    """Python 3.14 移除了 get_event_loop 隐式创建；用 asyncio.run。"""
    return asyncio.run(coro)


class TestWorkflowsStore(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        self.store = WorkflowsStore(db_path=self.db_path)
        run(self.store.init())

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    # ─────────── Workflows CRUD ───────────

    def test_create_and_get_workflow(self):
        wf = run(self.store.create_workflow("test wf", "desc"))
        wid = wf["id"]
        loaded = run(self.store.get_workflow(wid))
        self.assertEqual(loaded["name"], "test wf")
        self.assertEqual(loaded["description"], "desc")
        self.assertEqual(loaded["step_count"], 0)
        self.assertFalse(loaded["is_template"])

    def test_list_workflows_templates_first(self):
        run(self.store.create_workflow("user-wf-1"))
        run(self.store.create_workflow("template-1", is_template=True))
        run(self.store.create_workflow("user-wf-2"))
        rows = run(self.store.list_workflows())
        # 模板应该在前
        self.assertEqual(rows[0]["name"], "template-1")
        self.assertTrue(rows[0]["is_template"])
        # 后两个按 updated_at desc：user-wf-2 在 user-wf-1 前
        self.assertEqual(rows[1]["name"], "user-wf-2")
        self.assertEqual(rows[2]["name"], "user-wf-1")

    def test_update_workflow_refreshes_updated_at(self):
        wf = run(self.store.create_workflow("orig"))
        orig_updated = wf["updated_at"]
        run(self.store.update_workflow(wf["id"], name="renamed"))
        loaded = run(self.store.get_workflow(wf["id"]))
        self.assertEqual(loaded["name"], "renamed")
        self.assertGreaterEqual(loaded["updated_at"], orig_updated)

    def test_delete_workflow_cascades_steps_runs_results(self):
        wf = run(self.store.create_workflow("doomed"))
        wid = wf["id"]
        s = run(self.store.add_step(wid, name="step1"))
        r = run(self.store.create_run(wid))
        run(self.store.save_step_result(
            r["id"], s, status="done", output="x", cost_rmb=0.001, duration_ms=100,
        ))
        ok = run(self.store.delete_workflow(wid))
        self.assertTrue(ok)
        # workflow 没了
        self.assertIsNone(run(self.store.get_workflow(wid)))
        # steps 没了
        self.assertEqual(run(self.store.get_steps(wid)), [])
        # run 没了
        self.assertIsNone(run(self.store.get_run(r["id"])))
        # step_results 没了（CASCADE 走 run 那条）
        self.assertEqual(run(self.store.get_step_results(r["id"])), [])

    # ─────────── Steps CRUD ───────────

    def test_add_step_assigns_sequential_order(self):
        wf = run(self.store.create_workflow("wf"))
        wid = wf["id"]
        s1 = run(self.store.add_step(wid, name="A"))
        s2 = run(self.store.add_step(wid, name="B"))
        s3 = run(self.store.add_step(wid, name="C"))
        self.assertEqual(s1["step_order"], 1)
        self.assertEqual(s2["step_order"], 2)
        self.assertEqual(s3["step_order"], 3)

    def test_get_steps_returns_in_order_with_tools(self):
        wf = run(self.store.create_workflow("wf"))
        wid = wf["id"]
        run(self.store.add_step(wid, name="A", enable_tools=["grep_text", "read_file"]))
        run(self.store.add_step(wid, name="B"))  # 默认 ["*"]
        steps = run(self.store.get_steps(wid))
        self.assertEqual([s["name"] for s in steps], ["A", "B"])
        self.assertEqual(steps[0]["enable_tools"], ["grep_text", "read_file"])
        self.assertEqual(steps[1]["enable_tools"], ["*"])

    def test_update_step_partial_fields(self):
        wf = run(self.store.create_workflow("wf"))
        s = run(self.store.add_step(wf["id"], name="orig", model="deepseek"))
        run(self.store.update_step(s["id"], name="renamed", enable_tools=["read_file"]))
        loaded = run(self.store.get_step(s["id"]))
        self.assertEqual(loaded["name"], "renamed")
        self.assertEqual(loaded["model"], "deepseek")  # 不动
        self.assertEqual(loaded["enable_tools"], ["read_file"])

    def test_delete_step_compacts_order(self):
        wf = run(self.store.create_workflow("wf"))
        wid = wf["id"]
        run(self.store.add_step(wid, name="A"))
        s2 = run(self.store.add_step(wid, name="B"))
        run(self.store.add_step(wid, name="C"))
        ok = run(self.store.delete_step(s2["id"]))
        self.assertTrue(ok)
        steps = run(self.store.get_steps(wid))
        self.assertEqual([s["name"] for s in steps], ["A", "C"])
        # C 的 order 应从 3 → 2
        self.assertEqual(steps[1]["step_order"], 2)

    def test_reorder_steps_reassigns_orders(self):
        wf = run(self.store.create_workflow("wf"))
        wid = wf["id"]
        s1 = run(self.store.add_step(wid, name="A"))
        s2 = run(self.store.add_step(wid, name="B"))
        s3 = run(self.store.add_step(wid, name="C"))
        # 倒序
        run(self.store.reorder_steps(wid, [s3["id"], s2["id"], s1["id"]]))
        steps = run(self.store.get_steps(wid))
        self.assertEqual([s["name"] for s in steps], ["C", "B", "A"])
        self.assertEqual([s["step_order"] for s in steps], [1, 2, 3])

    # ─────────── Runs + StepResults ───────────

    def test_create_run_and_get(self):
        wf = run(self.store.create_workflow("wf"))
        r = run(self.store.create_run(wf["id"]))
        self.assertEqual(r["status"], "running")
        self.assertIsNone(r["completed_at"])
        loaded = run(self.store.get_run(r["id"]))
        self.assertEqual(loaded["workflow_id"], wf["id"])

    def test_update_run_to_terminal_sets_completed_at(self):
        wf = run(self.store.create_workflow("wf"))
        r = run(self.store.create_run(wf["id"]))
        run(self.store.update_run(r["id"], status="done", total_cost=0.05))
        loaded = run(self.store.get_run(r["id"]))
        self.assertEqual(loaded["status"], "done")
        self.assertIsNotNone(loaded["completed_at"])
        self.assertAlmostEqual(loaded["total_cost_rmb"], 0.05)

    def test_step_result_json_roundtrip(self):
        """tool_calls / file_diffs 两个 JSON 字段必须能完整往返。"""
        wf = run(self.store.create_workflow("wf"))
        s = run(self.store.add_step(wf["id"], name="A"))
        r = run(self.store.create_run(wf["id"]))
        tool_calls = [
            {"name": "grep_text", "args": {"pattern": "x"}, "result": "hit", "ok": True},
        ]
        file_diffs = [
            {"path": "a.py", "before": "old", "after": "new", "kind": "modified"},
        ]
        run(self.store.save_step_result(
            r["id"], s,
            status="done", output="完成",
            tool_calls=tool_calls, file_diffs=file_diffs,
            model_used="deepseek", cost_rmb=0.001, duration_ms=850,
        ))
        results = run(self.store.get_step_results(r["id"]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_calls"], tool_calls)
        self.assertEqual(results[0]["file_diffs"], file_diffs)
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["model_used"], "deepseek")

    def test_list_runs_ordered_by_started_desc(self):
        wf = run(self.store.create_workflow("wf"))
        r1 = run(self.store.create_run(wf["id"]))
        r2 = run(self.store.create_run(wf["id"]))
        rows = run(self.store.list_runs(wf["id"]))
        # r2 后创建 → 在前
        self.assertEqual(rows[0]["id"], r2["id"])
        self.assertEqual(rows[1]["id"], r1["id"])

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(run(self.store.delete_workflow("nope")))
        self.assertFalse(run(self.store.delete_step("nope")))


if __name__ == "__main__":
    unittest.main()
