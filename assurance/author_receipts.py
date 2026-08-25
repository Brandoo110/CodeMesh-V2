"""作者代理收据规范化（V2-P2-05）。

信任边界：只接收调用方提供的精确原始 JSON 字节，把它当作作者代理自己
声明的收据。本模块不读取 SQLite/文件/路径/网络/API/日志/仓库/Git 或环境
数据；不执行 payload、工具、命令、检查或结果内容；不调用模型、网络、Git、
子进程、shell、eval 或 exec；不验证作者断言；不声称已脱敏、秘密安全、
确定性执行、签名有效、观察到行为或满足策略。原始内容（V1 final_reply、
step output/error、工具参数/结果、文件 before/after）永不进入规范化模型
或 Evidence；只保留有界的显式通用声明与固定的非内容 V1 存在性摘要。
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence
from .digests import normalize_repo_path


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_RECEIPT_ID_RE = re.compile(r"^ar_[0-9a-f]{32}$")

_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_ITEM_BYTES = 256
_MAX_CLAIM_BYTES = 4096
_MAX_TUPLE_ITEMS = 64
_MAX_STEPS = 256
_MAX_TOOL_CALLS = 128
_MAX_FILE_DIFFS = 256
_COST_EPSILON = 1e-9

_PAYLOAD_ERROR_MESSAGE = "invalid author agent receipt payload"
_SUBJECT_ARGUMENT_ERROR_MESSAGE = "invalid expected subject digest"
_SUBJECT_MISMATCH_MESSAGE = "author agent receipt subject digest mismatch"
_ARTIFACT_ERROR_MESSAGE = "author agent receipt artifact persistence failed"

_MISSING_FIELD_ORDER = (
    "source_subject_digest",
    "session_id",
    "provider_refs",
    "model_refs",
    "tool_names",
    "files_touched",
    "command_claims",
    "check_claims",
    "declared_intent",
    "declared_completion",
    "input_tokens",
    "output_tokens",
    "cost",
)
_MISSING_FIELD_SET = frozenset(_MISSING_FIELD_ORDER)
_V1_REQUIRED_ABSENT_FIELDS = (
    "session_id",
    "provider_refs",
    "command_claims",
    "check_claims",
    "declared_intent",
    "input_tokens",
    "output_tokens",
)


class AuthorAgentReceiptError(Exception):
    """作者代理收据规范化失败。"""


class AuthorAgentReceiptPayloadError(AuthorAgentReceiptError):
    """原始 payload 字节、编码、JSON 或 schema 非法。"""


class AuthorAgentReceiptSubjectMismatch(AuthorAgentReceiptError):
    """收据 subject digest 与预期 subject digest 不一致。"""


class AuthorAgentReceiptArtifactError(AuthorAgentReceiptError):
    """原始收据工件持久化失败。"""


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_text(value: object, *, max_bytes: int) -> str:
    if type(value) is not str:
        raise ValueError("must be a str")
    if not value.strip():
        raise ValueError("must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"must not exceed {max_bytes} UTF-8 bytes")
    return value


def _validate_declared_path(value: object) -> str:
    if type(value) is not str:
        raise ValueError("path must be a str")
    if not value.strip():
        raise ValueError("path must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError("path must not contain NUL")
    if value.startswith("~"):
        raise ValueError("home-relative paths are not allowed")
    try:
        normalized = normalize_repo_path(value)
    except (TypeError, ValueError):
        raise ValueError("path must be canonical repo-relative") from None
    if normalized != value:
        raise ValueError("path must be canonical repo-relative")
    if len(value.encode("utf-8")) > _MAX_ITEM_BYTES:
        raise ValueError(f"path must not exceed {_MAX_ITEM_BYTES} UTF-8 bytes")
    return value


def _validate_string_tuple(
    value: object, *, item_max_bytes: int = _MAX_ITEM_BYTES, path_items: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("must be a tuple or list")
    if len(value) > _MAX_TUPLE_ITEMS:
        raise ValueError(f"must contain at most {_MAX_TUPLE_ITEMS} items")
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        item = _validate_declared_path(item) if path_items else _validate_text(
            item, max_bytes=item_max_bytes
        )
        if item in seen:
            raise ValueError("items must be unique")
        seen.add(item)
        items.append(item)
    return tuple(items)


def _validate_cost_number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("cost must be a JSON int or float")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("cost must be finite")
    if value < 0:
        raise ValueError("cost must be >= 0")
    return float(value)


def _validate_aware_datetime(value: object) -> object:
    if type(value) is str:
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value
    raise ValueError("datetime must be an ISO-8601 string or aware datetime")


def _require_json_value(value: object) -> object:
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, list):
        return [_require_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_require_json_value(item) for item in value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _require_json_value(item)
        return result
    raise ValueError("value must be JSON-compatible")


def _validate_sha256_digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


class AuthorAgentReceiptCost(BaseModel):
    """作者代理收据的声明性成本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    amount: float = Field(ge=0.0, allow_inf_nan=False)
    currency: str

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> float:
        return _validate_cost_number(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        if _CURRENCY_RE.fullmatch(value) is None:
            raise ValueError("currency must be an uppercase [A-Z]{3} code")
        return value


class GenericAuthorReceiptEnvelope(BaseModel):
    """严格版本化通用作者代理收据 v1 输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    run_id: str
    session_id: str | None = None
    provider_refs: tuple[str, ...] = ()
    model_refs: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    files_touched: tuple[str, ...] = ()
    command_claims: tuple[str, ...] = ()
    check_claims: tuple[str, ...] = ()
    declared_intent: str | None = None
    declared_completion: str | None = None
    completion_status: Literal[
        "success", "failure", "cancelled", "truncated"
    ]
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost: AuthorAgentReceiptCost | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @field_validator("subject_digest", mode="before")
    @classmethod
    def _validate_subject_digest(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("run_id", mode="before")
    @classmethod
    def _validate_run_id(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("session_id", mode="before")
    @classmethod
    def _validate_session_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("provider_refs", "model_refs", "tool_names", mode="before")
    @classmethod
    def _validate_ref_tuples(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(value)

    @field_validator("files_touched", mode="before")
    @classmethod
    def _validate_files_touched(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(value, path_items=True)

    @field_validator("command_claims", "check_claims", mode="before")
    @classmethod
    def _validate_claim_tuples(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(
            value, item_max_bytes=_MAX_CLAIM_BYTES
        )

    @field_validator("declared_intent", "declared_completion", mode="before")
    @classmethod
    def _validate_declared_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_text(value, max_bytes=_MAX_CLAIM_BYTES)

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _validate_datetime(cls, value: object) -> object:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "GenericAuthorReceiptEnvelope":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be >= started_at")
        return self


class AuthorAgentReceipt(BaseModel):
    """规范化后的作者代理收据（immutable，稳定 JSON）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    receipt_id: str
    source_kind: Literal["codemesh_v1", "generic"]
    source_schema: Literal[
        "codemesh_v1.RunDetail", "generic_author_receipt.v1"
    ]
    run_id: str
    session_id: str | None
    subject_digest: str
    provider_refs: tuple[str, ...]
    model_refs: tuple[str, ...]
    tool_names: tuple[str, ...]
    files_touched: tuple[str, ...]
    command_claims: tuple[str, ...]
    check_claims: tuple[str, ...]
    declared_intent: str | None
    declared_completion: str | None
    completion_status: Literal[
        "success", "failure", "cancelled", "truncated"
    ]
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost: AuthorAgentReceiptCost | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime
    missing_fields: tuple[Literal[_MISSING_FIELD_ORDER[0], *_MISSING_FIELD_ORDER[1:]], ...]
    raw_artifact_digest: str
    canonical_digest: str
    trust_level: Literal["declared"] = "declared"

    @field_validator("receipt_id")
    @classmethod
    def _validate_receipt_id(cls, value: str) -> str:
        if _RECEIPT_ID_RE.fullmatch(value) is None:
            raise ValueError("receipt_id must be ar_<32 lowercase hex>")
        return value

    @field_validator("run_id", mode="before")
    @classmethod
    def _validate_run_id(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("session_id", mode="before")
    @classmethod
    def _validate_session_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("subject_digest", "raw_artifact_digest", "canonical_digest", mode="before")
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("provider_refs", "model_refs", "tool_names", mode="before")
    @classmethod
    def _validate_ref_tuples(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(value)

    @field_validator("files_touched", mode="before")
    @classmethod
    def _validate_files_touched(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(value, path_items=True)

    @field_validator("command_claims", "check_claims", mode="before")
    @classmethod
    def _validate_claim_tuples(cls, value: object) -> tuple[str, ...]:
        return _validate_string_tuple(
            value, item_max_bytes=_MAX_CLAIM_BYTES
        )

    @field_validator("declared_intent", "declared_completion", mode="before")
    @classmethod
    def _validate_declared_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_text(value, max_bytes=_MAX_CLAIM_BYTES)

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _validate_datetime(cls, value: object) -> object:
        return _validate_aware_datetime(value)

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _validate_missing_fields(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("missing_fields must be a tuple or list")
        items: list[str] = []
        for item in value:
            if type(item) is not str or item not in _MISSING_FIELD_SET:
                raise ValueError(
                    "missing_fields must contain only fixed literal values"
                )
            items.append(item)
        if len(set(items)) != len(items):
            raise ValueError("missing_fields must be unique")
        ordered = tuple(
            name for name in _MISSING_FIELD_ORDER if name in set(items)
        )
        if ordered != tuple(items):
            raise ValueError("missing_fields must be in the fixed order")
        return tuple(items)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "AuthorAgentReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be >= started_at")
        return self


class AuthorAgentReceiptResult(BaseModel):
    """收据及其确定性 Evidence 的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    receipt: AuthorAgentReceipt
    evidence: Evidence

    @model_validator(mode="after")
    def _validate_cross_bindings(self) -> "AuthorAgentReceiptResult":
        receipt = self.receipt
        evidence = self.evidence
        _validate_v1_source_absences(receipt)
        expected_missing = _compute_missing_fields(receipt)
        if receipt.missing_fields != expected_missing:
            raise ValueError(
                "missing_fields must match normalized receipt facts"
            )
        if receipt.canonical_digest != _canonical_digest(receipt):
            raise ValueError(
                "canonical_digest must equal the sha256 of the canonical body"
            )
        if receipt.receipt_id != _receipt_id(receipt):
            raise ValueError(
                "receipt_id must be derived from source/subject/digests"
            )
        expected_schema = _source_schema_for(receipt.source_kind)
        if receipt.source_schema != expected_schema:
            raise ValueError("source_schema must match source_kind")
        if evidence.subject_digest != receipt.subject_digest:
            raise ValueError("evidence.subject_digest must equal receipt subject")
        if evidence.kind != "author_agent_receipt":
            raise ValueError("evidence.kind must be author_agent_receipt")
        if evidence.producer != _producer_for(receipt.source_kind):
            raise ValueError("evidence.producer must match receipt source_kind")
        if evidence.artifact_digest != receipt.raw_artifact_digest:
            raise ValueError(
                "evidence.artifact_digest must equal receipt raw digest"
            )
        expected_source_ref = (
            f"author_agent_receipt:{receipt.raw_artifact_digest}"
        )
        if evidence.source_ref != expected_source_ref:
            raise ValueError("evidence.source_ref must match receipt raw digest")
        if evidence.trust_level != "declared":
            raise ValueError("evidence.trust_level must be declared")
        if evidence.trace_id is not None:
            raise ValueError("evidence.trace_id must be None")
        if evidence.collected_at != receipt.completed_at:
            raise ValueError(
                "evidence.collected_at must equal receipt.completed_at"
            )
        expected_status = _evidence_status(receipt)
        if evidence.status != expected_status:
            raise ValueError("evidence.status must follow the frozen mapping")
        if evidence.evidence_id != _evidence_id(receipt):
            raise ValueError("evidence.evidence_id must be derived from receipt")
        return self


class _CodeMeshV1ToolCall(BaseModel):
    """V1 工具调用项的严格子集（永不执行内容）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    args: object | None = None
    result: object | None = None
    status: Literal["pending", "ok", "error"] | None = None
    ok: bool | None = Field(default=None, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("args", "result", mode="before")
    @classmethod
    def _validate_json_value(cls, value: object) -> object:
        return _require_json_value(value)


class _CodeMeshV1FileDiff(BaseModel):
    """V1 文件 diff 项的严格子集（内容永不输出）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    before: str | None = None
    after: str | None = None
    kind: Literal["created", "modified", "deleted"]
    truncated: bool | None = Field(default=None, strict=True)

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str:
        return _validate_declared_path(value)

    @field_validator("before", "after", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("before/after must be a str or null")
        return value


class _CodeMeshV1Step(BaseModel):
    """V1 StepResultInfo 的严格收据子集。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(strict=True, gt=0)
    run_id: str
    step_id: str
    step_order: int = Field(strict=True, gt=0)
    status: Literal["done", "error", "cancelled"]
    output: str | None = None
    error: str | None = None
    tool_calls: tuple[_CodeMeshV1ToolCall, ...] = ()
    file_diffs: tuple[_CodeMeshV1FileDiff, ...] = ()
    model_used: str | None = None
    cost_rmb: float | None = None
    duration_ms: int | None = Field(default=None, strict=True, ge=0)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @field_validator("run_id", "step_id", mode="before")
    @classmethod
    def _validate_ids(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("output", "error", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("must be a str or null")
        return value

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _validate_tool_calls(cls, value: object) -> tuple[object, ...]:
        if value is None:
            return ()
        if not isinstance(value, (tuple, list)):
            raise ValueError("tool_calls must be a tuple/list or null")
        if len(value) > _MAX_TOOL_CALLS:
            raise ValueError(
                f"tool_calls must contain at most {_MAX_TOOL_CALLS} items"
            )
        return tuple(value)

    @field_validator("file_diffs", mode="before")
    @classmethod
    def _validate_file_diffs(cls, value: object) -> tuple[object, ...]:
        if value is None:
            return ()
        if not isinstance(value, (tuple, list)):
            raise ValueError("file_diffs must be a tuple/list or null")
        if len(value) > _MAX_FILE_DIFFS:
            raise ValueError(
                f"file_diffs must contain at most {_MAX_FILE_DIFFS} items"
            )
        return tuple(value)

    @field_validator("model_used", mode="before")
    @classmethod
    def _validate_model_used(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("cost_rmb", mode="before")
    @classmethod
    def _validate_cost_rmb(cls, value: object) -> float | None:
        if value is None:
            return None
        return _validate_cost_number(value)

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _validate_datetime(cls, value: object) -> object:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def _validate_times(self) -> "_CodeMeshV1Step":
        if self.completed_at < self.started_at:
            raise ValueError("step completed_at must be >= started_at")
        return self


class _CodeMeshV1RunDetail(BaseModel):
    """V1 web.schemas.RunDetail 的严格收据子集。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    workflow_id: str
    status: Literal["done", "error", "cancelled"]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    total_cost_rmb: float | None = None
    error: str | None = None
    final_reply: str | None = None
    step_results: tuple[_CodeMeshV1Step, ...] = ()

    @field_validator("id", "workflow_id", mode="before")
    @classmethod
    def _validate_ids(cls, value: object) -> str:
        return _validate_text(value, max_bytes=_MAX_ITEM_BYTES)

    @field_validator("error", "final_reply", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("must be a str or null")
        return value

    @field_validator("total_cost_rmb", mode="before")
    @classmethod
    def _validate_total_cost(cls, value: object) -> float | None:
        if value is None:
            return None
        return _validate_cost_number(value)

    @field_validator("step_results", mode="before")
    @classmethod
    def _validate_step_results(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("step_results must be a tuple or list")
        if len(value) > _MAX_STEPS:
            raise ValueError(
                f"step_results must contain at most {_MAX_STEPS} items"
            )
        return tuple(value)

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _validate_datetime(cls, value: object) -> object:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "_CodeMeshV1RunDetail":
        if self.completed_at < self.started_at:
            raise ValueError("run completed_at must be >= started_at")
        step_ids = [step.id for step in self.step_results]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")
        step_identifiers = [step.step_id for step in self.step_results]
        if len(set(step_identifiers)) != len(step_identifiers):
            raise ValueError("step step_id values must be unique")
        orders = [step.step_order for step in self.step_results]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("step_order must be exactly 1..N in input order")
        for step in self.step_results:
            if step.run_id != self.id:
                raise ValueError("step run_id must equal run id")
            if (
                step.started_at < self.started_at
                or step.completed_at > self.completed_at
            ):
                raise ValueError(
                    "step interval must fall within the run interval"
                )
        if self.status == "done" and any(
            step.status != "done" for step in self.step_results
        ):
            raise ValueError(
                "done runs cannot contain error or cancelled steps"
            )
        if self.status == "error":
            has_run_error = isinstance(self.error, str) and bool(
                self.error.strip()
            )
            if not has_run_error and not any(
                step.status == "error" for step in self.step_results
            ):
                raise ValueError(
                    "error runs require a nonblank run error or an error step"
                )
        present_step_costs = [
            step.cost_rmb
            for step in self.step_results
            if step.cost_rmb is not None
        ]
        if present_step_costs:
            if self.total_cost_rmb is None:
                raise ValueError(
                    "run total cost is required when step costs are present"
                )
            if sum(present_step_costs) > self.total_cost_rmb + _COST_EPSILON:
                raise ValueError(
                    "step costs must not exceed run total cost"
                )
        return self


def _parse_strict_json(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    if text.startswith("\ufeff") or "\x00" in text:
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE)
    try:
        raw = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, TypeError, RecursionError):
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE) from None
    if type(raw) is not dict:
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE)
    return raw


def _validate_common(
    payload: object,
    expected_subject_digest: object,
    artifact_store: object,
) -> None:
    if type(payload) is not bytes:
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE)
    if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise AuthorAgentReceiptPayloadError(_PAYLOAD_ERROR_MESSAGE)
    if (
        type(expected_subject_digest) is not str
        or _SHA256_DIGEST_RE.fullmatch(expected_subject_digest) is None
    ):
        raise AuthorAgentReceiptPayloadError(_SUBJECT_ARGUMENT_ERROR_MESSAGE)
    if type(artifact_store) is not ArtifactStore:
        raise TypeError("artifact_store must be an exact ArtifactStore")


def _source_schema_for(source_kind: str) -> str:
    if source_kind == "codemesh_v1":
        return "codemesh_v1.RunDetail"
    return "generic_author_receipt.v1"


def _producer_for(source_kind: str) -> str:
    if source_kind == "codemesh_v1":
        return "normalizer.author_agent.codemesh_v1"
    return "normalizer.author_agent.generic"


def _compute_missing_fields_for(
    source_kind: str, facts: dict[str, object]
) -> tuple[str, ...]:
    missing: list[str] = []
    for name in _MISSING_FIELD_ORDER:
        if name == "source_subject_digest":
            if source_kind == "codemesh_v1":
                missing.append(name)
            continue
        value = facts[name]
        if value is None or value == ():
            missing.append(name)
    return tuple(missing)


def _compute_missing_fields(receipt: AuthorAgentReceipt) -> tuple[str, ...]:
    missing: list[str] = []
    for name in _MISSING_FIELD_ORDER:
        if name == "source_subject_digest":
            if receipt.source_kind == "codemesh_v1":
                missing.append(name)
            continue
        value = getattr(receipt, name)
        if value is None or value == ():
            missing.append(name)
    return tuple(missing)


def _validate_v1_source_absences(receipt: AuthorAgentReceipt) -> None:
    if receipt.source_kind != "codemesh_v1":
        return
    for name in _V1_REQUIRED_ABSENT_FIELDS:
        value = getattr(receipt, name)
        if value is not None and value != ():
            raise ValueError(
                f"{name} must be absent for codemesh_v1 receipts"
            )


def _canonical_receipt_body(receipt: AuthorAgentReceipt) -> bytes:
    data = receipt.model_dump(mode="json")
    data.pop("receipt_id")
    data.pop("canonical_digest")
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_digest(receipt: AuthorAgentReceipt) -> str:
    return _sha256_digest(_canonical_receipt_body(receipt))


def _receipt_id_from_parts(
    source_kind: str, subject_digest: str, raw_digest: str, canonical_digest: str
) -> str:
    value = (
        source_kind + subject_digest + raw_digest + canonical_digest
    ).encode("ascii")
    return "ar_" + hashlib.sha256(value).hexdigest()[:32]


def _receipt_id(receipt: AuthorAgentReceipt) -> str:
    return _receipt_id_from_parts(
        receipt.source_kind,
        receipt.subject_digest,
        receipt.raw_artifact_digest,
        receipt.canonical_digest,
    )


def _evidence_id(receipt: AuthorAgentReceipt) -> str:
    value = (
        receipt.receipt_id
        + receipt.raw_artifact_digest
        + receipt.canonical_digest
    ).encode("ascii")
    return "ev_author_" + hashlib.sha256(value).hexdigest()[:32]


def _evidence_status(receipt: AuthorAgentReceipt) -> str:
    if receipt.completion_status == "failure":
        return "failure"
    if receipt.completion_status == "cancelled":
        return "cancelled"
    if receipt.completion_status == "truncated":
        return "truncated"
    if receipt.missing_fields:
        return "truncated"
    return "success"


def _normalize_generic_facts(
    envelope: GenericAuthorReceiptEnvelope,
) -> dict[str, object]:
    return {
        "run_id": envelope.run_id,
        "session_id": envelope.session_id,
        "provider_refs": envelope.provider_refs,
        "model_refs": envelope.model_refs,
        "tool_names": envelope.tool_names,
        "files_touched": envelope.files_touched,
        "command_claims": envelope.command_claims,
        "check_claims": envelope.check_claims,
        "declared_intent": envelope.declared_intent,
        "declared_completion": envelope.declared_completion,
        "completion_status": envelope.completion_status,
        "input_tokens": envelope.input_tokens,
        "output_tokens": envelope.output_tokens,
        "cost": envelope.cost,
        "started_at": envelope.started_at,
        "completed_at": envelope.completed_at,
    }


def _normalize_v1_facts(run: _CodeMeshV1RunDetail) -> dict[str, object]:
    model_refs: list[str] = []
    seen_models: set[str] = set()
    tool_names: list[str] = []
    seen_tools: set[str] = set()
    files_touched: list[str] = []
    seen_files: set[str] = set()
    for step in run.step_results:
        if step.model_used is not None and step.model_used not in seen_models:
            seen_models.add(step.model_used)
            model_refs.append(step.model_used)
        for call in step.tool_calls:
            if call.name not in seen_tools:
                seen_tools.add(call.name)
                tool_names.append(call.name)
        for diff in step.file_diffs:
            if diff.path not in seen_files:
                seen_files.add(diff.path)
                files_touched.append(diff.path)
    has_final_reply = isinstance(run.final_reply, str) and bool(
        run.final_reply.strip()
    )
    return {
        "run_id": run.id,
        "session_id": None,
        "provider_refs": (),
        "model_refs": tuple(model_refs),
        "tool_names": tuple(tool_names),
        "files_touched": tuple(files_touched),
        "command_claims": (),
        "check_claims": (),
        "declared_intent": None,
        "declared_completion": (
            "provided_by_author_agent" if has_final_reply else None
        ),
        "completion_status": {
            "done": "success",
            "error": "failure",
            "cancelled": "cancelled",
        }[run.status],
        "input_tokens": None,
        "output_tokens": None,
        "cost": (
            AuthorAgentReceiptCost(
                schema_version="v1",
                amount=run.total_cost_rmb,
                currency="CNY",
            )
            if run.total_cost_rmb is not None
            else None
        ),
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _assemble_result(
    source_kind: str,
    subject_digest: str,
    raw_digest: str,
    facts: dict[str, object],
) -> AuthorAgentReceiptResult:
    missing_fields = _compute_missing_fields_for(source_kind, facts)
    placeholder = AuthorAgentReceipt(
        schema_version="v1",
        receipt_id="ar_" + "0" * 32,
        source_kind=source_kind,
        source_schema=_source_schema_for(source_kind),
        run_id=facts["run_id"],
        session_id=facts["session_id"],
        subject_digest=subject_digest,
        provider_refs=facts["provider_refs"],
        model_refs=facts["model_refs"],
        tool_names=facts["tool_names"],
        files_touched=facts["files_touched"],
        command_claims=facts["command_claims"],
        check_claims=facts["check_claims"],
        declared_intent=facts["declared_intent"],
        declared_completion=facts["declared_completion"],
        completion_status=facts["completion_status"],
        input_tokens=facts["input_tokens"],
        output_tokens=facts["output_tokens"],
        cost=facts["cost"],
        started_at=facts["started_at"],
        completed_at=facts["completed_at"],
        missing_fields=missing_fields,
        raw_artifact_digest=raw_digest,
        canonical_digest="sha256:" + "0" * 64,
        trust_level="declared",
    )
    canonical_digest = _canonical_digest(placeholder)
    receipt = placeholder.model_copy(
        update={
            "canonical_digest": canonical_digest,
            "receipt_id": _receipt_id_from_parts(
                source_kind,
                subject_digest,
                raw_digest,
                canonical_digest,
            ),
        }
    )
    evidence = Evidence(
        evidence_id=_evidence_id(receipt),
        subject_digest=receipt.subject_digest,
        kind="author_agent_receipt",
        producer=_producer_for(source_kind),
        artifact_digest=receipt.raw_artifact_digest,
        source_ref=f"author_agent_receipt:{receipt.raw_artifact_digest}",
        trace_id=None,
        status=_evidence_status(receipt),
        trust_level="declared",
        collected_at=receipt.completed_at,
    )
    return AuthorAgentReceiptResult(
        schema_version="v1",
        receipt=receipt,
        evidence=evidence,
    )


def _normalize(
    payload: bytes,
    expected_subject_digest: str,
    artifact_store: ArtifactStore,
    *,
    source_kind: str,
) -> AuthorAgentReceiptResult:
    _validate_common(payload, expected_subject_digest, artifact_store)
    raw = _parse_strict_json(payload)
    if source_kind == "generic":
        try:
            envelope = GenericAuthorReceiptEnvelope.model_validate(raw)
        except (ValidationError, RecursionError):
            raise AuthorAgentReceiptPayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None
        if envelope.subject_digest != expected_subject_digest:
            raise AuthorAgentReceiptSubjectMismatch(_SUBJECT_MISMATCH_MESSAGE)
        facts = _normalize_generic_facts(envelope)
    else:
        try:
            run = _CodeMeshV1RunDetail.model_validate(raw)
        except (ValidationError, RecursionError):
            raise AuthorAgentReceiptPayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None
        facts = _normalize_v1_facts(run)
    raw_digest = _sha256_digest(payload)
    try:
        result = _assemble_result(
            source_kind, expected_subject_digest, raw_digest, facts
        )
    except Exception:
        raise AuthorAgentReceiptPayloadError(
            _PAYLOAD_ERROR_MESSAGE
        ) from None
    try:
        stored_digest = artifact_store.put_bytes(payload)
        if stored_digest != raw_digest:
            raise AuthorAgentReceiptArtifactError(_ARTIFACT_ERROR_MESSAGE)
        if not artifact_store.verify(raw_digest):
            raise AuthorAgentReceiptArtifactError(_ARTIFACT_ERROR_MESSAGE)
        stored = artifact_store.get_bytes(raw_digest)
    except AuthorAgentReceiptError:
        raise
    except Exception:
        raise AuthorAgentReceiptArtifactError(
            _ARTIFACT_ERROR_MESSAGE
        ) from None
    if stored != payload:
        raise AuthorAgentReceiptArtifactError(_ARTIFACT_ERROR_MESSAGE)
    return result


class AuthorAgentReceiptNormalizer:
    """只接受精确原始字节的声明式作者代理收据规范化器。"""

    @staticmethod
    def normalize_codemesh_v1(
        payload: bytes,
        *,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> AuthorAgentReceiptResult:
        return _normalize(
            payload,
            expected_subject_digest,
            artifact_store,
            source_kind="codemesh_v1",
        )

    @staticmethod
    def normalize_generic(
        payload: bytes,
        *,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> AuthorAgentReceiptResult:
        return _normalize(
            payload,
            expected_subject_digest,
            artifact_store,
            source_kind="generic",
        )
