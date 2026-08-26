"""P7 lifecycle API: declared observations and read-only remediation lineage."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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
from web.assurance_lifecycle import (
    AssuranceLifecycleRepository,
    get_assurance_lifecycle_repository,
)


router = APIRouter(prefix="/assurance", tags=["assurance-lifecycle"])


class ReleaseObservationImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_base64: str = Field(min_length=1, max_length=2 * 1024 * 1024)


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
    repository: AssuranceLifecycleRepository = Depends(
        get_assurance_lifecycle_repository
    ),
) -> list[dict[str, Any]]:
    return _call(lambda: repository.list_remediations(case_id))


__all__ = ["router"]
