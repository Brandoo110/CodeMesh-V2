"""Case-bound, integrity-verified access to assurance artifact bytes.

The reader is deliberately a small deep module.  Callers provide only a Case,
Evidence ID, and (for reads) a digest; the repository projection supplies the
authorization boundary and the public ``ArtifactStore.get_bytes`` API supplies
content-addressed reads.  Manifest parsing is limited to the contracts already
used by the three deterministic collectors.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pydantic import ValidationError

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandBatchSnapshot, CommandObservation
from assurance.contracts import Evidence
from assurance.digests import normalize_repo_path
from assurance.intake import IntakeDocument, IntakeNotice, IntakeSnapshot
from web.assurance_store import (
    AssuranceWebNotFoundError,
    AssuranceWebRepository,
)


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INTEGRITY_STATUS = "SHA-256 integrity verified"
_TEXT_MEDIA_TYPE = "text/plain"


class _InvalidArtifact(ValueError):
    """Internal sentinel for any malformed or unauthorized artifact."""


class ArtifactReference(BaseModel):
    """A path-free, authorized entry in an Evidence artifact index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    digest: str
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    byte_size: StrictInt = Field(ge=0)
    media_type: Literal["text/plain"] = _TEXT_MEDIA_TYPE
    integrity_status: Literal["SHA-256 integrity verified"] = _INTEGRITY_STATUS
    role: Literal["top_level", "document", "stdout", "stderr"]
    path: str | None = None
    command_id: str | None = None
    stream: Literal["stdout", "stderr"] | None = None

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("digest must be a lowercase sha256 digest")
        return value

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("path must be a str or None")
        if normalize_repo_path(value) != value:
            raise ValueError("path must be a canonical repository-relative path")
        return value

    @field_validator("command_id", mode="before")
    @classmethod
    def _validate_command_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError("command_id must be a nonblank string")
        return value


class EvidenceArtifactIndex(BaseModel):
    """Deterministic, authorized artifact entries for one Case Evidence row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    evidence_kind: str = Field(min_length=1)
    artifacts: tuple[ArtifactReference, ...]

class VerifiedArtifact(BaseModel):
    """One authorized artifact's raw bytes and integrity status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    digest: str
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    byte_size: StrictInt = Field(ge=0)
    data: bytes
    media_type: Literal["text/plain"] = _TEXT_MEDIA_TYPE
    integrity_status: Literal["SHA-256 integrity verified"] = _INTEGRITY_STATUS
    role: Literal["top_level", "document", "stdout", "stderr"]
    path: str | None = None
    command_id: str | None = None
    stream: Literal["stdout", "stderr"] | None = None

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("digest must be a lowercase sha256 digest")
        return value

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: bytes) -> bytes:
        if type(value) is not bytes:
            raise ValueError("data must be bytes")
        return value

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or normalize_repo_path(value) != value:
            raise ValueError("path must be a canonical repository-relative path")
        return value

