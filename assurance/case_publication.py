"""Deep module for publishing one authoritative Case through CI.

The public operation is intentionally small: build and verify the local Bundle,
hand it to one remote transport adapter, verify the returned lineage, and only
then allow that adapter to delete its exact temporary ref.  GitHub, Actions,
SQLite, provider, and token details stay behind the adapter seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .evidence_bundle import BuiltEvidenceBundle, build_evidence_bundle, verify_evidence_bundle


class PublicationRemoteError(RuntimeError):
    """The remote CI result was rejected or could not be established."""


class IdempotencyConflict(PublicationRemoteError):
    """The exact temporary ref already contains a different bundle."""


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
        evidence_root: Path,
        repository: str,
        transport_head: str,
        remote: PublicationRemote,
        prefix: str | None = None,
    ) -> None:
        if not isinstance(evidence_root, Path):
            raise TypeError("evidence_root must be a Path")
        self._evidence_root = evidence_root
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

        bundle = build_evidence_bundle(
            self._evidence_root,
            case_id=case_id,
            repository=self._repository,
            pr_number=target_pr,
            producer_head=producer_head,
            transport_head=self._transport_head,
            prefix=self._prefix,
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
    "CasePublication",
    "IdempotencyConflict",
    "PublicationReceipt",
    "PublicationRemote",
    "PublicationRemoteError",
    "RemotePublication",
]
