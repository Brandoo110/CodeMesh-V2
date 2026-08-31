"""V2-P3-04A 单一强评审者：确定性 prompt 与纯响应规范化。

信任边界：本模块不包含任何 provider/网络传输。外部编排器把确定性 prompt
交给一个强模型，再把精确原始 UTF-8 JSON 字节注入本模块规范化。不授予任何
工具，尤其没有写工具；不读取仓库版本控制/路径/环境/当前时间/随机；不执行
命令，也绝不执行响应内容。redaction 状态只是上游声明，本模块不声称做过
秘密扫描或已核实脱敏。
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import ChangeSubject, ExecutionReceipt, ExecutionStep, Finding
from .risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
)


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_QUESTION_ID_RE = re.compile(r"^rq_[0-9a-f]{32}$")
_PROMPT_ID_RE = re.compile(r"^srp_[0-9a-f]{32}$")
_RESULT_ID_RE = re.compile(r"^srr_[0-9a-f]{32}$")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

_MAX_ID_BYTES = 256
_MAX_KIND_BYTES = 256
_MAX_CONTENT_BYTES = 65536
_MAX_QUESTION_BYTES = 4096
_MAX_CLAIM_BYTES = 4096
_MAX_REF_BYTES = 256
_MAX_REFS = 16
_MAX_CONTEXTS = 16
_MAX_CONTEXT_CONTENT_AGGREGATE = 262144
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 4096
_MAX_RESPONSE_ITEMS = 256
_MAX_PROMPT_BYTES = 1024 * 1024

_PAYLOAD_ERROR_MESSAGE = "invalid single reviewer payload"
_SUBJECT_MISMATCH_MESSAGE = "single reviewer subject digest mismatch"
_ARTIFACT_ERROR_MESSAGE = "single reviewer artifact persistence failed"

_RUBRIC_VERSION = "single_general.v0"

_RUBRIC_TABLE = MappingProxyType(
    {
        "rubric_version": _RUBRIC_VERSION,
        "roles": MappingProxyType(
            {
                "intent": MappingProxyType(
                    {
                        "items": (
                            MappingProxyType(
                                {
                                    "number": 1,
                                    "code": "SCOPE_ALIGNMENT",
                                    "name": "Scope alignment",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 2,
                                    "code": "ACCEPTANCE_NFR_COVERAGE",
                                    "name": "Acceptance and NFR coverage",
                                }
                            ),
                        )
                    }
                ),
                "architecture": MappingProxyType(
                    {
                        "items": (
                            MappingProxyType(
                                {
                                    "number": 3,
                                    "code": "BOUNDARY_DEPENDENCY_DIRECTION",
                                    "name": "Boundary and dependency direction",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 4,
                                    "code": "SECOND_SOURCE_DUPLICATION",
                                    "name": "Second-source duplication",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 5,
                                    "code": "PUBLIC_CONTRACT_ADR",
                                    "name": "Public contract and ADR",
                                }
                            ),
                        )
                    }
                ),
                "operability": MappingProxyType(
                    {
                        "items": (
                            MappingProxyType(
                                {
                                    "number": 6,
                                    "code": "MIGRATION_ROLLBACK",
                                    "name": "Migration and rollback",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 7,
                                    "code": "RETRY_IDEMPOTENCY_SIDE_EFFECTS",
                                    "name": "Retry idempotency and side effects",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 8,
                                    "code": "OBSERVABILITY_KILL_SWITCH",
                                    "name": "Observability and kill switch",
                                }
                            ),
                            MappingProxyType(
                                {
                                    "number": 9,
                                    "code": "OWNERSHIP_RUNBOOK",
                                    "name": "Ownership and runbook",
                                }
                            ),
                        )
                    }
                ),
            }
        ),
    }
)


def _jsonable(value):
    if isinstance(value, MappingProxyType) or isinstance(value, dict):
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


def _canonical_bytes(value) -> bytes:
    return _canonical_json_bytes(value)


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rubric_digest_from_table() -> str:
    return _sha256_digest(_canonical_json_bytes(_jsonable(_RUBRIC_TABLE)))


_RUBRIC_DIGEST = _rubric_digest_from_table()


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _validate_text(value: object, *, max_bytes: int) -> str:
    if type(value) is not str:
        raise ValueError("must be an exact str")
    if not value.strip():
        raise ValueError("must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"must not exceed {max_bytes} UTF-8 bytes")
    return value


def _validate_sha256_digest(value: object) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


def _validate_refs(value: tuple[str, ...], *, canonical: bool) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError("evidence_refs must be an exact tuple")
    if len(value) > _MAX_REFS:
        raise ValueError(f"evidence_refs must contain at most {_MAX_REFS} items")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip() or "\x00" in item:
            raise ValueError("refs must be nonblank exact strings")
        if len(item.encode("utf-8")) > _MAX_REF_BYTES:
            raise ValueError(
                f"refs must not exceed {_MAX_REF_BYTES} UTF-8 bytes"
            )
        if item in seen:
            raise ValueError("refs must be unique")
        seen.add(item)
        result.append(item)
    if canonical and result != sorted(result):
        raise ValueError("refs must be canonical sorted and unique")
    return tuple(result)


def _question_id_from_data(data: dict) -> str:
    body = {
        key: value for key, value in data.items() if key != "question_id"
    }
    return "rq_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:32]


def _prompt_id_from_data(subject_digest: str, prompt_digest: str) -> str:
    body = {
        "subject_digest": subject_digest,
        "rubric_version": _RUBRIC_VERSION,
        "rubric_hash": _RUBRIC_DIGEST,
        "prompt_digest": prompt_digest,
    }
    return "srp_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:32]


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


def _latency_ms(started: datetime, completed: datetime) -> int:
    delta = completed - started
    micros = (
        (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    return micros // 1000


class ReviewerEvidenceContext(BaseModel):
    """评审证据上下文：只读、有界、不可变。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence_id: str
    kind: str
    artifact_digest: str
    content: str
    content_digest: str
    truncated: StrictBool
    redaction_status: Literal["declared_redacted", "not_applicable"]

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _validate_evidence_id(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ID_BYTES)

    @field_validator("kind", mode="before")
    @classmethod
    def _validate_kind(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_KIND_BYTES)

    @field_validator("artifact_digest", "content_digest", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("content must be an exact str")
        if len(value.encode("utf-8")) >= _MAX_CONTENT_BYTES:
            raise ValueError(
                "content must be strictly less than 65536 UTF-8 bytes"
            )
        return value

    @model_validator(mode="after")
    def _bind_content_digest(self) -> "ReviewerEvidenceContext":
        expected = _sha256_digest(self.content.encode("utf-8"))
        if self.content_digest != expected:
            raise ValueError(
                "content_digest must equal the SHA-256 of content"
            )
        return self


class ReviewQuestion(BaseModel):
    """规范化评审问题：ID 从其余语义字段确定性派生。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    question_id: str
    reviewer_role: Literal["intent", "architecture", "operability"]
    question: str
    reason: Literal[
        "unsupported_finding_evidence", "model_question", "truncated_context"
    ]
    evidence_refs: tuple[str, ...]
    rubric_hash: str
    model_ref: str
    status: Literal["open"] = "open"

    @field_validator("subject_digest", "rubric_hash", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("question_id", mode="before")
    @classmethod
    def _validate_question_id(cls, value: object) -> str:
        if type(value) is not str or _QUESTION_ID_RE.fullmatch(value) is None:
            raise ValueError("question_id must be rq_<32 lowercase hex>")
        return value

    @field_validator("question", mode="before")
    @classmethod
    def _validate_question(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_QUESTION_BYTES)

    @field_validator("model_ref", mode="before")
    @classmethod
    def _validate_model_ref(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ID_BYTES)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("evidence_refs must be an exact tuple at raw validation")

    @field_validator("evidence_refs")
    @classmethod
    def _canonical_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_refs(value, canonical=True)

    @model_validator(mode="after")
    def _bind_question_id(self) -> "ReviewQuestion":
        expected = _question_id_from_data(self.model_dump(mode="json"))
        if self.question_id != expected:
            raise ValueError("question_id must be derived from its other fields")
        return self


class SingleReviewerInput(BaseModel):
    """单一评审者输入：主题、风险结果与只读证据上下文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject: ChangeSubject
    risk_result: RiskClassificationResult
    contexts: tuple[ReviewerEvidenceContext, ...]
    evaluated_at: AwareDatetime

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _reject_numeric_evaluated_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("contexts", mode="before")
    @classmethod
    def _exact_contexts_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("contexts must be an exact tuple at raw validation")

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("SingleReviewerInput must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("subject")) is dict:
                data["subject"] = ChangeSubject.model_validate(
                    data["subject"]
                )
            if type(data.get("risk_result")) is dict:
                data["risk_result"] = _risk_result_from_json(
                    data["risk_result"]
                )
            if type(data.get("contexts")) is list:
                data["contexts"] = tuple(
                    ReviewerEvidenceContext.model_validate(item)
                    for item in data["contexts"]
                )
            return data
        if type(data.get("subject")) is not ChangeSubject:
            raise ValueError("subject must be an exact ChangeSubject instance")
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
                    "context items must be exact ReviewerEvidenceContext instances"
                )
        return data

    @model_validator(mode="after")
    def _validate_bindings(self) -> "SingleReviewerInput":
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
        aggregate = sum(len(item.content.encode("utf-8")) for item in contexts)
        if aggregate >= _MAX_CONTEXT_CONTENT_AGGREGATE:
            raise ValueError(
                "aggregate context content must be strictly less than "
                "262144 UTF-8 bytes"
            )
        subject_digest = self.subject.subject_digest
        if subject_digest != self.risk_result.input.snapshot.subject_digest:
            raise ValueError(
                "subject digest must equal risk input subject digest"
            )
        if subject_digest != self.risk_result.classification.subject_digest:
            raise ValueError(
                "subject digest must equal risk classification subject digest"
            )
        entries = {
            entry.evidence_id: entry
            for entry in self.risk_result.input.manifest.entries
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
        if self.evaluated_at < self.subject.created_at:
            raise ValueError("evaluated_at must be >= subject.created_at")
        if self.evaluated_at < self.risk_result.input.manifest.evaluated_at:
            raise ValueError("evaluated_at must be >= manifest.evaluated_at")
        return self


class SingleReviewerInvocation(BaseModel):
    """单一评审者真实调用：measured/unavailable、无 fallback、无工具。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    run_id: str
    model_ref: str
    provider: str
    usage_status: Literal["measured", "unavailable"]
    input_tokens: StrictInt | None = None
    output_tokens: StrictInt | None = None
    cost_usd: float | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: StrictInt
    timeout_seconds: StrictInt
    result: Literal["success"] = "success"
    schema_status: Literal["valid", "repaired"]
    fallback_reason: None
    tool_grants: tuple[()]

    @field_validator("run_id", "model_ref", "provider", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ID_BYTES)

    @field_validator("input_tokens", "output_tokens", mode="before")
    @classmethod
    def _validate_usage_ints(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not int or isinstance(value, bool):
            raise ValueError("token counts must be exact ints or null")
        if value < 0:
            raise ValueError("token counts must be >= 0")
        return value

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) not in (int, float) or isinstance(value, bool):
            raise ValueError("cost_usd must be an exact JSON number or null")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("cost_usd must be finite")
        if value < 0:
            raise ValueError("cost_usd must be >= 0")
        return float(value)

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _reject_numeric_datetimes(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _validate_latency(cls, value: object) -> int:
        if type(value) is not int or isinstance(value, bool):
            raise ValueError("latency_ms must be an exact int")
        if value < 0:
            raise ValueError("latency_ms must be >= 0")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _validate_timeout(cls, value: object) -> int:
        if type(value) is not int or isinstance(value, bool):
            raise ValueError("timeout_seconds must be an exact int")
        if value <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return value

    @field_validator("fallback_reason", mode="before")
    @classmethod
    def _require_none_fallback(cls, value: object) -> None:
        if value is not None:
            raise ValueError("fallback_reason must be None")
        return None

    @field_validator("tool_grants", mode="before")
    @classmethod
    def _require_empty_tool_grants(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            if value:
                raise ValueError("tool_grants must be an exact empty tuple")
            return value
        if info.mode == "json" and type(value) is list:
            if value:
                raise ValueError("tool_grants must be an exact empty tuple")
            return ()
        raise ValueError("tool_grants must be an exact empty tuple")

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "SingleReviewerInvocation":
        if self.usage_status == "measured":
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or self.cost_usd is None
            ):
                raise ValueError(
                    "measured usage requires input_tokens, output_tokens, "
                    "and cost_usd"
                )
        elif (
            self.input_tokens is not None
            or self.output_tokens is not None
            or self.cost_usd is not None
        ):
            raise ValueError(
                "unavailable usage requires all usage fields to be None"
            )
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        expected_latency = _latency_ms(self.started_at, self.completed_at)
        if self.latency_ms != expected_latency:
            raise ValueError(
                "latency_ms must equal the truncated datetime delta"
            )
        return self


_PROMPT_HEADER = "CODEX_SAFE_SINGLE_REVIEWER_PROMPT_V1"
_NO_TOOLS_MARKER = "NO_TOOLS_OR_WRITE_GRANTS"
_SCHEMA_MARKER = "RESPONSE_JSON_SCHEMA"

_RESPONSE_SCHEMA_TEXT = """{
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "subject_digest", "rubric_hash", "findings", "questions"],
  "properties": {
    "schema_version": {"type": "string", "enum": ["v1"]},
    "subject_digest": {"type": "string"},
    "rubric_hash": {"type": "string"},
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["reviewer_role", "claim", "evidence_refs", "severity", "confidence"],
        "properties": {
          "reviewer_role": {"type": "string", "enum": ["intent", "architecture", "operability"]},
          "claim": {"type": "string"},
          "evidence_refs": {"type": "array", "items": {"type": "string"}},
          "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
          "confidence": {"type": "number"}
        }
      }
    },
    "questions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["reviewer_role", "question", "reason", "evidence_refs"],
        "properties": {
          "reviewer_role": {"type": "string", "enum": ["intent", "architecture", "operability"]},
          "question": {"type": "string"},
          "reason": {"type": "string", "enum": ["model_question", "truncated_context"]},
          "evidence_refs": {"type": "array", "items": {"type": "string"}}
        }
      }
    }
  }
}"""


def _rubric_sections_text() -> str:
    lines: list[str] = []
    for role in ("intent", "architecture", "operability"):
        lines.append(role.upper())
        for item in _RUBRIC_TABLE["roles"][role]["items"]:
            lines.append(
                f"{item['number']}. {item['code']} - {item['name']}"
            )
    return "\n".join(lines)


def _build_prompt_text(value: SingleReviewerInput) -> str:
    subject = value.subject
    risk = value.risk_result.classification
    manifest = value.risk_result.input.manifest
    parts: list[str] = [
        _PROMPT_HEADER,
        f"Subject digest: {subject.subject_digest}",
        f"Change ID: {subject.change_id}",
        f"Repository: {subject.repository}",
        f"Base revision: {subject.base_revision}",
        f"Head revision: {subject.head_revision}",
        f"Policy version: {subject.policy_version}",
        f"Task digest: {subject.task_digest}",
        f"Change created at: {subject.created_at.isoformat()}",
        "Risk facts:",
        f"Risk level: {risk.risk_level}",
        f"Reason codes: {', '.join(risk.reason_codes)}",
        f"Required reviewers: {', '.join(risk.required_reviewers)}",
        f"Required collectors: {', '.join(risk.required_collectors)}",
        f"Required human role: {risk.required_human_role}",
        f"Manifest evaluated at: {manifest.evaluated_at.isoformat()}",
        f"Manifest completeness: {manifest.completeness_status}",
        f"Rubric version: {_RUBRIC_VERSION}",
        f"Rubric hash: {_RUBRIC_DIGEST}",
        "Rubric:",
        _rubric_sections_text(),
        "Read-only evidence contexts:",
    ]
    for index, item in enumerate(value.contexts, start=1):
        parts.extend(
            [
                (
                    f"Context {index}: evidence_id={item.evidence_id} "
                    f"kind={item.kind} artifact_digest={item.artifact_digest} "
                    f"truncated={str(item.truncated).lower()} "
                    f"redaction_status={item.redaction_status}"
                ),
                "content:",
                item.content,
                "end context",
            ]
        )
    parts.extend(
        [
            "Instructions:",
            "You receive no tools. No tool grants and no write grants.",
            _NO_TOOLS_MARKER,
            "Do not guess facts that are not present in the evidence contexts.",
            "There is no hidden evidence beyond the contexts shown above.",
            (
                "Invalid or missing evidence must be recorded as a question, "
                "never as a finding."
            ),
            "Return exactly one JSON object matching the schema below.",
            _SCHEMA_MARKER,
            _RESPONSE_SCHEMA_TEXT,
            "END " + _PROMPT_HEADER,
        ]
    )
    return "\n".join(parts)


class SingleReviewerPrompt(BaseModel):
    """确定性 prompt：输入、rubric、文本、摘要与 ID 全绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: SingleReviewerInput
    rubric_version: Literal["single_general.v0"] = "single_general.v0"
    rubric_hash: str
    prompt_text: str
    prompt_digest: str
    prompt_id: str

    @field_validator("rubric_hash", "prompt_digest", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("prompt_id", mode="before")
    @classmethod
    def _validate_prompt_id(cls, value: object) -> str:
        if type(value) is not str or _PROMPT_ID_RE.fullmatch(value) is None:
            raise ValueError("prompt_id must be srp_<32 lowercase hex>")
        return value

    @field_validator("prompt_text", mode="before")
    @classmethod
    def _validate_prompt_text_bytes(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("prompt_text must be an exact str")
        if len(value.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValueError(
                f"prompt_text must not exceed {_MAX_PROMPT_BYTES} UTF-8 bytes"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_exact_input(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("SingleReviewerPrompt must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("input")) is dict:
                data["input"] = SingleReviewerInput.model_validate_json(
                    json.dumps(data["input"])
                )
            return data
        if type(data.get("input")) is not SingleReviewerInput:
            raise ValueError(
                "input must be an exact SingleReviewerInput instance"
            )
        return data

    @model_validator(mode="after")
    def _bind_prompt_fields(self) -> "SingleReviewerPrompt":
        if type(self.input) is not SingleReviewerInput:
            raise ValueError(
                "input must be an exact SingleReviewerInput instance"
            )
        if self.rubric_hash != _RUBRIC_DIGEST:
            raise ValueError(
                "rubric_hash must equal the single reviewer rubric digest"
            )
        expected_text = _build_prompt_text(self.input)
        if self.prompt_text != expected_text:
            raise ValueError("prompt_text must equal the deterministic build")
        expected_digest = _sha256_digest(self.prompt_text.encode("utf-8"))
        if self.prompt_digest != expected_digest:
            raise ValueError("prompt_digest must be recomputed")
        expected_id = _prompt_id_from_data(
            self.input.subject.subject_digest, self.prompt_digest
        )
        if self.prompt_id != expected_id:
            raise ValueError("prompt_id must be derived")
        return self


class SingleReviewerNormalizationInput(BaseModel):
    """规范化输入：评审输入、prompt、调用与精确原始响应字节。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    reviewer_input: SingleReviewerInput
    prompt: SingleReviewerPrompt
    invocation: SingleReviewerInvocation
    raw_response: bytes

    @field_validator("raw_response", mode="before")
    @classmethod
    def _require_exact_bytes(cls, value: object) -> bytes:
        if type(value) is not bytes:
            raise ValueError("raw_response must be exact bytes")
        if not value or len(value) > _MAX_RESPONSE_BYTES:
            raise ValueError(
                "raw_response must be nonempty and bounded to 1 MiB"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "SingleReviewerNormalizationInput must validate from a mapping"
            )
        expected = {
            "reviewer_input": SingleReviewerInput,
            "prompt": SingleReviewerPrompt,
            "invocation": SingleReviewerInvocation,
        }
        if info.mode == "json":
            data = dict(data)
            for name, model_type in expected.items():
                if type(data.get(name)) is dict:
                    data[name] = model_type.model_validate_json(
                        json.dumps(data[name])
                    )
            return data
        for name, model_type in expected.items():
            if type(data.get(name)) is not model_type:
                raise ValueError(
                    f"{name} must be an exact {model_type.__name__} instance"
                )
        return data

    @model_validator(mode="after")
    def _validate_bindings(self) -> "SingleReviewerNormalizationInput":
        if (
            type(self.prompt.input) is not SingleReviewerInput
            or self.prompt.input != self.reviewer_input
        ):
            raise ValueError("prompt.input must equal reviewer_input")
        if self.invocation.fallback_reason is not None:
            raise ValueError("invocation must have no fallback")
        if self.invocation.tool_grants != ():
            raise ValueError("invocation must have no tool grants")
        return self


class _FindingDraft(BaseModel):
    """响应中的 finding 草稿：严格 schema，不执行内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    claim: str
    evidence_refs: tuple[str, ...]
    severity: Literal["info", "low", "medium", "high", "critical"]
    confidence: float

    @field_validator("claim", mode="before")
    @classmethod
    def _validate_claim(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_CLAIM_BYTES)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("evidence_refs must be an exact tuple at raw validation")

    @field_validator("evidence_refs")
    @classmethod
    def _canonical_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_refs(value, canonical=False)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: object) -> float:
        if isinstance(value, bool) or type(value) not in (int, float):
            raise ValueError("confidence must be an exact JSON number")
        normalized = float(value)
        if normalized != normalized or normalized in (
            float("inf"),
            float("-inf"),
        ):
            raise ValueError("confidence must be finite")
        if not 0.0 <= normalized <= 1.0:
            raise ValueError("confidence must be within 0..1")
        return normalized


class _QuestionDraft(BaseModel):
    """响应中的 question 草稿：严格 schema，不执行内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    question: str
    reason: Literal["model_question", "truncated_context"]
    evidence_refs: tuple[str, ...]

    @field_validator("question", mode="before")
    @classmethod
    def _validate_question(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_QUESTION_BYTES)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("evidence_refs must be an exact tuple at raw validation")

    @field_validator("evidence_refs")
    @classmethod
    def _canonical_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_refs(value, canonical=False)


class _ResponseDraft(BaseModel):
    """严格模型响应顶层：extra-forbid、计数有界。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    rubric_hash: str
    findings: tuple[_FindingDraft, ...]
    questions: tuple[_QuestionDraft, ...]

    @field_validator("subject_digest", "rubric_hash", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("findings", "questions", mode="before")
    @classmethod
    def _exact_arrays(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("findings and questions must be exact tuples")

    @model_validator(mode="after")
    def _validate_counts(self) -> "_ResponseDraft":
        if len(self.findings) + len(self.questions) > _MAX_RESPONSE_ITEMS:
            raise ValueError(
                "findings and questions must not exceed 256 total items"
            )
        return self


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _scan_json_limits(text: str) -> None:
    depth = 0
    nodes = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == '"':
            nodes += 1
            if nodes > _MAX_JSON_NODES:
                raise ValueError("JSON node count exceeded")
            index += 1
            while index < length:
                current = text[index]
                if current == "\\":
                    index += 2
                    continue
                if current == '"':
                    index += 1
                    break
                index += 1
            continue
        if char in "[{":
            depth += 1
            nodes += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError("JSON depth exceeded")
            if nodes > _MAX_JSON_NODES:
                raise ValueError("JSON node count exceeded")
            index += 1
            continue
        if char in "]}":
            depth -= 1
            index += 1
            continue
        if char in "tfn-0123456789":
            nodes += 1
            if nodes > _MAX_JSON_NODES:
                raise ValueError("JSON node count exceeded")
            if char == "t":
                index += 4
            elif char == "f":
                index += 5
            elif char == "n":
                index += 4
            else:
                index += 1
                while (
                    index < length
                    and text[index] in "0123456789.eE+-"
                ):
                    index += 1
            continue
        index += 1


def _parse_strict_json(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    if text.startswith("\ufeff") or "\x00" in text:
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE)
    try:
        _scan_json_limits(text)
        raw = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, TypeError, RecursionError):
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    if type(raw) is not dict:
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE)
    return raw


def _question_from_fields(
    *,
    subject_digest: str,
    reviewer_role: str,
    question: str,
    reason: str,
    refs: tuple[str, ...],
    model_ref: str,
) -> ReviewQuestion:
    data = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "reviewer_role": reviewer_role,
        "question": question,
        "reason": reason,
        "evidence_refs": refs,
        "rubric_hash": _RUBRIC_DIGEST,
        "model_ref": model_ref,
        "status": "open",
    }
    question_id = _question_id_from_data(data)
    return ReviewQuestion.model_validate({**data, "question_id": question_id})


def _finding_from_draft(
    finding: _FindingDraft,
    refs: tuple[str, ...],
    reviewer_input: SingleReviewerInput,
    invocation: SingleReviewerInvocation,
) -> Finding:
    placeholder = Finding(
        schema_version="v1",
        finding_id="fnd_" + "0" * 32,
        subject_digest=reviewer_input.subject.subject_digest,
        reviewer_role=finding.reviewer_role,
        claim=finding.claim,
        evidence_refs=refs,
        basis="inferred",
        severity=finding.severity,
        confidence=finding.confidence,
        rubric_hash=_RUBRIC_DIGEST,
        model_ref=invocation.model_ref,
        status="open",
    )
    body = {
        key: value
        for key, value in placeholder.model_dump(mode="json").items()
        if key != "finding_id"
    }
    finding_id = "fnd_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[
        :32
    ]
    return placeholder.model_copy(update={"finding_id": finding_id})


def _dedupe_models(items: tuple) -> tuple:
    result = []
    seen = set()
    for item in items:
        key = json.dumps(
            item.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _normalize_drafts(
    draft: _ResponseDraft,
    reviewer_input: SingleReviewerInput,
    invocation: SingleReviewerInvocation,
) -> tuple[tuple[Finding, ...], tuple[ReviewQuestion, ...]]:
    valid_ids = frozenset(
        item.evidence_id for item in reviewer_input.contexts
    )
    findings: list[Finding] = []
    questions: list[ReviewQuestion] = []
    for item in draft.findings:
        refs = tuple(sorted(ref for ref in item.evidence_refs if ref in valid_ids))
        if item.evidence_refs and len(refs) == len(item.evidence_refs):
            findings.append(
                _finding_from_draft(item, refs, reviewer_input, invocation)
            )
        else:
            questions.append(
                _question_from_fields(
                    subject_digest=reviewer_input.subject.subject_digest,
                    reviewer_role=item.reviewer_role,
                    question=item.claim,
                    reason="unsupported_finding_evidence",
                    refs=refs,
                    model_ref=invocation.model_ref,
                )
            )
    for item in draft.questions:
        refs = tuple(sorted(ref for ref in item.evidence_refs if ref in valid_ids))
        questions.append(
            _question_from_fields(
                subject_digest=reviewer_input.subject.subject_digest,
                reviewer_role=item.reviewer_role,
                question=item.question,
                reason=item.reason,
                refs=refs,
                model_ref=invocation.model_ref,
            )
        )
    findings = tuple(
        sorted(
            _dedupe_models(tuple(findings)),
            key=lambda item: item.finding_id,
        )
    )
    questions = tuple(
        sorted(
            _dedupe_models(tuple(questions)),
            key=lambda item: item.question_id,
        )
    )
    return findings, questions


def _canonical_response_digest(
    reviewer_input: SingleReviewerInput,
    findings: tuple[Finding, ...],
    questions: tuple[ReviewQuestion, ...],
) -> str:
    body = {
        "schema_version": "v1",
        "subject_digest": reviewer_input.subject.subject_digest,
        "rubric_hash": _RUBRIC_DIGEST,
        "findings": [item.model_dump(mode="json") for item in findings],
        "questions": [item.model_dump(mode="json") for item in questions],
    }
    return _sha256_digest(_canonical_bytes(body))


def _parse_and_normalize_response(
    raw: bytes,
    reviewer_input: SingleReviewerInput,
    invocation: SingleReviewerInvocation,
) -> tuple[
    tuple[Finding, ...], tuple[ReviewQuestion, ...], str
]:
    payload = _parse_strict_json(raw)
    try:
        draft = _ResponseDraft.model_validate_json(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except (ValidationError, RecursionError):
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    if draft.subject_digest != reviewer_input.subject.subject_digest:
        raise SingleReviewerSubjectMismatchError(_SUBJECT_MISMATCH_MESSAGE)
    if draft.rubric_hash != _RUBRIC_DIGEST:
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE)
    try:
        findings, questions = _normalize_drafts(
            draft, reviewer_input, invocation
        )
    except (ValidationError, RecursionError, TypeError, ValueError):
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    canonical_digest = _canonical_response_digest(
        reviewer_input, findings, questions
    )
    return findings, questions, canonical_digest


def _build_execution_receipt(
    invocation: SingleReviewerInvocation,
    subject_digest: str,
) -> ExecutionReceipt:
    roles = ("intent", "architecture", "operability")
    steps = tuple(
        ExecutionStep(
            sequence=index,
            planned_role=role,
            actual_role=role,
            model_ref=invocation.model_ref,
            provider=invocation.provider,
            tool_grants=(),
            routing_rule="single_general.v0:shared_invocation",
            fallback_reason=None,
            token_budget=None,
            timeout_seconds=invocation.timeout_seconds,
            result=invocation.result,
            schema_status=invocation.schema_status,
        )
        for index, role in enumerate(roles)
    )
    if invocation.usage_status == "measured":
        input_tokens = invocation.input_tokens
        output_tokens = invocation.output_tokens
        cost_usd = invocation.cost_usd
    else:
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
    placeholder = ExecutionReceipt(
        schema_version="v1",
        receipt_id="exr_" + "0" * 32,
        run_id=invocation.run_id,
        subject_digest=subject_digest,
        steps=steps,
        overall_result="success",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        started_at=invocation.started_at,
        completed_at=invocation.completed_at,
    )
    body = {
        key: value
        for key, value in placeholder.model_dump(mode="json").items()
        if key != "receipt_id"
    }
    receipt_id = "exr_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[
        :32
    ]
    return placeholder.model_copy(update={"receipt_id": receipt_id})


class SingleReviewerResult(BaseModel):
    """单一评审者完整结果：所有字段从嵌套输入重算并防伪绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: SingleReviewerNormalizationInput
    raw_response_artifact_digest: str
    canonical_response_digest: str
    findings: tuple[Finding, ...]
    questions: tuple[ReviewQuestion, ...]
    execution_receipt: ExecutionReceipt
    result_digest: str
    result_id: str

    @field_validator(
        "raw_response_artifact_digest",
        "canonical_response_digest",
        "result_digest",
        mode="before",
    )
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("result_id", mode="before")
    @classmethod
    def _validate_result_id(cls, value: object) -> str:
        if type(value) is not str or _RESULT_ID_RE.fullmatch(value) is None:
            raise ValueError("result_id must be srr_<32 lowercase hex>")
        return value

    @field_validator("findings", "questions", mode="before")
    @classmethod
    def _exact_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("must be an exact tuple at raw validation")

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("SingleReviewerResult must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("input")) is dict:
                data["input"] = SingleReviewerNormalizationInput.model_validate_json(
                    json.dumps(data["input"])
                )
            if type(data.get("findings")) is list:
                data["findings"] = tuple(
                    Finding.model_validate(item)
                    for item in data["findings"]
                )
            if type(data.get("questions")) is list:
                data["questions"] = tuple(
                    ReviewQuestion.model_validate(item)
                    for item in data["questions"]
                )
            if type(data.get("execution_receipt")) is dict:
                data["execution_receipt"] = ExecutionReceipt.model_validate(
                    data["execution_receipt"]
                )
            return data
        if type(data.get("input")) is not SingleReviewerNormalizationInput:
            raise ValueError(
                "input must be an exact "
                "SingleReviewerNormalizationInput instance"
            )
        if type(data.get("findings")) is not tuple:
            raise ValueError("findings must be an exact tuple at raw validation")
        for item in data.get("findings", ()):
            if type(item) is not Finding:
                raise ValueError("findings items must be exact Finding instances")
        if type(data.get("questions")) is not tuple:
            raise ValueError(
                "questions must be an exact tuple at raw validation"
            )
        for item in data.get("questions", ()):
            if type(item) is not ReviewQuestion:
                raise ValueError(
                    "questions items must be exact ReviewQuestion instances"
                )
        if type(data.get("execution_receipt")) is not ExecutionReceipt:
            raise ValueError(
                "execution_receipt must be an exact ExecutionReceipt instance"
            )
        return data

    @model_validator(mode="after")
    def _require_recomputed_bindings(self) -> "SingleReviewerResult":
        reviewer_input = self.input.reviewer_input
        invocation = self.input.invocation
        raw_digest = _sha256_digest(self.input.raw_response)
        if self.raw_response_artifact_digest != raw_digest:
            raise ValueError("raw_response_artifact_digest must be recomputed")
        findings, questions, canonical_digest = _parse_and_normalize_response(
            self.input.raw_response, reviewer_input, invocation
        )
        if self.findings != findings or self.questions != questions:
            raise ValueError(
                "findings and questions must equal the canonical normalization"
            )
        if self.canonical_response_digest != canonical_digest:
            raise ValueError("canonical_response_digest must be recomputed")
        receipt = _build_execution_receipt(
            invocation, reviewer_input.subject.subject_digest
        )
        if self.execution_receipt != receipt:
            raise ValueError(
                "execution_receipt must be derived from the exact invocation"
            )
        body = _result_digest_body(self)
        if self.result_digest != _sha256_digest(_canonical_bytes(body)):
            raise ValueError("result_digest must be recomputed")
        if self.result_id != _result_id_from_body(body):
            raise ValueError("result_id must be derived")
        return self


def _result_digest_body(result: SingleReviewerResult) -> dict:
    return {
        "schema_version": "v1",
        "reviewer_input": result.input.reviewer_input.model_dump(mode="json"),
        "prompt": result.input.prompt.model_dump(mode="json"),
        "invocation": result.input.invocation.model_dump(mode="json"),
        "raw_response_digest": result.raw_response_artifact_digest,
        "canonical_response_digest": result.canonical_response_digest,
        "findings": [
            item.model_dump(mode="json") for item in result.findings
        ],
        "questions": [
            item.model_dump(mode="json") for item in result.questions
        ],
        "execution_receipt": result.execution_receipt.model_dump(mode="json"),
    }


def _result_id_from_body(body: dict) -> str:
    return "srr_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:32]


def _assemble_result(
    normalization_input: SingleReviewerNormalizationInput,
    raw_digest: str,
    canonical_digest: str,
    findings: tuple[Finding, ...],
    questions: tuple[ReviewQuestion, ...],
    receipt: ExecutionReceipt,
) -> SingleReviewerResult:
    dummy = SingleReviewerResult.model_construct(
        schema_version="v1",
        input=normalization_input,
        raw_response_artifact_digest=raw_digest,
        canonical_response_digest=canonical_digest,
        findings=findings,
        questions=questions,
        execution_receipt=receipt,
        result_digest="sha256:" + "0" * 64,
        result_id="srr_" + "0" * 32,
    )
    body = _result_digest_body(dummy)
    result_digest = _sha256_digest(_canonical_bytes(body))
    result_id = _result_id_from_body(body)
    data = {
        "schema_version": "v1",
        "input": normalization_input,
        "raw_response_artifact_digest": raw_digest,
        "canonical_response_digest": canonical_digest,
        "findings": findings,
        "questions": questions,
        "execution_receipt": receipt,
        "result_digest": result_digest,
        "result_id": result_id,
    }
    try:
        return SingleReviewerResult.model_validate(data)
    except (ValidationError, RecursionError):
        raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE) from None


class SingleReviewerError(Exception):
    """单一强评审者处理失败。"""


class SingleReviewerPayloadError(SingleReviewerError):
    """原始 payload 字节、编码、JSON 或 schema 非法。"""


class SingleReviewerSubjectMismatchError(SingleReviewerError):
    """响应 subject digest 与输入 subject 不一致。"""


class SingleReviewerArtifactError(SingleReviewerError):
    """原始响应工件持久化失败。"""


class SingleStrongReviewer:
    """单一强评审者纯确定性门面：prepare 与 normalize。"""

    @staticmethod
    def prepare(value: SingleReviewerInput) -> SingleReviewerPrompt:
        if type(value) is not SingleReviewerInput:
            raise TypeError("value must be an exact SingleReviewerInput")
        text = _build_prompt_text(value)
        prompt_digest = _sha256_digest(text.encode("utf-8"))
        prompt_id = _prompt_id_from_data(
            value.subject.subject_digest, prompt_digest
        )
        return SingleReviewerPrompt(
            schema_version="v1",
            input=value,
            rubric_version=_RUBRIC_VERSION,
            rubric_hash=_RUBRIC_DIGEST,
            prompt_text=text,
            prompt_digest=prompt_digest,
            prompt_id=prompt_id,
        )

    @staticmethod
    def normalize(
        value: SingleReviewerNormalizationInput,
        artifact_store: ArtifactStore,
    ) -> SingleReviewerResult:
        if type(value) is not SingleReviewerNormalizationInput:
            raise TypeError(
                "value must be an exact SingleReviewerNormalizationInput"
            )
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        raw = value.raw_response
        if (
            type(raw) is not bytes
            or not raw
            or len(raw) > _MAX_RESPONSE_BYTES
        ):
            raise SingleReviewerPayloadError(_PAYLOAD_ERROR_MESSAGE)
        raw_digest = _sha256_digest(raw)
        try:
            stored_digest = artifact_store.put_bytes(raw)
        except Exception:
            raise SingleReviewerArtifactError(_ARTIFACT_ERROR_MESSAGE) from None
        if stored_digest != raw_digest:
            raise SingleReviewerArtifactError(
                _ARTIFACT_ERROR_MESSAGE
            ) from None
        try:
            verified = artifact_store.verify(raw_digest)
            stored = artifact_store.get_bytes(raw_digest)
        except Exception:
            raise SingleReviewerArtifactError(
                _ARTIFACT_ERROR_MESSAGE
            ) from None
        if verified is not True or type(stored) is not bytes or stored != raw:
            raise SingleReviewerArtifactError(
                _ARTIFACT_ERROR_MESSAGE
            ) from None
        findings, questions, canonical_digest = _parse_and_normalize_response(
            raw, value.reviewer_input, value.invocation
        )
        receipt = _build_execution_receipt(
            value.invocation, value.reviewer_input.subject.subject_digest
        )
        return _assemble_result(
            value,
            raw_digest,
            canonical_digest,
            findings,
            questions,
            receipt,
        )


__all__ = (
    "ReviewerEvidenceContext",
    "ReviewQuestion",
    "SingleReviewerInput",
    "SingleReviewerInvocation",
    "SingleReviewerPrompt",
    "SingleReviewerNormalizationInput",
    "SingleReviewerResult",
    "SingleStrongReviewer",
    "SingleReviewerError",
    "SingleReviewerPayloadError",
    "SingleReviewerSubjectMismatchError",
    "SingleReviewerArtifactError",
)
