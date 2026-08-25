"""V2-P4-06 Model Routing v1.

Deterministic, opt-in, versioned model routing. A policy maps exact request
dimensions to an abstract tier alias; candidates inside the matched alias are
scanned in declared order and every rejection reason is recorded. Runtime
availability and the provider allowlist are exact caller facts and are never
probed here.

This module is routing-only: it performs no provider/model calls, no
environment/config reads, no filesystem/network/subprocess/persistence
access, no time/random usage, and it does not decide execution topology or
acceptance. A ``selected`` candidate is only a routing outcome.
"""

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
)


_PHASE = Literal["extraction", "review", "adjudication", "synthesis"]
_AGENT_ROLE = Literal["intent", "architecture", "operability"]
_RISK_LEVEL = Literal["low", "medium", "high", "critical"]
_PRIORITY = Literal["low", "normal", "high", "critical"]
_PROVIDER_BOUNDARY = Literal["local-only", "approved-provider", "any"]
_ATTEMPT_REASON = Literal[
    "candidate_unavailable",
    "provider_not_allowed",
    "provider_boundary_denied",
    "selected",
]
_BLOCK_REASON = Literal[
    "routing_disabled",
    "no_matching_rule",
    "no_eligible_candidate",
]

_MATCH_DIMENSIONS = (
    "phase",
    "agent_role",
    "effective_risk",
    "priority",
    "provider_boundary",
    "task_role",
)
_BOUNDARY_RANK = {
    "local-only": 0,
    "approved-provider": 1,
    "any": 2,
}
_P3_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_REQUEST_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


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


