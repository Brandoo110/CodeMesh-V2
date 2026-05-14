"""
多模型工作流编排器（v5 Phase 6.4）。

设计要点（详见 docs/workflow-design-plan.md §5.3）：

- 每步独立 Harness 实例，承载 step.model + step.system_prompt + step.enable_tools
- 步骤间数据流：上一步 output 隐式拼到下一步 user_prompt 前
- SSE 事件协议：run_start → step_start → token* → tool_start/end* → usage →
  step_end → ... → done（或 error / cancelled）
- 中断：cancel(run_id) 设 flag，下一步 boundary 退出（不中断当前 step）
- 工作目录快照（diff-aware）：Phase 6.6 实现；本 phase 占位为空 list

复用：
- harness.run_stream_full：Phase 3 已有的结构化事件流接口
- harness.set_tool_allowlist：Phase 6.4 本次加的白名单 filter 入口
- short_term.set_system：step.system_prompt 覆盖默认 SYSTEM_PROMPT
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from harness import Harness
from web.workflows_store import WorkflowsStore


class WorkflowOrchestrator:
    """
    单进程工作流编排器。

    一个 orchestrator 实例对应 store 单例；多次 run 共享 cancel flags dict。
    所有方法 async；run() 是 async generator（给 SSE 路由 consume）。
    """

    def __init__(self, store: WorkflowsStore, work_dir: Optional[Path] = None):
        self.store = store
        self.work_dir = work_dir or Path.cwd()
        # run_id → True 表示用户请求 cancel
        self._cancel_flags: dict[str, bool] = {}

    def cancel(self, run_id: str) -> None:
        """异步标志：下一个 step boundary 会检查并退出。当前 step 完整跑完。"""
        self._cancel_flags[run_id] = True

    # ─────────────── 主入口 ───────────────

    async def run(
        self,
        workflow_id: str,
        run_id: str,
        *,
        only_step_id: Optional[str] = None,
        seed_input: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """
        执行整个工作流（或单个 step），yield SSE 事件。

        Args:
            workflow_id: 工作流 id
            run_id:      run 实例 id（由路由层先 create_run 再传入）
            only_step_id: 只跑这一步（单步执行场景，Phase 6.8 启用）
            seed_input:   只跑单步时上一步 output 的注入值；整体跑时无效

        Yields:
            dict 事件（type / data 两键）；data 内含 step_id / step_order 等
        """
        steps = await self.store.get_steps(workflow_id)
        if only_step_id is not None:
            steps = [s for s in steps if s["id"] == only_step_id]
            if not steps:
                yield {"type": "error", "data": {"message": f"step {only_step_id} not found"}}
                return

        yield {
            "type": "run_start",
            "data": {"run_id": run_id, "workflow_id": workflow_id, "total_steps": len(steps)},
        }

        prev_output = seed_input or ""
        total_cost = 0.0

        for step in steps:
            # 用户 cancel 检查（在 step boundary 之间）
            if self._cancel_flags.get(run_id):
                await self.store.update_run(run_id, status="cancelled")
                yield {"type": "cancelled", "data": {"run_id": run_id}}
                self._cancel_flags.pop(run_id, None)
                return

            yield {
                "type": "step_start",
                "data": {
                    "step_id": step["id"],
                    "name": step["name"],
                    "model": step["model"],
                    "step_order": step["step_order"],
                },
            }

            # 拼 prompt：隐式继承上一步 output
            user_input = step["user_prompt"] or ""
            if prev_output:
                user_input = (
                    f"上一步输出：\n{prev_output}\n\n{user_input}".strip()
                )
            if not user_input:
                # 完全空 prompt 时给个最小指令，避免 LLM 因为空消息直接拒绝
                user_input = "（请基于上文继续工作）"

            harness = self._make_step_harness(step)

            full_answer = ""
            tool_calls: list[dict] = []
            cost_rmb = 0.0
            model_used = step["model"] or "auto"
            step_start_ts = time.time()
            step_error: Optional[str] = None

            try:
                async for ev in harness.run_stream_full(user_input):
                    etype = ev.get("type")
                    data = {**(ev.get("data") or {}), "step_id": step["id"]}

                    if etype == "token":
                        full_answer += data.get("delta", "")
                    elif etype == "tool_start":
                        tool_calls.append({
                            "name": data.get("name"),
                            "args": data.get("args"),
                            "status": "pending",
                        })
                    elif etype == "tool_end":
                        # FIFO 配对：找最近一个 pending 且 name 匹配
                        name = data.get("name")
                        for tc in reversed(tool_calls):
                            if tc["name"] == name and tc.get("status") == "pending":
                                tc["result"] = data.get("result")
                                tc["ok"] = data.get("ok", True)
                                tc["status"] = "ok" if data.get("ok", True) else "error"
                                break
                    elif etype == "usage":
                        cost_rmb = float(data.get("cost_rmb") or 0.0)
                        if data.get("model"):
                            model_used = data["model"]
                    elif etype == "error":
                        step_error = str(data.get("message"))
                    elif etype == "done":
                        # harness 内部 done 表示该 task 收尾——不向外透传
                        continue

                    # 透传给上层（包括 token / tool_start / tool_end / usage / error）
                    yield {"type": etype, "data": data}

                # 兜底：complex 任务 harness 用 token 一次性塞完整答案
                # （见 _stream_complex 注释：agent loop 内部不是流式）

            except Exception as e:
                step_error = f"{type(e).__name__}: {e}"

            duration_ms = int((time.time() - step_start_ts) * 1000)

            # 持久化 step result
            status = "error" if step_error else "done"
            await self.store.save_step_result(
                run_id,
                step,
                status=status,
                output=full_answer or None,
                error=step_error,
                tool_calls=tool_calls or None,
                file_diffs=None,  # Phase 6.6 填
                model_used=model_used,
                cost_rmb=cost_rmb,
                duration_ms=duration_ms,
            )
            total_cost += cost_rmb

            yield {
                "type": "step_end",
                "data": {
                    "step_id": step["id"],
                    "step_order": step["step_order"],
                    "ok": step_error is None,
                    "error": step_error,
                    "cost_rmb": cost_rmb,
                    "duration_ms": duration_ms,
                    "model_used": model_used,
                },
            }

            if step_error:
                # 整体工作流终止
                await self.store.update_run(
                    run_id, status="error", total_cost=total_cost, error=step_error
                )
                yield {"type": "done", "data": {"ok": False, "error": step_error, "run_id": run_id}}
                return

            prev_output = full_answer

        await self.store.update_run(run_id, status="done", total_cost=total_cost)
        yield {
            "type": "done",
            "data": {
                "ok": True,
                "total_cost": total_cost,
                "run_id": run_id,
            },
        }

    # ─────────────── 临时 harness 构造 ───────────────

    def _make_step_harness(self, step: dict) -> Harness:
        """
        按 step 配置创建一次性 Harness 实例。

        关键点：
        - enable_dreaming=False：避免每步触发 4 阶段巩固循环（耗时且无意义）
        - enable_memory_compression=False：step 是短任务，不需要压缩
        - set_tool_allowlist：核心差异化护城河 #1
        - short_term.set_system：用 step.system_prompt 覆盖 SYSTEM_PROMPT

        模型选择：
        - step.model 非空 → 通过 task 路由层的 model_hint（这里走 short_term system
          注入，不强制；最稳的方式是依赖 router 决策。MVP 暂不做强制）
        """
        h = Harness(
            enable_logging_hooks=False,    # 编排器自己产事件，不重复打 hooks 日志
            enable_memory_compression=False,
            enable_dreaming=False,
            use_rag=False,
        )
        if step.get("system_prompt"):
            h.short_term.set_system(step["system_prompt"])
        h.set_tool_allowlist(step.get("enable_tools") or ["*"])
        return h
