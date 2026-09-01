"""V2-P4-07 Council Execution Receipt v1 focused TDD tests.

The module under test is a pure, deterministic structured record. It makes no
model/provider/tool call, no routing, no Council run, no Gate evaluation, no
adjudication, no persistence, and no filesystem/network/subprocess/env/time/
random access. ``selected`` routes and ``success`` facts are records, never a
PASS/approval/acceptance signal.
"""

import ast
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ValidationError

import assurance
import assurance.execution_receipt as execution_receipt_module
from assurance import (
    ExecutionReceipt,
    ReviewerFailureOutcome,
)
from assurance.execution_receipt import (
    CouncilExecutionReceipt,
    CouncilExecutionStep,
    CouncilReceiptBuilder,
    CouncilTopologySnapshot,
    ReceiptRouteSnapshot,
    ReviewerExecutionFact,
)
from assurance.model_routing import (
    ModelRouteDecision,
    ModelRouteMatch,
    ModelRouteRule,
    ModelRouteTarget,
    ModelRouter,
    ModelRoutingPolicy,
    ModelTierAlias,
)
from assurance.review_council import (
    CouncilPlan,
    CouncilRunResult,
)
from assurance.contracts import ExecutionStep
from tests.test_assurance_model_routing import (
    _alias,
    _budget,
    _candidate,
    _match,
    _rule,
    _target,
)
from tests.test_assurance_review_council import (
    ROLE_ORDER,
    _default_inputs,
    _finding_output,
    _plan,
    _reviewer_input,
    _risk_result,
    _subject,
)


START_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
RECORDED_TIME = datetime(2026, 8, 25, 8, 0, 6, 0, tzinfo=timezone.utc)

ROLE_START = {
    "intent": datetime(2026, 8, 25, 8, 0, 0, 500000, tzinfo=timezone.utc),
    "architecture": datetime(2026, 8, 25, 8, 0, 1, 0, tzinfo=timezone.utc),
    "operability": datetime(2026, 8, 25, 8, 0, 2, 0, tzinfo=timezone.utc),
}
ROLE_COMPLETED = {
    "intent": datetime(2026, 8, 25, 8, 0, 3, 200000, tzinfo=timezone.utc),
    "architecture": datetime(2026, 8, 25, 8, 0, 5, 0, tzinfo=timezone.utc),
    "operability": datetime(2026, 8, 25, 8, 0, 4, 500000, tzinfo=timezone.utc),
}
ROLE_LATENCY_MS = {
    "intent": 2700,
    "architecture": 4000,
    "operability": 2500,
}
ROLE_TOKENS = {
    "intent": (2100, 800, 0.42),
    "architecture": (3300, 1200, 0.78),
    "operability": (4700, 1600, 1.05),
}

NEW_PUBLIC_NAMES = frozenset(
    {
        "ReceiptRouteSnapshot",
        "ReviewerExecutionFact",
        "CouncilExecutionStep",
        "CouncilTopologySnapshot",
        "CouncilExecutionReceipt",
        "CouncilReceiptBuilder",
    }
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _standard_only_policy():
    alias = _alias(
        "standard",
        (
            _candidate(
                "candidate-standard",
                "approved-provider",
                "approved-model",
                "approved-provider",
            ),
        ),
    )
    return ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "rule-council-standard",
                _match(phase="review"),
                _target(
                    "standard",
                    token_budget_cap=20_000,
                    cost_budget_cap_usd=4.0,
                ),
            ),
        ),
        aliases=(alias,),
    )


def _fallback_policy():
    alias = _alias(
        "standard",
        (
            _candidate(
                "candidate-light",
                "local-provider",
                "local-model",
                "local-only",
            ),
            _candidate(
                "candidate-standard",
                "approved-provider",
                "approved-model",
                "approved-provider",
            ),
        ),
    )
    return ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "rule-council-fallback",
                _match(phase="review"),
                _target(
                    "standard",
                    token_budget_cap=20_000,
                    cost_budget_cap_usd=4.0,
                ),
            ),
        ),
        aliases=(alias,),
    )


def _no_match_policy():
    alias = _alias("standard")
    return ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "rule-extraction-only",
                _match(phase="extraction"),
                _target("standard", token_budget_cap=20_000, cost_budget_cap_usd=4.0),
            ),
        ),
        aliases=(alias,),
    )


def _role_index(role):
    return ROLE_ORDER.index(role)


def _route(
    plan,
    role,
    *,
    policy=None,
    provider_boundary="any",
    available_candidate_ids=None,
    allowed_provider_refs=None,
):
    if policy is None:
        policy = _standard_only_policy()
    reviewer_input = plan.inputs[_role_index(role)]
    if available_candidate_ids is None:
        available_candidate_ids = ("candidate-standard",)
    if allowed_provider_refs is None:
        allowed_provider_refs = ("approved-provider",)
    request = _budget_request(
        reviewer_input,
        phase="review",
        agent_role=role,
        provider_boundary=provider_boundary,
        available_candidate_ids=available_candidate_ids,
        allowed_provider_refs=allowed_provider_refs,
    )
    return ModelRouter.route(policy, request)


