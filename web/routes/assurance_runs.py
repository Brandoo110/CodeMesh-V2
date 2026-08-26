"""Thin HTTP adapter for the product Assurance Run entry point."""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from assurance.run_service import (
    AssuranceRunError,
    AssuranceRunIntent,
    AssuranceRunRedactionError,
    AssuranceRunResult,
    AssuranceRunService,
    AssuranceRunStaleError,
    AssuranceRunValidationError,
    IdempotencyConflictError,
)
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebRepository,
)
from web.assurance_run_committer import AssuranceRunPersistenceError
from assurance.store import AssuranceStoreError


router = APIRouter(prefix="/assurance", tags=["assurance-runs"])


class AssuranceRunRequest(BaseModel):
    """The exact caller-controlled portion of a Golden Path run."""

    model_config = ConfigDict(extra="forbid")

    repository_path: Path
    repository_identity: str = Field(min_length=1)
    author: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    policy_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    runbook_paths: tuple[str, ...] = ()
    command_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    changed_lines_total: StrictInt | None = Field(default=None, ge=0)
    external_side_effects: Literal[
        "none_declared", "present_declared", "unknown"
    ] = "unknown"
    provider_boundary: Literal[
        "within_declared_boundary", "crosses_declared_boundary", "unknown"
    ] = "unknown"


class AssuranceRunResponse(BaseModel):
    """The only fields exposed by the product run endpoint."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    run_id: str
    request_digest: str
    cached: bool
    case_id: str
    case_view: dict[str, Any]


def get_assurance_run_dependencies(
    request: Request,
) -> AssuranceRunWebDependencies | None:
    """Resolve the explicit app-owned composition, if one was installed."""

    return getattr(request.app.state, "assurance_run_dependencies", None)


def get_assurance_run_client(request: Request) -> str | None:
    """Resolve the connected peer; tests may override this dependency."""

    return request.client.host if request.client is not None else None


def _detail(code: str, message: str, *reason_codes: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "reason_codes": list(reason_codes),
    }


def _error(
    status_code: int, code: str, message: str, *reason_codes: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_detail(code, message, *reason_codes),
    )


def _is_loopback(host: str | None) -> bool:
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def _to_intent(request: AssuranceRunRequest) -> AssuranceRunIntent:
    payload = request.model_dump(mode="python")
    # This is deliberately not caller-controlled.  The Service's source
    # binding records the same fixed provenance for the committed run.
    payload["author_provenance"] = "caller_declared"
    return AssuranceRunIntent.model_validate(payload)


def _map_run_exception(exc: BaseException) -> JSONResponse:
    """Map only stable public classes; never expose exception text."""

    if isinstance(exc, AssuranceRunValidationError):
        return _error(
            422,
            "ASSURANCE_RUN_INVALID",
            "assurance run request is invalid",
            "REQUEST_INVALID",
        )
    if isinstance(exc, AssuranceRunStaleError):
        return _error(
            409,
            "ASSURANCE_RUN_STALE",
            "assurance run source is stale",
            "STALE_SOURCE",
        )
    if isinstance(exc, (IdempotencyConflictError, AssuranceWebConflictError)):
        return _error(
            409,
            "ASSURANCE_RUN_CONFLICT",
            "assurance run conflicts with existing state",
            "RUN_CONFLICT",
        )
    if isinstance(exc, AssuranceRunRedactionError):
        return _error(
            503,
            "ASSURANCE_REDACTION_FAILED",
            "assurance run redaction failed",
            "REDACTION_FAILED",
        )
    if isinstance(
        exc,
        (
            AssuranceWebError,
            AssuranceStoreError,
            AssuranceRunPersistenceError,
            AssuranceRunError,
        ),
    ):
        return _error(
            500,
            "ASSURANCE_RUN_FAILED",
            "assurance run failed",
            "RUN_FAILED",
        )
    return _error(
        500,
        "ASSURANCE_RUN_FAILED",
        "assurance run failed",
        "RUN_FAILED",
    )


@router.post("/runs", response_model=AssuranceRunResponse, status_code=201)
async def create_assurance_run(
    request: AssuranceRunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    dependencies: AssuranceRunWebDependencies | None = Depends(
        get_assurance_run_dependencies
    ),
    client_host: str | None = Depends(get_assurance_run_client),
) -> JSONResponse:
    """Run the service and read the authoritative CaseView from its Repository."""

    if dependencies is None:
        return _error(
            503,
            "ASSURANCE_RUN_NOT_CONFIGURED",
            "assurance run composition is not configured",
            "NOT_CONFIGURED",
        )
    if not _is_loopback(client_host):
        return _error(
            403,
            "ASSURANCE_RUN_LOOPBACK_REQUIRED",
            "assurance run endpoint requires a loopback client",
            "LOOPBACK_REQUIRED",
        )
    if idempotency_key is None or not idempotency_key.strip():
        return _error(
            422,
            "ASSURANCE_RUN_INVALID",
            "assurance run request is invalid",
            "IDEMPOTENCY_KEY_REQUIRED",
        )
    try:
        if len(idempotency_key.encode("utf-8")) > 256:
            return _error(
                422,
                "ASSURANCE_RUN_INVALID",
                "assurance run request is invalid",
                "IDEMPOTENCY_KEY_TOO_LONG",
            )
    except UnicodeEncodeError:
        return _error(
            422,
            "ASSURANCE_RUN_INVALID",
            "assurance run request is invalid",
            "IDEMPOTENCY_KEY_INVALID",
        )

    try:
        intent = _to_intent(request)
    except (ValidationError, TypeError, ValueError):
        return _error(
            422,
            "ASSURANCE_RUN_INVALID",
            "assurance run request is invalid",
            "REQUEST_INVALID",
        )

    try:
        result = await dependencies.service.run(
            intent, idempotency_key=idempotency_key
        )
    except BaseException as exc:
        # CancelledError is a BaseException and must propagate.  Only ordinary
        # Exception subclasses are mapped into a product response.
        if isinstance(exc, asyncio.CancelledError):
            raise
        return _map_run_exception(exc)

    if type(result) is not AssuranceRunResult:
        return _error(
            500,
            "ASSURANCE_RUN_FAILED",
            "assurance run failed",
            "RUN_FAILED",
        )
    try:
        case_view = await asyncio.to_thread(
            dependencies.repository.get_change, result.bundle.case.case_id
        )
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        return _map_run_exception(exc)

    response = AssuranceRunResponse(
        schema_version="v1",
        run_id=result.run_id,
        request_digest=result.request_digest,
        cached=result.cached,
        case_id=result.bundle.case.case_id,
        case_view=case_view,
    )
    return JSONResponse(
        status_code=200 if result.cached else 201,
        content=response.model_dump(mode="json"),
    )


__all__ = [
    "AssuranceRunRequest",
    "AssuranceRunResponse",
    "create_assurance_run",
    "get_assurance_run_client",
    "get_assurance_run_dependencies",
    "router",
]
