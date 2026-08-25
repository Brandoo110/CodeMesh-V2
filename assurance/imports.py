"""通用证据导入（V2-P2-04）。

信任边界：只接收调用方提供的精确原始 evidence.json 字节，不做路径/URL
读取、仓库搜索、命令/结果/payload 执行、模型/网络/Git/命令调用、下载，
也不创建被引用工件。producer/kind/status/claimed trust/signature 均仅作
声明；输出 Evidence.trust_level 与 receipt 有效信任恒为 declared，
签名仅作未验证元数据，永不提升信任。
"""

import hashlib
import json
import re
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


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_PRODUCER_AND_KIND_BYTES = 128
_MAX_RESULT_CHARS = 4096
_MAX_TRACE_CHARS = 256
_MAX_COMMAND_ITEMS = 32
_MAX_COMMAND_UTF8_BYTES = 4096

_PAYLOAD_ERROR_MESSAGE = "invalid generic evidence payload"
_SUBJECT_ARGUMENT_ERROR_MESSAGE = "invalid expected subject digest"
_SUBJECT_MISMATCH_MESSAGE = "generic evidence subject digest mismatch"
_REFERENCED_ARTIFACT_ERROR_MESSAGE = (
    "referenced generic evidence artifact is missing or invalid"
)
_RAW_PERSIST_ERROR_MESSAGE = "generic evidence raw payload persistence failed"


class GenericEvidenceImportError(Exception):
    """通用证据导入失败。"""


class GenericEvidencePayloadError(GenericEvidenceImportError):
    """原始 payload 字节、编码、JSON 或 schema 非法。"""


class GenericEvidenceSubjectMismatch(GenericEvidenceImportError):
    """envelope subject digest 与预期 subject digest 不一致。"""


class GenericEvidenceArtifactError(GenericEvidenceImportError):
    """被引用工件或原始 payload 持久化失败。"""


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


