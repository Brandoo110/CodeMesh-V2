"""
长期记忆集成测试
==================

跑法：
    python -m tests.test_long_term

覆盖：
  - LongTermMemory.list_all（新方法）
  - 模块级单例 get_default_long_term / set_default_long_term
  - remember_fact / recall_facts / forget_fact 三个工具
  - 工具和 Harness 共享同一份 SQLite（通过单例）
"""

import asyncio
import tempfile
from pathlib import Path

from memory import (
    LongTermMemory,
    get_default_long_term,
    set_default_long_term,
)
from execution.tools import remember_fact, recall_facts, forget_fact


def _run(coro):
    return asyncio.run(coro)


def _new_temp_lt() -> LongTermMemory:
    """每个测试用一个独立 SQLite，避免互相污染。"""
    db = Path(tempfile.mkdtemp(prefix="codemesh-lt-")) / "memory.db"
    return LongTermMemory(db_path=db)


# ────────────────────────── LongTermMemory.list_all ──────────────────────────


def test_list_all_empty():
    lt = _new_temp_lt()
    _run(lt.init())
    assert _run(lt.list_all()) == {}


def test_list_all_returns_inserted_keys():
    lt = _new_temp_lt()
    _run(lt.init())
    _run(lt.save("indent_pref", "4 spaces"))
    _run(lt.save("test_cmd", "uv run pytest"))
    out = _run(lt.list_all())
    assert out == {"indent_pref": "4 spaces", "test_cmd": "uv run pytest"}


def test_list_all_handles_complex_values():
    lt = _new_temp_lt()
    _run(lt.init())
    _run(lt.save("config", {"editor": "nvim", "tabs": 4}))
    out = _run(lt.list_all())
    assert out == {"config": {"editor": "nvim", "tabs": 4}}


def test_list_all_after_delete():
    lt = _new_temp_lt()
    _run(lt.init())
    _run(lt.save("a", "1"))
    _run(lt.save("b", "2"))
    _run(lt.delete("a"))
    out = _run(lt.list_all())
    assert out == {"b": "2"}


# ────────────────────────── 单例切换 ──────────────────────────


def test_singleton_replaceable():
    """set_default_long_term 注入新实例后，get 应返回新实例。"""
    new_lt = _new_temp_lt()
    set_default_long_term(new_lt)
    got = get_default_long_term()
    assert got is new_lt


# ────────────────────────── 工具 ──────────────────────────


def test_tool_remember_then_recall():
    new_lt = _new_temp_lt()
    set_default_long_term(new_lt)
    out = _run(remember_fact("indent_pref", "4 spaces"))
    assert out.startswith("OK:")
    listed = _run(recall_facts())
    assert "indent_pref: 4 spaces" in listed


def test_tool_recall_when_empty():
    new_lt = _new_temp_lt()
    set_default_long_term(new_lt)
    listed = _run(recall_facts())
    assert "no facts" in listed


def test_tool_forget_works_and_is_idempotent():
    new_lt = _new_temp_lt()
    set_default_long_term(new_lt)
    _run(remember_fact("delete_me", "trash"))
    _run(forget_fact("delete_me"))
    listed = _run(recall_facts())
    assert "delete_me" not in listed
    # 再删一次应该不报错
    out = _run(forget_fact("delete_me"))
    assert out.startswith("OK:")


def test_tool_remember_overwrites_existing_key():
    new_lt = _new_temp_lt()
    set_default_long_term(new_lt)
    _run(remember_fact("name", "alice"))
    _run(remember_fact("name", "bob"))
    listed = _run(recall_facts())
    assert "name: bob" in listed
    assert "name: alice" not in listed


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_")
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} long_term tests passed.")
    if failed:
        raise SystemExit(1)
