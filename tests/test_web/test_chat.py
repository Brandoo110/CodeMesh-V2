"""
POST /api/chat 单测（mock Harness）。

策略：
  用 FastAPI app.dependency_overrides 替换 get_harness，注入 AsyncMock。
  比 monkeypatch 全局单例干净，每个测试 tearDown 清理。

测点：
  1. 正常 task → 200 + answer 字段
  2. 空 task → 422（Pydantic min_length=1 自动校验）
  3. harness.run 抛异常 → 500
  4. last_costs 推断 model 和 cost 总额

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_chat
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from web.deps import get_harness
from web.server import app


class TestChatEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

        # Fake harness 不调真实 API（CodeMesh CLAUDE.md 测试铁律）
        self.fake_harness = AsyncMock()
        self.fake_harness.run = AsyncMock(return_value="模拟回答内容")
        self.fake_harness.last_costs = []
        app.dependency_overrides[get_harness] = lambda: self.fake_harness

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_chat_returns_answer(self):
        r = self.client.post("/api/chat", json={"task": "hello"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["answer"], "模拟回答内容")
        self.assertIn("duration_ms", data)
        self.assertIn("model", data)
        self.assertIn("cost_rmb", data)

    def test_chat_rejects_empty_task(self):
        r = self.client.post("/api/chat", json={"task": ""})
        self.assertEqual(r.status_code, 422)

    def test_chat_rejects_missing_task(self):
        r = self.client.post("/api/chat", json={})
        self.assertEqual(r.status_code, 422)

    def test_chat_returns_500_on_exception(self):
        self.fake_harness.run = AsyncMock(side_effect=RuntimeError("boom"))
        r = self.client.post("/api/chat", json={"task": "hi"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("RuntimeError", r.json()["detail"])

    def test_chat_aggregates_cost_from_last_costs(self):
        # mock 两条 CallCost
        self.fake_harness.last_costs = [
            SimpleNamespace(model="deepseek", cost_rmb=0.0021),
            SimpleNamespace(model="deepseek", cost_rmb=0.0009),
        ]
        r = self.client.post("/api/chat", json={"task": "hi"})
        data = r.json()
        self.assertEqual(data["model"], "deepseek")
        self.assertAlmostEqual(data["cost_rmb"], 0.0030, places=4)

    def test_no_session_id_returns_none(self):
        """ephemeral chat：不传 session_id，response 也是 None。"""
        r = self.client.post("/api/chat", json={"task": "hi"})
        self.assertIsNone(r.json().get("session_id"))

    def test_nonexistent_session_id_returns_404(self):
        """Phase 5：不存在的 session_id 直接 404。"""
        r = self.client.post("/api/chat", json={"task": "hi", "session_id": "nope"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