def _budget_request(
    reviewer_input,
    *,
    phase="review",
    agent_role=None,
    provider_boundary="any",
    available_candidate_ids=None,
    allowed_provider_refs=None,
    risk_result=None,
    budget=None,
):
    from tests.test_assurance_model_routing import _request

    return _request(
        reviewer_input.risk_result if risk_result is None else risk_result,
        phase=phase,
        agent_role=agent_role,
        priority="normal",
        provider_boundary=provider_boundary,
        available_candidate_ids=available_candidate_ids,
        allowed_provider_refs=allowed_provider_refs,
        budget=(
            _budget(
                reviewer_input.token_budget,
                reviewer_input.cost_budget_usd,
            )
            if budget is None
            else budget
        ),
    )


def _fact(
    role,
    plan,
    *,
    result="success",
    provider="approved-provider",
    model="approved-model",
    grants=None,
    usage_status="measured",
    input_tokens=None,
    output_tokens=None,
    cost_usd=None,
    started_at=None,
    completed_at=None,
    latency_ms=None,
    failure_code=None,
):
    reviewer_input = plan.inputs[_role_index(role)]
    if grants is None:
        grants = reviewer_input.tool_allowlist
    if result in (
        "success",
        "failure",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "schema_invalid",
    ):
        if started_at is None:
            started_at = ROLE_START[role]
        if completed_at is None:
            completed_at = ROLE_COMPLETED[role]
        if latency_ms is None:
            latency_ms = ROLE_LATENCY_MS[role]
        if usage_status == "measured":
            if input_tokens is None:
                input_tokens, output_tokens, cost_usd = ROLE_TOKENS[role]
    else:
        grants = ()
        provider = None
        model = None
        usage_status = "not_applicable"
        started_at = None
        completed_at = None
        latency_ms = None
    values = {
        "schema_version": "v1",
        "role": role,
        "result": result,
        "actual_provider": provider,
        "actual_model": model,
        "actual_tool_grants": grants,
        "usage_status": usage_status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "started_at": started_at,
        "completed_at": completed_at,
        "latency_ms": latency_ms,
        "failure_code": failure_code,
    }
    return ReviewerExecutionFact.model_validate(values)


def _default_failure_codes():
    return {
        "failure": "execution_failed",
        "timeout": "timeout",
        "cancelled": "cancelled",
        "budget_exceeded": "budget_exceeded",
        "schema_invalid": "schema_invalid",
        "skipped": "skipped",
        "blocked": "blocked",
    }


def _failure_outcome(result):
    code = _default_failure_codes()[result]
    return ReviewerFailureOutcome.model_validate(
        {
            "schema_version": "v1",
            "code": code,
            "details": f"reviewer closed out with {result}",
        }
    )


def _success_result(plan, facts):
    outputs = tuple(
        _finding_output(
            plan.inputs[_role_index(role)],
            outcome="success",
            completed_at=facts[_role_index(role)].completed_at,
        )
        for role in ROLE_ORDER
    )
    return CouncilRunResult(
        schema_version="v1",
        plan=plan,
        outputs=outputs,
    )


def _build(
    plan,
    routes,
    facts,
    *,
    result=None,
    run_id="run-council-1",
    run_started_at=START_TIME,
    recorded_at=RECORDED_TIME,
):
    return CouncilReceiptBuilder.build(
        run_id=run_id,
        plan=plan,
        routes=routes,
        facts=facts,
        result=result,
        run_started_at=run_started_at,
        recorded_at=recorded_at,
    )


def _all_success_receipt(*, policy=None, run_id="run-council-1"):
    plan = _plan()
    routes = tuple(
        _route(plan, role, policy=policy) for role in ROLE_ORDER
    )
    facts = tuple(
        _fact(role, plan, failure_code=None) for role in ROLE_ORDER
    )
    result = _success_result(plan, facts)
    return _build(
        plan,
        routes,
        facts,
        result=result,
        run_id=run_id,
    )


def _plan_with_subject(subject_digest):
    risk = _risk_result(subject_digest)
    inputs = tuple(
        _reviewer_input(
            role,
            subject=_subject(subject_digest),
            risk_result=risk,
        )
        for role in ROLE_ORDER
    )
    return _plan(inputs)


def _assert_immutable_graph(value, seen=None):
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            _assert_immutable_graph(getattr(value, name), seen)
    elif isinstance(value, tuple):
        for item in value:
            _assert_immutable_graph(item, seen)
    elif isinstance(value, (list, dict, set)):
        raise AssertionError(
            "mutable container found: " + type(value).__name__
        )


