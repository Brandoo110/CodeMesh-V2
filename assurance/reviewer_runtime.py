"""V2-P4-02 shared pure reviewer runtime.

Deterministic prompt construction and bytes-only structured response
normalization shared by later role modules (P4-03/04). Trust boundary: this
module contains no provider/network transport, no tool execution, no file or
environment reads, no current time, no randomness, and no response execution.
It never persists raw bytes and never touches ArtifactStore.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import Finding
from .reviewer_contracts import (
    FindingOutput,
    ReviewerFailureOutcome,
    ReviewerInput,
)
from .single_reviewer import ReviewQuestion


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROMPT_ID_RE = re.compile(r"^irp_[0-9a-f]{32}$")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

_MAX_ID_BYTES = 256
_MAX_RUBRIC_VERSION_BYTES = 128
_MAX_ITEM_CODE_BYTES = 256
_MAX_ITEM_NAME_BYTES = 512
_MAX_ITEM_DESCRIPTION_BYTES = 4096
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PROMPT_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 4096
_MAX_RESPONSE_ITEMS = 256
_MAX_CLAIM_BYTES = 4096
_MAX_QUESTION_BYTES = 4096
_MAX_REF_BYTES = 256
_MAX_REFS = 16

_SCHEMA_INVALID_MESSAGE = "invalid reviewer response"

_NO_TOOLS_MARKER = "NO_TOOLS_GRANTED"
_SCHEMA_MARKER = "RESPONSE_JSON_SCHEMA"


def _prompt_header(role: str) -> str:
    return f"CODEX_SAFE_{role.upper()}_REVIEWER_PROMPT_V1"


def _validate_text(
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


def _validate_sha256_digest(value: object) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


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


def _validate_refs(
    value: tuple[str, ...], *, canonical: bool
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError("evidence_refs must be an exact tuple")
    if len(value) > _MAX_REFS:
        raise ValueError(
            f"evidence_refs must contain at most {_MAX_REFS} items"
        )
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


def _profile_binding_error(
    value: ReviewerInput, profile: "ReviewerProfile"
) -> None:
    if value.reviewer_role != profile.role:
        raise ValueError(
            "reviewer_role must equal the profile role"
        )
    if value.rubric_version != profile.rubric_version:
        raise ValueError(
            "rubric_version must equal the profile rubric version"
        )
    if value.rubric_hash != profile.rubric_hash:
        raise ValueError(
            "rubric_hash must equal the profile rubric digest"
        )
    authority = frozenset(profile.tool_authority)
    for tool in value.tool_allowlist:
        if tool not in authority:
            raise ValueError(
                "tool_allowlist must be a subset of the profile "
                "read-only tool authority"
            )


def _exact_tuple_fields(
    value: object, info: ValidationInfo
) -> object:
    if type(value) is tuple:
        return value
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    raise ValueError(
        f"{info.field_name} must be an exact tuple at raw validation"
    )


class RubricItem(BaseModel):
    """One immutable rubric item in the canonical profile authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    number: StrictInt
    code: str
    name: str
    description: str

    @field_validator("number", mode="before")
    @classmethod
    def _exact_number(cls, value: object) -> object:
        if type(value) is not int or isinstance(value, bool):
            raise ValueError("number must be an exact int")
        if value <= 0:
            raise ValueError("number must be > 0")
        return value

    @field_validator("code", mode="before")
    @classmethod
    def _validate_code(cls, value: object) -> str:
        return _validate_text(
            value,
            field_name="code",
            max_bytes=_MAX_ITEM_CODE_BYTES,
        )

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str:
        return _validate_text(
            value,
            field_name="name",
            max_bytes=_MAX_ITEM_NAME_BYTES,
        )

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, value: object) -> str:
        return _validate_text(
            value,
            field_name="description",
            max_bytes=_MAX_ITEM_DESCRIPTION_BYTES,
        )


