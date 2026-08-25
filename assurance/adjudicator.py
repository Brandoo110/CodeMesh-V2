"""V2-P4-08 Adjudicator overlay v1.

An on-demand, model-assisted but deterministically normalized adjudication
overlay over an already completed three-reviewer Council. The Adjudicator may
only propose ``duplicate_candidate``/``conflict_candidate`` groups and human
questions; it never deletes or replaces original Findings/Questions, never
chooses a winner, never resolves dissent, never invents Evidence, never
changes severity/status, and never outputs PASS/approval/waiver/acceptance.

This module prepares a bounded deterministic prompt and normalizes
caller-supplied exact response bytes. It does not call a model/provider,
execute tools, run the Council, evaluate the Gate, persist data, or perform
filesystem/network/subprocess/env/time/random access. Original Council
Findings and Questions remain the source of truth and are embedded and bound
in the normalized result.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import Finding
from .execution_receipt import CouncilExecutionReceipt
from .model_routing import (
    ModelCandidate,
    ModelRouteAttempt,
    ModelRouteBudget,
    ModelRouteDecision,
    ModelRouteRequest,
    ModelRouter,
    ModelRoutingPolicy,
)
from .review_council import CouncilRunResult
from .single_reviewer import ReviewQuestion


_ROLE_ORDER = ("intent", "architecture", "operability")
_ROLE_INDEX = {role: index for index, role in enumerate(_ROLE_ORDER)}
_TRIGGER_KINDS = frozenset(
    {
        "duplicate_candidate",
        "conflict_candidate",
        "high_severity_low_evidence",
        "consolidate_questions",
        "manual_review",
    }
)
_CLUSTER_KINDS = frozenset({"duplicate_candidate", "conflict_candidate"})
_QUESTION_REASONS = frozenset(
    {"conflict", "insufficient_evidence", "consolidation"}
)
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"clusters", "human_questions"})
_CLUSTER_KEYS = frozenset({"kind", "finding_ids", "rationale"})
_QUESTION_KEYS = frozenset(
    {
        "question",
        "reason",
        "source_finding_ids",
        "source_question_ids",
    }
)
_OUTCOMES = ("success", "schema_invalid")

_MAX_IDENTIFIER_BYTES = 128
_MAX_RATIONALE_BYTES = 4096
_MAX_QUESTION_BYTES = 4096
_MAX_FAILURE_CODE_BYTES = 128
_MAX_FAILURE_DETAILS_BYTES = 4096
_MAX_PROMPT_TEXT_BYTES = 262144
_MAX_RAW_RESPONSE_BYTES = 65536
_MAX_CLUSTERS = 32
_MAX_HUMAN_QUESTIONS = 64
_MAX_CLUSTER_MEMBERS = 64
_MAX_SOURCE_REFS = 64
_MAX_JSON_DEPTH = 32

_TRIGGER_ID_RE = re.compile(r"adj_trigger_[0-9a-f]{32}\Z")
_CLUSTER_ID_RE = re.compile(r"adj_cluster_[0-9a-f]{32}\Z")
_QUESTION_ID_RE = re.compile(r"adj_question_[0-9a-f]{32}\Z")
_PROMPT_ID_RE = re.compile(r"adj_prompt_[0-9a-f]{32}\Z")
_RESULT_ID_RE = re.compile(r"adj_result_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


class _PayloadError(Exception):
    """Internal deterministic payload rejection with a stable code."""

    def __init__(self, code: str, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


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


def _id32(prefix: str, body) -> str:
    return prefix + hashlib.sha256(
        _canonical_json_bytes(_jsonable(body))
    ).hexdigest()[:32]


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def _require_nonblank(value: str, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact string")
    if not value.strip():
        raise ValueError(f"{label} must not be blank or whitespace-only")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    return value


def _bounded_text(
    value: str, *, label: str, max_bytes: int
) -> str:
    _require_nonblank(value, label)
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"{label} must not exceed {max_bytes} UTF-8 bytes"
        )
    return value


def _exact_tuple(
    value: object, info: ValidationInfo, label: str
) -> object:
    if info.mode == "json":
        if type(value) is not list:
            raise ValueError(f"{label} must be an array in JSON mode")
        return tuple(value)
    if type(value) is not tuple:
        raise ValueError(f"{label} must be an exact tuple at raw validation")
    return value


def _canonical_id_tuple(
    value: tuple[str, ...],
    *,
    label: str,
    max_items: int = 64,
) -> tuple[str, ...]:
    if len(value) > max_items:
        raise ValueError(f"{label} must contain at most {max_items} items")
    seen = set()
    for item in value:
        _bounded_text(
            item,
            label=f"{label} item",
            max_bytes=_MAX_IDENTIFIER_BYTES,
        )
        if item in seen:
            raise ValueError(f"{label} items must be unique")
        seen.add(item)
    if tuple(value) != tuple(sorted(value)):
        raise ValueError(f"{label} must be canonical-sorted ascending")
    return tuple(value)


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _role_tuple_from_findings(
    findings: tuple[Finding, ...],
) -> tuple[str, ...]:
    roles = sorted(
        {item.reviewer_role for item in findings},
        key=_ROLE_INDEX.__getitem__,
    )
    return tuple(roles)


def _evidence_union_from_refs(
    *ref_sets: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(sorted({ref for refs in ref_sets for ref in refs}))


def _council_finding_ids(result: CouncilRunResult) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.finding_id
                for output in result.outputs
                for item in output.findings
            }
        )
    )


def _council_question_ids(result: CouncilRunResult) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.question_id
                for output in result.outputs
                for item in output.questions
            }
        )
    )


def _council_finding_by_id(
    result: CouncilRunResult,
) -> dict[str, Finding]:
    return {
        item.finding_id: item
        for output in result.outputs
        for item in output.findings
    }


def _council_question_by_id(
    result: CouncilRunResult,
) -> dict[str, ReviewQuestion]:
    return {
        item.question_id: item
        for output in result.outputs
        for item in output.questions
    }


class AdjudicationTrigger(BaseModel):
    """Deterministic trigger over Council Finding/Question IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    kind: Literal[
        "duplicate_candidate",
        "conflict_candidate",
        "high_severity_low_evidence",
        "consolidate_questions",
        "manual_review",
    ]
    finding_ids: tuple[str, ...]
    question_ids: tuple[str, ...]
    trigger_id: str | None = Field(default=None, validate_default=True)

    @field_validator("finding_ids", "question_ids", mode="before")
    @classmethod
    def _exact_id_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple(value, info, "trigger id tuple")

    @field_validator("finding_ids", "question_ids")
    @classmethod
    def _canonical_id_tuples(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _canonical_id_tuple(
            value, label=f"trigger {info.field_name}"
        )

    @field_validator("trigger_id", mode="before")
    @classmethod
    def _trigger_id_format(
        cls, value: object, info: ValidationInfo
    ) -> str:
        if value is None:
            kind = info.data.get("kind")
            finding_ids = info.data.get("finding_ids")
            question_ids = info.data.get("question_ids")
            if kind is None or finding_ids is None or question_ids is None:
                raise ValueError(
                    "trigger_id derivation requires kind and source IDs"
                )
            return _id32(
                "adj_trigger_",
                {
                    "kind": kind,
                    "finding_ids": finding_ids,
                    "question_ids": question_ids,
                },
            )
        if type(value) is not str or _TRIGGER_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "trigger_id must be adj_trigger_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _recompute_trigger_id(self) -> "AdjudicationTrigger":
        expected = _id32(
            "adj_trigger_",
            {
                "kind": self.kind,
                "finding_ids": self.finding_ids,
                "question_ids": self.question_ids,
            },
        )
        if self.trigger_id != expected:
            raise ValueError(
                "trigger_id must equal the deterministic recomputation "
                "from kind and source IDs"
            )
        return self


def _route_from_json(
    raw: object, risk_result
) -> ModelRouteDecision:
    if type(raw) is not dict:
        raise ValueError(
            "route must be a mapping in JSON mode"
        )
    data = dict(raw)
    policy_raw = data.get("policy")
    if type(policy_raw) is not dict:
        raise ValueError(
            "route.policy must be a mapping in JSON mode"
        )
    request_raw = data.get("request")
    if type(request_raw) is not dict:
        raise ValueError(
            "route.request must be a mapping in JSON mode"
        )
    policy = ModelRoutingPolicy.model_validate_json(
        json.dumps(policy_raw)
    )
    request_data = dict(request_raw)
    request_risk_raw = request_data.get("risk_result")
    if request_risk_raw != risk_result.model_dump(mode="json"):
        raise ValueError(
            "route request risk_result must equal the Council risk result"
        )
    request_data["risk_result"] = risk_result
    budget_raw = request_data.get("budget")
    if type(budget_raw) is not dict:
        raise ValueError(
            "route.request.budget must be a mapping in JSON mode"
        )
    request_data["budget"] = ModelRouteBudget.model_validate_json(
        json.dumps(budget_raw)
    )
    for field_name in ("available_candidate_ids", "allowed_provider_refs"):
        raw_value = request_data.get(field_name)
        if type(raw_value) is list:
            request_data[field_name] = tuple(raw_value)
    request = ModelRouteRequest.model_validate(request_data)
    if request.model_dump(mode="json") != request_raw:
        raise ValueError(
            "route request must equal the exact JSON reconstruction"
        )
    attempts_raw = data.get("attempts")
    if type(attempts_raw) is not list:
        raise ValueError(
            "route.attempts must be an array in JSON mode"
        )
    attempts = tuple(
        item
        if type(item) is ModelRouteAttempt
        else ModelRouteAttempt.model_validate_json(json.dumps(item))
        for item in attempts_raw
    )
    selected_raw = data.get("selected_candidate")
    selected_candidate = (
        None
        if selected_raw is None
        else (
            selected_raw
            if type(selected_raw) is ModelCandidate
            else ModelCandidate.model_validate_json(
                json.dumps(selected_raw)
            )
        )
    )
    budget_raw = data.get("allocated_budget")
    allocated_budget = (
        None
        if budget_raw is None
        else (
            budget_raw
            if type(budget_raw) is ModelRouteBudget
            else ModelRouteBudget.model_validate_json(
                json.dumps(budget_raw)
            )
        )
    )
    decision = ModelRouteDecision.model_validate(
        {
            "schema_version": data.get("schema_version", "v1"),
            "policy": policy,
            "request": request,
            "outcome": data.get("outcome"),
            "block_reason": data.get("block_reason"),
            "matched_rule_id": data.get("matched_rule_id"),
            "matched_tier_alias": data.get("matched_tier_alias"),
            "attempts": attempts,
            "selected_candidate": selected_candidate,
            "allocated_budget": allocated_budget,
            "decision_id": data.get("decision_id"),
        }
    )
    if decision.model_dump(mode="json") != raw:
        raise ValueError(
            "route must equal the exact JSON reconstruction"
        )
    return decision


def _validate_trigger_against_council(
    trigger: AdjudicationTrigger, result: CouncilRunResult
) -> None:
    council_finding_ids = frozenset(_council_finding_ids(result))
    council_question_ids = frozenset(_council_question_ids(result))
    if not set(trigger.finding_ids) <= council_finding_ids:
        raise ValueError(
            "trigger finding_ids must all exist in the Council result"
        )
    if not set(trigger.question_ids) <= council_question_ids:
        raise ValueError(
            "trigger question_ids must all exist in the Council result"
        )
    findings = _council_finding_by_id(result)
    if trigger.kind == "duplicate_candidate":
        if len(trigger.finding_ids) < 2:
            raise ValueError(
                "duplicate_candidate requires at least two Findings"
            )
    elif trigger.kind == "conflict_candidate":
        if len(trigger.finding_ids) < 2:
            raise ValueError(
                "conflict_candidate requires at least two Findings"
            )
        roles = {
            findings[finding_id].reviewer_role
            for finding_id in trigger.finding_ids
        }
        if len(roles) < 2:
            raise ValueError(
                "conflict_candidate requires Findings from at least "
                "two reviewer roles"
            )
    elif trigger.kind == "high_severity_low_evidence":
        if len(trigger.finding_ids) != 1:
            raise ValueError(
                "high_severity_low_evidence requires exactly one Finding"
            )
        finding = findings[trigger.finding_ids[0]]
        if finding.severity not in ("high", "critical"):
            raise ValueError(
                "high_severity_low_evidence requires a high/critical Finding"
            )
        if len(finding.evidence_refs) > 1:
            raise ValueError(
                "high_severity_low_evidence requires at most one "
                "Evidence reference"
            )
    elif trigger.kind == "consolidate_questions":
        if not trigger.question_ids:
            raise ValueError(
                "consolidate_questions requires at least one Question"
            )
    else:
        if not trigger.finding_ids and not trigger.question_ids:
            raise ValueError(
                "manual_review requires at least one Finding or Question"
            )


class AdjudicatorInput(BaseModel):
    """Exact Council-bound adjudication entry point with route facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    council_result: CouncilRunResult
    council_receipt: CouncilExecutionReceipt
    route: ModelRouteDecision
    trigger: AdjudicationTrigger
    requested_at: AwareDatetime

    @field_validator("requested_at", mode="before")
    @classmethod
    def _reject_numeric_requested_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "AdjudicatorInput must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            result_raw = data.get("council_result")
            if type(result_raw) is not dict:
                raise ValueError(
                    "council_result must be a mapping in JSON mode"
                )
            council_result = CouncilRunResult.model_validate_json(
                json.dumps(result_raw)
            )
            data["council_result"] = council_result
            receipt_raw = data.get("council_receipt")
            if type(receipt_raw) is not dict:
                raise ValueError(
                    "council_receipt must be a mapping in JSON mode"
                )
            data["council_receipt"] = (
                CouncilExecutionReceipt.model_validate_json(
                    json.dumps(receipt_raw)
                )
            )
            trigger_raw = data.get("trigger")
            if type(trigger_raw) is not dict:
                raise ValueError(
                    "trigger must be a mapping in JSON mode"
                )
            data["trigger"] = AdjudicationTrigger.model_validate_json(
                json.dumps(trigger_raw)
            )
            data["route"] = _route_from_json(
                data.get("route"),
                council_result.plan.inputs[0].risk_result,
            )
            return data
        if type(data.get("council_result")) is not CouncilRunResult:
            raise ValueError(
                "council_result must be an exact CouncilRunResult instance"
            )
        if type(data.get("council_receipt")) is not CouncilExecutionReceipt:
            raise ValueError(
                "council_receipt must be an exact "
                "CouncilExecutionReceipt instance"
            )
        if type(data.get("route")) is not ModelRouteDecision:
            raise ValueError(
                "route must be an exact ModelRouteDecision instance"
            )
        if type(data.get("trigger")) is not AdjudicationTrigger:
            raise ValueError(
                "trigger must be an exact AdjudicationTrigger instance"
            )
        return data

    @model_validator(mode="after")
    def _validate_bound_input(self) -> "AdjudicatorInput":
        council_result = self.council_result
        receipt = self.council_receipt
        route = self.route
        trigger = self.trigger
        if (
            CouncilRunResult.model_validate_json(
                council_result.model_dump_json()
            )
            != council_result
        ):
            raise ValueError(
                "council_result must survive public JSON round-trip "
                "validation"
            )
        if (
            CouncilExecutionReceipt.model_validate_json(
                receipt.model_dump_json()
            )
            != receipt
        ):
            raise ValueError(
                "council_receipt must survive public JSON round-trip "
                "validation"
            )
        if (
            AdjudicationTrigger.model_validate_json(
                trigger.model_dump_json()
            )
            != trigger
        ):
            raise ValueError(
                "trigger must survive public JSON round-trip validation"
            )
        rebuilt_route = ModelRouter.route(route.policy, route.request)
        if rebuilt_route != route:
            raise ValueError(
                "route must equal the recomputed route from its policy "
                "and request"
            )
        if receipt.result is None:
            raise ValueError(
                "council_receipt must carry the exact Council result; "
                "missing Reviewer execution is not rescuable"
            )
        if receipt.result != council_result:
            raise ValueError(
                "council_receipt.result must equal council_result exactly"
            )
        if receipt.plan != council_result.plan:
            raise ValueError(
                "council_receipt must bind the exact Council plan"
            )
        plan = council_result.plan
        subject = plan.inputs[0].subject
        risk_result = plan.inputs[0].risk_result
        if receipt.subject_digest != subject.subject_digest:
            raise ValueError(
                "council_receipt must bind the exact Council subject"
            )
        if len(receipt.steps) != 3 or any(
            step.fact.result != "success" for step in receipt.steps
        ):
            raise ValueError(
                "Adjudicator requires a completed three-role Council "
                "receipt with every role success"
            )
        if receipt.overall_result != "success":
            raise ValueError(
                "Adjudicator requires overall receipt result success"
            )
        if receipt.topology.actual_state != "completed":
            raise ValueError(
                "Adjudicator requires completed Council topology"
            )
        if receipt.result_digest is None:
            raise ValueError(
                "Adjudicator requires a Council result digest"
            )
        if route.request.phase != "adjudication":
            raise ValueError(
                "Adjudicator route must use phase=adjudication"
            )
        if route.request.agent_role is not None:
            raise ValueError(
                "Adjudicator route must have agent_role=None"
            )
        if route.request.task_role != "adjudicator":
            raise ValueError(
                "Adjudicator route must have task_role=adjudicator"
            )
        if route.outcome != "selected" or route.selected_candidate is None:
            raise ValueError(
                "Adjudicator requires a selected route with a candidate; "
                "blocked routes are rejected"
            )
        if route.request.risk_result != risk_result:
            raise ValueError(
                "Adjudicator route must bind the exact Council risk result"
            )
        if (
            route.request.risk_result.classification.subject_digest
            != subject.subject_digest
        ):
            raise ValueError(
                "Adjudicator route must bind the exact Council subject"
            )
        if self.requested_at < receipt.recorded_at:
            raise ValueError(
                "requested_at must be at or after council receipt "
                "recorded_at"
            )
        _validate_trigger_against_council(trigger, council_result)
        return self


_PROMPT_SCHEMA = """{
  "clusters": [
    {
      "kind": "duplicate_candidate | conflict_candidate",
      "finding_ids": ["existing finding id", "existing finding id"],
      "rationale": "bounded inferred explanation"
    }
  ],
  "human_questions": [
    {
      "question": "bounded question text",
      "reason": "conflict | insufficient_evidence | consolidation",
      "source_finding_ids": ["existing finding id"],
      "source_question_ids": ["existing question id"]
    }
  ]
}"""


def _build_prompt_text(input_: AdjudicatorInput) -> str:
    lines = []
    add = lines.append
    add("V2-P4-08 Adjudication Overlay Prompt v1")
    add(
        "You are a model-assisted overlay over an already completed "
        "three-reviewer Council. You have zero tools: do not execute "
        "tools, read files, access the network, or perform any I/O."
    )
    add(
        "You may only propose potential duplicate/conflict groups and "
        "human questions. You never confirm duplicates/conflicts, choose "
        "a winner, resolve dissent, change severity/status, or approve "
        "anything. Original Findings and Questions remain the source of "
        "truth and must never be deleted, replaced, or edited."
    )
    add("")
    subject = input_.council_result.plan.inputs[0].subject
    risk = input_.council_result.plan.inputs[0].risk_result.classification
    add("SUBJECT")
    add(f"change_id: {subject.change_id}")
    add(f"subject_digest: {subject.subject_digest}")
    add(f"repository: {subject.repository}")
    add(f"base_revision: {subject.base_revision}")
    add(f"head_revision: {subject.head_revision}")
    add(f"policy_version: {subject.policy_version}")
    add("")
    add("COUNCIL RISK")
    add(f"risk_level: {risk.risk_level}")
    add(f"classification_id: {risk.classification_id}")
    add(
        "required_reviewers: "
        + ", ".join(risk.required_reviewers)
    )
    add("")
    trigger = input_.trigger
    add("TRIGGER")
    add(f"kind: {trigger.kind}")
    add("finding_ids: " + ", ".join(trigger.finding_ids))
    add("question_ids: " + ", ".join(trigger.question_ids))
    add(f"trigger_id: {trigger.trigger_id}")
    add("")
    add(
        "ORIGINAL COUNCIL FINDINGS AND QUESTIONS "
        "(canonical role/ID order; source of truth)"
    )
    for output in input_.council_result.outputs:
        role = output.input.reviewer_role
        for finding in output.findings:
            add(
                f"finding {finding.finding_id} role={role} "
                f"severity={finding.severity} status={finding.status} "
                f"basis={finding.basis} confidence={finding.confidence} "
                f"rubric_hash={finding.rubric_hash} "
                f"model_ref={finding.model_ref} "
                f"evidence_refs={','.join(finding.evidence_refs)} "
                f"claim={finding.claim}"
            )
        for question in output.questions:
            add(
                f"question {question.question_id} role={role} "
                f"reason={question.reason} status={question.status} "
                f"rubric_hash={question.rubric_hash} "
                f"model_ref={question.model_ref} "
                f"evidence_refs={','.join(question.evidence_refs)} "
                f"question={question.question}"
            )
    add("")
    add("AVAILABLE EVIDENCE IDS (identifiers only; no artifact reads)")
    for entry in input_.council_result.evidence_index:
        add(f"evidence_id: {entry.evidence_id}")
    add("")
    route = input_.route
    selected = route.selected_candidate
    budget = route.allocated_budget
    add("SELECTED MODEL ROUTE (zero-tool authority)")
    add(f"phase: {route.request.phase}")
    add(f"task_role: {route.request.task_role}")
    add(f"agent_role: {route.request.agent_role}")
    add(f"effective_risk: {route.request.effective_risk}")
    add(f"priority: {route.request.priority}")
    add(f"provider_boundary: {route.request.provider_boundary}")
    add(f"candidate_id: {selected.candidate_id}")
    add(f"provider_ref: {selected.provider_ref}")
    add(f"model_ref: {selected.model_ref}")
    add(f"decision_id: {route.decision_id}")
    add(f"policy_digest: {route.policy.policy_digest}")
    add(f"matched_rule_id: {route.matched_rule_id}")
    add(f"matched_tier_alias: {route.matched_tier_alias}")
    add(
        "allocated_budget: token_cap="
        f"{budget.token_budget_cap} cost_cap_usd={budget.cost_budget_cap_usd}"
    )
    add("")
    add("EXACT JSON RESPONSE SCHEMA")
    add(
        "Respond with one strict UTF-8 JSON object, no Markdown "
        "fence/prefix/suffix, no duplicate object keys, no NaN/Infinity, "
        "exactly these top-level keys:"
    )
    add(_PROMPT_SCHEMA)
    add("")
    add("PROHIBITIONS")
    add("must not create new IDs (no group/question IDs)")
    add("must not supply Evidence refs, roles, severity, or status")
    add("must not supply winner, resolution, vote, score, or approval")
    add("must not output PASS, approval, waiver, or acceptance")
    add("must not delete or replace any original Finding or Question")
    add(
        "duplicate_candidate and conflict_candidate groups are candidate "
        "overlays only, never confirmed facts"
    )
    add("")
    add("Return the JSON object now.")
    return "\n".join(lines)


class AdjudicatorPrompt(BaseModel):
    """Deterministic bounded prompt carrying the exact bound input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: AdjudicatorInput
    prompt_text: str
    prompt_digest: str = Field(default=None, validate_default=True)
    prompt_id: str = Field(default=None, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "AdjudicatorPrompt must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            raw_input = data.get("input")
            if type(raw_input) is not dict:
                raise ValueError(
                    "input must be a mapping in JSON mode"
                )
            data["input"] = AdjudicatorInput.model_validate_json(
                json.dumps(raw_input)
            )
            return data
        if type(data.get("input")) is not AdjudicatorInput:
            raise ValueError(
                "input must be an exact AdjudicatorInput instance"
            )
        return data

    @field_validator("prompt_text", mode="before")
    @classmethod
    def _bounded_prompt_text(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("prompt_text must be an exact string")
        return _bounded_text(
            value,
            label="prompt_text",
            max_bytes=_MAX_PROMPT_TEXT_BYTES,
        )

    @field_validator("prompt_digest", mode="before")
    @classmethod
    def _prompt_digest_format(
        cls, value: object, info: ValidationInfo
    ) -> str:
        if value is None:
            prompt_text = info.data.get("prompt_text")
            if prompt_text is None:
                raise ValueError(
                    "prompt_digest derivation requires prompt_text"
                )
            return _sha256_digest(
                _canonical_json_bytes({"prompt_text": prompt_text})
            )
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise ValueError(
                "prompt_digest must be sha256:<64 lowercase hex>"
            )
        return value

    @field_validator("prompt_id", mode="before")
    @classmethod
    def _prompt_id_format(
        cls, value: object, info: ValidationInfo
    ) -> str:
        if value is None:
            prompt_digest = info.data.get("prompt_digest")
            prompt_input = info.data.get("input")
            if prompt_digest is None or prompt_input is None:
                raise ValueError(
                    "prompt_id derivation requires prompt digest and input"
                )
            return _id32(
                "adj_prompt_",
                {
                    "prompt_digest": prompt_digest,
                    "input": prompt_input.model_dump(mode="json"),
                },
            )
        if type(value) is not str or _PROMPT_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "prompt_id must be adj_prompt_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _recompute_digest_and_id(self) -> "AdjudicatorPrompt":
        expected_digest = _sha256_digest(
            _canonical_json_bytes({"prompt_text": self.prompt_text})
        )
        if self.prompt_digest != expected_digest:
            raise ValueError(
                "prompt_digest must equal the deterministic "
                "recomputation from prompt_text"
            )
        expected_id = _id32(
            "adj_prompt_",
            {
                "prompt_digest": self.prompt_digest,
                "input": self.input.model_dump(mode="json"),
            },
        )
        if self.prompt_id != expected_id:
            raise ValueError(
                "prompt_id must equal the deterministic recomputation "
                "from prompt digest and exact input"
            )
        return self


class AdjudicatorNormalizationInput(BaseModel):
    """Exact model draft bytes plus caller usage/timing facts."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    schema_version: Literal["v1"] = "v1"
    prompt: AdjudicatorPrompt
    raw_response_bytes: bytes
    usage_status: Literal["measured", "unavailable"]
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost_usd: float | None = Field(
        default=None, strict=True, ge=0, allow_inf_nan=False
    )
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: int = Field(strict=True, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "AdjudicatorNormalizationInput must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            raw_prompt = data.get("prompt")
            if type(raw_prompt) is not dict:
                raise ValueError(
                    "prompt must be a mapping in JSON mode"
                )
            data["prompt"] = AdjudicatorPrompt.model_validate_json(
                json.dumps(raw_prompt)
            )
            return data
        if type(data.get("prompt")) is not AdjudicatorPrompt:
            raise ValueError(
                "prompt must be an exact AdjudicatorPrompt instance"
            )
        return data

    @field_validator("raw_response_bytes", mode="before")
    @classmethod
    def _exact_raw_bytes(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if info.mode == "json":
            if type(value) is not str:
                raise ValueError(
                    "raw_response_bytes must be a base64 string in JSON mode"
                )
            return value
        if type(value) is not bytes:
            raise ValueError(
                "raw_response_bytes must be an exact bytes value"
            )
        return value

    @field_validator("raw_response_bytes")
    @classmethod
    def _bounded_nonempty_raw_bytes(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError(
                "raw_response_bytes must be nonempty"
            )
        if len(value) > _MAX_RAW_RESPONSE_BYTES:
            raise ValueError(
                "raw_response_bytes must not exceed "
                f"{_MAX_RAW_RESPONSE_BYTES} bytes"
            )
        return value

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

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _reject_numeric_timestamps(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @model_validator(mode="after")
    def _validate_usage_and_timing(self) -> "AdjudicatorNormalizationInput":
        if self.usage_status == "measured":
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or self.cost_usd is None
            ):
                raise ValueError(
                    "measured usage requires all usage values present"
                )
        else:
            if (
                self.input_tokens is not None
                or self.output_tokens is not None
                or self.cost_usd is not None
            ):
                raise ValueError(
                    "unavailable usage requires all usage values absent"
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
                "latency_ms must equal the exact derived millisecond latency"
            )
        return self


class AdjudicationCluster(BaseModel):
    """One normalized candidate group with exact Council source binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    kind: Literal["duplicate_candidate", "conflict_candidate"]
    finding_ids: tuple[str, ...]
    findings: tuple[Finding, ...]
    roles: tuple[str, ...] = Field(default=None, validate_default=True)
    evidence_refs: tuple[str, ...] = Field(default=None, validate_default=True)
    rationale: str
    cluster_id: str = Field(default=None, validate_default=True)

    @field_validator("finding_ids", mode="before")
    @classmethod
    def _exact_finding_ids(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple(value, info, "cluster finding_ids")

    @field_validator("findings", mode="before")
    @classmethod
    def _exact_findings(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if info.mode == "json":
            if type(value) is not list:
                raise ValueError(
                    "findings must be an array in JSON mode"
                )
            return tuple(
                item
                if type(item) is Finding
                else Finding.model_validate_json(json.dumps(item))
                for item in value
            )
        if type(value) is not tuple:
            raise ValueError(
                "findings must be an exact tuple at raw validation"
            )
        for item in value:
            if type(item) is not Finding:
                raise ValueError(
                    "findings must contain exact Finding instances"
                )
        return value

    @field_validator("roles", "evidence_refs", mode="before")
    @classmethod
    def _exact_derived_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if value is None:
            findings = info.data.get("findings")
            if findings is None:
                raise ValueError(
                    f"{info.field_name} derivation requires member Findings"
                )
            if info.field_name == "roles":
                return _role_tuple_from_findings(findings)
            return _evidence_union_from_refs(
                *(item.evidence_refs for item in findings)
            )
        return _exact_tuple(value, info, "cluster derived tuple")

    @field_validator("rationale", mode="before")
    @classmethod
    def _bounded_rationale(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("rationale must be an exact string")
        return _bounded_text(
            value,
            label="rationale",
            max_bytes=_MAX_RATIONALE_BYTES,
        )

    @field_validator("cluster_id", mode="before")
    @classmethod
    def _cluster_id_format(
        cls, value: object, info: ValidationInfo
    ) -> str:
        if value is None:
            kind = info.data.get("kind")
            finding_ids = info.data.get("finding_ids")
            rationale = info.data.get("rationale")
            if kind is None or finding_ids is None or rationale is None:
                raise ValueError(
                    "cluster_id derivation requires kind, finding_ids, "
                    "and rationale"
                )
            return _id32(
                "adj_cluster_",
                {
                    "kind": kind,
                    "finding_ids": finding_ids,
                    "rationale": rationale,
                },
            )
        if type(value) is not str or _CLUSTER_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "cluster_id must be adj_cluster_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _recompute_cluster(self) -> "AdjudicationCluster":
        if len(self.findings) < 2:
            raise ValueError(
                "a candidate group must contain at least two Findings"
            )
        expected_ids = tuple(
            sorted(item.finding_id for item in self.findings)
        )
        if self.finding_ids != expected_ids:
            raise ValueError(
                "finding_ids must equal the canonical sorted member "
                "Finding IDs"
            )
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError(
                "finding_ids must be unique within a cluster"
            )
        if tuple(self.findings) != tuple(
            sorted(self.findings, key=lambda item: item.finding_id)
        ):
            raise ValueError(
                "findings must be canonical-sorted by finding_id"
            )
        expected_roles = _role_tuple_from_findings(self.findings)
        if self.roles != expected_roles:
            raise ValueError(
                "roles must equal the deterministic Council role union "
                "of member Findings"
            )
        expected_evidence = _evidence_union_from_refs(
            *(item.evidence_refs for item in self.findings)
        )
        if self.evidence_refs != expected_evidence:
            raise ValueError(
                "evidence_refs must equal the exact union of member "
                "Finding Evidence refs"
            )
        if self.kind == "conflict_candidate" and len(self.roles) < 2:
            raise ValueError(
                "conflict_candidate must span at least two reviewer roles"
            )
        expected_id = _id32(
            "adj_cluster_",
            {
                "kind": self.kind,
                "finding_ids": self.finding_ids,
                "rationale": self.rationale,
            },
        )
        if self.cluster_id != expected_id:
            raise ValueError(
                "cluster_id must equal the deterministic recomputation"
            )
        return self


class AdjudicationHumanQuestion(BaseModel):
    """One normalized human question with exact Council source binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    question: str
    reason: Literal["conflict", "insufficient_evidence", "consolidation"]
    source_finding_ids: tuple[str, ...]
    source_question_ids: tuple[str, ...]
    source_findings: tuple[Finding, ...]
    source_questions: tuple[ReviewQuestion, ...]
    evidence_refs: tuple[str, ...] = Field(
        default=None, validate_default=True
    )
    question_id: str = Field(default=None, validate_default=True)

    @field_validator("source_finding_ids", "source_question_ids", mode="before")
    @classmethod
    def _exact_source_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple(value, info, "question source tuple")

    @field_validator("source_findings", "source_questions", mode="before")
    @classmethod
    def _exact_source_models(
        cls, value: object, info: ValidationInfo
    ) -> object:
        model_type = (
            Finding
            if info.field_name == "source_findings"
            else ReviewQuestion
        )
        if info.mode == "json":
            if type(value) is not list:
                raise ValueError(
                    f"{info.field_name} must be an array in JSON mode"
                )
            return tuple(
                item
                if type(item) is model_type
                else model_type.model_validate_json(json.dumps(item))
                for item in value
            )
        if type(value) is not tuple:
            raise ValueError(
                f"{info.field_name} must be an exact tuple at raw validation"
            )
        for item in value:
            if type(item) is not model_type:
                raise ValueError(
                    f"{info.field_name} items must be exact "
                    f"{model_type.__name__} instances"
                )
        return value

    @field_validator("question", mode="before")
    @classmethod
    def _bounded_question(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("question must be an exact string")
        return _bounded_text(
            value,
            label="question",
            max_bytes=_MAX_QUESTION_BYTES,
        )

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_evidence_refs(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if value is None:
            source_findings = info.data.get("source_findings")
            source_questions = info.data.get("source_questions")
            if source_findings is None or source_questions is None:
                raise ValueError(
                    "evidence_refs derivation requires source models"
                )
            return _evidence_union_from_refs(
                *(item.evidence_refs for item in source_findings),
                *(item.evidence_refs for item in source_questions),
            )
        return _exact_tuple(value, info, "question evidence_refs")

    @field_validator("question_id", mode="before")
    @classmethod
    def _question_id_format(
        cls, value: object, info: ValidationInfo
    ) -> str:
        if value is None:
            question = info.data.get("question")
            reason = info.data.get("reason")
            source_finding_ids = info.data.get("source_finding_ids")
            source_question_ids = info.data.get("source_question_ids")
            if (
                question is None
                or reason is None
                or source_finding_ids is None
                or source_question_ids is None
            ):
                raise ValueError(
                    "question_id derivation requires question, reason, "
                    "and source IDs"
                )
            return _id32(
                "adj_question_",
                {
                    "question": question,
                    "reason": reason,
                    "source_finding_ids": source_finding_ids,
                    "source_question_ids": source_question_ids,
                },
            )
        if type(value) is not str or _QUESTION_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "question_id must be adj_question_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _recompute_question(self) -> "AdjudicationHumanQuestion":
        expected_finding_ids = tuple(
            sorted(item.finding_id for item in self.source_findings)
        )
        expected_question_ids = tuple(
            sorted(item.question_id for item in self.source_questions)
        )
        if self.source_finding_ids != expected_finding_ids:
            raise ValueError(
                "source_finding_ids must equal the canonical sorted "
                "bound Finding IDs"
            )
        if self.source_question_ids != expected_question_ids:
            raise ValueError(
                "source_question_ids must equal the canonical sorted "
                "bound Question IDs"
            )
        if not self.source_finding_ids and not self.source_question_ids:
            raise ValueError(
                "a human question must reference at least one source"
            )
        expected_evidence = _evidence_union_from_refs(
            *(item.evidence_refs for item in self.source_findings),
            *(item.evidence_refs for item in self.source_questions),
        )
        if self.evidence_refs != expected_evidence:
            raise ValueError(
                "evidence_refs must equal the exact union of referenced "
                "source Evidence refs"
            )
        expected_id = _id32(
            "adj_question_",
            {
                "question": self.question,
                "reason": self.reason,
                "source_finding_ids": self.source_finding_ids,
                "source_question_ids": self.source_question_ids,
            },
        )
        if self.question_id != expected_id:
            raise ValueError(
                "question_id must equal the deterministic recomputation"
            )
        return self


def _result_base_fields(
    normalization_input: AdjudicatorNormalizationInput,
) -> dict:
    adjudicator_input = normalization_input.prompt.input
    council = adjudicator_input.council_result
    route = adjudicator_input.route
    selected = route.selected_candidate
    return {
        "schema_version": "v1",
        "input": normalization_input,
        "preserved_finding_ids": _council_finding_ids(council),
        "preserved_question_ids": _council_question_ids(council),
        "evidence_id_universe": tuple(
            entry.evidence_id for entry in council.evidence_index
        ),
        "selected_candidate_id": selected.candidate_id,
        "selected_provider_ref": selected.provider_ref,
        "selected_model_ref": selected.model_ref,
        "route_decision_id": route.decision_id,
        "raw_response_digest": _sha256_digest(
            normalization_input.raw_response_bytes
        ),
        "usage_status": normalization_input.usage_status,
        "input_tokens": normalization_input.input_tokens,
        "output_tokens": normalization_input.output_tokens,
        "cost_usd": normalization_input.cost_usd,
        "started_at": normalization_input.started_at,
        "completed_at": normalization_input.completed_at,
        "latency_ms": normalization_input.latency_ms,
    }


def _result_fields(
    normalization_input: AdjudicatorNormalizationInput,
    *,
    outcome: Literal["success", "schema_invalid"],
    failure_code: str | None,
    failure_details: str | None,
    clusters: tuple[AdjudicationCluster, ...],
    human_questions: tuple[AdjudicationHumanQuestion, ...],
) -> dict:
    fields = _result_base_fields(normalization_input)
    fields.update(
        {
            "outcome": outcome,
            "failure_code": failure_code,
            "failure_details": failure_details,
            "clusters": clusters,
            "human_questions": human_questions,
            "dissent_finding_ids": tuple(
                sorted(
                    {
                        finding_id
                        for cluster in clusters
                        if cluster.kind == "conflict_candidate"
                        for finding_id in cluster.finding_ids
                    }
                )
            ),
        }
    )
    body = {
        key: value
        for key, value in fields.items()
        if key != "result_id"
    }
    fields["result_id"] = _id32("adj_result_", body)
    return fields


class AdjudicationResult(BaseModel):
    """Normalized overlay result preserving every original Council ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: AdjudicatorNormalizationInput
    outcome: Literal["success", "schema_invalid"]
    failure_code: str | None = None
    failure_details: str | None = None
    clusters: tuple[AdjudicationCluster, ...]
    human_questions: tuple[AdjudicationHumanQuestion, ...]
    preserved_finding_ids: tuple[str, ...]
    preserved_question_ids: tuple[str, ...]
    dissent_finding_ids: tuple[str, ...]
    evidence_id_universe: tuple[str, ...]
    selected_candidate_id: str
    selected_provider_ref: str
    selected_model_ref: str
    route_decision_id: str
    raw_response_digest: str
    usage_status: Literal["measured", "unavailable"]
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost_usd: float | None = Field(
        default=None, strict=True, ge=0, allow_inf_nan=False
    )
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: int = Field(strict=True, ge=0)
    result_id: str

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "AdjudicationResult must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            raw_input = data.get("input")
            if type(raw_input) is not dict:
                raise ValueError(
                    "input must be a mapping in JSON mode"
                )
            data["input"] = (
                AdjudicatorNormalizationInput.model_validate_json(
                    json.dumps(raw_input)
                )
            )
            for field_name, model_type in (
                ("clusters", AdjudicationCluster),
                ("human_questions", AdjudicationHumanQuestion),
            ):
                raw_items = data.get(field_name)
                if type(raw_items) is not list:
                    raise ValueError(
                        f"{field_name} must be an array in JSON mode"
                    )
                data[field_name] = tuple(
                    item
                    if type(item) is model_type
                    else model_type.model_validate_json(json.dumps(item))
                    for item in raw_items
                )
            return data
        if type(data.get("input")) is not AdjudicatorNormalizationInput:
            raise ValueError(
                "input must be an exact "
                "AdjudicatorNormalizationInput instance"
            )
        for field_name, model_type in (
            ("clusters", AdjudicationCluster),
            ("human_questions", AdjudicationHumanQuestion),
        ):
            value = data.get(field_name)
            if type(value) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple at raw validation"
                )
            for item in value:
                if type(item) is not model_type:
                    raise ValueError(
                        f"{field_name} items must be exact "
                        f"{model_type.__name__} instances"
                    )
        return data

    @field_validator("failure_code", mode="before")
    @classmethod
    def _bounded_failure_code(cls, value: object) -> str | None:
        if value is None:
            return value
        if type(value) is not str:
            raise ValueError("failure_code must be an exact string or None")
        return _bounded_text(
            value,
            label="failure_code",
            max_bytes=_MAX_FAILURE_CODE_BYTES,
        )

    @field_validator("failure_details", mode="before")
    @classmethod
    def _bounded_failure_details(cls, value: object) -> str | None:
        if value is None:
            return value
        if type(value) is not str:
            raise ValueError(
                "failure_details must be an exact string or None"
            )
        return _bounded_text(
            value,
            label="failure_details",
            max_bytes=_MAX_FAILURE_DETAILS_BYTES,
        )

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

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _reject_numeric_timestamps(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("result_id", mode="before")
    @classmethod
    def _result_id_format(cls, value: object) -> str:
        if type(value) is not str or _RESULT_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "result_id must be adj_result_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="after")
    def _validate_result_recomputation(self) -> "AdjudicationResult":
        if self.outcome == "success":
            if self.failure_code is not None or self.failure_details is not None:
                raise ValueError(
                    "success must not carry a failure code or details"
                )
        else:
            if self.failure_code is None or self.failure_details is None:
                raise ValueError(
                    "schema_invalid requires a failure code and details"
                )
        rebuilt_input = _rebuild_normalization_input(self.input)
        if rebuilt_input != self.input:
            raise ValueError(
                "result input must equal its deterministic rebuild"
            )
        rebuilt_clusters = tuple(
            AdjudicationCluster.model_validate(
                {
                    "schema_version": cluster.schema_version,
                    "kind": cluster.kind,
                    "finding_ids": cluster.finding_ids,
                    "findings": cluster.findings,
                    "roles": cluster.roles,
                    "evidence_refs": cluster.evidence_refs,
                    "rationale": cluster.rationale,
                    "cluster_id": cluster.cluster_id,
                }
            )
            for cluster in self.clusters
        )
        if rebuilt_clusters != self.clusters:
            raise ValueError(
                "clusters must equal their deterministic rebuild"
            )
        rebuilt_questions = tuple(
            AdjudicationHumanQuestion.model_validate(
                {
                    "schema_version": question.schema_version,
                    "question": question.question,
                    "reason": question.reason,
                    "source_finding_ids": question.source_finding_ids,
                    "source_question_ids": question.source_question_ids,
                    "source_findings": question.source_findings,
                    "source_questions": question.source_questions,
                    "evidence_refs": question.evidence_refs,
                    "question_id": question.question_id,
                }
            )
            for question in self.human_questions
        )
        if rebuilt_questions != self.human_questions:
            raise ValueError(
                "human_questions must equal their deterministic rebuild"
            )
        adjudicator_input = self.input.prompt.input
        council = adjudicator_input.council_result
        trigger = adjudicator_input.trigger
        finding_by_id = _council_finding_by_id(council)
        question_by_id = _council_question_by_id(council)
        trigger_finding_ids = frozenset(trigger.finding_ids)
        trigger_question_ids = frozenset(trigger.question_ids)
        members_seen = set()
        for cluster in self.clusters:
            if not set(cluster.finding_ids) <= trigger_finding_ids:
                raise ValueError(
                    "cluster finding_ids must stay within trigger scope"
                )
            expected_findings = tuple(
                finding_by_id.get(finding_id)
                for finding_id in cluster.finding_ids
            )
            if (
                any(item is None for item in expected_findings)
                or expected_findings != cluster.findings
            ):
                raise ValueError(
                    "cluster findings must bind exactly to Council source"
                )
            overlap = members_seen & set(cluster.finding_ids)
            if overlap:
                raise ValueError(
                    "a Finding may appear in at most one cluster"
                )
            members_seen.update(cluster.finding_ids)
        for question in self.human_questions:
            if not set(question.source_finding_ids) <= trigger_finding_ids:
                raise ValueError(
                    "question source Finding IDs must stay within "
                    "trigger scope"
                )
            if not set(question.source_question_ids) <= trigger_question_ids:
                raise ValueError(
                    "question source Question IDs must stay within "
                    "trigger scope"
                )
            expected_findings = tuple(
                finding_by_id.get(finding_id)
                for finding_id in question.source_finding_ids
            )
            expected_questions = tuple(
                question_by_id.get(question_id)
                for question_id in question.source_question_ids
            )
            if (
                any(item is None for item in expected_findings)
                or any(item is None for item in expected_questions)
                or expected_findings != question.source_findings
                or expected_questions != question.source_questions
            ):
                raise ValueError(
                    "question sources must bind exactly to Council source"
                )
        expected_preserved_findings = _council_finding_ids(council)
        expected_preserved_questions = _council_question_ids(council)
        if self.preserved_finding_ids != expected_preserved_findings:
            raise ValueError(
                "preserved_finding_ids must equal every original "
                "Council Finding ID"
            )
        if self.preserved_question_ids != expected_preserved_questions:
            raise ValueError(
                "preserved_question_ids must equal every original "
                "Council Question ID"
            )
        expected_dissent = tuple(
            sorted(
                {
                    finding_id
                    for cluster in self.clusters
                    if cluster.kind == "conflict_candidate"
                    for finding_id in cluster.finding_ids
                }
            )
        )
        if self.dissent_finding_ids != expected_dissent:
            raise ValueError(
                "dissent_finding_ids must equal the exact union of all "
                "conflict-candidate members"
            )
        expected_evidence_universe = tuple(
            entry.evidence_id for entry in council.evidence_index
        )
        if self.evidence_id_universe != expected_evidence_universe:
            raise ValueError(
                "evidence_id_universe must equal the Council evidence index"
            )
        route = adjudicator_input.route
        selected = route.selected_candidate
        if (
            self.selected_candidate_id != selected.candidate_id
            or self.selected_provider_ref != selected.provider_ref
            or self.selected_model_ref != selected.model_ref
            or self.route_decision_id != route.decision_id
        ):
            raise ValueError(
                "result route metadata must equal the selected route facts"
            )
        expected_raw_digest = _sha256_digest(
            self.input.raw_response_bytes
        )
        if self.raw_response_digest != expected_raw_digest:
            raise ValueError(
                "raw_response_digest must equal the raw bytes digest"
            )
        if (
            self.usage_status != self.input.usage_status
            or self.input_tokens != self.input.input_tokens
            or self.output_tokens != self.input.output_tokens
            or self.cost_usd != self.input.cost_usd
            or self.started_at != self.input.started_at
            or self.completed_at != self.input.completed_at
            or self.latency_ms != self.input.latency_ms
        ):
            raise ValueError(
                "result usage/timing metadata must equal the exact input "
                "usage/timing facts"
            )
        expected_result_id = _id32(
            "adj_result_",
            self.model_dump(mode="json", exclude={"result_id"}),
        )
        if self.result_id != expected_result_id:
            raise ValueError(
                "result_id must equal the deterministic recomputation"
            )
        return self


def _rebuild_adjudicator_input(
    instance: AdjudicatorInput,
) -> AdjudicatorInput:
    return AdjudicatorInput.model_validate(
        {
            "schema_version": instance.schema_version,
            "council_result": instance.council_result,
            "council_receipt": instance.council_receipt,
            "route": instance.route,
            "trigger": instance.trigger,
            "requested_at": instance.requested_at,
        }
    )


def _rebuild_prompt(instance: AdjudicatorPrompt) -> AdjudicatorPrompt:
    return AdjudicatorPrompt.model_validate(
        {
            "schema_version": instance.schema_version,
            "input": _rebuild_adjudicator_input(instance.input),
            "prompt_text": instance.prompt_text,
        }
    )


def _rebuild_normalization_input(
    instance: AdjudicatorNormalizationInput,
) -> AdjudicatorNormalizationInput:
    return AdjudicatorNormalizationInput.model_validate(
        {
            "schema_version": instance.schema_version,
            "prompt": _rebuild_prompt(instance.prompt),
            "raw_response_bytes": instance.raw_response_bytes,
            "usage_status": instance.usage_status,
            "input_tokens": instance.input_tokens,
            "output_tokens": instance.output_tokens,
            "cost_usd": instance.cost_usd,
            "started_at": instance.started_at,
            "completed_at": instance.completed_at,
            "latency_ms": instance.latency_ms,
        }
    )


def _duplicate_key_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _PayloadError(
                "duplicate_object_key",
                f"duplicate JSON object key {key!r} is not allowed",
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(
        f"non-finite JSON constant {value!r} is not allowed"
    )


def _max_json_depth(text: str) -> int:
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif char in "]}":
            depth -= 1
    return max_depth


def _parse_cluster_item(
    item: object,
    *,
    finding_by_id: dict[str, Finding],
    trigger_finding_ids: frozenset[str],
    index: int,
) -> AdjudicationCluster:
    label = f"clusters[{index}]"
    if type(item) is not dict:
        raise _PayloadError(
            "cluster_item_invalid", f"{label} must be an object"
        )
    extra = set(item) - _CLUSTER_KEYS
    if extra:
        raise _PayloadError(
            "forbidden_field",
            f"{label} carries forbidden field {sorted(extra)[0]!r}",
        )
    missing = _CLUSTER_KEYS - set(item)
    if missing:
        raise _PayloadError(
            "cluster_item_invalid",
            f"{label} is missing required field {sorted(missing)[0]!r}",
        )
    kind = item["kind"]
    if type(kind) is not str or kind not in _CLUSTER_KINDS:
        raise _PayloadError(
            "cluster_item_invalid",
            f"{label}.kind must be duplicate_candidate or "
            "conflict_candidate",
        )
    finding_ids_raw = item["finding_ids"]
    if type(finding_ids_raw) is not list:
        raise _PayloadError(
            "cluster_item_invalid", f"{label}.finding_ids must be an array"
        )
    if len(finding_ids_raw) > _MAX_CLUSTER_MEMBERS:
        raise _PayloadError(
            "item_limit_exceeded",
            f"{label}.finding_ids must contain at most "
            f"{_MAX_CLUSTER_MEMBERS} IDs",
        )
    if len(finding_ids_raw) < 2:
        raise _PayloadError(
            "insufficient_members",
            f"{label}.finding_ids must contain at least two Findings",
        )
    if not all(type(item_id) is str for item_id in finding_ids_raw):
        raise _PayloadError(
            "cluster_item_invalid",
            f"{label}.finding_ids must contain only strings",
        )
    if len(set(finding_ids_raw)) != len(finding_ids_raw):
        raise _PayloadError(
            "cluster_item_invalid",
            f"{label}.finding_ids must be unique within a cluster",
        )
    if not set(finding_ids_raw) <= trigger_finding_ids:
        raise _PayloadError(
            "id_not_in_scope",
            f"{label} references a Finding ID outside the trigger scope",
        )
    ordered_ids = tuple(sorted(finding_ids_raw))
    findings = tuple(finding_by_id[finding_id] for finding_id in ordered_ids)
    if len(findings) != len(ordered_ids):
        raise _PayloadError(
            "id_not_in_scope",
            f"{label} references a Finding ID outside the Council source",
        )
    if (
        kind == "conflict_candidate"
        and len({finding.reviewer_role for finding in findings}) < 2
    ):
        raise _PayloadError(
            "same_role_conflict",
            f"{label} conflict_candidate must span at least two roles",
        )
    rationale = item["rationale"]
    if type(rationale) is not str:
        raise _PayloadError(
            "cluster_item_invalid", f"{label}.rationale must be a string"
        )
    if len(rationale.encode("utf-8")) > _MAX_RATIONALE_BYTES:
        raise _PayloadError(
            "text_limit_exceeded",
            f"{label}.rationale exceeds {_MAX_RATIONALE_BYTES} UTF-8 bytes",
        )
    try:
        return AdjudicationCluster.model_validate(
            {
                "schema_version": "v1",
                "kind": kind,
                "finding_ids": ordered_ids,
                "findings": findings,
                "rationale": rationale,
            }
        )
    except ValidationError as exc:
        message = exc.errors()[0].get("msg", "invalid cluster")
        raise _PayloadError(
            "cluster_item_invalid", f"{label}: {message}"
        ) from exc


def _parse_question_item(
    item: object,
    *,
    finding_by_id: dict[str, Finding],
    question_by_id: dict[str, ReviewQuestion],
    trigger_finding_ids: frozenset[str],
    trigger_question_ids: frozenset[str],
    index: int,
) -> AdjudicationHumanQuestion:
    label = f"human_questions[{index}]"
    if type(item) is not dict:
        raise _PayloadError(
            "question_item_invalid", f"{label} must be an object"
        )
    extra = set(item) - _QUESTION_KEYS
    if extra:
        raise _PayloadError(
            "forbidden_field",
            f"{label} carries forbidden field {sorted(extra)[0]!r}",
        )
    missing = _QUESTION_KEYS - set(item)
    if missing:
        raise _PayloadError(
            "question_item_invalid",
            f"{label} is missing required field {sorted(missing)[0]!r}",
        )
    question = item["question"]
    if type(question) is not str:
        raise _PayloadError(
            "question_item_invalid", f"{label}.question must be a string"
        )
    if len(question.encode("utf-8")) > _MAX_QUESTION_BYTES:
        raise _PayloadError(
            "text_limit_exceeded",
            f"{label}.question exceeds {_MAX_QUESTION_BYTES} UTF-8 bytes",
        )
    reason = item["reason"]
    if type(reason) is not str or reason not in _QUESTION_REASONS:
        raise _PayloadError(
            "question_item_invalid",
            f"{label}.reason must be conflict, insufficient_evidence, "
            "or consolidation",
        )

    def source_ids(raw, field_name: str) -> tuple[str, ...]:
        if type(raw) is not list:
            raise _PayloadError(
                "question_item_invalid",
                f"{label}.{field_name} must be an array",
            )
        if len(raw) > _MAX_SOURCE_REFS:
            raise _PayloadError(
                "item_limit_exceeded",
                f"{label}.{field_name} must contain at most "
                f"{_MAX_SOURCE_REFS} IDs",
            )
        if not all(type(item_id) is str for item_id in raw):
            raise _PayloadError(
                "question_item_invalid",
                f"{label}.{field_name} must contain only strings",
            )
        if len(set(raw)) != len(raw):
            raise _PayloadError(
                "question_item_invalid",
                f"{label}.{field_name} must be unique",
            )
        return tuple(sorted(raw))

    source_finding_ids = source_ids(
        item["source_finding_ids"], "source_finding_ids"
    )
    source_question_ids = source_ids(
        item["source_question_ids"], "source_question_ids"
    )
    if not source_finding_ids and not source_question_ids:
        raise _PayloadError(
            "insufficient_sources",
            f"{label} must reference at least one Finding or Question",
        )
    if not set(source_finding_ids) <= trigger_finding_ids:
        raise _PayloadError(
            "id_not_in_scope",
            f"{label} references a Finding ID outside the trigger scope",
        )
    if not set(source_question_ids) <= trigger_question_ids:
        raise _PayloadError(
            "id_not_in_scope",
            f"{label} references a Question ID outside the trigger scope",
        )
    source_findings = tuple(
        finding_by_id[finding_id] for finding_id in source_finding_ids
    )
    source_questions = tuple(
        question_by_id[question_id] for question_id in source_question_ids
    )
    if len(source_findings) != len(source_finding_ids) or len(
        source_questions
    ) != len(source_question_ids):
        raise _PayloadError(
            "id_not_in_scope",
            f"{label} references an ID outside the Council source",
        )
    try:
        return AdjudicationHumanQuestion.model_validate(
            {
                "schema_version": "v1",
                "question": question,
                "reason": reason,
                "source_finding_ids": source_finding_ids,
                "source_question_ids": source_question_ids,
                "source_findings": source_findings,
                "source_questions": source_questions,
            }
        )
    except ValidationError as exc:
        message = exc.errors()[0].get("msg", "invalid human question")
        raise _PayloadError(
            "question_item_invalid", f"{label}: {message}"
        ) from exc


def _schema_invalid_result(
    normalization_input: AdjudicatorNormalizationInput,
    code: str,
    details: str,
) -> AdjudicationResult:
    return AdjudicationResult.model_validate(
        _result_fields(
            normalization_input,
            outcome="schema_invalid",
            failure_code=code,
            failure_details=details,
            clusters=(),
            human_questions=(),
        )
    )


def _normalize_payload(
    normalization_input: AdjudicatorNormalizationInput,
) -> AdjudicationResult:
    try:
        raw = normalization_input.raw_response_bytes
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _schema_invalid_result(
                normalization_input,
                "invalid_utf8",
                "raw response bytes are not strict UTF-8",
            )
        depth = _max_json_depth(text)
        if depth > _MAX_JSON_DEPTH:
            return _schema_invalid_result(
                normalization_input,
                "depth_exceeded",
                f"raw response nesting exceeds {_MAX_JSON_DEPTH}",
            )
        try:
            payload = json.loads(
                text,
                object_pairs_hook=_duplicate_key_object,
                parse_constant=_reject_non_finite,
            )
        except _PayloadError as exc:
            return _schema_invalid_result(
                normalization_input, exc.code, exc.details
            )
        except Exception:
            return _schema_invalid_result(
                normalization_input,
                "malformed_json",
                "raw response is not one strict JSON object",
            )
        if type(payload) is not dict:
            return _schema_invalid_result(
                normalization_input,
                "root_not_object",
                "raw response root must be a JSON object",
            )
        if set(payload) != _ALLOWED_TOP_LEVEL_KEYS:
            return _schema_invalid_result(
                normalization_input,
                "top_level_fields_invalid",
                "raw response must contain exactly clusters and "
                "human_questions",
            )
        clusters_raw = payload["clusters"]
        questions_raw = payload["human_questions"]
        if type(clusters_raw) is not list:
            return _schema_invalid_result(
                normalization_input,
                "cluster_item_invalid",
                "clusters must be an array",
            )
        if len(clusters_raw) > _MAX_CLUSTERS:
            return _schema_invalid_result(
                normalization_input,
                "item_limit_exceeded",
                f"clusters must contain at most {_MAX_CLUSTERS} items",
            )
        if type(questions_raw) is not list:
            return _schema_invalid_result(
                normalization_input,
                "question_item_invalid",
                "human_questions must be an array",
            )
        if len(questions_raw) > _MAX_HUMAN_QUESTIONS:
            return _schema_invalid_result(
                normalization_input,
                "item_limit_exceeded",
                "human_questions must contain at most "
                f"{_MAX_HUMAN_QUESTIONS} items",
            )
        adjudicator_input = normalization_input.prompt.input
        council = adjudicator_input.council_result
        trigger = adjudicator_input.trigger
        finding_by_id = _council_finding_by_id(council)
        question_by_id = _council_question_by_id(council)
        trigger_finding_ids = frozenset(trigger.finding_ids)
        trigger_question_ids = frozenset(trigger.question_ids)
        clusters = []
        members_seen = set()
        for index, item in enumerate(clusters_raw):
            cluster = _parse_cluster_item(
                item,
                finding_by_id=finding_by_id,
                trigger_finding_ids=trigger_finding_ids,
                index=index,
            )
            overlap = members_seen & set(cluster.finding_ids)
            if overlap:
                raise _PayloadError(
                    "duplicate_cluster_membership",
                    f"clusters[{index}] reuses Finding IDs from another "
                    "cluster",
                )
            members_seen.update(cluster.finding_ids)
            clusters.append(cluster)
        questions = tuple(
            _parse_question_item(
                item,
                finding_by_id=finding_by_id,
                question_by_id=question_by_id,
                trigger_finding_ids=trigger_finding_ids,
                trigger_question_ids=trigger_question_ids,
                index=index,
            )
            for index, item in enumerate(questions_raw)
        )
        return AdjudicationResult.model_validate(
            _result_fields(
                normalization_input,
                outcome="success",
                failure_code=None,
                failure_details=None,
                clusters=tuple(clusters),
                human_questions=questions,
            )
        )
    except _PayloadError as exc:
        return _schema_invalid_result(
            normalization_input, exc.code, exc.details
        )
    except Exception:
        return _schema_invalid_result(
            normalization_input,
            "malformed_json",
            "model payload normalization failed closed",
        )


class Adjudicator:
    """Stateless deterministic adjudication overlay entry point."""

    @staticmethod
    def prepare(adjudicator_input: AdjudicatorInput) -> AdjudicatorPrompt:
        if type(adjudicator_input) is not AdjudicatorInput:
            raise TypeError(
                "input must be an exact AdjudicatorInput instance"
            )
        rebuilt = _rebuild_adjudicator_input(adjudicator_input)
        if rebuilt != adjudicator_input:
            raise ValueError(
                "forged AdjudicatorInput must be rejected before prompt "
                "preparation"
            )
        prompt_text = _build_prompt_text(rebuilt)
        return AdjudicatorPrompt.model_validate(
            {
                "schema_version": "v1",
                "input": rebuilt,
                "prompt_text": prompt_text,
            }
        )

    @staticmethod
    def normalize(
        normalization_input: AdjudicatorNormalizationInput,
    ) -> AdjudicationResult:
        if type(normalization_input) is not AdjudicatorNormalizationInput:
            raise TypeError(
                "input must be an exact "
                "AdjudicatorNormalizationInput instance"
            )
        rebuilt = _rebuild_normalization_input(normalization_input)
        if rebuilt != normalization_input:
            raise ValueError(
                "forged prompt/input/route objects must be rejected before "
                "payload normalization"
            )
        return _normalize_payload(rebuilt)
