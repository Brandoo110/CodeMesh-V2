"""V2-P4-01 Reviewer 基础合同：纯 Pydantic v2 模型验证，无 I/O、无外部调用。

本模块只为 P4-02~P4-05 提供统一 Reviewer 输入、Finding/Question 输出、
显式 budget/allowlist 与 fail-closed failure outcome。不实现具体 Reviewer、
prompt、provider/网络调用、工具执行、并发、路由、Receipt、Adjudicator、
Council Report、数据库、I/O、时钟读取或随机。
"""

import json
import re
from types import MappingProxyType
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import ChangeSubject, Finding
from .intake import IntakeSnapshot
from .manifest import EvidenceManifest
from .risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskDeclarations,
)
from .single_reviewer import ReviewerEvidenceContext, ReviewQuestion
from .snapshot import GitSnapshot

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

_MAX_CONTEXTS = 16
_MAX_ALLOWLIST_ITEMS = 64
_MAX_ALLOWLIST_ITEM_BYTES = 256
_MAX_RUBRIC_VERSION_BYTES = 128
_MAX_DETAILS_BYTES = 4096
_MAX_TIMEOUT_SECONDS = 3600

_OUTCOME_FAILURE_CODES = MappingProxyType(
    {
        "failure": "execution_failed",
        "timeout": "timeout",
        "cancelled": "cancelled",
        "budget_exceeded": "budget_exceeded",
        "schema_invalid": "schema_invalid",
    }
)


def _validate_exact_str(
    value: object, *, field_name: str, max_bytes: int
) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact str")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"{field_name} must not exceed {max_bytes} UTF-8 bytes"
        )
    return value


