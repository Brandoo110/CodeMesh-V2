"""V2-P4-08 Adjudicator overlay focused TDD tests.

The module under test is a pure, deterministic adjudication overlay: it
prepares a bounded prompt and normalizes caller-supplied exact response
bytes. It never calls a model/provider, executes tools, runs the Council,
evaluates the Gate, persists data, or performs filesystem/network/
subprocess/env/time/random access. ``duplicate_candidate`` and
``conflict_candidate`` are candidate overlays, never confirmed facts, and a
successful overlay is never a PASS/approval/waiver/acceptance signal.
"""

import ast
import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import assurance
import assurance.adjudicator as adjudicator_module
from assurance import (
    AdjudicationCluster,
    AdjudicationHumanQuestion,
    AdjudicationResult,
    Adjudicator,
    AdjudicatorInput,
    AdjudicatorNormalizationInput,
    AdjudicatorPrompt,
    AdjudicationTrigger,
    CouncilExecutionReceipt,
    CouncilEvidenceIndexEntry,
    CouncilPlan,
    CouncilRunResult,
    ModelRouteDecision,
    ModelRouteMatch,
    ModelRouter,
    ModelRoutingPolicy,
    ModelTierAlias,
)
from assurance.contracts import Finding
from assurance.single_reviewer import ReviewQuestion
from tests.test_assurance_execution_receipt import (
    RECORDED_TIME,
    ROLE_COMPLETED,
    _build,
    _fact,
    _route,
)
from tests.test_assurance_model_routing import (
    _alias as _routing_alias,
    _budget as _routing_budget,
    _candidate as _routing_candidate,
    _match as _routing_match,
    _request as _routing_request,
    _risk_result_high,
    _rule as _routing_rule,
    _target as _routing_target,
)
from tests.test_assurance_review_council import (
    ROLE_ORDER,
    _assert_immutable_graph,
    _default_contexts,
    _default_inputs,
    _failure,
    _finding,
    _finding_output,
    _plan,
    _question,
    _risk_result,
    _subject,
)


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
REQUESTED_AT = datetime(2026, 8, 25, 8, 0, 7, tzinfo=timezone.utc)
STARTED_AT = REQUESTED_AT
COMPLETED_AT = datetime(2026, 8, 25, 8, 0, 7, 250000, tzinfo=timezone.utc)
LATENCY_MS = 250

NEW_PUBLIC_NAMES = frozenset(
    {
        "AdjudicationTrigger",
        "AdjudicatorInput",
        "AdjudicatorPrompt",
        "AdjudicatorNormalizationInput",
        "AdjudicationCluster",
        "AdjudicationHumanQuestion",
        "AdjudicationResult",
        "Adjudicator",
    }
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _finding_for(input_, finding_id="fnd-a", *, evidence_refs=None, **overrides):
    if evidence_refs is None:
        evidence_refs = (input_.evidence_allowlist[0],)
    values = {
        "finding_id": finding_id,
        "evidence_refs": evidence_refs,
        "severity": "medium",
        "status": "open",
    }
    values.update(overrides)
    return _finding(input_, **values)


def _question_for(input_, *, evidence_refs=None, **overrides):
    if evidence_refs is None:
        evidence_refs = (input_.evidence_allowlist[0],)
    return _question(input_, evidence_refs=evidence_refs, **overrides)


def _council_result(plan, findings_by_role=None, questions_by_role=None):
    findings_by_role = findings_by_role or {}
    questions_by_role = questions_by_role or {}
    outputs = []
    for input_ in plan.inputs:
        findings = tuple(
            sorted(
                findings_by_role.get(input_.reviewer_role, ()),
                key=lambda item: item.finding_id,
            )
        )
        questions = tuple(
            sorted(
                questions_by_role.get(input_.reviewer_role, ()),
                key=lambda item: item.question_id,
            )
        )
        outputs.append(
            _finding_output(
                input_,
                outcome="success",
                findings=findings,
                questions=questions,
                completed_at=ROLE_COMPLETED[input_.reviewer_role],
            )
        )
    outputs = tuple(outputs)
    buckets = {}
    for output in outputs:
        role = output.input.reviewer_role
        for finding in output.findings:
            for evidence_id in finding.evidence_refs:
                bucket = buckets.setdefault(
                    evidence_id,
                    {
                        "finding_ids": set(),
                        "question_ids": set(),
                        "roles": set(),
                    },
                )
                bucket["finding_ids"].add(finding.finding_id)
                bucket["roles"].add(role)
        for question in output.questions:
            for evidence_id in question.evidence_refs:
                bucket = buckets.setdefault(
                    evidence_id,
                    {
                        "finding_ids": set(),
                        "question_ids": set(),
                        "roles": set(),
                    },
                )
                bucket["question_ids"].add(question.question_id)
                bucket["roles"].add(role)
    evidence_index = tuple(
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id=evidence_id,
            finding_ids=tuple(sorted(bucket["finding_ids"])),
            question_ids=tuple(sorted(bucket["question_ids"])),
            roles=tuple(
                sorted(bucket["roles"], key=ROLE_ORDER.index)
            ),
        )
        for evidence_id, bucket in sorted(buckets.items())
    )
    return CouncilRunResult(
        schema_version="v1",
        plan=plan,
        outputs=outputs,
        evidence_index=evidence_index,
    )


