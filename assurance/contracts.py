"""保障域领域合同。"""

import re
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

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_IDENTITY_FIELDS = (
    "change_id",
    "repository",
    "base_revision",
    "head_revision",
    "policy_version",
)

_EVIDENCE_IDENTITY_FIELDS = (
    "evidence_id",
    "kind",
    "producer",
    "source_ref",
)

_FINDING_TEXT_FIELDS = (
    "finding_id",
    "claim",
    "model_ref",
)

_HUMAN_DECISION_TEXT_FIELDS = (
    "decision_id",
    "owner",
    "owner_role",
    "reason",
)


class ChangeSubject(BaseModel):
    """一次变更的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    change_id: str = Field(min_length=1)
    subject_digest: str
    repository: str = Field(min_length=1)
    base_revision: str = Field(min_length=1)
    head_revision: str = Field(min_length=1)
    task_digest: str
    policy_version: str = Field(min_length=1)
    created_at: AwareDatetime

    @field_validator("subject_digest", "task_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator(*_IDENTITY_FIELDS)
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value


class Evidence(BaseModel):
    """一条保障证据的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence_id: str = Field(min_length=1)
    subject_digest: str
    kind: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    artifact_digest: str
    source_ref: str = Field(min_length=1)
    trace_id: str | None = Field(default=None, min_length=1)
    status: Literal[
        "success", "failure", "error", "timeout", "cancelled", "truncated"
    ]
    trust_level: Literal[
        "declared", "observed", "deterministic", "inferred", "human_attested"
    ]
    collected_at: AwareDatetime

    @field_validator("subject_digest", "artifact_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator(*_EVIDENCE_IDENTITY_FIELDS)
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("trace_id")
    @classmethod
    def _reject_blank_trace_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value


class Finding(BaseModel):
    """一条评审发现的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    finding_id: str = Field(min_length=1)
    subject_digest: str
    reviewer_role: Literal["intent", "architecture", "operability"]
    claim: str = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    basis: Literal["deterministic", "inferred"]
    severity: Literal["info", "low", "medium", "high", "critical"]
    confidence: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    rubric_hash: str
    model_ref: str = Field(min_length=1)
    status: Literal["open", "acknowledged", "resolved", "dismissed", "stale"]

    @field_validator("subject_digest", "rubric_hash")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator(*_FINDING_TEXT_FIELDS)
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("must contain at least one Evidence ID")
        seen = set()
        for item in value:
            if not item.strip():
                raise ValueError("Evidence IDs must not be empty or whitespace-only")
            if item in seen:
                raise ValueError("Evidence IDs must be unique")
            seen.add(item)
        return value


class ExecutionStep(BaseModel):
    """一次执行步骤的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(strict=True, ge=0)
    planned_role: Literal["intent", "architecture", "operability"]
    actual_role: Literal["intent", "architecture", "operability"] | None = None
    model_ref: str | None = Field(default=None, min_length=1)
    provider: str | None = Field(default=None, min_length=1)
    tool_grants: tuple[str, ...] = ()
    routing_rule: str = Field(min_length=1)
    fallback_reason: str | None = Field(default=None, min_length=1)
    token_budget: int | None = Field(default=None, strict=True, ge=0)
    timeout_seconds: int = Field(strict=True, gt=0)
    result: Literal[
        "success", "failure", "timeout", "cancelled", "skipped", "blocked"
    ]
    schema_status: Literal["valid", "repaired", "invalid", "not_produced"]

    @field_validator("routing_rule")
    @classmethod
    def _reject_blank_routing_rule(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("model_ref", "provider", "fallback_reason")
    @classmethod
    def _reject_blank_optional_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("tool_grants")
    @classmethod
    def _validate_tool_grants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen = set()
        for item in value:
            if not item.strip():
                raise ValueError("tool grants must not be empty or whitespace-only")
            if item in seen:
                raise ValueError("tool grants must be unique")
            seen.add(item)
        return value

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "ExecutionStep":
        if self.result in ("success", "failure", "timeout", "cancelled"):
            if self.actual_role is None:
                raise ValueError("actual_role is required for executed outcomes")
            if self.model_ref is None:
                raise ValueError("model_ref is required for executed outcomes")
            if self.provider is None:
                raise ValueError("provider is required for executed outcomes")
        elif self.result in ("skipped", "blocked"):
            if self.actual_role is not None:
                raise ValueError(
                    "actual_role must be None for skipped or blocked outcomes"
                )
        if self.result == "success" and self.schema_status not in (
            "valid",
            "repaired",
        ):
            raise ValueError("schema_status must be valid or repaired for success")
        if self.result in ("skipped", "blocked", "timeout", "cancelled"):
            if self.schema_status != "not_produced":
                raise ValueError(
                    "schema_status must be not_produced for "
                    "skipped, blocked, timeout, or cancelled outcomes"
                )
        return self


class ExecutionReceipt(BaseModel):
    """一次执行的整体不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    receipt_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    subject_digest: str
    steps: tuple[ExecutionStep, ...]
    overall_result: Literal["success", "partial", "failure", "blocked", "cancelled"]
    input_tokens: int = Field(default=0, strict=True, ge=0)
    output_tokens: int = Field(default=0, strict=True, ge=0)
    cost_usd: float = Field(default=0, strict=True, ge=0, allow_inf_nan=False)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @field_validator("receipt_id", "run_id")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("subject_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("steps")
    @classmethod
    def _validate_step_sequences(
        cls, value: tuple[ExecutionStep, ...]
    ) -> tuple[ExecutionStep, ...]:
        if not value:
            raise ValueError("must contain at least one ExecutionStep")
        for index, step in enumerate(value):
            if step.sequence != index:
                raise ValueError(
                    "step sequences must be exactly 0..n-1 in tuple order"
                )
        return value

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "ExecutionReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        if self.overall_result == "success":
            if any(step.result != "success" for step in self.steps):
                raise ValueError(
                    "every step must have result=success when overall_result=success"
                )
        if any(step.result == "blocked" for step in self.steps):
            if self.overall_result != "blocked":
                raise ValueError(
                    "overall_result must be blocked when any step is blocked"
                )
        if self.overall_result == "cancelled":
            if not any(step.result == "cancelled" for step in self.steps):
                raise ValueError(
                    "at least one step must be cancelled when "
                    "overall_result=cancelled"
                )
        return self


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


class PolicyDecision(BaseModel):
    """一次策略决策的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    decision_id: str = Field(min_length=1)
    subject_digest: str
    policy_version: str = Field(min_length=1)
    rules_digest: str
    outcome: Literal[
        "STALE", "BLOCKED", "NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"
    ]
    reason_codes: tuple[str, ...] = ()
    required_collectors: tuple[str, ...] = ()
    required_reviewers: tuple[
        Literal["intent", "architecture", "operability"], ...
    ] = ()
    required_human_role: str | None = Field(default=None, min_length=1)
    evaluated_evidence_refs: tuple[str, ...] = ()
    evaluated_finding_refs: tuple[str, ...] = ()
    evaluated_receipt_refs: tuple[str, ...] = ()
    waiver_ref: str | None = Field(default=None, min_length=1)
    evaluated_at: AwareDatetime

    @field_validator("subject_digest", "rules_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("decision_id", "policy_version")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("required_human_role", "waiver_ref")
    @classmethod
    def _reject_blank_optional_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator(
        "reason_codes",
        "required_collectors",
        "evaluated_evidence_refs",
        "evaluated_finding_refs",
        "evaluated_receipt_refs",
    )
    @classmethod
    def _validate_string_tuples(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _validate_unique_nonblank_tuple(value, info.field_name)

    @field_validator("required_reviewers")
    @classmethod
    def _validate_required_reviewers(
        cls,
        value: tuple[Literal["intent", "architecture", "operability"], ...],
    ) -> tuple[Literal["intent", "architecture", "operability"], ...]:
        seen = set()
        for item in value:
            if item in seen:
                raise ValueError("required_reviewers must be unique")
            seen.add(item)
        return value

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "PolicyDecision":
        if self.outcome in ("STALE", "BLOCKED", "NEEDS_HUMAN"):
            if not self.reason_codes:
                raise ValueError(
                    "reason_codes is required for "
                    "STALE, BLOCKED, and NEEDS_HUMAN outcomes"
                )
        if self.outcome == "PASS_WITH_WAIVER":
            if self.waiver_ref is None:
                raise ValueError("waiver_ref is required for PASS_WITH_WAIVER")
        elif self.waiver_ref is not None:
            raise ValueError("waiver_ref is only allowed for PASS_WITH_WAIVER")
        if self.outcome == "NEEDS_HUMAN":
            if self.required_human_role is None:
                raise ValueError(
                    "required_human_role is required for NEEDS_HUMAN"
                )
        return self


class HumanDecision(BaseModel):
    """一次人工审批决策的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    decision_id: str = Field(min_length=1)
    subject_digest: str
    actor_type: Literal["human"] = "human"
    owner: str = Field(min_length=1)
    owner_role: str = Field(min_length=1)
    decision: Literal["approve", "reject", "approve_with_waiver"]
    reason: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    waiver_id: str | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = None
    decided_at: AwareDatetime

    @field_validator("subject_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator(*_HUMAN_DECISION_TEXT_FIELDS)
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

    @field_validator("conditions")
    @classmethod
    def _validate_conditions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _validate_unique_nonblank_tuple(value, "conditions")

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "HumanDecision":
        if self.decision == "approve_with_waiver":
            if self.waiver_id is None:
                raise ValueError("waiver_id is required for approve_with_waiver")
            if self.expires_at is None:
                raise ValueError("expires_at is required for approve_with_waiver")
        else:
            if self.waiver_id is not None:
                raise ValueError(
                    "waiver_id is only allowed for approve_with_waiver"
                )
            if self.expires_at is not None:
                raise ValueError(
                    "expires_at is only allowed for approve_with_waiver"
                )
        if self.expires_at is not None and self.expires_at <= self.decided_at:
            raise ValueError("expires_at must be strictly later than decided_at")
        return self


class AcceptanceCase(BaseModel):
    """一个验收案例的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str = Field(min_length=1)
    subject_digest: str
    state: Literal[
        "DRAFT",
        "EVIDENCE_COLLECTED",
        "NEEDS_EVIDENCE",
        "CONFLICTED",
        "CONDITIONAL_ACCEPTED",
        "ACCEPTED",
        "REJECTED",
        "INVALIDATED",
    ]
    evidence_refs: tuple[str, ...] = ()
    finding_refs: tuple[str, ...] = ()
    execution_receipt_refs: tuple[str, ...] = ()
    policy_decision_refs: tuple[str, ...] = ()
    human_decision_refs: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    invalidation_reason: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("subject_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("case_id")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("invalidation_reason")
    @classmethod
    def _reject_blank_invalidation_reason(
        cls, value: str | None
    ) -> str | None:
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
    def _validate_cross_field_rules(self) -> "AcceptanceCase":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.invalidation_reason is not None:
            if self.state != "INVALIDATED":
                raise ValueError(
                    "invalidation_reason is only allowed for INVALIDATED"
                )
        if self.state == "INVALIDATED":
            if self.invalidation_reason is None:
                raise ValueError(
                    "invalidation_reason is required for INVALIDATED"
                )
            return self
        if self.state == "EVIDENCE_COLLECTED" and not self.evidence_refs:
            raise ValueError(
                "at least one evidence_ref is required for EVIDENCE_COLLECTED"
            )
        if self.state == "NEEDS_EVIDENCE" and not self.missing_evidence:
            raise ValueError(
                "at least one missing_evidence is required for NEEDS_EVIDENCE"
            )
        if self.state == "CONFLICTED" and not self.conflicts:
            raise ValueError("at least one conflict is required for CONFLICTED")
        if self.state == "CONDITIONAL_ACCEPTED":
            if not self.conditions:
                raise ValueError(
                    "at least one condition is required for CONDITIONAL_ACCEPTED"
                )
            if not self.policy_decision_refs:
                raise ValueError(
                    "at least one policy_decision_ref is required for "
                    "CONDITIONAL_ACCEPTED"
                )
            if not self.human_decision_refs:
                raise ValueError(
                    "at least one human_decision_ref is required for "
                    "CONDITIONAL_ACCEPTED"
                )
        if self.state == "ACCEPTED":
            if not self.evidence_refs:
                raise ValueError(
                    "at least one evidence_ref is required for ACCEPTED"
                )
            if not self.policy_decision_refs:
                raise ValueError(
                    "at least one policy_decision_ref is required for ACCEPTED"
                )
            if not self.human_decision_refs:
                raise ValueError(
                    "at least one human_decision_ref is required for ACCEPTED"
                )
        if self.state == "REJECTED":
            if not self.policy_decision_refs and not self.human_decision_refs:
                raise ValueError(
                    "at least one of policy_decision_refs or "
                    "human_decision_refs is required for REJECTED"
                )
        return self