class AssuranceArtifactReader:
    """Read only Case-bound Evidence artifacts through public repository APIs."""

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
        top_digest = evidence.artifact_digest
        top_bytes = self._verified_bytes(top_digest)
        top = self._reference(
            digest=top_digest,
            kind=evidence.kind,
            label=self._top_label(evidence.kind),
            byte_size=len(top_bytes),
            role="top_level",
        )

        if evidence.kind == "git_snapshot":
            references = (top,)
        elif evidence.kind == "intake_documents":
            documents = self._parse_intake_manifest(
                top_digest, top_bytes, evidence.subject_digest
            )
            references = (top,) + tuple(
                self._document_reference(document)
                for document in documents
            )
        elif evidence.kind == "command_batch":
            observations = self._parse_command_manifest(
                top_digest, top_bytes, evidence.subject_digest
            )
            references = (top,) + tuple(
                reference
                for observation in observations
                for reference in self._command_references(observation)
            )
        else:
            # Unknown kinds are intentionally opaque: only the Evidence's own
            # digest is authorized, even if its bytes happen to resemble JSON.
            references = (top,)

        return EvidenceArtifactIndex(
            case_id=case_id,
            evidence_id=evidence_id,
            evidence_kind=evidence.kind,
            artifacts=references,
        )

    def read_artifact(
        self, case_id: str, evidence_id: str, digest: str
    ) -> VerifiedArtifact:
        index = self.list_artifacts(case_id, evidence_id)
        reference = next(
            (item for item in index.artifacts if item.digest == digest), None
        )
        if reference is None:
            raise self._not_found()
        data = self._verified_bytes(digest)
        if len(data) != reference.byte_size:
            raise self._not_found()
        try:
            return VerifiedArtifact(
                case_id=case_id,
                evidence_id=evidence_id,
                digest=digest,
                kind=reference.kind,
                label=reference.label,
                byte_size=len(data),
                data=data,
                media_type=reference.media_type,
                integrity_status=reference.integrity_status,
                role=reference.role,
                path=reference.path,
                command_id=reference.command_id,
                stream=reference.stream,
            )
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    def _evidence_for_case(self, case_id: str, evidence_id: str) -> Evidence:
        try:
            projection = self._repository.get_change(case_id)
        except AssuranceWebNotFoundError:
            raise
        except Exception:
            raise self._not_found() from None
        try:
            case = projection["case"]
            if evidence_id not in case["evidence_refs"]:
                raise self._not_found()
            item = next(
                evidence
                for evidence in projection["evidence"]
                if evidence.get("evidence_id") == evidence_id
            )
            evidence = Evidence.model_validate(item)
            if evidence.subject_digest != case["subject_digest"]:
                raise self._not_found()
            return evidence
        except AssuranceWebNotFoundError:
            raise
        except (
            AttributeError,
            KeyError,
            StopIteration,
            TypeError,
            ValueError,
            ValidationError,
        ):
            raise self._not_found() from None

    def _verified_bytes(self, digest: str) -> bytes:
        if type(digest) is not str or _SHA256_DIGEST_RE.fullmatch(digest) is None:
            raise self._not_found()
        try:
            data = self._artifact_store.get_bytes(digest)
        except Exception:
            raise self._not_found() from None
        if type(data) is not bytes:
            raise self._not_found()
        # ArtifactStore already verifies this invariant.  Rechecking the
        # public result keeps the reader fail-closed if a test or adapter wraps
        # that public seam incorrectly.
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise self._not_found()
        return data

    def _reference(
        self,
        *,
        digest: str,
        kind: str,
        label: str,
        byte_size: int,
        role: Literal["top_level", "document", "stdout", "stderr"],
        path: str | None = None,
        command_id: str | None = None,
        stream: Literal["stdout", "stderr"] | None = None,
    ) -> ArtifactReference:
        try:
            return ArtifactReference(
                digest=digest,
                kind=kind,
                label=label,
                byte_size=byte_size,
                role=role,
                path=path,
                command_id=command_id,
                stream=stream,
            )
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    def _document_reference(self, document: IntakeDocument) -> ArtifactReference:
        data = self._verified_bytes(document.artifact_digest)
        if len(data) != document.byte_size:
            raise self._not_found()
        return self._reference(
            digest=document.artifact_digest,
            kind=document.kind,
            label=document.path,
            byte_size=len(data),
            role="document",
            path=document.path,
        )

    def _command_references(
        self, observation: CommandObservation
    ) -> tuple[ArtifactReference, ArtifactReference]:
        stdout = self._verified_bytes(observation.stdout_artifact_digest)
        stderr = self._verified_bytes(observation.stderr_artifact_digest)
        if len(stdout) != observation.stdout_bytes or len(stderr) != observation.stderr_bytes:
            raise self._not_found()
        return (
            self._reference(
                digest=observation.stdout_artifact_digest,
                kind="stdout",
                label=f"{observation.command_id}:stdout",
                byte_size=len(stdout),
                role="stdout",
                command_id=observation.command_id,
                stream="stdout",
            ),
            self._reference(
                digest=observation.stderr_artifact_digest,
                kind="stderr",
                label=f"{observation.command_id}:stderr",
                byte_size=len(stderr),
                role="stderr",
                command_id=observation.command_id,
                stream="stderr",
            ),
        )

    @staticmethod
    def _top_label(kind: str) -> str:
        if kind == "git_snapshot":
            return "diff"
        if kind in {"intake_documents", "command_batch"}:
            return "manifest"
        return "artifact"

    def _parse_intake_manifest(
        self, digest: str, raw: bytes, subject_digest: str
    ) -> tuple[IntakeDocument, ...]:
        data = self._parse_json(raw)
        self._require_keys(
            data,
            {
                "schema_version",
                "subject_digest",
                "documents",
                "notices",
                "task_digest",
                "task_present",
                "policy_count",
                "adr_count",
                "runbook_count",
                "complete",
                "limits",
            },
        )
        if data["schema_version"] != "v1" or data["subject_digest"] != subject_digest:
            raise self._not_found()
        documents_raw = data["documents"]
        notices_raw = data["notices"]
        if type(documents_raw) is not list or type(notices_raw) is not list:
            raise self._not_found()
        try:
            documents = tuple(IntakeDocument.model_validate(item) for item in documents_raw)
            notices = tuple(IntakeNotice.model_validate(item) for item in notices_raw)
            IntakeSnapshot(
                schema_version="v1",
                subject_digest=subject_digest,
                documents=documents,
                notices=notices,
                task_digest=data["task_digest"],
                task_present=data["task_present"],
                policy_count=data["policy_count"],
                adr_count=data["adr_count"],
                runbook_count=data["runbook_count"],
                manifest_artifact_digest=digest,
                complete=data["complete"],
                collected_at=datetime.now(timezone.utc),
            )
            self._validate_limits(
                data["limits"],
                {
                    "max_declared_paths",
                    "max_file_bytes",
                    "max_total_bytes",
                    "max_frontmatter_bytes",
                    "max_frontmatter_items",
                },
            )
            return documents
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    def _parse_command_manifest(
        self, digest: str, raw: bytes, subject_digest: str
    ) -> tuple[CommandObservation, ...]:
        data = self._parse_json(raw)
        self._require_keys(
            data,
            {
                "schema_version",
                "subject_digest",
                "observations",
                "environment_fingerprint",
                "complete",
                "all_passed",
                "limits",
            },
        )
        if data["schema_version"] != "v1" or data["subject_digest"] != subject_digest:
            raise self._not_found()
        observations_raw = data["observations"]
        if type(observations_raw) is not list:
            raise self._not_found()
        try:
            observations = tuple(
                CommandObservation.model_validate(item) for item in observations_raw
            )
            CommandBatchSnapshot(
                schema_version="v1",
                subject_digest=subject_digest,
                commands=observations,
                environment_fingerprint=data["environment_fingerprint"],
                manifest_artifact_digest=digest,
                complete=data["complete"],
                all_passed=data["all_passed"],
                collected_at=datetime.now(timezone.utc),
            )
            self._validate_limits(
                data["limits"], {"max_commands", "read_chunk_bytes"}
            )
            return observations
        except (TypeError, ValueError, ValidationError):
            raise self._not_found() from None

    @staticmethod
    def _parse_json(raw: bytes) -> dict[str, Any]:
        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise _InvalidArtifact("duplicate manifest key")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise _InvalidArtifact(f"invalid JSON constant: {value}")

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=object_pairs,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            raise AssuranceWebNotFoundError("artifact is unavailable") from None
        if type(value) is not dict:
            raise AssuranceWebNotFoundError("artifact is unavailable")
        return value

    @staticmethod
    def _require_keys(data: dict[str, Any], expected: set[str]) -> None:
        if set(data) != expected:
            raise AssuranceWebNotFoundError("artifact is unavailable")

    @staticmethod
    def _validate_limits(value: object, expected: set[str]) -> None:
        if type(value) is not dict or set(value) != expected:
            raise AssuranceWebNotFoundError("artifact is unavailable")
        for item in value.values():
            if type(item) is not int or item <= 0:
                raise AssuranceWebNotFoundError("artifact is unavailable")

    @staticmethod
    def _not_found() -> AssuranceWebNotFoundError:
        return AssuranceWebNotFoundError("artifact is unavailable")


__all__ = [
    "ArtifactReference",
    "AssuranceArtifactReader",
    "EvidenceArtifactIndex",
    "VerifiedArtifact",
]
