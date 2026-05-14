"""
Workflows / Steps / Runs CRUD（v5 Phase 6.1）。

只做 CRUD —— 执行端点（SSE）在 Phase 6.4 加。
存储后端：WorkflowsStore（~/.codemesh/workflows.db）。

路由前缀：/workflows（最终被 server.py 挂到 /api 下）。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from web.schemas import (
    RunDetail,
    RunInfo,
    StepCreateRequest,
    StepInfo,
    StepReorderRequest,
    StepResultInfo,
    StepUpdateRequest,
    WorkflowCreateRequest,
    WorkflowDetail,
    WorkflowInfo,
    WorkflowUpdateRequest,
)
from web.workflows_store import WorkflowsStore, get_workflows_store

router = APIRouter(prefix="/workflows", tags=["workflows"])

# 单例 init 标记，避免每次请求重复建表
_initialized = False


async def _ensure_init(store: WorkflowsStore) -> None:
    global _initialized
    if not _initialized:
        await store.init()
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