class ReviewerProfile(BaseModel):
    """Immutable reviewer profile: role, rubric authority, tool authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    role: str
    rubric_version: str
    rubric_hash: str
    rubric: tuple[RubricItem, ...]
    tool_authority: tuple[str, ...]

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: object) -> str:
        return _validate_text(
            value, field_name="role", max_bytes=_MAX_ID_BYTES
        )

    @field_validator("rubric_version", mode="before")
    @classmethod
    def _validate_rubric_version(cls, value: object) -> str:
        return _validate_text(
            value,
            field_name="rubric_version",
            max_bytes=_MAX_RUBRIC_VERSION_BYTES,
        )

    @field_validator("rubric_hash", mode="before")
    @classmethod
    def _validate_rubric_hash(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("rubric", "tool_authority", mode="before")
    @classmethod
    def _exact_profile_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_fields(value, info)

    @field_validator("tool_authority")
    @classmethod
    def _canonical_tool_authority(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = _validate_text(
                item,
                field_name="tool_authority item",
                max_bytes=_MAX_ID_BYTES,
            )
            if text in seen:
                raise ValueError("tool_authority items must be unique")
            seen.add(text)
            result.append(text)
        if result != sorted(result):
            raise ValueError(
                "tool_authority must be lexicographically sorted and unique"
            )
        return tuple(result)

    @model_validator(mode="before")
    @classmethod
    def _require_exact_rubric_items(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("ReviewerProfile must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            rubric = data.get("rubric")
            if type(rubric) in (list, tuple):
                data["rubric"] = tuple(
                    RubricItem.model_validate(item)
                    if type(item) is dict
                    else item
                    for item in rubric
                )
            return data
        if type(data.get("rubric")) is not tuple:
            raise ValueError(
                "rubric must be an exact tuple at raw validation"
            )
        for item in data.get("rubric", ()):
            if type(item) is not RubricItem:
                raise ValueError(
                    "rubric items must be exact RubricItem instances"
                )
        return data

    @model_validator(mode="after")
    def _bind_rubric_authority(self) -> "ReviewerProfile":
        if type(self.rubric) is not tuple:
            raise ValueError(
                "rubric must be an exact tuple at raw validation"
            )
        for item in self.rubric:
            if type(item) is not RubricItem:
                raise ValueError(
                    "rubric items must be exact RubricItem instances"
                )
        numbers = [item.number for item in self.rubric]
        if numbers != list(range(1, len(self.rubric) + 1)):
            raise ValueError(
                "rubric numbers must be sequential from 1 in exact order"
            )
        codes = [item.code for item in self.rubric]
        if len(set(codes)) != len(codes):
            raise ValueError("rubric codes must be unique")
        expected = _sha256_digest(
            _canonical_json_bytes(
                {
                    "role": self.role,
                    "rubric_version": self.rubric_version,
                    "rubric": [
                        item.model_dump(mode="json")
                        for item in self.rubric
                    ],
                }
            )
        )
        if self.rubric_hash != expected:
            raise ValueError(
                "rubric_hash must equal the canonical rubric digest"
            )
        return self


class ReviewerPrompt(BaseModel):
    """Deterministic prompt with full anti-forgery binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: ReviewerInput
    profile: ReviewerProfile
    prompt_text: str
    prompt_digest: str
    prompt_id: str

    @field_validator("prompt_text", mode="before")
    @classmethod
    def _validate_prompt_text(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("prompt_text must be an exact str")
        if len(value.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValueError(
                f"prompt_text must not exceed {_MAX_PROMPT_BYTES} UTF-8 bytes"
            )
        return value

    @field_validator("prompt_digest", mode="before")
    @classmethod
    def _validate_prompt_digest(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("prompt_id", mode="before")
    @classmethod
    def _validate_prompt_id(cls, value: object) -> str:
        if type(value) is not str or _PROMPT_ID_RE.fullmatch(value) is None:
            raise ValueError("prompt_id must be irp_<32 lowercase hex>")
        return value

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("ReviewerPrompt must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("input")) is dict:
                data["input"] = ReviewerInput.model_validate_json(
                    json.dumps(data["input"], ensure_ascii=False)
                )
            if type(data.get("profile")) is dict:
                data["profile"] = ReviewerProfile.model_validate_json(
                    json.dumps(data["profile"], ensure_ascii=False)
                )
            return data
        if type(data.get("input")) is not ReviewerInput:
            raise ValueError(
                "input must be an exact ReviewerInput instance"
            )
        if type(data.get("profile")) is not ReviewerProfile:
            raise ValueError(
                "profile must be an exact ReviewerProfile instance"
            )
        return data

    @model_validator(mode="after")
    def _bind_prompt_fields(self) -> "ReviewerPrompt":
        if type(self.input) is not ReviewerInput:
            raise ValueError(
                "input must be an exact ReviewerInput instance"
            )
        if type(self.profile) is not ReviewerProfile:
            raise ValueError(
                "profile must be an exact ReviewerProfile instance"
            )
        _profile_binding_error(self.input, self.profile)
        expected_text = _build_prompt_text(self.input, self.profile)
        if self.prompt_text != expected_text:
            raise ValueError("prompt_text must equal the deterministic build")
        expected_digest = _sha256_digest(
            self.prompt_text.encode("utf-8")
        )
        if self.prompt_digest != expected_digest:
            raise ValueError("prompt_digest must be recomputed")
        expected_id = _prompt_id_from_digest(
            self.input.subject.subject_digest,
            self.profile,
            expected_digest,
        )
        if self.prompt_id != expected_id:
            raise ValueError("prompt_id must be derived")
        return self


class ReviewerNormalizationInput(BaseModel):
    """Normalization input: exact prompt, model_ref, raw bytes, time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    prompt: ReviewerPrompt
    model_ref: str
    raw_response: bytes
    completed_at: AwareDatetime

    @field_validator("model_ref", mode="before")
    @classmethod
    def _validate_model_ref(cls, value: object) -> str:
        return _validate_text(
            value, field_name="model_ref", max_bytes=_MAX_ID_BYTES
        )

    @field_validator("raw_response", mode="before")
    @classmethod
    def _require_exact_bytes(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is bytes:
            return value
        if info.mode == "json" and type(value) is str:
            return value
        raise ValueError("raw_response must be exact bytes")

    @field_validator("raw_response")
    @classmethod
    def _bounded_raw_response(cls, value: bytes) -> bytes:
        if not value or len(value) > _MAX_RESPONSE_BYTES:
            raise ValueError(
                "raw_response must be nonempty and bounded to 1 MiB"
            )
        return value

    @field_validator("completed_at", mode="before")
    @classmethod
    def _reject_numeric_completed_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def _require_exact_prompt(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "ReviewerNormalizationInput must validate from a mapping"
            )
        if info.mode == "json":
            data = dict(data)
            if type(data.get("prompt")) is dict:
                data["prompt"] = ReviewerPrompt.model_validate_json(
                    json.dumps(data["prompt"], ensure_ascii=False)
                )
            return data
        if type(data.get("prompt")) is not ReviewerPrompt:
            raise ValueError(
                "prompt must be an exact ReviewerPrompt instance"
            )
        return data

    @model_validator(mode="after")
    def _bind_completed_at(self) -> "ReviewerNormalizationInput":
        if self.completed_at < self.prompt.input.requested_at:
            raise ValueError(
                "completed_at must be >= prompt.input.requested_at"
            )
        return self


class _FindingDraft(BaseModel):
    """Strict finding draft: model cannot supply identity or status fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    claim: str
    evidence_refs: tuple[str, ...]
    severity: Literal["info", "low", "medium", "high", "critical"]
    confidence: float

    @field_validator("claim", mode="before")
    @classmethod
    def _validate_claim(cls, value: object) -> str:
        return _validate_text(
            value, field_name="claim", max_bytes=_MAX_CLAIM_BYTES
        )

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_fields(value, info)

    @field_validator("evidence_refs")
    @classmethod
    def _unique_refs(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
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
    """Strict question draft with reviewer-role question semantics only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    question: str
    reason: Literal["model_question", "truncated_context"]
    evidence_refs: tuple[str, ...]

    @field_validator("question", mode="before")
    @classmethod
    def _validate_question(cls, value: object) -> str:
        return _validate_text(
            value,
            field_name="question",
            max_bytes=_MAX_QUESTION_BYTES,
        )

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _exact_refs_tuple(
        cls, value: object, info: ValidationInfo
    ) -> object:
        return _exact_tuple_fields(value, info)

    @field_validator("evidence_refs")
    @classmethod
    def _unique_refs(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _validate_refs(value, canonical=False)


class _ResponseDraft(BaseModel):
    """Strict response top level with bounded counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    reviewer_role: Literal["intent", "architecture", "operability"]
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
        return _exact_tuple_fields(value, info)

    @model_validator(mode="after")
    def _validate_counts(self) -> "_ResponseDraft":
        if len(self.findings) + len(self.questions) > _MAX_RESPONSE_ITEMS:
            raise ValueError(
                "findings and questions must not exceed 256 total items"
            )
        return self


def _response_schema(role: str) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "subject_digest",
            "reviewer_role",
            "rubric_hash",
            "findings",
            "questions",
        ],
        "properties": {
            "schema_version": {"const": "v1"},
            "subject_digest": {
                "type": "string",
                "pattern": "^sha256:[0-9a-f]{64}$",
            },
            "reviewer_role": {"const": role},
            "rubric_hash": {
                "type": "string",
                "pattern": "^sha256:[0-9a-f]{64}$",
            },
            "findings": {
                "type": "array",
                "maxItems": _MAX_RESPONSE_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "reviewer_role",
                        "claim",
                        "evidence_refs",
                        "severity",
                        "confidence",
                    ],
                    "properties": {
                        "reviewer_role": {"const": role},
                        "claim": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_CLAIM_BYTES,
                        },
                        "evidence_refs": {
                            "type": "array",
                            "maxItems": _MAX_REFS,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": _MAX_REF_BYTES,
                            },
                            "uniqueItems": True,
                        },
                        "severity": {
                            "enum": [
                                "info",
                                "low",
                                "medium",
                                "high",
                                "critical",
                            ]
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                },
            },
            "questions": {
                "type": "array",
                "maxItems": _MAX_RESPONSE_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "reviewer_role",
                        "question",
                        "reason",
                        "evidence_refs",
                    ],
                    "properties": {
                        "reviewer_role": {"const": role},
                        "question": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_QUESTION_BYTES,
                        },
                        "reason": {
                            "enum": [
                                "model_question",
                                "truncated_context",
                            ]
                        },
                        "evidence_refs": {
                            "type": "array",
                            "maxItems": _MAX_REFS,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": _MAX_REF_BYTES,
                            },
                            "uniqueItems": True,
                        },
                    },
                },
            },
        },
    }