def _adjudication_policy():
    alias = _routing_alias(
        "standard",
        (
            _routing_candidate(
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
            _routing_rule(
                "rule-adjudication-standard",
                _routing_match(phase="adjudication"),
                _routing_target(
                    "standard",
                    token_budget_cap=20_000,
                    cost_budget_cap_usd=4.0,
                ),
            ),
        ),
        aliases=(alias,),
    )


def _adjudication_route(
    plan,
    *,
    policy=None,
    risk_result=None,
    phase="adjudication",
    task_role="adjudicator",
):
    if policy is None:
        policy = _adjudication_policy()
    if risk_result is None:
        risk_result = plan.inputs[0].risk_result
    request = _routing_request(
        risk_result,
        phase=phase,
        agent_role=None,
        task_role=task_role,
        priority="normal",
        provider_boundary="any",
        available_candidate_ids=("candidate-standard",),
        allowed_provider_refs=("approved-provider",),
        budget=_routing_budget(20_000, 4.0),
    )
    return ModelRouter.route(policy, request)


def _trigger(kind, finding_ids=(), question_ids=()):
    return AdjudicationTrigger(
        kind=kind,
        finding_ids=tuple(sorted(finding_ids)),
        question_ids=tuple(sorted(question_ids)),
    )


def _input(plan, receipt, route, trigger, requested_at=REQUESTED_AT):
    return AdjudicatorInput(
        schema_version="v1",
        council_result=receipt.result,
        council_receipt=receipt,
        route=route,
        trigger=trigger,
        requested_at=requested_at,
    )


def _success_setup(
    kind="duplicate_candidate",
    findings_by_role=None,
    questions_by_role=None,
    trigger_finding_ids=None,
    trigger_question_ids=(),
):
    plan = _plan()
    if findings_by_role is None:
        findings_by_role = {
            "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
            "architecture": (_finding_for(plan.inputs[1], "fnd-b"),),
        }
    result = _council_result(
        plan, findings_by_role, questions_by_role or {}
    )
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan) for role in ROLE_ORDER)
    receipt = _build(plan, routes, facts, result=result)
    route = _adjudication_route(plan)
    if trigger_finding_ids is None:
        trigger_finding_ids = tuple(
            sorted(
                item.finding_id
                for items in (findings_by_role or {}).values()
                for item in items
            )
        )
    trigger = _trigger(
        kind, trigger_finding_ids, trigger_question_ids
    )
    return plan, result, receipt, route, trigger


def _raw_bytes(clusters=None, questions=None):
    if clusters is None:
        clusters = []
    if questions is None:
        questions = []
    return json.dumps(
        {"clusters": clusters, "human_questions": questions},
        separators=(",", ":"),
    ).encode("utf-8")


def _normalization_input(
    prompt,
    raw=None,
    *,
    usage_status="measured",
    input_tokens=120,
    output_tokens=45,
    cost_usd=0.0045,
    started_at=STARTED_AT,
    completed_at=COMPLETED_AT,
    latency_ms=LATENCY_MS,
):
    if raw is None:
        raw = _raw_bytes()
    return AdjudicatorNormalizationInput(
        schema_version="v1",
        prompt=prompt,
        raw_response_bytes=raw,
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=latency_ms,
    )


# ── Scenario 1: strict public contracts ──


def test_strict_contracts_field_order_immutability_and_json_round_trip():
    plan, result, receipt, route, trigger = _success_setup()
    adjudicator_input = _input(plan, receipt, route, trigger)

    assert tuple(type(trigger).model_fields)[:3] == (
        "schema_version",
        "kind",
        "finding_ids",
    )
    assert trigger.schema_version == "v1"
    assert trigger.trigger_id.startswith("adj_trigger_")
    assert re.fullmatch(r"adj_trigger_[0-9a-f]{32}", trigger.trigger_id)
    assert (
        AdjudicationTrigger.model_validate_json(trigger.model_dump_json())
        == trigger
    )

    assert tuple(type(adjudicator_input).model_fields)[:6] == (
        "schema_version",
        "council_result",
        "council_receipt",
        "route",
        "trigger",
        "requested_at",
    )
    assert type(adjudicator_input.council_result) is CouncilRunResult
    assert type(adjudicator_input.council_receipt) is CouncilExecutionReceipt
    assert type(adjudicator_input.route) is ModelRouteDecision
    assert type(adjudicator_input.trigger) is AdjudicationTrigger
    assert type(adjudicator_input.requested_at) is datetime
    assert adjudicator_input.requested_at.tzinfo is not None
    rebuilt_input = AdjudicatorInput.model_validate_json(
        adjudicator_input.model_dump_json()
    )
    assert rebuilt_input == adjudicator_input

    prompt = Adjudicator.prepare(adjudicator_input)
    assert tuple(type(prompt).model_fields) == (
        "schema_version",
        "input",
        "prompt_text",
        "prompt_digest",
        "prompt_id",
    )
    assert type(prompt.input) is AdjudicatorInput
    assert prompt.prompt_digest.startswith("sha256:")
    assert prompt.prompt_id.startswith("adj_prompt_")
    assert AdjudicatorPrompt.model_validate_json(
        prompt.model_dump_json()
    ) == prompt

    normalization_input = _normalization_input(prompt)
    assert tuple(type(normalization_input).model_fields) == (
        "schema_version",
        "prompt",
        "raw_response_bytes",
        "usage_status",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "started_at",
        "completed_at",
        "latency_ms",
    )
    assert type(normalization_input.prompt) is AdjudicatorPrompt
    assert type(normalization_input.raw_response_bytes) is bytes
    assert (
        AdjudicatorNormalizationInput.model_validate_json(
            normalization_input.model_dump_json()
        )
        == normalization_input
    )

    cluster_payload = [
        {
            "kind": "duplicate_candidate",
            "finding_ids": ["fnd-a", "fnd-b"],
            "rationale": "same acceptance direction claimed twice",
        }
    ]
    result_model = Adjudicator.normalize(
        _normalization_input(prompt, _raw_bytes(cluster_payload, []))
    )
    assert result_model.outcome == "success"
    assert len(result_model.clusters) == 1
    cluster = result_model.clusters[0]
    assert type(cluster) is AdjudicationCluster
    assert type(cluster.findings) is tuple
    assert all(type(item) is Finding for item in cluster.findings)
    assert type(cluster.roles) is tuple
    assert type(cluster.evidence_refs) is tuple
    assert cluster.cluster_id.startswith("adj_cluster_")
    assert AdjudicationCluster.model_validate_json(
        cluster.model_dump_json()
    ) == cluster
    assert type(result_model) is AdjudicationResult
    assert type(result_model.input) is AdjudicatorNormalizationInput
    assert type(result_model.clusters) is tuple
    assert type(result_model.human_questions) is tuple
    assert type(result_model.preserved_finding_ids) is tuple
    assert type(result_model.preserved_question_ids) is tuple
    assert type(result_model.dissent_finding_ids) is tuple
    assert type(result_model.evidence_id_universe) is tuple
    assert result_model.result_id.startswith("adj_result_")
    assert AdjudicationResult.model_validate_json(
        result_model.model_dump_json()
    ) == result_model

    for model in (
        trigger,
        adjudicator_input,
        prompt,
        normalization_input,
        cluster,
        result_model,
    ):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"
        _assert_immutable_graph(model)

    with pytest.raises(ValidationError):
        trigger.kind = "manual_review"
    with pytest.raises(ValidationError):
        adjudicator_input.requested_at = FIXED_TIME
    with pytest.raises(ValidationError):
        result_model.clusters = ()

    with pytest.raises(ValidationError):
        AdjudicationTrigger.model_validate(
            {
                "schema_version": "v1",
                "kind": "duplicate_candidate",
                "finding_ids": ["fnd-b", "fnd-a"],
                "question_ids": [],
            }
        )
    with pytest.raises(ValidationError):
        AdjudicationTrigger.model_validate(
            {
                "schema_version": "v1",
                "kind": "duplicate_candidate",
                "finding_ids": ("fnd-a", "fnd-b"),
                "question_ids": [],
                "evidence_ids": ("ev-a",),
            }
        )


