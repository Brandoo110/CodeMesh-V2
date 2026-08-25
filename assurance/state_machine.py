"""保障域验收状态机：事件、绑定、状态与纯函数转移。"""

from datetime import datetime
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import AcceptanceCase


_EVENT_KINDS = (
    "COLLECT_EVIDENCE",
    "REQUEST_EVIDENCE",
    "RECORD_CONFLICT",
    "CONDITIONALLY_ACCEPT",
    "ACCEPT",
    "REJECT",
    "INVALIDATE",
)

_DRAFT_KINDS = ("COLLECT_EVIDENCE", "REQUEST_EVIDENCE", "INVALIDATE")
_EVIDENCE_COLLECTED_KINDS = (
    "COLLECT_EVIDENCE",
    "REQUEST_EVIDENCE",
    "RECORD_CONFLICT",
    "CONDITIONALLY_ACCEPT",
    "ACCEPT",
    "REJECT",
    "INVALIDATE",
)
_NEEDS_EVIDENCE_KINDS = ("COLLECT_EVIDENCE", "REQUEST_EVIDENCE", "INVALIDATE")
_CONFLICTED_KINDS = (
    "COLLECT_EVIDENCE",
    "REQUEST_EVIDENCE",
    "RECORD_CONFLICT",
    "CONDITIONALLY_ACCEPT",
    "REJECT",
    "INVALIDATE",
)
_CONDITIONAL_ACCEPTED_KINDS = (
    "COLLECT_EVIDENCE",
    "REQUEST_EVIDENCE",
    "RECORD_CONFLICT",
    "CONDITIONALLY_ACCEPT",
    "ACCEPT",
    "REJECT",
    "INVALIDATE",
)
_ACCEPTED_KINDS = ("INVALIDATE",)
_REJECTED_KINDS = ("INVALIDATE",)
_INVALIDATED_KINDS = ()


class InvalidTransitionError(ValueError):
    """状态与事件类型不在精确转移矩阵内，或时间顺序非法。"""


class EventConflictError(ValueError):
    """同一事件 ID 已存在但内容不同。"""


class StaleSubjectError(ValueError):
    """事件或绑定针对的主题与当前案例不一致。"""


def _validate_unique_nonblank_tuple(
    value: tuple[str, ...], field_name: str
) -> tuple[str, ...]:
    seen = set()
    for item in value:
        if not item.strip():
            raise ValueError(
                f"{field_name} must not be empty or whitespace-only"
            )
        if item in seen:
            raise ValueError(f"{field_name} must be unique")
        seen.add(item)
    return value


class AcceptanceEvent(BaseModel):
    """一次验收事件的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    event_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal[
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "RECORD_CONFLICT",
        "CONDITIONALLY_ACCEPT",
        "ACCEPT",
        "REJECT",
        "INVALIDATE",
    ]
    evidence_refs: tuple[str, ...] = ()
    finding_refs: tuple[str, ...] = ()
    execution_receipt_refs: tuple[str, ...] = ()
    policy_decision_refs: tuple[str, ...] = ()
    human_decision_refs: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    reason: str | None = None
    occurred_at: AwareDatetime

    @field_validator("event_id")
    @classmethod
    def _reject_blank_event_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("reason")
    @classmethod
    def _reject_blank_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator(
        "evidence_refs",
        "finding_refs",
        "execution_receipt_refs",
        "policy_decision_refs",
        "human_decision_refs",
        "conditions",
        "conflicts",
        "missing_evidence",
    )
    @classmethod
    def _validate_string_tuples(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _validate_unique_nonblank_tuple(value, info.field_name)

    @model_validator(mode="after")
    def _validate_event_facts(self) -> "AcceptanceEvent":
        if self.kind == "COLLECT_EVIDENCE" and not self.evidence_refs:
            raise ValueError(
                "evidence_refs is required for COLLECT_EVIDENCE"
            )
        if self.kind == "REQUEST_EVIDENCE" and not self.missing_evidence:
            raise ValueError(
                "missing_evidence is required for REQUEST_EVIDENCE"
            )
        if self.kind == "RECORD_CONFLICT" and not self.conflicts:
            raise ValueError("conflicts is required for RECORD_CONFLICT")
        if self.kind == "CONDITIONALLY_ACCEPT":
            if not self.conditions:
                raise ValueError(
                    "conditions is required for CONDITIONALLY_ACCEPT"
                )
            if not self.policy_decision_refs:
                raise ValueError(
                    "policy_decision_refs is required for "
                    "CONDITIONALLY_ACCEPT"
                )
            if not self.human_decision_refs:
                raise ValueError(
                    "human_decision_refs is required for "
                    "CONDITIONALLY_ACCEPT"
                )
        if self.kind == "ACCEPT":
            if not self.policy_decision_refs:
                raise ValueError(
                    "policy_decision_refs is required for ACCEPT"
                )
            if not self.human_decision_refs:
                raise ValueError(
                    "human_decision_refs is required for ACCEPT"
                )
        if self.kind == "REJECT":
            if not self.policy_decision_refs and not self.human_decision_refs:
                raise ValueError(
                    "at least one of policy_decision_refs or "
                    "human_decision_refs is required for REJECT"
                )
        if self.kind == "INVALIDATE" and self.reason is None:
            raise ValueError("reason is required for INVALIDATE")
        if self.kind != "CONDITIONALLY_ACCEPT" and self.conditions:
            raise ValueError("conditions are only allowed for CONDITIONALLY_ACCEPT")
        if self.kind != "RECORD_CONFLICT" and self.conflicts:
            raise ValueError("conflicts are only allowed for RECORD_CONFLICT")
        if self.kind != "REQUEST_EVIDENCE" and self.missing_evidence:
            raise ValueError(
                "missing_evidence is only allowed for REQUEST_EVIDENCE"
            )
        if self.kind != "INVALIDATE" and self.reason is not None:
            raise ValueError("reason is only allowed for INVALIDATE")
        return self


class AcceptanceBinding(BaseModel):
    """验收绑定（策略 / 评分标准 / 豁免）的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    waiver_id: str | None = Field(default=None, min_length=1)
    waiver_expires_at: AwareDatetime | None = None

    @field_validator("policy_version", "rubric_version")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("waiver_id")
    @classmethod
    def _reject_blank_waiver_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @model_validator(mode="after")
    def _validate_waiver_pair(self) -> "AcceptanceBinding":
        if (self.waiver_id is None) != (self.waiver_expires_at is None):
            raise ValueError(
                "waiver_id and waiver_expires_at must either both exist "
                "or both be None"
            )
        return self