class SignatureMetadata(BaseModel):
    """未验证签名的声明性元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    scheme: str
    key_id: str
    signature_digest: str

    @field_validator("scheme", "key_id", mode="before")
    @classmethod
    def _validate_text(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a str")
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("signature_digest", mode="before")
    @classmethod
    def _validate_signature_digest(cls, value: object) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(
            value
        ) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value


class GenericEvidenceEnvelope(BaseModel):
    """调用方声明的一次通用证据 envelope。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    producer: str = Field(max_length=_MAX_PRODUCER_AND_KIND_BYTES)
    kind: str = Field(max_length=_MAX_PRODUCER_AND_KIND_BYTES)
    subject_digest: str
    status: Literal[
        "success", "failure", "error", "timeout", "cancelled", "truncated"
    ]
    artifact_digest: str
    collected_at: AwareDatetime
    claimed_trust_level: Literal[
        "declared", "observed", "deterministic", "inferred", "human_attested"
    ]
    command: tuple[str, ...] | None = None
    result: str = Field(max_length=_MAX_RESULT_CHARS)
    trace_id: str | None = Field(default=None, max_length=_MAX_TRACE_CHARS)
    signature: SignatureMetadata | None = None

    @field_validator("producer", "kind", "result", mode="before")
    @classmethod
    def _validate_required_text(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a str")
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("subject_digest", "artifact_digest", mode="before")
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(
            value
        ) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("trace_id", mode="before")
    @classmethod
    def _validate_trace_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("trace_id must be a str or null")
        if not value.strip():
            raise ValueError("trace_id must not be empty or whitespace-only")
        return value

    @field_validator("command", mode="before")
    @classmethod
    def _validate_command(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not isinstance(value, (tuple, list)):
            raise ValueError("command must be a tuple/list or null")
        items: list[str] = []
        total_utf8_bytes = 0
        for index, item in enumerate(value):
            if type(item) is not str:
                raise ValueError(f"command item {index} must be exactly a str")
            if not item.strip():
                raise ValueError(
                    "command items must not be empty or whitespace-only"
                )
            if "\x00" in item:
                raise ValueError("command items must not contain NUL")
            total_utf8_bytes += len(item.encode("utf-8"))
            items.append(item)
        if not items:
            raise ValueError("command must contain 1..32 items when present")
        if len(items) > _MAX_COMMAND_ITEMS:
            raise ValueError(
                f"command must contain at most {_MAX_COMMAND_ITEMS} items"
            )
        if total_utf8_bytes > _MAX_COMMAND_UTF8_BYTES:
            raise ValueError(
                "combined command UTF-8 bytes must not exceed"
                f" {_MAX_COMMAND_UTF8_BYTES}"
            )
        return tuple(items)


def _canonical_envelope_bytes(envelope: GenericEvidenceEnvelope) -> bytes:
    """Canonical UTF-8 JSON bytes for a validated evidence envelope."""
    return json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class GenericEvidenceImportReceipt(BaseModel):
    """一次通用证据导入的声明性收据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    import_payload_artifact_digest: str
    canonical_payload_digest: str
    referenced_artifact_digest: str
    referenced_artifact_verified: Literal[True] = True
    claimed_trust_level: Literal[
        "declared", "observed", "deterministic", "inferred", "human_attested"
    ]
    effective_trust_level: Literal["declared"] = "declared"
    signature_status: Literal["absent", "unverified_metadata"]

    @field_validator(
        "import_payload_artifact_digest",
        "canonical_payload_digest",
        "referenced_artifact_digest",
        mode="before",
    )
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(
            value
        ) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value


class GenericEvidenceImportResult(BaseModel):
    """一次通用证据导入的结果：envelope、收据与 Evidence。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    envelope: GenericEvidenceEnvelope
    receipt: GenericEvidenceImportReceipt
    evidence: Evidence

    @model_validator(mode="after")
    def _validate_cross_field_bindings(self) -> "GenericEvidenceImportResult":
        envelope = self.envelope
        receipt = self.receipt
        evidence = self.evidence
        if evidence.subject_digest != envelope.subject_digest:
            raise ValueError(
                "evidence.subject_digest must equal envelope.subject_digest"
            )
        if evidence.producer != envelope.producer:
            raise ValueError(
                "evidence.producer must equal envelope.producer"
            )
        if evidence.kind != envelope.kind:
            raise ValueError("evidence.kind must equal envelope.kind")
        if evidence.status != envelope.status:
            raise ValueError("evidence.status must equal envelope.status")
        if evidence.artifact_digest != envelope.artifact_digest:
            raise ValueError(
                "evidence.artifact_digest must equal envelope.artifact_digest"
            )
        if evidence.collected_at != envelope.collected_at:
            raise ValueError(
                "evidence.collected_at must equal envelope.collected_at"
            )
        if evidence.trace_id != envelope.trace_id:
            raise ValueError(
                "evidence.trace_id must equal envelope.trace_id"
            )
        if evidence.trust_level != "declared":
            raise ValueError("evidence.trust_level must be declared")
        expected_source_ref = (
            f"generic_import:{receipt.import_payload_artifact_digest}"
        )
        if evidence.source_ref != expected_source_ref:
            raise ValueError(
                "evidence.source_ref must equal "
                "generic_import:<receipt.import_payload_artifact_digest>"
            )
        if (
            evidence.artifact_digest
            != receipt.referenced_artifact_digest
        ):
            raise ValueError(
                "evidence.artifact_digest must equal "
                "receipt.referenced_artifact_digest"
            )
        if (
            envelope.artifact_digest
            != receipt.referenced_artifact_digest
        ):
            raise ValueError(
                "envelope.artifact_digest must equal "
                "receipt.referenced_artifact_digest"
            )
        if receipt.claimed_trust_level != envelope.claimed_trust_level:
            raise ValueError(
                "receipt.claimed_trust_level must equal "
                "envelope.claimed_trust_level"
            )
        if envelope.signature is None:
            if receipt.signature_status != "absent":
                raise ValueError(
                    "receipt.signature_status must be absent when "
                    "envelope.signature is None"
                )
        elif receipt.signature_status != "unverified_metadata":
            raise ValueError(
                "receipt.signature_status must be unverified_metadata "
                "when envelope.signature is present"
            )
        evidence_id_input = (
            receipt.import_payload_artifact_digest
            + receipt.canonical_payload_digest
            + receipt.referenced_artifact_digest
        ).encode("ascii")
        expected_evidence_id = (
            "ev_import_"
            + hashlib.sha256(evidence_id_input).hexdigest()[:32]
        )
        if evidence.evidence_id != expected_evidence_id:
            raise ValueError(
                "evidence.evidence_id must be derived from receipt digests"
            )
        if (
            receipt.canonical_payload_digest
            != _sha256_digest(_canonical_envelope_bytes(self.envelope))
        ):
            raise ValueError(
                "receipt.canonical_payload_digest must equal the sha256 "
                "of the validated envelope canonical JSON"
            )
        return self


class GenericEvidenceImporter:
    """只导入精确原始字节的声明式通用证据导入器。"""

    @staticmethod
    def import_bytes(
        payload: bytes,
        *,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> GenericEvidenceImportResult:
        if type(payload) is not bytes:
            raise GenericEvidencePayloadError(_PAYLOAD_ERROR_MESSAGE)
        if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
            raise GenericEvidencePayloadError(_PAYLOAD_ERROR_MESSAGE)
        if (
            type(expected_subject_digest) is not str
            or _SHA256_DIGEST_RE.fullmatch(expected_subject_digest) is None
        ):
            raise GenericEvidencePayloadError(
                _SUBJECT_ARGUMENT_ERROR_MESSAGE
            )
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")

        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise GenericEvidencePayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None
        if text.startswith("\ufeff") or "\x00" in text:
            raise GenericEvidencePayloadError(_PAYLOAD_ERROR_MESSAGE)

        try:
            raw = json.loads(
                text,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (ValueError, TypeError):
            raise GenericEvidencePayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None
        if type(raw) is not dict:
            raise GenericEvidencePayloadError(_PAYLOAD_ERROR_MESSAGE)

        try:
            envelope = GenericEvidenceEnvelope.model_validate(raw)
        except ValidationError:
            raise GenericEvidencePayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None

        if envelope.subject_digest != expected_subject_digest:
            raise GenericEvidenceSubjectMismatch(_SUBJECT_MISMATCH_MESSAGE)

        referenced_digest = envelope.artifact_digest
        try:
            if not artifact_store.exists(referenced_digest):
                raise GenericEvidenceArtifactError(
                    _REFERENCED_ARTIFACT_ERROR_MESSAGE
                )
            if not artifact_store.verify(referenced_digest):
                raise GenericEvidenceArtifactError(
                    _REFERENCED_ARTIFACT_ERROR_MESSAGE
                )
            artifact_store.get_bytes(referenced_digest)
        except GenericEvidenceImportError:
            raise
        except Exception:
            raise GenericEvidenceArtifactError(
                _REFERENCED_ARTIFACT_ERROR_MESSAGE
            ) from None

        try:
            canonical_digest = _sha256_digest(
                _canonical_envelope_bytes(envelope)
            )
        except Exception:
            raise GenericEvidencePayloadError(
                _PAYLOAD_ERROR_MESSAGE
            ) from None

        try:
            raw_digest = artifact_store.put_bytes(payload)
            if not artifact_store.verify(raw_digest):
                raise GenericEvidenceArtifactError(
                    _RAW_PERSIST_ERROR_MESSAGE
                )
            stored = artifact_store.get_bytes(raw_digest)
        except GenericEvidenceImportError:
            raise
        except Exception:
            raise GenericEvidenceArtifactError(
                _RAW_PERSIST_ERROR_MESSAGE
            ) from None
        if stored != payload:
            raise GenericEvidenceArtifactError(_RAW_PERSIST_ERROR_MESSAGE)

        receipt = GenericEvidenceImportReceipt(
            import_payload_artifact_digest=raw_digest,
            canonical_payload_digest=canonical_digest,
            referenced_artifact_digest=referenced_digest,
            referenced_artifact_verified=True,
            claimed_trust_level=envelope.claimed_trust_level,
            effective_trust_level="declared",
            signature_status=(
                "absent"
                if envelope.signature is None
                else "unverified_metadata"
            ),
        )

        evidence_id_input = (
            raw_digest + canonical_digest + referenced_digest
        ).encode("ascii")
        evidence_id = (
            "ev_import_" + hashlib.sha256(evidence_id_input).hexdigest()[:32]
        )

        evidence = Evidence(
            evidence_id=evidence_id,
            subject_digest=envelope.subject_digest,
            kind=envelope.kind,
            producer=envelope.producer,
            artifact_digest=envelope.artifact_digest,
            source_ref=f"generic_import:{raw_digest}",
            trace_id=envelope.trace_id,
            status=envelope.status,
            trust_level="declared",
            collected_at=envelope.collected_at,
        )

        return GenericEvidenceImportResult(
            envelope=envelope,
            receipt=receipt,
            evidence=evidence,
        )