def test_numeric_or_bool_datetimes_rejected():
    plan, result, receipt, route, trigger = _success_setup()
    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            route,
            trigger,
            requested_at=1_752_998_400,
        )
    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            route,
            trigger,
            requested_at="1752998400",
        )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    with pytest.raises(ValidationError):
        _normalization_input(
            prompt, started_at=1_752_998_407, completed_at=COMPLETED_AT
        )
    with pytest.raises(ValidationError):
        _normalization_input(
            prompt, started_at=STARTED_AT, completed_at=True
        )


# ── Scenario 2: trigger kinds and invalid source/rule combinations ──


def _input_with_trigger(trigger, findings_by_role=None, questions_by_role=None):
    plan, _, receipt, route, _ = _success_setup(
        findings_by_role=findings_by_role,
        questions_by_role=questions_by_role,
    )
    return _input(plan, receipt, route, trigger)


def test_each_trigger_kind_binds_to_valid_council_sources():
    plan = _plan()
    intent = plan.inputs[0]
    architecture = plan.inputs[1]
    operability = plan.inputs[2]
    findings_by_role = {
        "intent": (_finding_for(intent, "fnd-a"),),
        "architecture": (_finding_for(architecture, "fnd-b"),),
        "operability": (_finding_for(operability, "fnd-c"),),
    }
    questions_by_role = {
        "operability": (
            _question_for(operability, question="needs human sign-off?"),
        )
    }
    result = _council_result(plan, findings_by_role, questions_by_role)
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan) for role in ROLE_ORDER)
    receipt = _build(plan, routes, facts, result=result)
    route = _adjudication_route(plan)

    cases = (
        ("duplicate_candidate", ("fnd-a", "fnd-b"), ()),
        ("conflict_candidate", ("fnd-a", "fnd-c"), ()),
        ("high_severity_low_evidence", ("fnd-a",), ()),
        ("consolidate_questions", ("fnd-a",), ("q1",)),
        ("manual_review", ("fnd-a",), ()),
        ("manual_review", (), ("q1",)),
    )
    for kind, finding_ids, question_ids in cases:
        if kind == "high_severity_low_evidence":
            high = _finding_for(
                intent,
                "fnd-a",
                severity="high",
                evidence_refs=(intent.evidence_allowlist[0],),
            )
            result = _council_result(
                plan,
                {"intent": (high,), **{k: v for k, v in findings_by_role.items() if k != "intent"}},
                questions_by_role,
            )
            receipt = _build(plan, routes, facts, result=result)
            question_id = questions_by_role["operability"][0].question_id
            trigger = _trigger(
                kind, ("fnd-a",), (question_id,) if question_ids else ()
            )
            _input(plan, receipt, route, trigger)
            continue
        question_id = questions_by_role["operability"][0].question_id
        trigger = _trigger(
            kind,
            finding_ids,
            tuple(question_id for _ in question_ids),
        )
        _input(plan, receipt, route, trigger)
        assert re.fullmatch(r"adj_trigger_[0-9a-f]{32}", trigger.trigger_id)


def test_trigger_kind_rules_and_unknown_ids_fail_closed():
    plan = _plan()
    intent = plan.inputs[0]
    architecture = plan.inputs[1]
    findings_by_role = {
        "intent": (
            _finding_for(intent, "fnd-a"),
            _finding_for(intent, "fnd-b"),
        ),
    }
    result = _council_result(plan, findings_by_role)
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)
    facts = tuple(_fact(role, plan) for role in ROLE_ORDER)
    receipt = _build(plan, routes, facts, result=result)
    route = _adjudication_route(plan)

    def input_for(kind, finding_ids=(), question_ids=()):
        return _input(
            plan,
            receipt,
            route,
            _trigger(kind, finding_ids, question_ids),
        )

    with pytest.raises(ValidationError):
        input_for("duplicate_candidate", ("fnd-a",))
    with pytest.raises(ValidationError):
        input_for("conflict_candidate", ("fnd-a", "fnd-b"))
    with pytest.raises(ValidationError):
        input_for("high_severity_low_evidence", ("fnd-a", "fnd-b"))
    with pytest.raises(ValidationError):
        input_for("high_severity_low_evidence", ("fnd-a",))
    with pytest.raises(ValidationError):
        input_for("consolidate_questions", ())
    with pytest.raises(ValidationError):
        input_for("manual_review")
    with pytest.raises(ValidationError):
        input_for("duplicate_candidate", ("fnd-a", "fnd-unknown"))
    with pytest.raises(ValidationError):
        input_for("duplicate_candidate", ("fnd-a", "fnd-b"), ("rq-unknown",))


