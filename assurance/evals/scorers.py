"""Evaluator-side deterministic scoring for the public result artifact."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .dataset import DATASET_ID, load_hidden_gold, load_public_cases
from .result_artifact import ROLE_ORDER, replay_result_artifact
from .runner import ARM_ORDER, ArmRunResult, ComparisonRun


SCHEMA_VERSION: Final[str] = "change-assurance-score-v0"
HIDDEN_TO_PUBLIC_TAXONOMY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "intent_scope_creep": "intent.scope",
        "intent_missing_acceptance_nfr": "intent.acceptance_nfr",
        "architecture_dependency_reversal": "architecture.dependency_direction",
        "architecture_duplicate_rule_second_source": "architecture.single_source_policy",
        "architecture_public_contract_without_adr": "architecture.contract_decision",
        "operability_migration_without_rollback": "operability.rollback",
        "operability_retry_duplicate_side_effect": "operability.idempotency",
        "operability_missing_telemetry_kill_switch": "operability.telemetry_control",
        "cost_unbounded_retries_fallback": "cost.bounded_fallback",
        "ownership_missing_owner_runbook": "ownership.owner_runbook",
        "boundary_provider_data_residency": "boundary.provider_residency",
        "freshness_old_approval_survives_new_digest": "freshness.digest_binding",
    }
)
RULE_TO_PUBLIC_TAXONOMY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "rule:data_scope": "intent.scope",
        "rule:migration_reversibility": "operability.rollback",
        "rule:operator_control": "operability.telemetry_control",
        "rule:bounded_attempts": "cost.bounded_fallback",
        "rule:ownership_metadata": "ownership.owner_runbook",
        "rule:independent_digest_comparison": "freshness.digest_binding",
    }
)


def normalize_predicted_issue_ids(issue_ids: tuple[str, ...]) -> tuple[str, ...]:
    """将 Rules 的通用码归一化为 evaluator 的公开 taxonomy。"""

    return tuple(RULE_TO_PUBLIC_TAXONOMY.get(issue_id, issue_id) for issue_id in issue_ids)


_COUNCIL_ROLES: Final[tuple[str, ...]] = ("intent", "architecture", "operability")
_EXPECTED_ROLES: Final[dict[str, tuple[str, ...]]] = {
    "rules_only": ("rules",),
    "single_strong_reviewer": ("general",),
    "specialized_council": _COUNCIL_ROLES,
}
_COUNCIL_PRIORITY: Final[tuple[str, ...]] = (
    "STALE",
    "BLOCKED",
    "NEEDS_HUMAN",
    "PASS_WITH_WAIVER",
    "PASS",
)
_ROOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "dataset_id",
        "result_artifact_sha256",
        "arms",
        "score_digest",
    }
)
_ARM_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "arm",
        "predicted_issue_count",
        "matched_issue_count",
        "precision",
        "matched_case_count",
        "case_total",
        "macro_recall",
        "false_block",
        "missed_block",
        "unsupported_finding",
        "evidence_ref_exists",
        "evidence_location_correctness",
        "stale_escape",
        "required_role_execution",
        "council_conflict_retention",
        "outcome_counts",
        "finding_count",
        "question_count",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "latency_seconds",
    }
)
_STRICT_JSON_MAX_BYTES: Final[int] = 8 * 1024 * 1024


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
    raise ValueError("non-finite number")


def _parse_json(raw: object, label: str) -> object:
    if type(raw) is not bytes or len(raw) > _STRICT_JSON_MAX_BYTES:
        raise ValueError(f"invalid {label}")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise ValueError(f"invalid {label}")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except Exception:
        raise ValueError(f"invalid {label}") from None


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"invalid {label}")
    return value


def _require_exact_fields(value: object, fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"invalid {label}")


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _artifact_object(raw: bytes) -> dict[str, object]:
    value = _parse_json(raw, "result artifact")
    if type(value) is not dict:
        raise ValueError("result artifact must be an object")
    return value


def _gold_facts() -> tuple[dict[str, object], dict[str, tuple[str, ...]]]:
    cases = load_public_cases()
    gold = load_hidden_gold()
    public_refs = {case.case_id: case.evidence_refs for case in cases}
    facts: dict[str, object] = {}
    for case in cases:
        gold_case = gold[case.case_id]
        issue_labels = tuple(
            rubric_id.removeprefix("issue:")
            for rubric_id in gold_case.rubric_ids
            if rubric_id.startswith("issue:")
        )
        if len(issue_labels) != 1 or issue_labels[0] not in HIDDEN_TO_PUBLIC_TAXONOMY:
            raise ValueError("gold taxonomy mapping is incomplete")
        facts[case.case_id] = {
            "issue": HIDDEN_TO_PUBLIC_TAXONOMY[issue_labels[0]],
            "outcome": gold_case.expected_outcome,
            "evidence_refs": gold_case.evidence_refs,
        }
    return facts, public_refs


def _raw_role_facts(artifact: dict[str, object]) -> dict[str, dict[str, dict[str, object]]]:
    role_bundles = artifact.get("role_bundles")
    if type(role_bundles) is not dict or set(role_bundles) != set(ROLE_ORDER):
        raise ValueError("role bundles are invalid")
    result: dict[str, dict[str, dict[str, object]]] = {}
    for role in ROLE_ORDER:
        bundle = role_bundles[role]
        if type(bundle) is not dict or set(bundle) != {"role", "responses"}:
            raise ValueError("role bundle is invalid")
        if bundle["role"] != role or type(bundle["responses"]) is not list:
            raise ValueError("role bundle is invalid")
        by_case: dict[str, dict[str, object]] = {}
        for item in bundle["responses"]:
            if type(item) is not dict or set(item) != {"case_id", "raw_response"}:
                raise ValueError("raw role response is invalid")
            case_id = _require_text(item["case_id"], "case_id")
            raw_response = _require_text(item["raw_response"], "raw_response").encode(
                "utf-8"
            )
            parsed = _parse_json(raw_response, "raw role response")
            if type(parsed) is not dict or set(parsed) != {
                "findings",
                "questions",
                "predicted_outcome",
            }:
                raise ValueError("raw role response is invalid")
            by_case[case_id] = parsed
        if len(by_case) != 12:
            raise ValueError("raw role responses are incomplete")
        result[role] = by_case
    return result


def _finding_signature(finding: object, role: str) -> tuple[object, ...]:
    if type(finding) is not dict:
        raise ValueError("raw finding is invalid")
    return (
        finding.get("claim"),
        finding.get("severity"),
        finding.get("blocking"),
        tuple(finding.get("evidence_refs", ())),
        tuple(finding.get("predicted_issue_ids", ())),
        role,
    )


def _conflict_metric(
    run: ComparisonRun,
    raw_roles: dict[str, dict[str, dict[str, object]]],
) -> dict[str, object]:
    council_cases = 0
    retained = 0
    for comparison in run.cases:
        outcomes = tuple(
            raw_roles[role][comparison.case_id]["predicted_outcome"]
            for role in _COUNCIL_ROLES
        )
        if len(set(outcomes)) <= 1:
            continue
        council_cases += 1
        expected_outcome = next(
            outcome for outcome in _COUNCIL_PRIORITY if outcome in outcomes
        )
        council = comparison.arms[ARM_ORDER.index("specialized_council")]
        raw_signatures = tuple(
            _finding_signature(finding, role)
            for role in _COUNCIL_ROLES
            for finding in raw_roles[role][comparison.case_id]["findings"]
        )
        council_signatures = tuple(
            (
                finding.claim,
                finding.severity,
                finding.blocking,
                finding.evidence_refs,
                finding.predicted_issue_ids,
                finding.reviewer_role,
            )
            for finding in council.findings
        )
        raw_questions = {
            question
            for role in _COUNCIL_ROLES
            for question in raw_roles[role][comparison.case_id]["questions"]
        }
        if (
            council.predicted_outcome == expected_outcome
            and Counter(raw_signatures) == Counter(council_signatures)
            and set(council.questions) == raw_questions
        ):
            retained += 1
    return {
        "status": "measured",
        "conflict_cases": council_cases,
        "retained": retained,
        "rate": _rate(retained, council_cases),
    }


def _availability(arms: tuple[ArmRunResult, ...], field: str) -> dict[str, object]:
    values = tuple(getattr(arm, field) for arm in arms)
    if any(value is None for value in values):
        return {"available": False, "total": None}
    return {"available": True, "total": sum(values)}


def _score_arm(
    arm_name: str,
    arms: tuple[ArmRunResult, ...],
    gold_facts: dict[str, object],
    public_refs: dict[str, tuple[str, ...]],
    raw_roles: dict[str, dict[str, dict[str, object]]],
) -> dict[str, object]:
    predicted_issue_count = 0
    matched_issue_count = 0
    matched_case_count = 0
    false_block_numerator = 0
    false_block_denominator = 0
    missed_block_numerator = 0
    missed_block_denominator = 0
    unsupported_count = 0
    evidence_ref_total = 0
    evidence_ref_valid = 0
    blocking_total = 0
    blocking_location_correct = 0
    stale_escape_count = 0
    outcomes: Counter[str] = Counter()
    finding_count = 0
    question_count = 0
    expected_roles = _EXPECTED_ROLES[arm_name]
    role_matches = 0

    for arm in arms:
        case_gold = gold_facts[arm.case_id]
        gold_issue = case_gold["issue"]
        gold_outcome = case_gold["outcome"]
        gold_evidence_refs = set(case_gold["evidence_refs"])
        public_case_refs = set(public_refs[arm.case_id])
        outcomes[arm.predicted_outcome] += 1
        finding_count += len(arm.findings)
        question_count += len(arm.questions)
        if arm.executed_roles == expected_roles:
            role_matches += 1
        if gold_outcome in {"NEEDS_HUMAN", "PASS_WITH_WAIVER", "PASS"}:
            false_block_denominator += 1
            if arm.predicted_outcome == "BLOCKED":
                false_block_numerator += 1
        if gold_outcome == "BLOCKED":
            missed_block_denominator += 1
            if arm.predicted_outcome != "BLOCKED":
                missed_block_numerator += 1
        if gold_outcome == "STALE" and arm.predicted_outcome != "STALE":
            stale_escape_count += 1

        predicted_for_case: list[str] = []
        for finding in arm.findings:
            normalized_issue_ids = normalize_predicted_issue_ids(
                finding.predicted_issue_ids
            )
            predicted_for_case.extend(normalized_issue_ids)
            finding_has_match = gold_issue in normalized_issue_ids
            finding_refs = set(finding.evidence_refs)
            finding_refs_valid = bool(finding_refs) and finding_refs <= public_case_refs
            if not (finding_has_match and finding_refs_valid):
                unsupported_count += 1
            evidence_ref_total += len(finding.evidence_refs)
            evidence_ref_valid += sum(
                ref in public_case_refs for ref in finding.evidence_refs
            )
            if finding.blocking:
                blocking_total += 1
                if gold_evidence_refs <= finding_refs:
                    blocking_location_correct += 1
        predicted_issue_count += len(predicted_for_case)
        matched_issue_count += sum(issue == gold_issue for issue in predicted_for_case)
        if gold_issue in predicted_for_case:
            matched_case_count += 1

    return {
        "arm": arm_name,
        "predicted_issue_count": predicted_issue_count,
        "matched_issue_count": matched_issue_count,
        "precision": _rate(matched_issue_count, predicted_issue_count),
        "matched_case_count": matched_case_count,
        "case_total": len(arms),
        "macro_recall": _rate(matched_case_count, len(arms)),
        "false_block": {
            "numerator": false_block_numerator,
            "eligible_denominator": false_block_denominator,
            "rate": _rate(false_block_numerator, false_block_denominator),
        },
        "missed_block": {
            "numerator": missed_block_numerator,
            "denominator": missed_block_denominator,
            "rate": _rate(missed_block_numerator, missed_block_denominator),
        },
        "unsupported_finding": {
            "count": unsupported_count,
            "rate": _rate(unsupported_count, finding_count),
        },
        "evidence_ref_exists": {
            "total": evidence_ref_total,
            "valid": evidence_ref_valid,
            "rate": _rate(evidence_ref_valid, evidence_ref_total),
        },
        "evidence_location_correctness": {
            "blocking_finding_total": blocking_total,
            "correct": blocking_location_correct,
            "rate": _rate(blocking_location_correct, blocking_total),
        },
        "stale_escape": {
            "count": stale_escape_count,
            "denominator": 1,
            "rate": _rate(stale_escape_count, 1),
        },
        "required_role_execution": {
            "matched": role_matches,
            "total": len(arms),
            "rate": _rate(role_matches, len(arms)),
        },
        "council_conflict_retention": {"status": "not_applicable"},
        "outcome_counts": dict(outcomes),
        "finding_count": finding_count,
        "question_count": question_count,
        "input_tokens": _availability(arms, "input_tokens"),
        "output_tokens": _availability(arms, "output_tokens"),
        "cost_usd": _availability(arms, "cost_usd"),
        "latency_seconds": _availability(arms, "latency_seconds"),
    }


def _calculate_report(result_artifact_raw: bytes) -> dict[str, object]:
    run = replay_result_artifact(result_artifact_raw)
    artifact = _artifact_object(result_artifact_raw)
    canonical_result = _canonical_json(artifact)
    gold_facts, public_refs = _gold_facts()
    raw_roles = _raw_role_facts(artifact)
    arms_by_name = {
        arm_name: tuple(
            comparison.arms[ARM_ORDER.index(arm_name)] for comparison in run.cases
        )
        for arm_name in ARM_ORDER
    }
    arm_reports = [
        _score_arm(
            arm_name,
            arms_by_name[arm_name],
            gold_facts,
            public_refs,
            raw_roles,
        )
        for arm_name in ARM_ORDER
    ]
    council_report = _score_arm(
        "specialized_council",
        arms_by_name["specialized_council"],
        gold_facts,
        public_refs,
        raw_roles,
    )
    council_report["council_conflict_retention"] = _conflict_metric(run, raw_roles)
    arm_reports[-1] = council_report
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "result_artifact_sha256": "sha256:" + hashlib.sha256(canonical_result).hexdigest(),
        "arms": arm_reports,
    }


def build_score_report(result_artifact_raw: bytes) -> bytes:
    """重放公开 artifact 后，在 evaluator 侧读取 gold 并输出 metrics-only report。"""

    try:
        report = _calculate_report(result_artifact_raw)
        digest = "sha256:" + hashlib.sha256(_canonical_json(report)).hexdigest()
        return _canonical_json({**report, "score_digest": digest})
    except Exception:
        raise ValueError("invalid result artifact for scoring") from None


def verify_score_report(score_raw: bytes, result_artifact_raw: bytes) -> None:
    """严格重算 score 并拒绝任何 schema、digest 或 metric 篡改。"""

    try:
        score = _parse_json(score_raw, "score report")
        _require_exact_fields(score, _ROOT_FIELDS, "score report")
        if type(score["arms"]) is not list or tuple(
            item.get("arm") for item in score["arms"] if type(item) is dict
        ) != ARM_ORDER:
            raise ValueError("score arm order is invalid")
        for item in score["arms"]:
            _require_exact_fields(item, _ARM_FIELDS, "score arm")
        if score_raw.strip() != _canonical_json(score):
            raise ValueError("score report is not canonical")
        expected = build_score_report(result_artifact_raw)
        if score_raw.strip() != expected:
            raise ValueError("score report does not match recomputation")
    except Exception:
        raise ValueError("invalid score report") from None


__all__ = [
    "ARM_ORDER",
    "HIDDEN_TO_PUBLIC_TAXONOMY",
    "RULE_TO_PUBLIC_TAXONOMY",
    "SCHEMA_VERSION",
    "build_score_report",
    "normalize_predicted_issue_ids",
    "verify_score_report",
]
