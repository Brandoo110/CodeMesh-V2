from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from assurance.case_publication import (
    CasePublication,
    IdempotencyConflict,
    PublicationRemoteError,
    RemotePublication,
)
from assurance.evidence_bundle import build_evidence_bundle

from .test_assurance_evidence_bundle import (
    CASE_ID,
    PRODUCER_HEAD,
    SUBJECT,
    TRANSPORT_HEAD,
    _fixture,
)


class FakeRemote:
    def __init__(self, result: RemotePublication | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def publish(self, *, bundle, target_pr: int, producer_head: str, transport_head: str):
        self.calls.append(("publish", (bundle, target_pr, producer_head, transport_head)))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def cleanup(self, *, ref: str, commit_sha: str) -> None:
        self.calls.append(("cleanup", (ref, commit_sha)))


def _remote(**overrides: object) -> RemotePublication:
    values: dict[str, object] = {
        "transport_ref": "refs/heads/codex/evidence/" + PRODUCER_HEAD,
        "transport_ref_commit": "c" * 40,
        "transport_head": TRANSPORT_HEAD,
        "ci_run_id": "9001",
        "ci_job_id": "job-1",
        "run_attempt": 1,
        "artifact_id": "artifact-1",
        "check_id": 7001,
        "check_url": "https://github.com/acme/codemesh/runs/7001",
        "conclusion": "action_required",
        "case_id": CASE_ID,
        "run_id": "run_fixture",
        "subject_digest": SUBJECT,
        "producer_head": PRODUCER_HEAD,
        "passport_digest": "sha256:" + "2" * 64,
        "transport_id": "transport-placeholder",
        "origin": "local_authoritative_bundle",
        "workbench": {"origin": "local_authoritative_bundle"},
    }
    values.update(overrides)
    return RemotePublication(**values)


def _publication(tmp_path: Path, remote: FakeRemote) -> CasePublication:
    root, _, _ = _fixture(tmp_path)
    bundle = build_evidence_bundle(
        root,
        case_id=CASE_ID,
        repository="acme/codemesh",
        pr_number=2,
        producer_head=PRODUCER_HEAD,
        transport_head=TRANSPORT_HEAD,
    )
    if remote.result is not None:
        remote.result = replace(
            remote.result,
            passport_digest=bundle.passport_digest,
            transport_id=bundle.transport_id,
            workbench={
                **remote.result.workbench,
                "origin": "local_authoritative_bundle",
                "transport_id": bundle.transport_id,
                "bundle_digest": bundle.bundle_digest,
                "transport_ref": bundle.transport_ref,
                "transport_head": bundle.transport_head,
                "producer_head": bundle.producer_head,
                "repository": "acme/codemesh",
                "target_pr": 2,
                "case_id": bundle.case_id,
                "run_id": bundle.run_id,
                "subject_digest": bundle.subject_digest,
                "passport_digest": bundle.passport_digest,
            },
        )
    return CasePublication(
        evidence_root=root,
        repository="acme/codemesh",
        transport_head=TRANSPORT_HEAD,
        remote=remote,
    )


def test_publication_reads_back_exact_remote_lineage_before_cleanup(tmp_path: Path) -> None:
    remote = FakeRemote(_remote())
    publication = _publication(tmp_path, remote)

    receipt = publication.publish(case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD)

    assert receipt.case_id == CASE_ID
    assert receipt.run_id == "run_fixture"
    assert receipt.subject_digest == SUBJECT
    assert receipt.producer_head == PRODUCER_HEAD
    assert receipt.transport_head == TRANSPORT_HEAD
    assert receipt.origin == "local_authoritative_bundle"
    assert [name for name, _ in remote.calls] == ["publish", "cleanup"]
    assert remote.calls[-1][1] == (
        "refs/heads/codex/evidence/" + PRODUCER_HEAD,
        "c" * 40,
    )


def test_publication_fails_closed_and_does_not_cleanup_unknown_remote_result(
    tmp_path: Path,
) -> None:
    remote = FakeRemote(error=PublicationRemoteError("workflow result is unknown"))
    publication = _publication(tmp_path, remote)

    with pytest.raises(PublicationRemoteError, match="unknown"):
        publication.publish(case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD)
    assert [name for name, _ in remote.calls] == ["publish"]


def test_publication_rejects_remote_lineage_mismatch_without_cleanup(tmp_path: Path) -> None:
    remote = FakeRemote(_remote(case_id="case_other"))
    publication = _publication(tmp_path, remote)

    with pytest.raises(PublicationRemoteError, match="case"):
        publication.publish(case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD)
    assert [name for name, _ in remote.calls] == ["publish"]


def test_publication_surfaces_idempotency_conflict_without_overwrite(tmp_path: Path) -> None:
    remote = FakeRemote(error=IdempotencyConflict("temporary ref has another bundle"))
    publication = _publication(tmp_path, remote)

    with pytest.raises(IdempotencyConflict, match="another bundle"):
        publication.publish(case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD)
    assert [name for name, _ in remote.calls] == ["publish"]
