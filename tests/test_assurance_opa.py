"""Focused offline contract tests for the V2-P8-06 OPA adapter."""

import ast
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

from assurance.contracts import PolicyDecision
from assurance.integrations.opa import (
    OPADataAPIIntent,
    OPADataAdapter,
    OPAEvaluationResult,
    OPAEvaluationReceipt,
    OPADecision,
    OPAIntentError,
    canonical_opa_json_bytes,
)


SUBJECT = "sha256:" + "a" * 64
RULES = "sha256:" + "b" * 64


def _local(outcome: str = "PASS") -> PolicyDecision:
    values: dict[str, object] = {
        "decision_id": "policy-001",
        "subject_digest": SUBJECT,
        "policy_version": "v1",
        "rules_digest": RULES,
        "outcome": outcome,
        "evaluated_at": "2026-08-26T08:00:00+08:00",
    }
    if outcome in {"STALE", "BLOCKED", "NEEDS_HUMAN"}:
        values["reason_codes"] = ("LOCAL_POLICY",)
    if outcome == "NEEDS_HUMAN":
        values["required_human_role"] = "release_owner"
    if outcome == "PASS_WITH_WAIVER":
        values["waiver_ref"] = "waiver-001"
    return PolicyDecision(**values)


def _intent(*, required: bool = False, **overrides: object) -> OPADataAPIIntent:
    values: dict[str, object] = {
        "path": "codemesh/assurance/decision",
        "input_document": {"case_id": "case-001", "subject_digest": SUBJECT},
        "required": required,
    }
    values.update(overrides)
    return OPADataAdapter.build_intent(**values)


def _evaluate(
    local_outcome: str = "PASS",
    response: object = None,
    *,
    required: bool = False,
    available: bool = True,
) -> OPAEvaluationResult:
    return OPADataAdapter.evaluate(
        _local(local_outcome),
        _intent(required=required),
        response,
        provider_reachable=available,
    )


def test_contract_models_are_frozen_and_forbid_extra_fields():
    for model in (
        OPADataAPIIntent,
        OPADecision,
        OPAEvaluationReceipt,
        OPAEvaluationResult,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        OPADataAPIIntent.model_validate(
            {
                "path": "codemesh/assurance/decision",
                "input_document": {},
                "extra": True,
            }
        )


def test_intent_is_a_path_only_opa_data_api_post_with_canonical_input():
    intent = _intent(input_document={"z": 1, "a": "中文"})

    assert intent.method == "POST"
    assert intent.endpoint == "/v1/data/codemesh/assurance/decision"
    assert intent.body == {"input": {"z": 1, "a": "中文"}}
    assert intent.body_digest == (
        "sha256:"
        + hashlib.sha256(canonical_opa_json_bytes(intent.body)).hexdigest()
    )
    assert intent.intent_digest != intent.body_digest
    assert intent.model_dump(mode="json")["input_document"] == {
        "z": 1,
        "a": "中文",
    }


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/codemesh/assurance/decision",
        "codemesh//decision",
        "codemesh/../decision",
        "https://opa.example/v1/data/decision",
        "codemesh/assurance?x=1",
        "codemesh/%2e%2e/decision",
        "codemesh/assurance\ndecision",
    ],
)
def test_intent_rejects_non_path_or_unsafe_data_paths(path):
    with pytest.raises(OPAIntentError):
        _intent(path=path)


def test_opa_allow_cannot_clear_a_local_block_or_human_gate():
    for local_outcome in ("BLOCKED", "NEEDS_HUMAN", "STALE"):
        result = _evaluate(local_outcome, {"result": True})
        assert result.decision.status == "ALLOW"
        assert result.receipt.final_outcome == local_outcome
        assert result.receipt.authoritative == "local"


def test_opa_allow_preserves_nonblocking_local_outcome_without_upgrade():
    for local_outcome in ("PASS", "PASS_WITH_WAIVER", "NEEDS_HUMAN"):
        result = _evaluate(local_outcome, {"result": True})
        assert result.receipt.final_outcome == local_outcome
        assert result.receipt.opa_status == "ALLOW"


