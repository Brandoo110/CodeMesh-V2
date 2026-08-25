"""V2-P3-05 Baseline Report：纯、确定性、不可变的单样本对比报告。

本模块只消费已完成的精确类型结果（RulesOnlyBaselineResult、
SingleReviewerResult、PolicyGateResult）和显式评价判断；不执行 Rules、
RiskClassifier、PolicyGate、模型、reviewer、collector、I/O、时钟、随机、
环境、ArtifactStore、Git、子进程、provider 或网络。所有报告字段都从
BaselineReportInput 重新推导，任何同步伪造（metric、code、digest、ID）
都会被 BaselineReport 校验拒绝。
"""

import base64
import hashlib
import json
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import (
    ChangeSubject,
    ExecutionReceipt,
    Finding,
    HumanDecision,
    PolicyDecision,
)
from .intake import IntakeSnapshot
from .manifest import EvidenceManifest
from .policy import PolicyEvaluationInput, PolicyGateResult
from .risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskDeclarations,
)
from .rules_baseline import RulesOnlyBaselineResult
from .snapshot import GitSnapshot
from .single_reviewer import (
    ReviewQuestion,
    SingleReviewerInput,
    SingleReviewerInvocation,
    SingleReviewerNormalizationInput,
    SingleReviewerPrompt,
    SingleReviewerResult,
)


_MAX_TEXT_BYTES = 256
_MAX_MATCHED_ISSUE_IDS = 256
_MAX_JUDGMENTS = 512

_REASON_REF_RE = re.compile(r"^(?:risk|gate):[A-Z][A-Z0-9_]*\Z")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPORT_ID_RE = re.compile(r"^baseline_[0-9a-f]{32}$")

_OUTCOME_LITERAL = Literal[
    "STALE", "BLOCKED", "NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"
]
_BLOCKED_OUTCOMES = frozenset({"STALE", "BLOCKED", "NEEDS_HUMAN"})
_PASS_OUTCOMES = frozenset({"PASS", "PASS_WITH_WAIVER"})


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bounded_nonblank(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact str")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(
            f"{field_name} must not exceed {_MAX_TEXT_BYTES} UTF-8 bytes"
        )
    return value


def _canonical_matched_issue_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) > _MAX_MATCHED_ISSUE_IDS:
        raise ValueError(
            f"matched_issue_ids must contain at most "
            f"{_MAX_MATCHED_ISSUE_IDS} items"
        )
    seen = set()
    canonical = []
    for item in value:
        bounded = _bounded_nonblank(item, "matched_issue_ids item")
        if bounded in seen:
            raise ValueError("matched_issue_ids must be unique")
        seen.add(bounded)
        canonical.append(bounded)
    return tuple(sorted(canonical))


def _validate_readability(
    *,
    status: str,
    score: int | None,
    assessor_ref: str | None,
) -> None:
    if status == "assessed":
        if score is None:
            raise ValueError(
                "assessed readability requires readability_score"
            )
        if not 1 <= score <= 5:
            raise ValueError("readability_score must be within 1..5")
        if assessor_ref is None:
            raise ValueError(
                "assessed readability requires assessor_ref"
            )
    elif score is not None or assessor_ref is not None:
        raise ValueError(
            "unavailable readability requires score and assessor_ref "
            "to be None"
        )


