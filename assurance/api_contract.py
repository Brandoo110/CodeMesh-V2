"""Deterministic evidence for the repository's canonical OpenAPI contract.

The collector reads one fixed blob from the exact Git subject HEAD.  It does
not inspect the working tree, import the web application, execute contract
code, or contact a provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import ChangeSubject, Evidence
from .digests import normalize_repo_path


CONTRACT_SOURCE_PATH = "contracts/openapi.json"
_DEFAULT_MAX_SOURCE_BYTES = 262_144
_MAX_PATH_BYTES = 1_024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OMISSION_ORDER = (
    "source_missing",
    "path_escape",
    "source_symlink",
    "source_unsupported_type",
    "oversize",
    "unparseable",
    "subject_mismatch",
    "repository_unavailable",
)


class ApiContractError(ValueError):
    """Base error for API contract collection."""


class ApiContractInputError(ApiContractError):
    """Raised for invalid collector arguments."""


class ApiContractIntegrityError(ApiContractError):
    """Raised when content-addressed persistence cannot be verified."""


class ApiContractCollectionError(ApiContractError):
    """Raised for an unexpected read-only Git failure."""


# Keep the public names used by callers that classify collector failures.
ApiContractPathError = ApiContractCollectionError
ApiContractFormatError = ApiContractCollectionError
ApiContractSubjectMismatch = ApiContractCollectionError


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_digest(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase sha256:<64 hex> digest")
    return value


def _validate_revision(value: object) -> str:
    if type(value) is not str or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError("revision must be a full lowercase Git SHA")
    return value


class ApiContractSource(BaseModel):
    """Provenance for the one fixed repository-relative source blob."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    path: str
    digest: str
    byte_size: StrictInt = Field(ge=0)

    @field_validator("path", mode="before")
    @classmethod
    def _fixed_path(cls, value: object) -> str:
        if type(value) is not str or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
            raise ValueError("path must be a bounded string")
        if normalize_repo_path(value) != value or value != CONTRACT_SOURCE_PATH:
            raise ValueError("path must be the canonical OpenAPI source path")
        return value

    @field_validator("digest", mode="before")
    @classmethod
    def _digest(cls, value: object) -> str:
        return _validate_digest(value)