def test_strict_contracts_field_order_immutability_and_json_round_trip():
    receipt = _all_success_receipt()

    assert receipt.schema_version == "v1"
    assert receipt.receipt_version == "council-execution-receipt-v1"
    assert tuple(type(receipt).model_fields)[:8] == (
        "schema_version",
        "receipt_version",
        "receipt_id",
        "run_id",
        "subject_digest",
        "plan",
        "result",
        "plan_digest",
    )
    assert type(receipt.steps) is tuple
    assert len(receipt.steps) == 3
    for step in receipt.steps:
        assert type(step) is CouncilExecutionStep
        assert type(step.route) is ReceiptRouteSnapshot
        assert type(step.fact) is ReviewerExecutionFact
        assert type(step.route.attempts) is tuple
        assert type(step.fact.actual_tool_grants) is tuple
    assert type(receipt.topology) is CouncilTopologySnapshot
    assert type(receipt.gate_receipt) is ExecutionReceipt

    rebuilt = CouncilExecutionReceipt.model_validate_json(
        receipt.model_dump_json()
    )
    assert rebuilt == receipt

    for model in (
        receipt,
        receipt.topology,
        receipt.steps[0],
        receipt.steps[0].route,
        receipt.steps[0].fact,
        receipt.gate_receipt,
        receipt.gate_receipt.steps[0],
    ):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"
        _assert_immutable_graph(model)

    with pytest.raises(ValidationError):
        receipt.steps[0].fact.result = "blocked"
    with pytest.raises(ValidationError):
        receipt.topology.actual_state = "interrupted"
    with pytest.raises(ValidationError):
        receipt.overall_result = "partial"

    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {
                "schema_version": "v1",
                "receipt_version": "council-execution-receipt-v1",
                "receipt_id": "x",
                "run_id": "run",
                "subject_digest": "x",
                "plan": receipt.plan,
                "result": receipt.result,
                "plan_digest": "x",
                "result_digest": "x",
                "topology": receipt.topology,
                "steps": list(receipt.steps),
                "overall_result": "success",
                "usage_status": "measured",
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 1.0,
                "run_started_at": START_TIME,
                "recorded_at": RECORDED_TIME,
                "elapsed_ms": 6000,
                "gate_receipt": receipt.gate_receipt,
            }
        )


def test_receipt_route_snapshot_requires_exact_decision_and_rejects_grammar():
    plan = _plan()
    decision = _route(plan, "intent")
    snapshot = ReceiptRouteSnapshot.from_decision(decision)
    assert snapshot.decision_id == decision.decision_id
    assert snapshot.policy_digest == decision.policy.policy_digest
    assert snapshot.role == "intent"
    assert snapshot.phase == "review"
    assert snapshot.outcome == "selected"
    assert snapshot.matched_rule_id == "rule-council-standard"
    assert snapshot.selected_candidate is not None
    assert snapshot.selected_candidate.candidate_id == "candidate-standard"
    assert snapshot.allocated_budget is not None

    with pytest.raises(TypeError):
        ReceiptRouteSnapshot.from_decision({"outcome": "selected"})

    forged = ReceiptRouteSnapshot.model_construct(
        **{
            **snapshot.model_dump(mode="python"),
            "attempts": snapshot.attempts,
            "outcome": "blocked",
            "block_reason": "no_eligible_candidate",
            "selected_candidate": None,
        }
    )
    with pytest.raises(ValidationError):
        CouncilExecutionStep.model_validate(
            {
                "schema_version": "v1",
                "sequence": 0,
                "role": "intent",
                "route": forged,
                "planned_tool_grants": plan.inputs[0].tool_allowlist,
                "planned_timeout_seconds": plan.inputs[0].timeout_seconds,
                "fact": _fact("intent", plan, failure_code=None),
                "output_schema_ref": "finding-output.v1",
                "output_schema_status": "valid",
                "output_digest": "sha256:" + "0" * 64,
            }
        )


def test_receipt_route_snapshot_rejects_final_attempt_candidate_mismatch():
    plan = _plan()
    snapshot = ReceiptRouteSnapshot.from_decision(_route(plan, "intent"))
    assert snapshot.outcome == "selected"
    assert snapshot.attempts[-1].reason == "selected"
    assert snapshot.attempts[-1].candidate_id == "candidate-standard"

    mutated = json.loads(snapshot.model_dump_json())
    mutated["attempts"][-1]["candidate_id"] = "candidate-other"
    assert mutated["selected_candidate"]["candidate_id"] == "candidate-standard"

    with pytest.raises(ValidationError):
        ReceiptRouteSnapshot.model_validate_json(json.dumps(mutated))