def _response_schema_text(role: str) -> str:
    return json.dumps(
        _response_schema(role),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


def _build_prompt_text(
    value: ReviewerInput, profile: ReviewerProfile
) -> str:
    subject = value.subject
    parts: list[str] = [
        _prompt_header(profile.role),
        f"Subject digest: {subject.subject_digest}",
        f"Change ID: {subject.change_id}",
        f"Repository: {subject.repository}",
        f"Base revision: {subject.base_revision}",
        f"Head revision: {subject.head_revision}",
        f"Task digest: {subject.task_digest}",
        f"Policy version: {subject.policy_version}",
        f"Change created at: {subject.created_at.isoformat()}",
        f"Reviewer role: {profile.role}",
        f"Rubric version: {profile.rubric_version}",
        f"Rubric hash: {profile.rubric_hash}",
        "Rubric:",
    ]
    for item in profile.rubric:
        parts.append(
            f"{item.number}. {item.code} - {item.name}: "
            f"{item.description}"
        )
    parts.append("Granted read-only tools:")
    if value.tool_allowlist:
        parts.append(", ".join(value.tool_allowlist))
    else:
        parts.append(_NO_TOOLS_MARKER)
    parts.append(
        "Evidence allowlist: " + ", ".join(value.evidence_allowlist)
    )
    parts.append("Read-only evidence contexts:")
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
            (
                "Apply the current profile rubric and do not use any other "
                "reviewer rubric or findings."
            ),
            "There is no hidden evidence beyond the contexts shown above.",
            (
                "You must not approve or emit PASS/Gate; you never produce "
                "a gate decision."
            ),
            (
                "Unsupported or missing evidence claims must become "
                "questions, never findings."
            ),
            "Do not execute any response content; return data only.",
            "Return exactly one strict JSON object matching the schema below.",
            _SCHEMA_MARKER,
            _response_schema_text(profile.role),
            "END " + _prompt_header(profile.role),
        ]
    )
    return "\n".join(parts)


