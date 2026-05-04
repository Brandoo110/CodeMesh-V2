"""
本地调用日志（Harness 反馈层）
================================

【这模块解决什么】
README §7 承诺过一个 `codemesh stats` 子命令但没实现，因为 stats 强依赖
Langfuse 需要外网账号。这模块改用 **本地 JSONL 文件** 做日志，纯离线、
零依赖、面试现场也能演示。

【数据格式】
每行一个 JSON 对象，append-only（不会改写历史）：

    {"ts": 1730000000.5,
     "model": "deepseek",
     "tokens_in": 312,
     "tokens_out": 87,
     "cost_rmb": 0.0008,
     "latency_ms": 942.3,
     "task": "重构 auth 模块"}

JSONL 而不是 SQLite：
  1. append-only，进程崩溃也不会破坏文件结构
  2. 人肉 cat / grep 立刻能看；wc -l 就是调用次数
  3. 容量大了也方便迁移到 ClickHouse / Loki

【为什么不直接打 Langfuse】
我们已经有 feedback/observer.py 走 Langfuse。这边是补"无 Langfuse 时的最小可用"，
两套并行：本地 always-on，Langfuse 看用户是否配 key。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


# 默认日志路径：~/.codemesh/calls.jsonl
LOG_PATH = Path.home() / ".codemesh" / "calls.jsonl"


def log_call(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_rmb: float,
    latency_ms: Optional[float] = None,
    task: Optional[str] = None,
    log_path: Path = LOG_PATH,
) -> None:
    """追加一条调用记录到日志文件。失败时静默吞掉（不影响主流程）。"""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": time.time(),
            "model": model,
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "cost_rmb": float(cost_rmb),
        }
        if latency_ms is not None:
            record["latency_ms"] = float(latency_ms)
        if task is not None:
            # task 可能是几千字的 prompt；只留前 200 字做识别
            record["task"] = str(task)[:200]
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 日志失败绝对不能拖垮主调用链
        pass


def read_calls(
    *,
    since_days: Optional[float] = None,
    log_path: Path = LOG_PATH,
) -> list[dict[str, Any]]:
    """读所有调用记录。可选只看最近 N 天。损坏行自动跳过。"""
    if not log_path.exists():
        return []
    cutoff = time.time() - since_days * 86400 if since_days is not None else 0.0
    out: list[dict[str, Any]] = []
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts", 0) >= cutoff:
                    out.append(rec)
    except OSError:
        return []
    return out


def aggregate(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    按模型聚合。返回 {model: {calls, tokens_in, tokens_out, cost_rmb, avg_latency_ms}}。
    """
    by_model: dict[str, dict[str, Any]] = {}
    for r in records:
        m = r.get("model", "unknown")
        agg = by_model.setdefault(m, {
            "calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_rmb": 0.0,
            "_lat_sum": 0.0,
            "_lat_n": 0,
        })
        agg["calls"] += 1
        agg["tokens_in"] += int(r.get("tokens_in", 0))
        agg["tokens_out"] += int(r.get("tokens_out", 0))
        agg["cost_rmb"] += float(r.get("cost_rmb", 0.0))
        lat = r.get("latency_ms")
        if lat is not None:
            agg["_lat_sum"] += float(lat)
            agg["_lat_n"] += 1
    # 收尾：算平均，丢掉 _ 前缀的中间字段
    for m, agg in by_model.items():
        agg["avg_latency_ms"] = (
            agg["_lat_sum"] / agg["_lat_n"] if agg["_lat_n"] > 0 else None
        )
        del agg["_lat_sum"]
        del agg["_lat_n"]
    return by_model


__all__ = ["log_call", "read_calls", "aggregate", "LOG_PATH"]