def test_reviewer_fact_strict_usage_and_failure_code_rules():
    plan = _plan()
    with pytest.raises(ValidationError):
        _fact(
            "intent",
            plan,
            result="success",
            provider=None,
            model=None,
            failure_code=None,
        )
    with pytest.raises(ValidationError):
        _fact(
            "intent",
            plan,
            result="failure",
            failure_code=None,
        )
    with pytest.raises(ValidationError):
        _fact(
            "intent",
            plan,
            result="failure",
            failure_code="timeout",
        )
    with pytest.raises(ValidationError):
        _fact(
            "intent",
            plan,
            result="success",
            usage_status="unavailable",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.1,
            failure_code=None,
        )
    with pytest.raises(ValidationError):
        _fact(
            "intent",
            plan,
            result="success",
            usage_status="not_applicable",
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            failure_code=None,
        )
    skipped = _fact("intent", plan, result="skipped", failure_code="skipped")
    assert skipped.usage_status == "not_applicable"
    assert skipped.actual_tool_grants == ()
    assert skipped.actual_provider is None
    assert skipped.started_at is None
    assert skipped.latency_ms is None
    rebuilt = ReviewerExecutionFact.model_validate_json(
        skipped.model_dump_json()
    )
    assert rebuilt == skipped


def test_all_success_receipt_planned_actual_topology_usage_and_gate():
    receipt = _all_success_receipt()
    plan = receipt.plan
    topology = receipt.topology

    assert topology.topology_version == "parallel-isolated-v1"
    assert topology.planned_roles == ROLE_ORDER
    assert topology.start_barrier_roles == ROLE_ORDER
    assert topology.planned_dependencies == ()
    assert topology.required_roles == ("intent",)
    assert topology.actual_state == "completed"
    assert topology.executed_roles == ROLE_ORDER
    assert topology.skipped_or_blocked_roles == ()
    assert topology.completion_order == (
        "intent",
        "operability",
        "architecture",
    )
    assert topology.actual_dependencies == ()

    assert receipt.plan_digest == _sha256(
        json.dumps(
            plan.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    assert receipt.result_digest == _sha256(
        json.dumps(
            receipt.result.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )

    for index, role in enumerate(ROLE_ORDER):
        step = receipt.steps[index]
        route = step.route
        fact = step.fact
        reviewer_input = plan.inputs[index]
        assert step.sequence == index
        assert step.role == role
        assert step.output_schema_ref == "finding-output.v1"
        assert step.output_schema_status == "valid"
        assert route.outcome == "selected"
        assert route.matched_tier_alias == "standard"
        assert route.selected_candidate.provider_ref == "approved-provider"
        assert route.selected_candidate.model_ref == "approved-model"
        assert route.allocated_budget.token_budget_cap == (
            reviewer_input.token_budget
        )
        assert route.allocated_budget.cost_budget_cap_usd == (
            reviewer_input.cost_budget_usd
        )
        assert step.planned_tool_grants == reviewer_input.tool_allowlist
        assert step.planned_timeout_seconds == reviewer_input.timeout_seconds
        assert fact.actual_provider == "approved-provider"
        assert fact.actual_model == "approved-model"
        assert fact.actual_tool_grants == reviewer_input.tool_allowlist
        assert fact.usage_status == "measured"
        assert fact.latency_ms == ROLE_LATENCY_MS[role]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", step.output_digest)

    assert receipt.overall_result == "success"
    assert receipt.usage_status == "measured"
    assert receipt.input_tokens == 10_100
    assert receipt.output_tokens == 3_600
    assert receipt.cost_usd == pytest.approx(2.25)
    assert receipt.elapsed_ms == 6_000
    assert receipt.run_started_at == START_TIME
    assert receipt.recorded_at == RECORDED_TIME

    gate = receipt.gate_receipt
    assert gate.schema_version == "v1"
    assert gate.run_id == receipt.run_id
    assert gate.subject_digest == receipt.subject_digest
    assert gate.started_at == receipt.run_started_at
    assert gate.completed_at == receipt.recorded_at
    assert gate.overall_result == "success"
    assert gate.input_tokens == receipt.input_tokens
    assert gate.output_tokens == receipt.output_tokens
    assert gate.cost_usd == receipt.cost_usd
    assert tuple(step.planned_role for step in gate.steps) == ROLE_ORDER
    for index, step in enumerate(gate.steps):
        rich_step = receipt.steps[index]
        assert step.actual_role == ROLE_ORDER[index]
        assert step.model_ref == "approved-model"
        assert step.provider == "approved-provider"
        assert step.tool_grants == receipt.plan.inputs[index].tool_allowlist
        assert step.routing_rule == "rule-council-standard"
        assert step.fallback_reason is None
        assert step.result == "success"
        assert step.schema_status == "valid"
        assert step.timeout_seconds == receipt.plan.inputs[index].timeout_seconds
        assert step.token_budget == rich_step.route.allocated_budget.token_budget_cap
    assert re.fullmatch(r"council_receipt_[0-9a-f]{64}", receipt.receipt_id)
    assert re.fullmatch(r"gate_[0-9a-f]{64}", gate.receipt_id)


def test_same_alias_fallback_attempt_and_reason_preserved_in_gate():
    plan = _plan()
    policy = _fallback_policy()
    routes = tuple(
        _route(
            plan,
            role,
            policy=policy,
            provider_boundary="approved-provider",
            available_candidate_ids=("candidate-light", "candidate-standard"),
            allowed_provider_refs=("approved-provider",),
        )
        for role in ROLE_ORDER
    )
    facts = tuple(
        _fact(role, plan, failure_code=None) for role in ROLE_ORDER
    )
    receipt = _build(
        plan,
        routes,
        facts,
        result=_success_result(plan, facts),
    )

    for index, step in enumerate(receipt.steps):
        assert tuple(
            attempt.candidate_id for attempt in step.route.attempts
        ) == ("candidate-light", "candidate-standard")
        assert tuple(
            attempt.reason for attempt in step.route.attempts
        ) == ("provider_not_allowed", "selected")
        assert receipt.gate_receipt.steps[index].routing_rule == (
            "rule-council-fallback"
        )
        assert receipt.gate_receipt.steps[index].fallback_reason == (
            "provider_not_allowed"
        )


def test_one_blocked_route_no_result_overall_blocked_gate_absent():
    plan = _plan()
    routes = (
        _route(plan, "intent", policy=_no_match_policy()),
        _route(plan, "architecture"),
        _route(plan, "operability"),
    )
    assert routes[0].outcome == "blocked"
    assert routes[0].block_reason == "no_matching_rule"
    assert routes[0].attempts == ()
    assert routes[0].allocated_budget is None
    facts = (
        _fact("intent", plan, result="blocked", failure_code="blocked"),
        _fact("architecture", plan, result="skipped", failure_code="skipped"),
        _fact("operability", plan, result="skipped", failure_code="skipped"),
    )
    receipt = _build(plan, routes, facts, result=None)

    assert receipt.overall_result == "blocked"
    assert receipt.usage_status == "not_applicable"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.cost_usd is None
    assert receipt.result is None
    assert receipt.result_digest is None
    assert receipt.topology.actual_state == "not_started"
    assert receipt.topology.executed_roles == ()
    assert receipt.topology.skipped_or_blocked_roles == ROLE_ORDER
    assert receipt.topology.completion_order == ()

    gate = receipt.gate_receipt
    assert gate.overall_result == "blocked"
    assert gate.steps[0].planned_role == "intent"
    assert gate.steps[0].actual_role is None
    assert gate.steps[0].model_ref is None
    assert gate.steps[0].provider is None
    assert gate.steps[0].result == "blocked"
    assert gate.steps[0].schema_status == "not_produced"
    assert gate.steps[1].result == "skipped"
    assert gate.steps[2].result == "skipped"
    assert gate.input_tokens == 0
    assert gate.output_tokens == 0
    assert gate.cost_usd == 0
    assert "PASS" not in receipt.model_dump_json()
    assert "approval" not in receipt.model_dump_json()


def test_required_role_skipped_selected_route_blocked_optional_skip_never_success():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = (
        _fact("intent", plan, result="skipped", failure_code="skipped"),
        _fact("architecture", plan, result="failure", failure_code="execution_failed"),
        _fact("operability", plan, result="timeout", failure_code="timeout"),
    )
    receipt = _build(plan, routes, facts, result=None)
    assert receipt.overall_result == "blocked"
    assert receipt.topology.actual_state == "interrupted"
    assert receipt.topology.executed_roles == (
        "architecture",
        "operability",
    )
    assert receipt.topology.skipped_or_blocked_roles == ("intent",)
    assert receipt.overall_result != "success"

    optional_skip = (
        _fact("intent", plan, result="success", failure_code=None),
        _fact("architecture", plan, result="success", failure_code=None),
        _fact("operability", plan, result="skipped", failure_code="skipped"),
    )
    with pytest.raises(ValueError):
        _build(plan, routes, optional_skip, result=None)

    # An optional skip can never be part of a success receipt: a bound Council
    # result requires all three outputs, and a skipped role cannot bind to an
    # output outcome.
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            optional_skip,
            result=_success_result(plan, facts),
        )


def test_failure_timeout_cancelled_budget_schema_fail_closed_mappings():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    cases = {
        "failure": ("failure", "execution_failed", "failure", "invalid"),
        "timeout": ("timeout", "timeout", "timeout", "not_produced"),
        "budget_exceeded": (
            "budget_exceeded",
            "budget_exceeded",
            "failure",
            "invalid",
        ),
        "schema_invalid": (
            "schema_invalid",
            "schema_invalid",
            "failure",
            "invalid",
        ),
    }
    for outcome, code, gate_result, gate_schema in cases.values():
        facts = (
            _fact(
                "intent",
                plan,
                result=outcome,
                failure_code=code,
            ),
            _fact("architecture", plan, failure_code=None),
            _fact("operability", plan, failure_code=None),
        )
        outputs = tuple(
            _finding_output(
                plan.inputs[index],
                outcome=facts[index].result,
                failure=(
                    None
                    if facts[index].result == "success"
                    else _failure_outcome(facts[index].result)
                ),
                completed_at=facts[index].completed_at,
            )
            for index in range(3)
        )
        result = CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=outputs,
        )
        receipt = _build(plan, routes, facts, result=result)
        assert receipt.overall_result == "failure"
        assert receipt.steps[0].output_schema_status == "synthetic"
        assert receipt.steps[0].output_digest is not None
        gate_step = receipt.gate_receipt.steps[0]
        assert gate_step.result == gate_result
        assert gate_step.schema_status == gate_schema

    cancelled_facts = (
        _fact("intent", plan, result="cancelled", failure_code="cancelled"),
        _fact("architecture", plan, failure_code=None),
        _fact("operability", plan, failure_code=None),
    )
    cancelled_outputs = tuple(
        _finding_output(
            plan.inputs[index],
            outcome=cancelled_facts[index].result,
            failure=(
                None
                if cancelled_facts[index].result == "success"
                else _failure_outcome(cancelled_facts[index].result)
            ),
            completed_at=cancelled_facts[index].completed_at,
        )
        for index in range(3)
    )
    cancelled = _build(
        plan,
        routes,
        cancelled_facts,
        result=CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=cancelled_outputs,
        ),
    )
    assert cancelled.overall_result == "cancelled"
    assert cancelled.gate_receipt.steps[0].result == "cancelled"
    assert cancelled.gate_receipt.overall_result == "cancelled"


