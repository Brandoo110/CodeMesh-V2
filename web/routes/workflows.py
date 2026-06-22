"""
Workflows / Steps / Runs CRUD（v5 Phase 6.1）。

只做 CRUD —— 执行端点（SSE）在 Phase 6.4 加。
存储后端：WorkflowsStore（~/.codemesh/workflows.db）。

路由前缀：/workflows（最终被 server.py 挂到 /api 下）。
"""
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from web.schemas import (
    RunDetail,
    RunInfo,
    StepCreateRequest,
    StepInfo,
    StepReorderRequest,
    StepResultInfo,
    StepUpdateRequest,
    WorkflowContinueRequest,
    WorkflowCreateRequest,
    WorkflowDetail,
    WorkflowInfo,
    WorkflowPromptChange,
    WorkflowPromptDraftRequest,
    WorkflowPromptDraftResponse,
    WorkflowUpdateRequest,
)
from web.workflow_orchestrator import WorkflowOrchestrator
from web.workflow_templates import seed_templates
from web.workflows_store import WorkflowsStore, get_workflows_store

router = APIRouter(prefix="/workflows", tags=["workflows"])

# 单例 init 标记，避免每次请求重复建表
_initialized = False


async def _ensure_init(store: WorkflowsStore) -> None:
    """首请求时建表 + 注入内置模板。后续请求跳过。"""
    global _initialized
    if not _initialized:
        await store.init()
        # Phase 6.7：内置模板 idempotent seed
        try:
            await seed_templates(store)
        except Exception as e:
            print(f"[workflows] failed to seed templates: {e}")
        _initialized = True


# ─────────────── Workflows CRUD ───────────────