def _prompt_id_from_digest(
    subject_digest: str, profile: ReviewerProfile, prompt_digest: str
) -> str:
    body = {
        "subject_digest": subject_digest,
        "reviewer_role": profile.role,
        "rubric_version": profile.rubric_version,
        "rubric_hash": profile.rubric_hash,
        "prompt_digest": prompt_digest,
    }
    return "irp_" + hashlib.sha256(_canonical_json_bytes(body)).hexdigest()[
        :32
    ]


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
        raise ValueError(_SCHEMA_INVALID_MESSAGE) from None
    if text.startswith("\ufeff") or "\x00" in text:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    try:
        _scan_json_limits(text)
        raw = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_SCHEMA_INVALID_MESSAGE) from None
    if type(raw) is not dict:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    return raw


def _question_id_from_data(data: dict) -> str:
    body = {
        key: value for key, value in data.items() if key != "question_id"
    }
    return "rq_" + hashlib.sha256(_canonical_json_bytes(body)).hexdigest()[
        :32
    ]


def _question_from_fields(
    *,
    subject_digest: str,
    reviewer_role: str,
    question: str,
    reason: str,
    refs: tuple[str, ...],
    model_ref: str,
    rubric_hash: str,
) -> ReviewQuestion:
    data = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "reviewer_role": reviewer_role,
        "question": question,
        "reason": reason,
        "evidence_refs": refs,
        "rubric_hash": rubric_hash,
        "model_ref": model_ref,
        "status": "open",
    }
    question_id = _question_id_from_data(data)
    return ReviewQuestion.model_validate(
        {**data, "question_id": question_id}
    )