# ── Scenario 3: input binds receipt, result, route, subject, risk, time ──


def test_input_requires_exact_receipt_result_and_same_plan_subject_risk():
    plan, result, receipt, route, trigger = _success_setup()
    valid = _input(plan, receipt, route, trigger)
    assert valid.council_result == result

    with pytest.raises(ValidationError):
        AdjudicatorInput(
            council_result=result,
            council_receipt=receipt.model_copy(
                update={"result": None}
            ),
            route=route,
            trigger=trigger,
            requested_at=REQUESTED_AT,
        )

    other_subject = _subject(
        "sha256:" + "e" * 64,
        change_id="change-other",
    )
    other_plan = _plan(
        _default_inputs(
            **{
                role: {"subject": other_subject}
                for role in ROLE_ORDER
            }
        )
    )
    other_result = _council_result(other_plan)
    other_receipt = _build(
        other_plan,
        tuple(_route(other_plan, role) for role in ROLE_ORDER),
        tuple(_fact(role, other_plan) for role in ROLE_ORDER),
        result=other_result,
    )
    with pytest.raises(ValidationError):
        AdjudicatorInput(
            council_result=result,
            council_receipt=other_receipt,
            route=route,
            trigger=trigger,
            requested_at=REQUESTED_AT,
        )
    with pytest.raises(ValidationError):
        AdjudicatorInput(
            council_result=other_result,
            council_receipt=receipt,
            route=route,
            trigger=trigger,
            requested_at=REQUESTED_AT,
        )

    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            route,
            trigger,
            requested_at=RECORDED_TIME - timedelta(milliseconds=1),
        )


def test_input_requires_route_phase_task_agent_and_risk_binding():
    plan, result, receipt, _, trigger = _success_setup()

    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            _adjudication_route(plan, phase="review"),
            trigger,
        )
    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            _adjudication_route(plan, task_role="synthesizer"),
            trigger,
        )

    high_risk = _risk_result_high()
    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            _adjudication_route(plan, risk_result=high_risk),
            trigger,
        )

    disabled_policy = _adjudication_policy().model_copy(
        update={"enabled": False}
    )
    with pytest.raises(ValidationError):
        _input(
            plan,
            receipt,
            _adjudication_route(plan, policy=disabled_policy),
            trigger,
        )

    blocked = ModelRouter.route(
        _adjudication_policy(),
        _routing_request(
            plan.inputs[0].risk_result,
            phase="adjudication",
            agent_role=None,
            task_role="adjudicator",
            priority="normal",
            provider_boundary="local-only",
            available_candidate_ids=("candidate-standard",),
            allowed_provider_refs=("local-provider",),
            budget=_routing_budget(20_000, 4.0),
        ),
    )
    assert blocked.outcome == "blocked"
    with pytest.raises(ValidationError):
        _input(plan, receipt, blocked, trigger)


def test_input_rejects_missing_failed_or_incomplete_receipts():
    plan = _plan()
    findings_by_role = {
        "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
        "architecture": (_finding_for(plan.inputs[1], "fnd-b"),),
    }
    result = _council_result(plan, findings_by_role)
    routes = tuple(_route(plan, role) for role in ROLE_ORDER)

    missing_result = _build(
        plan,
        routes,
        tuple(_fact(role, plan) for role in ROLE_ORDER),
        result=result,
    ).model_copy(update={"result": None})
    failed_facts = tuple(
        _fact(role, plan, result="failure", failure_code="execution_failed")
        for role in ROLE_ORDER
    )
    failed_outputs = []
    for index, input_ in enumerate(plan.inputs):
        failed_outputs.append(
            _finding_output(
                input_,
                outcome="failure",
                findings=(),
                questions=(),
                failure=_failure("execution_failed"),
                completed_at=failed_facts[index].completed_at,
            )
        )
    failed_result = CouncilRunResult(
        schema_version="v1",
        plan=plan,
        outputs=tuple(failed_outputs),
    )
    failed_receipt = _build(
        plan, routes, failed_facts, result=failed_result
    )
    route = _adjudication_route(plan)

    with pytest.raises(ValidationError):
        _input(plan, missing_result, route, _trigger("manual_review", ("fnd-a",)))
    with pytest.raises(ValidationError):
        _input(
            plan,
            failed_receipt,
            route,
            _trigger("duplicate_candidate", ("fnd-a", "fnd-b")),
        )


# ── Scenario 5: deterministic prompt ──


