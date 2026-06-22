"""
Memory Panel API tests.

These tests use temporary storage so they never read or mutate the user's real
~/.codemesh memory files.

Run:
    .venv/bin/python -m unittest -v tests.test_web.test_memory
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.routes import memory as memory_module
from web.server import app
from web.memory_store import MemoryStore, get_memory_store


class TestMemoryEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(
            db_path=self.root / "memory.db",
            auto_memory_dir=self.root / "auto_memory",
            journal_dir=self.root / "journal",
        )
        asyncio.run(self.store.init())
        memory_module._initialized = True
        app.dependency_overrides[get_memory_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        memory_module._initialized = False
        self.tmp.cleanup()

    def _write_auto_memory(self, name: str, type_: str, description: str) -> None:
        self.store.auto_memory_dir.mkdir(parents=True, exist_ok=True)
        (self.store.auto_memory_dir / f"{name}.md").write_text(
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {type_}\n"
            f"---\n\n"
            f"**Why:** useful context\n\n"
            f"**How to apply:** use it next time\n",
            encoding="utf-8",
        )

    def test_summary_counts_empty_memory(self):
        r = self.client.get("/api/memory/summary")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["facts_count"], 0)
        self.assertEqual(r.json()["auto_memory_count"], 0)
        self.assertEqual(r.json()["journal_count"], 0)

    def test_facts_can_be_created_listed_and_deleted(self):
        create = self.client.post(
            "/api/memory/facts",
            json={"key": "reply_language", "value": "中文"},
        )
        self.assertEqual(create.status_code, 200)

        listed = self.client.get("/api/memory/facts")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            listed.json(),
            [{"key": "reply_language", "value": "中文"}],
        )

        deleted = self.client.delete("/api/memory/facts/reply_language")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"deleted": "reply_language"})
        self.assertEqual(self.client.get("/api/memory/facts").json(), [])

    def test_auto_memory_parses_frontmatter_and_filters_by_type(self):
        self._write_auto_memory("user_pref", "user", "User prefers concise Chinese")
        self._write_auto_memory("project_rule", "project", "CodeMesh is done")
        (self.store.auto_memory_dir / "MEMORY.md").write_text(
            "# Memory Index\n\n- [user_pref](user_pref.md) — User prefers concise Chinese\n",
            encoding="utf-8",
        )

        all_rows = self.client.get("/api/memory/auto").json()
        self.assertEqual({r["name"] for r in all_rows}, {"project_rule", "user_pref"})
        self.assertTrue(next(r for r in all_rows if r["name"] == "user_pref")["indexed"])

        users = self.client.get("/api/memory/auto?type=user").json()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["name"], "user_pref")

    def test_journals_are_listed_newest_first(self):
        self.store.journal_dir.mkdir(parents=True, exist_ok=True)
        old = self.store.journal_dir / "old.md"
        new = self.store.journal_dir / "new.md"
        old.write_text("# old\n\nfirst", encoding="utf-8")
        new.write_text("# new\n\nsecond", encoding="utf-8")

        rows = self.client.get("/api/memory/journal").json()
        self.assertEqual([r["name"] for r in rows], ["new", "old"])
        self.assertIn("second", rows[0]["preview"])

    def test_dream_status_reports_gate_inputs(self):
        self._write_auto_memory("a", "user", "A")
        self._write_auto_memory("b", "feedback", "B")

        r = self.client.get("/api/memory/dream/status")

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["memory_entries"], 2)
        self.assertIn("can_dream", data)
        self.assertIn("reason", data)


if __name__ == "__main__":
    unittest.main()
