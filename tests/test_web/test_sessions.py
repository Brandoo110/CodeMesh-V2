"""
Sessions CRUD 单测（Phase 5：真接 SQLite）。

策略：
  每个测试 setUp 用 tempfile 建临时 db，dependency_overrides 注入测试 store。
  tearDown 清理临时 db。比 mock 真实 —— 顺便测 SQL schema 没写错。

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_sessions
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.routes import sessions as sessions_module
from web.server import app
from web.sessions_store import SessionsStore, get_sessions_store


class TestSessionsEndpoints(unittest.TestCase):
    def setUp(self):
        # 临时 db 文件
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)

        # 测试用 store + 重置 init 标记
        self.store = SessionsStore(db_path=self.db_path)
        asyncio.run(self.store.init())
        sessions_module._initialized = True

        app.dependency_overrides[get_sessions_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        sessions_module._initialized = False
        if self.db_path.exists():
            self.db_path.unlink()

    def test_create_returns_session_with_uuid(self):
        r = self.client.post("/api/sessions", json={"title": "测试会话"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["title"], "测试会话")
        self.assertEqual(len(data["id"]), 36)
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

    def test_update_session_title(self):
        r1 = self.client.post("/api/sessions", json={"title": "新对话"})
        sid = r1.json()["id"]

        r2 = self.client.put(
            f"/api/sessions/{sid}",
            json={"title": "求职投递看板讨论"},
        )

        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["title"], "求职投递看板讨论")
        sessions = self.client.get("/api/sessions").json()
        self.assertEqual(sessions[0]["title"], "求职投递看板讨论")

    def test_update_session_title_rejects_blank(self):
        r1 = self.client.post("/api/sessions", json={"title": "新对话"})
        sid = r1.json()["id"]

        r2 = self.client.put(f"/api/sessions/{sid}", json={"title": "   "})

        self.assertEqual(r2.status_code, 422)

    def test_get_messages_empty_session(self):
        r = self.client.post("/api/sessions", json={"title": "empty"})
        sid = r.json()["id"]
        msgs = self.client.get(f"/api/sessions/{sid}/messages")
        self.assertEqual(msgs.status_code, 200)
        self.assertEqual(msgs.json(), [])

    def test_get_messages_nonexistent_session_404(self):
        r = self.client.get("/api/sessions/nope/messages")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
