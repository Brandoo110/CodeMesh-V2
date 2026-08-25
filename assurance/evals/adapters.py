"""P6-02B1: strict local model-response adapters.

The adapter accepts only injected callables and public JSON payloads.  It has no
network, tool, retry, fallback, or scoring path.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .runner import ArmRunResult, EvalFinding


ROLE_ORDER: Final[tuple[str, ...]] = (
    "general",
    "intent",
    "architecture",
    "operability",
)
COUNCIL_ROLES: Final[tuple[str, ...]] = ("intent", "architecture", "operability")
_SINGLE_ROLES: Final[tuple[str, ...]] = ("general",)
_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"STALE", "BLOCKED", "NEEDS_HUMAN", "PASS_WITH_WAIVER", "PASS"}
)
_SEVERITIES: Final[frozenset[str]] = frozenset(
    {"low", "medium", "high", "critical"}
)
_TOP_LEVEL_FIELDS: Final[frozenset[str]] = frozenset(
    {"findings", "questions", "predicted_outcome"}
)
_FINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {"claim", "severity", "blocking", "evidence_refs", "predicted_issue_ids"}
)
_OUTCOME_PRIORITY: Final[tuple[str, ...]] = (
    "STALE",
    "BLOCKED",
    "NEEDS_HUMAN",
    "PASS_WITH_WAIVER",
    "PASS",
)
_MAX_RAW_RESPONSE_BYTES: Final[int] = 256 * 1024
_MAX_JSON_DEPTH: Final[int] = 32
_MAX_JSON_NODES: Final[int] = 2048
_MAX_FINDINGS: Final[int] = 64
_MAX_QUESTIONS: Final[int] = 64


def _require_text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} is invalid")


def _validate_metric(value: object, label: str) -> None:
    if value is None:
        return
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} is invalid")


def _validate_optional_int(value: object, label: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{label} is invalid")


@dataclass(frozen=True)
class InvocationFact:
    """一条不含 secret/traceback 的本地 invocation fact。"""

    role: str
    model_ref: str
    provider: str
    raw_response: bytes
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: int | float | None = None
    latency_seconds: int | float | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLE_ORDER:
            raise ValueError("role is invalid")
        _require_text(self.model_ref, "model_ref")
        _require_text(self.provider, "provider")
        if type(self.raw_response) is not bytes:
            raise ValueError("raw_response must be exact bytes")
        if len(self.raw_response) > _MAX_RAW_RESPONSE_BYTES:
            raise ValueError("raw_response is too large")
        if b"\x00" in self.raw_response:
            raise ValueError("raw_response contains NUL")
        try:
            self.raw_response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("raw_response is not UTF-8") from exc
        _validate_optional_int(self.input_tokens, "input_tokens")
        _validate_optional_int(self.output_tokens, "output_tokens")
        _validate_metric(self.cost_usd, "cost_usd")
        _validate_metric(self.latency_seconds, "latency_seconds")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _finding_id(
    case_id: str,
    arm: str,
    role: str,
    claim: str,
    evidence_refs: tuple[str, ...],
    predicted_issue_ids: tuple[str, ...],
) -> str:
    body = {
        "case_id": case_id,
        "arm": arm,
        "role": role,
        "claim": claim,
        "evidence_refs": evidence_refs,
        "predicted_issue_ids": predicted_issue_ids,
    }
    return "finding_" + hashlib.sha256(_canonical_json(body)).hexdigest()


def derive_finding_id(
    case_id: str,
    arm: str,
    role: str,
    claim: str,
    evidence_refs: tuple[str, ...],
    predicted_issue_ids: tuple[str, ...],
) -> str:
    """返回 adapter 使用的 canonical finding ID。"""

    return _finding_id(
        case_id, arm, role, claim, evidence_refs, predicted_issue_ids
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _check_json_limits(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON response exceeds limits")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_limits(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for child in value:
            _check_json_limits(child, depth=depth + 1, nodes=nodes)


def _parse_json_response(raw_response: bytes) -> dict[str, object]:
    if len(raw_response) > _MAX_RAW_RESPONSE_BYTES:
        raise ValueError("raw response is too large")
    if raw_response.startswith(b"\xef\xbb\xbf") or b"\x00" in raw_response:
        raise ValueError("raw response encoding is invalid")
    try:
        text = raw_response.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("model response is not strict JSON") from exc
    if type(value) is not dict:
        raise ValueError("model response must be a JSON object")
    _check_json_limits(value)
    if not set(value) <= _TOP_LEVEL_FIELDS:
        raise ValueError("model response contains unknown fields")
    return value


def _string_list(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) is not list or (not allow_empty and not value):
        raise ValueError(f"{label} is invalid")
    result = []
    for item in value:
        _require_text(item, label)
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return tuple(result)


def _parse_findings(
    value: object,
    *,
    case_id: str,
    arm: str,
    role: str,
    public_evidence_refs: frozenset[str],
) -> tuple[EvalFinding, ...]:
    if type(value) is not list or len(value) > _MAX_FINDINGS:
        raise ValueError("findings is invalid")
    result: list[EvalFinding] = []
    for item in value:
        if type(item) is not dict or set(item) != _FINDING_FIELDS:
            raise ValueError("finding schema is invalid")
        claim = item["claim"]
        _require_text(claim, "claim")
        severity = item["severity"]
        if severity not in _SEVERITIES:
            raise ValueError("finding severity is invalid")
        blocking = item["blocking"]
        if type(blocking) is not bool:
            raise ValueError("finding blocking is invalid")
        evidence_refs = _string_list(item["evidence_refs"], "evidence_refs")
        if not set(evidence_refs) <= public_evidence_refs:
            raise ValueError("finding evidence ref is not public")
        predicted_issue_ids = _string_list(
            item["predicted_issue_ids"], "predicted_issue_ids"
        )
        result.append(
            EvalFinding(
                finding_id=_finding_id(
                    case_id,
                    arm,
                    role,
                    claim,
                    evidence_refs,
                    predicted_issue_ids,
                ),
                claim=claim,
                severity=severity,
                blocking=blocking,
                evidence_refs=evidence_refs,
                predicted_issue_ids=predicted_issue_ids,
                reviewer_role=role,
            )
        )
    if len({finding.finding_id for finding in result}) != len(result):
        raise ValueError("duplicate finding ID")
    return tuple(result)


def _parse_role_response(
    raw_response: bytes,
    *,
    case_id: str,
    arm: str,
    role: str,
    public_evidence_refs: frozenset[str],
) -> tuple[tuple[EvalFinding, ...], tuple[str, ...], str | None]:
    value = _parse_json_response(raw_response)
    findings = _parse_findings(
        value.get("findings", []),
        case_id=case_id,
        arm=arm,
        role=role,
        public_evidence_refs=public_evidence_refs,
    )
    questions_value = value.get("questions", [])
    if type(questions_value) is not list or len(questions_value) > _MAX_QUESTIONS:
        raise ValueError("questions is invalid")
    questions = _string_list(questions_value, "questions")
    predicted_outcome = value.get("predicted_outcome")
    if predicted_outcome is not None and predicted_outcome not in _OUTCOMES:
        raise ValueError("predicted_outcome is invalid")
    return findings, questions, predicted_outcome


def _sum_metric(facts: tuple[InvocationFact, ...], field: str):
    values = tuple(getattr(fact, field) for fact in facts)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _receipt_ref(facts: tuple[InvocationFact, ...]) -> str:
    material = tuple(
        {
            "role": fact.role,
            "model_ref": fact.model_ref,
            "provider": fact.provider,
            "raw_response_digest": "sha256:"
            + hashlib.sha256(fact.raw_response).hexdigest(),
            "input_tokens": fact.input_tokens,
            "output_tokens": fact.output_tokens,
            "cost_usd": fact.cost_usd,
            "latency_seconds": fact.latency_seconds,
        }
        for fact in facts
    )
    return "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()


class ModelArmAdapter:
    """把一个或三个注入 invoker 严格适配为 runner raw result。"""

    def __init__(
        self,
        invokers: Mapping[str, Callable[[dict[str, object]], object]],
        *,
        model_refs: Mapping[str, str] | None = None,
        providers: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(invokers, Mapping) or set(invokers) != set(ROLE_ORDER):
            raise ValueError("invokers must have exactly the role keys")
        if any(not callable(invokers[role]) for role in ROLE_ORDER):
            raise ValueError("each invoker must be callable")
        self._invokers = dict(invokers)
        self._model_refs = self._validated_mapping(
            model_refs or {role: f"model-{role}" for role in ROLE_ORDER},
            "model_refs",
            unique=False,
        )
        self._providers = self._validated_mapping(
            providers or {role: "injected" for role in ROLE_ORDER},
            "providers",
            unique=False,
        )

    @staticmethod
    def _validated_mapping(
        values: Mapping[str, str], label: str, *, unique: bool
    ) -> dict[str, str]:
        if not isinstance(values, Mapping) or set(values) != set(ROLE_ORDER):
            raise ValueError(f"{label} must have exactly the role keys")
        result = {}
        for role in ROLE_ORDER:
            _require_text(values[role], f"{label}.{role}")
            result[role] = values[role]
        if unique and len(set(result.values())) != len(result):
            raise ValueError(f"{label} values must be unique")
        return result

    @staticmethod
    def _payload_context(
        payload: dict[str, object],
    ) -> tuple[str, bytes, frozenset[str]]:
        if type(payload) is not dict:
            raise ValueError("public payload must be a dict")
        case_id = payload.get("case_id")
        _require_text(case_id, "case_id")
        evidence_refs = _string_list(payload.get("evidence_refs"), "evidence_refs")
        return case_id, _canonical_json(payload), frozenset(evidence_refs)

    def _coerce_fact(self, value: object, role: str) -> InvocationFact:
        if type(value) is bytes:
            return InvocationFact(
                role=role,
                model_ref=self._model_refs[role],
                provider=self._providers[role],
                raw_response=value,
            )
        if type(value) is not InvocationFact:
            raise ValueError("invoker returned an invalid fact")
        fact = InvocationFact(
            role=value.role,
            model_ref=value.model_ref,
            provider=value.provider,
            raw_response=value.raw_response,
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            cost_usd=value.cost_usd,
            latency_seconds=value.latency_seconds,
        )
        if (
            fact.role != role
            or fact.model_ref != self._model_refs[role]
            or fact.provider != self._providers[role]
        ):
            raise ValueError("invocation fact identity mismatch")
        return fact

    @staticmethod
    def _failure(
        case_id: str, arm: str, facts: tuple[InvocationFact, ...], error_code: str
    ) -> ArmRunResult:
        return ArmRunResult(
            case_id=case_id,
            arm=arm,
            status="failure" if error_code == "model_invocation_error" else "schema_invalid",
            error_code=error_code,
            executed_roles=tuple(fact.role for fact in facts),
            model_refs=tuple(fact.model_ref for fact in facts),
        )

    def _run(
        self,
        payload: dict[str, object],
        *,
        arm: str,
        roles: tuple[str, ...],
    ) -> ArmRunResult:
        case_id, payload_bytes, public_evidence_refs = self._payload_context(payload)
        facts: list[InvocationFact] = []
        all_findings: list[EvalFinding] = []
        all_questions: list[str] = []
        outcomes: list[str] = []
        for role in roles:
            isolated_payload = json.loads(payload_bytes.decode("utf-8"))
            try:
                returned = self._invokers[role](isolated_payload)
            except Exception:
                return self._failure(
                    case_id, arm, tuple(facts), "model_invocation_error"
                )
            try:
                fact = self._coerce_fact(returned, role)
                findings, questions, outcome = _parse_role_response(
                    fact.raw_response,
                    case_id=case_id,
                    arm=arm,
                    role=role,
                    public_evidence_refs=public_evidence_refs,
                )
            except Exception:
                return self._failure(
                    case_id, arm, tuple(facts), "invalid_model_response"
                )
            facts.append(fact)
            all_findings.extend(findings)
            for question in questions:
                if question not in all_questions:
                    all_questions.append(question)
            if outcome is not None:
                outcomes.append(outcome)
        if len({finding.finding_id for finding in all_findings}) != len(all_findings):
            return self._failure(
                case_id, arm, tuple(facts), "invalid_model_response"
            )
        outcome = next(
            (candidate for candidate in _OUTCOME_PRIORITY if candidate in outcomes),
            "PASS",
        )
        fact_tuple = tuple(facts)
        return ArmRunResult(
            case_id=case_id,
            arm=arm,
            status="success",
            findings=tuple(all_findings),
            questions=tuple(all_questions),
            predicted_outcome=outcome,
            receipt_ref=_receipt_ref(fact_tuple),
            input_tokens=_sum_metric(fact_tuple, "input_tokens"),
            output_tokens=_sum_metric(fact_tuple, "output_tokens"),
            cost_usd=_sum_metric(fact_tuple, "cost_usd"),
            latency_seconds=_sum_metric(fact_tuple, "latency_seconds"),
            executed_roles=tuple(fact.role for fact in fact_tuple),
            model_refs=tuple(fact.model_ref for fact in fact_tuple),
        )

    def run_single(self, payload: dict[str, object]) -> ArmRunResult:
        return self._run(
            payload,
            arm="single_strong_reviewer",
            roles=_SINGLE_ROLES,
        )

    def run_council(self, payload: dict[str, object]) -> ArmRunResult:
        return self._run(
            payload,
            arm="specialized_council",
            roles=COUNCIL_ROLES,
        )


__all__ = [
    "COUNCIL_ROLES",
    "InvocationFact",
    "ModelArmAdapter",
    "ROLE_ORDER",
    "derive_finding_id",
]
