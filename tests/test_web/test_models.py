"""
GET /api/models 单测。

行为约束：
  1. 只返回**已配置**的模型（is_configured=True）。未配 API key 的不出现。
  2. 5 个 provider 候选（deepseek/qwen/doubao/gemini/minimax），实际数量取决于 env。
  3. 每条记录有 id/name/configured/color 字段
  4. color 是 hex 格式
  5. 列表里所有项的 configured 都是 True（过滤逻辑保证）

跑法：
    .venv/bin/python -m unittest -v tests.test_web.test_models
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.server import app


class TestModelsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_only_lists_configured_models(self):
        """所有返回的 model.configured 都应该是 True。"""
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200)
        for m in r.json():
            self.assertTrue(m["configured"])

    def test_ids_subset_of_known_providers(self):
        """返回的 id 必须在 5 个支持的 provider 里。"""
        r = self.client.get("/api/models")
        known = {"deepseek", "qwen", "doubao", "gemini", "minimax"}
        for m in r.json():
            self.assertIn(m["id"], known)

    def test_each_model_has_required_fields(self):
        r = self.client.get("/api/models")
        for m in r.json():
            self.assertIn("id", m)
            self.assertIn("name", m)
            self.assertIn("configured", m)
            self.assertIn("color", m)
            # color 是 hex
            self.assertTrue(m["color"].startswith("#"))
            self.assertEqual(len(m["color"]), 7)
            self.assertIsInstance(m["configured"], bool)

    def test_returns_all_five_when_all_keys_set(self):
        """配齐 5 个 key 时返回 5 条；用 patch 模拟。"""
        fake_env = {
            "DEEPSEEK_API_KEY":  "a" * 30,
            "DASHSCOPE_API_KEY": "a" * 30,
            "VOLC_API_KEY":      "a" * 30,
            "GEMINI_API_KEY":    "a" * 30,
            "MINIMAX_API_KEY":   "a" * 30,
        }
        with patch.dict(os.environ, fake_env, clear=False):
            r = self.client.get("/api/models")
            ids = {m["id"] for m in r.json()}
            self.assertEqual(ids, {"deepseek", "qwen", "doubao", "gemini", "minimax"})

    def test_returns_empty_when_no_keys(self):
        """所有 key 都空时返回空列表（不应 crash）。"""
        empty_env = {
            "DEEPSEEK_API_KEY":  "",
            "DASHSCOPE_API_KEY": "",
            "VOLC_API_KEY":      "",
            "GEMINI_API_KEY":    "",
            "MINIMAX_API_KEY":   "",
        }
        with patch.dict(os.environ, empty_env, clear=False):
            r = self.client.get("/api/models")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), [])


if __name__ == "__main__":
    unittest.main()