def test_usage_measured_unavailable_partial_not_applicable_no_zero_ambiguity():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)

    measured = _all_success_receipt()
    assert measured.usage_status == "measured"
    assert measured.input_tokens == 10_100
    assert measured.gate_receipt.input_tokens == 10_100

    unavailable_facts = tuple(
        _fact(
            role,
            plan,
            result="failure",
            failure_code="execution_failed",
            usage_status="unavailable",
        )
        for role in ROLE_ORDER
    )
    unavailable = _build(
        plan,
        routes,
        unavailable_facts,
        result=None,
    )
    assert unavailable.usage_status == "unavailable"
    assert unavailable.input_tokens is None
    assert unavailable.output_tokens is None
    assert unavailable.cost_usd is None
    assert unavailable.gate_receipt.input_tokens == 0
    assert unavailable.gate_receipt.output_tokens == 0
    assert unavailable.gate_receipt.cost_usd == 0

    partial_facts = (
        _fact("intent", plan, failure_code=None),
        _fact(
            "architecture",
            plan,
            result="failure",
            failure_code="execution_failed",
            usage_status="unavailable",
        ),
        _fact("operability", plan, failure_code=None),
    )
    partial_outputs = tuple(
        _finding_output(
            plan.inputs[index],
            outcome=partial_facts[index].result,
            failure=(
                None
                if partial_facts[index].result == "success"
                else _failure_outcome(partial_facts[index].result)
            ),
            completed_at=partial_facts[index].completed_at,
        )
        for index in range(3)
    )
    partial = _build(
        plan,
        routes,
        partial_facts,
        result=CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=partial_outputs,
        ),
    )
    assert partial.usage_status == "partial"
    assert partial.input_tokens is None
    assert partial.output_tokens is None
    assert partial.cost_usd is None
    assert partial.gate_receipt.input_tokens == 0

    blocked_plan = _plan()
    blocked_routes = (
        _route(blocked_plan, "intent", policy=_no_match_policy()),
        _route(blocked_plan, "architecture"),
        _route(blocked_plan, "operability"),
    )
    blocked = _build(
        blocked_plan,
        blocked_routes,
        (
            _fact(
                "intent",
                blocked_plan,
                result="blocked",
                failure_code="blocked",
            ),
            _fact(
                "architecture",
                blocked_plan,
                result="skipped",
                failure_code="skipped",
            ),
            _fact(
                "operability",
                blocked_plan,
                result="skipped",
                failure_code="skipped",
            ),
        ),
        result=None,
    )
    assert blocked.usage_status == "not_applicable"
    assert blocked.input_tokens is None
    assert blocked.gate_receipt.input_tokens == 0