def _validate_sha256_digest(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase sha256:<64 hex> digest"
        )
    return value


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _validate_allowlist_items(
    value: tuple[str, ...], *, field_name: str
) -> tuple[str, ...]:
    if len(value) > _MAX_ALLOWLIST_ITEMS:
        raise ValueError(
            f"{field_name} must contain at most {_MAX_ALLOWLIST_ITEMS} items"
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _validate_exact_str(
            item,
            field_name=f"{field_name} item",
            max_bytes=_MAX_ALLOWLIST_ITEM_BYTES,
        )
        if text in seen:
            raise ValueError(f"{field_name} items must be unique")
        seen.add(text)
        result.append(text)
    if result != sorted(result):
        raise ValueError(
            f"{field_name} must be lexicographically sorted and unique"
        )
    return tuple(result)


def _required_mapping_value(raw: dict, key: str, *, path: str) -> dict:
    value = raw.get(key)
    if type(value) is not dict:
        raise ValueError(f"{path}.{key} must be a mapping in JSON mode")
    return value


def _risk_input_from_json_data(raw: dict) -> RiskClassificationInput:
    nested = {
        name: model_type.model_validate(
            _required_mapping_value(raw, name, path="risk_result.input")
        )
        for name, model_type in (
            ("snapshot", GitSnapshot),
            ("intake", IntakeSnapshot),
            ("manifest", EvidenceManifest),
            ("declarations", RiskDeclarations),
        )
    }
    return RiskClassificationInput.model_validate({**raw, **nested})


def _risk_result_from_json_data(raw: dict) -> RiskClassificationResult:
    input_raw = _required_mapping_value(raw, "input", path="risk_result")
    classification = RiskClassification.model_validate_json(
        json.dumps(
            _required_mapping_value(
                raw, "classification", path="risk_result"
            )
        )
    )
    return RiskClassificationResult.model_validate(
        {
            **raw,
            "input": _risk_input_from_json_data(input_raw),
            "classification": classification,
        }
    )


def _reviewer_input_from_json(raw: dict) -> "ReviewerInput":
    if type(raw) is not dict:
        raise ValueError("reviewer input must be a mapping in JSON mode")
    subject_raw = raw.get("subject")
    if type(subject_raw) is not dict:
        raise ValueError("subject must be a mapping in JSON mode")
    risk_result_raw = raw.get("risk_result")
    if type(risk_result_raw) is not dict:
        raise ValueError("risk_result must be a mapping in JSON mode")
    contexts_raw = raw.get("contexts")
    if type(contexts_raw) not in (list, tuple):
        raise ValueError("contexts must be an array in JSON mode")
    allowlists: dict[str, object] = {}
    for field_name in ("evidence_allowlist", "tool_allowlist"):
        value = raw.get(field_name)
        if type(value) is list:
            allowlists[field_name] = tuple(value)
    return ReviewerInput.model_validate(
        {
            **raw,
            "subject": ChangeSubject.model_validate(subject_raw),
            "risk_result": _risk_result_from_json_data(risk_result_raw),
            "contexts": tuple(
                ReviewerEvidenceContext.model_validate(item)
                for item in contexts_raw
            ),
            **allowlists,
        }
    )


class ReviewerInput(BaseModel):
    """评审者输入：主题、风险结果、证据上下文与显式 budget/allowlist。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    reviewer_role: Literal["intent", "architecture", "operability"]
    subject: ChangeSubject
    risk_result: RiskClassificationResult
    contexts: tuple[ReviewerEvidenceContext, ...]
    rubric_version: str
    rubric_hash: str
    evidence_allowlist: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    timeout_seconds: int
    token_budget: int | None
    cost_budget_usd: float | None = Field(
        default=None, allow_inf_nan=False, ge=0.0
    )
    requested_at: AwareDatetime

    @field_validator("requested_at", mode="before")
    @classmethod
    def _reject_numeric_requested_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("rubric_version", mode="before")
    @classmethod
    def _validate_rubric_version(cls, value: object) -> str:
        return _validate_exact_str(
            value,
            field_name="rubric_version",
            max_bytes=_MAX_RUBRIC_VERSION_BYTES,
        )

    @field_validator("rubric_hash", mode="before")
    @classmethod
    def _validate_rubric_hash(cls, value: object) -> str:
        return _validate_sha256_digest(value, field_name="rubric_hash")

    @field_validator(
        "contexts",
        "evidence_allowlist",
        "tool_allowlist",
        mode="before",
    )
    @classmethod
    def _exact_tuple_fields(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        raise ValueError(
            f"{info.field_name} must be an exact tuple at raw validation"
        )

    @field_validator("timeout_seconds", "token_budget", mode="before")
    @classmethod
    def _exact_int_fields(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if value is None and info.field_name == "token_budget":
            return value
        if type(value) is not int:
            raise ValueError(f"{info.field_name} must be an exact int")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout_range(cls, value: int) -> int:
        if not 0 < value <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be > 0 and <= {_MAX_TIMEOUT_SECONDS}"
            )
        return value

    @field_validator("token_budget")
    @classmethod
    def _validate_token_budget(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("token_budget must be > 0 when present")
        return value

    @field_validator("cost_budget_usd", mode="before")
    @classmethod
    def _exact_float_field(cls, value: object) -> object:
        if value is None:
            return value
        if type(value) is not float:
            raise ValueError("cost_budget_usd must be an exact float or None")
        return value

    @field_validator("evidence_allowlist", "tool_allowlist")
    @classmethod
    def _validate_allowlists(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        return _validate_allowlist_items(
            value, field_name=info.field_name
        )

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("ReviewerInput must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("subject")) is dict:
                data["subject"] = ChangeSubject.model_validate(data["subject"])
            if type(data.get("risk_result")) is dict:
                data["risk_result"] = _risk_result_from_json_data(
                    data["risk_result"]
                )
            contexts_raw = data.get("contexts")
            if type(contexts_raw) in (list, tuple):
                data["contexts"] = tuple(
                    ReviewerEvidenceContext.model_validate(item)
                    if type(item) is dict
                    else item
                    for item in contexts_raw
                )
            return data
        if type(data.get("subject")) is not ChangeSubject:
            raise ValueError(
                "subject must be an exact ChangeSubject instance"
            )
        if type(data.get("risk_result")) is not RiskClassificationResult:
            raise ValueError(
                "risk_result must be an exact RiskClassificationResult instance"
            )
        if type(data.get("contexts")) is not tuple:
            raise ValueError(
                "contexts must be an exact tuple at raw validation"
            )
        for item in data.get("contexts", ()):
            if type(item) is not ReviewerEvidenceContext:
                raise ValueError(
                    "context items must be exact ReviewerEvidenceContext "
                    "instances"
                )
        return data

    @model_validator(mode="after")
    def _validate_bindings(self) -> "ReviewerInput":
        if type(self.subject) is not ChangeSubject:
            raise ValueError("subject must be an exact ChangeSubject instance")
        if type(self.risk_result) is not RiskClassificationResult:
            raise ValueError(
                "risk_result must be an exact RiskClassificationResult instance"
            )
        contexts = self.contexts
        if not 1 <= len(contexts) <= _MAX_CONTEXTS:
            raise ValueError(f"contexts must contain 1..{_MAX_CONTEXTS} items")
        ids = [item.evidence_id for item in contexts]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ValueError(
                "contexts must be strictly sorted by evidence_id and unique"
            )
        for item in contexts:
            if type(item) is not ReviewerEvidenceContext:
                raise ValueError(
                    "context items must be exact ReviewerEvidenceContext "
                    "instances"
                )
        risk_input = self.risk_result.input
        subject_digest = self.subject.subject_digest
        if subject_digest != risk_input.snapshot.subject_digest:
            raise ValueError(
                "subject digest must equal risk input snapshot subject digest"
            )
        if subject_digest != risk_input.intake.subject_digest:
            raise ValueError(
                "subject digest must equal risk input intake subject digest"
            )
        if subject_digest != risk_input.manifest.subject_digest:
            raise ValueError(
                "subject digest must equal risk input manifest subject digest"
            )
        if subject_digest != self.risk_result.classification.subject_digest:
            raise ValueError(
                "subject digest must equal risk classification subject digest"
            )
        entries = {
            entry.evidence_id: entry
            for entry in risk_input.manifest.entries
        }
        for item in contexts:
            entry = entries.get(item.evidence_id)
            if entry is None:
                raise ValueError(
                    "every context must reference an existing manifest entry"
                )
            if (
                entry.kind != item.kind
                or entry.artifact_digest != item.artifact_digest
            ):
                raise ValueError(
                    "context must match manifest evidence_id, kind, "
                    "and artifact_digest"
                )
        if self.evidence_allowlist != tuple(
            item.evidence_id for item in contexts
        ):
            raise ValueError(
                "evidence_allowlist must exactly equal context evidence IDs"
            )
        if self.requested_at < self.subject.created_at:
            raise ValueError("requested_at must be >= subject.created_at")
        if self.requested_at < risk_input.manifest.evaluated_at:
            raise ValueError(
                "requested_at must be >= manifest.evaluated_at"
            )
        return self


class ReviewerFailureOutcome(BaseModel):
    """结构化失败事实：code + details，不带 payload/trace/工具输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    code: Literal[
        "execution_failed",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "schema_invalid",
    ]
    details: str

    @field_validator("details", mode="before")
    @classmethod
    def _validate_details(cls, value: object) -> str:
        return _validate_exact_str(
            value, field_name="details", max_bytes=_MAX_DETAILS_BYTES
        )


class FindingOutput(BaseModel):
    """评审输出：成功可带 findings/questions；失败必须 fail-closed。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: ReviewerInput
    outcome: Literal[
        "success",
        "failure",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "schema_invalid",
    ]
    findings: tuple[Finding, ...] = ()
    questions: tuple[ReviewQuestion, ...] = ()
    failure: ReviewerFailureOutcome | None = None
    completed_at: AwareDatetime

    @field_validator("completed_at", mode="before")
    @classmethod
    def _reject_numeric_completed_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("findings", "questions", mode="before")
    @classmethod
    def _exact_item_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        raise ValueError(
            f"{info.field_name} must be an exact tuple at raw validation"
        )

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("FindingOutput must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("input")) is dict:
                data["input"] = _reviewer_input_from_json(data["input"])
            if "findings" in data:
                items = data["findings"]
                if type(items) in (list, tuple):
                    data["findings"] = tuple(
                        Finding.model_validate(item)
                        if type(item) is dict
                        else item
                        for item in items
                    )
            if "questions" in data:
                items = data["questions"]
                if type(items) in (list, tuple):
                    data["questions"] = tuple(
                        _question_from_json(item)
                        if type(item) is dict
                        else item
                        for item in items
                    )
            if type(data.get("failure")) is dict:
                data["failure"] = ReviewerFailureOutcome.model_validate(
                    data["failure"]
                )
            return data
        if type(data.get("input")) is not ReviewerInput:
            raise ValueError("input must be an exact ReviewerInput instance")
        if "findings" in data and type(data["findings"]) is not tuple:
            raise ValueError(
                "findings must be an exact tuple at raw validation"
            )
        if "questions" in data and type(data["questions"]) is not tuple:
            raise ValueError(
                "questions must be an exact tuple at raw validation"
            )
        for item in data.get("findings", ()):
            if type(item) is not Finding:
                raise ValueError(
                    "finding items must be exact Finding instances"
                )
        for item in data.get("questions", ()):
            if type(item) is not ReviewQuestion:
                raise ValueError(
                    "question items must be exact ReviewQuestion instances"
                )
        failure = data.get("failure")
        if failure is not None and type(failure) is not ReviewerFailureOutcome:
            raise ValueError(
                "failure must be an exact ReviewerFailureOutcome instance "
                "or None"
            )
        return data

    @model_validator(mode="after")
    def _validate_outcome_and_bindings(self) -> "FindingOutput":
        if type(self.input) is not ReviewerInput:
            raise ValueError("input must be an exact ReviewerInput instance")
        if type(self.findings) is not tuple:
            raise ValueError(
                "findings must be an exact tuple at raw validation"
            )
        if type(self.questions) is not tuple:
            raise ValueError(
                "questions must be an exact tuple at raw validation"
            )
        for item in self.findings:
            if type(item) is not Finding:
                raise ValueError(
                    "finding items must be exact Finding instances"
                )
        for item in self.questions:
            if type(item) is not ReviewQuestion:
                raise ValueError(
                    "question items must be exact ReviewQuestion instances"
                )
        if (
            self.failure is not None
            and type(self.failure) is not ReviewerFailureOutcome
        ):
            raise ValueError(
                "failure must be an exact ReviewerFailureOutcome instance "
                "or None"
            )
        if self.completed_at < self.input.requested_at:
            raise ValueError("completed_at must be >= input.requested_at")
        finding_ids = [item.finding_id for item in self.findings]
        if finding_ids != sorted(finding_ids) or len(set(finding_ids)) != len(
            finding_ids
        ):
            raise ValueError(
                "findings must be strictly sorted by finding_id and unique"
            )
        question_ids = [item.question_id for item in self.questions]
        if question_ids != sorted(question_ids) or len(set(question_ids)) != (
            len(question_ids)
        ):
            raise ValueError(
                "questions must be strictly sorted by question_id and unique"
            )
        if self.outcome == "success":
            if self.failure is not None:
                raise ValueError(
                    "success must not carry a failure outcome"
                )
        else:
            if self.failure is None:
                raise ValueError(
                    "non-success outcomes require a failure outcome"
                )
            if self.failure.code != _OUTCOME_FAILURE_CODES[self.outcome]:
                raise ValueError("failure.code must match outcome")
            if self.findings or self.questions:
                raise ValueError(
                    "non-success outcomes must not carry findings or questions"
                )
        if self.findings or self.questions:
            expected_subject = self.input.subject.subject_digest
            expected_role = self.input.reviewer_role
            expected_rubric = self.input.rubric_hash
            allowed_refs = frozenset(self.input.evidence_allowlist)
            for item in self.findings:
                if item.subject_digest != expected_subject:
                    raise ValueError(
                        "finding subject_digest must equal input subject digest"
                    )
                if item.reviewer_role != expected_role:
                    raise ValueError(
                        "finding reviewer_role must equal input reviewer_role"
                    )
                if item.rubric_hash != expected_rubric:
                    raise ValueError(
                        "finding rubric_hash must equal input rubric_hash"
                    )
                for ref in item.evidence_refs:
                    if ref not in allowed_refs:
                        raise ValueError(
                            "finding evidence_refs must be a subset of "
                            "input evidence_allowlist"
                        )
            for item in self.questions:
                if item.subject_digest != expected_subject:
                    raise ValueError(
                        "question subject_digest must equal input subject digest"
                    )
                if item.reviewer_role != expected_role:
                    raise ValueError(
                        "question reviewer_role must equal input reviewer_role"
                    )
                if item.rubric_hash != expected_rubric:
                    raise ValueError(
                        "question rubric_hash must equal input rubric_hash"
                    )
                for ref in item.evidence_refs:
                    if ref not in allowed_refs:
                        raise ValueError(
                            "question evidence_refs must be a subset of "
                            "input evidence_allowlist"
                        )
        return self


def _question_from_json(raw: dict) -> ReviewQuestion:
    if type(raw) is not dict:
        raise ValueError("question must be a mapping in JSON mode")
    data = dict(raw)
    if type(data.get("evidence_refs")) is list:
        data["evidence_refs"] = tuple(data["evidence_refs"])
    return ReviewQuestion.model_validate(data)