def _strict_score(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError("readability_score must be an exact int or None")
    return value


class RulesPredictionJudgment(BaseModel):
    """Rules-Only 每条 observed reason ref 的显式评价判断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    prediction_ref: str
    matched_issue_ids: tuple[str, ...] = ()
    support_status: Literal["supported", "unsupported"]
    readability_status: Literal["assessed", "unavailable"]
    readability_score: StrictInt | None = None
    assessor_ref: str | None = None

    @field_validator("prediction_ref", mode="before")
    @classmethod
    def _validate_prediction_ref(cls, value: object) -> str:
        bounded = _bounded_nonblank(value, "prediction_ref")
        if _REASON_REF_RE.fullmatch(bounded) is None:
            raise ValueError(
                "prediction_ref must match risk:CODE or gate:CODE"
            )
        return bounded

    @field_validator("matched_issue_ids", mode="before")
    @classmethod
    def _exact_matched_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "matched_issue_ids must be an exact tuple at raw validation"
        )

    @field_validator("matched_issue_ids")
    @classmethod
    def _canonical_matched(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_matched_issue_ids(value)

    @field_validator("readability_score", mode="before")
    @classmethod
    def _validate_score(cls, value: object) -> object:
        return _strict_score(value)

    @field_validator("assessor_ref", mode="before")
    @classmethod
    def _validate_assessor(cls, value: object) -> object:
        if value is None:
            return None
        return _bounded_nonblank(value, "assessor_ref")

    @model_validator(mode="after")
    def _bind_readability(self) -> "RulesPredictionJudgment":
        _validate_readability(
            status=self.readability_status,
            score=self.readability_score,
            assessor_ref=self.assessor_ref,
        )
        return self


class SingleFindingJudgment(BaseModel):
    """Single Reviewer 每条 Finding 的显式评价判断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    finding_id: str
    matched_issue_ids: tuple[str, ...] = ()
    support_status: Literal["supported", "unsupported"]
    readability_status: Literal["assessed", "unavailable"]
    readability_score: StrictInt | None = None
    assessor_ref: str | None = None

    @field_validator("finding_id", mode="before")
    @classmethod
    def _validate_finding_id(cls, value: object) -> str:
        return _bounded_nonblank(value, "finding_id")

    @field_validator("matched_issue_ids", mode="before")
    @classmethod
    def _exact_matched_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "matched_issue_ids must be an exact tuple at raw validation"
        )

    @field_validator("matched_issue_ids")
    @classmethod
    def _canonical_matched(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_matched_issue_ids(value)

    @field_validator("readability_score", mode="before")
    @classmethod
    def _validate_score(cls, value: object) -> object:
        return _strict_score(value)

    @field_validator("assessor_ref", mode="before")
    @classmethod
    def _validate_assessor(cls, value: object) -> object:
        if value is None:
            return None
        return _bounded_nonblank(value, "assessor_ref")

    @model_validator(mode="after")
    def _bind_readability(self) -> "SingleFindingJudgment":
        _validate_readability(
            status=self.readability_status,
            score=self.readability_score,
            assessor_ref=self.assessor_ref,
        )
        return self


def _exact_json_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ValueError(f"{field_name} must be an exact JSON number or null")
    normalized = float(value)
    if normalized != normalized or normalized in (
        float("inf"),
        float("-inf"),
    ):
        raise ValueError(f"{field_name} must be finite")
    return normalized


class BaselineArmMetrics(BaseModel):
    """一个报告臂的冻结派生指标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    arm: Literal["rules_only", "single_strong_reviewer"]
    prediction_count: StrictInt
    true_positive_predictions: StrictInt
    false_positive_predictions: StrictInt
    precision_status: Literal["available", "unavailable"]
    precision: float | None
    gold_issue_count: StrictInt
    detected_gold_issue_count: StrictInt
    recall_status: Literal["available", "unavailable"]
    recall: float | None
    unsupported_count: StrictInt
    questions_count: StrictInt
    actual_outcome: _OUTCOME_LITERAL
    gold_outcome: _OUTCOME_LITERAL
    outcome_match: StrictBool
    false_block: StrictBool
    false_pass: StrictBool
    usage_status: Literal["not_applicable", "measured", "unavailable"]
    input_tokens: StrictInt | None
    output_tokens: StrictInt | None
    cost_status: Literal["not_applicable", "measured", "unavailable"]
    cost_usd: float | None
    latency_status: Literal["measured", "unavailable"]
    latency_ms: StrictInt | None
    readability_status: Literal["assessed", "unavailable"]
    readability_score: float | None

    @field_validator(
        "prediction_count",
        "true_positive_predictions",
        "false_positive_predictions",
        "gold_issue_count",
        "detected_gold_issue_count",
        "unsupported_count",
        "questions_count",
        mode="before",
    )
    @classmethod
    def _exact_nonnegative_int(cls, value: object) -> int:
        if isinstance(value, bool) or type(value) is not int:
            raise ValueError("counts must be exact nonnegative ints")
        if value < 0:
            raise ValueError("counts must be >= 0")
        return value

    @field_validator("precision", "recall", mode="before")
    @classmethod
    def _validate_rate(cls, value: object) -> object:
        if value is None:
            return None
        normalized = _exact_json_number(value, "precision/recall")
        if not 0.0 <= normalized <= 1.0:
            raise ValueError("precision/recall must be within 0..1")
        return normalized

    @field_validator("input_tokens", "output_tokens", mode="before")
    @classmethod
    def _exact_tokens(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or type(value) is not int or value < 0:
            raise ValueError("token counts must be exact nonnegative ints")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, value: object) -> object:
        if value is None:
            return None
        normalized = _exact_json_number(value, "cost_usd")
        if normalized < 0:
            raise ValueError("cost_usd must be >= 0")
        return normalized

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _exact_latency(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or type(value) is not int or value < 0:
            raise ValueError("latency_ms must be an exact nonnegative int")
        return value

    @field_validator("readability_score", mode="before")
    @classmethod
    def _validate_readability_score(cls, value: object) -> object:
        if value is None:
            return None
        normalized = _exact_json_number(value, "readability_score")
        if not 1.0 <= normalized <= 5.0:
            raise ValueError("readability_score must be within 1..5")
        return normalized

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "BaselineArmMetrics":
        if (self.precision_status == "available") != (self.precision is not None):
            raise ValueError(
                "precision_status must match precision value presence"
            )
        if (self.recall_status == "available") != (self.recall is not None):
            raise ValueError(
                "recall_status must match recall value presence"
            )
        if self.usage_status == "not_applicable":
            if (
                self.input_tokens is not None
                or self.output_tokens is not None
                or self.cost_status != "not_applicable"
            ):
                raise ValueError(
                    "not_applicable usage requires None tokens and "
                    "not_applicable cost status"
                )
        elif self.usage_status == "measured":
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or self.cost_status != "measured"
                or self.cost_usd is None
            ):
                raise ValueError(
                    "measured usage requires tokens, cost status, and cost"
                )
        elif (
            self.input_tokens is not None
            or self.output_tokens is not None
            or self.cost_status != "unavailable"
            or self.cost_usd is not None
        ):
            raise ValueError(
                "unavailable usage requires all usage/cost fields None "
                "and unavailable cost status"
            )
        if (self.latency_status == "measured") != (self.latency_ms is not None):
            raise ValueError(
                "latency_status must match latency_ms value presence"
            )
        if (self.readability_status == "assessed") != (
            self.readability_score is not None
        ):
            raise ValueError(
                "readability_status must match readability_score presence"
            )
        if self.arm == "rules_only":
            if (
                self.usage_status != "not_applicable"
                or self.cost_status != "not_applicable"
                or self.cost_usd != 0.0
                or self.latency_status != "unavailable"
                or self.latency_ms is not None
            ):
                raise ValueError(
                    "rules_only arm requires not_applicable usage/cost, "
                    "cost 0.0, and unavailable latency"
                )
        else:
            if self.usage_status == "not_applicable":
                raise ValueError(
                    "single arm usage must be measured or unavailable"
                )
            if self.latency_status != "measured" or self.latency_ms is None:
                raise ValueError(
                    "single arm requires measured latency"
                )
        return self


def _canonical_judgments(
    judgments: tuple[BaseModel, ...],
    key_name: str,
) -> tuple[BaseModel, ...]:
    if len(judgments) > _MAX_JUDGMENTS:
        raise ValueError(
            f"{key_name} judgments must contain at most {_MAX_JUDGMENTS} items"
        )
    canonical = tuple(
        sorted(
            judgments,
            key=lambda item: getattr(item, key_name),
        )
    )
    for index in range(1, len(canonical)):
        if getattr(canonical[index - 1], key_name) == getattr(
            canonical[index], key_name
        ):
            raise ValueError(f"{key_name} must be unique")
    return canonical


def _decode_raw_response(value: str) -> bytes:
    try:
        candidate = value.encode("utf-8")
        json.loads(candidate)
        return candidate
    except (UnicodeEncodeError, ValueError, TypeError):
        pass
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception:
        raise ValueError(
            "raw_response must be UTF-8 JSON text or base64 in JSON mode"
        ) from None


def _single_result_from_json(raw: dict) -> SingleReviewerResult:
    if type(raw) is not dict:
        raise ValueError("single_result must be a mapping in JSON mode")
    input_raw = raw.get("input")
    if type(input_raw) is not dict:
        raise ValueError(
            "single_result.input must be a mapping in JSON mode"
        )
    raw_response = input_raw.get("raw_response")
    if type(raw_response) is not str:
        raise ValueError(
            "single_result.input.raw_response must be a string in JSON mode"
        )
    reviewer_raw = input_raw.get("reviewer_input")
    prompt_raw = input_raw.get("prompt")
    invocation_raw = input_raw.get("invocation")
    if type(reviewer_raw) is not dict:
        raise ValueError(
            "single_result.input.reviewer_input must be a mapping "
            "in JSON mode"
        )
    if type(prompt_raw) is not dict:
        raise ValueError(
            "single_result.input.prompt must be a mapping in JSON mode"
        )
    if type(invocation_raw) is not dict:
        raise ValueError(
            "single_result.input.invocation must be a mapping in JSON mode"
        )
    reviewer_input = SingleReviewerInput.model_validate_json(
        json.dumps(reviewer_raw)
    )
    prompt = SingleReviewerPrompt.model_validate_json(
        json.dumps(prompt_raw)
    )
    invocation = SingleReviewerInvocation.model_validate_json(
        json.dumps(invocation_raw)
    )
    normalization_input = SingleReviewerNormalizationInput(
        schema_version="v1",
        reviewer_input=reviewer_input,
        prompt=prompt,
        invocation=invocation,
        raw_response=_decode_raw_response(raw_response),
    )
    findings_raw = raw.get("findings")
    questions_raw = raw.get("questions")
    receipt_raw = raw.get("execution_receipt")
    if type(findings_raw) is not list:
        raise ValueError(
            "single_result.findings must be an array in JSON mode"
        )
    if type(questions_raw) is not list:
        raise ValueError(
            "single_result.questions must be an array in JSON mode"
        )
    if type(receipt_raw) is not dict:
        raise ValueError(
            "single_result.execution_receipt must be a mapping in JSON mode"
        )
    findings = tuple(
        Finding.model_validate(item) for item in findings_raw
    )
    questions = tuple(
        ReviewQuestion.model_validate(item) for item in questions_raw
    )
    receipt = ExecutionReceipt.model_validate(receipt_raw)
    return SingleReviewerResult(
        schema_version="v1",
        input=normalization_input,
        raw_response_artifact_digest=raw["raw_response_artifact_digest"],
        canonical_response_digest=raw["canonical_response_digest"],
        findings=findings,
        questions=questions,
        execution_receipt=receipt,
        result_digest=raw["result_digest"],
        result_id=raw["result_id"],
    )


def _risk_input_from_json(raw: dict) -> RiskClassificationInput:
    if type(raw) is not dict:
        raise ValueError("risk_input must be a mapping in JSON mode")
    nested = {
        name: model_type.model_validate(raw[name])
        for name, model_type in (
            ("snapshot", GitSnapshot),
            ("intake", IntakeSnapshot),
            ("manifest", EvidenceManifest),
            ("declarations", RiskDeclarations),
        )
    }
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


def _policy_gate_result_from_json(raw: dict) -> PolicyGateResult:
    if type(raw) is not dict:
        raise ValueError(
            "single_policy_result must be a mapping in JSON mode"
        )
    input_raw = raw.get("input")
    if type(input_raw) is not dict:
        raise ValueError(
            "single_policy_result.input must be a mapping in JSON mode"
        )
    findings_raw = input_raw.get("findings")
    receipts_raw = input_raw.get("execution_receipts")
    decisions_raw = input_raw.get("human_decisions")
    if type(findings_raw) is not list:
        raise ValueError(
            "single_policy_result.input.findings must be an array "
            "in JSON mode"
        )
    if type(receipts_raw) is not list:
        raise ValueError(
            "single_policy_result.input.execution_receipts must be "
            "an array in JSON mode"
        )
    if type(decisions_raw) is not list:
        raise ValueError(
            "single_policy_result.input.human_decisions must be "
            "an array in JSON mode"
        )
    policy_input = PolicyEvaluationInput(
        schema_version="v1",
        subject=ChangeSubject.model_validate(input_raw["subject"]),
        risk_result=_risk_result_from_json(input_raw["risk_result"]),
        findings=tuple(
            Finding.model_validate(item) for item in findings_raw
        ),
        execution_receipts=tuple(
            ExecutionReceipt.model_validate(item)
            for item in receipts_raw
        ),
        human_decisions=tuple(
            HumanDecision.model_validate(item)
            for item in decisions_raw
        ),
        evaluated_at=input_raw["evaluated_at"],
    )
    return PolicyGateResult(
        schema_version="v1",
        input=policy_input,
        decision=PolicyDecision.model_validate(raw["decision"]),
    )


class BaselineReportInput(BaseModel):
    """报告输入：三个已完成的精确结果加上显式评价判断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    rules_result: RulesOnlyBaselineResult
    single_result: SingleReviewerResult
    single_policy_result: PolicyGateResult
    rules_judgments: tuple[RulesPredictionJudgment, ...]
    single_judgments: tuple[SingleFindingJudgment, ...]

    @field_validator("rules_judgments", "single_judgments", mode="before")
    @classmethod
    def _exact_judgment_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "judgments must be an exact tuple at raw validation"
        )

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "BaselineReportInput must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            if type(data.get("rules_result")) is dict:
                data["rules_result"] = RulesOnlyBaselineResult.model_validate_json(
                    json.dumps(data["rules_result"])
                )
            if type(data.get("single_result")) is dict:
                data["single_result"] = _single_result_from_json(
                    data["single_result"]
                )
            if type(data.get("single_policy_result")) is dict:
                data["single_policy_result"] = _policy_gate_result_from_json(
                    data["single_policy_result"]
                )
            if type(data.get("rules_judgments")) is list:
                data["rules_judgments"] = tuple(
                    RulesPredictionJudgment.model_validate_json(
                        json.dumps(item)
                    )
                    for item in data["rules_judgments"]
                )
            if type(data.get("single_judgments")) is list:
                data["single_judgments"] = tuple(
                    SingleFindingJudgment.model_validate_json(
                        json.dumps(item)
                    )
                    for item in data["single_judgments"]
                )
            if "rules_judgments" in data:
                data["rules_judgments"] = _canonical_judgments(
                    data["rules_judgments"], "prediction_ref"
                )
            if "single_judgments" in data:
                data["single_judgments"] = _canonical_judgments(
                    data["single_judgments"], "finding_id"
                )
            return data
        expected = {
            "rules_result": RulesOnlyBaselineResult,
            "single_result": SingleReviewerResult,
            "single_policy_result": PolicyGateResult,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact "
                    f"{model_type.__name__} instance"
                )
        for field_name, item_type in (
            ("rules_judgments", RulesPredictionJudgment),
            ("single_judgments", SingleFindingJudgment),
        ):
            items = data.get(field_name, ())
            if type(items) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple at raw validation"
                )
            for item in items:
                if type(item) is not item_type:
                    raise ValueError(
                        f"{field_name} items must be exact "
                        f"{item_type.__name__} instances"
                    )
        if "rules_judgments" in data:
            data["rules_judgments"] = _canonical_judgments(
                data["rules_judgments"], "prediction_ref"
            )
        if "single_judgments" in data:
            data["single_judgments"] = _canonical_judgments(
                data["single_judgments"], "finding_id"
            )
        return data

    @model_validator(mode="after")
    def _require_bindings(self) -> "BaselineReportInput":
        rules_subject = self.rules_result.fixture.subject.subject_digest
        reviewer_subject = (
            self.single_result.input.reviewer_input.subject.subject_digest
        )
        policy_subject = self.single_policy_result.input.subject.subject_digest
        if not (
            rules_subject == reviewer_subject == policy_subject
        ):
            raise ValueError(
                "rules, single reviewer and single policy must share "
                "exact subject_digest"
            )
        if (
            self.rules_result.risk_result
            != self.single_result.input.reviewer_input.risk_result
        ):
            raise ValueError(
                "rules and single reviewer must share exact risk_result"
            )
        if (
            self.single_policy_result.input.subject
            != self.single_result.input.reviewer_input.subject
        ):
            raise ValueError(
                "single policy subject must equal reviewer input subject"
            )
        if (
            self.single_policy_result.input.risk_result
            != self.single_result.input.reviewer_input.risk_result
        ):
            raise ValueError(
                "single policy risk_result must equal reviewer risk_result"
            )
        if (
            self.single_policy_result.input.findings
            != self.single_result.findings
        ):
            raise ValueError(
                "single policy findings must exactly equal single "
                "result findings in content and order"
            )
        if (
            self.single_result.execution_receipt
            not in self.single_policy_result.input.execution_receipts
        ):
            raise ValueError(
                "single policy execution_receipts must contain the exact "
                "single result execution_receipt"
            )
        observed_refs = self.rules_result.observed_reason_refs
        if len(self.rules_judgments) != len(observed_refs) or {
            judgment.prediction_ref for judgment in self.rules_judgments
        } != set(observed_refs):
            raise ValueError(
                "rules judgments must cover exactly and only observed "
                "reason refs, one judgment per ref"
            )
        finding_ids = {finding.finding_id for finding in self.single_result.findings}
        if len(self.single_judgments) != len(finding_ids) or {
            judgment.finding_id for judgment in self.single_judgments
        } != finding_ids:
            raise ValueError(
                "single judgments must cover exactly and only findings, "
                "one judgment per finding id"
            )
        gold_ids = {
            expectation.issue_id
            for expectation in self.rules_result.fixture.expectations
        }
        for judgment in (
            *self.rules_judgments,
            *self.single_judgments,
        ):
            unknown = set(judgment.matched_issue_ids) - gold_ids
            if unknown:
                raise ValueError(
                    "every matched issue id must exist in fixture "
                    "expectations"
                )
        return self


