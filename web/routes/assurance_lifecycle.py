"""P7 lifecycle API: declared observations and read-only remediation lineage."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance.release_observation import (
    ReleaseObservation,
    ReleaseObservationArtifactError,
    ReleaseObservationPayloadError,
    ReleaseObservationSubjectMismatch,
)
from assurance.store import (
    AssuranceStoreError,
    CaseNotFoundError,
    StoreConflictError,
)
from assurance.lifecycle_store import RemediationCommitReceipt
from web.assurance_remediation import (
    AssuranceRemediationError,
    AssuranceRemediationNotConfiguredError,
    AssuranceRemediationNotAppliedError,
    AssuranceRemediationPreparationError,
    AssuranceRemediationRequest,
    AssuranceRemediationResult,
    AssuranceRemediationValidationError,
)
from web.assurance_lifecycle import (
    AssuranceLifecycleRepository,
    get_assurance_lifecycle_repository,
)
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebNotFoundError,
    AssuranceWebPreconditionError,
)
from web.routes.assurance_runs import (
    _is_loopback,
    get_assurance_run_client,
    get_assurance_run_dependencies,
)


router = APIRouter(prefix="/assurance", tags=["assurance-lifecycle"])


_REMEDIATION_NOT_APPLIED_REASON_CODES = {
    "initial_validation_passed": "INITIAL_VALIDATION_PASSED",
    "total_wall_time_exhausted": "TOTAL_WALL_TIME_EXHAUSTED",
    "agent_timeout": "AGENT_TIMEOUT",
    "no_workspace_change": "NO_WORKSPACE_CHANGE",
    "max_repair_attempts": "MAX_REPAIR_ATTEMPTS",
    "subject_builder_invalid": "SUBJECT_BUILDER_INVALID",
    "subject_digest_unchanged": "SUBJECT_DIGEST_UNCHANGED",
    "reviewer_subject_mismatch": "REVIEWER_SUBJECT_MISMATCH",
    "agent_error:RemediationAgentBudgetError": "AGENT_BUDGET_ERROR",
    "agent_error:RemediationAgentResponseBudgetError": "AGENT_RESPONSE_BUDGET_ERROR",
    "agent_error:RemediationAgentActionBudgetError": "AGENT_ACTION_BUDGET_ERROR",
    "agent_error:RemediationAgentContentBudgetError": "AGENT_CONTENT_BUDGET_ERROR",
    "agent_error:RemediationAgentContextBudgetError": "AGENT_CONTEXT_BUDGET_ERROR",
    "agent_error:RemediationAgentProtocolError": "AGENT_PROTOCOL_ERROR",
    "agent_error:RemediationAgentResponseError": "AGENT_RESPONSE_ERROR",
    "agent_error:RemediationAgentActionSchemaError": "ACTION_SCHEMA_ERROR",
    "agent_error:RemediationAgentPathError": "PATH_ERROR",
    "agent_error:RemediationAgentActionPolicyError": "ACTION_POLICY_ERROR",
    "agent_error:RemediationAgentInternalProtocolError": "INTERNAL_PROTOCOL_ERROR",
    "agent_error:WorkspaceViolation": "WORKSPACE_ERROR",
    "agent_error:ValueError": "AGENT_VALUE_ERROR",
    "agent_error:TypeError": "AGENT_TYPE_ERROR",
    "agent_error:ValidationError": "AGENT_VALIDATION_ERROR",
}


def _public_remediation_not_applied_reason(reason_code: object) -> str:
    if isinstance(reason_code, str) and reason_code.startswith("agent_error:"):
        return _REMEDIATION_NOT_APPLIED_REASON_CODES.get(reason_code, "AGENT_ERROR")
    if isinstance(reason_code, str):
        return _REMEDIATION_NOT_APPLIED_REASON_CODES.get(
            reason_code, "PREPARATION_NOT_APPLIED"
        )
    return "PREPARATION_NOT_APPLIED"


class ReleaseObservationImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_base64: str = Field(min_length=1, max_length=2 * 1024 * 1024)


class AssuranceRemediationResponse(BaseModel):
    """The only fields exposed by the remediation endpoint."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v1"] = "v1"
    cached: bool
    receipt: RemediationCommitReceipt
    case_view: dict[str, Any]


def _detail(code: str, message: str, *reason_codes: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "reason_codes": list(reason_codes),
    }


def _fail(status: int, code: str, message: str, *reasons: str) -> None:
    raise HTTPException(status, _detail(code, message, *reasons))


