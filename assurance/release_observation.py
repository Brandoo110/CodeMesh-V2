"""Manual/import-only release observations for the assurance workbench.

This module validates a caller-provided release observation and, for imports,
stores only the exact raw payload in the local content-addressed ArtifactStore.
It never talks to monitoring, cloud, deployment, network, or subprocess APIs.
All external observations remain declared trust; an artifact digest identifies
the deployed artifact and is never replaced by an import payload digest.
"""

from __future__ import annotations

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


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAYLOAD_BYTES = 1024 * 1024
_PAYLOAD_ERROR = "invalid release observation payload"
_SUBJECT_ERROR = "release observation subject digest mismatch"
_PERSISTENCE_ERROR = "release observation payload persistence failed"


class ReleaseObservationError(Exception):
    """Base error for release observation validation/import."""


class ReleaseObservationPayloadError(ReleaseObservationError):
    """Raw import bytes or the typed observation payload is invalid."""


class ReleaseObservationSubjectMismatch(ReleaseObservationError):
    """An imported observation is not bound to the expected subject digest."""


class ReleaseObservationArtifactError(ReleaseObservationError):
    """The local raw import payload could not be persisted or verified."""


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_digest(value: object) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


def _validate_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a str")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _canonical_observation_bytes(observation: "ReleaseObservation") -> bytes:
    return json.dumps(
        observation.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ReleaseWindow(BaseModel):
    """The explicitly supplied observation window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    started_at: AwareDatetime
    ended_at: AwareDatetime
    completeness: Literal["complete", "partial", "unknown"]

    @model_validator(mode="after")
    def _validate_order(self) -> "ReleaseWindow":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not be earlier than started_at")
        return self


class ReleaseMetrics(BaseModel):
    """Caller-supplied SLO and canary/control metric deltas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    slo_status: Literal["met", "breached", "unknown"]
    error_rate_delta: float | None = Field(
        default=None, allow_inf_nan=False
    )
    latency_p95_delta_ms: float | None = Field(
        default=None, allow_inf_nan=False
    )
    cost_delta_usd: float | None = Field(default=None, allow_inf_nan=False)


class AlertRecord(BaseModel):
    """A declared alert state and an optional external reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    state: Literal["clear", "active", "unknown"]
    ref: str | None = Field(default=None, min_length=1)

    @field_validator("ref")
    @classmethod
    def _reject_blank_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("ref must not be empty or whitespace-only")
        return value

    @model_validator(mode="after")
    def _validate_reference(self) -> "AlertRecord":
        if self.state == "clear" and self.ref is not None:
            raise ValueError("clear alert must not carry a ref")
        if self.state != "clear" and self.ref is None:
            raise ValueError("active or unknown alert requires a ref")
        return self


class RollbackRecord(BaseModel):
    """A declared rollback state and an optional operation reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    state: Literal["not_executed", "executed", "unknown"]
    ref: str | None = Field(default=None, min_length=1)

    @field_validator("ref")
    @classmethod
    def _reject_blank_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("ref must not be empty or whitespace-only")
        return value

    @model_validator(mode="after")
    def _validate_reference(self) -> "RollbackRecord":
        if self.state == "not_executed" and self.ref is not None:
            raise ValueError("not_executed rollback must not carry a ref")
        if self.state == "executed" and self.ref is None:
            raise ValueError("executed rollback requires a ref")
        if self.state == "unknown" and self.ref is None:
            raise ValueError("unknown rollback requires a ref")
        return self


class ReleaseObservation(BaseModel):
    """Immutable, caller-supplied post-release observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    observation_id: str = Field(min_length=1)
    subject_digest: str
    artifact_digest: str
    environment: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    cohort: Literal["canary", "control", "both"]
    control_deployment_id: str | None = Field(default=None, min_length=1)
    window: ReleaseWindow
    metrics: ReleaseMetrics
    alert: AlertRecord
    rollback: RollbackRecord
    outcome: Literal["CONFIRMED", "ROLLED_BACK", "INCONCLUSIVE"]
    source: Literal["manual", "import"]
    trust_level: Literal["declared"] = "declared"
    recorded_by: str = Field(min_length=1)
    recorded_at: AwareDatetime

    @field_validator("subject_digest", "artifact_digest")
    @classmethod
    def _validate_digests(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator(
        "observation_id", "environment", "deployment_id", "recorded_by"
    )
    @classmethod
    def _validate_identity_text(cls, value: str, info) -> str:
        return _validate_text(value, info.field_name)

    @field_validator("control_deployment_id")
    @classmethod
    def _validate_optional_control_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_text(value, "control_deployment_id")
        return value

    @model_validator(mode="after")
    def _validate_outcome_facts(self) -> "ReleaseObservation":
        if self.recorded_at < self.window.ended_at:
            raise ValueError("recorded_at must not be earlier than window ended_at")

        if self.outcome == "ROLLED_BACK":
            if self.rollback.state != "executed" or self.rollback.ref is None:
                raise ValueError(
                    "ROLLED_BACK requires rollback state executed and ref"
                )
            return self

        if self.rollback.state == "executed":
            raise ValueError(
                "executed rollback must have outcome ROLLED_BACK"
            )

        if self.outcome != "CONFIRMED":
            return self

        if self.window.completeness != "complete":
            raise ValueError("CONFIRMED requires a complete observation window")
        if self.cohort != "both" or self.control_deployment_id is None:
            raise ValueError(
                "CONFIRMED requires canary/control and control deployment ID"
            )
        if self.metrics.slo_status != "met":
            raise ValueError("CONFIRMED requires SLO status met")
        if any(
            value is None
            for value in (
                self.metrics.error_rate_delta,
                self.metrics.latency_p95_delta_ms,
                self.metrics.cost_delta_usd,
            )
        ):
            raise ValueError("CONFIRMED requires all metric deltas")
        if self.alert.state != "clear":
            raise ValueError("CONFIRMED requires clear alerts")
        if self.rollback.state != "not_executed":
            raise ValueError("CONFIRMED requires rollback not_executed")
        return self


class ReleaseObservationImportReceipt(BaseModel):
    """Digest receipt for one exact raw observation import."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    payload_digest: str
    canonical_payload_digest: str
    artifact_digest: str
    effective_trust_level: Literal["declared"] = "declared"

    @field_validator(
        "payload_digest", "canonical_payload_digest", "artifact_digest"
    )
    @classmethod
    def _validate_digests(cls, value: str) -> str:
        return _validate_digest(value)


class ReleaseObservationImportResult(BaseModel):
    """The typed observation and its immutable import receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    observation: ReleaseObservation
    receipt: ReleaseObservationImportReceipt

    @model_validator(mode="after")
    def _validate_bindings(self) -> "ReleaseObservationImportResult":
        if self.observation.source != "import":
            raise ValueError("import result requires observation source import")
        if self.receipt.artifact_digest != self.observation.artifact_digest:
            raise ValueError(
                "receipt artifact_digest must equal observation artifact_digest"
            )
        expected_canonical_digest = _sha256_digest(
            _canonical_observation_bytes(self.observation)
        )
        if self.receipt.canonical_payload_digest != expected_canonical_digest:
            raise ValueError(
                "receipt canonical_payload_digest must equal observation canonical digest"
            )
        if self.receipt.effective_trust_level != "declared":
            raise ValueError("import trust must remain declared")
        return self


class ReleaseObservationImporter:
    """Import exact JSON bytes without external monitoring or deployment I/O."""

    @staticmethod
    def import_bytes(
        payload: bytes,
        *,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> ReleaseObservationImportResult:
        if type(payload) is not bytes or not payload:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR)
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR)
        try:
            expected_subject_digest = _validate_digest(expected_subject_digest)
        except ValueError:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR) from None
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")

        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR) from None
        if text.startswith("\ufeff") or "\x00" in text:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR)
        try:
            raw = json.loads(
                text,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (TypeError, ValueError):
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR) from None
        if type(raw) is not dict:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR)
        try:
            observation = ReleaseObservation.model_validate(raw)
        except ValidationError:
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR) from None
        if observation.subject_digest != expected_subject_digest:
            raise ReleaseObservationSubjectMismatch(_SUBJECT_ERROR)
        if observation.source != "import":
            raise ReleaseObservationPayloadError(_PAYLOAD_ERROR)

        canonical_digest = _sha256_digest(_canonical_observation_bytes(observation))
        payload_digest = _sha256_digest(payload)
        try:
            stored_digest = artifact_store.put_bytes(payload)
            if stored_digest != payload_digest or not artifact_store.verify(payload_digest):
                raise ReleaseObservationArtifactError(_PERSISTENCE_ERROR)
            if artifact_store.get_bytes(payload_digest) != payload:
                raise ReleaseObservationArtifactError(_PERSISTENCE_ERROR)
        except ReleaseObservationArtifactError:
            raise
        except Exception:
            raise ReleaseObservationArtifactError(_PERSISTENCE_ERROR) from None

        receipt = ReleaseObservationImportReceipt(
            payload_digest=payload_digest,
            canonical_payload_digest=canonical_digest,
            artifact_digest=observation.artifact_digest,
            effective_trust_level="declared",
        )
        return ReleaseObservationImportResult(
            observation=observation,
            receipt=receipt,
        )
