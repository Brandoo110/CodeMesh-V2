"""
Workflows / Steps / Runs / StepResults 持久化（v5 Phase 6.1）。

设计沿用 sessions_store.py：aiosqlite + ~/.codemesh/ 隐藏目录 + 模块级 lazy 单例。
独立 workflows.db，与 web_sessions.db / memory.db 分离——工作流与对话是不同领域，
分库便于备份/重置/迁移。

四张表：
  workflows               工作流模板（含 is_template 字段，1 = 内置不可删）
  workflow_steps          步骤定义（含 enable_tools JSON 数组）
  workflow_runs           一次执行实例
  workflow_step_results   单步结果（含 tool_calls / file_diffs JSON）

详见 docs/workflow-design-plan.md §4。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite


DB_DIR = Path.home() / ".codemesh"
DB_PATH = DB_DIR / "workflows.db"


def _now_iso() -> str:
    """ISO datetime UTC，给 SQLite TEXT 列存。"""
    return datetime.now(timezone.utc).isoformat()


def _dumps_tools(allowlist: list[str]) -> str:
    """工具白名单序列化。None 或空当作 ['*'] 即全开（向后兼容）。"""
    if not allowlist:
        return json.dumps(["*"])
    return json.dumps(allowlist, ensure_ascii=False)


def _loads_tools(raw: Optional[str]) -> list[str]:
    if not raw:
        return ["*"]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return ["*"]


class WorkflowsStore:
    """
    异步 SQLite CRUD。每个方法一次性 connect+commit，简化错误处理。

    设计取舍（详见 design plan §4.2）：
    - enable_tools / tool_calls / file_diffs 都存 TEXT JSON
      —— SQLite 无原生数组，JSON 简单灵活
    - step_order 在 step_results 表冗余存
      —— 避免每次查询都 JOIN steps 表
    - 手动 ON DELETE CASCADE
      —— SQLite 默认不启用 FK，与 sessions_store.py 风格一致
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        """建表 + 索引（IF NOT EXISTS 幂等）。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    description  TEXT,
                    is_template  INTEGER DEFAULT 0,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id              TEXT PRIMARY KEY,
                    workflow_id     TEXT NOT NULL,
                    step_order      INTEGER NOT NULL,
                    name            TEXT NOT NULL,
                    model           TEXT,
                    system_prompt   TEXT,
                    user_prompt     TEXT,
                    enable_tools    TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id              TEXT PRIMARY KEY,
                    workflow_id     TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    started_at      TEXT NOT NULL,
                    completed_at    TEXT,
                    total_cost_rmb  REAL DEFAULT 0,
                    error           TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_step_results (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       TEXT NOT NULL,
                    step_id      TEXT NOT NULL,
                    step_order   INTEGER NOT NULL,
                    status       TEXT NOT NULL,
                    output       TEXT,
                    error        TEXT,
                    tool_calls   TEXT,
                    file_diffs   TEXT,
                    model_used   TEXT,
                    cost_rmb     REAL,
                    duration_ms  INTEGER,
                    started_at   TEXT,
                    completed_at TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_wid ON workflow_steps(workflow_id, step_order)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_wid ON workflow_runs(workflow_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_step_results_run "
                "ON workflow_step_results(run_id, step_order)"
            )
            await db.commit()

    # ─────────────── Workflows CRUD ───────────────

    async def create_workflow(
        self, name: str, description: str = "", *, is_template: bool = False
    ) -> dict[str, Any]:
        """新建工作流，返回完整 metadata。"""
        wid = str(uuid.uuid4())
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO workflows (id, name, description, is_template, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (wid, name, description, 1 if is_template else 0, now, now),
            )
            await db.commit()
        return {
            "id": wid,
            "name": name,
            "description": description,
            "is_template": is_template,
            "created_at": now,
            "updated_at": now,
            "step_count": 0,
        }

    async def list_workflows(self) -> list[dict[str, Any]]:
        """全部工作流（含模板），按 updated_at desc 排序，附带 step_count。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT w.id, w.name, w.description, w.is_template,
                       w.created_at, w.updated_at,
                       (SELECT COUNT(*) FROM workflow_steps s WHERE s.workflow_id = w.id) AS step_count
                FROM workflows w
                ORDER BY w.is_template DESC, w.updated_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "is_template": bool(r["is_template"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "step_count": r["step_count"],
            }
            for r in rows
        ]

    async def get_workflow(self, wid: str) -> Optional[dict[str, Any]]:
        """单个工作流 metadata；不存在返回 None。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, name, description, is_template, created_at, updated_at
                FROM workflows WHERE id = ?
                """,
                (wid,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            count_cur = await db.execute(
                "SELECT COUNT(*) FROM workflow_steps WHERE workflow_id = ?",
                (wid,),
            )
            count = (await count_cur.fetchone())[0]
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "is_template": bool(row["is_template"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "step_count": count,
        }

    async def update_workflow(
        self, wid: str, *, name: Optional[str] = None, description: Optional[str] = None
    ) -> None:
        """更新元数据 + 永远刷新 updated_at。"""
        fields = ["updated_at = ?"]
        values: list[Any] = [_now_iso()]
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        values.append(wid)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE workflows SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )
            await db.commit()

    async def delete_workflow(self, wid: str) -> bool:
        """级联删 workflow + steps + runs + step_results。"""
        async with aiosqlite.connect(self.db_path) as db:
            # 先查 run ids 准备级联
            run_cur = await db.execute(
                "SELECT id FROM workflow_runs WHERE workflow_id = ?", (wid,)
            )
            run_ids = [r[0] for r in await run_cur.fetchall()]
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                await db.execute(
                    f"DELETE FROM workflow_step_results WHERE run_id IN ({placeholders})",
                    tuple(run_ids),
                )
            await db.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (wid,))
            await db.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (wid,))
            cursor = await db.execute("DELETE FROM workflows WHERE id = ?", (wid,))
            await db.commit()
            return cursor.rowcount > 0

    async def _touch_workflow(self, db, wid: str) -> None:
        """内部辅助：刷新 workflow.updated_at（在 add/update/delete step 时调用）。"""
        await db.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            (_now_iso(), wid),
        )

    # ─────────────── Steps CRUD ───────────────

    async def add_step(
        self,
        wid: str,
        *,
        name: str,
        model: Optional[str] = None,
        system_prompt: str = "",
        user_prompt: str = "",
        enable_tools: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """末尾追加一个 step，返回完整 step dict。"""
        sid = str(uuid.uuid4())
        async with aiosqlite.connect(self.db_path) as db:
            # 取当前最大 step_order
            cur = await db.execute(
                "SELECT COALESCE(MAX(step_order), 0) FROM workflow_steps WHERE workflow_id = ?",
                (wid,),
            )
            max_order = (await cur.fetchone())[0]
            next_order = max_order + 1
            tools_json = _dumps_tools(enable_tools or ["*"])
            await db.execute(
                """
                INSERT INTO workflow_steps
                    (id, workflow_id, step_order, name, model, system_prompt, user_prompt, enable_tools)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, wid, next_order, name, model, system_prompt, user_prompt, tools_json),
            )
            await self._touch_workflow(db, wid)
            await db.commit()
        return {
            "id": sid,
            "workflow_id": wid,
            "step_order": next_order,
            "name": name,
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "enable_tools": enable_tools or ["*"],
        }

    async def get_steps(self, wid: str) -> list[dict[str, Any]]:
        """workflow 的全部 steps 按 step_order asc。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, workflow_id, step_order, name, model,
                       system_prompt, user_prompt, enable_tools
                FROM workflow_steps
                WHERE workflow_id = ?
                ORDER BY step_order ASC
                """,
                (wid,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "workflow_id": r["workflow_id"],
                "step_order": r["step_order"],
                "name": r["name"],
                "model": r["model"],
                "system_prompt": r["system_prompt"] or "",
                "user_prompt": r["user_prompt"] or "",
                "enable_tools": _loads_tools(r["enable_tools"]),
            }
            for r in rows
        ]

    async def get_step(self, sid: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, workflow_id, step_order, name, model,
                       system_prompt, user_prompt, enable_tools
                FROM workflow_steps WHERE id = ?
                """,
                (sid,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "step_order": row["step_order"],
            "name": row["name"],
            "model": row["model"],
            "system_prompt": row["system_prompt"] or "",
            "user_prompt": row["user_prompt"] or "",
            "enable_tools": _loads_tools(row["enable_tools"]),
        }

    async def update_step(
        self,
        sid: str,
        *,
        name: Optional[str] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        enable_tools: Optional[list[str]] = None,
    ) -> None:
        """部分字段更新；同时刷新 workflow.updated_at。"""
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if model is not None:
            fields.append("model = ?")
            values.append(model)
        if system_prompt is not None:
            fields.append("system_prompt = ?")
            values.append(system_prompt)
        if user_prompt is not None:
            fields.append("user_prompt = ?")
            values.append(user_prompt)
        if enable_tools is not None:
            fields.append("enable_tools = ?")
            values.append(_dumps_tools(enable_tools))
        if not fields:
            return
        values.append(sid)
        async with aiosqlite.connect(self.db_path) as db:
            # 先查 workflow_id 用于 touch
            cur = await db.execute(
                "SELECT workflow_id FROM workflow_steps WHERE id = ?", (sid,)
            )
            row = await cur.fetchone()
            if not row:
                return
            wid = row[0]
            await db.execute(
                f"UPDATE workflow_steps SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )
            await self._touch_workflow(db, wid)
            await db.commit()

    async def delete_step(self, sid: str) -> bool:
        """删除 step + 把后续 step_order 补齐（避免空洞）。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT workflow_id, step_order FROM workflow_steps WHERE id = ?", (sid,)
            )
            row = await cur.fetchone()
            if not row:
                return False
            wid, order = row
            await db.execute("DELETE FROM workflow_steps WHERE id = ?", (sid,))
            # 后续 step_order 减 1
            await db.execute(
                """
                UPDATE workflow_steps
                SET step_order = step_order - 1
                WHERE workflow_id = ? AND step_order > ?
                """,
                (wid, order),
            )
            await self._touch_workflow(db, wid)
            await db.commit()
            return True

    async def reorder_steps(self, wid: str, step_ids: list[str]) -> None:
        """按给定 step_ids 顺序重新分配 step_order（1-based）。"""
        async with aiosqlite.connect(self.db_path) as db:
            for idx, sid in enumerate(step_ids, start=1):
                await db.execute(
                    "UPDATE workflow_steps SET step_order = ? WHERE id = ? AND workflow_id = ?",
                    (idx, sid, wid),
                )
            await self._touch_workflow(db, wid)
            await db.commit()

    # ─────────────── Runs CRUD ───────────────

    async def create_run(self, wid: str) -> dict[str, Any]:
        """新建 run，默认 status=running。"""
        run_id = str(uuid.uuid4())
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO workflow_runs (id, workflow_id, status, started_at)
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, wid, now),
            )
            await db.commit()
        return {
            "id": run_id,
            "workflow_id": wid,
            "status": "running",
            "started_at": now,
            "completed_at": None,
            "total_cost_rmb": 0.0,
            "error": None,
        }

    async def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        total_cost: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """状态推进：status 到终态（done/error/cancelled）时自动写 completed_at。"""
        fields: list[str] = []
        values: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
            if status in ("done", "error", "cancelled"):
                fields.append("completed_at = ?")
                values.append(_now_iso())
        if total_cost is not None:
            fields.append("total_cost_rmb = ?")
            values.append(total_cost)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if not fields:
            return
        values.append(run_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE workflow_runs SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )
            await db.commit()

    async def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, workflow_id, status, started_at, completed_at, total_cost_rmb, error
                FROM workflow_runs WHERE id = ?
                """,
                (run_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "total_cost_rmb": row["total_cost_rmb"] or 0.0,
            "error": row["error"],
        }

    async def list_runs(self, wid: str, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, workflow_id, status, started_at, completed_at, total_cost_rmb, error
                FROM workflow_runs WHERE workflow_id = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (wid, limit),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "workflow_id": r["workflow_id"],
                "status": r["status"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "total_cost_rmb": r["total_cost_rmb"] or 0.0,
                "error": r["error"],
            }
            for r in rows
        ]

    # ─────────────── Step Results ───────────────

    async def save_step_result(
        self,
        run_id: str,
        step: dict[str, Any],
        *,
        status: str,
        output: Optional[str] = None,
        error: Optional[str] = None,
        tool_calls: Optional[list[dict]] = None,
        file_diffs: Optional[list[dict]] = None,
        model_used: Optional[str] = None,
        cost_rmb: Optional[float] = None,
        duration_ms: Optional[int] = None,
        started_at: Optional[str] = None,
    ) -> int:
        """
        持久化一步的执行结果。

        step 必须含 id 和 step_order（编排器传完整 dict 进来即可）。
        tool_calls / file_diffs 自动 json.dumps。
        """
        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        diff_json = json.dumps(file_diffs, ensure_ascii=False) if file_diffs else None
        now = _now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO workflow_step_results
                    (run_id, step_id, step_order, status, output, error,
                     tool_calls, file_diffs, model_used, cost_rmb, duration_ms,
                     started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, step["id"], step["step_order"], status, output, error,
                    tc_json, diff_json, model_used, cost_rmb, duration_ms,
                    started_at or now, now,
                ),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_step_results(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, run_id, step_id, step_order, status, output, error,
                       tool_calls, file_diffs, model_used, cost_rmb, duration_ms,
                       started_at, completed_at
                FROM workflow_step_results
                WHERE run_id = ?
                ORDER BY step_order ASC
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "step_id": r["step_id"],
                "step_order": r["step_order"],
                "status": r["status"],
                "output": r["output"],
                "error": r["error"],
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else None,
                "file_diffs": json.loads(r["file_diffs"]) if r["file_diffs"] else None,
                "model_used": r["model_used"],
                "cost_rmb": r["cost_rmb"],
                "duration_ms": r["duration_ms"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
            }
            for r in rows
        ]


# 模块级单例（lazy init）
_store: Optional[WorkflowsStore] = None


def get_workflows_store() -> WorkflowsStore:
    """FastAPI Depends 入口。"""
    global _store
    if _store is None:
        _store = WorkflowsStore()
    return _store