def test_deterministic_prompt_content_digest_and_prohibitions():
    plan, result, receipt, route, trigger = _success_setup()
    adjudicator_input = _input(plan, receipt, route, trigger)
    first = Adjudicator.prepare(adjudicator_input)
    second = Adjudicator.prepare(adjudicator_input)

    assert first == second
    assert first.prompt_digest == second.prompt_digest
    assert first.prompt_id == second.prompt_id
    assert first.prompt_text == second.prompt_text
    assert first.prompt_digest == _sha256(
        json.dumps(
            {"prompt_text": first.prompt_text},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )

    text = first.prompt_text
    for item in result.outputs:
        for finding in item.findings:
            assert finding.finding_id in text
            assert finding.claim in text
        for question in item.questions:
            assert question.question_id in text
            assert question.question in text
    assert trigger.kind in text
    assert trigger.trigger_id in text
    for entry in result.evidence_index:
        assert entry.evidence_id in text
    assert plan.inputs[0].subject.subject_digest in text
    assert plan.inputs[0].risk_result.classification.risk_level in text
    assert route.selected_candidate.candidate_id in text
    assert route.selected_candidate.provider_ref in text
    assert route.selected_candidate.model_ref in text
    assert route.decision_id in text
    assert "zero-tool" in text
    assert "do not execute tools" in text.lower()
    assert '"kind"' in text
    assert '"finding_ids"' in text
    assert '"rationale"' in text
    assert '"human_questions"' in text
    assert '"source_finding_ids"' in text
    assert '"source_question_ids"' in text
    for forbidden in (
        "must not create new IDs",
        "must not supply Evidence",
        "severity",
        "status",
        "winner",
        "resolution",
        "PASS",
        "approval",
        "must not delete",
    ):
        assert forbidden in text


# ── Scenario 6: duplicate candidate overlay ──


def test_duplicate_candidate_overlay_roles_evidence_union_and_preservation():
    plan = _plan()
    findings_by_role = {
        "intent": (
            _finding_for(
                plan.inputs[0],
                "fnd-a",
                evidence_refs=(plan.inputs[0].evidence_allowlist[0],),
            ),
        ),
        "architecture": (
            _finding_for(
                plan.inputs[1],
                "fnd-b",
                evidence_refs=(plan.inputs[1].evidence_allowlist[1],),
            ),
        ),
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="duplicate_candidate",
        findings_by_role=findings_by_role,
        trigger_finding_ids=("fnd-a", "fnd-b"),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    raw = _raw_bytes(
        [
            {
                "kind": "duplicate_candidate",
                "finding_ids": ["fnd-b", "fnd-a"],
                "rationale": "both findings describe the same boundary gap",
            }
        ],
        [],
    )
    result = Adjudicator.normalize(_normalization_input(prompt, raw))

    assert result.outcome == "success"
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.kind == "duplicate_candidate"
    assert cluster.finding_ids == ("fnd-a", "fnd-b")
    assert cluster.roles == ("intent", "architecture")
    assert cluster.evidence_refs == ("ev-a", "ev-c")
    assert cluster.rationale == "both findings describe the same boundary gap"
    assert cluster.cluster_id == AdjudicationCluster.model_validate(
        {
            "kind": cluster.kind,
            "finding_ids": cluster.finding_ids,
            "findings": cluster.findings,
            "rationale": cluster.rationale,
        }
    ).cluster_id
    assert result.preserved_finding_ids == ("fnd-a", "fnd-b")
    assert result.preserved_question_ids == ()
    assert result.dissent_finding_ids == ()
    assert result.evidence_id_universe == ("ev-a", "ev-c")
    assert result.selected_candidate_id == "candidate-standard"
    assert result.selected_provider_ref == "approved-provider"
    assert result.selected_model_ref == "approved-model"
    assert result.route_decision_id == route.decision_id
    assert result.raw_response_digest == _sha256(raw)
    assert result.usage_status == "measured"
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.cost_usd == 0.0045
    assert result.started_at == STARTED_AT
    assert result.completed_at == COMPLETED_AT
    assert result.latency_ms == LATENCY_MS


# ── Scenario 7: conflict candidate overlay with dissent ──


def test_conflict_candidate_overlay_dissent_preserved_no_winner():
    plan = _plan()
    findings_by_role = {
        "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
        "operability": (_finding_for(plan.inputs[2], "fnd-c"),),
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="conflict_candidate",
        findings_by_role=findings_by_role,
        trigger_finding_ids=("fnd-a", "fnd-c"),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    raw = _raw_bytes(
        [
            {
                "kind": "conflict_candidate",
                "finding_ids": ["fnd-c", "fnd-a"],
                "rationale": "reviewers disagree on the required boundary",
            }
        ],
        [],
    )
    result = Adjudicator.normalize(_normalization_input(prompt, raw))
    cluster = result.clusters[0]
    assert cluster.kind == "conflict_candidate"
    assert cluster.roles == ("intent", "operability")
    assert result.dissent_finding_ids == ("fnd-a", "fnd-c")
    assert result.preserved_finding_ids == ("fnd-a", "fnd-c")
    assert "winner" not in type(cluster).model_fields
    assert "resolution" not in type(cluster).model_fields
    assert "vote" not in type(cluster).model_fields
    assert "score" not in type(cluster).model_fields
    assert "approval" not in type(cluster).model_fields
    assert "pass" not in type(cluster).model_fields


# ── Scenario 8: human questions with exact derived evidence union ──


def test_human_questions_from_findings_questions_or_both():
    plan = _plan()
    intent = plan.inputs[0]
    operability = plan.inputs[2]
    finding = _finding_for(
        intent,
        "fnd-a",
        evidence_refs=(intent.evidence_allowlist[0],),
    )
    question = _question_for(
        operability,
        evidence_refs=(operability.evidence_allowlist[2],),
        question="is the offline path covered by acceptance?",
    )
    findings_by_role = {"intent": (finding,)}
    questions_by_role = {"operability": (question,)}
    _, _, receipt, route, trigger = _success_setup(
        kind="manual_review",
        findings_by_role=findings_by_role,
        questions_by_role=questions_by_role,
        trigger_finding_ids=("fnd-a",),
        trigger_question_ids=(question.question_id,),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))

    from_findings = _raw_bytes(
        [],
        [
            {
                "question": "should the two boundary claims be merged?",
                "reason": "conflict",
                "source_finding_ids": ["fnd-a"],
                "source_question_ids": [],
            }
        ],
    )
    result = Adjudicator.normalize(_normalization_input(prompt, from_findings))
    item = result.human_questions[0]
    assert item.source_finding_ids == ("fnd-a",)
    assert item.source_question_ids == ()
    assert item.evidence_refs == ("ev-a",)
    assert item.question_id.startswith("adj_question_")

    from_questions = _raw_bytes(
        [],
        [
            {
                "question": "merge with the operability question?",
                "reason": "consolidation",
                "source_finding_ids": [],
                "source_question_ids": [question.question_id],
            }
        ],
    )
    result = Adjudicator.normalize(
        _normalization_input(prompt, from_questions)
    )
    item = result.human_questions[0]
    assert item.source_question_ids == (question.question_id,)
    assert item.evidence_refs == ("ev-c",)

    both = _raw_bytes(
        [],
        [
            {
                "question": "do finding and question share the same gap?",
                "reason": "insufficient_evidence",
                "source_finding_ids": ["fnd-a"],
                "source_question_ids": [question.question_id],
            }
        ],
    )
    result = Adjudicator.normalize(_normalization_input(prompt, both))
    item = result.human_questions[0]
    assert item.source_finding_ids == ("fnd-a",)
    assert item.source_question_ids == (question.question_id,)
    assert item.evidence_refs == ("ev-a", "ev-c")
    assert result.dissent_finding_ids == ()


# ── Scenario 9: fail-closed model output violations ──


@pytest.mark.parametrize(
    ("cluster_payload", "expected_code"),
    [
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-unknown"],
                    "rationale": "x",
                }
            ],
            "id_not_in_scope",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a"],
                    "rationale": "x",
                }
            ],
            "insufficient_members",
        ),
        (
            [
                {
                    "kind": "conflict_candidate",
                    "finding_ids": ["fnd-a", "fnd-d"],
                    "rationale": "x",
                }
            ],
            "same_role_conflict",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "cluster_id": "adj_cluster_forged",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "evidence_refs": ["ev-forged"],
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "severity": "high",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "status": "open",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "resolution": "merge",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "winner": "intent",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "approval": True,
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                    "pass": True,
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "kind": "confirmed_duplicate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                }
            ],
            "cluster_item_invalid",
        ),
        (
            [
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-a", "fnd-b"],
                    "rationale": "x",
                },
                {
                    "kind": "duplicate_candidate",
                    "finding_ids": ["fnd-b", "fnd-c"],
                    "rationale": "y",
                },
            ],
            "duplicate_cluster_membership",
        ),
    ],
)
def test_invalid_cluster_payloads_fail_closed(cluster_payload, expected_code):
    plan = _plan()
    findings_by_role = {
        "intent": (
            _finding_for(plan.inputs[0], "fnd-a"),
            _finding_for(plan.inputs[0], "fnd-d"),
        ),
        "architecture": (_finding_for(plan.inputs[1], "fnd-b"),),
        "operability": (_finding_for(plan.inputs[2], "fnd-c"),),
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="conflict_candidate",
        findings_by_role=findings_by_role,
        trigger_finding_ids=("fnd-a", "fnd-b", "fnd-c", "fnd-d"),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    result = Adjudicator.normalize(
        _normalization_input(
            prompt, _raw_bytes(cluster_payload, [])
        )
    )
    assert result.outcome == "schema_invalid"
    assert result.failure_code == expected_code
    assert result.clusters == ()
    assert result.human_questions == ()
    assert result.preserved_finding_ids == ("fnd-a", "fnd-b", "fnd-c", "fnd-d")
    assert result.dissent_finding_ids == ()


@pytest.mark.parametrize(
    ("question_payload", "expected_code"),
    [
        (
            [
                {
                    "question": "merge?",
                    "reason": "conflict",
                    "source_finding_ids": ["fnd-unknown"],
                    "source_question_ids": [],
                }
            ],
            "id_not_in_scope",
        ),
        (
            [
                {
                    "question": "merge?",
                    "reason": "conflict",
                    "source_finding_ids": [],
                    "source_question_ids": [],
                }
            ],
            "insufficient_sources",
        ),
        (
            [
                {
                    "question": "merge?",
                    "reason": "conflict",
                    "source_finding_ids": ["fnd-a"],
                    "source_question_ids": [],
                    "question_id": "adj_question_forged",
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "question": "merge?",
                    "reason": "conflict",
                    "source_finding_ids": ["fnd-a"],
                    "source_question_ids": [],
                    "evidence_refs": ["ev-forged"],
                }
            ],
            "forbidden_field",
        ),
        (
            [
                {
                    "question": "merge?",
                    "reason": "consensus",
                    "source_finding_ids": ["fnd-a"],
                    "source_question_ids": [],
                }
            ],
            "question_item_invalid",
        ),
    ],
)
def test_invalid_human_question_payloads_fail_closed(
    question_payload, expected_code
):
    plan = _plan()
    findings_by_role = {
        "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="manual_review",
        findings_by_role=findings_by_role,
        trigger_finding_ids=("fnd-a",),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    result = Adjudicator.normalize(
        _normalization_input(
            prompt, _raw_bytes([], question_payload)
        )
    )
    assert result.outcome == "schema_invalid"
    assert result.failure_code == expected_code
    assert result.clusters == ()
    assert result.human_questions == ()


# ── Scenario 10: malformed payloads ──


def test_malformed_payloads_return_schema_invalid():
    plan, _, receipt, route, trigger = _success_setup(
        kind="duplicate_candidate",
        trigger_finding_ids=("fnd-a", "fnd-b"),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))

    cases = {
        b"\xff\xfe\x00": "invalid_utf8",
        b"{not json": "malformed_json",
        b"prefix " + _raw_bytes(): "malformed_json",
        _raw_bytes() + b" suffix": "malformed_json",
        b"```json\n" + _raw_bytes() + b"\n```": "malformed_json",
        b'{"clusters":[],"human_questions":[],"clusters":[]}': (
            "duplicate_object_key"
        ),
        b'{"clusters":[{"kind":"duplicate_candidate",'
        b'"finding_ids":["fnd-a","fnd-b"],"rationale":NaN}],'
        b'"human_questions":[]}': "malformed_json",
        b'{"clusters":[{"kind":"duplicate_candidate",'
        b'"finding_ids":["fnd-a","fnd-b"],"rationale":Infinity}],'
        b'"human_questions":[]}': "malformed_json",
        b"[]": "root_not_object",
        b'{"clusters":[]}': "top_level_fields_invalid",
        b'{"clusters":[],"human_questions":[],"extra":1}': (
            "top_level_fields_invalid"
        ),
        b'{"clusters":{},"human_questions":[]}': "cluster_item_invalid",
    }
    for raw, expected_code in cases.items():
        result = Adjudicator.normalize(
            _normalization_input(prompt, raw)
        )
        assert result.outcome == "schema_invalid"
        assert result.failure_code == expected_code, raw
        assert result.clusters == ()
        assert result.human_questions == ()

    deep = '{"clusters":[{"kind":"duplicate_candidate",' '"finding_ids":' + "[" * 40 + '"fnd-a"' + "]" * 40 + ',"rationale":"x"}],"human_questions":[]}'
    result = Adjudicator.normalize(
        _normalization_input(prompt, deep.encode("utf-8"))
    )
    assert result.outcome == "schema_invalid"
    assert result.failure_code == "depth_exceeded"

    too_many = [
        {
            "kind": "duplicate_candidate",
            "finding_ids": ["fnd-a", "fnd-b"],
            "rationale": f"r{i}",
        }
        for i in range(33)
    ]
    result = Adjudicator.normalize(
        _normalization_input(prompt, _raw_bytes(too_many, []))
    )
    assert result.outcome == "schema_invalid"
    assert result.failure_code == "item_limit_exceeded"

    long_rationale = [
        {
            "kind": "duplicate_candidate",
            "finding_ids": ["fnd-a", "fnd-b"],
            "rationale": "x" * 5000,
        }
    ]
    result = Adjudicator.normalize(
        _normalization_input(prompt, _raw_bytes(long_rationale, []))
    )
    assert result.outcome == "schema_invalid"
    assert result.failure_code == "text_limit_exceeded"

    empty = _raw_bytes()
    result = Adjudicator.normalize(_normalization_input(prompt, empty))
    assert result.outcome == "success"
    assert result.clusters == ()
    assert result.human_questions == ()
    assert result.preserved_finding_ids == ("fnd-a", "fnd-b")


# ── Scenario 11: usage and timing rules ──


def test_usage_and_timing_rules_are_strict():
    plan, _, receipt, route, trigger = _success_setup()
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))

    unavailable = _normalization_input(
        prompt,
        usage_status="unavailable",
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
    )
    assert unavailable.usage_status == "unavailable"

    with pytest.raises(ValidationError):
        _normalization_input(
            prompt,
            usage_status="measured",
            input_tokens=None,
            output_tokens=45,
            cost_usd=0.0045,
        )
    with pytest.raises(ValidationError):
        _normalization_input(
            prompt,
            usage_status="unavailable",
            input_tokens=120,
            output_tokens=None,
            cost_usd=None,
        )
    with pytest.raises(ValidationError):
        _normalization_input(prompt, cost_usd=1)
    with pytest.raises(ValidationError):
        _normalization_input(prompt, input_tokens=True)
    with pytest.raises(ValidationError):
        _normalization_input(prompt, latency_ms=251)
    with pytest.raises(ValidationError):
        _normalization_input(
            prompt,
            started_at=COMPLETED_AT,
            completed_at=STARTED_AT,
            latency_ms=250,
        )
    with pytest.raises(ValidationError):
        _normalization_input(prompt, raw=b"")