def test_binding_rejections_wrong_order_role_risk_subject_candidate_tools_budget_time():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan, failure_code=None) for role in ROLE_ORDER)
    result = _success_result(plan, facts)

    with pytest.raises(ValueError):
        _build(plan, routes[::-1], facts, result=result)
    with pytest.raises(ValueError):
        _build(plan, routes, facts[::-1], result=result)
    with pytest.raises(ValueError):
        _build(plan, routes[:2], facts, result=result)
    with pytest.raises(ValueError):
        _build(plan, routes, facts[:2], result=result)

    wrong_role_fact = ReviewerExecutionFact.model_construct(
        schema_version="v1",
        role="architecture",
        result="success",
        actual_provider="approved-provider",
        actual_model="approved-model",
        actual_tool_grants=plan.inputs[0].tool_allowlist,
        usage_status="measured",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.1,
        started_at=ROLE_START["intent"],
        completed_at=ROLE_COMPLETED["intent"],
        latency_ms=ROLE_LATENCY_MS["intent"],
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(plan, routes, (wrong_role_fact, facts[1], facts[2]), result=result)

    other_subject = _subject(_sha256(b"other-subject"))
    other_risk = _risk_result(other_subject.subject_digest)
    wrong_risk_request = _budget_request(
        plan.inputs[0],
        phase="review",
        agent_role="intent",
        available_candidate_ids=("candidate-standard",),
        allowed_provider_refs=("approved-provider",),
        risk_result=other_risk,
    )
    wrong_risk_decision = ModelRouter.route(
        routes[0].policy, wrong_risk_request
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            (wrong_risk_decision, routes[1], routes[2]),
            facts,
            result=result,
        )

    wrong_provider_fact = _fact(
        "intent",
        plan,
        provider="local-provider",
        model="local-model",
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            (wrong_provider_fact, facts[1], facts[2]),
            result=result,
        )

    wrong_tool_fact = _fact(
        "intent",
        plan,
        grants=("read",),
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            (wrong_tool_fact, facts[1], facts[2]),
            result=result,
        )

    wrong_budget_request = _budget_request(
        plan.inputs[0],
        phase="review",
        agent_role="intent",
        available_candidate_ids=("candidate-standard",),
        allowed_provider_refs=("approved-provider",),
        budget=_budget(500, 0.1),
    )
    wrong_budget_decision = ModelRouter.route(
        routes[0].policy, wrong_budget_request
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            (wrong_budget_decision, routes[1], routes[2]),
            facts,
            result=result,
        )

    wrong_latency_fact = ReviewerExecutionFact.model_construct(
        schema_version="v1",
        role="intent",
        result="success",
        actual_provider="approved-provider",
        actual_model="approved-model",
        actual_tool_grants=facts[0].actual_tool_grants,
        usage_status="measured",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.1,
        started_at=ROLE_START["intent"],
        completed_at=ROLE_COMPLETED["intent"],
        latency_ms=2711,
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            (wrong_latency_fact, facts[1], facts[2]),
            result=result,
        )

    wrong_timestamp_fact = ReviewerExecutionFact.model_construct(
        schema_version="v1",
        role="intent",
        result="success",
        actual_provider="approved-provider",
        actual_model="approved-model",
        actual_tool_grants=facts[0].actual_tool_grants,
        usage_status="measured",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.1,
        started_at=ROLE_COMPLETED["intent"],
        completed_at=ROLE_START["intent"],
        latency_ms=0,
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            (wrong_timestamp_fact, facts[1], facts[2]),
            result=result,
        )


def test_council_result_binding_rejections():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan, failure_code=None) for role in ROLE_ORDER)
    result = _success_result(plan, facts)

    other_plan = _plan_with_subject(_sha256(b"other-plan"))
    with pytest.raises(ValueError):
        _build(
            plan,
            routes,
            facts,
            result=_success_result(other_plan, facts),
        )

    wrong_time_result = _success_result(plan, facts)
    wrong_time_result = CouncilRunResult.model_construct(
        schema_version="v1",
        plan=plan,
        outputs=tuple(
            _finding_output(
                plan.inputs[index],
                outcome="success",
                completed_at=RECORDED_TIME,
            )
            for index in range(3)
        ),
    )
    with pytest.raises(ValueError):
        _build(plan, routes, facts, result=wrong_time_result)

    wrong_outcome_result = CouncilRunResult.model_construct(
        schema_version="v1",
        plan=plan,
        outputs=tuple(
            _finding_output(
                plan.inputs[index],
                outcome="success" if index != 0 else "failure",
                failure=(
                    None
                    if index != 0
                    else _failure_outcome("failure")
                ),
                completed_at=facts[index].completed_at,
            )
            for index in range(3)
        ),
    )
    with pytest.raises(ValueError):
        _build(plan, routes, facts, result=wrong_outcome_result)

    skipped_facts = (
        _fact("intent", plan, result="skipped", failure_code="skipped"),
        facts[1],
        facts[2],
    )
    with pytest.raises(ValueError):
        _build(plan, routes, skipped_facts, result=result)

    success_without_result = (
        _fact("intent", plan, failure_code=None),
        facts[1],
        facts[2],
    )
    with pytest.raises(ValueError):
        _build(plan, routes, success_without_result, result=None)


