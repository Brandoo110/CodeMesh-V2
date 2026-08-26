"""Focused contract tests for the web-facing artifact integrity reader."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandObservation
from assurance.contracts import AcceptanceCase, Evidence
from assurance.intake import IntakeDocument
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_artifacts import AssuranceArtifactReader
from web.assurance_store import AssuranceWebRepository, AssuranceWebNotFoundError


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "1" * 64
RULES = "sha256:" + "2" * 64


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _new_repository(tmp_path) -> AssuranceWebRepository:
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    return repository


def _add_evidence(
    repository: AssuranceWebRepository,
    *,
    case_id: str,
    evidence: Evidence,
) -> None:
    case = AcceptanceCase(
        case_id=case_id,
        subject_digest=evidence.subject_digest,
        state="DRAFT",
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_change(
        case,
        AcceptanceBinding(
            subject_digest=evidence.subject_digest,
            policy_version="policy-v1",
            rubric_version="rubric-v1",
        ),
        {"author": "test", "risk": "medium"},
        f"create:{case_id}",
        {"case_id": case_id},
    )
    repository.collect(
        case_id,
        AcceptanceEvent(
            event_id=f"collect:{case_id}",
            subject_digest=evidence.subject_digest,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            occurred_at=NOW,
        ),
        evidence,
        f"collect:{case_id}",
        {"evidence_id": evidence.evidence_id},
    )


def _evidence(*, case_id: str, kind: str, artifact_digest: str) -> Evidence:
    return Evidence(
        evidence_id=f"evidence-{case_id}",
        subject_digest=SUBJECT,
        kind=kind,
        producer=f"collector.{kind}",
        artifact_digest=artifact_digest,
        source_ref=f"test://{case_id}",
        status="success",
        trust_level="deterministic",
        collected_at=NOW,
    )


def _intake_artifact(store: ArtifactStore, *, task_bytes: bytes) -> tuple[str, str]:
    task_digest = store.put_bytes(task_bytes)
    task = IntakeDocument(
        kind="task_spec",
        path="docs/task.md",
        artifact_digest=task_digest,
        byte_size=len(task_bytes),
        title="Task",
        owner="owner",
        acceptance_criteria=("ship",),
        metadata=(),
    )
    manifest = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "documents": [task.model_dump(mode="json")],
        "notices": [],
        "task_digest": task_digest,
        "task_present": True,
        "policy_count": 0,
        "adr_count": 0,
        "runbook_count": 0,
        "complete": True,
        "limits": {
            "max_declared_paths": 64,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "max_frontmatter_bytes": 1024,
            "max_frontmatter_items": 16,
        },
    }
    return store.put_bytes(_canonical_json(manifest)), task_digest


def _command_artifact(
    store: ArtifactStore, *, stdout: bytes, stderr: bytes
) -> tuple[str, str, str]:
    stdout_digest = store.put_bytes(stdout)
    stderr_digest = store.put_bytes(stderr)
    observation = CommandObservation(
        command_id="unit",
        kind="test",
        argv=("python", "-c", "pass"),
        cwd=".",
        outcome="failure",
        exit_code=1,
        duration_ms=3,
        stdout_artifact_digest=stdout_digest,
        stderr_artifact_digest=stderr_digest,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_truncated=False,
        stderr_truncated=False,
    )
    manifest = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "observations": [observation.model_dump(mode="json")],
        "environment_fingerprint": _digest(b"environment"),
        "complete": True,
        "all_passed": False,
        "limits": {"max_commands": 16, "read_chunk_bytes": 65536},
    }
    return store.put_bytes(_canonical_json(manifest)), stdout_digest, stderr_digest


def test_reader_lists_and_reads_git_intake_and_command_artifacts(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    repository = _new_repository(tmp_path)
    cases: list[tuple[str, str, str, bytes, tuple[str, ...]]] = []

    diff = b"diff --git a/a.txt b/a.txt\n+raw diff\n"
    diff_digest = store.put_bytes(diff)
    git = _evidence(case_id="git", kind="git_snapshot", artifact_digest=diff_digest)
    _add_evidence(repository, case_id="git", evidence=git)
    cases.append(("git", git.evidence_id, diff_digest, diff, (diff_digest,)))

    task = b"---\ntitle: Task\nowner: owner\n---\n<script>alert(1)</script>\n"
    intake_digest, task_digest = _intake_artifact(store, task_bytes=task)
    intake = _evidence(
        case_id="intake", kind="intake_documents", artifact_digest=intake_digest
    )
    _add_evidence(repository, case_id="intake", evidence=intake)
    cases.append(
        ("intake", intake.evidence_id, intake_digest, task, (intake_digest, task_digest))
    )

    stdout = b"stdout\n"
    stderr = b"stderr\n"
    command_digest, stdout_digest, stderr_digest = _command_artifact(
        store, stdout=stdout, stderr=stderr
    )
    command = _evidence(
        case_id="command", kind="command_batch", artifact_digest=command_digest
    )
    _add_evidence(repository, case_id="command", evidence=command)
    cases.append(
        (
            "command",
            command.evidence_id,
            command_digest,
            stdout,
            (command_digest, stdout_digest, stderr_digest),
        )
    )

    reader = AssuranceArtifactReader(repository, store)
    for case_id, evidence_id, top_digest, expected, expected_digests in cases:
        index = reader.list_artifacts(case_id, evidence_id)
        assert tuple(item.digest for item in index.artifacts) == expected_digests
        top = reader.read_artifact(case_id, evidence_id, top_digest)
        assert top.data == store.get_bytes(top_digest)
        assert top.digest == top_digest
        assert top.integrity_status == "SHA-256 integrity verified"
        assert top.media_type == "text/plain"
        if case_id == "git":
            assert top.data == expected


def test_reader_authorizes_task_and_command_child_digests(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    repository = _new_repository(tmp_path)

    task = b"task body\n"
    intake_digest, task_digest = _intake_artifact(store, task_bytes=task)
    intake = _evidence(
        case_id="task", kind="intake_documents", artifact_digest=intake_digest
    )
    _add_evidence(repository, case_id="task", evidence=intake)

    stdout, stderr = b"out\n", b"err\n"
    command_digest, stdout_digest, stderr_digest = _command_artifact(
        store, stdout=stdout, stderr=stderr
    )
    command = _evidence(
        case_id="logs", kind="command_batch", artifact_digest=command_digest
    )
    _add_evidence(repository, case_id="logs", evidence=command)

    reader = AssuranceArtifactReader(repository, store)
    assert reader.read_artifact("task", intake.evidence_id, task_digest).data == task
    assert (
        reader.read_artifact("logs", command.evidence_id, stdout_digest).data
        == stdout
    )
    assert (
        reader.read_artifact("logs", command.evidence_id, stderr_digest).data
        == stderr
    )


def test_reader_rejects_external_cross_case_and_unreferenced_digests(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    repository = _new_repository(tmp_path)
    first = b"first\n"
    second = b"second\n"
    first_digest = store.put_bytes(first)
    second_digest = store.put_bytes(second)
    first_evidence = _evidence(
        case_id="first", kind="git_snapshot", artifact_digest=first_digest
    )
    second_evidence = _evidence(
        case_id="second", kind="git_snapshot", artifact_digest=second_digest
    )
    _add_evidence(repository, case_id="first", evidence=first_evidence)
    _add_evidence(repository, case_id="second", evidence=second_evidence)
    reader = AssuranceArtifactReader(repository, store)
    unreferenced = store.put_bytes(b"unreferenced\n")

    with pytest.raises(AssuranceWebNotFoundError):
        reader.read_artifact("first", first_evidence.evidence_id, second_digest)
    with pytest.raises(AssuranceWebNotFoundError):
        reader.read_artifact("first", first_evidence.evidence_id, unreferenced)
    with pytest.raises(AssuranceWebNotFoundError):
        reader.read_artifact("second", first_evidence.evidence_id, first_digest)


def test_reader_fails_closed_for_missing_tampered_and_malformed_manifests(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    repository = _new_repository(tmp_path)
    manifest_digest, _ = _intake_artifact(store, task_bytes=b"task\n")
    evidence = _evidence(
        case_id="manifest", kind="intake_documents", artifact_digest=manifest_digest
    )
    _add_evidence(repository, case_id="manifest", evidence=evidence)
    reader = AssuranceArtifactReader(repository, store)
    manifest_path = store._artifact_path(manifest_digest)
    original_manifest = manifest_path.read_bytes()
    manifest_path.unlink()
    with pytest.raises(AssuranceWebNotFoundError):
        reader.list_artifacts("manifest", evidence.evidence_id)

    assert store.put_bytes(original_manifest) == manifest_digest
    malformed_digest = store.put_bytes(b'{"schema_version":"v1"}')
    malformed = _evidence(
        case_id="malformed", kind="command_batch", artifact_digest=malformed_digest
    )
    _add_evidence(repository, case_id="malformed", evidence=malformed)
    with pytest.raises(AssuranceWebNotFoundError):
        reader.list_artifacts("malformed", malformed.evidence_id)

    manifest_path.write_bytes(b"tampered")
    with pytest.raises(AssuranceWebNotFoundError):
        reader.list_artifacts("manifest", evidence.evidence_id)


def test_unknown_kind_exposes_only_verified_top_level_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    repository = _new_repository(tmp_path)
    raw = b'{"documents":[{"artifact_digest":"sha256:' + b"0" * 64 + b'"}]}'
    digest = store.put_bytes(raw)
    evidence = _evidence(case_id="unknown", kind="future_kind", artifact_digest=digest)
    _add_evidence(repository, case_id="unknown", evidence=evidence)
    reader = AssuranceArtifactReader(repository, store)

    index = reader.list_artifacts("unknown", evidence.evidence_id)
    assert len(index.artifacts) == 1
    assert index.artifacts[0].digest == digest
    assert reader.read_artifact("unknown", evidence.evidence_id, digest).data == raw


def test_return_values_do_not_expose_store_root_and_keep_script_as_plain_text(
    tmp_path,
):
    store = ArtifactStore(tmp_path / "private" / "artifacts")
    repository = _new_repository(tmp_path)
    raw = b"<script>alert('raw')</script>\n"
    manifest_digest, task_digest = _intake_artifact(store, task_bytes=raw)
    evidence = _evidence(
        case_id="script", kind="intake_documents", artifact_digest=manifest_digest
    )
    _add_evidence(repository, case_id="script", evidence=evidence)
    reader = AssuranceArtifactReader(repository, store)

    index = reader.list_artifacts("script", evidence.evidence_id)
    artifact = reader.read_artifact("script", evidence.evidence_id, task_digest)
    assert artifact.data == raw
    assert artifact.data.decode("utf-8") == raw.decode("utf-8")
    assert "&lt;script&gt;" not in artifact.data.decode("utf-8")
    assert str(tmp_path / "private" / "artifacts") not in repr(index)
    assert str(tmp_path / "private" / "artifacts") not in repr(artifact)
    assert all(
        not (item.path or "").startswith(("/", "~"))
        for item in index.artifacts
    )