# ── Scenario 12: schema-invalid preservation ──


def test_schema_invalid_preserves_originals_and_is_not_pass():
    plan = _plan()
    findings_by_role = {
        "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
        "architecture": (_finding_for(plan.inputs[1], "fnd-b"),),
    }
    questions_by_role = {
        "operability": (
            _question_for(plan.inputs[2], question="who owns follow-up?"),
        )
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="manual_review",
        findings_by_role=findings_by_role,
        questions_by_role=questions_by_role,
        trigger_finding_ids=("fnd-a", "fnd-b"),
        trigger_question_ids=(questions_by_role["operability"][0].question_id,),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    raw = b"{broken"
    result = Adjudicator.normalize(_normalization_input(prompt, raw))

    assert result.outcome == "schema_invalid"
    assert result.failure_code == "malformed_json"
    assert result.failure_details
    assert result.clusters == ()
    assert result.human_questions == ()
    assert result.preserved_finding_ids == ("fnd-a", "fnd-b")
    assert result.preserved_question_ids == (
        questions_by_role["operability"][0].question_id,
    )
    assert result.dissent_finding_ids == ()
    assert result.evidence_id_universe == ("ev-a", "ev-c")
    assert result.selected_candidate_id == route.selected_candidate.candidate_id
    assert result.route_decision_id == route.decision_id
    assert result.raw_response_digest == _sha256(raw)
    assert result.usage_status == "measured"
    assert result.latency_ms == LATENCY_MS
    for forbidden in (
        "pass",
        "approval",
        "waiver",
        "acceptance",
        "winner",
        "resolution",
        "vote",
        "score",
    ):
        assert forbidden not in type(result).model_fields


# ── Scenario 13: derived fields and forgeries ──


def test_derived_fields_and_upstream_forgeries_rejected():
    plan, result, receipt, route, trigger = _success_setup()

    forged_trigger = AdjudicationTrigger.model_construct(
        schema_version="v1",
        kind="duplicate_candidate",
        finding_ids=("fnd-a", "fnd-b"),
        question_ids=(),
        trigger_id="adj_trigger_" + "0" * 32,
    )
    with pytest.raises(ValidationError):
        _input(plan, receipt, route, forged_trigger)

    forged_route = ModelRouteDecision.model_construct(
        **{
            **route.model_dump(mode="python"),
            "decision_id": "sha256:" + "0" * 64,
        }
    )
    with pytest.raises(ValidationError):
        _input(plan, receipt, forged_route, trigger)

    forged_receipt = CouncilExecutionReceipt.model_construct(
        **{
            **receipt.model_dump(mode="python"),
            "result": None,
        }
    )
    with pytest.raises(ValidationError):
        AdjudicatorInput(
            council_result=result,
            council_receipt=forged_receipt,
            route=route,
            trigger=trigger,
            requested_at=REQUESTED_AT,
        )

    forged_input = AdjudicatorInput.model_construct(
        schema_version="v1",
        council_result=result,
        council_receipt=receipt,
        route=route,
        trigger=trigger,
        requested_at=FIXED_TIME,
    )
    with pytest.raises((TypeError, ValueError, ValidationError)):
        Adjudicator.prepare(forged_input)

    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    forged_prompt = AdjudicatorPrompt.model_construct(
        schema_version="v1",
        input=prompt.input,
        prompt_text=prompt.prompt_text,
        prompt_digest="sha256:" + "0" * 64,
        prompt_id=prompt.prompt_id,
    )
    with pytest.raises((TypeError, ValueError, ValidationError)):
        Adjudicator.normalize(_normalization_input(forged_prompt))

    normalization_input = _normalization_input(prompt)
    forged_normalization = AdjudicatorNormalizationInput.model_construct(
        schema_version="v1",
        prompt=prompt,
        raw_response_bytes=normalization_input.raw_response_bytes,
        usage_status="measured",
        input_tokens=120,
        output_tokens=45,
        cost_usd=0.0045,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
        latency_ms=999,
    )
    with pytest.raises((TypeError, ValueError, ValidationError)):
        Adjudicator.normalize(forged_normalization)


def test_mutated_result_json_rejected_by_recomputation():
    plan = _plan()
    findings_by_role = {
        "intent": (_finding_for(plan.inputs[0], "fnd-a"),),
        "architecture": (_finding_for(plan.inputs[1], "fnd-b"),),
    }
    _, _, receipt, route, trigger = _success_setup(
        kind="duplicate_candidate",
        findings_by_role=findings_by_role,
        trigger_finding_ids=("fnd-a", "fnd-b"),
    )
    prompt = Adjudicator.prepare(_input(plan, receipt, route, trigger))
    raw = _raw_bytes(
        [
            {
                "kind": "duplicate_candidate",
                "finding_ids": ["fnd-a", "fnd-b"],
                "rationale": "same gap",
            }
        ],
        [],
    )
    result = Adjudicator.normalize(_normalization_input(prompt, raw))
    data = json.loads(result.model_dump_json())

    mutated = json.loads(json.dumps(data))
    mutated["dissent_finding_ids"] = ["fnd-a"]
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))

    mutated = json.loads(json.dumps(data))
    mutated["clusters"][0]["evidence_refs"] = ["ev-a"]
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))

    mutated = json.loads(json.dumps(data))
    mutated["clusters"][0]["finding_ids"] = ["fnd-a"]
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))

    mutated = json.loads(json.dumps(data))
    mutated["preserved_finding_ids"] = ["fnd-a"]
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))

    mutated = json.loads(json.dumps(data))
    mutated["usage_status"] = "unavailable"
    mutated["input_tokens"] = None
    mutated["output_tokens"] = None
    mutated["cost_usd"] = None
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))

    mutated = json.loads(json.dumps(data))
    mutated["route_decision_id"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError):
        AdjudicationResult.model_validate_json(json.dumps(mutated))


