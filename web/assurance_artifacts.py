"""Case-bound access to the shared Evidence artifact authorization closure.

The Web layer owns the Case boundary only.  Parsing, one-level manifest
expansion, bounded content-addressed reads, and integrity checks live in
``assurance.evidence_artifacts`` so other callers cannot accidentally create a
second, weaker artifact policy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance.artifacts import ArtifactStore
from assurance.contracts import Evidence
from assurance.evidence_artifacts import (
    ArtifactReference,
    EvidenceArtifactError,
    EvidenceArtifactResolver,
)
from web.assurance_store import (
    AssuranceWebNotFoundError,
    AssuranceWebRepository,
)


class EvidenceArtifactIndex(BaseModel):
    """Deterministic, authorized artifact entries for one Case Evidence row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    evidence_kind: str = Field(min_length=1)
    artifacts: tuple[ArtifactReference, ...] = Field(min_length=1)


class VerifiedArtifact(BaseModel):
    """One Case-bound artifact's raw bytes and verified integrity status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    digest: str
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    byte_size: int = Field(strict=True, ge=0)
    data: bytes
    media_type: Literal["text/plain"] = "text/plain"
    integrity_status: Literal["SHA-256 integrity verified"] = (
        "SHA-256 integrity verified"
    )
    role: Literal["top_level", "document", "stdout", "stderr"]
    path: str | None = None
    command_id: str | None = None
    stream: Literal["stdout", "stderr"] | None = None


class AssuranceArtifactReader:
    """Read only Case-bound Evidence artifacts through the shared resolver."""

    def __init__(
        self,
        repository: AssuranceWebRepository,
        artifact_store: ArtifactStore,
    ) -> None:
        if not isinstance(repository, AssuranceWebRepository):
            raise TypeError("repository must be an AssuranceWebRepository")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        self._repository = repository
        self._artifact_store = artifact_store

    def list_artifacts(
        self, case_id: str, evidence_id: str
    ) -> EvidenceArtifactIndex:
        evidence = self._evidence_for_case(case_id, evidence_id)
        try:
            authorized = EvidenceArtifactResolver.index(
                evidence,
                artifact_store=self._artifact_store,
                subject_digest=evidence.subject_digest,
            )
        except EvidenceArtifactError:
            raise self._not_found() from None
        try:
            return EvidenceArtifactIndex(
                case_id=case_id,
                evidence_id=evidence_id,
                evidence_kind=authorized.evidence_kind,
                artifacts=authorized.artifacts,
            )
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    def read_artifact(
        self, case_id: str, evidence_id: str, digest: str
    ) -> VerifiedArtifact:
        evidence = self._evidence_for_case(case_id, evidence_id)
        try:
            authorized = EvidenceArtifactResolver.index(
                evidence,
                artifact_store=self._artifact_store,
                subject_digest=evidence.subject_digest,
            )
            verified = EvidenceArtifactResolver.read(
                authorized,
                evidence=evidence,
                subject_digest=evidence.subject_digest,
                artifact_store=self._artifact_store,
                digest=digest,
            )
        except EvidenceArtifactError:
            raise self._not_found() from None
        try:
            return VerifiedArtifact(
                case_id=case_id,
                evidence_id=evidence_id,
                digest=verified.digest,
                kind=verified.kind,
                label=verified.label,
                byte_size=verified.byte_size,
                data=verified.data,
                media_type=verified.media_type,
                integrity_status=verified.integrity_status,
                role=verified.role,
                path=verified.path,
                command_id=verified.command_id,
                stream=verified.stream,
            )
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    def _evidence_for_case(self, case_id: str, evidence_id: str) -> Evidence:
        try:
            return self._repository.get_authoritative_evidence(case_id, evidence_id)
        except AssuranceWebNotFoundError:
            raise
        except Exception:
            raise self._not_found() from None

    @staticmethod
    def _not_found() -> AssuranceWebNotFoundError:
        return AssuranceWebNotFoundError("artifact is unavailable")


__all__ = [
    "ArtifactReference",
    "AssuranceArtifactReader",
    "EvidenceArtifactIndex",
    "VerifiedArtifact",
]
