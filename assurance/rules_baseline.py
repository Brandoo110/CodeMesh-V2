"""V2-P3-03 Rules-Only baseline：纯确定性规则对照。

输入从已物化的 Collector 结果（已通过验证的 ``RiskClassificationInput``）
开始。Runner 只执行：

1. ``RiskClassifier.classify(fixture.risk_input)``
2. 用 fixture 的 subject、该精确风险结果、三个精确空 tuple 与 fixture 的
   ``evaluated_at`` 构造完全验证的 ``PolicyEvaluationInput``
3. ``PolicyGate.evaluate(policy_input)``

本模块不读取 Git/文件/工件，不运行 collector/命令，不调用模型或 reviewer，
不检查当前时间/随机/环境。不伪造 Reviewer receipt 或 Human decision；
``REQUIRED_REVIEWER_MISSING`` 是本基线的真实执行事实，可能造成 false block，
必须原样记录，不得绕过或抑制。
"""

import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import ChangeSubject, PolicyDecision
from .policy import PolicyEvaluationInput, PolicyGate, PolicyGateResult
from .risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
)


_RULES_VERSION = "rules_baseline.v0"

_RULES_TABLE = MappingProxyType(
    {
        "rules_version": _RULES_VERSION,
        "ref_prefixes": ("risk", "gate"),
        "ref_code_pattern": "[A-Z][A-Z0-9_]*",
        "ref_canonical_order": "risk_then_gate_source_order",
        "categories": (
            "intent",
            "architecture",
            "operability",
            "policy",
            "evidence",
            "boundary",
            "ownership",
        ),
        "gold_outcomes": (
            "STALE",
            "BLOCKED",
            "NEEDS_HUMAN",
            "PASS",
            "PASS_WITH_WAIVER",
        ),
        "outcome_sets": MappingProxyType(
            {
                "blocked": frozenset({"BLOCKED"}),
                "pass": frozenset({"PASS", "PASS_WITH_WAIVER"}),
                "false_block_gold": frozenset(
                    {"NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"}
                ),
                "false_pass_gold": frozenset(
                    {"STALE", "BLOCKED", "NEEDS_HUMAN"}
                ),
            }
        ),
        "limits": MappingProxyType(
            {
                "fixture_id_pattern": "[a-z][a-z0-9_]{0,63}",
                "issue_id_max_length": 128,
            }
        ),
        "expectation_order": "strictly_sorted_by_issue_id",
        "derivations": MappingProxyType(
            {
                "observed_reason_refs": (
                    "risk_reason_codes_then_gate_reason_codes_dedup_preserve"
                ),
                "detected_issue": "any_intersection_expected_observed",
                "missed_issue": "no_intersection_or_empty_expected",
                "unexpected_reason_refs": (
                    "observed_minus_union_expectations_allowed_"
                    "preserve_observed_order"
                ),
                "outcome_match": "gate_outcome_equals_gold_outcome",
                "false_block": (
                    "gate_blocked_and_gold_needs_human_or_pass_"
                    "or_pass_with_waiver"
                ),
                "false_pass": (
                    "gate_pass_or_pass_with_waiver_and_gold_stale_"
                    "or_blocked_or_needs_human"
                ),
                "result_id_prefix": "rulesb_",
                "result_digest_exclude": ("result_digest", "result_id"),
            }
        ),
    }
)


def _jsonable(value):
    if isinstance(value, MappingProxyType):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_jsonable(item) for item in value)
    return value


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


_RULES_DIGEST = _sha256_digest(
    _canonical_json_bytes(_jsonable(_RULES_TABLE))
)


def _reason_ref_pattern() -> str:
    prefixes = _RULES_TABLE["ref_prefixes"]
    code = _RULES_TABLE["ref_code_pattern"]
    return (
        r"^(?:"
        + "|".join(re.escape(item) for item in prefixes)
        + r"):"
        + code
        + r"\Z"
    )


_REASON_REF_RE = re.compile(_reason_ref_pattern())
_FIXTURE_ID_RE = re.compile(
    _RULES_TABLE["limits"]["fixture_id_pattern"] + r"\Z"
)
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESULT_ID_RE = re.compile(
    re.escape(_RULES_TABLE["derivations"]["result_id_prefix"])
    + r"[0-9a-f]{32}\Z"
)