def _readability_metrics(
    judgments: tuple,
) -> tuple[str, float | None]:
    if not judgments:
        return "unavailable", None
    if any(
        judgment.readability_status != "assessed" for judgment in judgments
    ):
        return "unavailable", None
    scores = [
        judgment.readability_score
        for judgment in judgments
        if judgment.readability_score is not None
    ]
    if len(scores) != len(judgments):
        return "unavailable", None
    return "assessed", float(sum(scores) / len(scores))


def _false_block(actual: str, gold: str) -> bool:
    return actual in _BLOCKED_OUTCOMES and gold in _PASS_OUTCOMES


def _false_pass(actual: str, gold: str) -> bool:
    return actual in _PASS_OUTCOMES and gold in _BLOCKED_OUTCOMES


def _rules_metrics(value: BaselineReportInput) -> BaselineArmMetrics:
    rules = value.rules_result
    judgments = value.rules_judgments
    prediction_count = len(rules.observed_reason_refs)
    true_positives = sum(
        1 for judgment in judgments if judgment.matched_issue_ids
    )
    false_positives = prediction_count - true_positives
    gold_ids = frozenset(
        expectation.issue_id for expectation in rules.fixture.expectations
    )
    matched_union = set().union(
        *(set(judgment.matched_issue_ids) for judgment in judgments)
    ) if judgments else set()
    detected = len(matched_union & gold_ids)
    actual = rules.policy_result.decision.outcome
    gold = rules.fixture.gold_outcome
    readability_status, readability_score = _readability_metrics(judgments)
    return BaselineArmMetrics(
        schema_version="v1",
        arm="rules_only",
        prediction_count=prediction_count,
        true_positive_predictions=true_positives,
        false_positive_predictions=false_positives,
        precision_status=(
            "available" if prediction_count else "unavailable"
        ),
        precision=(
            float(true_positives / prediction_count)
            if prediction_count
            else None
        ),
        gold_issue_count=len(gold_ids),
        detected_gold_issue_count=detected,
        recall_status="available" if gold_ids else "unavailable",
        recall=float(detected / len(gold_ids)) if gold_ids else None,
        unsupported_count=sum(
            1
            for judgment in judgments
            if judgment.support_status == "unsupported"
        ),
        questions_count=0,
        actual_outcome=actual,
        gold_outcome=gold,
        outcome_match=actual == gold,
        false_block=_false_block(actual, gold),
        false_pass=_false_pass(actual, gold),
        usage_status="not_applicable",
        input_tokens=None,
        output_tokens=None,
        cost_status="not_applicable",
        cost_usd=0.0,
        latency_status="unavailable",
        latency_ms=None,
        readability_status=readability_status,
        readability_score=readability_score,
    )


