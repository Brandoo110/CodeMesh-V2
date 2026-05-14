"""
WorkflowOrchestrator + SSE 端点单测（v5 Phase 6.4）。

策略：
  monkey-patch web.workflow_orchestrator.Harness 让 _make_step_harness 返回 FakeHarness。
  这样 orchestrator 跑出"假执行"——可以验证：
    1. step 事件序列正确（run_start → step_start* → step_end* → done）
    2. 上一步 output 被拼到下一步 user_input 前
    3. tool_allowlist 被传给 FakeHarness（差异化护城河 #1 落地证据）
    4. step_results 落库正确（含 tool_calls / model_used / duration）
    5. SSE 端点端到端通

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_workflow_orchestrator
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import web.workflow_orchestrator as wo_module
from web.routes import workflows as workflows_module
from web.server import app
from web.workflow_orchestrator import WorkflowOrchestrator
from web.workflows_store import WorkflowsStore, get_workflows_store


# ─────────── Fake Harness 注入 ───────────

class FakeShortTerm:
    def __init__(self):
        self.system = ""
    def set_system(self, prompt: str):
        self.system = prompt
    def clear(self):
        pass
    def add(self, role, content):
        pass


class FakeHarness:
    """
    替换 web.workflow_orchestrator.Harness 用，模拟 run_stream_full。

    记录每次实例化的 system / allowlist / inputs，方便断言。
    """
    # class-level：所有实例共享，方便测试用 setUp 清空
    instances: list["FakeHarness"] = []
    # 注入式 reply 序列：每次 run 用一份；用尽时返回 ""
    reply_queue: list[list[dict]] = []

    def __init__(self, **kwargs):
        self.short_term = FakeShortTerm()
        self.tool_allowlist = None
        self.received_tasks: list[str] = []
        FakeHarness.instances.append(self)

    def set_tool_allowlist(self, allowlist):
        self.tool_allowlist = allowlist

    async def run_stream_full(self, task: str):
        self.received_tasks.append(task)
        events = FakeHarness.reply_queue.pop(0) if FakeHarness.reply_queue else [
            {"type": "token", "data": {"delta": "ok"}},
            {"type": "usage", "data": {"cost_rmb": 0.001, "model": "fake"}},
            {"type": "done", "data": {}},
        ]
        for ev in events:
            yield ev


# ─────────── 测试 ───────────

class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        # tempfile DB
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        self.store = WorkflowsStore(db_path=self.db_path)
        asyncio.run(self.store.init())

        # monkey-patch Harness 类
        FakeHarness.instances = []
        FakeHarness.reply_queue = []
        self._orig_harness = wo_module.Harness
        wo_module.Harness = FakeHarness

    def tearDown(self):
        wo_module.Harness = self._orig_harness
        if self.db_path.exists():
            self.db_path.unlink()

    def _build_two_step_workflow(self):
        wf = asyncio.run(self.store.create_workflow("test"))
        wid = wf["id"]
        s1 = asyncio.run(self.store.add_step(
            wid, name="A", model="deepseek", system_prompt="sys A",
            enable_tools=["grep_text", "read_file"],
        ))
        s2 = asyncio.run(self.store.add_step(
            wid, name="B", model="qwen", system_prompt="sys B",
            enable_tools=["*"],
        ))
        return wid, s1, s2

    def _run_collect(self, orchestrator, wid, run_id, **kwargs):
        events = []
        async def collect():
            async for ev in orchestrator.run(wid, run_id, **kwargs):
                events.append(ev)
        asyncio.run(collect())
        return events

    def test_run_emits_event_sequence(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)

        events = self._run_collect(orch, wid, run["id"])
        types = [e["type"] for e in events]

        self.assertEqual(types[0], "run_start")
        # run_start, step_start, token, usage, step_end, step_start, token, usage, step_end, done
        self.assertIn("step_start", types)
        self.assertIn("step_end", types)
        self.assertEqual(types[-1], "done")
        # 两个 step_start / step_end
        self.assertEqual(types.count("step_start"), 2)
        self.assertEqual(types.count("step_end"), 2)

    def test_prev_output_carries_to_next_step(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)

        # 步骤 1 返回 "step1 output"
        FakeHarness.reply_queue = [
            [
                {"type": "token", "data": {"delta": "step1 output"}},
                {"type": "done", "data": {}},
            ],
            [  # 步骤 2 默认
                {"type": "token", "data": {"delta": "step2 reply"}},
                {"type": "done", "data": {}},
            ],
        ]
        self._run_collect(orch, wid, run["id"])

        # 第二个 FakeHarness 实例接到的 task 应该含 "step1 output"
        self.assertEqual(len(FakeHarness.instances), 2)
        task2 = FakeHarness.instances[1].received_tasks[0]
        self.assertIn("step1 output", task2)

    def test_tool_allowlist_propagated_per_step(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)
        self._run_collect(orch, wid, run["id"])

        # Step 1 设置了 ["grep_text", "read_file"]
        self.assertEqual(FakeHarness.instances[0].tool_allowlist,
                         ["grep_text", "read_file"])
        # Step 2 全开 ["*"]
        self.assertEqual(FakeHarness.instances[1].tool_allowlist, ["*"])

    def test_step_system_prompt_applied(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)
        self._run_collect(orch, wid, run["id"])

        self.assertEqual(FakeHarness.instances[0].short_term.system, "sys A")
        self.assertEqual(FakeHarness.instances[1].short_term.system, "sys B")

    def test_step_results_persisted_with_tool_calls(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)

        FakeHarness.reply_queue = [
            [
                {"type": "tool_start", "data": {
                    "name": "grep_text", "args": {"pattern": "x"},
                }},
                {"type": "tool_end", "data": {
                    "name": "grep_text", "result": "hit", "ok": True,
                }},
                {"type": "token", "data": {"delta": "done A"}},
                {"type": "usage", "data": {"cost_rmb": 0.002, "model": "deepseek"}},
                {"type": "done", "data": {}},
            ],
            [
                {"type": "token", "data": {"delta": "done B"}},
                {"type": "usage", "data": {"cost_rmb": 0.003, "model": "qwen"}},
                {"type": "done", "data": {}},
            ],
        ]
        self._run_collect(orch, wid, run["id"])

        results = asyncio.run(self.store.get_step_results(run["id"]))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["output"], "done A")
        self.assertEqual(results[0]["model_used"], "deepseek")
        self.assertAlmostEqual(results[0]["cost_rmb"], 0.002)
        # tool_calls 应有一条 ok 状态
        self.assertIsNotNone(results[0]["tool_calls"])
        self.assertEqual(results[0]["tool_calls"][0]["name"], "grep_text")
        self.assertEqual(results[0]["tool_calls"][0]["status"], "ok")
        # run 标记 done
        final_run = asyncio.run(self.store.get_run(run["id"]))
        self.assertEqual(final_run["status"], "done")

    def test_step_error_terminates_workflow(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)

        FakeHarness.reply_queue = [
            [
                {"type": "error", "data": {"message": "boom"}},
                {"type": "done", "data": {}},
            ],
        ]
        events = self._run_collect(orch, wid, run["id"])
        types = [e["type"] for e in events]

        # 不应跑到 step 2
        self.assertEqual(types.count("step_start"), 1)
        # 最后一个事件是 done with ok=False
        self.assertEqual(events[-1]["type"], "done")
        self.assertFalse(events[-1]["data"]["ok"])

        final_run = asyncio.run(self.store.get_run(run["id"]))
        self.assertEqual(final_run["status"], "error")

    def test_cancel_flag_stops_between_steps(self):
        wid, s1, s2 = self._build_two_step_workflow()
        run = asyncio.run(self.store.create_run(wid))
        orch = WorkflowOrchestrator(self.store)
        # 在执行前就 cancel —— 第一个 step 之前的 boundary 就退出
        orch.cancel(run["id"])
        events = self._run_collect(orch, wid, run["id"])
        types = [e["type"] for e in events]
        self.assertIn("cancelled", types)
        # cancelled 时不应有任何 step_start
        self.assertEqual(types.count("step_start"), 0)
        final_run = asyncio.run(self.store.get_run(run["id"]))
        self.assertEqual(final_run["status"], "cancelled")


# ─────────── SSE 端点测试 ───────────

class TestRunEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        self.store = WorkflowsStore(db_path=self.db_path)
        asyncio.run(self.store.init())
        workflows_module._initialized = True
        workflows_module._orchestrator = None

        # monkey-patch Harness
        FakeHarness.instances = []
        FakeHarness.reply_queue = []
        self._orig_harness = wo_module.Harness
        wo_module.Harness = FakeHarness

        app.dependency_overrides[get_workflows_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        wo_module.Harness = self._orig_harness
        app.dependency_overrides.clear()
        workflows_module._initialized = False
        workflows_module._orchestrator = None
        if self.db_path.exists():
            self.db_path.unlink()

    def test_run_empty_workflow_400(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        r = self.client.post(f"/api/workflows/{wf['id']}/run")
        self.assertEqual(r.status_code, 400)

    def test_run_workflow_streams_sse(self):
        wf = self.client.post("/api/workflows", json={"name": "w"}).json()
        wid = wf["id"]
        self.client.post(f"/api/workflows/{wid}/steps",
                         json={"name": "A", "model": "deepseek"})
        with self.client.stream("POST", f"/api/workflows/{wid}/run") as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())
        # SSE 帧含 run_start / step_start / done
        self.assertIn("event: run_start", body)
        self.assertIn("event: step_start", body)
        self.assertIn("event: step_end", body)
        self.assertIn("event: done", body)

    def test_cancel_endpoint_returns_ok(self):
        r = self.client.post("/api/workflows/runs/anything/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