# ── Scenario 14: determinism and no global state ──


def test_deterministic_construction_no_mutation_or_global_state():
    plan, _, receipt, route, trigger = _success_setup(
        kind="duplicate_candidate",
        trigger_finding_ids=("fnd-a", "fnd-b"),
    )
    adjudicator_input = _input(plan, receipt, route, trigger)
    first_prompt = Adjudicator.prepare(adjudicator_input)
    second_prompt = Adjudicator.prepare(adjudicator_input)
    assert first_prompt == second_prompt
    assert first_prompt is not second_prompt

    raw = _raw_bytes(
        [
            {
                "kind": "duplicate_candidate",
                "finding_ids": ["fnd-a", "fnd-b"],
                "rationale": "same gap",
            }
        ],
        [],
    )
    first_result = Adjudicator.normalize(
        _normalization_input(first_prompt, raw)
    )
    second_result = Adjudicator.normalize(
        _normalization_input(second_prompt, raw)
    )
    assert first_result == second_result
    assert first_result is not second_result

    assert hasattr(adjudicator_module, "Adjudicator")
    for name, value in vars(adjudicator_module).items():
        if name.startswith("_") and not name.startswith("__"):
            assert not isinstance(
                value, (list, dict, set)
            ), f"module-level mutable state: {name}"