def _call(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except AssuranceWebNotFoundError as exc:
        _fail(404, "ASSURANCE_NOT_FOUND", str(exc), "NOT_FOUND")
    except AssuranceWebPreconditionError as exc:
        _fail(412, "ASSURANCE_PRECONDITION", str(exc), "PRECONDITION_FAILED")
    except AssuranceWebConflictError as exc:
        _fail(409, "ASSURANCE_CONFLICT", str(exc), "CONFLICT")
    except AssuranceWebError as exc:
        _fail(500, "ASSURANCE_STORE_ERROR", str(exc), type(exc).__name__)
    except CaseNotFoundError as exc:
        _fail(404, "ASSURANCE_NOT_FOUND", str(exc), "NOT_FOUND")
    except ReleaseObservationSubjectMismatch as exc:
        _fail(409, "STALE_SUBJECT", str(exc), "STALE_DIGEST")
    except StoreConflictError as exc:
        _fail(409, "ASSURANCE_CONFLICT", str(exc), type(exc).__name__)
    except ReleaseObservationPayloadError as exc:
        _fail(422, "INVALID_IMPORT_PAYLOAD", str(exc), type(exc).__name__)
    except (ValidationError, TypeError, ValueError) as exc:
        _fail(422, "ASSURANCE_INVALID", str(exc), type(exc).__name__)
    except (ReleaseObservationArtifactError, AssuranceStoreError) as exc:
        _fail(500, "ASSURANCE_STORE_ERROR", str(exc), type(exc).__name__)


def _remediation_error(
    status_code: int, code: str, message: str, *reason_codes: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_detail(code, message, *reason_codes),
    )


def _map_remediation_exception(exc: BaseException) -> JSONResponse:
    """Map remediation failures to fixed, non-sensitive public errors."""

    if isinstance(exc, AssuranceRemediationNotAppliedError):
        return _remediation_error(
            409,
            "ASSURANCE_REMEDIATION_NOT_APPLIED",
            "assurance remediation was not applied",
            "REMEDIATION_NOT_APPLIED",
            exc.status.value,
            _public_remediation_not_applied_reason(exc.reason_code),
        )
    if isinstance(exc, AssuranceRemediationNotConfiguredError):
        return _remediation_error(
            503,
            "ASSURANCE_REMEDIATION_NOT_CONFIGURED",
            "assurance remediation service is not configured",
            "NOT_CONFIGURED",
        )
    if isinstance(exc, (AssuranceWebNotFoundError, CaseNotFoundError)):
        return _remediation_error(
            404,
            "ASSURANCE_NOT_FOUND",
            "assurance remediation Case or Finding was not found",
            "NOT_FOUND",
        )
    if isinstance(exc, AssuranceWebPreconditionError):
        return _remediation_error(
            412,
            "ASSURANCE_REMEDIATION_PRECONDITION",
            "assurance remediation precondition was not satisfied",
            "FRESHNESS_REQUIRED",
        )
    if isinstance(exc, (AssuranceWebConflictError, StoreConflictError)):
        return _remediation_error(
            409,
            "ASSURANCE_REMEDIATION_CONFLICT",
            "assurance remediation conflicts with existing state",
            "REMEDIATION_CONFLICT",
        )
    if isinstance(exc, AssuranceRemediationValidationError):
        return _remediation_error(
            422,
            "ASSURANCE_REMEDIATION_INVALID",
            "assurance remediation request is invalid",
            "REQUEST_INVALID",
        )
    if isinstance(exc, (ValidationError, TypeError, ValueError)):
        return _remediation_error(
            422,
            "ASSURANCE_REMEDIATION_INVALID",
            "assurance remediation request is invalid",
            "REQUEST_INVALID",
        )
    if isinstance(
        exc,
        (
            AssuranceRemediationPreparationError,
            AssuranceRemediationError,
            AssuranceWebError,
            AssuranceStoreError,
        ),
    ):
        return _remediation_error(
            500,
            "ASSURANCE_REMEDIATION_FAILED",
            "assurance remediation failed",
            "REMEDIATION_FAILED",
        )
    return _remediation_error(
        500,
        "ASSURANCE_REMEDIATION_FAILED",
        "assurance remediation failed",
        "REMEDIATION_FAILED",
    )


@router.post(
    "/changes/{case_id}/release-observations/manual",
    status_code=201,
)
def record_manual_release_observation(
    case_id: str,
    observation: ReleaseObservation,
    repository: AssuranceLifecycleRepository = Depends(
        get_assurance_lifecycle_repository
    ),
) -> dict[str, Any]:
    if observation.source != "manual":
        _fail(
            422,
            "MANUAL_SOURCE_REQUIRED",
            "manual endpoint requires observation source manual",
        )
    return _call(lambda: repository.record_manual(case_id, observation))


@router.post(
    "/changes/{case_id}/release-observations/import",
    status_code=201,
)
def import_release_observation(
    case_id: str,
    request: ReleaseObservationImportRequest,
    repository: AssuranceLifecycleRepository = Depends(
        get_assurance_lifecycle_repository
    ),
) -> dict[str, Any]:
    try:
        payload = base64.b64decode(request.payload_base64, validate=True)
    except (binascii.Error, ValueError):
        _fail(422, "INVALID_IMPORT_PAYLOAD", "payload_base64 is not valid base64")
    if not payload:
        _fail(422, "INVALID_IMPORT_PAYLOAD", "decoded import payload is empty")
    return _call(lambda: repository.import_payload(case_id, payload))


@router.get("/changes/{case_id}/release-observations")
def list_release_observations(
    case_id: str,
    repository: AssuranceLifecycleRepository = Depends(
        get_assurance_lifecycle_repository
    ),
) -> list[dict[str, Any]]:
    return _call(lambda: repository.list_release_observations(case_id))


@router.get("/changes/{case_id}/remediations")
def list_remediations(
    case_id: str,
    dependencies=Depends(get_assurance_run_dependencies),
) -> list[dict[str, Any]]:
    if dependencies is None:
        _fail(
            503,
            "ASSURANCE_REMEDIATION_NOT_CONFIGURED",
            "assurance remediation repository is not configured",
            "NOT_CONFIGURED",
        )
    return _call(lambda: dependencies.repository.list_remediations(case_id))


@router.post(
    "/changes/{case_id}/remediations",
    response_model=AssuranceRemediationResponse,
)
async def create_remediation(
    case_id: str,
    request: AssuranceRemediationRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    dependencies=Depends(get_assurance_run_dependencies),
    client_host: str | None = Depends(get_assurance_run_client),
) -> JSONResponse:
    """Prepare/commit one server-owned remediation transition."""

    # This guard intentionally precedes all dependency/service work.  The
    # dependency getters only retrieve app state; repository calls begin
    # below, after the loopback decision.
    if not _is_loopback(client_host):
        return _remediation_error(
            403,
            "ASSURANCE_REMEDIATION_LOOPBACK_REQUIRED",
            "assurance remediation endpoint requires a loopback client",
            "LOOPBACK_REQUIRED",
        )
    if idempotency_key is None or not idempotency_key.strip():
        return _remediation_error(
            422,
            "ASSURANCE_REMEDIATION_INVALID",
            "assurance remediation request is invalid",
            "IDEMPOTENCY_KEY_REQUIRED",
        )
    try:
        if len(idempotency_key.encode("utf-8")) > 256:
            return _remediation_error(
                422,
                "ASSURANCE_REMEDIATION_INVALID",
                "assurance remediation request is invalid",
                "IDEMPOTENCY_KEY_TOO_LONG",
            )
    except UnicodeEncodeError:
        return _remediation_error(
            422,
            "ASSURANCE_REMEDIATION_INVALID",
            "assurance remediation request is invalid",
            "IDEMPOTENCY_KEY_INVALID",
        )
    if dependencies is None or dependencies.remediation_service is None:
        return _remediation_error(
            503,
            "ASSURANCE_REMEDIATION_NOT_CONFIGURED",
            "assurance remediation service is not configured",
            "NOT_CONFIGURED",
        )

    try:
        result = await dependencies.remediation_service.remediate(
            case_id,
            request,
            idempotency_key=idempotency_key,
        )
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        return _map_remediation_exception(exc)

    try:
        response = AssuranceRemediationResponse(
            schema_version="v1",
            cached=result.cached,
            receipt=result.receipt,
            case_view=result.case_view,
        )
    except (ValidationError, TypeError, ValueError, AttributeError):
        return _remediation_error(
            500,
            "ASSURANCE_REMEDIATION_FAILED",
            "assurance remediation failed",
            "REMEDIATION_FAILED",
        )
    return JSONResponse(
        status_code=200 if response.cached else 201,
        content=response.model_dump(mode="json"),
    )


__all__ = [
    "AssuranceRemediationRequest",
    "AssuranceRemediationResponse",
    "create_remediation",
    "router",
]
