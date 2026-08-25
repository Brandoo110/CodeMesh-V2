"""Evidence manifest 构建（V2-P2-06）。

信任边界：本模块只接收调用方提供的 EvidenceManifestInput 与显式参数；不读取
文件/路径/网络/API/日志/仓库/Git 或环境数据；不执行 payload、工具、命令、
检查或结果内容；不调用模型、网络、Git、子进程、shell、eval 或 exec。引用
Artifact 只做存在性/校验/读取，绝不把引用字节内容放进模型、规范字节或错误。
"""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_ID_RE = re.compile(r"^em_[0-9a-f]{32}$")
_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

_MAX_ITEMS = 256
_MAX_TEXT_BYTES = 256
_MAX_SOURCE_REF_BYTES = 1024

_DUMMY_MANIFEST_ID = "em_" + "0" * 32
_DUMMY_DIGEST = "sha256:" + "0" * 64

_INPUT_ERROR_MESSAGE = "invalid evidence manifest input"
_SUBJECT_ERROR_MESSAGE = "evidence manifest subject digest mismatch"
_ARTIFACT_ERROR_MESSAGE = "evidence manifest artifact validation failed"
_PERSISTENCE_ERROR_MESSAGE = "evidence manifest artifact persistence failed"

_INCOMPLETE_STATUSES = frozenset(
    {"error", "timeout", "cancelled", "truncated"}
)
_EVIDENCE_STATUSES = Literal[
    "success", "failure", "error", "timeout", "cancelled", "truncated"
]
_TRUST_LEVELS = Literal[
    "declared", "observed", "deterministic", "inferred", "human_attested"
]
_REDACTION_STATUSES = Literal[
    "not_assessed",
    "declared_redacted",
    "contains_unredacted_content",
    "not_applicable",
]


class EvidenceManifestError(Exception):
    """证据清单构建或持久化失败。"""


class EvidenceManifestInputError(EvidenceManifestError):
    """输入、参数或引用校验失败。"""


class EvidenceManifestSubjectError(EvidenceManifestError):
    """条目 subject digest 与预期不一致。"""


class EvidenceManifestArtifactError(EvidenceManifestError):
    """引用 Artifact 缺失、损坏或读取失败。"""


class EvidenceManifestPersistenceError(EvidenceManifestError):
    """清单 Artifact 持久化校验失败。"""


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _freshness(evaluated_at: datetime, fresh_until: datetime | None) -> str:
    if fresh_until is None:
        return "unknown"
    if evaluated_at <= fresh_until:
        return "fresh"
    return "stale"


def _manifest_id(subject_digest: str, canonical_digest: str) -> str:
    value = (subject_digest + canonical_digest).encode("utf-8")
    return "em_" + hashlib.sha256(value).hexdigest()[:32]


def _canonical_body(manifest: "EvidenceManifest") -> bytes:
    data = manifest.model_dump(mode="json")
    data.pop("manifest_id")
    data.pop("canonical_digest")
    data.pop("artifact_digest")
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_sha256_digest(value: object) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


