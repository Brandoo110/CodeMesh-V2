"""
本地调用日志单元测试
======================

跑法：
    python -m tests.test_call_log

覆盖：
  - log_call 写入格式正确
  - 多次写入累加
  - read_calls 时间窗口过滤
  - aggregate 按模型聚合，平均延迟正确
  - 损坏行不让整个文件读不出来
"""

import json
import tempfile
import time
from pathlib import Path

from feedback.call_log import log_call, read_calls, aggregate


def _tmp_log() -> Path:
    return Path(tempfile.mkdtemp(prefix="cm-log-")) / "calls.jsonl"


# ────────────────────────── log_call ──────────────────────────


def test_log_call_creates_file_and_appends():
    log = _tmp_log()
    log_call(model="qwen", tokens_in=10, tokens_out=20, cost_rmb=0.001, log_path=log)
    log_call(model="qwen", tokens_in=5, tokens_out=15, cost_rmb=0.0008, log_path=log)
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["model"] == "qwen"
    assert rec["tokens_in"] == 10
    assert "ts" in rec


def test_log_call_optional_fields():
    log = _tmp_log()
    log_call(
        model="deepseek",
        tokens_in=1, tokens_out=2, cost_rmb=0.0,
        latency_ms=123.4, task="hello task",
        log_path=log,
    )
    rec = json.loads(log.read_text().strip())
    assert rec["latency_ms"] == 123.4
    assert rec["task"] == "hello task"


def test_log_call_truncates_long_task():
    log = _tmp_log()
    log_call(
        model="x", tokens_in=0, tokens_out=0, cost_rmb=0.0,
        task="A" * 1000, log_path=log,
    )
    rec = json.loads(log.read_text().strip())
    assert len(rec["task"]) == 200


def test_log_call_silent_on_failure():
    """日志路径不可写时函数应静默返回，不抛异常。"""
    bad_path = Path("/proc/this_does_not_work/calls.jsonl")
    # 不应抛
    log_call(model="x", tokens_in=0, tokens_out=0, cost_rmb=0.0, log_path=bad_path)


# ────────────────────────── read_calls ──────────────────────────


def test_read_calls_returns_empty_when_no_file():
    log = _tmp_log()
    assert read_calls(log_path=log) == []


def test_read_calls_returns_all_when_no_window():
    log = _tmp_log()
    log_call(model="a", tokens_in=1, tokens_out=1, cost_rmb=0.0, log_path=log)
    log_call(model="b", tokens_in=2, tokens_out=2, cost_rmb=0.0, log_path=log)
    out = read_calls(log_path=log)
    assert len(out) == 2


def test_read_calls_filters_by_time_window():
    log = _tmp_log()
    # 写一条假的"很久以前"的记录
    old_ts = time.time() - 30 * 86400  # 30 天前
    with log.open("w") as f:
        f.write(json.dumps({
            "ts": old_ts,
            "model": "old", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.0,
        }) + "\n")
    log_call(model="recent", tokens_in=1, tokens_out=1, cost_rmb=0.0, log_path=log)
    # 只看最近 7 天
    out = read_calls(since_days=7, log_path=log)
    assert len(out) == 1
    assert out[0]["model"] == "recent"


def test_read_calls_skips_corrupt_lines():
    log = _tmp_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        '{"ts": 1, "model": "x", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.0}\n'
        "this is not json\n"
        '{"ts": 2, "model": "y", "tokens_in": 2, "tokens_out": 2, "cost_rmb": 0.0}\n'
    )
    out = read_calls(log_path=log)
    assert len(out) == 2
    assert {r["model"] for r in out} == {"x", "y"}


# ────────────────────────── aggregate ──────────────────────────


def test_aggregate_groups_by_model():
    records = [
        {"ts": 1, "model": "qwen", "tokens_in": 10, "tokens_out": 20, "cost_rmb": 0.001},
        {"ts": 2, "model": "qwen", "tokens_in": 5, "tokens_out": 5, "cost_rmb": 0.0005},
        {"ts": 3, "model": "deepseek", "tokens_in": 100, "tokens_out": 50, "cost_rmb": 0.01},
    ]
    out = aggregate(records)
    assert set(out.keys()) == {"qwen", "deepseek"}
    assert out["qwen"]["calls"] == 2
    assert out["qwen"]["tokens_in"] == 15
    assert out["qwen"]["tokens_out"] == 25
    assert abs(out["qwen"]["cost_rmb"] - 0.0015) < 1e-9
    assert out["deepseek"]["calls"] == 1


def test_aggregate_average_latency():
    records = [
        {"model": "x", "tokens_in": 0, "tokens_out": 0, "cost_rmb": 0.0, "latency_ms": 100},
        {"model": "x", "tokens_in": 0, "tokens_out": 0, "cost_rmb": 0.0, "latency_ms": 300},
        # 一条没 latency_ms
        {"model": "x", "tokens_in": 0, "tokens_out": 0, "cost_rmb": 0.0},
    ]
    out = aggregate(records)
    assert out["x"]["avg_latency_ms"] == 200.0
    assert out["x"]["calls"] == 3


def test_aggregate_no_latency_at_all():
    records = [
        {"model": "x", "tokens_in": 1, "tokens_out": 1, "cost_rmb": 0.0},
    ]
    out = aggregate(records)
    assert out["x"]["avg_latency_ms"] is None


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
    print(f"\n{len(tests) - failed}/{len(tests)} call_log tests passed.")
    if failed:
        raise SystemExit(1)