def _canonical_reason_refs(items: tuple[str, ...]) -> tuple[str, ...]:
    prefixes = _RULES_TABLE["ref_prefixes"]
    grouped = {prefix: [] for prefix in prefixes}
    seen = set()
    for item in items:
        if type(item) is not str or _REASON_REF_RE.fullmatch(item) is None:
            raise ValueError(
                "reason refs must match risk:CODE or gate:CODE"
            )
        if item in seen:
            raise ValueError("reason refs must be unique")
        seen.add(item)
        grouped[item.split(":", 1)[0]].append(item)
    result = []
    for prefix in prefixes:
        result.extend(sorted(grouped[prefix]))
    return tuple(result)


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _risk_input_from_json(raw: dict) -> RiskClassificationInput:
    if type(raw) is not dict:
        raise ValueError("risk_input must be a mapping in JSON mode")
    nested_types = {
        name: RiskClassificationInput.model_fields[name].annotation
        for name in ("snapshot", "intake", "manifest", "declarations")
    }
    nested = {}
    for name, model_type in nested_types.items():
        nested[name] = model_type.model_validate(raw[name])
    return RiskClassificationInput.model_validate({**raw, **nested})


def _risk_result_from_json(raw: dict) -> RiskClassificationResult:
    if type(raw) is not dict:
        raise ValueError("risk_result must be a mapping in JSON mode")
    return RiskClassificationResult.model_validate(
        {
            "schema_version": raw["schema_version"],
            "input": _risk_input_from_json(raw["input"]),
            "classification": RiskClassification.model_validate_json(
                json.dumps(raw["classification"])
            ),
        }
    )


def _policy_result_from_json(raw: dict) -> PolicyGateResult:
    if type(raw) is not dict:
        raise ValueError("policy_result must be a mapping in JSON mode")
    input_raw = raw["input"]
    subject_raw = input_raw["subject"]
    subject = (
        ChangeSubject.model_validate(subject_raw)
        if type(subject_raw) is dict
        else subject_raw
    )
    input_model = PolicyEvaluationInput.model_validate(
        {
            "schema_version": input_raw["schema_version"],
            "subject": subject,
            "risk_result": _risk_result_from_json(input_raw["risk_result"]),
            "findings": tuple(input_raw.get("findings", [])),
            "execution_receipts": tuple(
                input_raw.get("execution_receipts", [])
            ),
            "human_decisions": tuple(input_raw.get("human_decisions", [])),
            "evaluated_at": input_raw["evaluated_at"],
        }
    )
    decision = PolicyDecision.model_validate_json(
        json.dumps(raw["decision"])
    )
    return PolicyGateResult.model_validate(
        {
            "schema_version": raw["schema_version"],
            "input": input_model,
            "decision": decision,
        }
    )


