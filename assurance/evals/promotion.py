"""Fail-closed promotion gate for the committed public eval artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .scorers import verify_score_report


DATASET_ID: Final[str] = "change_assurance_v0"
PROMOTION_SCHEMA_VERSION: Final[str] = "change-assurance-promotion-v0"
NOT_PROMOTED: Final[str] = "NOT_PROMOTED"
PROMOTED: Final[str] = "PROMOTED"
THRESHOLDS: Final[Mapping[str, float | int]] = MappingProxyType(
    {
        "macro_recall_gain_min": 0.10,
        "unsupported_rate_reduction_min": 0.25,
        "human_review_time_reduction_min": 0.25,
        "false_block_delta_max": 0.03,
        "stale_escape_count_max": 0,
        "blocking_evidence_rate_min": 1.0,
        "council_cost_multiplier_max": 2.5,
    }
)
_STRICT_JSON_MAX_BYTES: Final[int] = 8 * 1024 * 1024
_ROOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "dataset_id",
        "result_artifact_sha256",
        "score_report_digest",
        "thresholds",
        "checks",
        "decision",
        "default_topology",
        "council_policy",
        "council_constraints",
        "decision_digest",
    }
)
_CHECK_NAMES: Final[tuple[str, ...]] = (
    "benefit",
    "false_block",
    "stale_escape",
    "blocking_evidence",
    "cost",
    "role_execution",
    "conflict_retention",
)
_CHECK_FIELDS: Final[frozenset[str]] = frozenset(
    {"status", "observed", "required", "reason_code"}
)
_CHECK_STATUSES: Final[frozenset[str]] = frozenset(
    {"PASS", "FAIL", "UNAVAILABLE_FAIL_CLOSED"}
)


def derive_promotion_state(check_statuses: Mapping[str, object]) -> dict[str, object]:
    """Derive the re-evaluable topology from the exact gate check statuses."""

    if not isinstance(check_statuses, Mapping) or set(check_statuses) != set(_CHECK_NAMES):
        raise ValueError("promotion check statuses are incomplete")
    if any(
        type(check_statuses[name]) is not str
        or check_statuses[name] not in _CHECK_STATUSES
        for name in _CHECK_NAMES
    ):
        raise ValueError("promotion check status is invalid")
    promoted = all(check_statuses[name] == "PASS" for name in _CHECK_NAMES)
    if promoted:
        return {
            "decision": PROMOTED,
            "default_topology": "specialized_council+policy_gate",
            "council_policy": "default_after_promotion",
            "council_constraints": {
                "default_enabled": True,
                "allowed_when": ["default"],
                "requires": ["policy_gate"],
                "can_override_stale_or_evidence_gate": False,
                "re_evaluation_required": False,
            },
        }
    return {
        "decision": NOT_PROMOTED,
        "default_topology": "single_strong_reviewer+policy_gate",
        "council_policy": "experimental_high_risk_or_conflict_only",
        "council_constraints": {
            "default_enabled": False,
            "allowed_when": ["high_risk", "cross_role_conflict"],
            "requires": ["policy_gate", "human_review"],
            "can_override_stale_or_evidence_gate": False,
            "re_evaluation_required": True,
        },
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


def _require_exact_fields(value: object, fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"invalid {label}")


def _finite_number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"invalid {label}")
    return float(value)


def _score_and_result(score_raw: bytes, result_raw: bytes) -> tuple[dict[str, object], dict[str, object]]:
    verify_score_report(score_raw, result_raw)
    score = _parse_json(score_raw, "score report")
    result = _parse_json(result_raw, "result artifact")
    if type(score) is not dict or type(result) is not dict:
        raise ValueError("artifacts must be objects")
    if score.get("dataset_id") != DATASET_ID or result.get("dataset_id") != DATASET_ID:
        raise ValueError("dataset mismatch")
    if type(score.get("arms")) is not list:
        raise ValueError("score arms are invalid")
    return score, result


def _arms_by_name(score: dict[str, object]) -> dict[str, dict[str, object]]:
    arms = score["arms"]
    result: dict[str, dict[str, object]] = {}
    for item in arms:
        if type(item) is not dict or type(item.get("arm")) is not str:
            raise ValueError("score arm is invalid")
        result[item["arm"]] = item
    if set(result) != {"rules_only", "single_strong_reviewer", "specialized_council"}:
        raise ValueError("score arm set is invalid")
    return result


def _metric(arm: dict[str, object], name: str, label: str) -> float:
    value = arm.get(name)
    return _finite_number(value, label)


def _nested_metric(arm: dict[str, object], group: str, name: str, label: str) -> float:
    value = arm.get(group)
    if type(value) is not dict:
        raise ValueError(f"invalid {label}")
    return _finite_number(value.get(name), label)


def _check(
    status: str,
    observed: object,
    required: object,
    reason_code: str,
) -> dict[str, object]:
    return {
        "status": status,
        "observed": observed,
        "required": required,
        "reason_code": reason_code,
    }


def _calculate_decision(score_raw: bytes, result_raw: bytes) -> dict[str, object]:
    score, result = _score_and_result(score_raw, result_raw)
    arms = _arms_by_name(score)
    single = arms["single_strong_reviewer"]
    council = arms["specialized_council"]

    single_recall = _metric(single, "macro_recall", "single macro recall")
    council_recall = _metric(council, "macro_recall", "council macro recall")
    recall_gain = council_recall - single_recall
    single_unsupported = _nested_metric(
        single, "unsupported_finding", "rate", "single unsupported rate"
    )
    council_unsupported = _nested_metric(
        council, "unsupported_finding", "rate", "council unsupported rate"
    )
    unsupported_reduction = (
        (single_unsupported - council_unsupported) / single_unsupported
        if single_unsupported > 0
        else None
    )
    human_time_reduction = None
    benefit_pass = (
        recall_gain >= float(THRESHOLDS["macro_recall_gain_min"])
        or (
            unsupported_reduction is not None
            and unsupported_reduction
            >= float(THRESHOLDS["unsupported_rate_reduction_min"])
        )
        or (
            human_time_reduction is not None
            and human_time_reduction
            >= float(THRESHOLDS["human_review_time_reduction_min"])
        )
    )

    single_false_block = _nested_metric(
        single, "false_block", "rate", "single false-block rate"
    )
    council_false_block = _nested_metric(
        council, "false_block", "rate", "council false-block rate"
    )
    false_block_delta = council_false_block - single_false_block
    stale_escape_count = _nested_metric(
        council, "stale_escape", "count", "council stale escape count"
    )
    blocking_evidence_rate = _nested_metric(
        council,
        "evidence_location_correctness",
        "rate",
        "council evidence location rate",
    )

    single_cost = single.get("cost_usd")
    council_cost = council.get("cost_usd")
    if type(single_cost) is not dict or type(council_cost) is not dict:
        raise ValueError("cost metrics are invalid")
    cost_available = (
        single_cost.get("available") is True
        and council_cost.get("available") is True
        and single_cost.get("total") is not None
        and council_cost.get("total") is not None
    )
    cost_ratio = None
    if cost_available:
        single_total = _finite_number(single_cost["total"], "single cost")
        council_total = _finite_number(council_cost["total"], "council cost")
        if single_total <= 0:
            cost_ratio = 0.0 if council_total <= 0 else None
        else:
            cost_ratio = council_total / single_total

    single_role_rate = _nested_metric(
        single, "required_role_execution", "rate", "single role execution rate"
    )
    council_role_rate = _nested_metric(
        council, "required_role_execution", "rate", "council role execution rate"
    )
    conflict = council.get("council_conflict_retention")
    if type(conflict) is not dict or conflict.get("status") != "measured":
        raise ValueError("council conflict metric is invalid")
    conflict_rate = _finite_number(conflict.get("rate"), "council conflict rate")

    checks = {
        "benefit": _check(
            "PASS" if benefit_pass else "FAIL",
            {
                "macro_recall_gain": recall_gain,
                "single_unsupported_rate": single_unsupported,
                "council_unsupported_rate": council_unsupported,
                "unsupported_rate_reduction": unsupported_reduction,
                "human_review_time_reduction": human_time_reduction,
            },
            {
                "macro_recall_gain_min": THRESHOLDS["macro_recall_gain_min"],
                "unsupported_rate_reduction_min": THRESHOLDS["unsupported_rate_reduction_min"],
                "human_review_time_reduction_min": THRESHOLDS["human_review_time_reduction_min"],
            },
            "QUALIFYING_BENEFIT_MET" if benefit_pass else "NO_QUALIFYING_BENEFIT",
        ),
        "false_block": _check(
            "PASS"
            if false_block_delta <= float(THRESHOLDS["false_block_delta_max"])
            else "FAIL",
            false_block_delta,
            {"rate_delta_max": THRESHOLDS["false_block_delta_max"]},
            "FALSE_BLOCK_DELTA_WITHIN_LIMIT"
            if false_block_delta <= float(THRESHOLDS["false_block_delta_max"])
            else "FALSE_BLOCK_DELTA_EXCEEDS_LIMIT",
        ),
        "stale_escape": _check(
            "PASS"
            if stale_escape_count == float(THRESHOLDS["stale_escape_count_max"])
            else "FAIL",
            int(stale_escape_count),
            {"escape_count_max": THRESHOLDS["stale_escape_count_max"]},
            "STALE_ESCAPE_ZERO"
            if stale_escape_count == float(THRESHOLDS["stale_escape_count_max"])
            else "STALE_ESCAPE_PRESENT",
        ),
        "blocking_evidence": _check(
            "PASS"
            if blocking_evidence_rate
            >= float(THRESHOLDS["blocking_evidence_rate_min"])
            else "FAIL",
            blocking_evidence_rate,
            {"rate_min": THRESHOLDS["blocking_evidence_rate_min"]},
            "COUNCIL_BLOCKING_EVIDENCE_COMPLETE"
            if blocking_evidence_rate >= float(THRESHOLDS["blocking_evidence_rate_min"])
            else "COUNCIL_BLOCKING_EVIDENCE_INCOMPLETE",
        ),
        "cost": _check(
            "UNAVAILABLE_FAIL_CLOSED"
            if not cost_available
            else (
                "PASS"
                if cost_ratio is not None
                and cost_ratio <= float(THRESHOLDS["council_cost_multiplier_max"])
                else "FAIL"
            ),
            {
                "single_cost_usd": None
                if not cost_available
                else _finite_number(single_cost["total"], "single cost"),
                "council_cost_usd": None
                if not cost_available
                else _finite_number(council_cost["total"], "council cost"),
                "cost_ratio": cost_ratio,
            },
            {
                "ratio_max": THRESHOLDS["council_cost_multiplier_max"],
                "high_risk_or_conflict_only": True,
            },
            "COST_METRICS_UNAVAILABLE"
            if not cost_available
            else (
                "COUNCIL_COST_WITHIN_LIMIT"
                if cost_ratio is not None
                and cost_ratio <= float(THRESHOLDS["council_cost_multiplier_max"])
                else "COUNCIL_COST_EXCEEDS_LIMIT"
            ),
        ),
        "role_execution": _check(
            "PASS"
            if single_role_rate >= 1.0 and council_role_rate >= 1.0
            else "FAIL",
            {"single": single_role_rate, "council": council_role_rate},
            {"single_rate_min": 1.0, "council_rate_min": 1.0},
            "REQUIRED_ROLE_EXECUTION_COMPLETE"
            if single_role_rate >= 1.0 and council_role_rate >= 1.0
            else "REQUIRED_ROLE_EXECUTION_INCOMPLETE",
        ),
        "conflict_retention": _check(
            "PASS" if conflict_rate >= 1.0 else "FAIL",
            conflict_rate,
            {"retention_rate_min": 1.0},
            "COUNCIL_CONFLICT_RETENTION_COMPLETE"
            if conflict_rate >= 1.0
            else "COUNCIL_CONFLICT_RETENTION_INCOMPLETE",
        ),
    }
    state = derive_promotion_state(
        {name: checks[name]["status"] for name in _CHECK_NAMES}
    )
    result_canonical = _canonical_json(result)
    report = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "result_artifact_sha256": "sha256:" + hashlib.sha256(result_canonical).hexdigest(),
        "score_report_digest": score["score_digest"],
        "thresholds": dict(THRESHOLDS),
        "checks": checks,
        "decision": state["decision"],
        "default_topology": state["default_topology"],
        "council_policy": state["council_policy"],
        "council_constraints": state["council_constraints"],
    }
    report["decision_digest"] = "sha256:" + hashlib.sha256(_canonical_json(report)).hexdigest()
    return report


def build_promotion_decision(score_raw: bytes, result_raw: bytes) -> bytes:
    """严格验证既有 score/result，并生成 metrics-only 晋级决定。"""

    try:
        return _canonical_json(_calculate_decision(score_raw, result_raw))
    except Exception:
        raise ValueError("invalid score/result artifacts for promotion") from None


def verify_promotion_decision(
    decision_raw: bytes, score_raw: bytes, result_raw: bytes
) -> None:
    """重算并精确比较 promotion artifact，拒绝所有结构或内容篡改。"""

    try:
        decision = _parse_json(decision_raw, "promotion decision")
        _require_exact_fields(decision, _ROOT_FIELDS, "promotion decision")
        if decision_raw.strip() != _canonical_json(decision):
            raise ValueError("promotion decision is not canonical")
        if decision["schema_version"] != PROMOTION_SCHEMA_VERSION:
            raise ValueError("promotion schema is invalid")
        if type(decision["thresholds"]) is not dict or decision["thresholds"] != dict(THRESHOLDS):
            raise ValueError("promotion thresholds are invalid")
        checks = decision["checks"]
        if type(checks) is not dict or set(checks) != set(_CHECK_NAMES):
            raise ValueError("promotion checks are invalid")
        for name in _CHECK_NAMES:
            _require_exact_fields(checks[name], _CHECK_FIELDS, f"promotion check {name}")
        constraints = decision["council_constraints"]
        if type(constraints) is not dict or set(constraints) != {
            "default_enabled",
            "allowed_when",
            "requires",
            "can_override_stale_or_evidence_gate",
            "re_evaluation_required",
        }:
            raise ValueError("promotion council constraints are invalid")
        expected = build_promotion_decision(score_raw, result_raw)
        if decision_raw.strip() != expected:
            raise ValueError("promotion decision does not match recomputation")
    except Exception:
        raise ValueError("invalid promotion decision") from None


__all__ = [
    "DATASET_ID",
    "NOT_PROMOTED",
    "PROMOTION_SCHEMA_VERSION",
    "PROMOTED",
    "THRESHOLDS",
    "build_promotion_decision",
    "derive_promotion_state",
    "verify_promotion_decision",
]