class AcceptanceMachineState(BaseModel):
    """验收状态机当前快照（案例 + 已应用事件历史）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case: AcceptanceCase
    applied_events: tuple[AcceptanceEvent, ...] = ()

    @model_validator(mode="after")
    def _validate_history(self) -> "AcceptanceMachineState":
        seen = set()
        for event in self.applied_events:
            if event.event_id in seen:
                raise ValueError("applied_events event_id must be unique")
            seen.add(event.event_id)
            if event.subject_digest != self.case.subject_digest:
                raise ValueError(
                    "every historical event subject_digest must equal "
                    "case.subject_digest"
                )
        return self


def _allowed_kinds(state_name: str) -> tuple[str, ...]:
    if state_name == "DRAFT":
        return _DRAFT_KINDS
    if state_name == "EVIDENCE_COLLECTED":
        return _EVIDENCE_COLLECTED_KINDS
    if state_name == "NEEDS_EVIDENCE":
        return _NEEDS_EVIDENCE_KINDS
    if state_name == "CONFLICTED":
        return _CONFLICTED_KINDS
    if state_name == "CONDITIONAL_ACCEPTED":
        return _CONDITIONAL_ACCEPTED_KINDS
    if state_name == "ACCEPTED":
        return _ACCEPTED_KINDS
    if state_name == "REJECTED":
        return _REJECTED_KINDS
    if state_name == "INVALIDATED":
        return _INVALIDATED_KINDS
    raise ValueError(f"unknown acceptance state: {state_name!r}")


def allowed_event_kinds(state_name) -> tuple[str, ...]:
    """返回指定状态在声明顺序下的精确允许事件类型元组。"""
    if not isinstance(state_name, str):
        raise TypeError("state_name must be a str")
    return _allowed_kinds(state_name)


def _merge_refs(
    old: tuple[str, ...], new: tuple[str, ...]
) -> tuple[str, ...]:
    result = list(old)
    seen = set(old)
    for item in new:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return tuple(result)


def apply_acceptance_event(state, event) -> AcceptanceMachineState:
    """应用一个验收事件，返回经过模型验证的新状态快照。"""
    if not isinstance(state, AcceptanceMachineState):
        raise TypeError("state must be an AcceptanceMachineState")
    if not isinstance(event, AcceptanceEvent):
        raise TypeError("event must be an AcceptanceEvent")

    for existing in state.applied_events:
        if existing.event_id == event.event_id:
            if existing == event:
                return state
            raise EventConflictError(
                f"event_id {event.event_id!r} already exists "
                "with different content"
            )

    if event.subject_digest != state.case.subject_digest:
        raise StaleSubjectError(
            "event subject_digest does not match the current case subject"
        )
    if event.occurred_at < state.case.updated_at:
        raise InvalidTransitionError(
            "event occurred_at must not be earlier than case.updated_at"
        )
    if event.kind not in _allowed_kinds(state.case.state):
        raise InvalidTransitionError(
            f"{event.kind} is not allowed from {state.case.state}"
        )

    old = state.case
    evidence_refs = _merge_refs(old.evidence_refs, event.evidence_refs)
    finding_refs = _merge_refs(old.finding_refs, event.finding_refs)
    execution_receipt_refs = _merge_refs(
        old.execution_receipt_refs, event.execution_receipt_refs
    )
    policy_decision_refs = _merge_refs(
        old.policy_decision_refs, event.policy_decision_refs
    )
    human_decision_refs = _merge_refs(
        old.human_decision_refs, event.human_decision_refs
    )

    if event.kind == "COLLECT_EVIDENCE":
        new_state = "EVIDENCE_COLLECTED"
        conditions = ()
        conflicts = ()
        missing_evidence = ()
        invalidation_reason = None
    elif event.kind == "REQUEST_EVIDENCE":
        new_state = "NEEDS_EVIDENCE"
        conditions = ()
        conflicts = ()
        missing_evidence = event.missing_evidence
        invalidation_reason = None
    elif event.kind == "RECORD_CONFLICT":
        new_state = "CONFLICTED"
        conditions = ()
        conflicts = event.conflicts
        missing_evidence = ()
        invalidation_reason = None
    elif event.kind == "CONDITIONALLY_ACCEPT":
        new_state = "CONDITIONAL_ACCEPTED"
        conditions = event.conditions
        conflicts = ()
        missing_evidence = ()
        invalidation_reason = None
    elif event.kind == "ACCEPT":
        new_state = "ACCEPTED"
        conditions = ()
        conflicts = ()
        missing_evidence = ()
        invalidation_reason = None
    elif event.kind == "REJECT":
        new_state = "REJECTED"
        conditions = ()
        conflicts = ()
        missing_evidence = ()
        invalidation_reason = None
    else:
        new_state = "INVALIDATED"
        conditions = old.conditions
        conflicts = old.conflicts
        missing_evidence = old.missing_evidence
        invalidation_reason = event.reason

    new_case = AcceptanceCase(
        schema_version="v1",
        case_id=old.case_id,
        subject_digest=old.subject_digest,
        state=new_state,
        evidence_refs=evidence_refs,
        finding_refs=finding_refs,
        execution_receipt_refs=execution_receipt_refs,
        policy_decision_refs=policy_decision_refs,
        human_decision_refs=human_decision_refs,
        conditions=conditions,
        conflicts=conflicts,
        missing_evidence=missing_evidence,
        invalidation_reason=invalidation_reason,
        created_at=old.created_at,
        updated_at=event.occurred_at,
    )
    return AcceptanceMachineState(
        schema_version="v1",
        case=new_case,
        applied_events=state.applied_events + (event,),
    )


def invalidation_reasons(bound, current, now) -> tuple[str, ...]:
    """比较绑定与当前事实，返回固定顺序的失效原因。"""
    if not isinstance(bound, AcceptanceBinding):
        raise TypeError("bound must be an AcceptanceBinding")
    if not isinstance(current, AcceptanceBinding):
        raise TypeError("current must be an AcceptanceBinding")
    if not isinstance(now, datetime):
        raise TypeError("now must be an aware datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    reasons = []
    if bound.subject_digest != current.subject_digest:
        reasons.append("SUBJECT_DIGEST_CHANGED")
    if bound.policy_version != current.policy_version:
        reasons.append("POLICY_VERSION_CHANGED")
    if bound.rubric_version != current.rubric_version:
        reasons.append("RUBRIC_VERSION_CHANGED")
    if (
        bound.waiver_expires_at is not None
        and now >= bound.waiver_expires_at
    ):
        reasons.append("WAIVER_EXPIRED")
    return tuple(reasons)


def invalidate_if_needed(
    state, bound, current, now, event_id
) -> AcceptanceMachineState:
    """按需生成 INVALIDATE 事件并应用，全部时间与 ID 由调用方注入。"""
    if not isinstance(state, AcceptanceMachineState):
        raise TypeError("state must be an AcceptanceMachineState")
    if not isinstance(bound, AcceptanceBinding):
        raise TypeError("bound must be an AcceptanceBinding")
    if not isinstance(current, AcceptanceBinding):
        raise TypeError("current must be an AcceptanceBinding")
    if not isinstance(now, datetime):
        raise TypeError("now must be an aware datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(event_id, str):
        raise TypeError("event_id must be a str")
    if not event_id.strip():
        raise ValueError("event_id must not be empty or whitespace-only")

    if bound.subject_digest != state.case.subject_digest:
        raise StaleSubjectError(
            "bound.subject_digest does not match the current case subject"
        )

    reasons = invalidation_reasons(bound, current, now)
    if not reasons:
        return state

    if state.case.state == "INVALIDATED":
        if event_id not in {e.event_id for e in state.applied_events}:
            return state

    generated = AcceptanceEvent(
        schema_version="v1",
        event_id=event_id,
        subject_digest=bound.subject_digest,
        kind="INVALIDATE",
        reason=",".join(reasons),
        occurred_at=now,
    )
    return apply_acceptance_event(state, generated)
