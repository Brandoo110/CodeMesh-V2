"""Local-first CI Evidence Adapter (V2-P8-02A).

The adapter accepts exact provider-produced JSON bytes and never performs
provider/network, subprocess, or credential I/O.  It intentionally uses the
local ArtifactStore for raw and referenced bytes.  A provider's claimed trust
is retained as metadata only; imported Evidence is always ``declared`` until
a separately implemented live observer establishes a stronger trust boundary.
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

from ..artifacts import ArtifactStore
from ..contracts import Evidence


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_TEXT_BYTES = 512
_TRUST_LEVELS = Literal[
    "declared", "observed", "deterministic", "inferred", "human_attested"
]
_EVIDENCE_STATUSES = Literal[
    "success", "failure", "error", "cancelled"
]


class CIImportError(Exception):
    """Base error for strict CI evidence import."""


class CIPayloadError(CIImportError):
    """Raw bytes, JSON, or CI report schema is invalid."""


class CISubjectMismatch(CIImportError):
    """The report subject is not the expected current subject."""


class CIArtifactError(CIImportError):
    """A referenced or persisted artifact is missing or corrupt."""


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


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be exactly a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"{field_name} is too long")
    return value


def _validate_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase sha256:<64 hex> digest"
        )
    return value


def _canonical_report_bytes(report: "CIReport") -> bytes:
    return json.dumps(
        report.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _evidence_status(report: "CIReport") -> str:
    status = report.status.casefold()
    conclusion = report.conclusion.casefold()
    if conclusion == "success" and status in {"completed", "success"}:
        return "success"
    if status in {"cancelled", "canceled"} or conclusion in {
        "cancelled",
        "canceled",
    }:
        return "cancelled"
    if status in {"failure", "failed"} or conclusion in {
        "failure",
        "failed",
    }:
        return "failure"
    return "error"


class CIReport(BaseModel):
    """A bounded, provider-neutral CI report declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    provider: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    commit_sha: str = Field(min_length=1)
    subject_digest: str
    status: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)
    started_at: AwareDatetime
    completed_at: AwareDatetime
    artifact_digest: str
    artifact_name: str = Field(min_length=1)
    claimed_trust_level: _TRUST_LEVELS = "declared"

    @field_validator(
        "provider",
        "run_id",
        "repository",
        "commit_sha",
        "status",
        "conclusion",
        "artifact_name",
        mode="before",
    )
    @classmethod
    def _validate_text_fields(cls, value: object, info) -> str:
        return _validate_text(value, info.field_name)

    @field_validator("subject_digest", "artifact_digest", mode="before")
    @classmethod
    def _validate_digests(cls, value: object, info) -> str:
        return _validate_digest(value, info.field_name)

    @model_validator(mode="after")
    def _validate_time_order(self) -> "CIReport":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be >= started_at")
        return self


class CIReceipt(BaseModel):
    """Immutable import receipt retaining claim and effective trust."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    raw_payload_artifact_digest: str
    canonical_report_digest: str
    referenced_artifact_digest: str
    referenced_artifact_verified: Literal[True] = True
    claimed_trust_level: _TRUST_LEVELS
    effective_trust_level: Literal["declared"] = "declared"
    evidence_status: _EVIDENCE_STATUSES

    @field_validator(
        "raw_payload_artifact_digest",
        "canonical_report_digest",
        "referenced_artifact_digest",
        mode="before",
    )
    @classmethod
    def _validate_digests(cls, value: object, info) -> str:
        return _validate_digest(value, info.field_name)


class CIResult(BaseModel):
    """CI report, immutable receipt, and subject-bound Evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    report: CIReport
    receipt: CIReceipt
    evidence: Evidence

    @model_validator(mode="after")
    def _validate_bindings(self) -> "CIResult":
        report = self.report
        receipt = self.receipt
        evidence = self.evidence
        if evidence.subject_digest != report.subject_digest:
            raise ValueError("evidence subject must equal report subject")
        if evidence.kind != "ci_run":
            raise ValueError("evidence kind must be ci_run")
        expected_producer = f"adapter.ci.{report.provider}"
        if evidence.producer != expected_producer:
            raise ValueError("evidence producer must match report provider")
        if evidence.artifact_digest != report.artifact_digest:
            raise ValueError("evidence artifact must equal report artifact")
        if receipt.claimed_trust_level != report.claimed_trust_level:
            raise ValueError(
                "receipt claimed trust must equal report claimed trust"
            )
        expected_status = _evidence_status(report)
        if receipt.evidence_status != expected_status:
            raise ValueError(
                "receipt evidence status must equal report evidence status"
            )
        if evidence.status != receipt.evidence_status:
            raise ValueError("evidence status must equal receipt status")
        if evidence.trust_level != "declared":
            raise ValueError("CI evidence trust must remain declared")
        if receipt.effective_trust_level != "declared":
            raise ValueError("effective CI trust must remain declared")
        if receipt.referenced_artifact_digest != report.artifact_digest:
            raise ValueError("receipt artifact must equal report artifact")
        if receipt.canonical_report_digest != _sha256_digest(
            _canonical_report_bytes(report)
        ):
            raise ValueError("canonical_report_digest must be recomputed")
        expected_source_ref = (
            f"ci_run:{report.provider}:{report.run_id}:"
            f"{receipt.raw_payload_artifact_digest}"
        )
        if evidence.source_ref != expected_source_ref:
            raise ValueError("evidence source_ref is not subject-bound")
        expected_evidence_id = "ev_ci_" + hashlib.sha256(
            (
                receipt.raw_payload_artifact_digest
                + receipt.canonical_report_digest
                + receipt.referenced_artifact_digest
            ).encode("ascii")
        ).hexdigest()[:32]
        if evidence.evidence_id != expected_evidence_id:
            raise ValueError("evidence_id must be derived from receipt digests")
        if evidence.collected_at != report.completed_at:
            raise ValueError("evidence collected_at must equal completed_at")
        return self


