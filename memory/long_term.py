"""
长期记忆：跨会话持久化（Harness 记忆层）
=========================================

【为什么要长期记忆】
短期记忆只在这一次对话里有用，进程一关就没了。
但 Agent 往往需要「记得之前帮你做过什么」：
  - 用户的偏好（"我喜欢 tab 缩进"）
  - 项目级别的配置（"测试命令是 pytest -xvs"）
  - 历史任务 outcome（"上次重构已完成，这次别再重复"）

这些要持久化到磁盘。最简单的选择是 SQLite —— 零依赖、零运维、文件即数据库。

【为什么不用 JSON 文件】
JSON 写入时必须全量重写，并发写会丢数据。SQLite 有 WAL、原子提交、并发读写。
而且它是个真正的数据库，以后想加索引、做查询都方便。

【为什么用 aiosqlite 而不是 sqlite3】
aiosqlite 是 sqlite3 的异步包装。因为 CodeMesh 其他部分都是 async（模型调用、
CLI 的并发），保持异步一致可以避免在 asyncio 事件循环里阻塞（阻塞调用会冻住
整个进程的其他任务）。

【数据模型】
最简单的 KV 表：key TEXT PRIMARY KEY, value TEXT。
value 用 JSON 字符串存，读的时候反序列化 —— 这样可以存任意结构。
复杂场景可以扩展成多张表。

【存哪】
~/.codemesh/memory.db
放在 home 目录下的隐藏文件夹，避免污染项目目录。
"""

import json
from pathlib import Path
from typing import Any

import aiosqlite


# 数据库文件路径：~/.codemesh/memory.db
DB_DIR = Path.home() / ".codemesh"
DB_PATH = DB_DIR / "memory.db"


class LongTermMemory:
    """
    基于 SQLite 的简单 KV 存储。

    用法：
        mem = LongTermMemory()
        await mem.init()                      # 首次使用需初始化表
        await mem.save("user_pref", {"tab": 4})
        val = await mem.load("user_pref")     # -> {"tab": 4}
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        # 确保目录存在（第一次运行时会自动建目录）
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        """
        首次启动建表。重复调用无副作用（IF NOT EXISTS）。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def save(self, key: str, value: Any) -> None:
        """
        写入一个键值。value 可以是任何能 JSON 序列化的对象（dict / list / str / int …）。

        INSERT OR REPLACE 的语义：如果 key 已存在就覆盖，不存在就插入。
        比先 SELECT 再决定 INSERT/UPDATE 更高效，也避免并发 race。
        """
        # json.dumps 默认把中文转 \uXXXX，ensure_ascii=False 保留原字符，方便人工看
        serialized = json.dumps(value, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (key, serialized),
            )
            await db.commit()

    async def load(self, key: str) -> Any | None:
        """读取。key 不存在返回 None。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    async def delete(self, key: str) -> None:
        """删除一个键。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM kv WHERE key = ?", (key,))
            await db.commit()

    async def list_all(self) -> dict[str, Any]:
        """
        把整张 kv 表读出来。Harness 在 run() 前会调一次，把内容拼进 system prompt。
        条数大时（>200）应该改成分页，但目前作为demo，直接全量读。
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT key, value FROM kv ORDER BY key") as cursor:
                rows = await cursor.fetchall()
        out: dict[str, Any] = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        return out


# ─────────────── 模块级单例 ───────────────
# 工具 (remember_fact / recall_facts / forget_fact) 和 Harness 共享同一实例，
# 保证模型保存的事实下一轮立刻可见。

_default: "LongTermMemory | None" = None


def get_default_long_term() -> "LongTermMemory":
    global _default
    if _default is None:
        _default = LongTermMemory()
    return _default


def set_default_long_term(mem: "LongTermMemory") -> None:
    """测试 / 自定义 db_path 时显式注入。"""
    global _default
    _default = mem