def test_forged_topology_aggregate_receipt_id_gate_plan_result_output_digest():
    receipt = _all_success_receipt()
    data = receipt.model_dump(mode="python")

    forged_topology = CouncilTopologySnapshot.model_construct(
        **{
            **data["topology"],
            "executed_roles": ("intent",),
            "completion_order": ("intent",),
            "actual_state": "interrupted",
        }
    )
    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate({**data, "topology": forged_topology})

    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "overall_result": "partial"}
        )

    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "receipt_id": "council_receipt_" + "0" * 64}
        )

    forged_gate = ExecutionReceipt.model_construct(
        **{**data["gate_receipt"], "overall_result": "partial"}
    )
    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "gate_receipt": forged_gate}
        )

    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "plan_digest": _sha256(b"forged")}
        )
    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "result_digest": _sha256(b"forged")}
        )

    forged_steps = tuple(
        step.model_copy(
            update={"output_digest": _sha256(b"forged")}
        )
        for step in receipt.steps
    )
    with pytest.raises(ValidationError):
        CouncilExecutionReceipt.model_validate(
            {**data, "steps": forged_steps}
        )


def test_model_copy_and_model_construct_forgeries_rejected_at_builder():
    plan = _plan()
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan, failure_code=None) for role in ROLE_ORDER)
    result = _success_result(plan, facts)

    forged_plan = CouncilPlan.model_construct(
        schema_version="v1",
        inputs=(plan.inputs[1], plan.inputs[2], plan.inputs[0]),
    )
    with pytest.raises(ValueError):
        _build(forged_plan, routes, facts, result=result)

    forged_decision = ModelRouteDecision.model_construct(
        schema_version="v1",
        policy=routes[0].policy,
        request=routes[0].request,
        outcome="blocked",
        block_reason="no_eligible_candidate",
        matched_rule_id=routes[0].matched_rule_id,
        matched_tier_alias=routes[0].matched_tier_alias,
        attempts=(),
        selected_candidate=None,
        allocated_budget=routes[0].allocated_budget,
        decision_id=routes[0].decision_id,
    )
    with pytest.raises(ValueError):
        _build(plan, (forged_decision, routes[1], routes[2]), facts, result=result)

    forged_fact = ReviewerExecutionFact.model_construct(
        schema_version="v1",
        role="intent",
        result="success",
        actual_provider="approved-provider",
        actual_model="approved-model",
        actual_tool_grants=facts[0].actual_tool_grants,
        usage_status="measured",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.1,
        started_at=facts[0].started_at,
        completed_at=facts[0].completed_at,
        latency_ms=facts[0].latency_ms + 1,
        failure_code=None,
    )
    with pytest.raises(ValueError):
        _build(plan, routes, (forged_fact, facts[1], facts[2]), result=result)

    forged_result = CouncilRunResult.model_construct(
        schema_version="v1",
        plan=plan,
        outputs=(),
    )
    with pytest.raises(ValueError):
        _build(plan, routes, facts, result=forged_result)


