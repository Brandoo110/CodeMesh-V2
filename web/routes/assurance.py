"""Local Acceptance Case API for the CodeMesh V2 assurance workbench."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from assurance.contracts import (
    AcceptanceCase,
    Evidence,
    ExecutionReceipt,
    Finding,
    HumanDecision,
    PolicyDecision,
)
from assurance.state_machine import (
    AcceptanceBinding,
    AcceptanceEvent,
    InvalidTransitionError,
    StaleSubjectError,
)
from assurance.store import AssuranceStoreError, StoreConflictError
from web.assurance_case_view import resolve_action
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebNotFoundError,
    AssuranceWebRepository,
    get_assurance_repository,
)

router = APIRouter(prefix="/assurance", tags=["assurance"])
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1)
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangeCreateRequest(_Request):
    change_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    owner_role: str = Field(min_length=1)
    author: str = Field(min_length=1)
    risk: Literal["low", "medium", "high", "critical"]
    priority: int = Field(ge=0, le=100)
    value: int = Field(ge=0, le=100)
    release_status: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    intent_coverage: str = ""
    architecture_impact: str = ""
    operational_readiness: str = ""
    knowledge_notes: str = ""
    ownership_notes: str = ""


class CollectRequest(_Request):
    event_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal[
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "RECORD_CONFLICT",
        "INVALIDATE",
    ]
    occurred_at: AwareDatetime
    evidence: Evidence | None = None
    missing_evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    reason: str | None = None


class ReviewRequest(_Request):
    event_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    occurred_at: AwareDatetime
    findings: tuple[Finding, ...] = ()
    receipt: ExecutionReceipt
    policy_decision: PolicyDecision
    evidence_refs: tuple[str, ...] = ()


class DecisionRequest(_Request):
    decision_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    owner: str = Field(min_length=1)
    owner_role: str = Field(min_length=1)
    decision: Literal[
        "approve", "reject", "approve_with_conditions", "waiver"
    ]
    reason: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    waiver_id: str | None = None
    expires_at: AwareDatetime | None = None
    decided_at: AwareDatetime
    high_risk_confirmed: bool = False


def _detail(code: str, message: str, *reason_codes: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "reason_codes": list(reason_codes),
    }


def _fail(
    status: int, code: str, message: str, *reason_codes: str
) -> None:
    raise HTTPException(status, _detail(code, message, *reason_codes))


def _call(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except AssuranceWebNotFoundError as exc:
        _fail(404, "ASSURANCE_NOT_FOUND", str(exc), "NOT_FOUND")
    except (
        AssuranceWebConflictError,
        StoreConflictError,
        InvalidTransitionError,
        StaleSubjectError,
    ) as exc:
        _fail(409, "ASSURANCE_CONFLICT", str(exc), type(exc).__name__)
    except (ValidationError, TypeError, ValueError) as exc:
        _fail(422, "ASSURANCE_INVALID", str(exc), type(exc).__name__)
    except (AssuranceWebError, AssuranceStoreError) as exc:
        _fail(500, "ASSURANCE_STORE_ERROR", str(exc), type(exc).__name__)


def _idempotency(value: str) -> str:
    if not value.strip():
        _fail(422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is blank")
    return value


def _case_projection(
    repository: AssuranceWebRepository, change_id: str
) -> dict[str, Any]:
    return _call(lambda: repository.get_change(change_id))


def _require_current_digest(projection: dict[str, Any], digest: str) -> None:
    if digest != projection["case"]["subject_digest"]:
        _fail(409, "STALE_SUBJECT", "subject digest is stale", "STALE_DIGEST")


def _unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return tuple(result)


@router.post("/changes", status_code=201)
def create_change(
    request: ChangeCreateRequest,
    idempotency_key: IdempotencyKey,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    case = AcceptanceCase(
        case_id=request.change_id,
        subject_digest=request.subject_digest,
        state="DRAFT",
        created_at=now,
        updated_at=now,
    )
    binding = AcceptanceBinding(
        subject_digest=request.subject_digest,
        policy_version=request.policy_version,
        rubric_version=request.rubric_version,
    )
    metadata = request.model_dump(
        mode="json", exclude={"subject_digest", "policy_version", "rubric_version"}
    )
    payload = request.model_dump(mode="json")
    return _call(
        lambda: repository.create_change(
            case, binding, metadata, _idempotency(idempotency_key), payload
        )
    )


@router.get("/changes")
def list_changes(
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> list[dict[str, Any]]:
    return _call(repository.list_changes)


@router.get("/changes/{change_id}")
def get_change(
    change_id: str,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    return _case_projection(repository, change_id)


@router.post("/changes/{change_id}/collect")
def collect_evidence(
    change_id: str,
    request: CollectRequest,
    idempotency_key: IdempotencyKey,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    projection = _case_projection(repository, change_id)
    _require_current_digest(projection, request.subject_digest)
    if request.kind == "COLLECT_EVIDENCE":
        if request.evidence is None:
            _fail(422, "EVIDENCE_REQUIRED", "COLLECT_EVIDENCE requires Evidence")
        if request.evidence.subject_digest != request.subject_digest:
            _fail(409, "STALE_EVIDENCE", "Evidence subject digest is stale")
        facts = {"evidence_refs": (request.evidence.evidence_id,)}
    elif request.kind == "REQUEST_EVIDENCE":
        facts = {"missing_evidence": request.missing_evidence}
    elif request.kind == "RECORD_CONFLICT":
        facts = {"conflicts": request.conflicts}
    else:
        facts = {"reason": request.reason}
    event = AcceptanceEvent(
        event_id=request.event_id,
        subject_digest=request.subject_digest,
        kind=request.kind,
        occurred_at=request.occurred_at,
        **facts,
    )
    return _call(
        lambda: repository.collect(
            change_id,
            event,
            request.evidence if request.kind == "COLLECT_EVIDENCE" else None,
            _idempotency(idempotency_key),
            request.model_dump(mode="json"),
        )
    )


@router.post("/changes/{change_id}/review")
def review_change(
    change_id: str,
    request: ReviewRequest,
    idempotency_key: IdempotencyKey,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    projection = _case_projection(repository, change_id)
    _require_current_digest(projection, request.subject_digest)
    nested = (request.receipt, request.policy_decision, *request.findings)
    if any(item.subject_digest != request.subject_digest for item in nested):
        _fail(409, "STALE_REVIEW", "review payload contains a stale digest")
    evidence_refs = _unique(
        tuple(projection["case"]["evidence_refs"]), request.evidence_refs
    )
    if not evidence_refs:
        _fail(422, "EVIDENCE_REQUIRED", "review requires collected Evidence")
    event = AcceptanceEvent(
        event_id=request.event_id,
        subject_digest=request.subject_digest,
        kind="COLLECT_EVIDENCE",
        evidence_refs=evidence_refs,
        finding_refs=tuple(item.finding_id for item in request.findings),
        execution_receipt_refs=(request.receipt.receipt_id,),
        policy_decision_refs=(request.policy_decision.decision_id,),
        occurred_at=request.occurred_at,
    )
    return _call(
        lambda: repository.review(
            change_id,
            event,
            request.findings,
            request.receipt,
            request.policy_decision,
            _idempotency(idempotency_key),
            request.model_dump(mode="json"),
        )
    )


@router.post("/changes/{change_id}/decisions")
def decide_change(
    change_id: str,
    request: DecisionRequest,
    idempotency_key: IdempotencyKey,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    projection = _case_projection(repository, change_id)
    _require_current_digest(projection, request.subject_digest)
    action = resolve_action(projection["allowed_actions"], request.decision)
    if action is None:
        _fail(
            409,
            "ACTION_NOT_ALLOWED",
            f"action {request.decision!r} is not allowed for this CaseView",
            "ACTION_NOT_ALLOWED",
        )
    metadata = projection.get("metadata") or {}
    if action["self_approval_forbidden"] and request.owner == metadata.get("author"):
        _fail(403, "SELF_APPROVAL_FORBIDDEN", "authors cannot approve their own change")
    required_role = action["required_human_role"]
    if required_role is not None and request.owner_role != required_role:
        _fail(
            403,
            "POLICY_ROLE_REQUIRED",
            f"policy requires human role {required_role}",
            "HUMAN_ROLE_MISMATCH",
        )
    if (
        action["high_risk_confirmation_required"]
        and not request.high_risk_confirmed
    ):
        _fail(
            403,
            "HIGH_RISK_CONFIRMATION_REQUIRED",
            "confirm high-risk approval",
        )
    latest_policy = next(
        (
            decision
            for decision in reversed(projection["decisions"])
            if decision["kind"] == "policy"
        ),
        None,
    )
    decision_policy_refs = (
        (latest_policy["decision_id"],) if latest_policy is not None else ()
    )
    if request.decision in {"approve_with_conditions", "waiver"}:
        if not request.conditions:
            _fail(422, "CONDITIONS_REQUIRED", "conditional approval requires conditions")
    elif request.conditions:
        _fail(422, "CONDITIONS_NOT_ALLOWED", "conditions require conditional approval")

    human_kind = "approve_with_waiver" if request.decision == "waiver" else (
        "reject" if request.decision == "reject" else "approve"
    )
    human = HumanDecision(
        decision_id=request.decision_id,
        subject_digest=request.subject_digest,
        owner=request.owner,
        owner_role=request.owner_role,
        decision=human_kind,
        reason=request.reason,
        conditions=request.conditions,
        waiver_id=request.waiver_id,
        expires_at=request.expires_at,
        decided_at=request.decided_at,
    )
    if request.decision == "reject":
        kind = "REJECT"
        facts = {
            "policy_decision_refs": decision_policy_refs,
            "human_decision_refs": (request.decision_id,),
        }
    elif request.decision == "approve":
        kind = "ACCEPT"
        facts = {
            "policy_decision_refs": decision_policy_refs,
            "human_decision_refs": (request.decision_id,),
        }
    else:
        kind = "CONDITIONALLY_ACCEPT"
        facts = {
            "conditions": request.conditions,
            "policy_decision_refs": decision_policy_refs,
            "human_decision_refs": (request.decision_id,),
        }
    event = AcceptanceEvent(
        event_id=f"decision:{request.decision_id}",
        subject_digest=request.subject_digest,
        kind=kind,
        occurred_at=request.decided_at,
        **facts,
    )
    return _call(
        lambda: repository.decide(
            change_id,
            human,
            event,
            _idempotency(idempotency_key),
            request.model_dump(mode="json"),
        )
    )


@router.get("/changes/{change_id}/evidence/{evidence_id}")
def get_evidence(
    change_id: str,
    evidence_id: str,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    return _call(lambda: repository.get_evidence(change_id, evidence_id))


@router.get("/changes/{change_id}/receipt")
def get_receipt(
    change_id: str,
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any]:
    return _call(lambda: repository.get_receipt(change_id))


@router.get("/changes/{change_id}/passport", response_model=None)
def get_passport(
    change_id: str,
    format: Literal["json", "markdown"] = Query("json"),
    repository: AssuranceWebRepository = Depends(get_assurance_repository),
) -> dict[str, Any] | PlainTextResponse:
    passport = _call(lambda: repository.get_passport(change_id))
    if format == "markdown":
        return PlainTextResponse(passport["markdown"], media_type="text/markdown")
    return passport["canonical"]