def _single_metrics(value: BaselineReportInput) -> BaselineArmMetrics:
    single = value.single_result
    judgments = value.single_judgments
    severity_by_id = {
        finding.finding_id: finding.severity for finding in single.findings
    }
    precision_eligible = [
        judgment
        for judgment in judgments
        if severity_by_id[judgment.finding_id] != "info"
    ]
    prediction_count = len(precision_eligible)
    true_positives = sum(
        1 for judgment in precision_eligible if judgment.matched_issue_ids
    )
    false_positives = prediction_count - true_positives
    gold_ids = frozenset(
        expectation.issue_id
        for expectation in value.rules_result.fixture.expectations
    )
    matched_union = set().union(
        *(set(judgment.matched_issue_ids) for judgment in judgments)
    ) if judgments else set()
    detected = len(matched_union & gold_ids)
    actual = value.single_policy_result.decision.outcome
    gold = value.rules_result.fixture.gold_outcome
    invocation = single.input.invocation
    if invocation.usage_status == "measured":
        usage_status = "measured"
        input_tokens = invocation.input_tokens
        output_tokens = invocation.output_tokens
        cost_status = "measured"
        cost_usd = invocation.cost_usd
    else:
        usage_status = "unavailable"
        input_tokens = None
        output_tokens = None
        cost_status = "unavailable"
        cost_usd = None
    readability_status, readability_score = _readability_metrics(judgments)
    return BaselineArmMetrics(
        schema_version="v1",
        arm="single_strong_reviewer",
        prediction_count=prediction_count,
        true_positive_predictions=true_positives,
        false_positive_predictions=false_positives,
        precision_status=(
            "available" if prediction_count else "unavailable"
        ),
        precision=(
            float(true_positives / prediction_count)
            if prediction_count
            else None
        ),
        gold_issue_count=len(gold_ids),
        detected_gold_issue_count=detected,
        recall_status="available" if gold_ids else "unavailable",
        recall=float(detected / len(gold_ids)) if gold_ids else None,
        unsupported_count=sum(
            1
            for judgment in judgments
            if judgment.support_status == "unsupported"
        ),
        questions_count=len(single.questions),
        actual_outcome=actual,
        gold_outcome=gold,
        outcome_match=actual == gold,
        false_block=_false_block(actual, gold),
        false_pass=_false_pass(actual, gold),
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_status=cost_status,
        cost_usd=cost_usd,
        latency_status="measured",
        latency_ms=invocation.latency_ms,
        readability_status=readability_status,
        readability_score=readability_score,
    )


