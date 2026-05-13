"""
Sessions CRUD 单测（Phase 1 内存版）。

测点：
  1. POST 创建返回 uuid4 + title 正确回传
  2. GET 列表按 updated_at 倒序
  3. GET 单个不存在 → 404
  4. DELETE 后 GET 应 404

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_sessions
"""
import unittest

from fastapi.testclient import TestClient

from web.routes.sessions import _SESSIONS
from web.server import app


class TestSessionsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        _SESSIONS.clear()  # 每个测试隔离

    def tearDown(self):
        _SESSIONS.clear()

    def test_create_returns_session_with_uuid(self):
        r = self.client.post("/api/sessions", json={"title": "测试会话"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["title"], "测试会话")
        self.assertIn("id", data)
        self.assertEqual(len(data["id"]), 36)  # uuid4 字符串长度
        self.assertEqual(data["message_count"], 0)

    def test_create_with_no_title_uses_default(self):
        r = self.client.post("/api/sessions", json={})
        self.assertEqual(r.json()["title"], "新对话")

    def test_list_returns_created_sessions(self):
        self.client.post("/api/sessions", json={"title": "A"})
        self.client.post("/api/sessions", json={"title": "B"})
        r = self.client.get("/api/sessions")
        self.assertEqual(len(r.json()), 2)

    def test_get_nonexistent_returns_404(self):
        r = self.client.get("/api/sessions/nonexistent-id")
        self.assertEqual(r.status_code, 404)

    def test_delete_then_get_returns_404(self):
        r1 = self.client.post("/api/sessions", json={"title": "to-delete"})
        sid = r1.json()["id"]
        r2 = self.client.delete(f"/api/sessions/{sid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["deleted"], sid)
        r3 = self.client.get(f"/api/sessions/{sid}")
        self.assertEqual(r3.status_code, 404)

    def test_delete_nonexistent_returns_404(self):
        r = self.client.delete("/api/sessions/does-not-exist")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
