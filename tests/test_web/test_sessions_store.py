"""
SessionsStore 直接单测（Phase 5）。

不走 FastAPI，直接测 SQLite 层 —— 校验 schema / CRUD / message FIFO 排序。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from web.sessions_store import SessionsStore


def run(coro):
    """简化的 async test runner（Python 3.14 移除了 get_event_loop 隐式创建）。"""
    return asyncio.run(coro)


class TestSessionsStore(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        self.store = SessionsStore(db_path=self.db_path)
        run(self.store.init())

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_create_and_get(self):
        sess = run(self.store.create_session("test"))
        sid = sess["id"]
        loaded = run(self.store.get_session(sid))
        self.assertEqual(loaded["title"], "test")
        self.assertEqual(loaded["message_count"], 0)

    def test_list_orders_by_updated_at_desc(self):
        s1 = run(self.store.create_session("A"))
        # 让 B 在 A 之后创建
        s2 = run(self.store.create_session("B"))
        sessions = run(self.store.list_sessions())
        # B 最新所以在前
        self.assertEqual(sessions[0]["title"], "B")
        self.assertEqual(sessions[1]["title"], "A")

    def test_append_and_get_messages_in_order(self):
        sess = run(self.store.create_session("chat"))
        sid = sess["id"]
        run(self.store.append_message(sid, "user", "你好"))
        run(self.store.append_message(sid, "assistant", "你好！",
                                       model="deepseek", cost_rmb=0.001, duration_ms=850))
        msgs = run(self.store.get_messages(sid))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "你好")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["model"], "deepseek")
        self.assertAlmostEqual(msgs[1]["cost_rmb"], 0.001)

    def test_append_message_with_tool_calls_json_roundtrip(self):
        sess = run(self.store.create_session("tools"))
        sid = sess["id"]
        tool_calls = [
            {"name": "grep_text", "args": {"pattern": "x"}, "result": "hit", "ok": True, "status": "ok"},
        ]
        run(self.store.append_message(sid, "assistant", "找到", tool_calls=tool_calls))
        msgs = run(self.store.get_messages(sid))
        self.assertEqual(msgs[0]["tool_calls"], tool_calls)

    def test_delete_cascade_removes_messages(self):
        sess = run(self.store.create_session("doomed"))
        sid = sess["id"]
        run(self.store.append_message(sid, "user", "hi"))
        ok = run(self.store.delete_session(sid))
        self.assertTrue(ok)
        # 再查就 None
        self.assertIsNone(run(self.store.get_session(sid)))
        # 消息也应该没了
        msgs = run(self.store.get_messages(sid))
        self.assertEqual(msgs, [])

    def test_delete_nonexistent_returns_false(self):
        ok = run(self.store.delete_session("nope"))
        self.assertFalse(ok)

    def test_message_count_in_get_session(self):
        sess = run(self.store.create_session("counted"))
        sid = sess["id"]
        run(self.store.append_message(sid, "user", "1"))
        run(self.store.append_message(sid, "assistant", "2"))
        run(self.store.append_message(sid, "user", "3"))
        loaded = run(self.store.get_session(sid))
        self.assertEqual(loaded["message_count"], 3)

    def test_update_session_refreshes_updated_at(self):
        sess = run(self.store.create_session("orig"))
        sid = sess["id"]
        orig_updated = sess["updated_at"]
        # 更新 model
        run(self.store.update_session(sid, model="qwen"))
        loaded = run(self.store.get_session(sid))
        self.assertEqual(loaded["model"], "qwen")
        # updated_at 应该被刷新
        self.assertGreater(loaded["updated_at"], orig_updated)


if __name__ == "__main__":
    unittest.main()