def _require_nonblank(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank or whitespace-only")
    return value


class ModelCandidate(BaseModel):
    """Stable concrete model candidate with an exact provider boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    candidate_id: StrictStr = Field(min_length=1, max_length=128)
    provider_ref: StrictStr = Field(min_length=1, max_length=128)
    model_ref: StrictStr = Field(min_length=1, max_length=128)
    required_provider_boundary: _PROVIDER_BOUNDARY

    @field_validator("candidate_id", "provider_ref", "model_ref")
    @classmethod
    def _nonblank_ids(cls, value: str) -> str:
        return _require_nonblank(value, "candidate/provider/model references")


class ModelTierAlias(BaseModel):
    """Abstract tier alias with an ordered, non-empty candidate tuple."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    alias: StrictStr = Field(min_length=1, max_length=64)
    candidates: tuple[ModelCandidate, ...]

    @field_validator("candidates", mode="before")
    @classmethod
    def _exact_candidates_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if info.mode == "json":
            if type(value) is not list:
                raise ValueError(
                    "candidates must be an array in JSON mode"
                )
            return tuple(
                item
                if type(item) is ModelCandidate
                else ModelCandidate.model_validate_json(json.dumps(item))
                for item in value
            )
        if type(value) is not tuple:
            raise ValueError(
                "candidates must be an exact tuple at raw validation"
            )
        for item in value:
            if type(item) is not ModelCandidate:
                raise ValueError(
                    "candidates must contain exact ModelCandidate instances"
                )
        return value

    @field_validator("alias")
    @classmethod
    def _alias_nonblank(cls, value: str) -> str:
        return _require_nonblank(value, "alias")

    @model_validator(mode="after")
    def _validate_candidate_tuple(self) -> "ModelTierAlias":
        if not self.candidates:
            raise ValueError("candidates must be non-empty")
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError(
                "candidate_id must be unique within a tier alias"
            )
        pairs = [
            (item.provider_ref, item.model_ref) for item in self.candidates
        ]
        if len(pairs) != len(set(pairs)):
            raise ValueError(
                "(provider_ref, model_ref) pairs must be unique "
                "within a tier alias"
            )
        return self


class ModelRouteMatch(BaseModel):
    """Exact-equality match dimensions; all-None is the explicit catch-all."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    phase: _PHASE | None = None
    agent_role: _AGENT_ROLE | None = None
    effective_risk: _RISK_LEVEL | None = None
    priority: _PRIORITY | None = None
    provider_boundary: _PROVIDER_BOUNDARY | None = None
    task_role: StrictStr | None = Field(
        default=None, min_length=1, max_length=128
    )

    @field_validator("task_role")
    @classmethod
    def _task_role_nonblank(cls, value: str | None) -> str | None:
        if value is not None:
            return _require_nonblank(value, "task_role")
        return value


class ModelRouteTarget(BaseModel):
    """One existing tier alias plus strict ceiling budgets."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    tier_alias: StrictStr = Field(min_length=1, max_length=64)
    token_budget_cap: StrictInt = Field(ge=1)
    cost_budget_cap_usd: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("tier_alias")
    @classmethod
    def _tier_alias_nonblank(cls, value: str) -> str:
        return _require_nonblank(value, "tier_alias")

    @field_validator("cost_budget_cap_usd", mode="before")
    @classmethod
    def _exact_float_budget(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError(
                "cost_budget_cap_usd must be an exact float "
                "(int/bool coercion is not allowed)"
            )
        return value

    @model_validator(mode="after")
    def _finite_cost_budget(self) -> "ModelRouteTarget":
        if not math.isfinite(self.cost_budget_cap_usd):
            raise ValueError("cost_budget_cap_usd must be finite")
        return self


class ModelRouteRule(BaseModel):
    """Stable unique rule with exact match and target."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    rule_id: StrictStr = Field(min_length=1, max_length=128)
    match: ModelRouteMatch
    target: ModelRouteTarget

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("ModelRouteRule must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("match")) is dict:
                data["match"] = ModelRouteMatch.model_validate_json(
                    json.dumps(data["match"])
                )
            if type(data.get("target")) is dict:
                data["target"] = ModelRouteTarget.model_validate_json(
                    json.dumps(data["target"])
                )
            return data
        if type(data.get("match")) is not ModelRouteMatch:
            raise ValueError(
                "match must be an exact ModelRouteMatch instance"
            )
        if type(data.get("target")) is not ModelRouteTarget:
            raise ValueError(
                "target must be an exact ModelRouteTarget instance"
            )
        return data

    @field_validator("rule_id")
    @classmethod
    def _rule_id_nonblank(cls, value: str) -> str:
        return _require_nonblank(value, "rule_id")


def _policy_digest(policy: "ModelRoutingPolicy") -> str:
    body = policy.model_dump(mode="json", exclude={"policy_digest"})
    return _sha256_digest(_canonical_json_bytes(body))


class ModelRoutingPolicy(BaseModel):
    """Versioned opt-in routing policy with a derived anti-forgery digest."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    policy_version: Literal["routing-v1"] = "routing-v1"
    enabled: StrictBool
    rules: tuple[ModelRouteRule, ...]
    aliases: tuple[ModelTierAlias, ...]
    policy_digest: StrictStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _rebuild_derive_digest_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "ModelRoutingPolicy must validate from a mapping"
            )
        data = dict(data)
        if info.mode == "json":
            rules_raw = data.get("rules")
            if type(rules_raw) is list:
                data["rules"] = tuple(
                    item
                    if type(item) is ModelRouteRule
                    else ModelRouteRule.model_validate_json(json.dumps(item))
                    for item in rules_raw
                )
            aliases_raw = data.get("aliases")
            if type(aliases_raw) is list:
                data["aliases"] = tuple(
                    item
                    if type(item) is ModelTierAlias
                    else ModelTierAlias.model_validate_json(json.dumps(item))
                    for item in aliases_raw
                )
        else:
            if type(data.get("rules")) is not tuple:
                raise ValueError(
                    "rules must be an exact tuple at raw validation"
                )
            for item in data.get("rules", ()):
                if type(item) is not ModelRouteRule:
                    raise ValueError(
                        "rules must contain exact ModelRouteRule instances"
                    )
            if type(data.get("aliases")) is not tuple:
                raise ValueError(
                    "aliases must be an exact tuple at raw validation"
                )
            for item in data.get("aliases", ()):
                if type(item) is not ModelTierAlias:
                    raise ValueError(
                        "aliases must contain exact ModelTierAlias instances"
                    )
        body = {
            key: value
            for key, value in data.items()
            if key != "policy_digest"
        }
        body.setdefault("schema_version", "v1")
        body.setdefault("policy_version", "routing-v1")
        expected_digest = _sha256_digest(
            _canonical_json_bytes(_jsonable(body))
        )
        provided = data.get("policy_digest")
        if provided is not None:
            if (
                type(provided) is not str
                or _SHA256_RE.fullmatch(provided) is None
            ):
                raise ValueError(
                    "policy_digest must be sha256:<64 lowercase hex>"
                )
            if provided != expected_digest:
                raise ValueError(
                    "policy_digest must equal the derived policy digest"
                )
        data["policy_digest"] = expected_digest
        return data

    @field_validator("policy_digest")
    @classmethod
    def _policy_digest_format(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(
                "policy_digest must be sha256:<64 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _validate_policy_semantics(self) -> "ModelRoutingPolicy":
        alias_names = tuple(alias.alias for alias in self.aliases)
        if len(set(alias_names)) != len(alias_names):
            raise ValueError("aliases must be unique by alias")
        if tuple(alias_names) != tuple(sorted(alias_names)):
            raise ValueError(
                "aliases must be canonical-sorted ascending by alias"
            )
        candidate_ids = []
        candidate_pairs = []
        for alias in self.aliases:
            for candidate in alias.candidates:
                candidate_ids.append(candidate.candidate_id)
                candidate_pairs.append(
                    (candidate.provider_ref, candidate.model_ref)
                )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError(
                "candidate_id must be unique across all aliases"
            )
        if len(candidate_pairs) != len(set(candidate_pairs)):
            raise ValueError(
                "(provider_ref, model_ref) pairs must be unique "
                "across all aliases"
            )
        alias_set = set(alias_names)
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule_id must be unique")
        for rule in self.rules:
            if rule.target.tier_alias not in alias_set:
                raise ValueError(
                    f"rule {rule.rule_id!r} references unknown "
                    f"tier alias {rule.target.tier_alias!r}"
                )
        for index, earlier in enumerate(self.rules):
            for later in self.rules[index + 1 :]:
                if earlier.match == later.match:
                    raise ValueError(
                        "exact duplicate matches are not allowed"
                    )
                if _match_covers(earlier.match, later.match):
                    raise ValueError(
                        f"rule {later.rule_id!r} is unreachable: "
                        f"earlier rule {earlier.rule_id!r} has a broader "
                        f"or equal match"
                    )
        return self


class ModelRouteBudget(BaseModel):
    """Exact strict ceilings for one route (not a pricing prediction)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    token_budget_cap: StrictInt = Field(ge=1)
    cost_budget_cap_usd: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("cost_budget_cap_usd", mode="before")
    @classmethod
    def _exact_float_budget(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError(
                "cost_budget_cap_usd must be an exact float "
                "(int/bool coercion is not allowed)"
            )
        return value

    @model_validator(mode="after")
    def _finite_cost_budget(self) -> "ModelRouteBudget":
        if not math.isfinite(self.cost_budget_cap_usd):
            raise ValueError("cost_budget_cap_usd must be finite")
        return self


class ModelRouteRequest(BaseModel):
    """Exact caller-fact routing request bound to one P3 risk result."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    risk_result: RiskClassificationResult
    phase: _PHASE
    agent_role: _AGENT_ROLE | None = None
    task_role: StrictStr | None = Field(
        default=None, min_length=1, max_length=128
    )
    effective_risk: _RISK_LEVEL
    risk_upgrade_reason: StrictStr | None = Field(
        default=None, min_length=1, max_length=512
    )
    priority: _PRIORITY
    provider_boundary: _PROVIDER_BOUNDARY
    available_candidate_ids: tuple[StrictStr, ...]
    allowed_provider_refs: tuple[StrictStr, ...]
    budget: ModelRouteBudget

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "ModelRouteRequest must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            if type(data.get("risk_result")) is dict:
                raise ValueError(
                    "RiskClassificationResult has no JSON rebuild path; "
                    "validate ModelRouteRequest in Python mode with an "
                    "exact RiskClassificationResult instance"
                )
            if type(data.get("budget")) is dict:
                data["budget"] = ModelRouteBudget.model_validate_json(
                    json.dumps(data["budget"])
                )
            for field_name in (
                "available_candidate_ids",
                "allowed_provider_refs",
            ):
                raw = data.get(field_name)
                if type(raw) is list:
                    data[field_name] = tuple(raw)
            return data
        if type(data.get("risk_result")) is not RiskClassificationResult:
            raise ValueError(
                "risk_result must be an exact "
                "RiskClassificationResult instance"
            )
        if type(data.get("budget")) is not ModelRouteBudget:
            raise ValueError(
                "budget must be an exact ModelRouteBudget instance"
            )
        for field_name in (
            "available_candidate_ids",
            "allowed_provider_refs",
        ):
            value = data.get(field_name)
            if type(value) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple at raw validation"
                )
            for item in value:
                if type(item) is not str:
                    raise ValueError(
                        f"{field_name} items must be exact strings"
                    )
        return data

    @field_validator("task_role", "risk_upgrade_reason")
    @classmethod
    def _optional_strings_nonblank(
        cls, value: str | None
    ) -> str | None:
        if value is not None:
            return _require_nonblank(value, "task_role/risk_upgrade_reason")
        return value

    @field_validator(
        "available_candidate_ids", "allowed_provider_refs"
    )
    @classmethod
    def _canonical_sorted_unique_strings(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        previous = None
        for item in value:
            _require_nonblank(item, "request id/provider tuples")
            if previous is not None and item <= previous:
                raise ValueError(
                    "request id/provider tuples must be "
                    "canonical-sorted ascending and unique"
                )
            previous = item
        return value

    @model_validator(mode="after")
    def _validate_risk_and_phase(
        self,
    ) -> "ModelRouteRequest":
        p3_level = self.risk_result.classification.risk_level
        if (
            _REQUEST_RISK_RANK[self.effective_risk]
            < _P3_RISK_RANK[p3_level]
        ):
            raise ValueError(
                "effective_risk must not decrease the P3 risk_level"
            )
        if self.effective_risk == p3_level:
            if self.risk_upgrade_reason is not None:
                raise ValueError(
                    "same-level risk must not carry a "
                    "risk_upgrade_reason"
                )
        elif self.risk_upgrade_reason is None:
            raise ValueError(
                "risk upgrade requires a nonblank risk_upgrade_reason"
            )
        if self.phase == "review" and self.agent_role is None:
            raise ValueError("phase review requires an agent_role")
        return self


class ModelRouteAttempt(BaseModel):
    """One ordered candidate attempt with its exact reason."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    candidate_id: StrictStr = Field(min_length=1, max_length=128)
    reason: _ATTEMPT_REASON

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_nonblank(cls, value: str) -> str:
        return _require_nonblank(value, "candidate_id")


def _match_covers(
    earlier: ModelRouteMatch, later: ModelRouteMatch
) -> bool:
    """Return True when every request matched by ``later`` also matches
    ``earlier`` (earlier match set is a superset of later match set)."""
    for dimension in _MATCH_DIMENSIONS:
        earlier_value = getattr(earlier, dimension)
        if (
            earlier_value is not None
            and getattr(later, dimension) != earlier_value
        ):
            return False
    return True


def _request_matches(
    match: ModelRouteMatch, request: ModelRouteRequest
) -> bool:
    if match.phase is not None and match.phase != request.phase:
        return False
    if (
        match.agent_role is not None
        and match.agent_role != request.agent_role
    ):
        return False
    if (
        match.effective_risk is not None
        and match.effective_risk != request.effective_risk
    ):
        return False
    if match.priority is not None and match.priority != request.priority:
        return False
    if (
        match.provider_boundary is not None
        and match.provider_boundary != request.provider_boundary
    ):
        return False
    if match.task_role is not None and match.task_role != request.task_role:
        return False
    return True


def _attempt_reason(
    candidate: ModelCandidate, request: ModelRouteRequest
) -> str:
    if candidate.candidate_id not in request.available_candidate_ids:
        return "candidate_unavailable"
    if candidate.provider_ref not in request.allowed_provider_refs:
        return "provider_not_allowed"
    if (
        _BOUNDARY_RANK[candidate.required_provider_boundary]
        > _BOUNDARY_RANK[request.provider_boundary]
    ):
        return "provider_boundary_denied"
    return "selected"


def _compute_route(
    policy: ModelRoutingPolicy, request: ModelRouteRequest
) -> dict:
    if not policy.enabled:
        return {
            "outcome": "blocked",
            "block_reason": "routing_disabled",
            "matched_rule_id": None,
            "matched_tier_alias": None,
            "attempts": (),
            "selected_candidate": None,
            "allocated_budget": None,
        }
    known_ids = {
        candidate.candidate_id
        for alias in policy.aliases
        for candidate in alias.candidates
    }
    unknown = tuple(
        candidate_id
        for candidate_id in request.available_candidate_ids
        if candidate_id not in known_ids
    )
    if unknown:
        raise ValueError(
            "unknown runtime available candidate IDs: "
            + ", ".join(unknown)
        )
    matched_rule = None
    for rule in policy.rules:
        if _request_matches(rule.match, request):
            matched_rule = rule
            break
    if matched_rule is None:
        return {
            "outcome": "blocked",
            "block_reason": "no_matching_rule",
            "matched_rule_id": None,
            "matched_tier_alias": None,
            "attempts": (),
            "selected_candidate": None,
            "allocated_budget": None,
        }
    matched_alias = next(
        alias
        for alias in policy.aliases
        if alias.alias == matched_rule.target.tier_alias
    )
    attempts = []
    selected_candidate = None
    for candidate in matched_alias.candidates:
        reason = _attempt_reason(candidate, request)
        attempts.append(
            ModelRouteAttempt(
                candidate_id=candidate.candidate_id, reason=reason
            )
        )
        if reason == "selected":
            selected_candidate = candidate
            break
    allocated_budget = ModelRouteBudget(
        token_budget_cap=min(
            request.budget.token_budget_cap,
            matched_rule.target.token_budget_cap,
        ),
        cost_budget_cap_usd=min(
            request.budget.cost_budget_cap_usd,
            matched_rule.target.cost_budget_cap_usd,
        ),
    )
    if selected_candidate is None:
        return {
            "outcome": "blocked",
            "block_reason": "no_eligible_candidate",
            "matched_rule_id": matched_rule.rule_id,
            "matched_tier_alias": matched_alias.alias,
            "attempts": tuple(attempts),
            "selected_candidate": None,
            "allocated_budget": allocated_budget,
        }
    return {
        "outcome": "selected",
        "block_reason": None,
        "matched_rule_id": matched_rule.rule_id,
        "matched_tier_alias": matched_alias.alias,
        "attempts": tuple(attempts),
        "selected_candidate": selected_candidate,
        "allocated_budget": allocated_budget,
    }


def _revalidate_policy(policy: ModelRoutingPolicy) -> ModelRoutingPolicy:
    """Rebuild and revalidate an exact-class Policy through public contracts.

    ``model_copy``/``model_construct`` can create an exact
    ``ModelRoutingPolicy`` whose nested contents or ``policy_digest`` bypassed
    the constructor validators. Rebuilding the full tree re-runs every
    validator and requires the provided digest to match the rebuilt body.
    """
    rebuilt_aliases = tuple(
        ModelTierAlias.model_validate(
            {
                "schema_version": alias.schema_version,
                "alias": alias.alias,
                "candidates": tuple(
                    ModelCandidate.model_validate(
                        {
                            "schema_version": candidate.schema_version,
                            "candidate_id": candidate.candidate_id,
                            "provider_ref": candidate.provider_ref,
                            "model_ref": candidate.model_ref,
                            "required_provider_boundary": (
                                candidate.required_provider_boundary
                            ),
                        }
                    )
                    for candidate in alias.candidates
                ),
            }
        )
        for alias in policy.aliases
    )
    rebuilt_rules = tuple(
        ModelRouteRule.model_validate(
            {
                "schema_version": rule.schema_version,
                "rule_id": rule.rule_id,
                "match": ModelRouteMatch.model_validate(
                    rule.match.model_dump()
                ),
                "target": ModelRouteTarget.model_validate(
                    rule.target.model_dump()
                ),
            }
        )
        for rule in policy.rules
    )
    rebuilt = ModelRoutingPolicy.model_validate(
        {
            "schema_version": policy.schema_version,
            "policy_version": policy.policy_version,
            "enabled": policy.enabled,
            "rules": rebuilt_rules,
            "aliases": rebuilt_aliases,
            "policy_digest": policy.policy_digest,
        }
    )
    if rebuilt.policy_digest != policy.policy_digest:
        raise ValueError(
            "policy_digest must equal the derived policy digest"
        )
    return rebuilt


def _revalidate_risk_result(
    result: RiskClassificationResult,
) -> RiskClassificationResult:
    """Re-run the P3 result's public validators on an exact-class instance."""
    rebuilt_input = RiskClassificationInput.model_validate(
        {
            "schema_version": result.input.schema_version,
            "snapshot": result.input.snapshot,
            "intake": result.input.intake,
            "manifest": result.input.manifest,
            "declarations": result.input.declarations,
        }
    )
    rebuilt_classification = RiskClassification.model_validate(
        result.classification.model_dump()
    )
    return RiskClassificationResult.model_validate(
        {
            "schema_version": result.schema_version,
            "input": rebuilt_input,
            "classification": rebuilt_classification,
        }
    )


def _revalidate_request(request: ModelRouteRequest) -> ModelRouteRequest:
    """Rebuild and revalidate an exact-class Request through public contracts."""
    rebuilt_budget = ModelRouteBudget.model_validate(
        {
            "schema_version": request.budget.schema_version,
            "token_budget_cap": request.budget.token_budget_cap,
            "cost_budget_cap_usd": request.budget.cost_budget_cap_usd,
        }
    )
    return ModelRouteRequest.model_validate(
        {
            "schema_version": request.schema_version,
            "risk_result": _revalidate_risk_result(request.risk_result),
            "phase": request.phase,
            "agent_role": request.agent_role,
            "task_role": request.task_role,
            "effective_risk": request.effective_risk,
            "risk_upgrade_reason": request.risk_upgrade_reason,
            "priority": request.priority,
            "provider_boundary": request.provider_boundary,
            "available_candidate_ids": request.available_candidate_ids,
            "allowed_provider_refs": request.allowed_provider_refs,
            "budget": rebuilt_budget,
        }
    )


class ModelRouteDecision(BaseModel):
    """Self-validating immutable routing decision for one policy+request."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["v1"] = "v1"
    policy: ModelRoutingPolicy
    request: ModelRouteRequest
    outcome: Literal["selected", "blocked"]
    block_reason: _BLOCK_REASON | None = None
    matched_rule_id: StrictStr | None = None
    matched_tier_alias: StrictStr | None = None
    attempts: tuple[ModelRouteAttempt, ...] = ()
    selected_candidate: ModelCandidate | None = None
    allocated_budget: ModelRouteBudget | None = None
    decision_id: StrictStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _rebuild_derive_digest_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "ModelRouteDecision must validate from a mapping"
            )
        data = dict(data)
        if info.mode == "json":
            if type(data.get("policy")) is dict or type(
                data.get("request")
            ) is dict:
                raise ValueError(
                    "ModelRouteDecision has no full JSON rebuild path "
                    "because RiskClassificationResult cannot rebuild from "
                    "JSON; use Python mode with exact policy/request "
                    "instances"
                )
            attempts_raw = data.get("attempts")
            if type(attempts_raw) is list:
                data["attempts"] = tuple(
                    item
                    if type(item) is ModelRouteAttempt
                    else ModelRouteAttempt.model_validate_json(
                        json.dumps(item)
                    )
                    for item in attempts_raw
                )
            selected_raw = data.get("selected_candidate")
            if type(selected_raw) is dict:
                data["selected_candidate"] = (
                    ModelCandidate.model_validate_json(
                        json.dumps(selected_raw)
                    )
                )
            budget_raw = data.get("allocated_budget")
            if type(budget_raw) is dict:
                data["allocated_budget"] = (
                    ModelRouteBudget.model_validate_json(
                        json.dumps(budget_raw)
                    )
                )
        else:
            if type(data.get("policy")) is not ModelRoutingPolicy:
                raise ValueError(
                    "policy must be an exact ModelRoutingPolicy instance"
                )
            if type(data.get("request")) is not ModelRouteRequest:
                raise ValueError(
                    "request must be an exact ModelRouteRequest instance"
                )
            attempts = data.get("attempts", ())
            if type(attempts) is not tuple:
                raise ValueError(
                    "attempts must be an exact tuple at raw validation"
                )
            for item in attempts:
                if type(item) is not ModelRouteAttempt:
                    raise ValueError(
                        "attempts must contain exact "
                        "ModelRouteAttempt instances"
                    )
            selected = data.get("selected_candidate")
            if selected is not None and type(selected) is not ModelCandidate:
                raise ValueError(
                    "selected_candidate must be an exact "
                    "ModelCandidate instance or None"
                )
            allocated = data.get("allocated_budget")
            if (
                allocated is not None
                and type(allocated) is not ModelRouteBudget
            ):
                raise ValueError(
                    "allocated_budget must be an exact "
                    "ModelRouteBudget instance or None"
                )
        body = {
            key: value
            for key, value in data.items()
            if key != "decision_id"
        }
        body.setdefault("schema_version", "v1")
        body.setdefault("block_reason", None)
        body.setdefault("matched_rule_id", None)
        body.setdefault("matched_tier_alias", None)
        body.setdefault("attempts", ())
        body.setdefault("selected_candidate", None)
        body.setdefault("allocated_budget", None)
        expected_digest = _sha256_digest(
            _canonical_json_bytes(_jsonable(body))
        )
        provided = data.get("decision_id")
        if provided is not None:
            if (
                type(provided) is not str
                or _SHA256_RE.fullmatch(provided) is None
            ):
                raise ValueError(
                    "decision_id must be sha256:<64 lowercase hex>"
                )
            if provided != expected_digest:
                raise ValueError(
                    "decision_id must equal the derived decision digest"
                )
        data["decision_id"] = expected_digest
        return data

    @field_validator("decision_id")
    @classmethod
    def _decision_id_format(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError(
                "decision_id must be sha256:<64 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _validate_entire_route_recomputation(
        self,
    ) -> "ModelRouteDecision":
        _revalidate_policy(self.policy)
        _revalidate_request(self.request)
        expected = _compute_route(self.policy, self.request)
        for field_name in (
            "outcome",
            "block_reason",
            "matched_rule_id",
            "matched_tier_alias",
            "attempts",
            "selected_candidate",
            "allocated_budget",
        ):
            if getattr(self, field_name) != expected[field_name]:
                raise ValueError(
                    f"{field_name} must be recomputed from policy "
                    f"and request"
                )
        expected_digest = _sha256_digest(
            _canonical_json_bytes(
                self.model_dump(mode="json", exclude={"decision_id"})
            )
        )
        if self.decision_id != expected_digest:
            raise ValueError(
                "decision_id must equal the derived decision digest"
            )
        return self


class ModelRouter:
    """Stateless deterministic router; returns a self-validating decision."""

    @staticmethod
    def route(
        policy: ModelRoutingPolicy, request: ModelRouteRequest
    ) -> ModelRouteDecision:
        if type(policy) is not ModelRoutingPolicy:
            raise TypeError("policy must be an exact ModelRoutingPolicy")
        if type(request) is not ModelRouteRequest:
            raise TypeError("request must be an exact ModelRouteRequest")
        _revalidate_policy(policy)
        _revalidate_request(request)
        result = _compute_route(policy, request)
        return ModelRouteDecision.model_validate(
            {
                "schema_version": "v1",
                "policy": policy,
                "request": request,
                **result,
            }
        )