class RulesOnlyExpectation(BaseModel):
    """一条规则的期望：issue 标识、类别与预期 reason refs。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    issue_id: str = Field(
        min_length=1,
        max_length=_RULES_TABLE["limits"]["issue_id_max_length"],
    )
    category: Literal[
        "intent",
        "architecture",
        "operability",
        "policy",
        "evidence",
        "boundary",
        "ownership",
    ]
    expected_reason_refs: tuple[str, ...] = ()

    @field_validator("issue_id")
    @classmethod
    def _validate_issue_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("issue_id must not be blank or whitespace-only")
        return value

    @field_validator("category")
    @classmethod
    def _category_from_rules_table(cls, value: str) -> str:
        if value not in _RULES_TABLE["categories"]:
            raise ValueError("category must belong to the rules table set")
        return value

    @field_validator("expected_reason_refs", mode="before")
    @classmethod
    def _exact_reason_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "expected_reason_refs must be an exact tuple at raw validation"
        )

    @field_validator("expected_reason_refs")
    @classmethod
    def _canonical_expected_reason_refs(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _canonical_reason_refs(value)


def _expectation_from_json(raw: dict) -> RulesOnlyExpectation:
    if type(raw) is not dict:
        raise ValueError("expectation must be a mapping in JSON mode")
    if type(raw.get("expected_reason_refs")) is list:
        raw = {
            **raw,
            "expected_reason_refs": tuple(raw["expected_reason_refs"]),
        }
    return RulesOnlyExpectation.model_validate(raw)


class RulesOnlyFixture(BaseModel):
    """Rules-Only 基线 fixture：已物化 Collector 输出与金标准。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    fixture_id: str
    subject: ChangeSubject
    risk_input: RiskClassificationInput
    expectations: tuple[RulesOnlyExpectation, ...]
    allowed_reason_refs: tuple[str, ...] = ()
    gold_outcome: Literal[
        "STALE",
        "BLOCKED",
        "NEEDS_HUMAN",
        "PASS",
        "PASS_WITH_WAIVER",
    ]
    evaluated_at: AwareDatetime

    @field_validator("fixture_id", mode="before")
    @classmethod
    def _validate_fixture_id(cls, value: object) -> str:
        if (
            type(value) is not str
            or _FIXTURE_ID_RE.fullmatch(value) is None
        ):
            raise ValueError(
                "fixture_id must match the conservative grammar"
            )
        return value

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _reject_numeric_evaluated_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("expectations", mode="before")
    @classmethod
    def _exact_expectations_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "expectations must be an exact tuple at raw validation"
        )

    @field_validator("allowed_reason_refs", mode="before")
    @classmethod
    def _exact_allowed_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "allowed_reason_refs must be an exact tuple at raw validation"
        )

    @field_validator("allowed_reason_refs")
    @classmethod
    def _canonical_allowed_reason_refs(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _canonical_reason_refs(value)

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("RulesOnlyFixture must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("subject")) is dict:
                data["subject"] = ChangeSubject.model_validate(
                    data["subject"]
                )
            if type(data.get("risk_input")) is dict:
                data["risk_input"] = _risk_input_from_json(
                    data["risk_input"]
                )
            if type(data.get("expectations")) is list:
                data["expectations"] = tuple(
                    _expectation_from_json(item)
                    for item in data["expectations"]
                )
            if type(data.get("allowed_reason_refs")) is list:
                data["allowed_reason_refs"] = tuple(
                    data["allowed_reason_refs"]
                )
            return data
        if type(data.get("subject")) is not ChangeSubject:
            raise ValueError(
                "subject must be an exact ChangeSubject instance"
            )
        if type(data.get("risk_input")) is not RiskClassificationInput:
            raise ValueError(
                "risk_input must be an exact "
                "RiskClassificationInput instance"
            )
        expectations = data.get("expectations", ())
        if type(expectations) is not tuple:
            raise ValueError(
                "expectations must be an exact tuple at raw validation"
            )
        for item in expectations:
            if type(item) is not RulesOnlyExpectation:
                raise ValueError(
                    "expectations items must be exact "
                    "RulesOnlyExpectation instances"
                )
        allowed = data.get("allowed_reason_refs", ())
        if type(allowed) is not tuple:
            raise ValueError(
                "allowed_reason_refs must be an exact tuple "
                "at raw validation"
            )
        return data

    @model_validator(mode="after")
    def _require_exact_parsed_types(self) -> "RulesOnlyFixture":
        if type(self.subject) is not ChangeSubject:
            raise ValueError(
                "subject must be an exact ChangeSubject instance"
            )
        if type(self.risk_input) is not RiskClassificationInput:
            raise ValueError(
                "risk_input must be an exact "
                "RiskClassificationInput instance"
            )
        for item in self.expectations:
            if type(item) is not RulesOnlyExpectation:
                raise ValueError(
                    "expectations items must be exact "
                    "RulesOnlyExpectation instances"
                )
        return self

    @model_validator(mode="after")
    def _require_canonical_expectation_order(
        self,
    ) -> "RulesOnlyFixture":
        expectations = self.expectations
        for index in range(len(expectations) - 1):
            if (
                expectations[index].issue_id
                >= expectations[index + 1].issue_id
            ):
                raise ValueError(
                    "expectations must be strictly sorted by "
                    "issue_id and unique"
                )
        return self

    @model_validator(mode="after")
    def _require_consistent_subject(self) -> "RulesOnlyFixture":
        if (
            self.subject.subject_digest
            != self.risk_input.snapshot.subject_digest
        ):
            raise ValueError(
                "subject subject_digest must equal risk input "
                "subject digest"
            )
        return self

    @model_validator(mode="after")
    def _require_time_bounds(self) -> "RulesOnlyFixture":
        if self.evaluated_at < self.subject.created_at:
            raise ValueError(
                "evaluated_at must be >= subject.created_at"
            )
        if self.evaluated_at < self.risk_input.manifest.evaluated_at:
            raise ValueError(
                "evaluated_at must be >= risk manifest evaluated_at"
            )
        return self


def _observed_reason_refs(
    risk_result: RiskClassificationResult,
    policy_result: PolicyGateResult,
) -> tuple[str, ...]:
    prefixes = _RULES_TABLE["ref_prefixes"]
    risk_codes = risk_result.classification.reason_codes
    gate_codes = policy_result.decision.reason_codes
    seen = set()
    result = []
    for prefix, codes in zip(prefixes, (risk_codes, gate_codes)):
        for code in codes:
            ref = f"{prefix}:{code}"
            if ref not in seen:
                seen.add(ref)
                result.append(ref)
    return tuple(result)


def _detected_and_missed_issue_ids(
    fixture: RulesOnlyFixture,
    observed_reason_refs: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    observed_set = frozenset(observed_reason_refs)
    detected = []
    missed = []
    for expectation in fixture.expectations:
        expected_set = frozenset(expectation.expected_reason_refs)
        if expected_set & observed_set:
            detected.append(expectation.issue_id)
        else:
            missed.append(expectation.issue_id)
    return tuple(detected), tuple(missed)


def _unexpected_reason_refs(
    fixture: RulesOnlyFixture,
    observed_reason_refs: tuple[str, ...],
) -> tuple[str, ...]:
    union = set(fixture.allowed_reason_refs)
    for expectation in fixture.expectations:
        union.update(expectation.expected_reason_refs)
    return tuple(
        ref for ref in observed_reason_refs if ref not in union
    )


def _false_block(actual_outcome: str, gold_outcome: str) -> bool:
    return (
        actual_outcome in _RULES_TABLE["outcome_sets"]["blocked"]
        and gold_outcome in _RULES_TABLE["outcome_sets"]["false_block_gold"]
    )


def _false_pass(actual_outcome: str, gold_outcome: str) -> bool:
    return (
        actual_outcome in _RULES_TABLE["outcome_sets"]["pass"]
        and gold_outcome in _RULES_TABLE["outcome_sets"]["false_pass_gold"]
    )


def _result_json_data(data: dict) -> dict:
    return {
        "schema_version": data["schema_version"],
        "fixture": data["fixture"].model_dump(mode="json"),
        "risk_result": data["risk_result"].model_dump(mode="json"),
        "policy_result": data["policy_result"].model_dump(mode="json"),
        "observed_reason_refs": list(data["observed_reason_refs"]),
        "detected_issue_ids": list(data["detected_issue_ids"]),
        "missed_issue_ids": list(data["missed_issue_ids"]),
        "unexpected_reason_refs": list(data["unexpected_reason_refs"]),
        "outcome_match": data["outcome_match"],
        "false_block": data["false_block"],
        "false_pass": data["false_pass"],
        "spec_digest": data["spec_digest"],
        "result_digest": data["result_digest"],
        "result_id": data["result_id"],
    }


def _result_digest_body(data: dict) -> dict:
    body = _result_json_data(data)
    exclude = _RULES_TABLE["derivations"]["result_digest_exclude"]
    return {
        key: value for key, value in body.items() if key not in exclude
    }


def _result_id_from_body(body: dict) -> str:
    prefix = _RULES_TABLE["derivations"]["result_id_prefix"]
    return prefix + hashlib.sha256(
        _canonical_json_bytes(body)
    ).hexdigest()[:32]


def _derive_result_data(fixture: RulesOnlyFixture) -> dict:
    if type(fixture) is not RulesOnlyFixture:
        raise TypeError("fixture must be an exact RulesOnlyFixture")
    risk_result = RiskClassifier.classify(fixture.risk_input)
    policy_input = PolicyEvaluationInput(
        schema_version="v1",
        subject=fixture.subject,
        risk_result=risk_result,
        findings=(),
        execution_receipts=(),
        human_decisions=(),
        evaluated_at=fixture.evaluated_at,
    )
    policy_result = PolicyGate.evaluate(policy_input)
    observed = _observed_reason_refs(risk_result, policy_result)
    detected, missed = _detected_and_missed_issue_ids(fixture, observed)
    unexpected = _unexpected_reason_refs(fixture, observed)
    actual_outcome = policy_result.decision.outcome
    gold_outcome = fixture.gold_outcome
    data = {
        "schema_version": "v1",
        "fixture": fixture,
        "risk_result": risk_result,
        "policy_result": policy_result,
        "observed_reason_refs": observed,
        "detected_issue_ids": detected,
        "missed_issue_ids": missed,
        "unexpected_reason_refs": unexpected,
        "outcome_match": actual_outcome == gold_outcome,
        "false_block": _false_block(actual_outcome, gold_outcome),
        "false_pass": _false_pass(actual_outcome, gold_outcome),
        "spec_digest": _RULES_DIGEST,
        "result_digest": "",
        "result_id": "",
    }
    digest_body = _result_digest_body(data)
    data["result_digest"] = _sha256_digest(
        _canonical_json_bytes(digest_body)
    )
    data["result_id"] = _result_id_from_body(digest_body)
    return data


class RulesOnlyBaselineResult(BaseModel):
    """Rules-Only 基线完整结果：fixture 与纯派生的逐字段绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    fixture: RulesOnlyFixture
    risk_result: RiskClassificationResult
    policy_result: PolicyGateResult
    observed_reason_refs: tuple[str, ...]
    detected_issue_ids: tuple[str, ...]
    missed_issue_ids: tuple[str, ...]
    unexpected_reason_refs: tuple[str, ...]
    outcome_match: StrictBool
    false_block: StrictBool
    false_pass: StrictBool
    spec_digest: str
    result_digest: str
    result_id: str

    @field_validator(
        "observed_reason_refs",
        "unexpected_reason_refs",
        mode="before",
    )
    @classmethod
    def _exact_reason_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("must be an exact tuple at raw validation")

    @field_validator(
        "detected_issue_ids",
        "missed_issue_ids",
        mode="before",
    )
    @classmethod
    def _exact_issue_ids_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("must be an exact tuple at raw validation")

    @field_validator("observed_reason_refs", "unexpected_reason_refs")
    @classmethod
    def _validate_reason_refs(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        seen = set()
        for item in value:
            if (
                type(item) is not str
                or _REASON_REF_RE.fullmatch(item) is None
            ):
                raise ValueError(
                    "reason refs must match risk:CODE or gate:CODE"
                )
            if item in seen:
                raise ValueError("reason refs must be unique")
            seen.add(item)
        return value

    @field_validator("detected_issue_ids", "missed_issue_ids")
    @classmethod
    def _validate_issue_ids(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        seen = set()
        for item in value:
            if type(item) is not str or not item.strip():
                raise ValueError(
                    "issue ids must be nonblank exact strings"
                )
            if item in seen:
                raise ValueError("issue ids must be unique")
            seen.add(item)
        return value

    @field_validator("spec_digest", "result_digest", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        if (
            type(value) is not str
            or _SHA256_DIGEST_RE.fullmatch(value) is None
        ):
            raise ValueError(
                "must be a lowercase sha256:<64 hex> digest"
            )
        return value

    @field_validator("result_id", mode="before")
    @classmethod
    def _validate_result_id(cls, value: object) -> str:
        if type(value) is not str or _RESULT_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "result_id must be rulesb_<32 lowercase hex>"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "RulesOnlyBaselineResult must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            if type(data.get("fixture")) is dict:
                data["fixture"] = RulesOnlyFixture.model_validate_json(
                    json.dumps(data["fixture"])
                )
            if type(data.get("risk_result")) is dict:
                data["risk_result"] = _risk_result_from_json(
                    data["risk_result"]
                )
            if type(data.get("policy_result")) is dict:
                data["policy_result"] = _policy_result_from_json(
                    data["policy_result"]
                )
            for field_name in (
                "observed_reason_refs",
                "detected_issue_ids",
                "missed_issue_ids",
                "unexpected_reason_refs",
            ):
                if type(data.get(field_name)) is list:
                    data[field_name] = tuple(data[field_name])
            return data
        expected = {
            "fixture": RulesOnlyFixture,
            "risk_result": RiskClassificationResult,
            "policy_result": PolicyGateResult,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact "
                    f"{model_type.__name__} instance"
                )
        for field_name in (
            "observed_reason_refs",
            "detected_issue_ids",
            "missed_issue_ids",
            "unexpected_reason_refs",
        ):
            if type(data.get(field_name)) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple "
                    "at raw validation"
                )
        return data

    @model_validator(mode="after")
    def _require_exact_parsed_types(
        self,
    ) -> "RulesOnlyBaselineResult":
        for field_name, model_type in (
            ("fixture", RulesOnlyFixture),
            ("risk_result", RiskClassificationResult),
            ("policy_result", PolicyGateResult),
        ):
            if type(getattr(self, field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact "
                    f"{model_type.__name__} instance"
                )
        return self

    @model_validator(mode="after")
    def _require_derived_result(
        self,
    ) -> "RulesOnlyBaselineResult":
        expected = _derive_result_data(self.fixture)
        if self.model_dump(mode="json") != _result_json_data(expected):
            raise ValueError(
                "result must equal the pure derivation from fixture"
            )
        return self


class RulesOnlyBaselineRunner:
    """纯、无状态、确定性的 Rules-Only 基线 Runner。"""

    @staticmethod
    def run(value: RulesOnlyFixture) -> RulesOnlyBaselineResult:
        if type(value) is not RulesOnlyFixture:
            raise TypeError("value must be an exact RulesOnlyFixture")
        return RulesOnlyBaselineResult(**_derive_result_data(value))


__all__ = (
    "RulesOnlyExpectation",
    "RulesOnlyFixture",
    "RulesOnlyBaselineResult",
    "RulesOnlyBaselineRunner",
)