def _validate_text(value: object, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise ValueError("must be a str")
    if not value.strip():
        raise ValueError("must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"must not exceed {max_bytes} UTF-8 bytes")
    return value


def _validate_source_ref(value: object) -> str:
    value = _validate_text(value, max_bytes=_MAX_SOURCE_REF_BYTES)
    if "\r" in value or "\n" in value:
        raise ValueError("must not contain CR or LF")
    if value.startswith(
        ("/", "\\", "~", "file://")
    ) or re.match(r"^[A-Za-z]:[\\/]", value) is not None:
        raise ValueError("local file references are not allowed")
    if (
        "/Users/" in value
        or "/home/" in value
        or "file://" in value
        or "~/" in value
        or "\\\\" in value
        or re.search(r"(?:^|[\s:|])[A-Za-z]:[\\/]", value) is not None
    ):
        raise ValueError("local file references are not allowed")
    return value


def _reject_numeric_datetime(value: object) -> object:
    """拒绝 bool/数值/数字字符串 datetime 原始值（fail closed）。"""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        raise ValueError("datetime must not be a numeric value")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None:
            raise ValueError("datetime must not be a numeric string")
    return value


def _evidence_id(manifest: "EvidenceManifest") -> str:
    value = (manifest.manifest_id + manifest.artifact_digest).encode("utf-8")
    return "ev_manifest_" + hashlib.sha256(value).hexdigest()[:32]


class EvidenceManifestInput(BaseModel):
    """一条待纳入清单的证据输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence: Evidence
    fresh_until: AwareDatetime | None = None
    redaction_status: _REDACTION_STATUSES

    @field_validator("evidence", mode="before")
    @classmethod
    def _reject_numeric_evidence_collected_at(
        cls, value: object
    ) -> object:
        if isinstance(value, Evidence):
            return value
        if isinstance(value, dict) and "collected_at" in value:
            _reject_numeric_datetime(value["collected_at"])
        return value

    @field_validator("fresh_until", mode="before")
    @classmethod
    def _reject_numeric_fresh_until(cls, value: object) -> object:
        if value is None:
            return value
        return _reject_numeric_datetime(value)

    @model_validator(mode="after")
    def _validate_fresh_until(self) -> "EvidenceManifestInput":
        if (
            self.fresh_until is not None
            and self.fresh_until < self.evidence.collected_at
        ):
            raise ValueError("fresh_until must be >= evidence.collected_at")
        return self


class EvidenceManifestEntry(BaseModel):
    """清单中的扁平化证据条目。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence_id: str
    kind: str
    trust_level: _TRUST_LEVELS
    producer: str
    subject_digest: str
    artifact_digest: str
    source_ref: str
    status: _EVIDENCE_STATUSES
    collected_at: AwareDatetime
    fresh_until: AwareDatetime | None = None
    freshness: Literal["unknown", "fresh", "stale"]
    redaction_status: _REDACTION_STATUSES

    @field_validator("collected_at", mode="before")
    @classmethod
    def _reject_numeric_collected_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("fresh_until", mode="before")
    @classmethod
    def _reject_numeric_fresh_until(cls, value: object) -> object:
        if value is None:
            return value
        return _reject_numeric_datetime(value)

    @field_validator("evidence_id", "kind", "producer", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: object) -> str:
        return _validate_text(value)

    @field_validator(
        "subject_digest", "artifact_digest", mode="before"
    )
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @field_validator("source_ref", mode="before")
    @classmethod
    def _validate_source_field(cls, value: object) -> str:
        return _validate_source_ref(value)

    @model_validator(mode="after")
    def _validate_freshness_relation(self) -> "EvidenceManifestEntry":
        if self.fresh_until is None:
            if self.freshness != "unknown":
                raise ValueError(
                    "freshness must be unknown without a deadline"
                )
        else:
            if self.freshness == "unknown":
                raise ValueError(
                    "freshness must be fresh or stale with a deadline"
                )
            if self.fresh_until < self.collected_at:
                raise ValueError(
                    "fresh_until must be >= collected_at"
                )
        return self


class EvidenceManifest(BaseModel):
    """不可变证据清单及全部自校验摘要绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    manifest_id: str
    subject_digest: str
    evaluated_at: AwareDatetime
    entries: tuple[EvidenceManifestEntry, ...]
    evidence_count: StrictInt = Field(ge=1)
    completeness_status: Literal["complete", "has_gaps"]
    has_incomplete_evidence: StrictBool
    has_stale_evidence: StrictBool
    has_unknown_freshness: StrictBool
    has_unredacted_content: StrictBool
    has_unassessed_redaction: StrictBool
    canonical_digest: str
    artifact_digest: str

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _reject_numeric_evaluated_at(cls, value: object) -> object:
        return _reject_numeric_datetime(value)

    @field_validator("manifest_id", mode="before")
    @classmethod
    def _validate_manifest_id(cls, value: object) -> str:
        if type(value) is not str or _MANIFEST_ID_RE.fullmatch(value) is None:
            raise ValueError("manifest_id must be em_<32 lowercase hex>")
        return value

    @field_validator(
        "subject_digest", "canonical_digest", "artifact_digest", mode="before"
    )
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        return _validate_sha256_digest(value)

    @model_validator(mode="after")
    def _validate_bindings(self) -> "EvidenceManifest":
        entries = self.entries
        if not entries:
            raise ValueError("entries must not be empty")
        ids = [entry.evidence_id for entry in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("entries must be unique")
        if ids != sorted(ids):
            raise ValueError("entries must be sorted by evidence_id")
        if self.evidence_count != len(entries):
            raise ValueError("evidence_count must equal entry count")
        if any(entry.subject_digest != self.subject_digest for entry in entries):
            raise ValueError("entry subject must equal manifest subject")
        if any(entry.collected_at > self.evaluated_at for entry in entries):
            raise ValueError(
                "evaluated_at must not be earlier than collected_at"
            )
        for entry in entries:
            expected = _freshness(self.evaluated_at, entry.fresh_until)
            if entry.freshness != expected:
                raise ValueError(
                    "entry freshness must be derived from evaluated_at"
                )
        incomplete = any(entry.status in _INCOMPLETE_STATUSES for entry in entries)
        stale = any(entry.freshness == "stale" for entry in entries)
        unknown = any(entry.freshness == "unknown" for entry in entries)
        unredacted = any(
            entry.redaction_status == "contains_unredacted_content"
            for entry in entries
        )
        unassessed = any(
            entry.redaction_status == "not_assessed" for entry in entries
        )
        has_gaps = incomplete or stale or unknown or unredacted or unassessed
        flags = (
            (self.has_incomplete_evidence, incomplete),
            (self.has_stale_evidence, stale),
            (self.has_unknown_freshness, unknown),
            (self.has_unredacted_content, unredacted),
            (self.has_unassessed_redaction, unassessed),
        )
        for actual, expected in flags:
            if actual is not expected:
                raise ValueError(
                    "summary flags must match the entry facts"
                )
        if (self.completeness_status == "has_gaps") != has_gaps:
            raise ValueError(
                "completeness_status must match the summary flags"
            )
        try:
            body = _canonical_body(self)
        except Exception:
            raise ValueError("manifest canonicalization failed") from None
        digest = _sha256_digest(body)
        if self.canonical_digest != digest:
            raise ValueError("canonical_digest must be recomputed")
        if self.artifact_digest != digest:
            raise ValueError("artifact_digest must equal canonical digest")
        if self.manifest_id != _manifest_id(self.subject_digest, digest):
            raise ValueError("manifest_id must be derived")
        return self


class EvidenceManifestResult(BaseModel):
    """清单及其确定性 Evidence 的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    manifest: EvidenceManifest
    evidence: Evidence

    @field_validator("evidence", mode="before")
    @classmethod
    def _reject_numeric_evidence_collected_at(
        cls, value: object
    ) -> object:
        if isinstance(value, Evidence):
            return value
        if isinstance(value, dict) and "collected_at" in value:
            _reject_numeric_datetime(value["collected_at"])
        return value

    @model_validator(mode="after")
    def _validate_bindings(self) -> "EvidenceManifestResult":
        manifest = self.manifest
        evidence = self.evidence
        if evidence.kind != "evidence_manifest":
            raise ValueError("evidence.kind must be evidence_manifest")
        if evidence.producer != "builder.evidence_manifest":
            raise ValueError(
                "evidence.producer must be builder.evidence_manifest"
            )
        if evidence.subject_digest != manifest.subject_digest:
            raise ValueError(
                "evidence.subject_digest must equal manifest subject"
            )
        if evidence.artifact_digest != manifest.artifact_digest:
            raise ValueError(
                "evidence.artifact_digest must equal manifest artifact"
            )
        expected_ref = f"evidence_manifest:{manifest.manifest_id}"
        if evidence.source_ref != expected_ref:
            raise ValueError("evidence.source_ref must match manifest ID")
        if evidence.collected_at != manifest.evaluated_at:
            raise ValueError(
                "evidence.collected_at must equal evaluated_at"
            )
        if evidence.trust_level != "deterministic":
            raise ValueError("evidence.trust_level must be deterministic")
        if evidence.trace_id is not None:
            raise ValueError("evidence.trace_id must be None")
        expected_status = (
            "success"
            if manifest.completeness_status == "complete"
            else "truncated"
        )
        if evidence.status != expected_status:
            raise ValueError("evidence.status must follow completeness")
        if evidence.evidence_id != _evidence_id(manifest):
            raise ValueError("evidence.evidence_id must be derived")
        if any(
            entry.evidence_id == manifest.manifest_id
            or entry.kind == "evidence_manifest"
            for entry in manifest.entries
        ):
            raise ValueError(
                "manifest must not be recursively included as an entry"
            )
        return self


class EvidenceManifestBuilder:
    """只接受精确 EvidenceManifestInput 序列的证据清单构建器。"""

    @staticmethod
    def build(
        items: Sequence[EvidenceManifestInput],
        *,
        subject_digest: str,
        evaluated_at: datetime,
        artifact_store: ArtifactStore,
    ) -> "EvidenceManifestResult":
        if not isinstance(items, Sequence) or isinstance(
            items, (str, bytes, bytearray, memoryview)
        ):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
        try:
            materialized = tuple(items)
        except Exception:
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None
        if not 1 <= len(materialized) <= _MAX_ITEMS:
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
        if (
            type(subject_digest) is not str
            or _SHA256_DIGEST_RE.fullmatch(subject_digest) is None
        ):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
        if (
            type(evaluated_at) is not datetime
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() is None
        ):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
        if type(artifact_store) is not ArtifactStore:
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)

        seen_ids: set[str] = set()
        try:
            for item in materialized:
                if type(item) is not EvidenceManifestInput:
                    raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
                if item.evidence.subject_digest != subject_digest:
                    raise EvidenceManifestSubjectError(_SUBJECT_ERROR_MESSAGE)
                if item.evidence.evidence_id in seen_ids:
                    raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
                seen_ids.add(item.evidence.evidence_id)
                if evaluated_at < item.evidence.collected_at:
                    raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE)
        except EvidenceManifestError:
            raise
        except (TypeError, ValueError):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None

        ordered = sorted(
            materialized,
            key=lambda item: item.evidence.evidence_id,
        )
        try:
            entries = tuple(
                EvidenceManifestEntry(
                    evidence_id=item.evidence.evidence_id,
                    kind=item.evidence.kind,
                    trust_level=item.evidence.trust_level,
                    producer=item.evidence.producer,
                    subject_digest=item.evidence.subject_digest,
                    artifact_digest=item.evidence.artifact_digest,
                    source_ref=item.evidence.source_ref,
                    status=item.evidence.status,
                    collected_at=item.evidence.collected_at,
                    fresh_until=item.fresh_until,
                    freshness=_freshness(evaluated_at, item.fresh_until),
                    redaction_status=item.redaction_status,
                )
                for item in ordered
            )
        except (ValidationError, RecursionError, TypeError, ValueError):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None

        incomplete = any(
            entry.status in _INCOMPLETE_STATUSES for entry in entries
        )
        stale = any(entry.freshness == "stale" for entry in entries)
        unknown = any(entry.freshness == "unknown" for entry in entries)
        unredacted = any(
            entry.redaction_status == "contains_unredacted_content"
            for entry in entries
        )
        unassessed = any(
            entry.redaction_status == "not_assessed" for entry in entries
        )
        has_gaps = incomplete or stale or unknown or unredacted or unassessed

        unique_digests = sorted(
            {entry.artifact_digest for entry in entries}
        )
        try:
            for digest in unique_digests:
                if artifact_store.exists(digest) is not True:
                    raise EvidenceManifestArtifactError(
                        _ARTIFACT_ERROR_MESSAGE
                    )
                if artifact_store.verify(digest) is not True:
                    raise EvidenceManifestArtifactError(
                        _ARTIFACT_ERROR_MESSAGE
                    )
                referenced_bytes = artifact_store.get_bytes(digest)
                if type(referenced_bytes) is not bytes:
                    raise EvidenceManifestArtifactError(
                        _ARTIFACT_ERROR_MESSAGE
                    )
                if _sha256_digest(referenced_bytes) != digest:
                    raise EvidenceManifestArtifactError(
                        _ARTIFACT_ERROR_MESSAGE
                    )
        except EvidenceManifestArtifactError:
            raise
        except Exception:
            raise EvidenceManifestArtifactError(
                _ARTIFACT_ERROR_MESSAGE
            ) from None

        provisional = EvidenceManifest.model_construct(
            schema_version="v1",
            manifest_id=_DUMMY_MANIFEST_ID,
            subject_digest=subject_digest,
            evaluated_at=evaluated_at,
            entries=entries,
            evidence_count=len(entries),
            completeness_status=(
                "has_gaps" if has_gaps else "complete"
            ),
            has_incomplete_evidence=incomplete,
            has_stale_evidence=stale,
            has_unknown_freshness=unknown,
            has_unredacted_content=unredacted,
            has_unassessed_redaction=unassessed,
            canonical_digest=_DUMMY_DIGEST,
            artifact_digest=_DUMMY_DIGEST,
        )
        try:
            body = _canonical_body(provisional)
        except Exception:
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None
        digest = _sha256_digest(body)
        manifest_id = _manifest_id(subject_digest, digest)
        manifest_data = {
            "schema_version": "v1",
            "manifest_id": manifest_id,
            "subject_digest": subject_digest,
            "evaluated_at": evaluated_at,
            "entries": [entry.model_dump(mode="json") for entry in entries],
            "evidence_count": len(entries),
            "completeness_status": (
                "has_gaps" if has_gaps else "complete"
            ),
            "has_incomplete_evidence": incomplete,
            "has_stale_evidence": stale,
            "has_unknown_freshness": unknown,
            "has_unredacted_content": unredacted,
            "has_unassessed_redaction": unassessed,
            "canonical_digest": digest,
            "artifact_digest": digest,
        }
        try:
            manifest = EvidenceManifest.model_validate(manifest_data)
        except (ValidationError, RecursionError):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None

        evidence = Evidence(
            schema_version="v1",
            evidence_id=_evidence_id(manifest),
            subject_digest=manifest.subject_digest,
            kind="evidence_manifest",
            producer="builder.evidence_manifest",
            artifact_digest=manifest.artifact_digest,
            source_ref=f"evidence_manifest:{manifest.manifest_id}",
            trace_id=None,
            status=(
                "success"
                if manifest.completeness_status == "complete"
                else "truncated"
            ),
            trust_level="deterministic",
            collected_at=manifest.evaluated_at,
        )
        result_data = {
            "schema_version": "v1",
            "manifest": manifest.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json"),
        }
        try:
            result = EvidenceManifestResult.model_validate(result_data)
        except (ValidationError, RecursionError):
            raise EvidenceManifestInputError(_INPUT_ERROR_MESSAGE) from None

        try:
            stored_digest = artifact_store.put_bytes(body)
        except Exception:
            raise EvidenceManifestPersistenceError(
                _PERSISTENCE_ERROR_MESSAGE
            ) from None
        if stored_digest != manifest.artifact_digest:
            raise EvidenceManifestPersistenceError(
                _PERSISTENCE_ERROR_MESSAGE
            )
        try:
            verified = artifact_store.verify(manifest.artifact_digest)
            persisted = artifact_store.get_bytes(manifest.artifact_digest)
        except Exception:
            raise EvidenceManifestPersistenceError(
                _PERSISTENCE_ERROR_MESSAGE
            ) from None
        if verified is not True or persisted != body:
            raise EvidenceManifestPersistenceError(
                _PERSISTENCE_ERROR_MESSAGE
            )
        return result