# ── Scenario 15: source AST/import audit ──


def test_source_audit_no_forbidden_imports_io_or_authority():
    source = inspect.getsource(adjudicator_module)
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
        "single_reviewer",
        "review_council",
        "model_routing",
        "execution_receipt",
    }

    forbidden_source_tokens = (
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
        "random",
        "time.",
        "sleep",
        "open(",
        "datetime.now",
        "uuid",
        "sqlite",
        "model_copy(",
        "model_construct(",
        "asyncio",
    )
    for token in forbidden_source_tokens:
        assert token not in source, token

    forbidden_identifiers = {
        "accept",
        "acceptance",
        "approval",
        "approve",
        "pass",
        "waiver",
        "winner",
        "resolution",
        "vote",
        "score",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_identifiers
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_identifiers
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            assert name not in {
                "eval",
                "exec",
                "compile",
                "open",
                "input",
                "__import__",
            }

    for model in (
        AdjudicationTrigger,
        AdjudicatorInput,
        AdjudicatorPrompt,
        AdjudicatorNormalizationInput,
        AdjudicationCluster,
        AdjudicationHumanQuestion,
        AdjudicationResult,
    ):
        for field_name in model.model_fields:
            lowered = field_name.lower()
            assert not any(
                token in lowered for token in forbidden_identifiers
            )


# ── Scenario 16: package exports ──


def test_package_exports_public_names():
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.Adjudicator is Adjudicator
    assert Path(adjudicator_module.__file__).name == "adjudicator.py"
