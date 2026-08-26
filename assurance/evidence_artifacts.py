"""Case-independent authorization and integrity verification for artifacts.

The resolver is intentionally independent from the Web Case projection.  It
accepts one revalidated Evidence value, derives the one-level artifact closure
declared by the collector's typed manifest, and reads only content-addressed
files that are in that closure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandBatchSnapshot, CommandObservation
from assurance.contracts import Evidence
from assurance.digests import normalize_repo_path
from assurance.intake import IntakeDocument, IntakeNotice, IntakeSnapshot


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INTEGRITY_STATUS = "SHA-256 integrity verified"
_TEXT_MEDIA_TYPE = "text/plain"
_READ_CHUNK_BYTES = 64 * 1024

# These limits are the collector contracts.  The resolver never trusts a
# manifest to widen them; manifest-local limits may only be narrower.
_MAX_GIT_BYTES = 262_144
_MAX_INTAKE_MANIFEST_BYTES = 4_718_592  # approximately 4.5 MiB
_MAX_COMMAND_MANIFEST_BYTES = 262_144
_MAX_UNKNOWN_ARTIFACT_BYTES = 262_144
_MAX_INTAKE_DECLARED_PATHS = 64
_MAX_INTAKE_FILE_BYTES = 1024 * 1024
_MAX_INTAKE_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_FRONTMATTER_BYTES = 16 * 1024
_MAX_FRONTMATTER_ITEMS = 64
_MAX_COMMANDS = 16
_MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_MAX_COMMAND_READ_CHUNK_BYTES = 65_536


class EvidenceArtifactError(ValueError):
    """Stable, path-free failure for malformed or unauthorized artifacts."""

    message = "artifact is unavailable"

    def __init__(self, *_args: object) -> None:
        # Never let a lower-level exception, digest, or path become part of
        # the domain error even if a future call site passes one accidentally.
        super().__init__(self.message)


def _error() -> EvidenceArtifactError:
    return EvidenceArtifactError()


def _is_digest(value: object) -> bool:
    return type(value) is str and _SHA256_DIGEST_RE.fullmatch(value) is not None


def _require_digest(value: object) -> str:
    if not _is_digest(value):
        raise _error()
    return value


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

    @field_validator("digest", mode="before")
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if not _is_digest(value):
            raise ValueError("invalid digest")
        return value

    @field_validator("kind", "label", mode="before")
    @classmethod
    def _validate_text(cls, value: object) -> str:
        if type(value) is not str or not value:
            raise ValueError("text must be a nonempty string")
        return value

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("path must be a string or None")
        try:
            if normalize_repo_path(value) != value:
                raise ValueError("path must be canonical")
        except (TypeError, ValueError):
            raise ValueError("path must be canonical") from None
        return value

    @field_validator("command_id", mode="before")
    @classmethod
    def _validate_command_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError("command_id must be a nonblank string")
        return value

    @model_validator(mode="after")
    def _validate_role_fields(self) -> "ArtifactReference":
        if self.role == "top_level":
            if self.path is not None or self.command_id is not None or self.stream is not None:
                raise ValueError("top-level references must not carry child metadata")
        elif self.role == "document":
            if self.path is None or self.command_id is not None or self.stream is not None:
                raise ValueError("document references require only a path")
        elif self.role in ("stdout", "stderr"):
            if self.command_id is None or self.stream != self.role:
                raise ValueError("stream references require matching command metadata")
            if self.path is not None:
                raise ValueError("stream references must not carry a path")
        return self


class AuthorizedArtifactIndex(BaseModel):
    """The deterministic, one-Evidence artifact authorization closure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence_id: str = Field(min_length=1)
    evidence_kind: str = Field(min_length=1)
    subject_digest: str
    top_level_digest: str
    artifacts: tuple[ArtifactReference, ...] = Field(min_length=1)
    intake_documents: tuple[IntakeDocument, ...] = ()
    command_observations: tuple[CommandObservation, ...] = ()

    @field_validator("evidence_id", "evidence_kind", mode="before")
    @classmethod
    def _validate_identity_text(cls, value: object) -> str:
        if type(value) is not str or not value.strip():
            raise ValueError("identity must be a nonblank string")
        return value

    @field_validator("subject_digest", "top_level_digest", mode="before")
    @classmethod
    def _validate_index_digest(cls, value: object) -> str:
        if not _is_digest(value):
            raise ValueError("invalid digest")
        return value

    @model_validator(mode="after")
    def _validate_closure(self) -> "AuthorizedArtifactIndex":
        first = self.artifacts[0]
        if first.role != "top_level" or first.digest != self.top_level_digest:
            raise ValueError("top-level artifact must be first and bound")
        if any(item.role == "top_level" for item in self.artifacts[1:]):
            raise ValueError("only one top-level artifact is allowed")
        if self.evidence_kind == "intake_documents":
            if self.command_observations:
                raise ValueError("intake index cannot expose command observations")
        elif self.evidence_kind == "command_batch":
            if self.intake_documents:
                raise ValueError("command index cannot expose intake documents")
        elif self.intake_documents or self.command_observations:
            raise ValueError("opaque index cannot expose typed projections")
        return self


