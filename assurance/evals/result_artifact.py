"""Strict canonical ingest and replay for public evaluation result artifacts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Final

from .adapters import ModelArmAdapter
from .dataset import DATASET_ID, load_public_cases, reviewer_payload
from .rules_adapter import run_rules_only
from .runner import (
    ARM_ORDER,
    ArmRunResult,
    CaseComparison,
    ComparisonRun,
    ComparisonRunner,
    EvalFinding,
)


SCHEMA_VERSION: Final[str] = "change-assurance-result-v0"
RUN_LABEL: Final[str] = "codex-desktop-luna-max"
MODEL_REF: Final[str] = "gpt-5.6-luna"
PROVIDER: Final[str] = "openai-codex-desktop"
ROLE_ORDER: Final[tuple[str, ...]] = (
    "general",
    "intent",
    "architecture",
    "operability",
)
CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"ca_v0_{index:03d}" for index in range(1, 13)
)
PUBLIC_ISSUE_TAXONOMY: Final[tuple[str, ...]] = (
    "intent.scope",
    "intent.acceptance_nfr",
    "architecture.dependency_direction",
    "architecture.single_source_policy",
    "architecture.contract_decision",
    "operability.rollback",
    "operability.idempotency",
    "operability.telemetry_control",
    "cost.bounded_fallback",
    "ownership.owner_runbook",
    "boundary.provider_residency",
    "freshness.digest_binding",
)

_MAX_BYTES: Final[int] = 256 * 1024
_MAX_ARTIFACT_BYTES: Final[int] = 4 * _MAX_BYTES + 512 * 1024
_MAX_JSON_DEPTH: Final[int] = 64
_MAX_JSON_NODES: Final[int] = 20_000
_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"STALE", "BLOCKED", "NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"}
)
_SEVERITIES: Final[frozenset[str]] = frozenset(
    {"low", "medium", "high", "critical"}
)
_OUTER_FIELDS: Final[frozenset[str]] = frozenset({"role", "responses"})
_INNER_FIELDS: Final[frozenset[str]] = frozenset(
    {"case_id", "findings", "questions", "predicted_outcome"}
)
_MODEL_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {"findings", "questions", "predicted_outcome"}
)
_FINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {"claim", "severity", "blocking", "evidence_refs", "predicted_issue_ids"}
)
_ARTIFACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "dataset_id",
        "run_label",
        "model_ref",
        "provider",
        "public_issue_taxonomy",
        "role_bundles",
        "comparison_run",
    }
)
_ARTIFACT_ROLE_FIELDS: Final[frozenset[str]] = frozenset({"role", "responses"})
_ARTIFACT_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {"case_id", "raw_response"}
)
_COMPARISON_FIELDS: Final[frozenset[str]] = frozenset({"dataset_id", "cases"})
_CASE_COMPARISON_FIELDS: Final[frozenset[str]] = frozenset(
    {"case_id", "public_payload_digest", "arms"}
)
_ARM_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "case_id",
        "arm",
        "status",
        "findings",
        "questions",
        "predicted_outcome",
        "receipt_ref",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "latency_seconds",
        "error_code",
        "executed_roles",
        "model_refs",
    }
)
_FINDING_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "finding_id",
        "claim",
        "severity",
        "blocking",
        "evidence_refs",
        "predicted_issue_ids",
        "reviewer_role",
    }
)
_ROLE_TAXONOMY: Final[dict[str, frozenset[str]]] = {
    "general": frozenset(PUBLIC_ISSUE_TAXONOMY),
    "intent": frozenset(
        issue for issue in PUBLIC_ISSUE_TAXONOMY if issue.startswith("intent.")
    ),
    "architecture": frozenset(
        issue
        for issue in PUBLIC_ISSUE_TAXONOMY
        if issue.startswith("architecture.") or issue.startswith("boundary.")
    ),
    "operability": frozenset(
        issue
        for issue in PUBLIC_ISSUE_TAXONOMY
        if issue.startswith(("operability.", "cost.", "ownership.", "freshness."))
    ),
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_json_tree(
    value: object, *, depth: int = 0, nodes: list[int] | None = None
) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON limits exceeded")
    if type(value) is str:
        if "\x00" in value:
            raise ValueError("NUL in JSON")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
    elif type(value) is list:
        for item in value:
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise ValueError("invalid JSON key")
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
    elif value is None or type(value) is bool or type(value) is int:
        return
    else:
        raise ValueError("non-JSON value")


def _parse_json(
    raw: object, label: str, *, max_bytes: int | None = _MAX_BYTES
) -> object:
    if type(raw) is not bytes or (max_bytes is not None and len(raw) > max_bytes):
        raise ValueError(f"invalid {label}")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise ValueError(f"invalid {label}")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception as exc:
        raise ValueError(f"invalid {label}") from None
    try:
        _validate_json_tree(value)
    except Exception:
        raise ValueError(f"invalid {label}") from None
    return value


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"invalid {label}")
    return value


def _require_exact_fields(value: object, fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"invalid {label}")


def _require_string_list(
    value: object, label: str, *, unique: bool = True
) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"invalid {label}")
    result = tuple(_require_text(item, label) for item in value)
    if unique and len(set(result)) != len(result):
        raise ValueError(f"duplicate {label}")
    return result


def _validate_finding_input(role: str, finding: object) -> None:
    _require_exact_fields(finding, _FINDING_FIELDS, "finding")
    _require_text(finding["claim"], "claim")
    if finding["severity"] not in _SEVERITIES:
        raise ValueError("invalid severity")
    if type(finding["blocking"]) is not bool:
        raise ValueError("invalid blocking")
    _require_string_list(finding["evidence_refs"], "evidence_refs")
    issue_ids = _require_string_list(
        finding["predicted_issue_ids"], "predicted_issue_ids"
    )
    if not set(issue_ids) <= _ROLE_TAXONOMY[role]:
        raise ValueError("taxonomy is not allowed for role")


def _validate_role_response(
    role: str, response: object
) -> tuple[str, bytes]:
    _require_exact_fields(response, _INNER_FIELDS, "role response")
    case_id = _require_text(response["case_id"], "case_id")
    findings = response["findings"]
    if type(findings) is not list:
        raise ValueError("invalid findings")
    for finding in findings:
        _validate_finding_input(role, finding)
    questions = _require_string_list(response["questions"], "questions")
    if len(questions) > 64:
        raise ValueError("too many questions")
    outcome = response["predicted_outcome"]
    if outcome is not None and outcome not in _OUTCOMES:
        raise ValueError("invalid predicted outcome")
    inner = {
        "findings": response["findings"],
        "questions": response["questions"],
        "predicted_outcome": outcome,
    }
    return case_id, _canonical_json(inner)


def _ingest_role_bundles(
    role_bundles: Mapping[str, bytes],
) -> tuple[dict[str, dict[str, bytes]], dict[str, dict[str, object]]]:
    if not isinstance(role_bundles, Mapping) or set(role_bundles) != set(ROLE_ORDER):
        raise ValueError("role bundles must contain exactly four roles")
    parsed: dict[str, dict[str, bytes]] = {}
    stored: dict[str, dict[str, object]] = {}
    for role in ROLE_ORDER:
        outer = _parse_json(role_bundles[role], "role bundle")
        _require_exact_fields(outer, _OUTER_FIELDS, "role bundle")
        if outer["role"] != role:
            raise ValueError("role bundle role mismatch")
        responses = outer["responses"]
        if type(responses) is not list or len(responses) != len(CASE_IDS):
            raise ValueError("role bundle must contain twelve responses")
        role_cases: dict[str, bytes] = {}
        stored_responses: list[dict[str, str]] = []
        for expected_case_id, response in zip(CASE_IDS, responses):
            case_id, raw_response = _validate_role_response(role, response)
            if case_id != expected_case_id or case_id in role_cases:
                raise ValueError("role response order is invalid")
            role_cases[case_id] = raw_response
            stored_responses.append(
                {
                    "case_id": case_id,
                    "raw_response": raw_response.decode("utf-8"),
                }
            )
        parsed[role] = role_cases
        stored[role] = {"role": role, "responses": stored_responses}
    return parsed, stored


def _make_model_adapter(role_cases: dict[str, dict[str, bytes]]) -> ModelArmAdapter:
    def invoker_for(role: str):
        def invoke(payload: dict[str, object]) -> bytes:
            return role_cases[role][payload["case_id"]]

        return invoke

    return ModelArmAdapter(
        {role: invoker_for(role) for role in ROLE_ORDER},
        model_refs={role: MODEL_REF for role in ROLE_ORDER},
        providers={role: PROVIDER for role in ROLE_ORDER},
    )


def _run_comparison(role_cases: dict[str, dict[str, bytes]]) -> ComparisonRun:
    model_adapter = _make_model_adapter(role_cases)
    runner = ComparisonRunner(
        {
            "rules_only": run_rules_only,
            "single_strong_reviewer": model_adapter.run_single,
            "specialized_council": model_adapter.run_council,
        }
    )
    result = runner.run(load_public_cases())
    if any(
        arm.status != "success"
        for case in result.cases
        for arm in case.arms
    ):
        raise ValueError("comparison run contains a non-success arm")
    return result


def _finding_to_json(finding: EvalFinding) -> dict[str, object]:
    return {
        "finding_id": finding.finding_id,
        "claim": finding.claim,
        "severity": finding.severity,
        "blocking": finding.blocking,
        "evidence_refs": list(finding.evidence_refs),
        "predicted_issue_ids": list(finding.predicted_issue_ids),
        "reviewer_role": finding.reviewer_role,
    }


def _arm_to_json(arm: ArmRunResult) -> dict[str, object]:
    return {
        "case_id": arm.case_id,
        "arm": arm.arm,
        "status": arm.status,
        "findings": [_finding_to_json(item) for item in arm.findings],
        "questions": list(arm.questions),
        "predicted_outcome": arm.predicted_outcome,
        "receipt_ref": arm.receipt_ref,
        "input_tokens": arm.input_tokens,
        "output_tokens": arm.output_tokens,
        "cost_usd": arm.cost_usd,
        "latency_seconds": arm.latency_seconds,
        "error_code": arm.error_code,
        "executed_roles": list(arm.executed_roles),
        "model_refs": list(arm.model_refs),
    }


def _run_to_json(run: ComparisonRun) -> dict[str, object]:
    return {
        "dataset_id": run.dataset_id,
        "cases": [
            {
                "case_id": case.case_id,
                "public_payload_digest": case.public_payload_digest,
                "arms": [_arm_to_json(arm) for arm in case.arms],
            }
            for case in run.cases
        ],
    }


def _finding_from_json(value: object) -> EvalFinding:
    _require_exact_fields(value, _FINDING_JSON_FIELDS, "stored finding")
    return EvalFinding(
        finding_id=_require_text(value["finding_id"], "finding_id"),
        claim=_require_text(value["claim"], "claim"),
        severity=value["severity"],
        blocking=value["blocking"],
        evidence_refs=tuple(_require_string_list(value["evidence_refs"], "evidence_refs")),
        predicted_issue_ids=tuple(
            _require_string_list(value["predicted_issue_ids"], "predicted_issue_ids")
        ),
        reviewer_role=_require_text(value["reviewer_role"], "reviewer_role"),
    )


def _arm_from_json(value: object) -> ArmRunResult:
    _require_exact_fields(value, _ARM_FIELDS, "stored arm")
    findings = value["findings"]
    if type(findings) is not list:
        raise ValueError("stored findings are invalid")
    questions = _require_string_list(value["questions"], "questions")
    executed_roles = _require_string_list(value["executed_roles"], "executed_roles")
    model_refs = _require_string_list(
        value["model_refs"], "model_refs", unique=False
    )
    return ArmRunResult(
        case_id=_require_text(value["case_id"], "case_id"),
        arm=_require_text(value["arm"], "arm"),
        status=_require_text(value["status"], "status"),
        findings=tuple(_finding_from_json(item) for item in findings),
        questions=questions,
        predicted_outcome=value["predicted_outcome"],
        receipt_ref=(
            None
            if value["receipt_ref"] is None
            else _require_text(value["receipt_ref"], "receipt_ref")
        ),
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        cost_usd=value["cost_usd"],
        latency_seconds=value["latency_seconds"],
        error_code=(
            None
            if value["error_code"] is None
            else _require_text(value["error_code"], "error_code")
        ),
        executed_roles=executed_roles,
        model_refs=model_refs,
    )


def _run_from_json(value: object) -> ComparisonRun:
    _require_exact_fields(value, _COMPARISON_FIELDS, "stored comparison run")
    cases = value["cases"]
    if type(cases) is not list or len(cases) != len(CASE_IDS):
        raise ValueError("stored comparison cases are invalid")
    comparisons: list[CaseComparison] = []
    for expected_case_id, case in zip(CASE_IDS, cases):
        _require_exact_fields(case, _CASE_COMPARISON_FIELDS, "stored case comparison")
        if case["case_id"] != expected_case_id:
            raise ValueError("stored case order is invalid")
        arms = case["arms"]
        if type(arms) is not list or len(arms) != len(ARM_ORDER):
            raise ValueError("stored arms are invalid")
        comparisons.append(
            CaseComparison(
                case_id=_require_text(case["case_id"], "case_id"),
                public_payload_digest=_require_text(
                    case["public_payload_digest"], "public_payload_digest"
                ),
                arms=tuple(_arm_from_json(arm) for arm in arms),
            )
        )
    return ComparisonRun(
        dataset_id=_require_text(value["dataset_id"], "dataset_id"),
        cases=tuple(comparisons),
    )


def _role_cases_from_stored(value: object) -> dict[str, dict[str, bytes]]:
    if type(value) is not dict or set(value) != set(ROLE_ORDER):
        raise ValueError("stored role bundles are invalid")
    result: dict[str, dict[str, bytes]] = {}
    for role in ROLE_ORDER:
        bundle = value[role]
        _require_exact_fields(bundle, _ARTIFACT_ROLE_FIELDS, "stored role bundle")
        if bundle["role"] != role:
            raise ValueError("stored role mismatch")
        responses = bundle["responses"]
        if type(responses) is not list or len(responses) != len(CASE_IDS):
            raise ValueError("stored role responses are invalid")
        role_cases: dict[str, bytes] = {}
        for expected_case_id, item in zip(CASE_IDS, responses):
            _require_exact_fields(item, _ARTIFACT_RESPONSE_FIELDS, "stored role response")
            case_id = _require_text(item["case_id"], "case_id")
            if case_id != expected_case_id or case_id in role_cases:
                raise ValueError("stored role response order is invalid")
            raw_response = _require_text(item["raw_response"], "raw_response").encode(
                "utf-8"
            )
            parsed = _parse_json(raw_response, "stored raw response")
            _require_exact_fields(parsed, _MODEL_RESPONSE_FIELDS, "stored raw response")
            stored_response = {
                "case_id": case_id,
                "findings": parsed["findings"],
                "questions": parsed["questions"],
                "predicted_outcome": parsed["predicted_outcome"],
            }
            validated_case_id, canonical_response = _validate_role_response(
                role, stored_response
            )
            if validated_case_id != case_id or canonical_response != raw_response:
                raise ValueError("stored raw response is not canonical")
            role_cases[case_id] = raw_response
        result[role] = role_cases
    return result


def build_result_artifact(role_bundles: Mapping[str, bytes]) -> bytes:
    """严格 ingest 四角色 JSON bytes 并生成确定性的公开 artifact。"""

    try:
        role_cases, stored_roles = _ingest_role_bundles(role_bundles)
        comparison = _run_comparison(role_cases)
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": DATASET_ID,
            "run_label": RUN_LABEL,
            "model_ref": MODEL_REF,
            "provider": PROVIDER,
            "public_issue_taxonomy": list(PUBLIC_ISSUE_TAXONOMY),
            "role_bundles": stored_roles,
            "comparison_run": _run_to_json(comparison),
        }
        return _canonical_json(artifact)
    except Exception:
        raise ValueError("invalid role bundles") from None


def replay_result_artifact(raw: bytes) -> ComparisonRun:
    """仅从 bytes 解析并重新执行公开 role raw response，拒绝任何篡改。"""

    try:
        artifact = _parse_json(
            raw, "result artifact", max_bytes=_MAX_ARTIFACT_BYTES
        )
        _require_exact_fields(artifact, _ARTIFACT_FIELDS, "result artifact")
        if artifact["schema_version"] != SCHEMA_VERSION:
            raise ValueError("schema version mismatch")
        if artifact["dataset_id"] != DATASET_ID:
            raise ValueError("dataset mismatch")
        if artifact["run_label"] != RUN_LABEL:
            raise ValueError("run label mismatch")
        if artifact["model_ref"] != MODEL_REF or artifact["provider"] != PROVIDER:
            raise ValueError("model identity mismatch")
        if type(artifact["public_issue_taxonomy"]) is not list or tuple(
            artifact["public_issue_taxonomy"]
        ) != PUBLIC_ISSUE_TAXONOMY:
            raise ValueError("taxonomy mismatch")
        role_cases = _role_cases_from_stored(artifact["role_bundles"])
        stored = _run_from_json(artifact["comparison_run"])
        replayed = _run_comparison(role_cases)
        if replayed != stored:
            raise ValueError("stored comparison does not match replay")
        return replayed
    except Exception:
        raise ValueError("invalid result artifact") from None


__all__ = [
    "CASE_IDS",
    "MODEL_REF",
    "PROVIDER",
    "PUBLIC_ISSUE_TAXONOMY",
    "ROLE_ORDER",
    "RUN_LABEL",
    "SCHEMA_VERSION",
    "build_result_artifact",
    "replay_result_artifact",
]
