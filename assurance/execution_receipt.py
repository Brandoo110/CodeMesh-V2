"""V2-P4-07 Council Execution Receipt v1.

This module builds and validates an immutable, JSON-round-trippable receipt
that reconstructs planned versus actual Council execution from exact caller
inputs: one ``CouncilPlan``, three P4-06 ``ModelRouteDecision`` routes in
canonical role order, three runtime ``ReviewerExecutionFact`` values, an
optional bound ``CouncilRunResult``, and explicit aware timestamps.

The rich receipt is the canonical source. Its nested P1
``assurance.contracts.ExecutionReceipt`` is a deterministic, fully
cross-validated projection for the existing Policy Gate input; it is never an
independently editable second truth source.

This module performs no model/provider/tool call, no routing, no Council run,
no Gate evaluation, no dispute resolution, no persistence, and no filesystem,
network, process, environment, or time access. It emits no acceptance or
authorization signal.
"""

import hashlib
import json
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

from .contracts import ExecutionReceipt, ExecutionStep
from .model_routing import ModelRouteDecision, ModelRouter
from .review_council import CouncilPlan, CouncilRunResult


_ROLE_ORDER = ("intent", "architecture", "operability")
_ROLE_KEYS = frozenset(_ROLE_ORDER)
_ROLE = Literal["intent", "architecture", "operability"]

_EXECUTED_RESULTS = frozenset(
    {
        "success",
        "failure",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "schema_invalid",
    }
)
_EXECUTED_NON_SUCCESS = _EXECUTED_RESULTS - {"success"}
_NOT_EXECUTED_RESULTS = frozenset({"skipped", "blocked"})

_OUTCOME = Literal[
    "success",
    "failure",
    "timeout",
    "cancelled",
    "budget_exceeded",
    "schema_invalid",
    "skipped",
    "blocked",
]
_USAGE_STATUS = Literal["measured", "unavailable", "not_applicable"]
_OUTPUT_SCHEMA_STATUS = Literal["valid", "synthetic", "not_produced"]
_ACTUAL_STATE = Literal["not_started", "completed", "interrupted"]
_OVERALL_RESULT = Literal[
    "success", "partial", "failure", "blocked", "cancelled"
]
_USAGE_AGGREGATE = Literal["measured", "partial", "unavailable", "not_applicable"]
_PROVIDER_BOUNDARY = Literal["local-only", "approved-provider", "any"]
_BLOCK_REASON = Literal[
    "routing_disabled", "no_matching_rule", "no_eligible_candidate"
]
_ATTEMPT_REASON = Literal[
    "candidate_unavailable",
    "provider_not_allowed",
    "provider_boundary_denied",
    "selected",
]
_FAILURE_CODE = Literal[
    "execution_failed",
    "timeout",
    "cancelled",
    "budget_exceeded",
    "schema_invalid",
    "skipped",
    "blocked",
]

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RECEIPT_ID_RE = re.compile(r"council_receipt_[0-9a-f]{64}\Z")
_GATE_RECEIPT_ID_RE = re.compile(r"gate_[0-9a-f]{64}\Z")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

_MAX_IDENTIFIER_BYTES = 256
_MAX_TOOL_GRANT_ITEMS = 64
_MAX_TOOL_GRANT_BYTES = 256
_MAX_TIMEOUT_SECONDS = 3600


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _model_digest(model: BaseModel) -> str:
    return _sha256_digest(_canonical_json_bytes(model.model_dump(mode="json")))


