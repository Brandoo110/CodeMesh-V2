"""
GET /api/models 单测。

测点：
  1. 返回 4 个模型（deepseek/qwen/doubao/gemini）
  2. 每个 model 有 id/name/configured/color 字段
  3. color 是 hex 格式
  4. configured 是 bool

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_models
"""
import unittest

from fastapi.testclient import TestClient

from web.server import app


class TestModelsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_lists_four_supported_models(self):
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data), 4)
        ids = {m["id"] for m in data}
        self.assertEqual(ids, {"deepseek", "qwen", "doubao", "gemini"})

    def test_each_model_has_required_fields(self):
        r = self.client.get("/api/models")
        for m in r.json():
            self.assertIn("id", m)
            self.assertIn("name", m)
            self.assertIn("configured", m)
            self.assertIn("color", m)

    def test_color_is_hex_and_configured_is_bool(self):
        r = self.client.get("/api/models")
        for m in r.json():
            self.assertTrue(m["color"].startswith("#"))
            self.assertEqual(len(m["color"]), 7)
            self.assertIsInstance(m["configured"], bool)


if __name__ == "__main__":
    unittest.main()