def _finding_from_draft(
    finding: _FindingDraft,
    refs: tuple[str, ...],
    reviewer_input: ReviewerInput,
    model_ref: str,
    profile: ReviewerProfile,
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
        rubric_hash=profile.rubric_hash,
        model_ref=model_ref,
        status="open",
    )
    body = {
        key: value
        for key, value in placeholder.model_dump(mode="json").items()
        if key != "finding_id"
    }
    finding_id = "fnd_" + hashlib.sha256(
        _canonical_json_bytes(body)
    ).hexdigest()[:32]
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
    reviewer_input: ReviewerInput,
    model_ref: str,
    profile: ReviewerProfile,
) -> tuple[tuple[Finding, ...], tuple[ReviewQuestion, ...]]:
    valid_ids = frozenset(reviewer_input.evidence_allowlist)
    truncated_ids = frozenset(
        item.evidence_id
        for item in reviewer_input.contexts
        if item.truncated
    )
    findings: list[Finding] = []
    questions: list[ReviewQuestion] = []
    for item in draft.findings:
        refs = tuple(
            sorted(ref for ref in item.evidence_refs if ref in valid_ids)
        )
        if item.evidence_refs and len(refs) == len(item.evidence_refs):
            if any(ref in truncated_ids for ref in refs):
                questions.append(
                    _question_from_fields(
                        subject_digest=reviewer_input.subject.subject_digest,
                        reviewer_role=item.reviewer_role,
                        question=item.claim,
                        reason="truncated_context",
                        refs=refs,
                        model_ref=model_ref,
                        rubric_hash=profile.rubric_hash,
                    )
                )
            else:
                findings.append(
                    _finding_from_draft(
                        item,
                        refs,
                        reviewer_input,
                        model_ref,
                        profile,
                    )
                )
        else:
            questions.append(
                _question_from_fields(
                    subject_digest=reviewer_input.subject.subject_digest,
                    reviewer_role=item.reviewer_role,
                    question=item.claim,
                    reason="unsupported_finding_evidence",
                    refs=refs,
                    model_ref=model_ref,
                    rubric_hash=profile.rubric_hash,
                )
            )
    for item in draft.questions:
        if any(ref not in valid_ids for ref in item.evidence_refs):
            raise ValueError(_SCHEMA_INVALID_MESSAGE)
        refs = tuple(sorted(item.evidence_refs))
        if item.reason == "truncated_context":
            if not refs or not any(ref in truncated_ids for ref in refs):
                raise ValueError(_SCHEMA_INVALID_MESSAGE)
        questions.append(
            _question_from_fields(
                subject_digest=reviewer_input.subject.subject_digest,
                reviewer_role=item.reviewer_role,
                question=item.question,
                reason=item.reason,
                refs=refs,
                model_ref=model_ref,
                rubric_hash=profile.rubric_hash,
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


def _normalize_response(
    raw: bytes,
    reviewer_input: ReviewerInput,
    model_ref: str,
    profile: ReviewerProfile,
) -> tuple[tuple[Finding, ...], tuple[ReviewQuestion, ...]]:
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
    except (ValidationError, RecursionError, TypeError, ValueError):
        raise ValueError(_SCHEMA_INVALID_MESSAGE) from None
    if draft.subject_digest != reviewer_input.subject.subject_digest:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    if draft.reviewer_role != reviewer_input.reviewer_role:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    if draft.rubric_hash != profile.rubric_hash:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    try:
        findings, questions = _normalize_drafts(
            draft, reviewer_input, model_ref, profile
        )
    except (ValidationError, RecursionError, TypeError, ValueError):
        raise ValueError(_SCHEMA_INVALID_MESSAGE) from None
    return findings, questions


def _verify_prompt_binding(
    prompt: ReviewerPrompt, profile: ReviewerProfile
) -> None:
    if type(prompt) is not ReviewerPrompt:
        raise TypeError("prompt must be an exact ReviewerPrompt instance")
    if type(prompt.profile) is not ReviewerProfile:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    if prompt.profile != profile:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    _profile_binding_error(prompt.input, prompt.profile)
    expected_text = _build_prompt_text(prompt.input, prompt.profile)
    if prompt.prompt_text != expected_text:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    expected_digest = _sha256_digest(prompt.prompt_text.encode("utf-8"))
    if prompt.prompt_digest != expected_digest:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)
    expected_id = _prompt_id_from_digest(
        prompt.input.subject.subject_digest,
        prompt.profile,
        expected_digest,
    )
    if prompt.prompt_id != expected_id:
        raise ValueError(_SCHEMA_INVALID_MESSAGE)


def _schema_invalid_output(
    value: ReviewerNormalizationInput,
) -> FindingOutput:
    return FindingOutput(
        schema_version="v1",
        input=value.prompt.input,
        outcome="schema_invalid",
        findings=(),
        questions=(),
        failure=ReviewerFailureOutcome(
            schema_version="v1",
            code="schema_invalid",
            details=_SCHEMA_INVALID_MESSAGE,
        ),
        completed_at=value.completed_at,
    )


class StructuredReviewerRuntime:
    """Pure deterministic shared facade: prepare and normalize."""

    @staticmethod
    def prepare(
        value: ReviewerInput, profile: ReviewerProfile
    ) -> ReviewerPrompt:
        if type(value) is not ReviewerInput:
            raise TypeError("value must be an exact ReviewerInput")
        if type(profile) is not ReviewerProfile:
            raise TypeError("profile must be an exact ReviewerProfile")
        _profile_binding_error(value, profile)
        text = _build_prompt_text(value, profile)
        prompt_digest = _sha256_digest(text.encode("utf-8"))
        prompt_id = _prompt_id_from_digest(
            value.subject.subject_digest, profile, prompt_digest
        )
        return ReviewerPrompt(
            schema_version="v1",
            input=value,
            profile=profile,
            prompt_text=text,
            prompt_digest=prompt_digest,
            prompt_id=prompt_id,
        )

    @staticmethod
    def normalize(
        value: ReviewerNormalizationInput, profile: ReviewerProfile
    ) -> FindingOutput:
        if type(value) is not ReviewerNormalizationInput:
            raise TypeError(
                "value must be an exact ReviewerNormalizationInput"
            )
        if type(profile) is not ReviewerProfile:
            raise TypeError("profile must be an exact ReviewerProfile")
        try:
            _verify_prompt_binding(value.prompt, profile)
            findings, questions = _normalize_response(
                value.raw_response,
                value.prompt.input,
                value.model_ref,
                profile,
            )
            return FindingOutput(
                schema_version="v1",
                input=value.prompt.input,
                outcome="success",
                findings=findings,
                questions=questions,
                failure=None,
                completed_at=value.completed_at,
            )
        except (ValidationError, RecursionError, TypeError, ValueError):
            return _schema_invalid_output(value)


__all__ = (
    "ReviewerPrompt",
    "ReviewerNormalizationInput",
    "StructuredReviewerRuntime",
)
