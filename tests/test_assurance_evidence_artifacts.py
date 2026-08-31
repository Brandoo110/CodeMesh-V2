"""Focused contracts for the Case-independent Evidence artifact resolver."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import assurance.evidence_artifacts as evidence_artifacts_module
from assurance.artifacts import ArtifactStore
from assurance.commands import CommandObservation
from assurance.contracts import Evidence
from assurance.evidence_artifacts import (
    ArtifactReference,
    AuthorizedArtifactIndex,
    EvidenceArtifactError,
    EvidenceArtifactResolver,
    ResolvedEvidenceArtifacts,
)
from assurance.intake import IntakeDocument


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "1" * 64


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _evidence(kind: str, digest: str) -> Evidence:
    return Evidence(
        evidence_id=f"evidence-{kind}",
        subject_digest=SUBJECT,
        kind=kind,
        producer=f"collector.{kind}",
        artifact_digest=digest,
        source_ref=f"test://{kind}",
        status="success",
        trust_level="deterministic",
        collected_at=NOW,
    )


def _intake_artifact(
    store: ArtifactStore,
    *,
    task_bytes: bytes = b"task body\n",
    manifest_updates: dict[str, object] | None = None,
    document_updates: dict[str, object] | None = None,
) -> tuple[str, str, bytes]:
    task_digest = store.put_bytes(task_bytes)
    document = IntakeDocument(
        kind="task_spec",
        path="docs/task.md",
        artifact_digest=task_digest,
        byte_size=len(task_bytes),
        title="Task",
        owner="owner",
        acceptance_criteria=("ship",),
        metadata=(),
    ).model_dump(mode="json")
    if document_updates:
        document.update(document_updates)
    manifest: dict[str, object] = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "documents": [document],
        "notices": [],
        "task_digest": task_digest,
        "task_present": True,
        "policy_count": 0,
        "adr_count": 0,
        "runbook_count": 0,
        "complete": True,
        "limits": {
            "max_declared_paths": 64,
            "max_file_bytes": 1024 * 1024,
            "max_total_bytes": 4 * 1024 * 1024,
            "max_frontmatter_bytes": 16 * 1024,
            "max_frontmatter_items": 64,
        },
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    raw = _json(manifest)
    return store.put_bytes(raw), task_digest, raw


def _command_artifact(
    store: ArtifactStore,
    *,
    stdout: bytes = b"stdout\n",
    stderr: bytes = b"stderr\n",
    manifest_updates: dict[str, object] | None = None,
) -> tuple[str, str, str, bytes]:
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
    manifest: dict[str, object] = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "observations": [observation.model_dump(mode="json")],
        "environment_fingerprint": _digest(b"environment"),
        "complete": True,
        "all_passed": False,
        "limits": {"max_commands": 16, "read_chunk_bytes": 65_536},
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    raw = _json(manifest)
    return store.put_bytes(raw), stdout_digest, stderr_digest, raw


def test_resolver_indexes_and_reads_git_top_level_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"diff --git a/a.txt b/a.txt\n+line\n"
    digest = store.put_bytes(payload)
    evidence = _evidence("git_snapshot", digest)

    index = EvidenceArtifactResolver.index(
        evidence,
        artifact_store=store,
        subject_digest=SUBJECT,
    )

    assert index.evidence_kind == "git_snapshot"
    assert index.subject_digest == SUBJECT
    assert index.top_level_digest == digest
    assert tuple(item.digest for item in index.artifacts) == (digest,)
    verified = EvidenceArtifactResolver.read(
        index,
        evidence=evidence,
        subject_digest=SUBJECT,
        artifact_store=store,
        digest=digest,
    )
    assert verified.evidence_id == "evidence-git_snapshot"
    assert verified.data == payload
    assert verified.byte_size == len(payload)


@pytest.mark.parametrize("byte_size", (262_145, 1_048_576))
def test_resolver_allows_git_top_level_through_snapshot_cap(tmp_path, byte_size):
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"x" * byte_size
    digest = store.put_bytes(payload)
    evidence = _evidence("git_snapshot", digest)

    resolved = EvidenceArtifactResolver.resolve(
        evidence,
        artifact_store=store,
        subject_digest=SUBJECT,
    )

    assert resolved.index.artifacts[0].byte_size == byte_size
    assert resolved.artifacts[0].data == payload


def test_resolver_rejects_git_top_level_above_snapshot_cap(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"x" * 1_048_577
    digest = store.put_bytes(payload)

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.resolve(
            _evidence("git_snapshot", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


@pytest.mark.parametrize(
    ("kind", "byte_size"),
    (
        ("intake_documents", 4_718_593),
        ("command_batch", 262_145),
        ("future_kind", 262_145),
    ),
)
def test_resolver_keeps_non_git_and_unknown_top_level_cap(
    tmp_path, kind, byte_size
):
    store = ArtifactStore(tmp_path / "artifacts")
    digest = store.put_bytes(b"x" * byte_size)

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence(kind, digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolver_indexes_and_reads_intake_and_command_child_closures(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    intake_digest, task_digest, _ = _intake_artifact(store)
    command_digest, stdout_digest, stderr_digest, _ = _command_artifact(store)
    intake_evidence = _evidence("intake_documents", intake_digest)
    command_evidence = _evidence("command_batch", command_digest)

    intake_index = EvidenceArtifactResolver.index(
        intake_evidence,
        artifact_store=store,
        subject_digest=SUBJECT,
    )
    command_index = EvidenceArtifactResolver.index(
        command_evidence,
        artifact_store=store,
        subject_digest=SUBJECT,
    )

    assert tuple(item.digest for item in intake_index.artifacts) == (
        intake_digest,
        task_digest,
    )
    assert len(intake_index.intake_documents) == 1
    assert intake_index.intake_documents[0].path == "docs/task.md"
    assert intake_index.command_observations == ()
    assert intake_index.artifacts[1].role == "document"
    assert intake_index.artifacts[1].path == "docs/task.md"
    assert tuple(item.digest for item in command_index.artifacts) == (
        command_digest,
        stdout_digest,
        stderr_digest,
    )
    assert tuple(item.role for item in command_index.artifacts) == (
        "top_level",
        "stdout",
        "stderr",
    )
    assert len(command_index.command_observations) == 1
    assert command_index.command_observations[0].command_id == "unit"
    assert command_index.intake_documents == ()
    assert EvidenceArtifactResolver.read(
        intake_index,
        evidence=intake_evidence,
        subject_digest=SUBJECT,
        artifact_store=store,
        digest=task_digest,
    ).data == b"task body\n"
    assert EvidenceArtifactResolver.read(
        command_index,
        evidence=command_evidence,
        subject_digest=SUBJECT,
        artifact_store=store,
        digest=stdout_digest,
    ).data == b"stdout\n"
    assert EvidenceArtifactResolver.read(
        command_index,
        evidence=command_evidence,
        subject_digest=SUBJECT,
        artifact_store=store,
        digest=stderr_digest,
    ).data == b"stderr\n"


def test_resolver_binds_subject_and_top_level_digest_and_rejects_unreferenced(
    tmp_path,
):
    store = ArtifactStore(tmp_path / "artifacts")
    top_digest, task_digest, _ = _intake_artifact(store)
    evidence = _evidence("intake_documents", top_digest)
    index = EvidenceArtifactResolver.index(
        evidence, artifact_store=store, subject_digest=SUBJECT
    )
    orphan_digest = store.put_bytes(b"orphan\n")

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            evidence,
            artifact_store=store,
            subject_digest="sha256:" + "2" * 64,
        )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=orphan_digest,
        )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest="sha256:" + "0" * 64,
        )
    assert task_digest != orphan_digest


@pytest.mark.parametrize("mutation", ("missing", "tampered", "symlink", "directory"))
def test_resolver_fails_closed_for_unsafe_cas_files(tmp_path, mutation):
    store = ArtifactStore(tmp_path / "artifacts")
    digest, _, _ = _intake_artifact(store)
    target = store._artifact_path(digest)
    original = target.read_bytes()

    if mutation == "missing":
        target.unlink()
    elif mutation == "tampered":
        target.write_bytes(b"tampered")
    elif mutation == "symlink":
        target.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(original)
        target.symlink_to(outside)
    else:
        target.unlink()
        target.mkdir()

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolver_enforces_top_level_and_read_byte_caps(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    oversized_unknown = store.put_bytes(b"u" * (262_144 + 1))
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("future_kind", oversized_unknown),
            artifact_store=store,
            subject_digest=SUBJECT,
        )

    digest, _, _ = _intake_artifact(store)
    evidence = _evidence("intake_documents", digest)
    index = EvidenceArtifactResolver.index(
        evidence,
        artifact_store=store,
        subject_digest=SUBJECT,
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=digest,
            max_bytes=index.artifacts[0].byte_size - 1,
        )


@pytest.mark.parametrize(
    "raw",
    (
        b"\xff",
        b'{"schema_version":"v1","schema_version":"v1"}',
        b'{"schema_version":"v1","subject_digest":NaN}',
    ),
)
def test_resolver_rejects_invalid_utf8_duplicate_keys_and_nan(tmp_path, raw):
    store = ArtifactStore(tmp_path / "artifacts")
    digest = store.put_bytes(raw)
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


@pytest.mark.parametrize(
    "manifest_updates",
    (
        {"unexpected": True},
        {"schema_version": "v2"},
        {"limits": {
            "max_declared_paths": 65,
            "max_file_bytes": 1024 * 1024,
            "max_total_bytes": 4 * 1024 * 1024,
            "max_frontmatter_bytes": 16 * 1024,
            "max_frontmatter_items": 64,
        }},
        {"limits": {
            "max_declared_paths": 64,
            "max_file_bytes": 1024 * 1024,
            "max_total_bytes": 4 * 1024 * 1024,
            "max_frontmatter_bytes": 16 * 1024,
            "max_frontmatter_items": 64,
            "extra_limit": 1,
        }},
        {"limits": {
            "max_declared_paths": 64,
            "max_file_bytes": 1024 * 1024 + 1,
            "max_total_bytes": 4 * 1024 * 1024,
            "max_frontmatter_bytes": 16 * 1024,
            "max_frontmatter_items": 64,
        }},
    ),
)
def test_resolver_rejects_extra_schema_and_widened_limits(tmp_path, manifest_updates):
    store = ArtifactStore(tmp_path / "artifacts")
    digest, _, _ = _intake_artifact(store, manifest_updates=manifest_updates)
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolver_rejects_strict_child_schema_and_command_limits(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    intake_digest, _, _ = _intake_artifact(
        store, document_updates={"unexpected": True}
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", intake_digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )

    command_digest, _, _, _ = _command_artifact(
        store,
        manifest_updates={
            "limits": {"max_commands": 17, "read_chunk_bytes": 65_536}
        },
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("command_batch", command_digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolver_revalidates_model_copy_and_model_construct_inputs(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    digest, _, _ = _intake_artifact(store)
    evidence = _evidence("intake_documents", digest)

    copied_evidence = evidence.model_copy(
        update={"subject_digest": "not-a-digest"}
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            copied_evidence, artifact_store=store, subject_digest=SUBJECT
        )

    constructed_evidence = Evidence.model_construct(
        **{**evidence.model_dump(), "artifact_digest": "not-a-digest"}
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            constructed_evidence, artifact_store=store, subject_digest=SUBJECT
        )

    valid_index = EvidenceArtifactResolver.index(
        evidence, artifact_store=store, subject_digest=SUBJECT
    )
    copied_index = valid_index.model_copy(
        update={"top_level_digest": "sha256:" + "0" * 64}
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            copied_index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=digest,
        )

    constructed_index = AuthorizedArtifactIndex.model_construct(
        **{
            **valid_index.model_dump(mode="python"),
            "artifacts": valid_index.artifacts,
            "intake_documents": valid_index.intake_documents,
            "subject_digest": "not-a-digest",
        }
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            constructed_index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=digest,
        )


def test_resolver_rejects_forged_child_entries_after_authoritative_reindex(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    digest, task_digest, _ = _intake_artifact(store)
    evidence = _evidence("intake_documents", digest)
    valid_index = EvidenceArtifactResolver.index(
        evidence, artifact_store=store, subject_digest=SUBJECT
    )
    orphan_digest = store.put_bytes(b"orphan\n")
    forged_child = valid_index.artifacts[1].model_copy(
        update={"digest": orphan_digest, "byte_size": len(b"orphan\n")}
    )
    copied_index = valid_index.model_copy(
        update={
            "artifacts": (valid_index.artifacts[0], forged_child),
        }
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            copied_index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=orphan_digest,
        )

    forged_constructed_child = ArtifactReference.model_construct(
        **{
            **valid_index.artifacts[1].model_dump(mode="python"),
            "digest": orphan_digest,
            "byte_size": len(b"orphan\n"),
        }
    )
    constructed_data = {
        **valid_index.model_dump(mode="python"),
        "artifacts": (valid_index.artifacts[0], forged_constructed_child),
        "intake_documents": valid_index.intake_documents,
    }
    constructed_index = AuthorizedArtifactIndex.model_construct(**constructed_data)
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            constructed_index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=orphan_digest,
        )
    assert task_digest != orphan_digest


def test_authorized_index_projection_is_typed_and_kind_bound(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    intake_digest, _, _ = _intake_artifact(store)
    command_digest, _, _, _ = _command_artifact(store)
    intake_evidence = _evidence("intake_documents", intake_digest)
    command_evidence = _evidence("command_batch", command_digest)
    intake_index = EvidenceArtifactResolver.index(
        intake_evidence, artifact_store=store, subject_digest=SUBJECT
    )
    command_index = EvidenceArtifactResolver.index(
        command_evidence, artifact_store=store, subject_digest=SUBJECT
    )

    round_tripped = AuthorizedArtifactIndex.model_validate(
        intake_index.model_dump(mode="python")
    )
    assert round_tripped == intake_index
    assert intake_index.intake_complete is True
    assert intake_index.command_complete is None
    assert intake_index.command_all_passed is None
    assert command_index.intake_complete is None
    assert command_index.command_complete is True
    assert command_index.command_all_passed is False
    with pytest.raises(ValidationError):
        AuthorizedArtifactIndex(
            evidence_id=intake_index.evidence_id,
            evidence_kind="intake_documents",
            subject_digest=intake_index.subject_digest,
            top_level_digest=intake_index.top_level_digest,
            artifacts=intake_index.artifacts,
            intake_documents=intake_index.intake_documents,
            command_observations=command_index.command_observations,
        )
    with pytest.raises(ValidationError):
        AuthorizedArtifactIndex(
            evidence_id=command_index.evidence_id,
            evidence_kind="future_kind",
            subject_digest=command_index.subject_digest,
            top_level_digest=command_index.top_level_digest,
            artifacts=command_index.artifacts,
            command_observations=command_index.command_observations,
        )


@pytest.mark.parametrize(
    ("field", "value_kind"),
    (
        ("evidence_id", "text"),
        ("evidence_kind", "text"),
        ("subject_digest", "other_subject"),
        ("top_level_digest", "orphan"),
    ),
)
def test_resolver_does_not_trust_forged_unknown_index_binding(
    tmp_path, field, value_kind
):
    store = ArtifactStore(tmp_path / "artifacts")
    raw = b'{"opaque":true}\n'
    digest = store.put_bytes(raw)
    orphan_digest = store.put_bytes(b"orphan\n")
    evidence = _evidence("future_kind", digest)
    index = EvidenceArtifactResolver.index(
        evidence, artifact_store=store, subject_digest=SUBJECT
    )
    value = {
        "text": "forged-binding",
        "other_subject": "sha256:" + "2" * 64,
        "orphan": orphan_digest,
    }[value_kind]
    forged_index = index.model_copy(update={field: value})

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            forged_index,
            evidence=evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=digest,
        )

    forged_evidence = evidence.model_copy(
        update={"artifact_digest": orphan_digest}
    )
    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.read(
            index,
            evidence=forged_evidence,
            subject_digest=SUBJECT,
            artifact_store=store,
            digest=orphan_digest,
        )


def test_resolver_rejects_deeply_recursive_json_manifest(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    raw = b'{"nested":' + (b"[" * 2_000) + b"0" + (b"]" * 2_000) + b"}"
    digest = store.put_bytes(raw)

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolver_rejects_intermediate_prefix_symlink(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    digest, _, _ = _intake_artifact(store)
    target = store._artifact_path(digest)
    prefix = target.parent
    real_prefix = tmp_path / "real-prefix"
    prefix.rename(real_prefix)
    prefix.symlink_to(real_prefix, target_is_directory=True)

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.index(
            _evidence("intake_documents", digest),
            artifact_store=store,
            subject_digest=SUBJECT,
        )


def test_resolve_builds_one_authoritative_closure_and_returns_all_bytes(
    tmp_path, monkeypatch
):
    store = ArtifactStore(tmp_path / "artifacts")
    top_digest, stdout_digest, stderr_digest, top_raw = _command_artifact(store)
    evidence = _evidence("command_batch", top_digest)
    original_index = evidence_artifacts_module._index_from_binding
    original_parse = evidence_artifacts_module._parse_command_manifest
    original_read = evidence_artifacts_module._read_cas_bytes
    calls = {"index": 0, "parse": 0, "digests": []}

    def counted_index(*args, **kwargs):
        calls["index"] += 1
        return original_index(*args, **kwargs)

    def counted_parse(*args, **kwargs):
        calls["parse"] += 1
        return original_parse(*args, **kwargs)

    def counted_read(*args, **kwargs):
        digest = args[1] if len(args) > 1 else kwargs["digest"]
        calls["digests"].append(digest)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(
        evidence_artifacts_module, "_index_from_binding", counted_index
    )
    monkeypatch.setattr(
        evidence_artifacts_module, "_parse_command_manifest", counted_parse
    )
    monkeypatch.setattr(
        evidence_artifacts_module, "_read_cas_bytes", counted_read
    )

    resolved = EvidenceArtifactResolver.resolve(
        evidence, artifact_store=store, subject_digest=SUBJECT
    )

    assert type(resolved) is ResolvedEvidenceArtifacts
    assert calls["index"] == 1
    assert calls["parse"] == 1
    assert calls["digests"] == [top_digest, stdout_digest, stderr_digest]
    assert resolved.index.command_complete is True
    assert resolved.index.command_all_passed is False
    assert tuple(item.digest for item in resolved.artifacts) == (
        top_digest,
        stdout_digest,
        stderr_digest,
    )
    assert resolved.artifacts[0].data == top_raw
    assert resolved.artifacts[1].data == b"stdout\n"
    assert resolved.artifacts[2].data == b"stderr\n"


def test_resolve_rejects_wrong_subject_missing_and_tampered_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    top_digest, task_digest, _ = _intake_artifact(store)
    evidence = _evidence("intake_documents", top_digest)

    with pytest.raises(EvidenceArtifactError):
        EvidenceArtifactResolver.resolve(
            evidence,
            artifact_store=store,
            subject_digest="sha256:" + "2" * 64,
        )

    store._artifact_path(task_digest).write_bytes(b"tampered\n")
    with pytest.raises(EvidenceArtifactError) as exc_info:
        EvidenceArtifactResolver.resolve(
            evidence, artifact_store=store, subject_digest=SUBJECT
        )
    assert str(exc_info.value) == EvidenceArtifactError.message
    assert task_digest not in str(exc_info.value)
