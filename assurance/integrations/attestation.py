"""Offline in-toto Statement v1 and DSSE preparation for CodeMesh.

This module is deliberately an exporter, not a signer, verifier, or publisher.
It produces a subject-bound Statement and a DSSE envelope using canonical JSON.
An injected signer may sign the DSSE PAE, but the result is never reported as
verified and is never published by this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)


ATTESTATION_PAYLOAD_TYPE = "application/vnd.in-toto+json"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
DEFAULT_PREDICATE_TYPE = "https://codemesh.dev/assurance/v1"

_RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


class AttestationExportError(ValueError):
    """Raised when an attestation cannot be prepared without ambiguity."""


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects post-validation mutation."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("attestation mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return _FrozenDict(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


class DSSERawSigner(Protocol):
    """Minimal injected signing seam; no cryptographic dependency is assumed."""

    keyid: str | None

    def sign(self, message: bytes) -> bytes:
        """Sign the exact DSSE PAE bytes and return raw signature bytes."""


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AttestationExportError("value is not canonical JSON") from exc


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _normalise_subject_digest(value: object) -> str:
    if type(value) is not str:
        raise AttestationExportError("subject_digest must be a string")
    prefixed = _PREFIXED_SHA256_RE.fullmatch(value)
    if prefixed is not None:
        return prefixed.group(1)
    if _RAW_SHA256_RE.fullmatch(value) is not None:
        return value
    raise AttestationExportError(
        "subject_digest must be sha256:<64 lowercase hex> or raw lowercase hex"
    )


def _nonblank_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")
    return value


def _type_uri(value: object, field_name: str) -> str:
    result = _nonblank_text(value, field_name)
    if any(character.isspace() or ord(character) < 32 for character in result):
        raise ValueError(f"{field_name} must not contain whitespace or controls")
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", result)
    if match is None:
        raise ValueError(f"{field_name} must be an absolute RFC 3986 URI")
    scheme = match.group(1)
    if scheme != scheme.lower():
        raise ValueError(f"{field_name} scheme must be lowercase")
    remainder = result[len(scheme) + 1 :]
    authority = ""
    if remainder.startswith("//"):
        authority = re.split(r"[/\\?#]", remainder[2:], maxsplit=1)[0]
        if not authority:
            raise ValueError(f"{field_name} authority must not be empty")
        if authority != authority.lower():
            raise ValueError(f"{field_name} authority must be lowercase")
    if scheme in {"http", "https"} and not authority:
        raise ValueError(f"{field_name} HTTP URI must contain an authority")
    if not remainder:
        raise ValueError(f"{field_name} must contain a type identifier")
    return result


def _decode_base64(value: object, field_name: str = "base64 value") -> bytes:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a nonempty base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid standard base64") from exc


class InTotoSubject(BaseModel):
    """A resource descriptor with exactly one raw SHA-256 digest."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    name: str = Field(min_length=1)
    digest: _FrozenDict

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str:
        return _nonblank_text(value, "name")

    @field_validator("digest", mode="before")
    @classmethod
    def _validate_digest(cls, value: object) -> _FrozenDict:
        if type(value) is not dict or set(value) != {"sha256"}:
            raise ValueError("digest must contain exactly one sha256 key")
        digest = value.get("sha256")
        if type(digest) is not str or _RAW_SHA256_RE.fullmatch(digest) is None:
            raise ValueError("digest.sha256 must be raw lowercase 64-hex")
        return _FrozenDict(value)