def _require_nonblank(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be empty or whitespace-only")
    return value


def _validate_optional_identifier(value: str | None, label: str) -> str | None:
    if value is None:
        return value
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact string or None")
    _require_nonblank(value, label)
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise ValueError(
            f"{label} must not exceed {_MAX_IDENTIFIER_BYTES} UTF-8 bytes"
        )
    return value


def _validate_sha256(value: str | None, label: str) -> str | None:
    if value is None:
        return value
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a sha256:<64 lowercase hex> digest")
    return value


def _validate_sorted_unique_strings(
    value: tuple[str, ...], label: str
) -> tuple[str, ...]:
    if len(value) > _MAX_TOOL_GRANT_ITEMS:
        raise ValueError(
            f"{label} must contain at most {_MAX_TOOL_GRANT_ITEMS} items"
        )
    seen = set()
    result = []
    for item in value:
        if type(item) is not str:
            raise ValueError(f"{label} items must be exact strings")
        _require_nonblank(item, f"{label} item")
        if len(item.encode("utf-8")) > _MAX_TOOL_GRANT_BYTES:
            raise ValueError(
                f"{label} item must not exceed {_MAX_TOOL_GRANT_BYTES} "
                "UTF-8 bytes"
            )
        if item in seen:
            raise ValueError(f"{label} items must be unique")
        seen.add(item)
        result.append(item)
    if result != sorted(result):
        raise ValueError(f"{label} must be lexicographically sorted and unique")
    return tuple(result)


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _role_index(role: str) -> int:
    return _ROLE_ORDER.index(role)


def _boundary_rank(value: str) -> int:
    if value == "local-only":
        return 0
    if value == "approved-provider":
        return 1
    return 2


def _failure_code_for(result: str) -> str:
    if result == "failure":
        return "execution_failed"
    if result == "timeout":
        return "timeout"
    if result == "cancelled":
        return "cancelled"
    if result == "budget_exceeded":
        return "budget_exceeded"
    if result == "schema_invalid":
        return "schema_invalid"
    if result == "skipped":
        return "skipped"
    return "blocked"


def _exact_tuple_field(value: object, info: ValidationInfo) -> object:
    if type(value) is tuple:
        return value
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    raise ValueError(
        f"{info.field_name} must be an exact tuple at raw validation"
    )


class _RouteAttemptSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    candidate_id: str = Field(min_length=1, max_length=128)
    reason: _ATTEMPT_REASON

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_nonblank(cls, value: str) -> str:
        return _require_nonblank(value, "attempt candidate_id")


class _CandidateSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    candidate_id: str = Field(min_length=1, max_length=128)
    provider_ref: str = Field(min_length=1, max_length=128)
    model_ref: str = Field(min_length=1, max_length=128)
    required_provider_boundary: _PROVIDER_BOUNDARY

    @field_validator("candidate_id", "provider_ref", "model_ref")
    @classmethod
    def _nonblank_ids(cls, value: str) -> str:
        return _require_nonblank(value, "candidate/provider/model reference")


class _BudgetSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    token_budget_cap: int = Field(strict=True, ge=1)
    cost_budget_cap_usd: float = Field(
        strict=True, ge=0, allow_inf_nan=False
    )

    @field_validator("cost_budget_cap_usd", mode="before")
    @classmethod
    def _exact_float_budget(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError(
                "cost_budget_cap_usd must be an exact float "
                "(int/bool coercion is not allowed)"
            )
        return value


class ReceiptRouteSnapshot(BaseModel):
    """JSON-safe immutable projection of one accepted P4-06 route decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    decision_id: str
    policy_digest: str
    subject_digest: str
    risk_classification_id: str = Field(min_length=1)
    role: _ROLE
    phase: Literal["review"] = "review"
    effective_risk: Literal["low", "medium", "high", "critical"]
    priority: Literal["low", "normal", "high", "critical"]
    request_provider_boundary: _PROVIDER_BOUNDARY
    outcome: Literal["selected", "blocked"]
    block_reason: _BLOCK_REASON | None = None
    matched_rule_id: str | None = Field(default=None, min_length=1)
    matched_tier_alias: str | None = Field(default=None, min_length=1)
    attempts: tuple[_RouteAttemptSnapshot, ...] = ()
    selected_candidate: _CandidateSnapshot | None = None
    allocated_budget: _BudgetSnapshot | None = None

    @field_validator("decision_id", "policy_digest", "subject_digest")
    @classmethod
    def _sha256_fields(cls, value: str) -> str:
        return _validate_sha256(value, "digest field")

    @field_validator("matched_rule_id", "matched_tier_alias")
    @classmethod
    def _optional_nonblank(cls, value: str | None) -> str | None:
        return _validate_optional_identifier(value, "route reference")

    @field_validator("attempts", mode="before")
    @classmethod
    def _exact_attempts_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_field(value, info)

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "ReceiptRouteSnapshot must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            attempts_raw = data.get("attempts")
            if type(attempts_raw) is list:
                data["attempts"] = tuple(
                    item
                    if type(item) is _RouteAttemptSnapshot
                    else _RouteAttemptSnapshot.model_validate_json(
                        json.dumps(item)
                    )
                    for item in attempts_raw
                )
            for field_name, model_type in (
                ("selected_candidate", _CandidateSnapshot),
                ("allocated_budget", _BudgetSnapshot),
            ):
                raw = data.get(field_name)
                if type(raw) is dict:
                    data[field_name] = model_type.model_validate_json(
                        json.dumps(raw)
                    )
            return data
        if type(data.get("attempts")) is not tuple:
            raise ValueError(
                "attempts must be an exact tuple at raw validation"
            )
        for item in data.get("attempts", ()):
            if type(item) is not _RouteAttemptSnapshot:
                raise ValueError(
                    "attempts items must be exact "
                    "_RouteAttemptSnapshot instances"
                )
        for field_name, model_type in (
            ("selected_candidate", _CandidateSnapshot),
            ("allocated_budget", _BudgetSnapshot),
        ):
            raw = data.get(field_name)
            if raw is not None and type(raw) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact {model_type.__name__} "
                    "instance or None"
                )
        return data

    @model_validator(mode="after")
    def _validate_route_grammar(self) -> "ReceiptRouteSnapshot":
        if self.outcome == "selected":
            if self.block_reason is not None:
                raise ValueError(
                    "selected outcome must not carry a block_reason"
                )
            if self.matched_rule_id is None or self.matched_tier_alias is None:
                raise ValueError(
                    "selected outcome requires matched rule and tier alias"
                )
            if not self.attempts:
                raise ValueError(
                    "selected outcome requires at least one attempt"
                )
            if self.attempts[-1].reason != "selected":
                raise ValueError(
                    "selected outcome requires a final selected attempt"
                )
            if any(
                attempt.reason == "selected" for attempt in self.attempts[:-1]
            ):
                raise ValueError(
                    "only the final attempt may be selected"
                )
            if self.selected_candidate is None:
                raise ValueError(
                    "selected outcome requires a selected candidate"
                )
            if (
                self.attempts[-1].candidate_id
                != self.selected_candidate.candidate_id
            ):
                raise ValueError(
                    "selected outcome final attempt must match "
                    "the selected candidate"
                )
            if self.allocated_budget is None:
                raise ValueError(
                    "selected outcome requires an allocated budget"
                )
        else:
            if self.block_reason is None:
                raise ValueError("blocked outcome requires a block_reason")
            if self.selected_candidate is not None:
                raise ValueError(
                    "blocked outcome must not carry a selected candidate"
                )
            if self.block_reason == "no_eligible_candidate":
                if (
                    self.matched_rule_id is None
                    or self.matched_tier_alias is None
                ):
                    raise ValueError(
                        "no_eligible_candidate requires matched rule "
                        "and tier alias"
                    )
                if not self.attempts:
                    raise ValueError(
                        "no_eligible_candidate requires rejection attempts"
                    )
                if any(
                    attempt.reason == "selected" for attempt in self.attempts
                ):
                    raise ValueError(
                        "blocked attempts must never be selected"
                    )
                if self.allocated_budget is None:
                    raise ValueError(
                        "no_eligible_candidate requires an allocated budget"
                    )
            else:
                if (
                    self.matched_rule_id is not None
                    or self.matched_tier_alias is not None
                ):
                    raise ValueError(
                        "routing_disabled/no_matching_rule must not carry "
                        "matched rule fields"
                    )
                if self.attempts:
                    raise ValueError(
                        "routing_disabled/no_matching_rule must not carry "
                        "attempts"
                    )
                if self.allocated_budget is not None:
                    raise ValueError(
                        "routing_disabled/no_matching_rule must not carry "
                        "an allocated budget"
                    )
        candidate_ids = tuple(
            attempt.candidate_id for attempt in self.attempts
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("attempt candidate_id must be unique")
        if self.selected_candidate is not None:
            if _boundary_rank(
                self.selected_candidate.required_provider_boundary
            ) > _boundary_rank(self.request_provider_boundary):
                raise ValueError(
                    "selected candidate required boundary must not exceed "
                    "the request provider boundary"
                )
        if (self.matched_rule_id is None) != (
            self.allocated_budget is None
        ):
            raise ValueError(
                "allocated_budget must be present exactly when a rule matched"
            )
        return self

    @classmethod
    def from_decision(cls, decision: ModelRouteDecision) -> "ReceiptRouteSnapshot":
        if type(decision) is not ModelRouteDecision:
            raise TypeError(
                "decision must be an exact ModelRouteDecision instance"
            )
        rebuilt = ModelRouter.route(decision.policy, decision.request)
        if rebuilt != decision:
            raise ValueError(
                "decision must equal the recomputed route from its "
                "policy and request"
            )
        if decision.request.phase != "review":
            raise ValueError(
                "council route projection requires phase=review"
            )
        role = decision.request.agent_role
        if role not in _ROLE_KEYS:
            raise ValueError(
                "council route projection requires an exact council role"
            )
        risk = decision.request.risk_result
        data = {
            "schema_version": "v1",
            "decision_id": decision.decision_id,
            "policy_digest": decision.policy.policy_digest,
            "subject_digest": risk.classification.subject_digest,
            "risk_classification_id": risk.classification.classification_id,
            "role": role,
            "phase": "review",
            "effective_risk": decision.request.effective_risk,
            "priority": decision.request.priority,
            "request_provider_boundary": decision.request.provider_boundary,
            "outcome": decision.outcome,
            "block_reason": decision.block_reason,
            "matched_rule_id": decision.matched_rule_id,
            "matched_tier_alias": decision.matched_tier_alias,
            "attempts": tuple(
                _RouteAttemptSnapshot(
                    schema_version="v1",
                    candidate_id=attempt.candidate_id,
                    reason=attempt.reason,
                )
                for attempt in decision.attempts
            ),
            "selected_candidate": (
                None
                if decision.selected_candidate is None
                else _CandidateSnapshot(
                    schema_version="v1",
                    candidate_id=decision.selected_candidate.candidate_id,
                    provider_ref=decision.selected_candidate.provider_ref,
                    model_ref=decision.selected_candidate.model_ref,
                    required_provider_boundary=(
                        decision.selected_candidate.required_provider_boundary
                    ),
                )
            ),
            "allocated_budget": (
                None
                if decision.allocated_budget is None
                else _BudgetSnapshot(
                    schema_version="v1",
                    token_budget_cap=decision.allocated_budget.token_budget_cap,
                    cost_budget_cap_usd=(
                        decision.allocated_budget.cost_budget_cap_usd
                    ),
                )
            ),
        }
        return cls.model_validate(data)


class ReviewerExecutionFact(BaseModel):
    """Exact runtime facts for one Council reviewer role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    role: _ROLE
    result: _OUTCOME
    actual_provider: str | None = Field(default=None, min_length=1)
    actual_model: str | None = Field(default=None, min_length=1)
    actual_tool_grants: tuple[str, ...] = ()
    usage_status: _USAGE_STATUS
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost_usd: float | None = Field(
        default=None, strict=True, ge=0, allow_inf_nan=False
    )
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    latency_ms: int | None = Field(default=None, strict=True, ge=0)
    failure_code: _FAILURE_CODE | None = None

    @field_validator(
        "actual_provider",
        "actual_model",
        mode="before",
    )
    @classmethod
    def _optional_identifier_before(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if value is None:
            return value
        if type(value) is not str:
            raise ValueError(f"{info.field_name} must be an exact string")
        return _validate_optional_identifier(
            value, info.field_name
        )

    @field_validator("actual_tool_grants", mode="before")
    @classmethod
    def _exact_grants_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_field(value, info)

    @field_validator("actual_tool_grants")
    @classmethod
    def _validate_grants(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _validate_sorted_unique_strings(value, "actual_tool_grants")

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _reject_numeric_timestamps(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _exact_cost(cls, value: object) -> object:
        if value is None:
            return value
        if type(value) is not float:
            raise ValueError(
                "cost_usd must be an exact float or None "
                "(int/bool coercion is not allowed)"
            )
        return value

    @model_validator(mode="after")
    def _validate_runtime_fact_rules(self) -> "ReviewerExecutionFact":
        if self.result in _EXECUTED_RESULTS:
            if self.actual_provider is None or self.actual_model is None:
                raise ValueError(
                    "executed outcomes require actual provider and model"
                )
            if (
                self.started_at is None
                or self.completed_at is None
                or self.latency_ms is None
            ):
                raise ValueError(
                    "executed outcomes require started/completed timestamps "
                    "and latency"
                )
            if self.completed_at < self.started_at:
                raise ValueError(
                    "completed_at must not be earlier than started_at"
                )
            derived_latency = int(
                (self.completed_at - self.started_at).total_seconds() * 1000
            )
            if self.latency_ms != derived_latency:
                raise ValueError(
                    "latency_ms must equal the exact derived millisecond "
                    "latency"
                )
            if self.usage_status == "measured":
                if (
                    self.input_tokens is None
                    or self.output_tokens is None
                    or self.cost_usd is None
                ):
                    raise ValueError(
                        "measured usage requires all usage values"
                    )
            elif self.usage_status == "unavailable":
                if (
                    self.input_tokens is not None
                    or self.output_tokens is not None
                    or self.cost_usd is not None
                ):
                    raise ValueError(
                        "unavailable usage requires all usage values absent"
                    )
            else:
                raise ValueError(
                    "not_applicable usage is only for skipped/blocked outcomes"
                )
            if self.result == "success":
                if self.failure_code is not None:
                    raise ValueError("success must not carry a failure code")
            elif self.failure_code != _failure_code_for(self.result):
                raise ValueError(
                    "failure_code must match the executed outcome"
                )
        else:
            if (
                self.actual_provider is not None
                or self.actual_model is not None
                or self.actual_tool_grants
                or self.usage_status != "not_applicable"
                or self.input_tokens is not None
                or self.output_tokens is not None
                or self.cost_usd is not None
                or self.started_at is not None
                or self.completed_at is not None
                or self.latency_ms is not None
            ):
                raise ValueError(
                    "skipped/blocked facts must not carry provider, model, "
                    "tool grants, timestamps, latency, or usage values"
                )
            if self.failure_code != _failure_code_for(self.result):
                raise ValueError(
                    "failure_code must match the skipped/blocked outcome"
                )
        return self


class CouncilExecutionStep(BaseModel):
    """One canonical Council step: route projection, plan and runtime facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    sequence: int = Field(strict=True, ge=0)
    role: _ROLE
    route: ReceiptRouteSnapshot
    planned_tool_grants: tuple[str, ...]
    planned_timeout_seconds: int = Field(strict=True, gt=0)
    fact: ReviewerExecutionFact
    output_schema_ref: Literal["finding-output.v1"] = "finding-output.v1"
    output_schema_status: _OUTPUT_SCHEMA_STATUS
    output_digest: str | None = Field(default=None, min_length=1)

    @field_validator("planned_tool_grants", mode="before")
    @classmethod
    def _exact_planned_grants(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_field(value, info)

    @field_validator("planned_tool_grants")
    @classmethod
    def _validate_planned_grants(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _validate_sorted_unique_strings(value, "planned_tool_grants")

    @field_validator("planned_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if not 0 < value <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"planned_timeout_seconds must be > 0 and "
                f"<= {_MAX_TIMEOUT_SECONDS}"
            )
        return value

    @field_validator("output_digest")
    @classmethod
    def _validate_output_digest(cls, value: str | None) -> str | None:
        return _validate_sha256(value, "output_digest")

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "CouncilExecutionStep must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            for field_name, model_type in (
                ("route", ReceiptRouteSnapshot),
                ("fact", ReviewerExecutionFact),
            ):
                raw = data.get(field_name)
                if type(raw) is dict:
                    data[field_name] = model_type.model_validate_json(
                        json.dumps(raw)
                    )
            grants_raw = data.get("planned_tool_grants")
            if type(grants_raw) is list:
                data["planned_tool_grants"] = tuple(grants_raw)
            return data
        if type(data.get("route")) is not ReceiptRouteSnapshot:
            raise ValueError(
                "route must be an exact ReceiptRouteSnapshot instance"
            )
        if type(data.get("fact")) is not ReviewerExecutionFact:
            raise ValueError(
                "fact must be an exact ReviewerExecutionFact instance"
            )
        if type(data.get("planned_tool_grants")) is not tuple:
            raise ValueError(
                "planned_tool_grants must be an exact tuple at raw "
                "validation"
            )
        return data

    @model_validator(mode="after")
    def _validate_step_cross_fields(self) -> "CouncilExecutionStep":
        if _role_index(self.role) != self.sequence:
            raise ValueError(
                "sequence must follow the canonical role order"
            )
        if self.route.role != self.role:
            raise ValueError("route role must equal step role")
        if self.fact.role != self.role:
            raise ValueError("fact role must equal step role")
        if self.route.outcome == "blocked":
            if self.fact.result != "blocked":
                raise ValueError(
                    "blocked route requires a blocked runtime fact"
                )
        else:
            if self.fact.result == "blocked":
                raise ValueError(
                    "selected route must not claim blocked; an explicit "
                    "non-blocked outcome is required"
                )
            if self.fact.result in _EXECUTED_RESULTS:
                selected = self.route.selected_candidate
                if selected is None:
                    raise ValueError(
                        "executed selected route requires a selected candidate"
                    )
                if (
                    self.fact.actual_provider != selected.provider_ref
                    or self.fact.actual_model != selected.model_ref
                ):
                    raise ValueError(
                        "executed fact provider/model must equal the "
                        "selected candidate"
                    )
                if self.fact.actual_tool_grants != self.planned_tool_grants:
                    raise ValueError(
                        "executed actual tool grants must equal the planned "
                        "tool allowlist"
                    )
            elif self.fact.actual_tool_grants:
                raise ValueError(
                    "skipped facts must not carry actual tool grants"
                )
        if self.fact.result == "success":
            if (
                self.output_schema_status != "valid"
                or self.output_digest is None
            ):
                raise ValueError(
                    "success requires output schema valid and an output digest"
                )
        elif self.fact.result in _EXECUTED_NON_SUCCESS:
            if self.output_schema_status == "synthetic":
                if self.output_digest is None:
                    raise ValueError(
                        "synthetic schema status requires an output digest"
                    )
            elif self.output_schema_status == "not_produced":
                if self.output_digest is not None:
                    raise ValueError(
                        "not_produced schema status must not carry an "
                        "output digest"
                    )
            else:
                raise ValueError(
                    "executed non-success steps must be synthetic or "
                    "not_produced"
                )
        else:
            if (
                self.output_schema_status != "not_produced"
                or self.output_digest is not None
            ):
                raise ValueError(
                    "skipped/blocked steps must be not_produced without "
                    "an output digest"
                )
        return self


class CouncilTopologySnapshot(BaseModel):
    """Planned versus actual Council topology with derived completion order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    topology_version: Literal["parallel-isolated-v1"] = "parallel-isolated-v1"
    planned_roles: tuple[_ROLE, ...]
    start_barrier_roles: tuple[_ROLE, ...]
    planned_dependencies: tuple[tuple[str, str], ...]
    required_roles: tuple[_ROLE, ...]
    actual_state: _ACTUAL_STATE
    executed_roles: tuple[_ROLE, ...]
    skipped_or_blocked_roles: tuple[_ROLE, ...]
    completion_order: tuple[_ROLE, ...]
    actual_dependencies: tuple[tuple[str, str], ...]

    @field_validator(
        "planned_roles",
        "start_barrier_roles",
        "planned_dependencies",
        "required_roles",
        "executed_roles",
        "skipped_or_blocked_roles",
        "completion_order",
        "actual_dependencies",
        mode="before",
    )
    @classmethod
    def _exact_tuples(cls, value: object, info: ValidationInfo) -> object:
        return _exact_tuple_field(value, info)

    @field_validator(
        "planned_roles",
        "start_barrier_roles",
        "required_roles",
        "executed_roles",
        "skipped_or_blocked_roles",
    )
    @classmethod
    def _role_tuples(
        cls,
        value: tuple[_ROLE, ...],
        info: ValidationInfo,
    ) -> tuple[_ROLE, ...]:
        if tuple(value) != tuple(sorted(value, key=_role_index)):
            raise ValueError(
                f"{info.field_name} must be canonical-sorted by role"
            )
        if len(set(value)) != len(value):
            raise ValueError(f"{info.field_name} must be unique")
        return value

    @field_validator("completion_order")
    @classmethod
    def _completion_order_roles(
        cls, value: tuple[_ROLE, ...]
    ) -> tuple[_ROLE, ...]:
        if not set(value) <= _ROLE_KEYS:
            raise ValueError(
                "completion_order must reference council roles"
            )
        if len(set(value)) != len(value):
            raise ValueError("completion_order must be unique")
        return value

    @model_validator(mode="after")
    def _validate_topology(self) -> "CouncilTopologySnapshot":
        if self.planned_roles != _ROLE_ORDER:
            raise ValueError(
                "planned_roles must be exactly intent, architecture, "
                "operability in canonical order"
            )
        if self.start_barrier_roles != _ROLE_ORDER:
            raise ValueError(
                "start_barrier_roles must be exactly intent, architecture, "
                "operability in canonical order"
            )
        if self.planned_dependencies:
            raise ValueError(
                "planned_dependencies must be empty for parallel-isolated-v1"
            )
        if self.actual_dependencies:
            raise ValueError(
                "actual_dependencies must be empty for parallel-isolated-v1"
            )
        if not set(self.required_roles) <= _ROLE_KEYS:
            raise ValueError("required_roles must reference council roles")
        executed = set(self.executed_roles)
        skipped = set(self.skipped_or_blocked_roles)
        if executed & skipped:
            raise ValueError(
                "executed and skipped/blocked role sets must be disjoint"
            )
        if executed | skipped != set(_ROLE_ORDER):
            raise ValueError(
                "executed and skipped/blocked roles must partition all "
                "three council roles"
            )
        if len(self.completion_order) != len(executed):
            raise ValueError(
                "completion_order must contain exactly the executed roles"
            )
        if set(self.completion_order) != executed:
            raise ValueError(
                "completion_order must contain exactly the executed roles"
            )
        expected_state = (
            "completed"
            if len(executed) == 3
            else ("not_started" if not executed else "interrupted")
        )
        if self.actual_state != expected_state:
            raise ValueError(
                "actual_state must be derived from executed roles"
            )
        return self


def _derive_topology(
    plan: CouncilPlan,
    steps: tuple[CouncilExecutionStep, ...],
) -> CouncilTopologySnapshot:
    executed = tuple(
        role
        for role in _ROLE_ORDER
        if steps[_role_index(role)].fact.result in _EXECUTED_RESULTS
    )
    skipped = tuple(
        role
        for role in _ROLE_ORDER
        if steps[_role_index(role)].fact.result in _NOT_EXECUTED_RESULTS
    )
    completion_order = tuple(
        sorted(
            executed,
            key=lambda role: (
                steps[_role_index(role)].fact.completed_at,
                _role_index(role),
            ),
        )
    )
    required = plan.inputs[0].risk_result.classification.required_reviewers
    return CouncilTopologySnapshot.model_validate(
        {
            "schema_version": "v1",
            "topology_version": "parallel-isolated-v1",
            "planned_roles": _ROLE_ORDER,
            "start_barrier_roles": _ROLE_ORDER,
            "planned_dependencies": (),
            "required_roles": required,
            "actual_state": (
                "completed"
                if len(executed) == 3
                else ("not_started" if not executed else "interrupted")
            ),
            "executed_roles": executed,
            "skipped_or_blocked_roles": skipped,
            "completion_order": completion_order,
            "actual_dependencies": (),
        }
    )


def _derive_overall_result(
    topology: CouncilTopologySnapshot,
    steps: tuple[CouncilExecutionStep, ...],
) -> str:
    required = set(topology.required_roles)
    if any(step.route.outcome == "blocked" for step in steps):
        return "blocked"
    if any(
        step.role in required
        and step.fact.result not in _EXECUTED_RESULTS
        for step in steps
    ):
        return "blocked"
    if any(step.fact.result == "cancelled" for step in steps):
        return "cancelled"
    if any(
        step.fact.result
        in ("failure", "timeout", "budget_exceeded", "schema_invalid")
        for step in steps
    ):
        return "failure"
    if all(step.fact.result == "success" for step in steps):
        return "success"
    return "partial"


def _derive_usage(
    steps: tuple[CouncilExecutionStep, ...],
) -> tuple[str, tuple[int, int, float] | None]:
    executed = [
        step for step in steps if step.fact.result in _EXECUTED_RESULTS
    ]
    if not executed:
        return "not_applicable", None
    statuses = {step.fact.usage_status for step in executed}
    if statuses == {"measured"}:
        totals = (
            sum(step.fact.input_tokens for step in executed),
            sum(step.fact.output_tokens for step in executed),
            sum(step.fact.cost_usd for step in executed),
        )
        return "measured", totals
    if statuses == {"unavailable"}:
        return "unavailable", None
    return "partial", None


def _gate_result(fact_result: str) -> str:
    if fact_result in ("budget_exceeded", "schema_invalid"):
        return "failure"
    return fact_result


def _gate_schema_status(
    rich_status: str, gate_result: str
) -> str:
    if rich_status == "valid":
        return "valid"
    if rich_status == "not_produced":
        return "not_produced"
    if gate_result in ("timeout", "cancelled"):
        return "not_produced"
    return "invalid"


def _gate_fallback_reason(
    route: ReceiptRouteSnapshot,
) -> str | None:
    reasons = tuple(
        attempt.reason
        for attempt in route.attempts
        if attempt.reason != "selected"
    )
    if not reasons:
        return None
    return ";".join(reasons)


def _derive_gate_receipt(
    *,
    run_id: str,
    subject_digest: str,
    plan: CouncilPlan,
    steps: tuple[CouncilExecutionStep, ...],
    overall_result: str,
    usage_status: str,
    totals: tuple[int, int, float] | None,
    run_started_at: AwareDatetime,
    recorded_at: AwareDatetime,
    elapsed_ms: int,
    plan_digest: str,
    result_digest: str | None,
    topology: CouncilTopologySnapshot,
    result: CouncilRunResult | None,
) -> tuple[str, ExecutionReceipt]:
    rich_body = {
        "schema_version": "v1",
        "receipt_version": "council-execution-receipt-v1",
        "run_id": run_id,
        "subject_digest": subject_digest,
        "plan": plan.model_dump(mode="json"),
        "result": None if result is None else result.model_dump(mode="json"),
        "plan_digest": plan_digest,
        "result_digest": result_digest,
        "topology": topology.model_dump(mode="json"),
        "steps": [step.model_dump(mode="json") for step in steps],
        "overall_result": overall_result,
        "usage_status": usage_status,
        "input_tokens": None if totals is None else totals[0],
        "output_tokens": None if totals is None else totals[1],
        "cost_usd": None if totals is None else totals[2],
        "run_started_at": run_started_at.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "elapsed_ms": elapsed_ms,
    }
    gate_receipt_id = "gate_" + hashlib.sha256(
        _canonical_json_bytes(rich_body)
    ).hexdigest()
    gate_steps = tuple(
        ExecutionStep.model_validate(
            {
                "sequence": index,
                "planned_role": role,
                "actual_role": (
                    role
                    if step.fact.result in _EXECUTED_RESULTS
                    else None
                ),
                "model_ref": step.fact.actual_model,
                "provider": step.fact.actual_provider,
                "tool_grants": step.fact.actual_tool_grants,
                "routing_rule": (
                    step.route.matched_rule_id
                    if step.route.matched_rule_id is not None
                    else step.route.block_reason
                ),
                "fallback_reason": _gate_fallback_reason(step.route),
                "token_budget": (
                    None
                    if step.route.allocated_budget is None
                    else step.route.allocated_budget.token_budget_cap
                ),
                "timeout_seconds": plan.inputs[index].timeout_seconds,
                "result": _gate_result(step.fact.result),
                "schema_status": _gate_schema_status(
                    step.output_schema_status, _gate_result(step.fact.result)
                ),
            }
        )
        for index, (role, step) in enumerate(zip(_ROLE_ORDER, steps))
    )
    gate_receipt = ExecutionReceipt.model_validate(
        {
            "schema_version": "v1",
            "receipt_id": gate_receipt_id,
            "run_id": run_id,
            "subject_digest": subject_digest,
            "steps": gate_steps,
            "overall_result": overall_result,
            "input_tokens": 0 if totals is None else totals[0],
            "output_tokens": 0 if totals is None else totals[1],
            "cost_usd": 0.0 if totals is None else totals[2],
            "started_at": run_started_at,
            "completed_at": recorded_at,
        }
    )
    receipt_id = "council_receipt_" + hashlib.sha256(
        _canonical_json_bytes(
            {**rich_body, "gate_receipt": gate_receipt.model_dump(mode="json")}
        )
    ).hexdigest()
    return receipt_id, gate_receipt


def _derive_receipt_fields(
    *,
    run_id: str,
    subject_digest: str,
    plan: CouncilPlan,
    result: CouncilRunResult | None,
    steps: tuple[CouncilExecutionStep, ...],
    run_started_at: AwareDatetime,
    recorded_at: AwareDatetime,
) -> dict[str, object]:
    if len(steps) != 3:
        raise ValueError("steps must contain exactly three Council steps")
    plan_subject = plan.inputs[0].subject.subject_digest
    if subject_digest != plan_subject:
        raise ValueError(
            "subject_digest must bind to the exact plan subject"
        )
    for index, step in enumerate(steps):
        role = _ROLE_ORDER[index]
        if step.sequence != index or step.role != role:
            raise ValueError(
                "steps must be in canonical role order with exact sequences"
            )
        reviewer_input = plan.inputs[index]
        if step.planned_tool_grants != reviewer_input.tool_allowlist:
            raise ValueError(
                "planned tool grants must equal the plan tool allowlist"
            )
        if step.planned_timeout_seconds != reviewer_input.timeout_seconds:
            raise ValueError(
                "planned timeout must equal the plan input timeout"
            )
        if (
            step.route.subject_digest
            != reviewer_input.subject.subject_digest
        ):
            raise ValueError(
                "route subject digest must bind to the plan subject"
            )
        if (
            step.route.risk_classification_id
            != reviewer_input.risk_result.classification.classification_id
        ):
            raise ValueError(
                "route risk classification must bind to the plan risk result"
            )
        allocated = step.route.allocated_budget
        if allocated is not None:
            if (
                reviewer_input.token_budget is None
                or reviewer_input.token_budget != allocated.token_budget_cap
            ):
                raise ValueError(
                    "selected route token budget must equal the plan "
                    "token budget"
                )
            if (
                reviewer_input.cost_budget_usd is None
                or reviewer_input.cost_budget_usd
                != allocated.cost_budget_cap_usd
            ):
                raise ValueError(
                    "selected route cost budget must equal the plan "
                    "cost budget"
                )
    if result is not None:
        if result.plan != plan:
            raise ValueError(
                "Council result must bind to the exact plan"
            )
        for index, step in enumerate(steps):
            output = result.outputs[index]
            if output.input != plan.inputs[index]:
                raise ValueError(
                    "every Council output must bind to its exact plan input"
                )
            if step.fact.result != output.outcome:
                raise ValueError(
                    "runtime fact result must equal the bound output outcome"
                )
            if step.fact.completed_at != output.completed_at:
                raise ValueError(
                    "runtime fact completed time must equal the bound "
                    "output completed time"
                )
            expected_digest = _model_digest(output)
            if step.output_digest != expected_digest:
                raise ValueError(
                    "output digest must equal the derived bound output digest"
                )
            expected_status = (
                "valid" if output.outcome == "success" else "synthetic"
            )
            if step.output_schema_status != expected_status:
                raise ValueError(
                    "output schema status must be valid for success and "
                    "synthetic for non-success bound outputs"
                )
    else:
        for step in steps:
            if step.fact.result == "success":
                raise ValueError(
                    "success is forbidden without a bound Council result"
                )
            if (
                step.output_schema_status != "not_produced"
                or step.output_digest is not None
            ):
                raise ValueError(
                    "without a bound Council result, schema status must be "
                    "not_produced and digest must be None"
                )
    if recorded_at < run_started_at:
        raise ValueError(
            "recorded_at must not be earlier than run_started_at"
        )
    elapsed_ms = int((recorded_at - run_started_at).total_seconds() * 1000)
    for step in steps:
        fact = step.fact
        if fact.started_at is not None:
            if fact.started_at < run_started_at:
                raise ValueError(
                    "every executed fact must start at or after "
                    "run_started_at"
                )
            if fact.completed_at is None or fact.completed_at > recorded_at:
                raise ValueError(
                    "every executed fact must complete at or before "
                    "recorded_at"
                )
    topology = _derive_topology(plan, steps)
    overall_result = _derive_overall_result(topology, steps)
    usage_status, totals = _derive_usage(steps)
    plan_digest = _model_digest(plan)
    result_digest = None if result is None else _model_digest(result)
    receipt_id, gate_receipt = _derive_gate_receipt(
        run_id=run_id,
        subject_digest=subject_digest,
        plan=plan,
        steps=steps,
        overall_result=overall_result,
        usage_status=usage_status,
        totals=totals,
        run_started_at=run_started_at,
        recorded_at=recorded_at,
        elapsed_ms=elapsed_ms,
        plan_digest=plan_digest,
        result_digest=result_digest,
        topology=topology,
        result=result,
    )
    return {
        "plan_digest": plan_digest,
        "result_digest": result_digest,
        "topology": topology,
        "overall_result": overall_result,
        "usage_status": usage_status,
        "input_tokens": None if totals is None else totals[0],
        "output_tokens": None if totals is None else totals[1],
        "cost_usd": None if totals is None else totals[2],
        "elapsed_ms": elapsed_ms,
        "gate_receipt": gate_receipt,
        "receipt_id": receipt_id,
    }


class CouncilExecutionReceipt(BaseModel):
    """Canonical rich Council execution receipt with derived Gate projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    receipt_version: Literal["council-execution-receipt-v1"] = (
        "council-execution-receipt-v1"
    )
    receipt_id: str
    run_id: str = Field(min_length=1)
    subject_digest: str
    plan: CouncilPlan
    result: CouncilRunResult | None = None
    plan_digest: str
    result_digest: str | None = None
    topology: CouncilTopologySnapshot
    steps: tuple[CouncilExecutionStep, ...]
    overall_result: _OVERALL_RESULT
    usage_status: _USAGE_AGGREGATE
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost_usd: float | None = Field(
        default=None, strict=True, ge=0, allow_inf_nan=False
    )
    run_started_at: AwareDatetime
    recorded_at: AwareDatetime
    elapsed_ms: int = Field(strict=True, ge=0)
    gate_receipt: ExecutionReceipt

    @field_validator("receipt_id", "run_id")
    @classmethod
    def _nonblank_ids(cls, value: str) -> str:
        return _require_nonblank(value, "receipt/run id")

    @field_validator("subject_digest")
    @classmethod
    def _subject_digest(cls, value: str) -> str:
        return _validate_sha256(value, "subject_digest")

    @field_validator("steps", mode="before")
    @classmethod
    def _exact_steps_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_field(value, info)

    @field_validator("run_started_at", "recorded_at", mode="before")
    @classmethod
    def _reject_numeric_timestamps(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _exact_cost(cls, value: object) -> object:
        if value is None:
            return value
        if type(value) is not float:
            raise ValueError(
                "cost_usd must be an exact float or None"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "CouncilExecutionReceipt must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            if type(data.get("plan")) is dict:
                data["plan"] = CouncilPlan.model_validate_json(
                    json.dumps(data["plan"])
                )
            result_raw = data.get("result")
            if result_raw is not None and type(result_raw) is dict:
                data["result"] = CouncilRunResult.model_validate_json(
                    json.dumps(result_raw)
                )
            steps_raw = data.get("steps")
            if type(steps_raw) is list:
                data["steps"] = tuple(
                    item
                    if type(item) is CouncilExecutionStep
                    else CouncilExecutionStep.model_validate_json(
                        json.dumps(item)
                    )
                    for item in steps_raw
                )
            if type(data.get("topology")) is dict:
                data["topology"] = (
                    CouncilTopologySnapshot.model_validate_json(
                        json.dumps(data["topology"])
                    )
                )
            if type(data.get("gate_receipt")) is dict:
                data["gate_receipt"] = ExecutionReceipt.model_validate_json(
                    json.dumps(data["gate_receipt"])
                )
            return data
        if type(data.get("plan")) is not CouncilPlan:
            raise ValueError(
                "plan must be an exact CouncilPlan instance"
            )
        result = data.get("result")
        if result is not None and type(result) is not CouncilRunResult:
            raise ValueError(
                "result must be an exact CouncilRunResult instance or None"
            )
        if type(data.get("steps")) is not tuple:
            raise ValueError(
                "steps must be an exact tuple at raw validation"
            )
        for item in data.get("steps", ()):
            if type(item) is not CouncilExecutionStep:
                raise ValueError(
                    "steps items must be exact CouncilExecutionStep instances"
                )
        if type(data.get("topology")) is not CouncilTopologySnapshot:
            raise ValueError(
                "topology must be an exact CouncilTopologySnapshot instance"
            )
        if type(data.get("gate_receipt")) is not ExecutionReceipt:
            raise ValueError(
                "gate_receipt must be an exact ExecutionReceipt instance"
            )
        return data

    @model_validator(mode="after")
    def _validate_derived_receipt(self) -> "CouncilExecutionReceipt":
        if len(self.steps) != 3:
            raise ValueError(
                "steps must contain exactly three canonical Council steps"
            )
        derived = _derive_receipt_fields(
            run_id=self.run_id,
            subject_digest=self.subject_digest,
            plan=self.plan,
            result=self.result,
            steps=self.steps,
            run_started_at=self.run_started_at,
            recorded_at=self.recorded_at,
        )
        for field_name, expected in derived.items():
            if getattr(self, field_name) != expected:
                raise ValueError(
                    f"{field_name} must equal the deterministic "
                    f"recomputation from the rich receipt"
                )
        return self


class CouncilReceiptBuilder:
    """Stateless builder that derives every projection from exact inputs."""

    @staticmethod
    def build(
        *,
        run_id: str,
        plan: CouncilPlan,
        routes: tuple[ModelRouteDecision, ModelRouteDecision, ModelRouteDecision],
        facts: tuple[ReviewerExecutionFact, ReviewerExecutionFact, ReviewerExecutionFact],
        result: CouncilRunResult | None,
        run_started_at: AwareDatetime,
        recorded_at: AwareDatetime,
    ) -> CouncilExecutionReceipt:
        _require_nonblank(run_id, "run_id")
        if type(plan) is not CouncilPlan:
            raise TypeError("plan must be an exact CouncilPlan instance")
        if type(routes) is not tuple or len(routes) != 3:
            raise ValueError(
                "routes must be an exact tuple of three ModelRouteDecision "
                "instances"
            )
        for route in routes:
            if type(route) is not ModelRouteDecision:
                raise TypeError(
                    "routes must contain exact ModelRouteDecision instances"
                )
        if type(facts) is not tuple or len(facts) != 3:
            raise ValueError(
                "facts must be an exact tuple of three "
                "ReviewerExecutionFact instances"
            )
        for fact in facts:
            if type(fact) is not ReviewerExecutionFact:
                raise TypeError(
                    "facts must contain exact ReviewerExecutionFact instances"
                )
        if result is not None and type(result) is not CouncilRunResult:
            raise TypeError(
                "result must be an exact CouncilRunResult instance or None"
            )
        if (
            CouncilPlan.model_validate_json(plan.model_dump_json()) != plan
        ):
            raise ValueError(
                "plan must survive public JSON round-trip validation"
            )
        for fact in facts:
            if (
                ReviewerExecutionFact.model_validate_json(
                    fact.model_dump_json()
                )
                != fact
            ):
                raise ValueError(
                    "facts must survive public JSON round-trip validation"
                )
        if result is not None:
            if (
                CouncilRunResult.model_validate_json(
                    result.model_dump_json()
                )
                != result
            ):
                raise ValueError(
                    "Council result must survive public JSON round-trip "
                    "validation"
                )
        if tuple(fact.role for fact in facts) != _ROLE_ORDER:
            raise ValueError(
                "facts must be in canonical role order"
            )
        for index, route in enumerate(routes):
            expected_role = _ROLE_ORDER[index]
            if route.request.phase != "review":
                raise ValueError(
                    "council routes must use phase=review"
                )
            if route.request.agent_role != expected_role:
                raise ValueError(
                    "routes must be in canonical role order"
                )
            rebuilt = ModelRouter.route(route.policy, route.request)
            if rebuilt != route:
                raise ValueError(
                    "route decision must equal the recomputed route from "
                    "its policy and request"
                )
            if route.request.risk_result != plan.inputs[0].risk_result:
                raise ValueError(
                    "every route must bind to the same plan risk result"
                )
            if (
                route.request.risk_result.classification.subject_digest
                != plan.inputs[0].subject.subject_digest
            ):
                raise ValueError(
                    "every route must bind to the same plan subject"
                )
        snapshots = tuple(
            ReceiptRouteSnapshot.from_decision(route) for route in routes
        )
        step_values = []
        for index, (role, snapshot, fact) in enumerate(
            zip(_ROLE_ORDER, snapshots, facts)
        ):
            reviewer_input = plan.inputs[index]
            if result is None:
                output_schema_status = "not_produced"
                output_digest = None
            else:
                output = result.outputs[index]
                output_schema_status = (
                    "valid"
                    if output.outcome == "success"
                    else "synthetic"
                )
                output_digest = _model_digest(output)
            step_values.append(
                {
                    "schema_version": "v1",
                    "sequence": index,
                    "role": role,
                    "route": snapshot,
                    "planned_tool_grants": reviewer_input.tool_allowlist,
                    "planned_timeout_seconds": reviewer_input.timeout_seconds,
                    "fact": fact,
                    "output_schema_ref": "finding-output.v1",
                    "output_schema_status": output_schema_status,
                    "output_digest": output_digest,
                }
            )
        steps = tuple(
            CouncilExecutionStep.model_validate(values)
            for values in step_values
        )
        subject_digest = plan.inputs[0].subject.subject_digest
        derived = _derive_receipt_fields(
            run_id=run_id,
            subject_digest=subject_digest,
            plan=plan,
            result=result,
            steps=steps,
            run_started_at=run_started_at,
            recorded_at=recorded_at,
        )
        return CouncilExecutionReceipt.model_validate(
            {
                "schema_version": "v1",
                "receipt_version": "council-execution-receipt-v1",
                "receipt_id": derived["receipt_id"],
                "run_id": run_id,
                "subject_digest": subject_digest,
                "plan": plan,
                "result": result,
                "plan_digest": derived["plan_digest"],
                "result_digest": derived["result_digest"],
                "topology": derived["topology"],
                "steps": steps,
                "overall_result": derived["overall_result"],
                "usage_status": derived["usage_status"],
                "input_tokens": derived["input_tokens"],
                "output_tokens": derived["output_tokens"],
                "cost_usd": derived["cost_usd"],
                "run_started_at": run_started_at,
                "recorded_at": recorded_at,
                "elapsed_ms": derived["elapsed_ms"],
                "gate_receipt": derived["gate_receipt"],
            }
        )
