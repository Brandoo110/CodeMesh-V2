"""
内置模板 seed 单测（v5 Phase 6.7）。

验证：
  - 3 个 Coding 模板都能注入
  - seed 是 idempotent（重复调不重复插入）
  - 模板的 is_template 标记正确
  - 各步骤的 enable_tools / model 字段写入
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from web.workflow_templates import TEMPLATES, seed_templates
from web.workflows_store import WorkflowsStore


def run(coro):
    return asyncio.run(coro)


class TestSeedTemplates(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        self.store = WorkflowsStore(db_path=self.db_path)
        run(self.store.init())

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_seed_creates_all_three_templates(self):
        n = run(seed_templates(self.store))
        self.assertEqual(n, len(TEMPLATES))
        self.assertEqual(n, 3)

        wfs = run(self.store.list_workflows())
        names = {w["name"] for w in wfs if w["is_template"]}
        self.assertEqual(len(names), 3)
        self.assertTrue(any("Aider" in n for n in names))
        self.assertTrue(any("三角审查" in n for n in names))
        self.assertTrue(any("多模型对比" in n for n in names))

    def test_seed_is_idempotent(self):
        n1 = run(seed_templates(self.store))
        n2 = run(seed_templates(self.store))
        self.assertEqual(n1, 3)
        self.assertEqual(n2, 0)
        # 模板总数仍是 3
        wfs = run(self.store.list_workflows())
        tpls = [w for w in wfs if w["is_template"]]
        self.assertEqual(len(tpls), 3)

    def test_templates_have_correct_step_count(self):
        run(seed_templates(self.store))
        wfs = run(self.store.list_workflows())
        by_name = {w["name"]: w for w in wfs}

        # Aider: 2 步
        aider = next(w for w in wfs if "Aider" in w["name"])
        self.assertEqual(aider["step_count"], 2)
        # 三角审查: 3 步
        triangle = next(w for w in wfs if "三角审查" in w["name"])
        self.assertEqual(triangle["step_count"], 3)
        # 多模型对比: 3 步（2 实现 + 1 综合）
        compare = next(w for w in wfs if "多模型对比" in w["name"])
        self.assertEqual(compare["step_count"], 3)

    def test_reviewer_step_is_read_only(self):
        """三角审查模板的 Reviewer 步骤应该只读（护城河 #1 实证）。"""
        run(seed_templates(self.store))
        wfs = run(self.store.list_workflows())
        triangle = next(w for w in wfs if "三角审查" in w["name"])
        steps = run(self.store.get_steps(triangle["id"]))
        reviewer = steps[2]  # 第 3 步
        self.assertIn("Reviewer", reviewer["name"])
        # 不应含 edit_file / write_file
        self.assertNotIn("edit_file", reviewer["enable_tools"])
        self.assertNotIn("write_file", reviewer["enable_tools"])
        self.assertIn("read_file", reviewer["enable_tools"])

    def test_compare_summary_step_has_no_tools(self):
        """多模型对比模板的综合点评步骤应禁用所有工具。"""
        run(seed_templates(self.store))
        wfs = run(self.store.list_workflows())
        compare = next(w for w in wfs if "多模型对比" in w["name"])
        steps = run(self.store.get_steps(compare["id"]))
        # 综合点评是最后一步
        summary = steps[-1]
        self.assertIn("综合", summary["name"])
        self.assertEqual(summary["enable_tools"], [])

    def test_templates_have_three_different_models(self):
        """Aider 流水线两步用不同模型（DeepSeek 架构 + Gemini 编辑）。"""
        run(seed_templates(self.store))
        wfs = run(self.store.list_workflows())
        aider = next(w for w in wfs if "Aider" in w["name"])
        steps = run(self.store.get_steps(aider["id"]))
        self.assertEqual(steps[0]["model"], "deepseek")
        self.assertEqual(steps[1]["model"], "gemini")
        self.assertNotEqual(steps[0]["model"], steps[1]["model"])


if __name__ == "__main__":
    unittest.main()