def _advisory_disagreement(
    single_result: SingleReviewerResult,
    single_policy_result: PolicyGateResult,
) -> bool:
    if single_policy_result.decision.outcome not in _PASS_OUTCOMES:
        return False
    return any(
        finding.basis == "inferred"
        and finding.severity in ("high", "critical")
        and finding.status in ("open", "acknowledged")
        for finding in single_result.findings
    )


def _limitation_codes(
    value: BaselineReportInput,
    rules_metrics: BaselineArmMetrics,
    single_metrics: BaselineArmMetrics,
) -> tuple[str, ...]:
    codes = ["single_case_only"]
    if (
        rules_metrics.readability_status == "unavailable"
        or single_metrics.readability_status == "unavailable"
    ):
        codes.append("readability_unavailable")
    if value.single_result.input.invocation.usage_status == "unavailable":
        codes.append("single_usage_unavailable")
    codes.append("rules_latency_unavailable")
    if _advisory_disagreement(
        value.single_result, value.single_policy_result
    ):
        codes.append("policy_advisory_disagreement")
    return tuple(codes)


def _report_digest_body(data: dict) -> dict:
    return {
        key: value
        for key, value in _report_json_data(data).items()
        if key not in ("report_id", "report_digest")
    }


def _report_json_data(data: dict) -> dict:
    return {
        "schema_version": data["schema_version"],
        "report_id": data["report_id"],
        "input": data["input"].model_dump(mode="json"),
        "subject_digest": data["subject_digest"],
        "sample_size": data["sample_size"],
        "minimum_promotion_sample_size": data[
            "minimum_promotion_sample_size"
        ],
        "promotion_eligible": data["promotion_eligible"],
        "rules_metrics": data["rules_metrics"].model_dump(mode="json"),
        "single_metrics": data["single_metrics"].model_dump(mode="json"),
        "limitation_codes": list(data["limitation_codes"]),
        "conclusion_codes": list(data["conclusion_codes"]),
        "report_digest": data["report_digest"],
    }