class ApiContractSnapshot(BaseModel):
    """Typed contract facts with explicit, ordered omissions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    head_revision: str
    source: ApiContractSource | None = None
    source_path: str | None = None
    source_digest: str | None = None
    source_byte_size: StrictInt | None = Field(default=None, ge=0)
    artifact_digest: str
    status: Literal["success", "truncated"]
    trust_level: Literal["deterministic"] = "deterministic"
    omissions: tuple[str, ...] = ()
    complete: StrictBool
    collected_at: AwareDatetime

    @field_validator("subject_digest", "artifact_digest", mode="before")
    @classmethod
    def _digest_fields(cls, value: object) -> str:
        return _validate_digest(value)

    @field_validator("head_revision", mode="before")
    @classmethod
    def _revision(cls, value: object) -> str:
        return _validate_revision(value)

    @field_validator("source_path", mode="before")
    @classmethod
    def _source_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
            raise ValueError("source_path must be a bounded string")
        if normalize_repo_path(value) != value or value != CONTRACT_SOURCE_PATH:
            raise ValueError("source_path must be the fixed OpenAPI source path")
        return value

    @field_validator("source_digest", mode="before")
    @classmethod
    def _optional_digest(cls, value: object) -> str | None:
        return None if value is None else _validate_digest(value)

    @field_validator("omissions", mode="before")
    @classmethod
    def _omissions(cls, value: object) -> tuple[str, ...]:
        if type(value) not in (tuple, list):
            raise ValueError("omissions must be a tuple or list")
        result = tuple(value)
        if any(type(item) is not str or not item.strip() for item in result):
            raise ValueError("omissions must contain nonblank strings")
        if len(result) != len(set(result)):
            raise ValueError("omissions must be unique")
        if set(result) - set(_OMISSION_ORDER):
            raise ValueError("omissions contain an unsupported reason")
        ordered = tuple(item for item in _OMISSION_ORDER if item in result)
        if result != ordered:
            raise ValueError("omissions must use the fixed order")
        return result

    @model_validator(mode="after")
    def _bindings(self) -> "ApiContractSnapshot":
        if self.source is not None:
            if self.source_path != self.source.path:
                raise ValueError("source_path must match source.path")
            if self.source_digest != self.source.digest:
                raise ValueError("source_digest must match source.digest")
            if self.source_byte_size != self.source.byte_size:
                raise ValueError("source_byte_size must match source.byte_size")
        if self.status == "success":
            if not self.complete or self.omissions or self.source is None:
                raise ValueError("successful collection must be complete")
            if self.source_digest != self.artifact_digest:
                raise ValueError("successful artifact must equal source digest")
        elif self.complete or not self.omissions:
            raise ValueError("truncated collection must carry omissions")
        return self


def _evidence_id(snapshot: ApiContractSnapshot) -> str:
    material = "|".join(
        (
            snapshot.subject_digest,
            snapshot.head_revision,
            snapshot.source_path or "",
            snapshot.artifact_digest,
            snapshot.status,
        )
    ).encode("ascii")
    return "ev_api_contract_" + hashlib.sha256(material).hexdigest()[:32]


class ApiContractResult(BaseModel):
    """The typed snapshot and its shared-pipeline Evidence binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot: ApiContractSnapshot
    evidence: Evidence

    @model_validator(mode="after")
    def _bind(self) -> "ApiContractResult":
        snapshot, evidence = self.snapshot, self.evidence
        if evidence.kind != "api_contract":
            raise ValueError("evidence.kind must be api_contract")
        if evidence.producer != "collector.api_contract":
            raise ValueError("evidence.producer must be collector.api_contract")
        if evidence.subject_digest != snapshot.subject_digest:
            raise ValueError("evidence subject must match snapshot")
        if evidence.artifact_digest != snapshot.artifact_digest:
            raise ValueError("evidence artifact must match snapshot")
        if evidence.status != snapshot.status:
            raise ValueError("evidence status must match snapshot")
        if evidence.trust_level != snapshot.trust_level:
            raise ValueError("evidence trust must match snapshot")
        if evidence.collected_at != snapshot.collected_at:
            raise ValueError("evidence time must match snapshot")
        if evidence.trace_id is not None:
            raise ValueError("api contract Evidence must not carry trace_id")
        expected_ref = f"api_contract:{snapshot.source_path or 'missing'}"
        if evidence.source_ref != expected_ref:
            raise ValueError("evidence source_ref must match source path")
        if evidence.evidence_id != _evidence_id(snapshot):
            raise ValueError("evidence ID must be derived from snapshot facts")
        return self