# Descriptive aliases for callers that prefer the full domain name.
CIEvidenceReceipt = CIReceipt
CIEvidenceResult = CIResult


class CIEvidenceAdapter:
    """Strict offline importer for exact CI report bytes."""

    @staticmethod
    def import_bytes(
        payload: bytes,
        *,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> CIResult:
        if type(payload) is not bytes or not payload:
            raise CIPayloadError("invalid CI report payload")
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise CIPayloadError("invalid CI report payload")
        if (
            type(expected_subject_digest) is not str
            or _SHA256_DIGEST_RE.fullmatch(expected_subject_digest) is None
        ):
            raise CIPayloadError("invalid expected subject digest")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")

        try:
            text = payload.decode("utf-8")
            if text.startswith("\ufeff") or "\x00" in text:
                raise ValueError("invalid text")
            raw = json.loads(
                text,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
            if type(raw) is not dict:
                raise ValueError("CI report must be an object")
            report = CIReport.model_validate(raw)
        except (UnicodeDecodeError, RecursionError, TypeError, ValueError, ValidationError):
            raise CIPayloadError("invalid CI report payload") from None

        if report.subject_digest != expected_subject_digest:
            raise CISubjectMismatch("CI report subject digest mismatch")

        try:
            if not artifact_store.exists(report.artifact_digest):
                raise CIArtifactError("referenced CI artifact is missing or invalid")
            if not artifact_store.verify(report.artifact_digest):
                raise CIArtifactError("referenced CI artifact is missing or invalid")
            artifact_store.get_bytes(report.artifact_digest)
        except CIImportError:
            raise
        except Exception:
            raise CIArtifactError(
                "referenced CI artifact is missing or invalid"
            ) from None

        raw_digest = _sha256_digest(payload)
        try:
            stored_digest = artifact_store.put_bytes(payload)
            if stored_digest != raw_digest:
                raise CIArtifactError("CI report raw persistence failed")
            if not artifact_store.verify(raw_digest):
                raise CIArtifactError("CI report raw persistence failed")
            stored = artifact_store.get_bytes(raw_digest)
        except CIImportError:
            raise
        except Exception:
            raise CIArtifactError("CI report raw persistence failed") from None
        if stored != payload:
            raise CIArtifactError("CI report raw persistence failed")

        evidence_status = _evidence_status(report)
        canonical_digest = _sha256_digest(_canonical_report_bytes(report))
        receipt = CIReceipt(
            raw_payload_artifact_digest=raw_digest,
            canonical_report_digest=canonical_digest,
            referenced_artifact_digest=report.artifact_digest,
            claimed_trust_level=report.claimed_trust_level,
            effective_trust_level="declared",
            evidence_status=evidence_status,
        )
        evidence_id = "ev_ci_" + hashlib.sha256(
            (
                raw_digest + canonical_digest + report.artifact_digest
            ).encode("ascii")
        ).hexdigest()[:32]
        evidence = Evidence(
            evidence_id=evidence_id,
            subject_digest=report.subject_digest,
            kind="ci_run",
            producer=f"adapter.ci.{report.provider}",
            artifact_digest=report.artifact_digest,
            source_ref=f"ci_run:{report.provider}:{report.run_id}:{raw_digest}",
            trace_id=None,
            status=evidence_status,
            trust_level="declared",
            collected_at=report.completed_at,
        )
        return CIResult(report=report, receipt=receipt, evidence=evidence)


__all__ = [
    "CIArtifactError",
    "CIEvidenceAdapter",
    "CIEvidenceReceipt",
    "CIEvidenceResult",
    "CIImportError",
    "CIPayloadError",
    "CIReport",
    "CIReceipt",
    "CIResult",
    "CISubjectMismatch",
]