@router.post("", response_model=WorkflowInfo)
async def create_workflow(
    req: WorkflowCreateRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> WorkflowInfo:
    """新建工作流模板（用户自定义，is_template=0）。"""
    await _ensure_init(store)
    data = await store.create_workflow(req.name, req.description)
    return WorkflowInfo(**data)


@router.get("", response_model=list[WorkflowInfo])
async def list_workflows(
    store: WorkflowsStore = Depends(get_workflows_store),
) -> list[WorkflowInfo]:
    """全部工作流（含内置模板）；模板优先，余下按 updated_at desc。"""
    await _ensure_init(store)
    rows = await store.list_workflows()
    return [WorkflowInfo(**r) for r in rows]


@router.get("/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(
    workflow_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> WorkflowDetail:
    """工作流详情含 steps。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    steps = await store.get_steps(workflow_id)
    return WorkflowDetail(**wf, steps=[StepInfo(**s) for s in steps])


@router.put("/{workflow_id}", response_model=WorkflowInfo)
async def update_workflow(
    workflow_id: str,
    req: WorkflowUpdateRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> WorkflowInfo:
    """更新 name / description（不动 steps）。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    await store.update_workflow(
        workflow_id, name=req.name, description=req.description
    )
    updated = await store.get_workflow(workflow_id)
    return WorkflowInfo(**updated)


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> dict[str, str]:
    """级联删除 workflow + steps + runs + results。模板不可删。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates cannot be deleted")
    ok = await store.delete_workflow(workflow_id)
    if not ok:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    return {"deleted": workflow_id}


# ─────────────── Template Fork ───────────────

@router.post("/{workflow_id}/fork", response_model=WorkflowDetail)
async def fork_workflow(
    workflow_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> WorkflowDetail:
    """
    基于现有工作流（含模板）创建一份用户副本。

    新工作流 is_template=0，可自由编辑。所有 step 完整复制（含 prompt + tools）。
    """
    await _ensure_init(store)
    src = await store.get_workflow(workflow_id)
    if not src:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    src_steps = await store.get_steps(workflow_id)
    new_name = src["name"] + "（副本）"
    new_wf = await store.create_workflow(new_name, src["description"])
    for s in src_steps:
        await store.add_step(
            new_wf["id"],
            name=s["name"],
            model=s["model"],
            system_prompt=s["system_prompt"],
            user_prompt=s["user_prompt"],
            enable_tools=s["enable_tools"],
        )
    final = await store.get_workflow(new_wf["id"])
    final_steps = await store.get_steps(new_wf["id"])
    return WorkflowDetail(**final, steps=[StepInfo(**st) for st in final_steps])


# ─────────────── Steps CRUD ───────────────

@router.post("/{workflow_id}/steps", response_model=StepInfo)
async def add_step(
    workflow_id: str,
    req: StepCreateRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> StepInfo:
    """末尾追加一个 step。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    data = await store.add_step(
        workflow_id,
        name=req.name,
        model=req.model,
        system_prompt=req.system_prompt,
        user_prompt=req.user_prompt,
        enable_tools=req.enable_tools,
    )
    return StepInfo(**data)


@router.put("/{workflow_id}/steps/{step_id}", response_model=StepInfo)
async def update_step(
    workflow_id: str,
    step_id: str,
    req: StepUpdateRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> StepInfo:
    """部分字段更新（None 字段不动）。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    step = await store.get_step(step_id)
    if not step or step["workflow_id"] != workflow_id:
        raise HTTPException(404, f"step {step_id} not found in workflow {workflow_id}")
    await store.update_step(
        step_id,
        name=req.name,
        model=req.model,
        system_prompt=req.system_prompt,
        user_prompt=req.user_prompt,
        enable_tools=req.enable_tools,
    )
    updated = await store.get_step(step_id)
    return StepInfo(**updated)


@router.post("/{workflow_id}/prompt-draft", response_model=WorkflowPromptDraftResponse)
async def draft_prompt_changes(
    workflow_id: str,
    req: WorkflowPromptDraftRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> WorkflowPromptDraftResponse:
    """
    把右侧继续修改输入转成可确认的 prompt 草案。

    这里故意先做轻量本地意图路由，不额外调用模型：用户要的是“及时看到
    Planner / Coder / Reviewer 哪些 prompt 会被改”，确认后再执行真正的
    workflow。后续如果要升级成 LLM classifier，可以保持这个响应合同不变。
    """
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    steps = await store.get_steps(workflow_id)
    if not steps:
        raise HTTPException(400, "workflow has no steps")

    changes = _draft_prompt_changes(steps, req.user_request, req.run_context)
    if not changes:
        raise HTTPException(400, "no prompt changes could be drafted")
    start_step_id = _earliest_changed_step_id(steps, changes)
    changed_names = "、".join(change.step_name for change in changes)
    return WorkflowPromptDraftResponse(
        summary=f"已根据你的补充生成 {len(changes)} 处 prompt 修改草案：{changed_names}。",
        start_step_id=start_step_id,
        changes=changes,
    )


@router.delete("/{workflow_id}/steps/{step_id}")
async def delete_step(
    workflow_id: str,
    step_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> dict[str, str]:
    """删 step，后续 step_order 自动 -1 补齐。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    ok = await store.delete_step(step_id)
    if not ok:
        raise HTTPException(404, f"step {step_id} not found")
    return {"deleted": step_id}


@router.post("/{workflow_id}/steps/reorder")
async def reorder_steps(
    workflow_id: str,
    req: StepReorderRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> dict[str, Any]:
    """按 req.step_ids 顺序重新分配 step_order（1-based）。"""
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["is_template"]:
        raise HTTPException(403, "built-in templates are read-only; fork before editing")
    await store.reorder_steps(workflow_id, req.step_ids)
    return {"ok": True, "step_ids": req.step_ids}


# ─────────────── Runs (read-only at Phase 6.1) ───────────────

@router.get("/{workflow_id}/runs", response_model=list[RunInfo])
async def list_runs(
    workflow_id: str,
    limit: int = 20,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> list[RunInfo]:
    """工作流的执行历史（最近 limit 条，按 started_at desc）。"""
    await _ensure_init(store)
    rows = await store.list_runs(workflow_id, limit=limit)
    return [RunInfo(**r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> RunDetail:
    """run 详情含全部 step_results。"""
    await _ensure_init(store)
    run = await store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"run {run_id} not found")
    results = await store.get_step_results(run_id)
    return RunDetail(
        **run,
        step_results=[StepResultInfo(**r) for r in results],
    )


# ─────────────── Execution (SSE) ───────────────

# 模块级 orchestrator 单例。所有 run 共享一份 cancel flags dict——
# 这是有意为之：跨请求才能让 cancel 路由找到正在跑的 run。
_orchestrator: Optional[WorkflowOrchestrator] = None


def _get_orchestrator(store: WorkflowsStore) -> WorkflowOrchestrator:
    global _orchestrator
    if _orchestrator is None or _orchestrator.store is not store:
        _orchestrator = WorkflowOrchestrator(store=store)
    return _orchestrator


async def _stream_run_events(
    orchestrator: WorkflowOrchestrator,
    workflow_id: str,
    run_id: str,
    *,
    only_step_id: Optional[str] = None,
    start_step_id: Optional[str] = None,
    seed_input: Optional[str] = None,
):
    """共享 SSE event generator：把 orchestrator 事件转 EventSource 帧。"""
    try:
        async for event in orchestrator.run(
            workflow_id, run_id,
            only_step_id=only_step_id,
            start_step_id=start_step_id,
            seed_input=seed_input,
        ):
            yield {
                "event": event.get("type", "message"),
                "data": json.dumps(event.get("data", {}), ensure_ascii=False),
            }
    except Exception as e:
        yield {
            "event": "error",
            "data": json.dumps(
                {"message": f"{type(e).__name__}: {e}"}, ensure_ascii=False,
            ),
        }


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
):
    """
    执行整个工作流（SSE）。

    协议：每个 step 依次推 step_start → token* → tool_start/end* → usage →
    step_end。流末尾 done（含 total_cost）。任一步骤失败立即终止整个 run。

    Run id 在端点内 create_run 创建并通过 run_start 事件回传给前端，
    便于后续 cancel / 查询。
    """
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    if wf["step_count"] == 0:
        raise HTTPException(400, "workflow has no steps")

    run = await store.create_run(workflow_id)
    orchestrator = _get_orchestrator(store)

    return EventSourceResponse(
        _stream_run_events(orchestrator, workflow_id, run["id"]),
    )


@router.post("/{workflow_id}/steps/{step_id}/run")
async def run_single_step(
    workflow_id: str,
    step_id: str,
    seed_input: str = "",
    store: WorkflowsStore = Depends(get_workflows_store),
):
    """
    单步执行（Phase 6.8 启用入口；Phase 6.4 路由先就绪）。

    seed_input 作为上一步输出注入；若空则只用 step.user_prompt。
    """
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    step = await store.get_step(step_id)
    if not step or step["workflow_id"] != workflow_id:
        raise HTTPException(404, f"step {step_id} not found in workflow {workflow_id}")

    run = await store.create_run(workflow_id)
    orchestrator = _get_orchestrator(store)
    return EventSourceResponse(
        _stream_run_events(
            orchestrator, workflow_id, run["id"],
            only_step_id=step_id, seed_input=seed_input or None,
        ),
    )


@router.post("/{workflow_id}/continue")
async def continue_workflow(
    workflow_id: str,
    req: WorkflowContinueRequest,
    store: WorkflowsStore = Depends(get_workflows_store),
):
    """
    基于上次执行结果继续迭代（SSE）。

    不重跑 Planner；默认从最后一个具备写权限的 step 开始，继续执行后续
    Reviewer / Summary 步骤。这样用户可以像对话一样提出后续修改要求。
    """
    await _ensure_init(store)
    wf = await store.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    steps = await store.get_steps(workflow_id)
    if not steps:
        raise HTTPException(400, "workflow has no steps")

    orchestrator = _get_orchestrator(store)
    start_step_id = orchestrator.find_continue_start_step_id(
        steps, req.start_step_id
    )
    if not start_step_id:
        raise HTTPException(400, "no step available for continuation")

    run = await store.create_run(workflow_id)
    return EventSourceResponse(
        _stream_run_events(
            orchestrator,
            workflow_id,
            run["id"],
            start_step_id=start_step_id,
            seed_input=_build_continue_seed(req.user_request, req.run_context),
        ),
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    store: WorkflowsStore = Depends(get_workflows_store),
) -> dict[str, Any]:
    """
    标记 run 为 cancelled。当前 step 跑完后退出（不打断 LLM call）。

    返回 ok=True 即使 run 不存在 / 已结束——cancel 是幂等动作。
    """
    await _ensure_init(store)
    orchestrator = _get_orchestrator(store)
    orchestrator.cancel(run_id)
    return {"ok": True, "run_id": run_id}


def _build_continue_seed(user_request: str, run_context: str) -> str:
    context = run_context.strip() or "（没有可用的上次执行上下文）"
    return (
        "用户追加修改要求：\n"
        f"{user_request.strip()}\n\n"
        "上一次执行上下文：\n"
        f"{context}\n\n"
        "请基于当前文件继续迭代，只处理用户追加要求相关内容；"
        "不要从头重写无关部分。完成后简短说明修改点和验证方式。"
    )


_PLANNER_HINTS = (
    "planner",
    "plan",
    "规划",
    "计划",
    "需求",
    "requirement",
    "最开始",
    "第一步",
    "重新设计",
    "方案",
)
_CODER_HINTS = (
    "coder",
    "code",
    "代码",
    "实现",
    "修改",
    "修复",
    "bug",
    "页面",
    "ui",
    "按钮",
    "样式",
)
_REVIEWER_HINTS = (
    "reviewer",
    "review",
    "审查",
    "验收",
    "检查",
    "自检",
    "测试标准",
    "验证",
)
_WRITE_TOOLS = {"edit_file", "write_file", "delete_file"}


def _draft_prompt_changes(
    steps: list[dict],
    user_request: str,
    run_context: str,
) -> list[WorkflowPromptChange]:
    selected = _select_prompt_target_steps(steps, user_request)
    changes: list[WorkflowPromptChange] = []
    for role, step, reason in selected:
        field = "user_prompt"
        old_text = str(step.get(field) or "")
        new_text = _append_prompt_instruction(
            old_text,
            role=role,
            user_request=user_request,
            run_context=run_context,
        )
        changes.append(WorkflowPromptChange(
            step_id=step["id"],
            step_name=step["name"],
            field=field,
            old_text=old_text,
            new_text=new_text,
            reason=reason,
        ))
    return changes


def _select_prompt_target_steps(
    steps: list[dict],
    user_request: str,
) -> list[tuple[str, dict, str]]:
    text = user_request.lower()
    targets: list[tuple[str, dict, str]] = []

    if _contains_any(text, _PLANNER_HINTS):
        step = _find_named_step(steps, ("planner", "plan", "规划", "计划")) or steps[0]
        targets.append((
            "planner",
            step,
            "用户提到计划、需求或最开始的任务定义，先更新 Planner prompt。",
        ))

    if _contains_any(text, _CODER_HINTS):
        step = _find_named_step(steps, ("coder", "code", "实现", "开发")) or _find_last_writable_step(steps) or steps[-1]
        targets.append((
            "coder",
            step,
            "用户描述的是实现、bug、页面或按钮问题，更新 Coder prompt。",
        ))

    if _contains_any(text, _REVIEWER_HINTS):
        step = _find_named_step(steps, ("reviewer", "review", "审查", "验收")) or steps[-1]
        targets.append((
            "reviewer",
            step,
            "用户提到审查、验收或验证标准，更新 Reviewer prompt。",
        ))

    if not targets:
        step = _find_last_writable_step(steps) or steps[0]
        targets.append((
            "coder",
            step,
            "未命中特定角色关键词，默认把追加要求交给最近的可写步骤处理。",
        ))

    deduped: list[tuple[str, dict, str]] = []
    seen: set[str] = set()
    for target in targets:
        step_id = target[1]["id"]
        if step_id in seen:
            continue
        deduped.append(target)
        seen.add(step_id)
    return deduped


def _append_prompt_instruction(
    old_text: str,
    *,
    role: str,
    user_request: str,
    run_context: str,
) -> str:
    role_label = {
        "planner": "请先把这条补充要求纳入计划和需求拆解。",
        "coder": "请在实现时优先处理这条补充要求。",
        "reviewer": "请在审查时重点验证这条补充要求是否被满足。",
    }.get(role, "请处理这条补充要求。")
    context = _truncate_for_prompt(run_context.strip(), 800)
    parts = [
        old_text.rstrip(),
        "",
        "【用户确认后追加】",
        f"用户补充：{user_request.strip()}",
        f"执行重点：{role_label}",
    ]
    if context:
        parts.extend(["参考上次执行上下文：", context])
    return "\n".join(part for part in parts if part != "")


def _earliest_changed_step_id(
    steps: list[dict],
    changes: list[WorkflowPromptChange],
) -> str:
    changed = {change.step_id for change in changes}
    for step in steps:
        if step["id"] in changed:
            return step["id"]
    return changes[0].step_id


def _find_named_step(steps: list[dict], names: tuple[str, ...]) -> Optional[dict]:
    for step in steps:
        step_name = str(step.get("name") or "").lower()
        if any(name in step_name for name in names):
            return step
    return None


def _find_last_writable_step(steps: list[dict]) -> Optional[dict]:
    for step in reversed(steps):
        tools = set(step.get("enable_tools") or [])
        if "*" in tools or tools.intersection(_WRITE_TOOLS):
            return step
    return None


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _truncate_for_prompt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[已截断]"
