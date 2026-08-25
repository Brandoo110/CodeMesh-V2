"""P6-02A: 三臂公开评测的隔离运行合同。

本模块只编排公开 case、执行注入的本地 callable 并保存原始事实；不连接
模型或 adapter，也不读取判定数据，不计算任何评分指标。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .dataset import DATASET_ID, PublicEvalCase, load_public_cases, reviewer_payload


ARM_ORDER: Final[tuple[str, ...]] = (
    "rules_only",
    "single_strong_reviewer",
    "specialized_council",
)
REVIEWER_ROLE_ORDER: Final[tuple[str, ...]] = (
    "rules",
    "general",
    "intent",
    "architecture",
    "operability",
)
_REVIEWER_ROLES: Final[frozenset[str]] = frozenset(REVIEWER_ROLE_ORDER)
_ARM_ROLE_SETS: Final[dict[str, tuple[str, ...]]] = {
    "rules_only": ("rules",),
    "single_strong_reviewer": ("general",),
    "specialized_council": ("intent", "architecture", "operability"),
}
_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "failure", "timeout", "blocked", "schema_invalid"}
)
_SEVERITIES: Final[frozenset[str]] = frozenset(
    {"low", "medium", "high", "critical"}
)
_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"STALE", "BLOCKED", "NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"}
)
_CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"ca_v0_{index:03d}" for index in range(1, 13)
)
_PUBLIC_CATEGORIES: Final[tuple[str, ...]] = (
    "intent",
    "intent",
    "architecture",
    "architecture",
    "architecture",
    "operability",
    "operability",
    "operability",
    "cost",
    "ownership",
    "boundary",
    "freshness",
)
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _validate_unique_strings(value: object, label: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{label} must be a tuple")
    for item in value:
        _require_text(item, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")


def _validate_strings(value: object, label: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{label} must be a tuple")
    for item in value:
        _require_text(item, label)


def _validate_optional_text(value: object, label: str) -> None:
    if value is not None:
        _require_text(value, label)


def _validate_optional_int(value: object, label: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{label} must be a non-negative integer or None")


def _validate_optional_number(value: object, label: str) -> None:
    if value is None:
        return
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative number or None")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number or None")


@dataclass(frozen=True)
class EvalFinding:
    """一个评测臂产生的原始 finding。"""

    finding_id: str
    claim: str
    severity: str
    blocking: bool
    evidence_refs: tuple[str, ...]
    predicted_issue_ids: tuple[str, ...]
    reviewer_role: str = "general"

    def __post_init__(self) -> None:
        _require_text(self.finding_id, "finding_id")
        _require_text(self.claim, "claim")
        if self.severity not in _SEVERITIES:
            raise ValueError("severity is invalid")
        if type(self.blocking) is not bool:
            raise ValueError("blocking must be bool")
        if self.reviewer_role not in _REVIEWER_ROLES:
            raise ValueError("reviewer_role is invalid")
        _validate_unique_strings(self.evidence_refs, "evidence_refs")
        _validate_unique_strings(self.predicted_issue_ids, "predicted_issue_ids")


@dataclass(frozen=True)
class ArmRunResult:
    """一个 case/arm 的原始运行结果，不包含评分结论。"""

    case_id: str
    arm: str
    status: str
    findings: tuple[EvalFinding, ...] = ()
    questions: tuple[str, ...] = ()
    predicted_outcome: str | None = None
    receipt_ref: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: int | float | None = None
    latency_seconds: int | float | None = None
    error_code: str | None = None
    executed_roles: tuple[str, ...] = ()
    model_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if self.arm not in ARM_ORDER:
            raise ValueError("arm is invalid")
        if self.status not in _STATUSES:
            raise ValueError("status is invalid")
        if type(self.findings) is not tuple or any(
            not isinstance(item, EvalFinding) for item in self.findings
        ):
            raise ValueError("findings must be a tuple of EvalFinding")
        _validate_unique_strings(self.questions, "questions")
        if self.predicted_outcome is not None and self.predicted_outcome not in _OUTCOMES:
            raise ValueError("predicted_outcome is invalid")
        _validate_optional_text(self.receipt_ref, "receipt_ref")
        _validate_optional_int(self.input_tokens, "input_tokens")
        _validate_optional_int(self.output_tokens, "output_tokens")
        _validate_optional_number(self.cost_usd, "cost_usd")
        _validate_optional_number(self.latency_seconds, "latency_seconds")
        _validate_optional_text(self.error_code, "error_code")
        _validate_unique_strings(self.executed_roles, "executed_roles")
        if any(role not in _REVIEWER_ROLES for role in self.executed_roles):
            raise ValueError("executed_roles contains an invalid role")
        if tuple(
            sorted(self.executed_roles, key=REVIEWER_ROLE_ORDER.index)
        ) != self.executed_roles:
            raise ValueError("executed_roles must be canonical")
        _validate_strings(self.model_refs, "model_refs")
        if len(self.model_refs) != len(self.executed_roles):
            raise ValueError("model_refs must align with executed_roles")
        expected_roles = _ARM_ROLE_SETS[self.arm]
        if self.status == "success" and self.executed_roles != expected_roles:
            raise ValueError("success result has an invalid executed_roles set")
        if self.status != "success" and not set(self.executed_roles) <= set(
            expected_roles
        ):
            raise ValueError("non-success result has an invalid executed_roles set")
        if any(
            type(finding) is not EvalFinding
            or finding.reviewer_role not in self.executed_roles
            for finding in self.findings
        ):
            raise ValueError("finding reviewer_role is not executed by this arm")
        if self.status == "success" and self.error_code is not None:
            raise ValueError("success result must not contain error_code")
        if self.status != "success" and self.error_code is None:
            raise ValueError("non-success result requires error_code")


@dataclass(frozen=True)
class CaseComparison:
    """一个公开 case 的三臂结果及共同 public payload digest。"""

    case_id: str
    public_payload_digest: str
    arms: tuple[ArmRunResult, ...]

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if type(self.public_payload_digest) is not str or not _DIGEST_RE.fullmatch(
            self.public_payload_digest
        ):
            raise ValueError("public_payload_digest is invalid")
        if type(self.arms) is not tuple or len(self.arms) != len(ARM_ORDER):
            raise ValueError("arms must contain exactly three results")
        if any(not isinstance(item, ArmRunResult) for item in self.arms):
            raise ValueError("arms must contain ArmRunResult values")
        if tuple(item.arm for item in self.arms) != ARM_ORDER:
            raise ValueError("arms must follow ARM_ORDER")
        if any(item.case_id != self.case_id for item in self.arms):
            raise ValueError("arm result case_id mismatch")


@dataclass(frozen=True)
class ComparisonRun:
    """完整 12-case、36-arm 的原始事实集合。"""

    dataset_id: str
    cases: tuple[CaseComparison, ...]

    def __post_init__(self) -> None:
        if self.dataset_id != DATASET_ID:
            raise ValueError("dataset_id is invalid")
        if type(self.cases) is not tuple or len(self.cases) != len(_CASE_IDS):
            raise ValueError("cases must contain exactly 12 comparisons")
        if any(not isinstance(item, CaseComparison) for item in self.cases):
            raise ValueError("cases must contain CaseComparison values")
        if tuple(item.case_id for item in self.cases) != _CASE_IDS:
            raise ValueError("cases must follow the fixed public case order")
        if sum(len(item.arms) for item in self.cases) != 36:
            raise ValueError("comparison run must contain 36 arm results")


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload_bytes).hexdigest()


def _public_case_signature(case: PublicEvalCase) -> tuple[object, ...]:
    return (
        case.case_id,
        case.category,
        case.title,
        case.task_spec,
        case.source_files,
        case.public_test_files,
        case.change_material,
        case.evidence_refs,
    )


_FIXED_PUBLIC_CASE_SIGNATURES: Final[tuple[tuple[object, ...], ...]] = tuple(
    _public_case_signature(case) for case in load_public_cases()
)


def _executor_error(case_id: str, arm: str) -> ArmRunResult:
    return ArmRunResult(
        case_id=case_id,
        arm=arm,
        status="failure",
        error_code="executor_error",
    )


def _schema_invalid(case_id: str, arm: str) -> ArmRunResult:
    return ArmRunResult(
        case_id=case_id,
        arm=arm,
        status="schema_invalid",
        error_code="invalid_arm_result",
    )


class ComparisonRunner:
    """隔离执行三个注入的本地 callable。"""

    def __init__(self, executors: Mapping[str, Callable[[dict[str, object]], object]]):
        if not isinstance(executors, Mapping) or set(executors) != set(ARM_ORDER):
            raise ValueError("executors must have exactly the ARM_ORDER keys")
        if any(not callable(executors[arm]) for arm in ARM_ORDER):
            raise ValueError("each executor must be callable")
        self._executors = dict(executors)

    @staticmethod
    def _validate_public_cases(
        public_cases: tuple[PublicEvalCase, ...],
    ) -> None:
        if type(public_cases) is not tuple or len(public_cases) != len(_CASE_IDS):
            raise ValueError("public_cases must contain exactly 12 cases")
        if any(type(case) is not PublicEvalCase for case in public_cases):
            raise ValueError("public_cases must contain PublicEvalCase values")
        try:
            signatures = tuple(
                _public_case_signature(case) for case in public_cases
            )
        except Exception:
            raise ValueError("public_cases failed fixed-value validation") from None
        if signatures != _FIXED_PUBLIC_CASE_SIGNATURES:
            raise ValueError("public_cases differ from the fixed public dataset")
        if tuple(case.case_id for case in public_cases) != _CASE_IDS:
            raise ValueError("public_cases must follow the fixed public case order")
        if tuple(case.category for case in public_cases) != _PUBLIC_CATEGORIES:
            raise ValueError("public_cases must follow the fixed public category order")

    @staticmethod
    def _revalidate_result(
        result: object,
        case: PublicEvalCase,
        arm: str,
    ) -> ArmRunResult:
        if type(result) is not ArmRunResult:
            return _schema_invalid(case.case_id, arm)
        try:
            if type(result.findings) is not tuple:
                return _schema_invalid(case.case_id, arm)
            findings: list[EvalFinding] = []
            for finding in result.findings:
                if type(finding) is not EvalFinding:
                    return _schema_invalid(case.case_id, arm)
                findings.append(
                    EvalFinding(
                        finding_id=finding.finding_id,
                        claim=finding.claim,
                        severity=finding.severity,
                        blocking=finding.blocking,
                        evidence_refs=finding.evidence_refs,
                        predicted_issue_ids=finding.predicted_issue_ids,
                        reviewer_role=finding.reviewer_role,
                    )
                )
            if len({finding.finding_id for finding in findings}) != len(findings):
                return _schema_invalid(case.case_id, arm)
            rebuilt = ArmRunResult(
                case_id=result.case_id,
                arm=result.arm,
                status=result.status,
                findings=tuple(findings),
                questions=result.questions,
                predicted_outcome=result.predicted_outcome,
                receipt_ref=result.receipt_ref,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                latency_seconds=result.latency_seconds,
                error_code=result.error_code,
                executed_roles=result.executed_roles,
                model_refs=result.model_refs,
            )
            if rebuilt.case_id != case.case_id or rebuilt.arm != arm:
                return _schema_invalid(case.case_id, arm)
            public_refs = set(case.evidence_refs)
            if any(
                not set(finding.evidence_refs) <= public_refs
                for finding in rebuilt.findings
            ):
                return _schema_invalid(case.case_id, arm)
            return rebuilt
        except (AttributeError, TypeError, ValueError, OverflowError):
            return _schema_invalid(case.case_id, arm)

    def run(
        self,
        public_cases: tuple[PublicEvalCase, ...] | None = None,
    ) -> ComparisonRun:
        cases = load_public_cases() if public_cases is None else public_cases
        self._validate_public_cases(cases)
        comparisons: list[CaseComparison] = []
        for case in cases:
            public_payload = reviewer_payload(case)
            payload_bytes = _canonical_payload(public_payload)
            payload_digest = _digest(payload_bytes)
            arm_results: list[ArmRunResult] = []
            for arm in ARM_ORDER:
                isolated_payload = json.loads(payload_bytes.decode("utf-8"))
                try:
                    raw_result = self._executors[arm](isolated_payload)
                except Exception:
                    arm_results.append(_executor_error(case.case_id, arm))
                else:
                    arm_results.append(
                        self._revalidate_result(raw_result, case, arm)
                    )
            comparisons.append(
                CaseComparison(case.case_id, payload_digest, tuple(arm_results))
            )
        return ComparisonRun(DATASET_ID, tuple(comparisons))


__all__ = [
    "ARM_ORDER",
    "ArmRunResult",
    "CaseComparison",
    "ComparisonRun",
    "ComparisonRunner",
    "EvalFinding",
]
