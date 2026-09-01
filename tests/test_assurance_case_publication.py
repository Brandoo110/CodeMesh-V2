from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from assurance.case_publication import (
    AuthoritativeCaseExport,
    AuthoritativeEvidenceExport,
    CasePublication,
    IdempotencyConflict,
    LocalAuthoritativeCaseSource,
    PublicationRemoteError,
    RemotePublication,
)
from assurance.entry import AssuranceArtifactReadback
from assurance.evidence_bundle import build_evidence_bundle, transport_ref_for

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
        "transport_ref": transport_ref_for(
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        ),
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
        transport_ref_for(
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        ),
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


def _authoritative_export(tmp_path: Path) -> AuthoritativeCaseExport:
    root, artifact_path, artifact_digest = _fixture(tmp_path)
    prefix = "mvp-08f-remediation-post-fix-03-"
    case_view = json.loads(
        (root / f"{prefix}authoritative-case.json").read_text(encoding="utf-8")
    )
    passport = json.loads(
        (root / f"{prefix}passport.json").read_text(encoding="utf-8")
    )
    run = json.loads((root / f"{prefix}response.json").read_text(encoding="utf-8"))
    case_view["case"] = dict(case_view)
    case_view["receipt"] = run["receipt"]
    index_path = next((root / f"{prefix}artifacts").glob("*-index.json"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return AuthoritativeCaseExport(
        case_view=case_view,
        receipt=run["receipt"],
        passport=passport,
        passport_markdown=(root / f"{prefix}passport.md").read_text(encoding="utf-8"),
        evidence=(
            AuthoritativeEvidenceExport(
                index=index,
                artifacts={artifact_digest: artifact_path.read_bytes()},
            ),
        ),
    )


class _CountingRemote:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def publish(self, **_kwargs):
        self.calls.append("publish")
        raise AssertionError("remote publish must not run for an invalid source export")

    def cleanup(self, **_kwargs):
        self.calls.append("cleanup")
        raise AssertionError("remote cleanup must not run for an invalid source export")


class _ExportApi:
    def __init__(
        self,
        exported: AuthoritativeCaseExport,
        *,
        drift: bool = False,
        artifact_mismatch: bool = False,
    ) -> None:
        self.exported = exported
        self.drift = drift
        self.artifact_mismatch = artifact_mismatch
        self.calls: list[str] = []
        self._case_reads = 0

    def get_case(self, case_id: str):
        self.calls.append("case")
        self._case_reads += 1
        case_view = deepcopy(dict(self.exported.case_view))
        if self.drift and self._case_reads == 2:
            case_view["revision"] = int(case_view["revision"]) + 1
        return case_view

    def get_receipt(self, case_id: str):
        self.calls.append("receipt")
        return dict(self.exported.receipt)

    def get_passport(self, case_id: str):
        self.calls.append("passport")
        return deepcopy(dict(self.exported.passport))

    def get_passport_markdown(self, case_id: str):
        self.calls.append("markdown")
        return self.exported.passport_markdown

    def list_artifacts(self, case_id: str, evidence_id: str):
        self.calls.append("index")
        return deepcopy(dict(self.exported.evidence[0].index))

    def read_artifact(self, case_id: str, evidence_id: str, digest: str):
        self.calls.append("artifact")
        data = next(iter(self.exported.evidence[0].artifacts.values()))
        if self.artifact_mismatch:
            data = b"mismatched artifact"
        return AssuranceArtifactReadback(
            case_id=case_id,
            evidence_id=evidence_id,
            digest=digest,
            byte_size=len(data),
            data=data,
        )


def _source_publication(
    tmp_path: Path, exported: AuthoritativeCaseExport, api: _ExportApi, remote: _CountingRemote
) -> CasePublication:
    return CasePublication(
        source=LocalAuthoritativeCaseSource(api),
        repository="acme/codemesh",
        transport_head=TRANSPORT_HEAD,
        remote=remote,
    )


def test_local_source_reads_complete_export_before_publication(tmp_path: Path) -> None:
    exported = _authoritative_export(tmp_path)
    api = _ExportApi(exported)

    result = LocalAuthoritativeCaseSource(api).export(CASE_ID)

    assert result.case_view["case_id"] == CASE_ID
    assert api.calls == ["case", "receipt", "passport", "markdown", "index", "artifact", "case"]


def test_local_source_preserves_same_digest_stream_references_and_deduplicates_bytes(
    tmp_path: Path,
) -> None:
    exported = _authoritative_export(tmp_path)
    empty_digest = "sha256:" + hashlib.sha256(b"").hexdigest()
    index = deepcopy(dict(exported.evidence[0].index))
    index["artifacts"] = [
        {
            "schema_version": "v1",
            "digest": empty_digest,
            "kind": "stdout",
            "label": "command:stdout",
            "byte_size": 0,
            "media_type": "text/plain",
            "integrity_status": "SHA-256 integrity verified",
            "role": "stdout",
            "path": None,
            "command_id": "command",
            "stream": "stdout",
        },
        {
            "schema_version": "v1",
            "digest": empty_digest,
            "kind": "stderr",
            "label": "command:stderr",
            "byte_size": 0,
            "media_type": "text/plain",
            "integrity_status": "SHA-256 integrity verified",
            "role": "stderr",
            "path": None,
            "command_id": "command",
            "stream": "stderr",
        },
    ]
    case_view = deepcopy(dict(exported.case_view))
    passport = deepcopy(dict(exported.passport))
    case_view["evidence"][0]["artifact_digest"] = empty_digest
    passport["evidence"][0]["artifact_digest"] = empty_digest
    duplicate_export = replace(
        exported,
        case_view=case_view,
        passport=passport,
        evidence=(
            AuthoritativeEvidenceExport(index=index, artifacts={empty_digest: b""}),
        ),
    )

    class DigestAwareApi(_ExportApi):
        def read_artifact(self, case_id: str, evidence_id: str, digest: str):
            self.calls.append("artifact")
            data = self.exported.evidence[0].artifacts[digest]
            return AssuranceArtifactReadback(
                case_id=case_id,
                evidence_id=evidence_id,
                digest=digest,
                byte_size=len(data),
                data=data,
            )

    result = LocalAuthoritativeCaseSource(
        DigestAwareApi(duplicate_export)
    ).export(CASE_ID)

    references = result.evidence[0].index["artifacts"]
    assert [(item["role"], item["stream"]) for item in references] == [
        ("stdout", "stdout"),
        ("stderr", "stderr"),
    ]
    assert result.evidence[0].artifacts == {empty_digest: b""}


def test_case_drift_from_source_makes_zero_remote_calls(tmp_path: Path) -> None:
    exported = _authoritative_export(tmp_path)
    api = _ExportApi(exported, drift=True)
    remote = _CountingRemote()

    with pytest.raises(PublicationRemoteError, match="changed"):
        _source_publication(tmp_path, exported, api, remote).publish(
            case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD
        )

    assert remote.calls == []


def test_artifact_mismatch_from_source_makes_zero_remote_calls(tmp_path: Path) -> None:
    exported = _authoritative_export(tmp_path)
    api = _ExportApi(exported, artifact_mismatch=True)
    remote = _CountingRemote()

    with pytest.raises(PublicationRemoteError, match="artifact readback"):
        _source_publication(tmp_path, exported, api, remote).publish(
            case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD
        )

    assert remote.calls == []


def test_publication_builds_from_authoritative_source_without_evidence_root(
    tmp_path: Path,
) -> None:
    exported = _authoritative_export(tmp_path)

    class FakeSource:
        def export(self, case_id: str) -> AuthoritativeCaseExport:
            assert case_id == CASE_ID
            return exported

    class DynamicRemote:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def publish(self, *, bundle, target_pr: int, producer_head: str, transport_head: str):
            self.calls.append("publish")
            base = _remote()
            workbench = {
                "origin": "local_authoritative_bundle",
                "transport_id": bundle.transport_id,
                "bundle_digest": bundle.bundle_digest,
                "transport_ref": bundle.transport_ref,
                "transport_head": bundle.transport_head,
                "producer_head": bundle.producer_head,
                "repository": "acme/codemesh",
                "target_pr": target_pr,
                "case_id": bundle.case_id,
                "run_id": bundle.run_id,
                "subject_digest": bundle.subject_digest,
                "passport_digest": bundle.passport_digest,
            }
            return replace(
                base,
                passport_digest=bundle.passport_digest,
                transport_id=bundle.transport_id,
                transport_ref=bundle.transport_ref,
                transport_head=transport_head,
                producer_head=producer_head,
                case_id=bundle.case_id,
                run_id=bundle.run_id,
                subject_digest=bundle.subject_digest,
                workbench=workbench,
            )

        def cleanup(self, **_kwargs) -> None:
            self.calls.append("cleanup")

    remote = DynamicRemote()
    publication = CasePublication(
        source=FakeSource(),
        repository="acme/codemesh",
        transport_head=TRANSPORT_HEAD,
        remote=remote,
    )

    receipt = publication.publish(case_id=CASE_ID, target_pr=2, producer_head=PRODUCER_HEAD)

    assert receipt.case_id == CASE_ID
    assert [name for name in remote.calls] == ["publish", "cleanup"]