class InTotoStatement(BaseModel):
    """Canonical in-toto Statement v1 payload."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    type_: Literal[IN_TOTO_STATEMENT_TYPE] = Field(
        default=IN_TOTO_STATEMENT_TYPE,
        alias="_type",
    )
    subject: tuple[InTotoSubject, ...]
    predicate_type: str = Field(
        default=DEFAULT_PREDICATE_TYPE,
        alias="predicateType",
    )
    predicate: _FrozenDict = Field(default_factory=_FrozenDict)

    @field_validator("subject", mode="before")
    @classmethod
    def _validate_subject_collection(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) not in (tuple, list):
            raise ValueError("subject must be a tuple or list")
        if not value:
            raise ValueError("subject must contain at least one resource")
        return value

    @field_validator("predicate_type", mode="before")
    @classmethod
    def _validate_predicate_type(cls, value: object) -> str:
        return _type_uri(value, "predicateType")

    @field_validator("predicate", mode="before")
    @classmethod
    def _validate_predicate(cls, value: object) -> _FrozenDict:
        if type(value) is not dict:
            raise ValueError("predicate must be a JSON object")
        # Validate serializability now so canonical export cannot fail later.
        _canonical_json_bytes(value)
        frozen = _freeze_json(value)
        if not isinstance(frozen, _FrozenDict):  # pragma: no cover - defensive
            raise ValueError("predicate must be a JSON object")
        return frozen

    @model_validator(mode="after")
    def _validate_subject_names(self) -> "InTotoStatement":
        names = [subject.name for subject in self.subject]
        if len(names) != len(set(names)):
            raise ValueError("subject names must be unique")
        return self


class DSSESignature(BaseModel):
    """One base64-encoded DSSE signature."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keyid: str | None = None
    sig: str

    @field_validator("keyid", mode="before")
    @classmethod
    def _validate_keyid(cls, value: object) -> str | None:
        if value is None:
            return None
        return _nonblank_text(value, "keyid")

    @field_validator("sig", mode="before")
    @classmethod
    def _validate_signature(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("sig must be a string")
        decoded = _decode_base64(value, "sig")
        if not decoded:
            raise ValueError("sig must not be empty")
        return value


class DSSEEnvelope(BaseModel):
    """The recommended JSON DSSE envelope, possibly unsigned/prepared."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )

    payload: str
    payload_type: str = Field(alias="payloadType")
    signatures: tuple[DSSESignature, ...] = ()

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload(cls, value: object) -> str:
        _decode_base64(value, "payload")
        return value  # type: ignore[return-value]

    @field_validator("payload_type", mode="before")
    @classmethod
    def _validate_payload_type(cls, value: object) -> str:
        return _nonblank_text(value, "payloadType")

    @field_validator("signatures", mode="before")
    @classmethod
    def _validate_signatures(cls, value: object) -> object:
        if type(value) not in (tuple, list):
            raise ValueError("signatures must be a tuple or list")
        return value


class AttestationReceipt(BaseModel):
    """Local preparation receipt; verified and published are always false."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    payload_digest: str
    pae_digest: str
    signing_status: Literal["prepared", "signature_present"]
    signature_present: StrictBool
    verified: Literal[False] = False
    published: Literal[False] = False

    @field_validator("payload_digest", "pae_digest", mode="before")
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if type(value) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("digest must be sha256:<64 lowercase hex>")
        return value


class AttestationExportResult(BaseModel):
    """Statement, DSSE envelope, and a self-consistent non-publication receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: InTotoStatement
    envelope: DSSEEnvelope
    receipt: AttestationReceipt

    @model_validator(mode="after")
    def _bind_payload_and_receipt(self) -> "AttestationExportResult":
        if self.receipt.verified is not False:
            raise ValueError("attestation receipt must not claim verified")
        if self.receipt.published is not False:
            raise ValueError("attestation receipt must not claim published")
        payload = _decode_base64(self.envelope.payload, "payload")
        expected_payload = canonical_statement_bytes(self.statement)
        if payload != expected_payload:
            raise ValueError("DSSE payload does not match canonical Statement")
        if self.envelope.payload_type != ATTESTATION_PAYLOAD_TYPE:
            raise ValueError("unsupported attestation payload type")
        pae = dsse_pae(self.envelope.payload_type, payload)
        if self.receipt.payload_digest != _sha256_digest(payload):
            raise ValueError("receipt payload_digest does not match payload")
        if self.receipt.pae_digest != _sha256_digest(pae):
            raise ValueError("receipt pae_digest does not match PAE")
        has_signature = bool(self.envelope.signatures)
        expected_status = "signature_present" if has_signature else "prepared"
        if self.receipt.signing_status != expected_status:
            raise ValueError("receipt signing_status does not match envelope")
        if self.receipt.signature_present is not has_signature:
            raise ValueError("receipt signature_present flag does not match envelope")
        return self


def canonical_statement_bytes(statement: InTotoStatement) -> bytes:
    """Serialize a Statement with stable UTF-8 canonical JSON bytes."""

    if type(statement) is not InTotoStatement:
        raise TypeError("statement must be an exact InTotoStatement")
    return _canonical_json_bytes(
        statement.model_dump(mode="json", by_alias=True)
    )


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """Return the exact DSSE v1 pre-authentication encoding bytes."""

    if type(payload_type) is not str or not payload_type:
        raise AttestationExportError("payload_type must be a nonempty string")
    if type(payload) is not bytes:
        raise AttestationExportError("payload must be exactly bytes")
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def _sign(signer: DSSERawSigner, pae: bytes) -> DSSESignature:
    sign = getattr(signer, "sign", None)
    if not callable(sign):
        raise AttestationExportError("signer must expose sign(bytes)")
    try:
        signature = sign(pae)
    except Exception as exc:
        raise AttestationExportError("signer failed") from exc
    if type(signature) is not bytes or not signature:
        raise AttestationExportError("signer must return nonempty bytes")

    keyid = getattr(signer, "keyid", None)
    if callable(keyid):
        try:
            keyid = keyid()
        except Exception as exc:
            raise AttestationExportError("signer keyid failed") from exc
    if keyid is not None and (type(keyid) is not str or not keyid.strip()):
        raise AttestationExportError("signer keyid must be a nonblank string")
    encoded = base64.b64encode(signature).decode("ascii")
    return DSSESignature(keyid=keyid, sig=encoded)


class InTotoAttestationExporter:
    """Prepare a Statement and DSSE envelope without network or publication."""

    @staticmethod
    def export(
        subject_digest: str,
        *,
        subject_name: str = "_",
        predicate_type: str = DEFAULT_PREDICATE_TYPE,
        predicate: Mapping[str, Any] | None = None,
        signer: DSSERawSigner | None = None,
    ) -> AttestationExportResult:
        try:
            raw_digest = _normalise_subject_digest(subject_digest)
            statement = InTotoStatement(
                subject=(
                    InTotoSubject(
                        name=subject_name,
                        digest={"sha256": raw_digest},
                    ),
                ),
                predicate_type=predicate_type,
                predicate={} if predicate is None else dict(predicate),
            )
            payload = canonical_statement_bytes(statement)
            encoded_payload = base64.b64encode(payload).decode("ascii")
            pae = dsse_pae(ATTESTATION_PAYLOAD_TYPE, payload)
            signatures = () if signer is None else (_sign(signer, pae),)
            envelope = DSSEEnvelope(
                payload=encoded_payload,
                payload_type=ATTESTATION_PAYLOAD_TYPE,
                signatures=signatures,
            )
            receipt = AttestationReceipt(
                payload_digest=_sha256_digest(payload),
                pae_digest=_sha256_digest(pae),
                signing_status=(
                    "signature_present" if signatures else "prepared"
                ),
                signature_present=bool(signatures),
            )
            return AttestationExportResult(
                statement=statement,
                envelope=envelope,
                receipt=receipt,
            )
        except AttestationExportError:
            raise
        except Exception as exc:
            raise AttestationExportError("attestation export failed") from exc


__all__ = (
    "ATTESTATION_PAYLOAD_TYPE",
    "AttestationExportError",
    "AttestationExportResult",
    "AttestationReceipt",
    "DEFAULT_PREDICATE_TYPE",
    "DSSEEnvelope",
    "DSSERawSigner",
    "DSSESignature",
    "IN_TOTO_STATEMENT_TYPE",
    "InTotoAttestationExporter",
    "InTotoStatement",
    "InTotoSubject",
    "canonical_statement_bytes",
    "dsse_pae",
)