def _derive_report_data(value: BaselineReportInput) -> dict:
    if type(value) is not BaselineReportInput:
        raise TypeError("value must be an exact BaselineReportInput")
    rules_metrics = _rules_metrics(value)
    single_metrics = _single_metrics(value)
    subject_digest = value.rules_result.fixture.subject.subject_digest
    data = {
        "schema_version": "v1",
        "report_id": "",
        "input": value,
        "subject_digest": subject_digest,
        "sample_size": 1,
        "minimum_promotion_sample_size": 12,
        "promotion_eligible": False,
        "rules_metrics": rules_metrics,
        "single_metrics": single_metrics,
        "limitation_codes": _limitation_codes(
            value, rules_metrics, single_metrics
        ),
        "conclusion_codes": ("no_promotion_claim",),
        "report_digest": "",
    }
    report_digest = _sha256_digest(
        _canonical_json_bytes(_report_digest_body(data))
    )
    report_id = "baseline_" + hashlib.sha256(
        (subject_digest + report_digest).encode("utf-8")
    ).hexdigest()[:32]
    data["report_digest"] = report_digest
    data["report_id"] = report_id
    return data


class BaselineReport(BaseModel):
    """不可变基线报告：全部字段从输入重新推导并防伪。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    report_id: str
    input: BaselineReportInput
    subject_digest: str
    sample_size: Literal[1] = 1
    minimum_promotion_sample_size: Literal[12] = 12
    promotion_eligible: Literal[False] = False
    rules_metrics: BaselineArmMetrics
    single_metrics: BaselineArmMetrics
    limitation_codes: tuple[str, ...]
    conclusion_codes: tuple[str, ...]
    report_digest: str

    @field_validator("report_id", mode="before")
    @classmethod
    def _validate_report_id(cls, value: object) -> str:
        if type(value) is not str or _REPORT_ID_RE.fullmatch(value) is None:
            raise ValueError("report_id must be baseline_<32 lowercase hex>")
        return value

    @field_validator("subject_digest", "report_digest", mode="before")
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

    @field_validator("limitation_codes", "conclusion_codes", mode="before")
    @classmethod
    def _exact_code_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError(
            "code tuples must be exact tuples at raw validation"
        )

    @field_validator("limitation_codes", "conclusion_codes")
    @classmethod
    def _validate_code_strings(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for item in value:
            if type(item) is not str or not item.strip():
                raise ValueError("codes must be nonblank exact strings")
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("BaselineReport must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("input")) is dict:
                data["input"] = BaselineReportInput.model_validate_json(
                    json.dumps(data["input"])
                )
            if type(data.get("rules_metrics")) is dict:
                data["rules_metrics"] = BaselineArmMetrics.model_validate_json(
                    json.dumps(data["rules_metrics"])
                )
            if type(data.get("single_metrics")) is dict:
                data["single_metrics"] = BaselineArmMetrics.model_validate_json(
                    json.dumps(data["single_metrics"])
                )
            for field_name in ("limitation_codes", "conclusion_codes"):
                if type(data.get(field_name)) is list:
                    data[field_name] = tuple(data[field_name])
            return data
        expected = {
            "input": BaselineReportInput,
            "rules_metrics": BaselineArmMetrics,
            "single_metrics": BaselineArmMetrics,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact "
                    f"{model_type.__name__} instance"
                )
        for field_name in ("limitation_codes", "conclusion_codes"):
            if type(data.get(field_name)) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple at raw validation"
                )
        return data

    @model_validator(mode="after")
    def _require_derived_report(self) -> "BaselineReport":
        expected = _derive_report_data(self.input)
        if self.model_dump(mode="json") != _report_json_data(expected):
            raise ValueError(
                "report must equal the pure derivation from input"
            )
        return self


class BaselineReportBuilder:
    """纯、无状态、确定性的基线报告 Builder。"""

    @staticmethod
    def build(value: BaselineReportInput) -> BaselineReport:
        if type(value) is not BaselineReportInput:
            raise TypeError("value must be an exact BaselineReportInput")
        return BaselineReport.model_validate(_derive_report_data(value))


__all__ = (
    "RulesPredictionJudgment",
    "SingleFindingJudgment",
    "BaselineArmMetrics",
    "BaselineReportInput",
    "BaselineReport",
    "BaselineReportBuilder",
)