def test_opa_deny_blocks_a_locally_passing_outcome():
    result = _evaluate("PASS", {"result": False})

    assert result.decision.status == "DENY"
    assert result.receipt.final_outcome == "BLOCKED"
    assert result.receipt.authoritative == "local"


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"result": True}, "ALLOW"),
        ({"result": False}, "DENY"),
        ({"result": {"allow": True}}, "ERROR"),
        ({"result": {"decision": "deny"}}, "ERROR"),
        ({"result": {"allow": True, "decision": "deny"}}, "ERROR"),
        ({}, "UNDEFINED"),
        ({"result": None}, "UNDEFINED"),
        ({"error": "policy compile failed"}, "ERROR"),
    ],
)
def test_decision_normalization_is_explicit_and_fail_closed(response, expected):
    result = _evaluate("PASS", response)
    assert result.decision.status == expected


def test_required_opa_unavailable_undefined_or_error_is_blocked():
    for response, available in ((None, False), ({}, True), ({"error": "x"}, True)):
        result = _evaluate("PASS", response, required=True, available=available)
        assert result.receipt.final_outcome == "BLOCKED"
        assert result.receipt.opa_status in {"UNAVAILABLE", "UNDEFINED", "ERROR"}
        assert result.receipt.decision_available is False


def test_optional_opa_unavailable_undefined_or_error_preserves_local_outcome():
    for response, available in ((None, False), ({}, True), ({"error": "x"}, True)):
        result = _evaluate("PASS_WITH_WAIVER", response, required=False, available=available)
        assert result.receipt.final_outcome == "PASS_WITH_WAIVER"
        assert result.receipt.decision_available is False


def test_result_receipt_is_bound_to_intent_and_decision_canonical_digests():
    result = _evaluate("PASS", {"result": True})
    assert result.receipt.intent_digest == result.intent.intent_digest
    assert result.receipt.decision_digest == (
        "sha256:"
        + hashlib.sha256(
            canonical_opa_json_bytes(result.decision.model_dump(mode="json"))
        ).hexdigest()
    )
    assert result.receipt.local_decision_digest == (
        "sha256:"
        + hashlib.sha256(
            canonical_opa_json_bytes(result.local_decision.model_dump(mode="json"))
        ).hexdigest()
    )

    forged = result.receipt.model_copy(update={"final_outcome": "ACCEPTED"})
    with pytest.raises(ValidationError):
        OPAEvaluationResult(
            intent=result.intent,
            local_decision=result.local_decision,
            decision=result.decision,
            receipt=forged,
        )


def test_receipt_binds_endpoint_status_local_decision_and_required_mode():
    result = _evaluate("PASS", {"result": True}, required=True)

    with pytest.raises(ValidationError):
        OPAEvaluationResult(
            intent=result.intent.model_copy(update={"path": "attacker/allow"}),
            local_decision=result.local_decision,
            decision=result.decision,
            receipt=result.receipt,
        )
    with pytest.raises(ValidationError):
        OPAEvaluationResult(
            intent=result.intent,
            local_decision=result.local_decision,
            decision=result.decision,
            receipt=result.receipt.model_copy(update={"opa_status": "DENY"}),
        )
    with pytest.raises(ValidationError):
        OPAEvaluationResult(
            intent=result.intent,
            local_decision=result.local_decision,
            decision=result.decision,
            receipt=result.receipt.model_copy(
                update={"required": False, "final_outcome": "PASS"}
            ),
        )


def test_arbitrary_local_outcome_is_not_an_authoritative_policy_decision():
    with pytest.raises(OPAIntentError):
        OPADataAdapter.evaluate(
            "GODMODE",
            _intent(),
            {"result": True},
        )


def test_provider_reachability_and_decision_availability_are_not_conflated():
    undefined = _evaluate("PASS", {}, available=True)
    assert undefined.receipt.provider_reachable is True
    assert undefined.receipt.decision_available is False

    unreachable = _evaluate("PASS", None, available=False)
    assert unreachable.receipt.provider_reachable is False
    assert unreachable.receipt.decision_available is False


def test_intent_input_is_immutable_after_digest_binding():
    intent = _intent()
    with pytest.raises(TypeError):
        intent.input_document["case_id"] = "case-forged"


def test_non_json_input_and_extra_receipt_fields_fail_closed():
    with pytest.raises(OPAIntentError):
        _intent(input_document={"not_json": float("nan")})

    result = _evaluate("PASS", {"result": True})
    with pytest.raises(ValidationError):
        OPAEvaluationReceipt.model_validate(
            {**result.receipt.model_dump(), "token": "secret"}
        )


def test_no_network_client_imports_or_http_side_effects():
    import assurance.integrations.opa as opa

    tree = ast.parse(inspect.getsource(opa))
    forbidden = {"httpx", "requests", "urllib", "socket", "aiohttp"}
    imported = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint(forbidden)
