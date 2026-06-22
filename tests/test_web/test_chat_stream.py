"""
POST /api/chat/stream 单测（SSE 流式）。

策略：
  Mock harness.run_stream_full 让它 yield 一个预设的 event 序列。
  用 TestClient.stream 拿完整 SSE body，按字符串匹配验证 event 类型 / data。

注意：
  - app.dependency_overrides 替换 get_harness（同 test_chat.py 模式）
  - run_stream_full 是 async generator，不是 AsyncMock —— 用真的 async gen 函数
  - SSE 帧格式（sse-starlette 输出）:
        event: <type>\\n
        data: <json>\\n
        \\n

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_chat_stream
"""
import unittest

from fastapi.testclient import TestClient

from web.deps import get_harness
from web.server import app


def make_fake_stream(events: list[dict]):
    """构造 async generator 返回预设 events。"""
    async def gen(_task: str):
        for ev in events:
            yield ev
    return gen


class TestChatStreamEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _inject_stream(self, events):
        class FakeShortTerm:
            def clear(self):
                pass
            def add(self, role, content):
                pass

        class FakeHarness:
            # chat.py 用 try/finally 保存/恢复 preferred_model；fake 也要有这两个
            preferred_model: "str | None" = None
            def set_preferred_model(self, m):
                self.preferred_model = m
        fake = FakeHarness()
        fake.run_stream_full = make_fake_stream(events)
        fake.short_term = FakeShortTerm()  # chat_stream ephemeral 路径会 .clear()
        app.dependency_overrides[get_harness] = lambda: fake

    def test_stream_yields_token_and_done(self):
        self._inject_stream([
            {"type": "token", "data": {"delta": "你"}},
            {"type": "token", "data": {"delta": "好"}},
            {"type": "done", "data": {}},
        ])
        with self.client.stream("POST", "/api/chat/stream", json={"task": "hi"}) as r:
            self.assertEqual(r.status_code, 200)
            body = "".join(r.iter_text())

        # SSE 帧含三个 event
        self.assertIn("event: token", body)
        self.assertIn('"delta": "你"', body)
        self.assertIn('"delta": "好"', body)
        self.assertIn("event: done", body)

    def test_stream_yields_tool_events(self):
        self._inject_stream([
            {"type": "tool_start", "data": {"name": "grep_text", "args": {"pattern": "harness"}}},
            {"type": "tool_end", "data": {"name": "grep_text", "result": "hit1\nhit2", "ok": True}},
            {"type": "token", "data": {"delta": "找到 2 个匹配"}},
            {"type": "done", "data": {}},
        ])
        with self.client.stream("POST", "/api/chat/stream", json={"task": "找 harness"}) as r:
            body = "".join(r.iter_text())

        self.assertIn("event: tool_start", body)
        self.assertIn('"name": "grep_text"', body)
        self.assertIn("event: tool_end", body)
        self.assertIn('"ok": true', body)
        self.assertIn("event: done", body)

    def test_stream_yields_error_event(self):
        self._inject_stream([
            {"type": "error", "data": {"message": "RuntimeError: boom"}},
        ])
        with self.client.stream("POST", "/api/chat/stream", json={"task": "x"}) as r:
            self.assertEqual(r.status_code, 200)  # SSE 一旦开始 HTTP 永远 200
            body = "".join(r.iter_text())

        self.assertIn("event: error", body)
        self.assertIn("RuntimeError", body)

    def test_stream_rejects_empty_task(self):
        self._inject_stream([])
        # Pydantic min_length=1 在解析阶段就 422，不进 stream
        r = self.client.post("/api/chat/stream", json={"task": ""})
        self.assertEqual(r.status_code, 422)

    def test_stream_emits_usage_event(self):
        self._inject_stream([
            {"type": "token", "data": {"delta": "答"}},
            {"type": "usage", "data": {"prompt": 100, "completion": 50, "cost_rmb": 0.0021, "model": "deepseek"}},
            {"type": "done", "data": {}},
        ])
        with self.client.stream("POST", "/api/chat/stream", json={"task": "hi"}) as r:
            body = "".join(r.iter_text())

        self.assertIn("event: usage", body)
        self.assertIn('"cost_rmb": 0.0021', body)
        self.assertIn('"model": "deepseek"', body)


if __name__ == "__main__":
    unittest.main()
