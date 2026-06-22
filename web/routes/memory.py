"""
Memory Panel API.

These routes expose CodeMesh's existing memory stores for inspection:
  - SQLite long-term facts
  - auto-extracted markdown memory cards
  - session journal markdown files
  - dreamer gate/status metadata
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from web.memory_store import MemoryStore, get_memory_store
from web.schemas import (
    AutoMemoryInfo,
    DreamStatusInfo,
    JournalInfo,
    LongTermFactCreateRequest,
    LongTermFactInfo,
    MemorySummary,
)

router = APIRouter(prefix="/memory", tags=["memory"])

_initialized = False


async def _ensure_init(store: MemoryStore) -> None:
    global _initialized
    if not _initialized:
        await store.init()
        _initialized = True


@router.get("/summary", response_model=MemorySummary)
async def memory_summary(
    store: MemoryStore = Depends(get_memory_store),
) -> MemorySummary:
    await _ensure_init(store)
    return MemorySummary(**await store.summary())


@router.get("/facts", response_model=list[LongTermFactInfo])
async def list_facts(
    store: MemoryStore = Depends(get_memory_store),
) -> list[LongTermFactInfo]:
    await _ensure_init(store)
    return [LongTermFactInfo(**row) for row in await store.list_facts()]


@router.post("/facts", response_model=LongTermFactInfo)
async def create_fact(
    req: LongTermFactCreateRequest,
    store: MemoryStore = Depends(get_memory_store),
) -> LongTermFactInfo:
    await _ensure_init(store)
    try:
        row = await store.save_fact(req.key, req.value)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return LongTermFactInfo(**row)


@router.delete("/facts/{key}")
async def delete_fact(
    key: str,
    store: MemoryStore = Depends(get_memory_store),
) -> dict[str, str]:
    await _ensure_init(store)
    ok = await store.delete_fact(key)
    if not ok:
        raise HTTPException(404, f"fact {key!r} not found")
    return {"deleted": key}


@router.get("/auto", response_model=list[AutoMemoryInfo])
async def list_auto_memories(
    type: Optional[str] = Query(None, description="Optional memory type filter"),
    store: MemoryStore = Depends(get_memory_store),
) -> list[AutoMemoryInfo]:
    await _ensure_init(store)
    rows = store.list_auto_memories(type_filter=type)
    return [AutoMemoryInfo(**row) for row in rows]


@router.get("/journal", response_model=list[JournalInfo])
async def list_journals(
    limit: int = Query(50, ge=1, le=200),
    store: MemoryStore = Depends(get_memory_store),
) -> list[JournalInfo]:
    await _ensure_init(store)
    return [JournalInfo(**row) for row in store.list_journals(limit=limit)]


@router.get("/dream/status", response_model=DreamStatusInfo)
async def dream_status(
    store: MemoryStore = Depends(get_memory_store),
) -> DreamStatusInfo:
    await _ensure_init(store)
    return DreamStatusInfo(**store.dream_status())


@router.post("/dream/rebuild-index")
async def rebuild_dream_index(
    store: MemoryStore = Depends(get_memory_store),
) -> dict[str, str]:
    await _ensure_init(store)
    return store.rebuild_index()