def test_deterministic_construction_no_global_state():
    first = _all_success_receipt(run_id="run-deterministic")
    second = _all_success_receipt(run_id="run-deterministic")
    assert first == second
    assert first.receipt_id == second.receipt_id
    assert first.plan_digest == second.plan_digest
    assert first.result_digest == second.result_digest
    assert first.gate_receipt == second.gate_receipt
    assert first.model_dump_json() == second.model_dump_json()
    for name, value in vars(execution_receipt_module).items():
        if name.startswith("__") or callable(value) or inspect.ismodule(value):
            continue
        assert not isinstance(value, (list, dict, set)), name


def test_source_audit_no_provider_tool_gate_io_env_time_random_persistence():
    source = inspect.getsource(execution_receipt_module)
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.lstrip(".").split(".")[0])
    assert imports == {
        "hashlib",
        "json",
        "re",
        "typing",
        "pydantic",
        "contracts",
        "model_routing",
        "review_council",
    }
    forbidden_tokens = (
        "os.",
        "environ[",
        "getenv",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "pathlib",
        "PolicyGate",
        "PolicyEvaluationInput",
        "PASS",
        "approval",
        "waiver",
        "adjudicat",
        "random",
        "time.",
        "sleep",
        "open(",
        "datetime.now",
        "model_copy(",
        "model_construct(",
    )
    for token in forbidden_tokens:
        assert token not in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in {
                    "eval",
                    "exec",
                    "compile",
                    "open",
                    "input",
                    "__import__",
                }
            elif isinstance(func, ast.Attribute):
                assert func.attr not in {
                    "read_bytes",
                    "write_bytes",
                    "read_text",
                    "write_text",
                    "unlink",
                    "mkdir",
                    "system",
                    "popen",
                    "spawn",
                    "connect",
                    "urlopen",
                    "request",
                }


def test_public_exports_present_in_assurance_package():
    for name in sorted(NEW_PUBLIC_NAMES):
        assert getattr(execution_receipt_module, name) is not None
