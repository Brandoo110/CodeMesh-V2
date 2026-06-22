"""
Workflows / Steps / Runs endpoints 集成测试（v5 Phase 6.1）。

策略沿用 test_sessions.py：tempfile db + dependency_overrides 注入测试 store。

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_workflows
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.routes import workflows as workflows_module
from web.server import app
from web.workflows_store import WorkflowsStore, get_workflows_store


class TestWorkflowsEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)

        self.store = WorkflowsStore(db_path=self.db_path)
        asyncio.run(self.store.init())
        workflows_module._initialized = True

        app.dependency_overrides[get_workflows_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        workflows_module._initialized = False
        if self.db_path.exists():
            self.db_path.unlink()

    # ─────────── Workflows ───────────

    def test_create_and_list_workflow(self):
        r = self.client.post("/api/workflows", json={"name": "test", "description": "d"})
        self.assertEqual(r.status_code, 200)
        wf = r.json()
        self.assertEqual(wf["name"], "test")
        self.assertEqual(wf["step_count"], 0)
        self.assertFalse(wf["is_template"])

        lst = self.client.get("/api/workflows").json()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], wf["id"])

    def test_get_workflow_includes_steps(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        self.client.post(f"/api/workflows/{wf['id']}/steps",
                         json={"name": "step1", "model": "deepseek"})
        detail = self.client.get(f"/api/workflows/{wf['id']}").json()
        self.assertEqual(len(detail["steps"]), 1)
        self.assertEqual(detail["steps"][0]["name"], "step1")
        self.assertEqual(detail["steps"][0]["model"], "deepseek")

    def test_get_nonexistent_workflow_404(self):
        r = self.client.get("/api/workflows/nope")
        self.assertEqual(r.status_code, 404)

    def test_update_workflow_renames(self):
        wf = self.client.post("/api/workflows", json={"name": "old"}).json()
        r = self.client.put(f"/api/workflows/{wf['id']}",
                            json={"name": "new"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "new")

    def test_delete_workflow(self):
        wf = self.client.post("/api/workflows", json={"name": "doomed"}).json()
        r = self.client.delete(f"/api/workflows/{wf['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], wf["id"])
        # 再 get 应 404
        r2 = self.client.get(f"/api/workflows/{wf['id']}")
        self.assertEqual(r2.status_code, 404)

    # ─────────── Steps ───────────

    def test_add_step_assigns_order(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        s1 = self.client.post(f"/api/workflows/{wf['id']}/steps",
                              json={"name": "A"}).json()
        s2 = self.client.post(f"/api/workflows/{wf['id']}/steps",
                              json={"name": "B"}).json()
        self.assertEqual(s1["step_order"], 1)
        self.assertEqual(s2["step_order"], 2)

    def test_add_step_with_tool_allowlist(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        r = self.client.post(
            f"/api/workflows/{wf['id']}/steps",
            json={"name": "reviewer", "enable_tools": ["grep_text", "read_file"]},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["enable_tools"], ["grep_text", "read_file"])

    def test_update_step_partial(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        s = self.client.post(f"/api/workflows/{wf['id']}/steps",
                             json={"name": "orig", "model": "deepseek"}).json()
        r = self.client.put(
            f"/api/workflows/{wf['id']}/steps/{s['id']}",
            json={"name": "renamed"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "renamed")
        self.assertEqual(r.json()["model"], "deepseek")  # 不动

    def test_prompt_draft_routes_plan_request_to_planner(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        wid = wf["id"]
        planner = self.client.post(
            f"/api/workflows/{wid}/steps",
            json={"name": "1. Planner", "user_prompt": "先制定实现计划。"},
        ).json()
        self.client.post(
            f"/api/workflows/{wid}/steps",
            json={
                "name": "2. Coder",
                "user_prompt": "按计划写代码。",
                "enable_tools": ["write_file"],
            },
        )

        r = self.client.post(
            f"/api/workflows/{wid}/prompt-draft",
            json={
                "user_request": "在最开始的 plan prompt 里增加移动端适配需求",
                "run_context": "上次已经生成 HTML demo",
            },
        )

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["start_step_id"], planner["id"])
        self.assertEqual(data["changes"][0]["step_id"], planner["id"])
        self.assertEqual(data["changes"][0]["field"], "user_prompt")
        self.assertIn("移动端适配", data["changes"][0]["new_text"])

        detail = self.client.get(f"/api/workflows/{wid}").json()
        self.assertEqual(detail["steps"][0]["user_prompt"], "先制定实现计划。")

    def test_prompt_draft_routes_bug_request_to_coder(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        wid = wf["id"]
        self.client.post(
            f"/api/workflows/{wid}/steps",
            json={"name": "1. Planner", "user_prompt": "先制定实现计划。"},
        )
        coder = self.client.post(
            f"/api/workflows/{wid}/steps",
            json={
                "name": "2. Coder",
                "user_prompt": "按计划写代码。",
                "enable_tools": ["write_file"],
            },
        ).json()

        r = self.client.post(
            f"/api/workflows/{wid}/prompt-draft",
            json={
                "user_request": "修一下 Reset 按钮的 bug",
                "run_context": "上次已经生成 HTML demo",
            },
        )

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["start_step_id"], coder["id"])
        self.assertEqual(data["changes"][0]["step_id"], coder["id"])
        self.assertIn("Reset", data["changes"][0]["new_text"])

    def test_delete_step_compacts_order(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        wid = wf["id"]
        self.client.post(f"/api/workflows/{wid}/steps", json={"name": "A"})
        s2 = self.client.post(f"/api/workflows/{wid}/steps", json={"name": "B"}).json()
        self.client.post(f"/api/workflows/{wid}/steps", json={"name": "C"})
        self.client.delete(f"/api/workflows/{wid}/steps/{s2['id']}")
        detail = self.client.get(f"/api/workflows/{wid}").json()
        self.assertEqual([s["name"] for s in detail["steps"]], ["A", "C"])
        self.assertEqual(detail["steps"][1]["step_order"], 2)

    def test_reorder_steps(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        wid = wf["id"]
        s1 = self.client.post(f"/api/workflows/{wid}/steps", json={"name": "A"}).json()
        s2 = self.client.post(f"/api/workflows/{wid}/steps", json={"name": "B"}).json()
        s3 = self.client.post(f"/api/workflows/{wid}/steps", json={"name": "C"}).json()
        self.client.post(
            f"/api/workflows/{wid}/steps/reorder",
            json={"step_ids": [s3["id"], s1["id"], s2["id"]]},
        )
        detail = self.client.get(f"/api/workflows/{wid}").json()
        self.assertEqual([s["name"] for s in detail["steps"]], ["C", "A", "B"])

    # ─────────── Fork ───────────

    def test_fork_copies_steps(self):
        src = self.client.post("/api/workflows", json={"name": "src"}).json()
        self.client.post(f"/api/workflows/{src['id']}/steps",
                         json={"name": "A", "enable_tools": ["read_file"]})
        self.client.post(f"/api/workflows/{src['id']}/steps", json={"name": "B"})

        forked = self.client.post(f"/api/workflows/{src['id']}/fork").json()
        self.assertIn("副本", forked["name"])
        self.assertEqual(len(forked["steps"]), 2)
        self.assertEqual(forked["steps"][0]["enable_tools"], ["read_file"])

    # ─────────── Template Protection ───────────

    def test_template_workflow_cannot_be_edited(self):
        # 直接走 store 建模板（路由层不暴露 is_template 创建口）
        tpl = asyncio.run(self.store.create_workflow("tpl", "x", is_template=True))
        # update 应被拒
        r = self.client.put(f"/api/workflows/{tpl['id']}", json={"name": "hack"})
        self.assertEqual(r.status_code, 403)
        # delete 应被拒
        r2 = self.client.delete(f"/api/workflows/{tpl['id']}")
        self.assertEqual(r2.status_code, 403)
        # add step 应被拒
        r3 = self.client.post(f"/api/workflows/{tpl['id']}/steps", json={"name": "x"})
        self.assertEqual(r3.status_code, 403)

    # ─────────── Runs (read-only at Phase 6.1) ───────────

    def test_list_runs_empty(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        runs = self.client.get(f"/api/workflows/{wf['id']}/runs").json()
        self.assertEqual(runs, [])

    def test_get_run_nonexistent_404(self):
        r = self.client.get("/api/workflows/runs/nope")
        self.assertEqual(r.status_code, 404)

    def test_get_run_detail_includes_final_reply_and_step_logs(self):
        wf = asyncio.run(self.store.create_workflow("wf"))
        step = asyncio.run(self.store.add_step(wf["id"], name="Coder"))
        run = asyncio.run(self.store.create_run(wf["id"]))
        asyncio.run(self.store.save_step_result(
            run["id"],
            step,
            status="done",
            output="写好了页面",
            tool_calls=[{"name": "write_file", "ok": True}],
            cost_rmb=0.02,
            duration_ms=1000,
        ))
        asyncio.run(self.store.update_run(
            run["id"],
            status="done",
            total_cost=0.03,
            final_reply="最终回复",
        ))

        r = self.client.get(f"/api/workflows/runs/{run['id']}")

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["final_reply"], "最终回复")
        self.assertEqual(data["step_results"][0]["output"], "写好了页面")
        self.assertEqual(data["step_results"][0]["tool_calls"][0]["name"], "write_file")


if __name__ == "__main__":
    unittest.main()