class VerifiedArtifactBytes(BaseModel):
    """Bytes read from one entry in an authorized artifact closure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
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

    @field_validator("digest", mode="before")
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if not _is_digest(value):
            raise ValueError("invalid digest")
        return value

    @field_validator("kind", "label", mode="before")
    @classmethod
    def _validate_text(cls, value: object) -> str:
        if type(value) is not str or not value:
            raise ValueError("text must be a nonempty string")
        return value

    @field_validator("data", mode="before")
    @classmethod
    def _validate_bytes(cls, value: object) -> bytes:
        if type(value) is not bytes:
            raise ValueError("data must be exactly bytes")
        return value

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("path must be a string or None")
        try:
            if normalize_repo_path(value) != value:
                raise ValueError("path must be canonical")
        except (TypeError, ValueError):
            raise ValueError("path must be canonical") from None
        return value

    @field_validator("command_id", mode="before")
    @classmethod
    def _validate_command_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError("command_id must be a nonblank string")
        return value

    @model_validator(mode="after")
    def _validate_bytes_contract(self) -> "VerifiedArtifactBytes":
        if len(self.data) != self.byte_size:
            raise ValueError("byte_size must match data")
        if _digest_bytes(self.data) != self.digest:
            raise ValueError("data digest mismatch")
        reference = ArtifactReference.model_validate(
            {
                "schema_version": self.schema_version,
                "digest": self.digest,
                "kind": self.kind,
                "label": self.label,
                "byte_size": self.byte_size,
                "media_type": self.media_type,
                "integrity_status": self.integrity_status,
                "role": self.role,
                "path": self.path,
                "command_id": self.command_id,
                "stream": self.stream,
            }
        )
        del reference
        return self


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class EvidenceArtifactResolver:
    """Resolve and read exactly the artifacts authorized by one Evidence."""

    @staticmethod
    def index(
        evidence: Evidence,
        *,
        artifact_store: ArtifactStore,
        subject_digest: str,
    ) -> AuthorizedArtifactIndex:
        normalized_evidence = _revalidate_evidence(evidence)
        store = _require_store(artifact_store)
        expected_subject = _require_digest(subject_digest)
        if normalized_evidence.subject_digest != expected_subject:
            raise _error()

        top_digest = _require_digest(normalized_evidence.artifact_digest)
        return _index_from_binding(
            evidence_id=normalized_evidence.evidence_id,
            evidence_kind=normalized_evidence.kind,
            subject_digest=expected_subject,
            top_level_digest=top_digest,
            artifact_store=store,
        )

    @staticmethod
    def read(
        index: AuthorizedArtifactIndex,
        *,
        evidence: Evidence,
        subject_digest: str,
        artifact_store: ArtifactStore,
        digest: str,
        max_bytes: int | None = None,
    ) -> VerifiedArtifactBytes:
        normalized_evidence = _revalidate_evidence(evidence)
        normalized_index = _revalidate_index(index)
        store = _require_store(artifact_store)
        expected_subject = _require_digest(subject_digest)
        if normalized_evidence.subject_digest != expected_subject:
            raise _error()
        requested_digest = _require_digest(digest)
        if max_bytes is not None and (
            type(max_bytes) is not int or max_bytes < 0
        ):
            raise _error()
        authoritative_index = _index_from_binding(
            evidence_id=normalized_evidence.evidence_id,
            evidence_kind=normalized_evidence.kind,
            subject_digest=expected_subject,
            top_level_digest=normalized_evidence.artifact_digest,
            artifact_store=store,
        )
        if authoritative_index != normalized_index:
            raise _error()
        reference = next(
            (
                item
                for item in authoritative_index.artifacts
                if item.digest == requested_digest
            ),
            None,
        )
        if reference is None:
            raise _error()

        cap = _reference_cap(authoritative_index.evidence_kind, reference)
        if max_bytes is not None:
            cap = min(cap, max_bytes)
        if reference.byte_size > cap:
            raise _error()
        data = _read_cas_bytes(store, requested_digest, cap)
        if len(data) != reference.byte_size:
            raise _error()
        try:
            return VerifiedArtifactBytes(
                evidence_id=authoritative_index.evidence_id,
                digest=requested_digest,
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
            raise _error() from None


def _require_store(value: object) -> ArtifactStore:
    if type(value) is not ArtifactStore:
        raise _error()
    return value


def _revalidate_evidence(value: object) -> Evidence:
    if type(value) is not Evidence:
        raise _error()
    try:
        return Evidence.model_validate(value.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError, ValidationError, RecursionError):
        raise _error() from None


def _revalidate_index(value: object) -> AuthorizedArtifactIndex:
    if type(value) is not AuthorizedArtifactIndex:
        raise _error()
    try:
        return AuthorizedArtifactIndex.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError, RecursionError):
        raise _error() from None


def _top_level_cap(kind: str) -> int | None:
    if kind == "git_snapshot":
        return _MAX_GIT_BYTES
    if kind == "intake_documents":
        return _MAX_INTAKE_MANIFEST_BYTES
    if kind == "command_batch":
        return _MAX_COMMAND_MANIFEST_BYTES
    return _MAX_UNKNOWN_ARTIFACT_BYTES


def _index_from_binding(
    *,
    evidence_id: str,
    evidence_kind: str,
    subject_digest: str,
    top_level_digest: str,
    artifact_store: ArtifactStore,
) -> AuthorizedArtifactIndex:
    """Rebuild the closure from its immutable binding and current CAS bytes."""

    expected_subject = _require_digest(subject_digest)
    top_digest = _require_digest(top_level_digest)
    top_bytes = _read_cas_bytes(
        artifact_store, top_digest, _top_level_cap(evidence_kind)
    )
    top = _reference(
        digest=top_digest,
        kind=evidence_kind,
        label=_top_label(evidence_kind),
        byte_size=len(top_bytes),
        role="top_level",
    )
    if evidence_kind == "git_snapshot":
        references = (top,)
        intake_documents = ()
        command_observations = ()
    elif evidence_kind == "intake_documents":
        intake_documents = _parse_intake_manifest(
            top_digest, top_bytes, expected_subject
        )
        command_observations = ()
        references = (top,) + tuple(
            _document_reference(artifact_store, document)
            for document in intake_documents
        )
    elif evidence_kind == "command_batch":
        intake_documents = ()
        command_observations = _parse_command_manifest(
            top_digest, top_bytes, expected_subject
        )
        references = (top,) + tuple(
            reference
            for observation in command_observations
            for reference in _command_references(artifact_store, observation)
        )
    else:
        # Unknown kinds remain opaque.  A JSON-looking top-level artifact does
        # not grant any child digest authorization.
        references = (top,)
        intake_documents = ()
        command_observations = ()
    try:
        return AuthorizedArtifactIndex(
            evidence_id=evidence_id,
            evidence_kind=evidence_kind,
            subject_digest=expected_subject,
            top_level_digest=top_digest,
            artifacts=references,
            intake_documents=intake_documents,
            command_observations=command_observations,
        )
    except (TypeError, ValueError, ValidationError):
        raise _error() from None


def _reference_cap(kind: str, reference: ArtifactReference) -> int:
    if reference.role == "top_level":
        cap = _top_level_cap(kind)
        return reference.byte_size if cap is None else cap
    if reference.role == "document":
        return _MAX_INTAKE_FILE_BYTES
    return _MAX_COMMAND_OUTPUT_BYTES


def _read_cas_bytes(
    artifact_store: ArtifactStore,
    digest: str,
    max_bytes: int | None,
) -> bytes:
    """Read one derived CAS path with a bounded, no-follow file descriptor."""

    digest = _require_digest(digest)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise _error()
    if not hasattr(os, "O_NOFOLLOW"):
        raise _error()
    root_fd: int | None = None
    sha_root_fd: int | None = None
    prefix_fd: int | None = None
    fd: int | None = None
    try:
        root = Path(artifact_store.root)
        no_follow = os.O_NOFOLLOW
        directory_flags = os.O_RDONLY | no_follow
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        root_fd = os.open(root, directory_flags)
        sha_root_fd = os.open("sha256", directory_flags, dir_fd=root_fd)
        prefix_fd = os.open(digest[7:9], directory_flags, dir_fd=sha_root_fd)
        fd = os.open(digest[9:], os.O_RDONLY | no_follow, dir_fd=prefix_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
            raise OSError("artifact target is not regular")
        if max_bytes is not None and info.st_size > max_bytes:
            raise OSError("artifact exceeds limit")

        remaining = info.st_size
        collected = bytearray()
        digest_state = hashlib.sha256()
        while remaining:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError("artifact read size changed")
            collected.extend(chunk)
            digest_state.update(chunk)
            remaining -= len(chunk)
        # Detect a growth race after the initial fstat/read boundary.
        if os.read(fd, 1):
            raise OSError("artifact grew during read")
        if digest_state.hexdigest() != digest[7:]:
            raise OSError("artifact digest mismatch")
        return bytes(collected)
    except (OSError, TypeError, ValueError):
        raise _error() from None
    finally:
        for descriptor in (fd, prefix_fd, sha_root_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _reference(
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
        raise _error() from None


def _document_reference(
    artifact_store: ArtifactStore, document: IntakeDocument
) -> ArtifactReference:
    if type(document.byte_size) is not int or document.byte_size > _MAX_INTAKE_FILE_BYTES:
        raise _error()
    data = _read_cas_bytes(artifact_store, document.artifact_digest, document.byte_size)
    if len(data) != document.byte_size:
        raise _error()
    return _reference(
        digest=document.artifact_digest,
        kind=document.kind,
        label=document.path,
        byte_size=len(data),
        role="document",
        path=document.path,
    )


def _command_references(
    artifact_store: ArtifactStore, observation: CommandObservation
) -> tuple[ArtifactReference, ArtifactReference]:
    for byte_size in (observation.stdout_bytes, observation.stderr_bytes):
        if type(byte_size) is not int or byte_size > _MAX_COMMAND_OUTPUT_BYTES:
            raise _error()
    stdout = _read_cas_bytes(
        artifact_store,
        observation.stdout_artifact_digest,
        observation.stdout_bytes,
    )
    stderr = _read_cas_bytes(
        artifact_store,
        observation.stderr_artifact_digest,
        observation.stderr_bytes,
    )
    if len(stdout) != observation.stdout_bytes or len(stderr) != observation.stderr_bytes:
        raise _error()
    return (
        _reference(
            digest=observation.stdout_artifact_digest,
            kind="stdout",
            label=f"{observation.command_id}:stdout",
            byte_size=len(stdout),
            role="stdout",
            command_id=observation.command_id,
            stream="stdout",
        ),
        _reference(
            digest=observation.stderr_artifact_digest,
            kind="stderr",
            label=f"{observation.command_id}:stderr",
            byte_size=len(stderr),
            role="stderr",
            command_id=observation.command_id,
            stream="stderr",
        ),
    )


def _top_label(kind: str) -> str:
    if kind == "git_snapshot":
        return "diff"
    if kind in {"intake_documents", "command_batch"}:
        return "manifest"
    return "artifact"


def _parse_intake_manifest(
    digest: str, raw: bytes, subject_digest: str
) -> tuple[IntakeDocument, ...]:
    data = _parse_json(raw)
    _require_keys(
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
        raise _error()
    documents_raw = data["documents"]
    notices_raw = data["notices"]
    if type(documents_raw) is not list or type(notices_raw) is not list:
        raise _error()
    limits = _validate_limits(
        data["limits"],
        {
            "max_declared_paths",
            "max_file_bytes",
            "max_total_bytes",
            "max_frontmatter_bytes",
            "max_frontmatter_items",
        },
    )
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
    except (TypeError, ValueError, ValidationError):
        raise _error() from None
    if limits["max_declared_paths"] > _MAX_INTAKE_DECLARED_PATHS:
        raise _error()
    if len(documents) > limits["max_declared_paths"]:
        raise _error()
    if limits["max_file_bytes"] > _MAX_INTAKE_FILE_BYTES:
        raise _error()
    if limits["max_total_bytes"] > _MAX_INTAKE_TOTAL_BYTES:
        raise _error()
    if limits["max_frontmatter_bytes"] > _MAX_FRONTMATTER_BYTES:
        raise _error()
    if limits["max_frontmatter_items"] > _MAX_FRONTMATTER_ITEMS:
        raise _error()
    if any(
        document.byte_size > limits["max_file_bytes"]
        or document.byte_size > _MAX_INTAKE_FILE_BYTES
        for document in documents
    ):
        raise _error()
    if sum(document.byte_size for document in documents) > min(
        limits["max_total_bytes"], _MAX_INTAKE_TOTAL_BYTES
    ):
        raise _error()
    return documents


def _parse_command_manifest(
    digest: str, raw: bytes, subject_digest: str
) -> tuple[CommandObservation, ...]:
    data = _parse_json(raw)
    _require_keys(
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
        raise _error()
    observations_raw = data["observations"]
    if type(observations_raw) is not list:
        raise _error()
    limits = _validate_limits(data["limits"], {"max_commands", "read_chunk_bytes"})
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
    except (TypeError, ValueError, ValidationError):
        raise _error() from None
    if limits["max_commands"] > _MAX_COMMANDS:
        raise _error()
    if len(observations) > limits["max_commands"]:
        raise _error()
    if limits["read_chunk_bytes"] > _MAX_COMMAND_READ_CHUNK_BYTES:
        raise _error()
    if any(
        observation.stdout_bytes > _MAX_COMMAND_OUTPUT_BYTES
        or observation.stderr_bytes > _MAX_COMMAND_OUTPUT_BYTES
        for observation in observations
    ):
        raise _error()
    return observations


def _parse_json(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise _error()

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("invalid JSON constant")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise _error() from None
    if type(value) is not dict:
        raise _error()
    return value


def _require_keys(data: dict[str, Any], expected: set[str]) -> None:
    if set(data) != expected:
        raise _error()


def _validate_limits(value: object, expected: set[str]) -> dict[str, int]:
    if type(value) is not dict or set(value) != expected:
        raise _error()
    result: dict[str, int] = {}
    for key, item in value.items():
        if type(item) is not int or item <= 0:
            raise _error()
        result[key] = item
    return result


__all__ = [
    "ArtifactReference",
    "AuthorizedArtifactIndex",
    "EvidenceArtifactError",
    "EvidenceArtifactResolver",
    "VerifiedArtifactBytes",
]