def _strict_json(data: bytes) -> Any:
    """Parse JSON without duplicate keys, NaN, Infinity, or non-UTF-8 bytes."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=reject_constant,
    )


def _parse_contract(data: bytes) -> dict[str, Any]:
    """Parse and minimally validate the fixed OpenAPI JSON contract."""

    try:
        parsed = _strict_json(data)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise ValueError("contract is not parseable strict JSON") from None
    if type(parsed) is not dict:
        raise ValueError("contract root must be an object")
    if type(parsed.get("openapi")) is not str or not parsed["openapi"].strip():
        raise ValueError("contract openapi version is missing")
    if type(parsed.get("paths")) is not dict:
        raise ValueError("contract paths must be an object")
    return parsed


class ApiContractCollector:
    """Read only ``contracts/openapi.json`` from the exact Git subject HEAD."""

    def __init__(
        self,
        max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        if type(max_source_bytes) is not int or max_source_bytes <= 0:
            raise ValueError("max_source_bytes must be a positive int")
        if not isinstance(command_timeout_seconds, (int, float)) or isinstance(
            command_timeout_seconds, bool
        ) or command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.max_source_bytes = max_source_bytes
        self.command_timeout_seconds = float(command_timeout_seconds)

    def collect(
        self,
        repository_path: Path,
        *,
        subject_digest: str | None = None,
        head_revision: str | None = None,
        artifact_store: ArtifactStore,
        source_path: str | None = None,
        collected_at: datetime | None = None,
        subject: ChangeSubject | None = None,
        repository_identity: str | None = None,
    ) -> ApiContractResult:
        """Collect one exact subject blob and return success or bounded truncation."""

        del repository_identity
        if not isinstance(repository_path, Path):
            raise ApiContractInputError("repository_path must be a pathlib.Path")
        if type(artifact_store) is not ArtifactStore:
            raise ApiContractInputError("artifact_store must be an exact ArtifactStore")
        if subject is not None and type(subject) is not ChangeSubject:
            raise ApiContractInputError("subject must be an exact ChangeSubject")
        if subject is not None:
            if subject_digest is not None and subject_digest != subject.subject_digest:
                return self._incomplete(
                    subject_digest,
                    head_revision,
                    source_path=None,
                    omissions=("subject_mismatch",),
                    artifact_store=artifact_store,
                    collected_at=collected_at,
                )
            if head_revision is not None and head_revision != subject.head_revision:
                return self._incomplete(
                    subject.subject_digest,
                    head_revision,
                    source_path=None,
                    omissions=("subject_mismatch",),
                    artifact_store=artifact_store,
                    collected_at=collected_at,
                )
            subject_digest = subject.subject_digest
            head_revision = subject.head_revision
        if type(subject_digest) is not str or _SHA256_RE.fullmatch(subject_digest) is None:
            raise ApiContractInputError("subject_digest must be a lowercase sha256 digest")
        if head_revision is not None:
            _validate_revision(head_revision)
        when = collected_at or datetime.now(timezone.utc)
        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            raise ApiContractInputError("collected_at must be timezone-aware")

        if source_path is not None:
            try:
                normalized = normalize_repo_path(source_path)
            except (TypeError, ValueError):
                return self._incomplete(
                    subject_digest,
                    head_revision,
                    source_path=None,
                    omissions=("path_escape",),
                    artifact_store=artifact_store,
                    collected_at=when,
                )
            if (
                normalized != source_path
                or normalized != CONTRACT_SOURCE_PATH
                or len(source_path.encode("utf-8")) > _MAX_PATH_BYTES
            ):
                return self._incomplete(
                    subject_digest,
                    head_revision,
                    source_path=None,
                    omissions=("path_escape",),
                    artifact_store=artifact_store,
                    collected_at=when,
                )

        try:
            root = self._resolve_root(repository_path)
            actual_head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
            actual_head = actual_head.decode("ascii").strip()
        except (OSError, ValueError, ApiContractCollectionError, UnicodeDecodeError):
            return self._incomplete(
                subject_digest,
                head_revision,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("repository_unavailable",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        requested_head = head_revision or actual_head
        if requested_head != actual_head:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("subject_mismatch",),
                artifact_store=artifact_store,
                collected_at=when,
            )

        try:
            mode = self._source_mode(root, requested_head)
        except ApiContractCollectionError:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("repository_unavailable",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        if mode is None:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("source_missing",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        if mode == "symlink":
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("source_symlink",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        if mode != "100644":
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("source_unsupported_type",),
                artifact_store=artifact_store,
                collected_at=when,
            )

        try:
            size = int(
                self._git(
                    root,
                    "cat-file",
                    "-s",
                    f"{requested_head}:{CONTRACT_SOURCE_PATH}",
                )
                .decode("ascii")
                .strip()
            )
        except (ApiContractCollectionError, UnicodeDecodeError, ValueError):
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("source_missing",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        if size > self.max_source_bytes:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                source_byte_size=size,
                omissions=("oversize",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        try:
            raw = self._git(
                root, "show", f"{requested_head}:{CONTRACT_SOURCE_PATH}"
            )
        except ApiContractCollectionError:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                omissions=("source_missing",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        source_digest = _sha256(raw)
        if len(raw) != size or len(raw) > self.max_source_bytes:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                source_digest=source_digest,
                source_byte_size=len(raw),
                omissions=("oversize",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        try:
            _parse_contract(raw)
        except ValueError:
            return self._incomplete(
                subject_digest,
                requested_head,
                source_path=CONTRACT_SOURCE_PATH,
                source_digest=source_digest,
                source_byte_size=len(raw),
                omissions=("unparseable",),
                artifact_store=artifact_store,
                collected_at=when,
            )
        artifact_digest = self._store_verified(artifact_store, raw)
        source = ApiContractSource(
            path=CONTRACT_SOURCE_PATH,
            digest=source_digest,
            byte_size=len(raw),
        )
        snapshot = ApiContractSnapshot(
            subject_digest=subject_digest,
            head_revision=requested_head,
            source=source,
            source_path=CONTRACT_SOURCE_PATH,
            source_digest=source_digest,
            source_byte_size=len(raw),
            artifact_digest=artifact_digest,
            status="success",
            omissions=(),
            complete=True,
            collected_at=when,
        )
        return self._result(snapshot)

    def _result(self, snapshot: ApiContractSnapshot) -> ApiContractResult:
        evidence = Evidence(
            evidence_id=_evidence_id(snapshot),
            subject_digest=snapshot.subject_digest,
            kind="api_contract",
            producer="collector.api_contract",
            artifact_digest=snapshot.artifact_digest,
            source_ref=f"api_contract:{snapshot.source_path or 'missing'}",
            status=snapshot.status,
            trust_level="deterministic",
            collected_at=snapshot.collected_at,
        )
        return ApiContractResult(snapshot=snapshot, evidence=evidence)

    def _incomplete(
        self,
        subject_digest: str,
        head_revision: str | None,
        *,
        source_path: str | None,
        omissions: tuple[str, ...],
        artifact_store: ArtifactStore,
        collected_at: datetime | None,
        source_digest: str | None = None,
        source_byte_size: int | None = None,
    ) -> ApiContractResult:
        safe_head = (
            head_revision
            if type(head_revision) is str and _FULL_SHA_RE.fullmatch(head_revision)
            else "0" * 40
        )
        metadata = {
            "schema_version": "v1",
            "subject_digest": subject_digest,
            "head_revision": safe_head,
            "source_path": source_path,
            "source_digest": source_digest,
            "source_byte_size": source_byte_size,
            "omissions": omissions,
        }
        artifact_digest = self._store_verified(
            artifact_store,
            json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8"),
        )
        snapshot = ApiContractSnapshot(
            subject_digest=subject_digest,
            head_revision=safe_head,
            source_path=source_path,
            source_digest=source_digest,
            source_byte_size=source_byte_size,
            artifact_digest=artifact_digest,
            status="truncated",
            omissions=omissions,
            complete=False,
            collected_at=collected_at or datetime.now(timezone.utc),
        )
        return self._result(snapshot)

    @staticmethod
    def _resolve_root(path: Path) -> Path:
        root = Path(os.path.abspath(path))
        current = root
        while True:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("repository path must not traverse symlinks")
            if current == root and not stat.S_ISDIR(info.st_mode):
                raise ValueError("repository path must be a real directory")
            if current == Path(current.anchor):
                break
            current = current.parent
        return root

    def _git(self, root: Path, *args: str) -> bytes:
        try:
            result = subprocess.run(
                ("git", "--no-optional-locks", *args),
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiContractCollectionError("read-only Git command failed") from exc
        if result.returncode != 0:
            raise ApiContractCollectionError("read-only Git command failed")
        return result.stdout

    def _source_mode(self, root: Path, revision: str) -> str | None:
        for path, expected_type in (
            ("contracts", "tree"),
            (CONTRACT_SOURCE_PATH, "blob"),
        ):
            records = self._git(root, "ls-tree", "-z", revision, "--", path).split(
                b"\0"
            )
            entry = None
            for record in records:
                if not record:
                    continue
                try:
                    metadata, raw_path = record.split(b"\t", 1)
                    mode, entry_type, _object_id = metadata.split()
                    entry = (
                        mode.decode("ascii"),
                        entry_type.decode("ascii"),
                        raw_path.decode("utf-8"),
                    )
                except (UnicodeDecodeError, ValueError):
                    raise ApiContractCollectionError("malformed Git tree entry") from None
                if entry[2] == path:
                    break
            if entry is None:
                return None
            mode, entry_type, _ = entry
            if path == "contracts":
                if mode == "120000":
                    return "symlink"
                if entry_type != expected_type or mode != "040000":
                    return "unsupported"
            elif mode == "120000":
                return "symlink"
            elif entry_type != expected_type:
                return "unsupported"
            else:
                return mode
        return None

    @staticmethod
    def _store_verified(store: ArtifactStore, data: bytes) -> str:
        expected = _sha256(data)
        try:
            digest = store.put_bytes(data)
            if digest != expected or store.get_bytes(digest) != data:
                raise ApiContractIntegrityError("artifact persistence digest mismatch")
        except ApiContractIntegrityError:
            raise
        except Exception as exc:
            raise ApiContractIntegrityError(
                "artifact persistence could not be verified"
            ) from exc
        return digest


__all__ = (
    "CONTRACT_SOURCE_PATH",
    "ApiContractCollector",
    "ApiContractCollectionError",
    "ApiContractError",
    "ApiContractFormatError",
    "ApiContractInputError",
    "ApiContractIntegrityError",
    "ApiContractPathError",
    "ApiContractResult",
    "ApiContractSnapshot",
    "ApiContractSource",
    "ApiContractSubjectMismatch",
)
