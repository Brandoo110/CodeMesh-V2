"""
多模型工作流编排器（v5 Phase 6.4）。

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
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from harness import Harness
from web.workflows_store import WorkflowsStore


# diff snapshot 配置：跳过依赖目录和大文件，限制单步 diff payload。
_SNAPSHOT_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".codemesh",  # 跳过工作流自己的 DB 目录
}
_SNAPSHOT_MAX_FILE_BYTES = 100_000  # 跳过大文件（图片 / 数据）
_DIFF_MAX_FILES = 30                # 单步 diff 最多记 30 个文件
_DIFF_MAX_BYTES = 200_000           # file_diffs JSON 总上限 ~200KB

_FINAL_REPLY_SYSTEM = (
    "你负责在 coding workflow 结束后，替用户整理最终回复。"
    "请用自然、简洁的中文说明已经完成了什么、产物在哪里、用户怎么验证。"
    "不要逐字复述 Reviewer 或工具日志，不要输出 <think>、分析过程或内部提示。"
    "如果 Reviewer 提到风险，只保留对用户有用的结论。"
)
_REVIEW_DECISION_SYSTEM = (
    "你是 coding workflow 的 Review Decision Controller。"
    "你的任务是根据 workflow 上下文、Coder 产物、Reviewer 输出和文件变更，"
    "判断当前任务是否已经达到用户要求。"
    "如果 Reviewer 明确或隐含认为交付物未完成、存在阻塞问题、缺少必需模块、"
    "验证未通过或需要补充后才能交付，返回 needs_rework。"
    "如果只是非阻塞建议、风格建议或后续优化，返回 done。"
    "必须只输出 JSON，不要 Markdown，不要 <think>。"
)
_WRITE_TOOLS = {"edit_file", "write_file", "delete_file"}


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
        start_step_id: Optional[str] = None,
        seed_input: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """
        执行整个工作流（或单个 step），yield SSE 事件。

        Args:
            workflow_id: 工作流 id
            run_id:      run 实例 id（由路由层先 create_run 再传入）
            only_step_id: 只跑这一步（单步执行场景，Phase 6.8 启用）
            start_step_id: 从这个 step 开始顺序跑到末尾（用户继续修改场景）
            seed_input:   注入到首个待执行 step 的上下文

        Yields:
            dict 事件（type / data 两键）；data 内含 step_id / step_order 等
        """
        steps = await self.store.get_steps(workflow_id)
        if only_step_id is not None:
            steps = [s for s in steps if s["id"] == only_step_id]
            if not steps:
                yield {"type": "error", "data": {"message": f"step {only_step_id} not found"}}
                return
        elif start_step_id is not None:
            start_idx = next(
                (idx for idx, step in enumerate(steps) if step["id"] == start_step_id),
                None,
            )
            if start_idx is None:
                yield {"type": "error", "data": {"message": f"step {start_step_id} not found"}}
                return
            steps = steps[start_idx:]

        yield {
            "type": "run_start",
            "data": {"run_id": run_id, "workflow_id": workflow_id, "total_steps": len(steps)},
        }

        prev_output = seed_input or ""
        total_cost = 0.0
        step_summaries: list[dict[str, Any]] = []
        step_idx = 0
        rework_used = False

        while step_idx < len(steps):
            step = steps[step_idx]
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

            # diff snapshot before（Phase 6.6 护城河 #3）
            before_snapshot = self._snapshot_dir()

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
                        cost_rmb += float(data.get("cost_rmb") or 0.0)
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

            # diff snapshot after + 计算 diff（Phase 6.6 护城河 #3）
            file_diffs: Optional[list[dict]] = None
            try:
                after_snapshot = self._snapshot_dir()
                file_diffs = self._compute_diffs(before_snapshot, after_snapshot)
                # diff 也作为 SSE 事件吐给前端（实时展示）
                if file_diffs:
                    yield {
                        "type": "diff",
                        "data": {"step_id": step["id"], "diffs": file_diffs},
                    }
            except Exception as snap_err:
                # diff 失败不影响主流程（锦上添花层）
                print(f"[orchestrator] diff snapshot failed: {snap_err}")

            # 持久化 step result
            status = "error" if step_error else "done"
            await self.store.save_step_result(
                run_id,
                step,
                status=status,
                output=full_answer or None,
                error=step_error,
                tool_calls=tool_calls or None,
                file_diffs=file_diffs,
                model_used=model_used,
                cost_rmb=cost_rmb,
                duration_ms=duration_ms,
            )
            total_cost += cost_rmb
            step_summaries.append({
                "name": step["name"],
                "status": status,
                "output": full_answer,
                "error": step_error,
                "file_diffs": file_diffs or [],
                "model_used": model_used,
                "cost_rmb": cost_rmb,
            })

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

            if only_step_id is None and self._is_reviewer_step(step):
                rework_step_idx = self._find_previous_writable_step_idx(
                    steps, step_idx
                )
                decision, decision_cost, decision_model = await self._decide_rework_with_model(
                    steps=steps,
                    step_summaries=step_summaries,
                    reviewer_step=step,
                    reviewer_output=full_answer,
                    target_step=steps[rework_step_idx] if rework_step_idx is not None else None,
                )
                total_cost += decision_cost
                target_step = (
                    steps[rework_step_idx]
                    if rework_step_idx is not None
                    and decision["status"] == "needs_rework"
                    else None
                )
                yield {
                    "type": "review_decision",
                    "data": {
                        "reviewer_step_id": step["id"],
                        "reviewer_name": step["name"],
                        "status": decision["status"],
                        "target_step_id": target_step["id"] if target_step else None,
                        "target_name": target_step["name"] if target_step else None,
                        "reason": decision["reason"],
                        "rework_prompt": decision["rework_prompt"],
                        "cost_rmb": decision_cost,
                        "model": decision_model,
                    },
                }

                if (
                    not rework_used
                    and target_step is not None
                    and decision["status"] == "needs_rework"
                ):
                    rework_used = True
                    prev_output = self._build_rework_prompt(decision, full_answer)
                    yield {
                        "type": "rework_requested",
                        "data": {
                            "reviewer_step_id": step["id"],
                            "reviewer_name": step["name"],
                            "target_step_id": steps[rework_step_idx]["id"],
                            "target_name": steps[rework_step_idx]["name"],
                            "reason": decision["reason"],
                        },
                    }
                    step_idx = rework_step_idx
                    continue

            step_idx += 1

        final_reply, final_cost, final_model = await self._generate_final_reply(
            steps, step_summaries
        )
        if final_reply:
            total_cost += final_cost
            yield {
                "type": "final_start",
                "data": {"model": final_model},
            }
            yield {
                "type": "final_end",
                "data": {
                    "reply": final_reply,
                    "cost_rmb": final_cost,
                    "model": final_model,
                },
            }

        await self.store.update_run(
            run_id,
            status="done",
            total_cost=total_cost,
            final_reply=final_reply or None,
        )
        yield {
            "type": "done",
            "data": {
                "ok": True,
                "total_cost": total_cost,
                "run_id": run_id,
                "final_reply": final_reply,
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
        # 区分 None（未配，默认全开）和 []（明确禁用）——避免 truthy 陷阱
        et = step.get("enable_tools")
        h.set_tool_allowlist(["*"] if et is None else et)
        # v5 核心卖点：每步用配置的模型，绕开 router 自动决策
        if step.get("model"):
            h.set_preferred_model(step["model"])
        return h

    async def _generate_final_reply(
        self,
        steps: list[dict],
        step_summaries: list[dict[str, Any]],
    ) -> tuple[str, float, str]:
        """
        用第一步模型整理面向用户的最终回复。

        这不是新的业务 step，不允许调工具，也不写入 step_results；它只是把
        Planner/Coder/Reviewer 的输出整理成用户最终能读的一段话。
        """
        if not steps:
            return "", 0.0, "auto"

        first_model = steps[0].get("model") or "auto"
        prompt = self._build_final_reply_prompt(step_summaries)
        summary_step = {
            "model": first_model,
            "system_prompt": _FINAL_REPLY_SYSTEM,
            "enable_tools": [],
        }
        harness = self._make_step_harness(summary_step)

        reply_parts: list[str] = []
        cost_rmb = 0.0
        model_used = first_model
        try:
            async for ev in harness.run_stream_full(prompt):
                etype = ev.get("type")
                data = ev.get("data") or {}
                if etype == "token":
                    reply_parts.append(str(data.get("delta") or ""))
                elif etype == "usage":
                    cost_rmb += float(data.get("cost_rmb") or 0.0)
                    if data.get("model"):
                        model_used = str(data["model"])
                elif etype == "error":
                    return (
                        f"工作流已经完成，但最终回复生成失败：{data.get('message')}",
                        cost_rmb,
                        model_used,
                    )
        except Exception as e:
            return (
                f"工作流已经完成，但最终回复生成失败：{type(e).__name__}: {e}",
                cost_rmb,
                model_used,
            )

        return _sanitize_final_reply("".join(reply_parts)), cost_rmb, model_used

    def _build_final_reply_prompt(self, step_summaries: list[dict[str, Any]]) -> str:
        parts = [
            "请根据下面的 workflow 执行结果，给用户写最终回复。",
            "要求：",
            "- 用中文自然表达，像 coding agent 完成任务后的回复；",
            "- 不要直接复制 Reviewer 原文；",
            "- 不要输出 <think> 或内部推理；",
            "- 如果有产物文件，明确告诉用户路径；",
            "- 如果有验证方式，说明用户下一步怎么验证；",
            "- 控制在 120-220 字。",
            "",
            "执行结果：",
        ]
        for idx, item in enumerate(step_summaries, start=1):
            diffs = item.get("file_diffs") or []
            diff_paths = ", ".join(
                d.get("path", "") for d in diffs if d.get("path")
            )
            output = _truncate(str(item.get("output") or ""), 1800)
            error = item.get("error")
            parts.extend([
                f"\nStep {idx}: {item.get('name')}",
                f"状态: {item.get('status')}",
                f"模型: {item.get('model_used')}",
                f"成本: ¥{float(item.get('cost_rmb') or 0.0):.4f}",
            ])
            if diff_paths:
                parts.append(f"文件变更: {diff_paths}")
            if error:
                parts.append(f"错误: {error}")
            if output:
                parts.append(f"输出摘要来源:\n{output}")
        return "\n".join(parts)

    async def _decide_rework_with_model(
        self,
        *,
        steps: list[dict],
        step_summaries: list[dict[str, Any]],
        reviewer_step: dict,
        reviewer_output: str,
        target_step: Optional[dict],
    ) -> tuple[dict[str, str], float, str]:
        """
        用模型做 Review → Rework 决策。

        编排器不再通过关键词猜 Reviewer 意图，而是让一个禁工具的隐藏
        decision pass 读取当前任务状态并返回结构化 JSON。
        """
        model = reviewer_step.get("model") or (steps[0].get("model") if steps else "auto")
        harness = self._make_step_harness({
            "model": model,
            "system_prompt": _REVIEW_DECISION_SYSTEM,
            "enable_tools": [],
        })
        prompt = self._build_review_decision_prompt(
            step_summaries=step_summaries,
            reviewer_step=reviewer_step,
            reviewer_output=reviewer_output,
            target_step=target_step,
        )

        parts: list[str] = []
        cost_rmb = 0.0
        model_used = str(model or "auto")
        try:
            async for ev in harness.run_stream_full(prompt):
                etype = ev.get("type")
                data = ev.get("data") or {}
                if etype == "token":
                    parts.append(str(data.get("delta") or ""))
                elif etype == "usage":
                    cost_rmb += float(data.get("cost_rmb") or 0.0)
                    if data.get("model"):
                        model_used = str(data["model"])
                elif etype == "error":
                    return _done_review_decision(
                        f"review decision failed: {data.get('message')}"
                    ), cost_rmb, model_used
        except Exception as e:
            return _done_review_decision(
                f"review decision failed: {type(e).__name__}: {e}"
            ), cost_rmb, model_used

        return _parse_review_decision("".join(parts)), cost_rmb, model_used

    def _build_review_decision_prompt(
        self,
        *,
        step_summaries: list[dict[str, Any]],
        reviewer_step: dict,
        reviewer_output: str,
        target_step: Optional[dict],
    ) -> str:
        parts = [
            "请判断当前 workflow 是否需要自动返工。",
            "输出必须是 JSON，格式如下：",
            '{"status":"done|needs_rework","target_step":"none|previous_writable",'
            '"reason":"一句话原因","rework_prompt":"如果需要返工，写给 Coder 的具体修改指令"}',
            "",
            "判定规则：",
            "- 只有必需功能缺失、验收未达标、Reviewer 认为需要补充后才能交付时，status 才是 needs_rework；",
            "- 如果只是建议优化、可选增强、风格意见，status 是 done；",
            "- rework_prompt 必须可直接交给目标 step 执行；",
            "- 不要因为 Reviewer 语气委婉就忽略实际未完成状态。",
            "",
        ]
        if target_step:
            parts.extend([
                "可返工目标 step：",
                f"- {target_step.get('name')} (id={target_step.get('id')})",
                "",
            ])
        parts.append("已完成步骤摘要：")
        for idx, item in enumerate(step_summaries, start=1):
            diffs = item.get("file_diffs") or []
            diff_paths = ", ".join(
                d.get("path", "") for d in diffs if d.get("path")
            )
            parts.extend([
                f"\nStep {idx}: {item.get('name')}",
                f"状态: {item.get('status')}",
            ])
            if diff_paths:
                parts.append(f"文件变更: {diff_paths}")
            output = _truncate(str(item.get("output") or ""), 1200)
            if output:
                parts.append(f"输出:\n{output}")
        parts.extend([
            "",
            f"当前 Reviewer step: {reviewer_step.get('name')}",
            "Reviewer 输出全文：",
            _truncate(reviewer_output, 2400),
        ])
        return "\n".join(parts)

    def _is_reviewer_step(self, step: dict) -> bool:
        name = str(step.get("name") or "").lower()
        return "reviewer" in name or "review" in name or "审查" in name

    def _find_previous_writable_step_idx(
        self,
        steps: list[dict],
        reviewer_idx: int,
    ) -> Optional[int]:
        for idx in range(reviewer_idx - 1, -1, -1):
            if self._step_can_write(steps[idx]):
                return idx
        return None

    def find_continue_start_step_id(
        self,
        steps: list[dict],
        explicit_step_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        用户追加修改时默认从最后一个可写 step 继续。

        三角审查模板里这通常是 Coder；后续 Reviewer 仍会继续执行，形成
        “用户要求 → Coder 改 → Reviewer 复查”的闭环。
        """
        if explicit_step_id:
            return explicit_step_id if any(s["id"] == explicit_step_id for s in steps) else None
        for step in reversed(steps):
            if self._step_can_write(step):
                return step["id"]
        return steps[0]["id"] if steps else None

    def _step_can_write(self, step: dict) -> bool:
        allowlist = step.get("enable_tools")
        tools = set(allowlist or [])
        return "*" in tools or bool(tools.intersection(_WRITE_TOOLS))

    def _build_rework_prompt(self, decision: dict[str, str], reviewer_output: str) -> str:
        prompt = decision.get("rework_prompt") or decision.get("reason") or reviewer_output
        return (
            "Review Decision 判定当前任务需要返工。\n"
            "请基于当前文件继续修复，只处理下面指出的具体问题，不要重写无关内容。"
            "修完后简短说明修改点和验证方式。\n\n"
            f"返工原因：\n{decision.get('reason') or '未提供'}\n\n"
            f"返工指令：\n{prompt}\n\n"
            f"Reviewer 输出：\n{reviewer_output}"
        )

    # ─────────────── Diff snapshot（Phase 6.6 护城河 #3） ───────────────

    def _snapshot_dir(self) -> dict[str, tuple[str, str]]:
        """
        遍历 work_dir，返回 {rel_path: (sha256, content)} 映射。

        性能保护（详见 design plan §8.3）：
        - skip 目录：_SNAPSHOT_SKIP_DIRS
        - skip 大文件：> _SNAPSHOT_MAX_FILE_BYTES
        - 二进制文件用 errors="ignore" 兜底（read_text 不会抛）
        - 任何单文件错误 → 跳过（snapshot 不能阻塞主流程）
        """
        snap: dict[str, tuple[str, str]] = {}
        try:
            paths = list(self.work_dir.rglob("*"))
        except OSError:
            return snap
        for path in paths:
            try:
                if not path.is_file():
                    continue
                if any(p in path.parts for p in _SNAPSHOT_SKIP_DIRS):
                    continue
                stat = path.stat()
                if stat.st_size > _SNAPSHOT_MAX_FILE_BYTES:
                    continue
                content = path.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
            rel = str(path.relative_to(self.work_dir))
            snap[rel] = (digest, content)
        return snap

    def _compute_diffs(
        self,
        before: dict[str, tuple[str, str]],
        after: dict[str, tuple[str, str]],
    ) -> list[dict]:
        """对比 before / after，返回变化文件列表（含完整 before/after 内容）。"""
        diffs: list[dict] = []
        total_bytes = 0
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            b_digest, b_content = before.get(path, ("", ""))
            a_digest, a_content = after.get(path, ("", ""))
            if b_digest == a_digest:
                continue
            kind = (
                "modified" if (path in before and path in after)
                else ("created" if path in after else "deleted")
            )
            entry = {
                "path": path,
                "before": b_content,
                "after": a_content,
                "kind": kind,
            }
            # 单条 diff 大小
            entry_size = len(b_content) + len(a_content)
            if total_bytes + entry_size > _DIFF_MAX_BYTES:
                # 超阈值 → 截断 content，保留 path 让 UI 显示"变更但不展开"
                entry["before"] = b_content[:500] + ("\n…[truncated]" if len(b_content) > 500 else "")
                entry["after"] = a_content[:500] + ("\n…[truncated]" if len(a_content) > 500 else "")
                entry["truncated"] = True
            total_bytes += entry_size
            diffs.append(entry)
            if len(diffs) >= _DIFF_MAX_FILES:
                break
        return diffs


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _parse_review_decision(text: str) -> dict[str, str]:
    cleaned = _sanitize_final_reply(text)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    raw = match.group(0) if match else cleaned
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _done_review_decision("review decision JSON parse failed")

    status = str(data.get("status") or "done").strip().lower()
    if status in {"needs_rework", "rework", "needs-work", "needs work"}:
        status = "needs_rework"
    else:
        status = "done"

    target_step = str(data.get("target_step") or "none").strip()
    if status != "needs_rework":
        target_step = "none"

    reason = str(data.get("reason") or "").strip()
    rework_prompt = str(data.get("rework_prompt") or "").strip()
    if status == "needs_rework" and not rework_prompt:
        rework_prompt = reason
    if not reason:
        reason = "review decision did not provide a reason"

    return {
        "status": status,
        "target_step": target_step,
        "reason": _truncate(reason, 500).strip(),
        "rework_prompt": _truncate(rework_prompt, 1200).strip(),
    }


def _done_review_decision(reason: str) -> dict[str, str]:
    return {
        "status": "done",
        "target_step": "none",
        "reason": reason,
        "rework_prompt": "",
    }


def _sanitize_final_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    return text.strip()
