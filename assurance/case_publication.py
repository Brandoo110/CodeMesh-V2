"""Deep module for publishing one authoritative Case through CI.

The public operation is intentionally small: build and verify the local Bundle,
hand it to one remote transport adapter, verify the returned lineage, and only
then allow that adapter to delete its exact temporary ref.  GitHub, Actions,
SQLite, provider, and token details stay behind the adapter seam.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .entry import AssuranceArtifactReadback, _case_views_match
from .evidence_bundle import (
    BuiltEvidenceBundle,
    build_evidence_bundle,
    canonical_json_bytes,
    verify_evidence_bundle,
)


_SOURCE_PREFIX = "api-authoritative-"
_SOURCE_EVIDENCE_ID_RE = re.compile(r"^ev_[A-Za-z0-9_-]+$")
_SOURCE_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_MAX_BYTES = 256 * 1024
_SOURCE_MAX_EVIDENCE = 256
_SOURCE_MAX_ARTIFACTS = 256


class PublicationRemoteError(RuntimeError):
    """The remote CI result was rejected or could not be established."""


class IdempotencyConflict(PublicationRemoteError):
    """The exact temporary ref already contains a different bundle."""


@dataclass(frozen=True)
class AuthoritativeEvidenceExport:
    """One Case Evidence artifact index and its verified raw bytes."""

    index: Mapping[str, Any]
    artifacts: Mapping[str, bytes]


@dataclass(frozen=True)
class AuthoritativeCaseExport:
    """The complete read-only API export consumed by the Bundle builder."""

    case_view: Mapping[str, Any]
    receipt: Mapping[str, Any]
    passport: Mapping[str, Any]
    passport_markdown: str
    evidence: tuple[AuthoritativeEvidenceExport, ...]


def _source_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationRemoteError(f"authoritative source {label} is invalid")
    return dict(value)


def _source_digest(value: object, label: str) -> str:
    if type(value) is not str or _SOURCE_SHA256_RE.fullmatch(value) is None:
        raise PublicationRemoteError(f"authoritative source {label} is invalid")
    return value


def _source_case_id(value: object) -> str:
    return _nonblank(value, "case_id")


def _source_validate_case_view(case_view: Mapping[str, Any], case_id: str) -> str:
    if case_view.get("schema_version") != "v1" or case_view.get("case_id") != case_id:
        raise PublicationRemoteError("authoritative source CaseView binding did not match")
    subject_digest = _source_digest(
        case_view.get("subject_digest"), "CaseView subject_digest"
    )
    case_record = case_view.get("case")
    if not isinstance(case_record, Mapping):
        raise PublicationRemoteError("authoritative source Case payload is missing")
    if (
        case_record.get("case_id") != case_id
        or case_record.get("subject_digest") != subject_digest
    ):
        raise PublicationRemoteError("authoritative source Case binding did not match")
    evidence = case_view.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > _SOURCE_MAX_EVIDENCE:
        raise PublicationRemoteError("authoritative source Evidence list is invalid")
    seen: set[str] = set()
    for item in evidence:
        row = _source_mapping(item, "Evidence row")
        evidence_id = _nonblank(row.get("evidence_id"), "evidence_id")
        if evidence_id in seen:
            raise PublicationRemoteError("authoritative source Evidence IDs are not unique")
        seen.add(evidence_id)
        if row.get("subject_digest") != subject_digest:
            raise PublicationRemoteError("authoritative source Evidence subject did not match")
        _source_digest(row.get("artifact_digest"), "Evidence artifact_digest")
    return subject_digest


class LocalAuthoritativeCaseSource:
    """Read one complete authoritative Case export from the loopback API."""

    def __init__(self, client: object) -> None:
        if client is None:
            raise TypeError("client is required")
        required = (
            "get_case",
            "get_receipt",
            "get_passport",
            "get_passport_markdown",
            "list_artifacts",
            "read_artifact",
        )
        if any(not callable(getattr(client, name, None)) for name in required):
            raise TypeError("client does not implement the Assurance read contract")
        self._client = client

    def export(self, case_id: str) -> AuthoritativeCaseExport:
        case_id = _source_case_id(case_id)
        case_start = _source_mapping(self._client.get_case(case_id), "CaseView")
        subject_digest = _source_validate_case_view(case_start, case_id)
        receipt = _source_mapping(self._client.get_receipt(case_id), "receipt")
        if receipt.get("schema_version") != "v1":
            raise PublicationRemoteError("authoritative source receipt schema is unsupported")
        run_id = _nonblank(receipt.get("run_id"), "run_id")
        if _source_digest(receipt.get("subject_digest"), "receipt subject_digest") != subject_digest:
            raise PublicationRemoteError("authoritative source receipt subject did not match")
        if case_start.get("receipt") != receipt:
            raise PublicationRemoteError("authoritative source Case receipt did not match")
        metadata = case_start.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("run_id") not in {None, run_id}:
            raise PublicationRemoteError("authoritative source metadata run_id did not match")

        passport = _source_mapping(self._client.get_passport(case_id), "Passport")
        if (
            passport.get("schema") != "codemesh.assurance.passport.v1"
            or passport.get("case_id") != case_id
            or _source_digest(passport.get("subject_digest"), "Passport subject_digest")
            != subject_digest
        ):
            raise PublicationRemoteError("authoritative source Passport binding did not match")
        passport_evidence = passport.get("evidence")
        if not isinstance(passport_evidence, list) or len(passport_evidence) > _SOURCE_MAX_EVIDENCE:
            raise PublicationRemoteError("authoritative source Passport Evidence is invalid")
        passport_by_id: dict[str, Mapping[str, Any]] = {}
        for item in passport_evidence:
            row = _source_mapping(item, "Passport Evidence row")
            evidence_id = _nonblank(row.get("evidence_id"), "Passport evidence_id")
            if evidence_id in passport_by_id:
                raise PublicationRemoteError("authoritative source Passport Evidence IDs are not unique")
            _source_digest(row.get("artifact_digest"), "Passport artifact_digest")
            passport_by_id[evidence_id] = row

        passport_markdown = self._client.get_passport_markdown(case_id)
        if type(passport_markdown) is not str or not passport_markdown:
            raise PublicationRemoteError("authoritative source Passport Markdown is invalid")

        evidence_exports: list[AuthoritativeEvidenceExport] = []
        case_evidence = case_start["evidence"]
        assert isinstance(case_evidence, list)
        for item in case_evidence:
            row = _source_mapping(item, "Evidence row")
            evidence_id = _nonblank(row.get("evidence_id"), "evidence_id")
            artifact_digest = _source_digest(row.get("artifact_digest"), "artifact_digest")
            passport_row = passport_by_id.get(evidence_id)
            if passport_row is None or passport_row.get("artifact_digest") != artifact_digest:
                raise PublicationRemoteError("authoritative source Evidence Passport binding did not match")
            index = _source_mapping(
                self._client.list_artifacts(case_id, evidence_id), "artifact index"
            )
            if (
                index.get("schema_version") != "v1"
                or index.get("case_id") != case_id
                or index.get("evidence_id") != evidence_id
                or index.get("evidence_kind") != row.get("kind")
            ):
                raise PublicationRemoteError("authoritative source artifact index binding did not match")
            artifacts = index.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts or len(artifacts) > _SOURCE_MAX_ARTIFACTS:
                raise PublicationRemoteError("authoritative source artifact index is invalid")
            artifact_bytes: dict[str, bytes] = {}
            for artifact in artifacts:
                artifact_row = _source_mapping(artifact, "artifact reference")
                digest = _source_digest(artifact_row.get("digest"), "artifact digest")
                if digest in artifact_bytes:
                    raise PublicationRemoteError("authoritative source artifact digests are not unique")
                byte_size = artifact_row.get("byte_size")
                if type(byte_size) is not int or byte_size < 0 or byte_size > _SOURCE_MAX_BYTES:
                    raise PublicationRemoteError("authoritative source artifact size is invalid")
                readback = self._client.read_artifact(case_id, evidence_id, digest)
                if not isinstance(readback, AssuranceArtifactReadback):
                    raise PublicationRemoteError("authoritative source artifact readback is invalid")
                if (
                    readback.case_id != case_id
                    or readback.evidence_id != evidence_id
                    or readback.digest != digest
                    or readback.byte_size != byte_size
                    or type(readback.data) is not bytes
                    or len(readback.data) != byte_size
                    or "sha256:" + hashlib.sha256(readback.data).hexdigest() != digest
                ):
                    raise PublicationRemoteError("authoritative source artifact readback did not match")
                artifact_bytes[digest] = readback.data
            if artifact_digest not in artifact_bytes:
                raise PublicationRemoteError("authoritative source Case artifact was absent from index")
            evidence_exports.append(
                AuthoritativeEvidenceExport(index=index, artifacts=artifact_bytes)
            )
        if set(passport_by_id) != {
            str(_source_mapping(item, "Evidence row").get("evidence_id"))
            for item in case_evidence
        }:
            raise PublicationRemoteError("authoritative source Case and Passport Evidence IDs did not match")

        case_end = _source_mapping(self._client.get_case(case_id), "CaseView end")
        if not _case_views_match(case_start, case_end):
            raise PublicationRemoteError("authoritative source Case changed during export")
        return AuthoritativeCaseExport(
            case_view=case_start,
            receipt=receipt,
            passport=passport,
            passport_markdown=passport_markdown,
            evidence=tuple(evidence_exports),
        )


def _materialize_authoritative_export(
    root: Path, exported: AuthoritativeCaseExport
) -> str:
    if not isinstance(exported, AuthoritativeCaseExport):
        raise PublicationRemoteError("authoritative source export is invalid")
    case_view = _source_mapping(exported.case_view, "CaseView export")
    case_id = _source_case_id(case_view.get("case_id"))
    receipt = _source_mapping(exported.receipt, "receipt export")
    passport = _source_mapping(exported.passport, "Passport export")
    run = {
        "schema_version": "v1",
        "case_view": case_view,
        "receipt": {**receipt, "case_id": case_id},
    }
    root.joinpath(f"{_SOURCE_PREFIX}authoritative-case.json").write_bytes(
        canonical_json_bytes(case_view)
    )
    root.joinpath(f"{_SOURCE_PREFIX}passport.json").write_bytes(
        canonical_json_bytes(passport)
    )
    root.joinpath(f"{_SOURCE_PREFIX}response.json").write_bytes(
        canonical_json_bytes(run)
    )
    root.joinpath(f"{_SOURCE_PREFIX}passport.md").write_text(
        exported.passport_markdown, encoding="utf-8"
    )
    artifact_root = root / f"{_SOURCE_PREFIX}artifacts"
    artifact_root.mkdir()
    for exported_evidence in exported.evidence:
        index = _source_mapping(exported_evidence.index, "artifact index export")
        evidence_id = _nonblank(index.get("evidence_id"), "evidence_id")
        if _SOURCE_EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
            raise PublicationRemoteError("authoritative source evidence_id is invalid")
        index_path = artifact_root / f"{evidence_id}-index.json"
        index_path.write_bytes(canonical_json_bytes(index))
        artifacts = exported_evidence.artifacts
        if not isinstance(artifacts, Mapping) or len(artifacts) > _SOURCE_MAX_ARTIFACTS:
            raise PublicationRemoteError("authoritative source artifact export is invalid")
        for digest, data in artifacts.items():
            digest = _source_digest(digest, "artifact export digest")
            if type(data) is not bytes or len(data) > _SOURCE_MAX_BYTES:
                raise PublicationRemoteError("authoritative source artifact export is invalid")
            (artifact_root / f"sha256_{digest.removeprefix('sha256:')}").write_bytes(data)
    return _SOURCE_PREFIX


@dataclass(frozen=True)
class RemotePublication:
    """The remote facts required for authoritative publication readback."""

    transport_ref: str
    transport_ref_commit: str
    transport_head: str
    ci_run_id: str
    ci_job_id: str
    run_attempt: int
    artifact_id: str
    check_id: int
    check_url: str
    conclusion: str
    case_id: str
    run_id: str
    subject_digest: str
    producer_head: str
    passport_digest: str
    transport_id: str
    origin: str
    workbench: Mapping[str, Any]
    cleanup_allowed: bool = True


class PublicationRemote(Protocol):
    """The one remote adapter operation used by :class:`CasePublication`."""

    def publish(
        self,
        *,
        bundle: BuiltEvidenceBundle,
        target_pr: int,
        producer_head: str,
        transport_head: str,
    ) -> RemotePublication: ...

    def cleanup(self, *, ref: str, commit_sha: str) -> None: ...


@dataclass(frozen=True)
class PublicationReceipt:
    """Safe local receipt after remote readback and exact cleanup."""

    schema_version: str
    origin: str
    transport_id: str
    transport_ref: str
    transport_ref_commit: str
    transport_head: str
    producer_head: str
    repository: str
    target_pr: int
    bundle_digest: str
    passport_digest: str
    case_id: str
    run_id: str
    subject_digest: str
    ci_run_id: str
    ci_job_id: str
    run_attempt: int
    artifact_id: str
    check_id: int
    check_url: str
    conclusion: str
    workbench: Mapping[str, Any]


def _nonblank(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise PublicationRemoteError(f"{field} is invalid")
    return value


def _validate_remote(
    remote: object,
    *,
    bundle: BuiltEvidenceBundle,
    repository: str,
    target_pr: int,
) -> RemotePublication:
    if not isinstance(remote, RemotePublication):
        raise PublicationRemoteError("remote publication receipt is invalid")
    expected = {
        "transport_ref": bundle.transport_ref,
        "transport_head": bundle.transport_head,
        "producer_head": bundle.producer_head,
        "case_id": bundle.case_id,
        "run_id": bundle.run_id,
        "subject_digest": bundle.subject_digest,
        "passport_digest": bundle.passport_digest,
        "transport_id": bundle.transport_id,
        "origin": "local_authoritative_bundle",
    }
    for field, value in expected.items():
        if getattr(remote, field) != value:
            raise PublicationRemoteError(f"remote {field} did not match Bundle")
    _nonblank(remote.transport_ref_commit, "transport_ref_commit")
    _nonblank(remote.ci_run_id, "ci_run_id")
    _nonblank(remote.ci_job_id, "ci_job_id")
    _nonblank(remote.artifact_id, "artifact_id")
    _nonblank(remote.check_url, "check_url")
    _nonblank(remote.conclusion, "conclusion")
    if type(remote.run_attempt) is not int or remote.run_attempt <= 0:
        raise PublicationRemoteError("remote run_attempt is invalid")
    if type(remote.check_id) is not int or remote.check_id <= 0:
        raise PublicationRemoteError("remote check_id is invalid")
    if not isinstance(remote.workbench, Mapping):
        raise PublicationRemoteError("remote Workbench is invalid")
    workbench_bindings = {
        "origin": "local_authoritative_bundle",
        "transport_id": bundle.transport_id,
        "bundle_digest": bundle.bundle_digest,
        "transport_ref": bundle.transport_ref,
        "transport_head": bundle.transport_head,
        "producer_head": bundle.producer_head,
        "repository": repository,
        "target_pr": target_pr,
        "case_id": bundle.case_id,
        "run_id": bundle.run_id,
        "subject_digest": bundle.subject_digest,
        "passport_digest": bundle.passport_digest,
    }
    for field, value in workbench_bindings.items():
        if remote.workbench.get(field) != value:
            raise PublicationRemoteError(f"remote Workbench {field} did not match Bundle")
    if not remote.cleanup_allowed:
        raise PublicationRemoteError(
            "remote temporary ref was not created by this publication; cleanup is not authorized"
        )
    return remote


class CasePublication:
    """Export, verify, transport, read back, and clean up one Case."""

    def __init__(
        self,
        *,
        source: object | None = None,
        evidence_root: Path | None = None,
        repository: str,
        transport_head: str,
        remote: PublicationRemote,
        prefix: str | None = None,
    ) -> None:
        if (source is None) == (evidence_root is None):
            raise TypeError("exactly one of source or evidence_root is required")
        if source is not None and not callable(getattr(source, "export", None)):
            raise TypeError("source must implement export")
        if evidence_root is not None and not isinstance(evidence_root, Path):
            raise TypeError("evidence_root must be a Path")
        if source is not None and prefix is not None:
            raise TypeError("prefix is only supported for evidence_root")
        self._evidence_root = evidence_root
        self._source = source
        self._repository = _nonblank(repository, "repository")
        self._transport_head = _nonblank(transport_head, "transport_head")
        if remote is None:
            raise TypeError("remote is required")
        self._remote = remote
        self._prefix = prefix

    def publish(
        self,
        *,
        case_id: str,
        target_pr: int,
        producer_head: str,
    ) -> PublicationReceipt:
        """Complete the operation or raise without claiming publication success."""

        if self._source is not None:
            exported = self._source.export(case_id)
            with tempfile.TemporaryDirectory(
                prefix="codemesh-authoritative-export-"
            ) as temporary_root:
                evidence_root = Path(temporary_root)
                prefix = _materialize_authoritative_export(evidence_root, exported)
                return self._publish_bundle(
                    evidence_root=evidence_root,
                    prefix=prefix,
                    case_id=case_id,
                    target_pr=target_pr,
                    producer_head=producer_head,
                )
        if self._evidence_root is None:
            raise PublicationRemoteError("publication source is unavailable")
        return self._publish_bundle(
            evidence_root=self._evidence_root,
            prefix=self._prefix,
            case_id=case_id,
            target_pr=target_pr,
            producer_head=producer_head,
        )

    def _publish_bundle(
        self,
        *,
        evidence_root: Path,
        prefix: str | None,
        case_id: str,
        target_pr: int,
        producer_head: str,
    ) -> PublicationReceipt:
        """Publish one already materialized local Bundle through the remote."""

        bundle = build_evidence_bundle(
            evidence_root,
            case_id=case_id,
            repository=self._repository,
            pr_number=target_pr,
            producer_head=producer_head,
            transport_head=self._transport_head,
            prefix=prefix,
        )
        verified = verify_evidence_bundle(bundle.bundle_bytes)
        if verified.bundle_digest != bundle.bundle_digest:
            raise PublicationRemoteError("local Bundle verification changed its digest")
        remote = self._remote.publish(
            bundle=bundle,
            target_pr=target_pr,
            producer_head=producer_head,
            transport_head=self._transport_head,
        )
        verified_remote = _validate_remote(
            remote,
            bundle=bundle,
            repository=self._repository,
            target_pr=target_pr,
        )
        try:
            self._remote.cleanup(
                ref=bundle.transport_ref,
                commit_sha=verified_remote.transport_ref_commit,
            )
        except PublicationRemoteError:
            raise
        except Exception as exc:
            raise PublicationRemoteError("temporary transport ref cleanup failed") from exc
        return PublicationReceipt(
            schema_version="v1",
            origin=verified_remote.origin,
            transport_id=bundle.transport_id,
            transport_ref=bundle.transport_ref,
            transport_ref_commit=verified_remote.transport_ref_commit,
            transport_head=verified_remote.transport_head,
            producer_head=verified_remote.producer_head,
            repository=self._repository,
            target_pr=target_pr,
            bundle_digest=bundle.bundle_digest,
            passport_digest=bundle.passport_digest,
            case_id=verified_remote.case_id,
            run_id=verified_remote.run_id,
            subject_digest=verified_remote.subject_digest,
            ci_run_id=verified_remote.ci_run_id,
            ci_job_id=verified_remote.ci_job_id,
            run_attempt=verified_remote.run_attempt,
            artifact_id=verified_remote.artifact_id,
            check_id=verified_remote.check_id,
            check_url=verified_remote.check_url,
            conclusion=verified_remote.conclusion,
            workbench=verified_remote.workbench,
        )


__all__ = [
    "AuthoritativeCaseExport",
    "AuthoritativeEvidenceExport",
    "CasePublication",
    "IdempotencyConflict",
    "LocalAuthoritativeCaseSource",
    "PublicationReceipt",
    "PublicationRemote",
    "PublicationRemoteError",
    "RemotePublication",
]
