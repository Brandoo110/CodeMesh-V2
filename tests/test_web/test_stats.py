"""GET /api/stats 单测（HTML 嵌入用）。"""
import unittest

from fastapi.testclient import TestClient

from web.server import app


class TestStatsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_returns_html_content_type(self):
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_html_body_contains_codemesh_title(self):
        r = self.client.get("/api/stats?range=30d")
        # render_stats_dashboard 始终输出含 "CodeMesh stats" 字样
        self.assertIn("CodeMesh stats", r.text)

    def test_default_range_30d(self):
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 200)
        # 默认 30 天窗口
        self.assertIn("last 30d", r.text)

    def test_all_range_returns_all_time(self):
        r = self.client.get("/api/stats?range=all")
        self.assertIn("all-time", r.text)

    def test_invalid_range_falls_back_to_all(self):
        r = self.client.get("/api/stats?range=garbage")
        self.assertEqual(r.status_code, 200)  # 不报 422，fallback all
        self.assertIn("all-time", r.text)


if __name__ == "__main__":
    unittest.main()
