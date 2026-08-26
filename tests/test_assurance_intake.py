"""Focused contract and collector tests for assurance.intake (V2-P2-02)."""

import ast
import hashlib
import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    ArtifactStore,
    IntakeChangedError,
    IntakeCollectionError,
    IntakeDocument,
    IntakeFormatError,
    IntakeNotice,
    IntakePathError,
    IntakeResult,
    IntakeSnapshot,
    TaskPolicyCollector,
)
from assurance import intake as intake_module
from assurance.contracts import Evidence


SUBJECT = "sha256:" + "0" * 64
FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)

NOTICE_SEMANTICS = {
    "task_spec_not_declared": ("missing_evidence", False),
    "task_spec_not_found": ("missing_evidence", True),
    "task_title_missing": ("missing_evidence", True),
    "task_owner_missing": ("missing_evidence", True),
    "acceptance_criteria_missing": ("missing_evidence", True),
    "policy_not_declared": ("missing_evidence", False),
    "policy_not_found": ("missing_evidence", True),
    "adr_not_declared": ("unknown", False),
    "adr_not_found": ("unknown", True),
    "runbook_not_found": ("missing_evidence", True),
}

PRIOR_PUBLIC_NAMES = {
    "AcceptanceCase",
    "ChangeSubject",
    "Evidence",
    "ExecutionReceipt",
    "ExecutionStep",
    "Finding",
    "HumanDecision",
    "PolicyDecision",
    "SubjectDigestInput",
    "canonical_subject_payload",
    "changed_subject_fields",
    "compute_normalized_diff_digest",
    "compute_subject_digest",
    "normalize_line_endings",
    "normalize_repo_path",
    "normalize_repository_identity",
    "AcceptanceEvent",
    "AcceptanceBinding",
    "AcceptanceMachineState",
    "InvalidTransitionError",
    "EventConflictError",
    "StaleSubjectError",
    "apply_acceptance_event",
    "allowed_event_kinds",
    "invalidation_reasons",
    "invalidate_if_needed",
    "ArtifactStore",
    "ArtifactDigestError",
    "ArtifactNotFoundError",
    "ArtifactIntegrityError",
    "SQLiteAssuranceStore",
    "AssuranceStoreError",
    "StoreMigrationError",
    "CaseNotFoundError",
    "StoreConflictError",
    "ProjectionIntegrityError",
    "StorePersistenceError",
    "GitChange",
    "GitSnapshot",
    "GitSnapshotResult",
    "GitSnapshotCollector",
    "GitSnapshotError",
    "GitRepositoryError",
    "GitCommandError",
    "GitWorktreeChangedError",
}

NEW_PUBLIC_NAMES = {
    "IntakeDocument",
    "IntakeNotice",
    "IntakeSnapshot",
    "IntakeResult",
    "TaskPolicyCollector",
    "IntakeCollectionError",
    "IntakePathError",
    "IntakeFormatError",
    "IntakeChangedError",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write(repo: Path, rel: str, data: bytes) -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _collect(
    repo,
    store,
    task_path=None,
    policy_paths=(),
    adr_paths=(),
    runbook_paths=(),
    **overrides,
):
    kwargs = {
        "subject_digest": SUBJECT,
        "artifact_store": store,
        "task_path": task_path,
        "policy_paths": policy_paths,
        "adr_paths": adr_paths,
        "runbook_paths": runbook_paths,
        "collected_at": FIXED_TIME,
    }
    kwargs.update(overrides)
    return TaskPolicyCollector().collect(repo, **kwargs)


def _artifact_files(store):
    root = store.root
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    )


def _doc(**overrides):
    values = {
        "schema_version": "v1",
        "kind": "task_spec",
        "path": "task.md",
        "artifact_digest": "sha256:" + "a" * 64,
        "byte_size": 3,
        "title": None,
        "owner": None,
        "version": None,
        "status": None,
        "acceptance_criteria": (),
        "metadata": (),
    }
    values.update(overrides)
    return IntakeDocument(**values)


def _notice(**overrides):
    values = {
        "schema_version": "v1",
        "category": "missing_evidence",
        "code": "task_title_missing",
        "path": "task.md",
    }
    values.update(overrides)
    return IntakeNotice(**values)


def _snapshot(**overrides):
    values = {
        "schema_version": "v1",
        "subject_digest": "sha256:" + "b" * 64,
        "documents": (),
        "notices": (),
        "task_digest": None,
        "task_present": False,
        "policy_count": 0,
        "adr_count": 0,
        "runbook_count": 0,
        "manifest_artifact_digest": "sha256:" + "c" * 64,
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return IntakeSnapshot(**values)


def _evidence(**overrides):
    values = {
        "schema_version": "v1",
        "evidence_id": "ev_intake_" + "a" * 32,
        "subject_digest": "sha256:" + "b" * 64,
        "kind": "intake_documents",
        "producer": "collector.intake",
        "artifact_digest": "sha256:" + "c" * 64,
        "source_ref": "intake_documents:sha256:" + "b" * 64,
        "status": "success",
        "trust_level": "deterministic",
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return Evidence(**values)


def _result(**overrides):
    values = {
        "schema_version": "v1",
        "snapshot": _snapshot(),
        "evidence": _evidence(),
    }
    values.update(overrides)
    return IntakeResult(**values)


def test_public_api_imports_and_collect_signature():
    """P2-02 public API must be importable at package top level."""
    assert IntakeCollectionError is not None
    assert IntakeChangedError is not None
    assert IntakeFormatError is not None
    assert IntakePathError is not None
    assert IntakeDocument is not None
    assert IntakeNotice is not None
    assert IntakeSnapshot is not None
    assert IntakeResult is not None
    assert TaskPolicyCollector is not None
    signature = inspect.signature(TaskPolicyCollector.collect)
    assert list(signature.parameters) == [
        "self",
        "repository_path",
        "subject_digest",
        "artifact_store",
        "task_path",
        "policy_paths",
        "adr_paths",
        "runbook_paths",
        "collected_at",
    ]
    probe_signature = inspect.signature(TaskPolicyCollector.probe_task_digest)
    assert list(probe_signature.parameters) == [
        "self",
        "repository_path",
        "task_path",
    ]
    assert probe_signature.parameters["task_path"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert probe_signature.return_annotation is str


def test_collector_has_no_public_knobs_or_extra_methods():
    collector = TaskPolicyCollector()
    assert [name for name in dir(collector) if not name.startswith("_")] == [
        "collect",
        "probe_task_digest",
    ]


def test_probe_task_digest_repeats_raw_sha256_and_collect_snapshot_digest(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = b"---\ntitle: T\nowner: o\n---\n- [ ] probe\n"
    _write(repo, "task.md", raw)

    collector = TaskPolicyCollector()
    first = collector.probe_task_digest(repo, task_path="task.md")
    second = collector.probe_task_digest(repo, task_path="task.md")
    snapshot = _collect(
        repo,
        ArtifactStore(tmp_path / "artifacts"),
        task_path="task.md",
    ).snapshot

    assert first == second == _sha256(raw)
    assert first == snapshot.task_digest


def test_probe_task_digest_reuses_safe_intake_seams_without_path_read_bytes(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = b"---\ntitle: T\nowner: o\n---\n- [ ] probe\n"
    _write(repo, "task.md", raw)
    collector = TaskPolicyCollector()
    calls = []

    original_root = collector._resolve_repository_root
    original_validate = collector._validate_path
    original_inspect = collector._inspect_present
    original_revalidate = collector._revalidate_file

    def wrapped_root(path):
        calls.append(("root", path))
        return original_root(path)

    def wrapped_validate(path):
        calls.append(("validate", path))
        return original_validate(path)

    def wrapped_inspect(root, declared):
        calls.append(("inspect", root, declared))
        return original_inspect(root, declared)

    def wrapped_revalidate(root, path, pre, digest):
        calls.append(("revalidate", root, path, pre, digest))
        return original_revalidate(root, path, pre, digest)

    monkeypatch.setattr(collector, "_resolve_repository_root", wrapped_root)
    monkeypatch.setattr(collector, "_validate_path", wrapped_validate)
    monkeypatch.setattr(collector, "_inspect_present", wrapped_inspect)
    monkeypatch.setattr(collector, "_revalidate_file", wrapped_revalidate)

    def forbidden_read_bytes(*args, **kwargs):
        raise AssertionError("probe must use the intake safe read seam")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    digest = collector.probe_task_digest(repo, task_path="task.md")

    assert digest == _sha256(raw)
    assert [call[0] for call in calls] == [
        "root",
        "validate",
        "inspect",
        "revalidate",
    ]
    assert calls[2][1:] == (repo, [("task_spec", "task.md")])
    assert calls[3][1:3] == (repo, "task.md")
    assert calls[3][4] == digest


@pytest.mark.parametrize("task_path", ["missing.md", "../task.md"])
def test_probe_task_digest_rejects_missing_or_noncanonical_task_path(
    tmp_path,
    task_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(IntakePathError):
        TaskPolicyCollector().probe_task_digest(repo, task_path=task_path)


def test_probe_task_digest_rejects_non_path_repository_and_non_string_task(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", b"task\n")
    collector = TaskPolicyCollector()

    with pytest.raises(TypeError):
        collector.probe_task_digest(str(repo), task_path="task.md")
    with pytest.raises(TypeError):
        collector.probe_task_digest(repo, task_path=123)


def test_probe_task_digest_fails_on_same_lstat_same_size_content_drift(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _write(
        repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ] ok\n"
    )
    original = intake_module._read_regular_file

    def mutate_fingerprint(path):
        raw = original(path)
        stat_result = path.stat()
        path.write_bytes(raw.replace(b"ok", b"no"))
        os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns))
        return raw

    monkeypatch.setattr(intake_module, "_read_regular_file", mutate_fingerprint)
    with pytest.raises(IntakeChangedError, match="content"):
        TaskPolicyCollector().probe_task_digest(repo, task_path="task.md")
    assert target.read_bytes() == b"---\ntitle: T\nowner: o\n---\n- [ ] no\n"


def test_package_exports_preserve_prior_names_and_add_p2_02_api():
    assert PRIOR_PUBLIC_NAMES <= set(assurance.__all__)
    assert NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.IntakeDocument is intake_module.IntakeDocument
    assert assurance.IntakeNotice is intake_module.IntakeNotice
    assert assurance.IntakeSnapshot is intake_module.IntakeSnapshot
    assert assurance.IntakeResult is intake_module.IntakeResult
    assert assurance.TaskPolicyCollector is intake_module.TaskPolicyCollector
    assert assurance.IntakeCollectionError is intake_module.IntakeCollectionError
    assert assurance.IntakePathError is intake_module.IntakePathError
    assert assurance.IntakeFormatError is intake_module.IntakeFormatError
    assert assurance.IntakeChangedError is intake_module.IntakeChangedError


def test_exception_hierarchy_is_simple():
    assert issubclass(IntakePathError, IntakeCollectionError)
    assert issubclass(IntakeFormatError, IntakeCollectionError)
    assert issubclass(IntakeChangedError, IntakeCollectionError)
    assert issubclass(IntakeCollectionError, Exception)


def test_intake_document_field_order_frozen_extra_forbid_and_round_trip():
    assert list(IntakeDocument.model_fields) == [
        "schema_version",
        "kind",
        "path",
        "artifact_digest",
        "byte_size",
        "title",
        "owner",
        "version",
        "status",
        "acceptance_criteria",
        "metadata",
    ]
    assert IntakeDocument.model_config["frozen"] is True
    assert IntakeDocument.model_config["extra"] == "forbid"
    doc = _doc()
    assert doc.schema_version == "v1"
    with pytest.raises(ValidationError):
        _doc(schema_version="v2")
    with pytest.raises(ValidationError):
        doc.path = "mutated.md"
    with pytest.raises(ValidationError):
        IntakeDocument.model_validate({**doc.model_dump(), "unexpected": 1})
    restored = IntakeDocument.model_validate_json(doc.model_dump_json())
    assert restored == doc


def test_intake_document_path_digest_size_and_optional_text_rules():
    for bad in ("", "/x.md", "a/../x.md", "a/./x.md", "a//x.md", "a\\b.md"):
        with pytest.raises(ValidationError):
            _doc(path=bad)
    for bad in ("sha256:" + "A" * 64, "abc", None):
        with pytest.raises(ValidationError):
            _doc(artifact_digest=bad)
    for bad in (-1, True, "1", 1.5):
        with pytest.raises(ValidationError):
            _doc(byte_size=bad)
    for field in ("title", "owner", "version", "status"):
        with pytest.raises(ValidationError):
            _doc(**{field: " "})
        with pytest.raises(ValidationError):
            _doc(**{field: ""})
    assert _doc(title=None, owner=None).title is None
    assert _doc(title="ok", owner="ok").owner == "ok"


def test_intake_document_criteria_only_for_task_spec_and_order_preserved():
    with pytest.raises(ValidationError):
        _doc(kind="policy", acceptance_criteria=("x",))
    with pytest.raises(ValidationError):
        _doc(kind="adr", acceptance_criteria=("x",))
    with pytest.raises(ValidationError):
        _doc(kind="runbook", acceptance_criteria=("x",))
    with pytest.raises(ValidationError):
        _doc(acceptance_criteria=(" ",))
    with pytest.raises(ValidationError):
        _doc(acceptance_criteria=("a", "a"))
    doc = _doc(acceptance_criteria=("b", "a"))
    assert doc.acceptance_criteria == ("b", "a")


def test_intake_document_metadata_sorted_unique_and_copy_safe():
    with pytest.raises(ValidationError):
        _doc(metadata=(("b", "2"), ("a", "1")))
    with pytest.raises(ValidationError):
        _doc(metadata=(("a", "1"), ("a", "2")))
    with pytest.raises(ValidationError):
        _doc(metadata=(("A", "1"),))
    with pytest.raises(ValidationError):
        _doc(metadata=(("a-b", "1"),))
    with pytest.raises(ValidationError):
        _doc(metadata=(("1a", "1"),))
    with pytest.raises(ValidationError):
        _doc(metadata=(("a", " "),))
    with pytest.raises(ValidationError):
        _doc(metadata=(("a", ""),))
    doc = _doc(metadata=(("a", "1"), ("b", "2:3")))
    assert doc.metadata == (("a", "1"), ("b", "2:3"))
    pairs = [("a", "1"), ("b", "2")]
    criteria = ["x", "y"]
    doc = _doc(metadata=pairs, acceptance_criteria=criteria)
    pairs.append(("c", "3"))
    criteria.append("z")
    assert doc.metadata == (("a", "1"), ("b", "2"))
    assert doc.acceptance_criteria == ("x", "y")
    with pytest.raises(ValidationError):
        doc.acceptance_criteria = ("z",)
    with pytest.raises(ValidationError):
        doc.metadata = ()
    with pytest.raises(TypeError):
        doc.metadata[0] = ("z", "0")
    with pytest.raises(TypeError):
        doc.acceptance_criteria[0] = "z"


def test_intake_notice_field_order_v1_extra_forbid_and_round_trip():
    assert list(IntakeNotice.model_fields) == [
        "schema_version",
        "category",
        "code",
        "path",
    ]
    assert IntakeNotice.model_config["frozen"] is True
    assert IntakeNotice.model_config["extra"] == "forbid"
    notice = _notice()
    assert notice.schema_version == "v1"
    with pytest.raises(ValidationError):
        _notice(schema_version="v2")
    with pytest.raises(ValidationError):
        notice.path = "other.md"
    with pytest.raises(ValidationError):
        IntakeNotice.model_validate({**notice.model_dump(), "unexpected": 1})
    restored = IntakeNotice.model_validate_json(notice.model_dump_json())
    assert restored == notice
    with pytest.raises(ValidationError):
        IntakeNotice(
            schema_version="v1",
            category="missing_evidence",
            code="not_a_real_code",
            path=None,
        )


def test_intake_notice_all_codes_have_exact_semantics():
    for code, (category, requires_path) in NOTICE_SEMANTICS.items():
        path = "task.md" if requires_path else None
        notice = IntakeNotice(
            schema_version="v1",
            category=category,
            code=code,
            path=path,
        )
        assert notice.code == code
        restored = IntakeNotice.model_validate_json(notice.model_dump_json())
        assert restored == notice


def test_intake_notice_category_and_path_rules_enforced():
    for code, (expected_category, requires_path) in NOTICE_SEMANTICS.items():
        wrong_category = (
            "unknown"
            if expected_category == "missing_evidence"
            else "missing_evidence"
        )
        with pytest.raises(ValidationError):
            IntakeNotice(
                schema_version="v1",
                category=wrong_category,
                code=code,
                path="task.md" if requires_path else None,
            )
        with pytest.raises(ValidationError):
            IntakeNotice(
                schema_version="v1",
                category=expected_category,
                code=code,
                path=None if requires_path else "task.md",
            )
    with pytest.raises(ValidationError):
        IntakeNotice(
            schema_version="v1",
            category="missing_evidence",
            code="task_title_missing",
            path="a/../task.md",
        )


def test_intake_snapshot_field_order_frozen_and_round_trip():
    assert list(IntakeSnapshot.model_fields) == [
        "schema_version",
        "subject_digest",
        "documents",
        "notices",
        "task_digest",
        "task_present",
        "policy_count",
        "adr_count",
        "runbook_count",
        "manifest_artifact_digest",
        "complete",
        "collected_at",
    ]
    assert IntakeSnapshot.model_config["frozen"] is True
    assert IntakeSnapshot.model_config["extra"] == "forbid"
    snap = _snapshot()
    assert snap.schema_version == "v1"
    with pytest.raises(ValidationError):
        _snapshot(schema_version="v2")
    with pytest.raises(ValidationError):
        snap.documents = ()
    with pytest.raises(ValidationError):
        IntakeSnapshot.model_validate({**snap.model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        _snapshot(subject_digest="nope")
    with pytest.raises(ValidationError):
        _snapshot(manifest_artifact_digest="nope")
    task = _doc(path="t.md")
    valid = _snapshot(
        documents=(task,),
        task_digest=task.artifact_digest,
        task_present=True,
    )
    restored = IntakeSnapshot.model_validate_json(valid.model_dump_json())
    assert restored == valid


def test_intake_snapshot_document_order_and_unique_paths():
    policy = _doc(kind="policy", path="a.md")
    task = _doc(kind="task_spec", path="z.md")
    with pytest.raises(ValidationError):
        _snapshot(
            documents=(policy, task),
            task_digest=task.artifact_digest,
            task_present=True,
            policy_count=1,
            adr_count=0,
            runbook_count=0,
        )
    valid = _snapshot(
        documents=(task, policy),
        task_digest=task.artifact_digest,
        task_present=True,
        policy_count=1,
        adr_count=0,
        runbook_count=0,
    )
    assert [doc.kind for doc in valid.documents] == ["task_spec", "policy"]
    duplicate = _doc(kind="policy", path=task.path)
    with pytest.raises(ValidationError):
        _snapshot(
            documents=(task, duplicate),
            task_digest=task.artifact_digest,
            task_present=True,
            policy_count=1,
            adr_count=0,
            runbook_count=0,
        )


def test_intake_snapshot_task_digest_present_and_counts_agree():
    task = _doc(path="t.md")
    with pytest.raises(ValidationError):
        _snapshot(documents=(task,), task_digest=None, task_present=True)
    with pytest.raises(ValidationError):
        _snapshot(
            documents=(task,),
            task_digest="sha256:" + "f" * 64,
            task_present=True,
        )
    with pytest.raises(ValidationError):
        _snapshot(documents=(), task_digest=None, task_present=True)
    with pytest.raises(ValidationError):
        _snapshot(
            documents=(task,),
            task_digest=task.artifact_digest,
            task_present=False,
        )
    docs = (
        task,
        _doc(kind="policy", path="p.md"),
        _doc(kind="adr", path="a.md"),
        _doc(kind="runbook", path="r.md"),
    )
    with pytest.raises(ValidationError):
        _snapshot(
            documents=docs,
            task_digest=task.artifact_digest,
            task_present=True,
            policy_count=0,
            adr_count=1,
            runbook_count=1,
        )
    valid = _snapshot(
        documents=docs,
        task_digest=task.artifact_digest,
        task_present=True,
        policy_count=1,
        adr_count=1,
        runbook_count=1,
    )
    assert valid.task_digest == task.artifact_digest


def test_intake_snapshot_notice_order_unique_and_complete_rule():
    owner = IntakeNotice(
        category="missing_evidence",
        code="task_owner_missing",
        path="t.md",
    )
    title = IntakeNotice(
        category="missing_evidence",
        code="task_title_missing",
        path="t.md",
    )
    with pytest.raises(ValidationError):
        _snapshot(notices=(title, owner), complete=False)
    with pytest.raises(ValidationError):
        _snapshot(notices=(owner, owner), complete=False)
    with pytest.raises(ValidationError):
        _snapshot(notices=(owner,), complete=True)
    valid = _snapshot(notices=(owner, title), complete=False)
    assert [notice.code for notice in valid.notices] == [
        "task_owner_missing",
        "task_title_missing",
    ]
    unknown = IntakeNotice(
        category="unknown",
        code="adr_not_declared",
        path=None,
    )
    assert _snapshot(notices=(unknown,), complete=True).complete is True
    with pytest.raises(ValidationError):
        _snapshot(notices=(unknown,), complete=False)


def test_intake_result_field_order_frozen_and_exact_binding():
    assert list(IntakeResult.model_fields) == [
        "schema_version",
        "snapshot",
        "evidence",
    ]
    assert IntakeResult.model_config["frozen"] is True
    assert IntakeResult.model_config["extra"] == "forbid"
    result = _result()
    assert result.schema_version == "v1"
    with pytest.raises(ValidationError):
        _result(schema_version="v2")
    with pytest.raises(ValidationError):
        result.snapshot = _snapshot()
    with pytest.raises(ValidationError):
        IntakeResult.model_validate({**result.model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(subject_digest="sha256:" + "d" * 64))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(artifact_digest="sha256:" + "d" * 64))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(kind="other"))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(producer="other"))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(trust_level="observed"))
    with pytest.raises(ValidationError):
        _result(
            snapshot=_snapshot(complete=False),
            evidence=_evidence(status="success"),
        )
    truncated_snapshot = _snapshot(
        notices=(
            IntakeNotice(
                category="missing_evidence",
                code="task_owner_missing",
                path="t.md",
            ),
        ),
        complete=False,
    )
    truncated = _result(
        snapshot=truncated_snapshot,
        evidence=_evidence(status="truncated"),
    )
    assert truncated.snapshot.complete is False
    unknown = _snapshot(
        notices=(
            IntakeNotice(category="unknown", code="adr_not_declared", path=None),
        ),
        complete=True,
    )
    success = _result(snapshot=unknown, evidence=_evidence())
    assert success.evidence.status == "success"
    restored = IntakeResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_valid_full_collection_binds_manifest_and_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    task = (
        b"---\n"
        b"title: My Task\n"
        b"owner: alice\n"
        b"version: 1.2\n"
        b"status: active\n"
        b"priority: high\n"
        b"tags: alpha\n"
        b"---\n"
        b"# Task\n\n"
        b"- [x] first\n"
        b"- [ ] second\n"
    )
    policy = (
        b"---\nowner: security\nversion: 3\n---\npolicy body\n"
    )
    adr = b"---\nstatus: accepted\n---\nADR body\n"
    runbook = b"runbook body\n- [x] not a task criterion\n"
    _write(repo, "docs/task.md", task)
    _write(repo, "policies/access.md", policy)
    _write(repo, "adr/001.md", adr)
    _write(repo, "runbooks/release.md", runbook)
    store = ArtifactStore(tmp_path / "artifacts")
    result = _collect(
        repo,
        store,
        task_path="docs/task.md",
        policy_paths=("policies/access.md",),
        adr_paths=("adr/001.md",),
        runbook_paths=("runbooks/release.md",),
    )
    snap = result.snapshot
    assert snap.complete is True
    assert result.evidence.status == "success"
    assert snap.notices == ()
    assert [doc.kind for doc in snap.documents] == [
        "task_spec",
        "policy",
        "adr",
        "runbook",
    ]
    assert [doc.path for doc in snap.documents] == [
        "docs/task.md",
        "policies/access.md",
        "adr/001.md",
        "runbooks/release.md",
    ]
    task_doc = snap.documents[0]
    assert task_doc.artifact_digest == _sha256(task)
    assert task_doc.byte_size == len(task)
    assert task_doc.title == "My Task"
    assert task_doc.owner == "alice"
    assert task_doc.version == "1.2"
    assert task_doc.status == "active"
    assert task_doc.acceptance_criteria == ("first", "second")
    assert task_doc.metadata == (
        ("owner", "alice"),
        ("priority", "high"),
        ("status", "active"),
        ("tags", "alpha"),
        ("title", "My Task"),
        ("version", "1.2"),
    )
    policy_doc = snap.documents[1]
    assert policy_doc.title is None
    assert policy_doc.acceptance_criteria == ()
    assert policy_doc.metadata == (("owner", "security"), ("version", "3"))
    assert snap.documents[2].metadata == (("status", "accepted"),)
    assert snap.documents[3].acceptance_criteria == ()
    assert snap.task_present is True
    assert snap.task_digest == task_doc.artifact_digest
    assert (snap.policy_count, snap.adr_count, snap.runbook_count) == (1, 1, 1)
    for doc in snap.documents:
        assert store.get_bytes(doc.artifact_digest) == (
            repo / doc.path
        ).read_bytes()

    manifest = json.loads(
        store.get_bytes(snap.manifest_artifact_digest).decode("utf-8")
    )
    assert set(manifest) == {
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
    }
    assert manifest["limits"] == {
        "max_declared_paths": 64,
        "max_file_bytes": 1024 * 1024,
        "max_total_bytes": 4 * 1024 * 1024,
        "max_frontmatter_bytes": 16 * 1024,
        "max_frontmatter_items": 64,
    }
    assert "collected_at" not in json.dumps(manifest)
    assert str(repo) not in json.dumps(manifest)
    expected_payload = {
        "schema_version": "v1",
        "subject_digest": snap.subject_digest,
        "documents": [doc.model_dump(mode="json") for doc in snap.documents],
        "notices": [notice.model_dump(mode="json") for notice in snap.notices],
        "task_digest": snap.task_digest,
        "task_present": snap.task_present,
        "policy_count": snap.policy_count,
        "adr_count": snap.adr_count,
        "runbook_count": snap.runbook_count,
        "complete": snap.complete,
        "limits": manifest["limits"],
    }
    encoded = json.dumps(
        expected_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert snap.manifest_artifact_digest == _sha256(encoded)
    assert store.get_bytes(snap.manifest_artifact_digest) == encoded

    evidence = result.evidence
    assert evidence.subject_digest == snap.subject_digest == SUBJECT
    assert evidence.artifact_digest == snap.manifest_artifact_digest
    assert evidence.kind == "intake_documents"
    assert evidence.producer == "collector.intake"
    assert evidence.trust_level == "deterministic"
    assert evidence.source_ref == f"intake_documents:{SUBJECT}"
    assert str(repo) not in evidence.source_ref
    assert evidence.collected_at == FIXED_TIME
    assert len(_artifact_files(store)) == len(snap.documents) + 1


def test_fixed_time_repeat_equal_and_artifact_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ] a\n")
    _write(repo, "policy.md", b"---\nversion: 1\n---\npolicy\n")
    _write(repo, "adr.md", b"---\nstatus: accepted\n---\nadr\n")
    store = ArtifactStore(tmp_path / "artifacts")
    kwargs = dict(
        task_path="task.md",
        policy_paths=("policy.md",),
        adr_paths=("adr.md",),
    )
    first = _collect(repo, store, **kwargs)
    before = _artifact_files(store)
    second = _collect(repo, store, **kwargs)
    assert first == second
    assert first.evidence.evidence_id == second.evidence.evidence_id
    assert _artifact_files(store) == before
    manifest_text = store.get_bytes(
        first.snapshot.manifest_artifact_digest
    ).decode("utf-8")
    assert "collected_at" not in manifest_text


def test_undeclared_and_declared_missing_semantics(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    result = _collect(
        repo,
        store,
        task_path="task.md",
        policy_paths=("policies/a.md",),
        adr_paths=("adr/1.md",),
        runbook_paths=("runbooks/r.md",),
    )
    snap = result.snapshot
    assert snap.notices == (
        IntakeNotice(
            category="missing_evidence",
            code="policy_not_found",
            path="policies/a.md",
        ),
        IntakeNotice(
            category="missing_evidence",
            code="runbook_not_found",
            path="runbooks/r.md",
        ),
        IntakeNotice(
            category="missing_evidence",
            code="task_spec_not_found",
            path="task.md",
        ),
        IntakeNotice(
            category="unknown",
            code="adr_not_found",
            path="adr/1.md",
        ),
    )
    assert snap.documents == ()
    assert snap.task_present is False
    assert snap.task_digest is None
    assert snap.complete is False
    assert result.evidence.status == "truncated"


def test_undeclared_empty_tuples_produce_exact_notices(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "runbook.md", b"runbook\n")
    store = ArtifactStore(tmp_path / "artifacts")
    result = _collect(
        repo,
        store,
        task_path=None,
        policy_paths=(),
        adr_paths=(),
        runbook_paths=("runbook.md",),
    )
    snap = result.snapshot
    assert snap.notices == (
        IntakeNotice(
            category="missing_evidence",
            code="policy_not_declared",
            path=None,
        ),
        IntakeNotice(
            category="missing_evidence",
            code="task_spec_not_declared",
            path=None,
        ),
        IntakeNotice(
            category="unknown",
            code="adr_not_declared",
            path=None,
        ),
    )
    assert snap.complete is False
    assert result.evidence.status == "truncated"


def test_unknown_only_notices_keep_complete_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ] a\n")
    _write(repo, "policy.md", b"policy\n")
    _write(repo, "runbook.md", b"runbook\n")
    store = ArtifactStore(tmp_path / "artifacts")
    missing_adr = _collect(
        repo,
        store,
        task_path="task.md",
        policy_paths=("policy.md",),
        adr_paths=("adr/missing.md",),
        runbook_paths=("runbook.md",),
    )
    assert missing_adr.snapshot.notices == (
        IntakeNotice(
            category="unknown",
            code="adr_not_found",
            path="adr/missing.md",
        ),
    )
    assert missing_adr.snapshot.complete is True
    assert missing_adr.evidence.status == "success"
    empty_adr = _collect(
        repo,
        store,
        task_path="task.md",
        policy_paths=("policy.md",),
        adr_paths=(),
        runbook_paths=("runbook.md",),
    )
    assert empty_adr.snapshot.notices == (
        IntakeNotice(category="unknown", code="adr_not_declared", path=None),
    )
    assert empty_adr.snapshot.complete is True
    assert empty_adr.evidence.status == "success"


def test_present_task_missing_title_owner_criteria_notices(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", b"# Task body\n")
    _write(repo, "policy.md", b"policy\n")
    _write(repo, "adr.md", b"---\nstatus: accepted\n---\nadr\n")
    store = ArtifactStore(tmp_path / "artifacts")
    result = _collect(
        repo,
        store,
        task_path="task.md",
        policy_paths=("policy.md",),
        adr_paths=("adr.md",),
    )
    snap = result.snapshot
    task_doc = snap.documents[0]
    assert task_doc.title is None
    assert task_doc.owner is None
    assert task_doc.acceptance_criteria == ()
    assert snap.notices == (
        IntakeNotice(
            category="missing_evidence",
            code="acceptance_criteria_missing",
            path="task.md",
        ),
        IntakeNotice(
            category="missing_evidence",
            code="task_owner_missing",
            path="task.md",
        ),
        IntakeNotice(
            category="missing_evidence",
            code="task_title_missing",
            path="task.md",
        ),
    )
    assert snap.complete is False
    assert result.evidence.status == "truncated"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../x.md",
        "a/../x.md",
        "a/./x.md",
        "a//x.md",
        "./x.md",
        "/tmp/x.md",
        "//server/x.md",
        "C:/x.md",
        "a\\b.md",
        "x\x00.md",
        "x.txt",
        "x.MD",
        "README",
    ],
)
def test_invalid_declared_paths_rejected(tmp_path, bad):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(IntakePathError):
        _collect(repo, store, task_path=bad)
    assert not (store.root / "sha256").exists()


def test_wrong_types_and_invalid_subject_or_time_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    collector = TaskPolicyCollector()
    with pytest.raises(TypeError):
        collector.collect(
            str(repo),
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=123,
            artifact_store=store,
            task_path=None,
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=repo,
            task_path=None,
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=123,
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
            policy_paths=["a.md"],
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
            adr_paths=("a.md", 2),
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
            runbook_paths="a.md",
        )
    with pytest.raises(ValueError):
        collector.collect(
            repo,
            subject_digest="sha256:" + "A" * 64,
            artifact_store=store,
            task_path=None,
        )
    with pytest.raises(ValueError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
            collected_at=datetime(2026, 8, 25, 8, 0),
        )
    with pytest.raises(TypeError):
        collector.collect(
            repo,
            subject_digest=SUBJECT,
            artifact_store=store,
            task_path=None,
            collected_at="now",
        )
    assert not (store.root / "sha256").exists()


def test_repository_root_must_be_real_directory(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(IntakePathError):
        _collect(tmp_path / "missing", store, task_path=None)
    plain = tmp_path / "plain"
    plain.write_text("x")
    with pytest.raises(IntakePathError):
        _collect(plain, store, task_path=None)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(IntakePathError):
        _collect(link, store, task_path=None)


def test_duplicate_and_too_many_declared_paths_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(IntakePathError):
        _collect(repo, store, task_path="a.md", policy_paths=("a.md",))
    with pytest.raises(IntakePathError):
        _collect(
            repo,
            store,
            task_path=None,
            policy_paths=("a.md", "a.md"),
        )
    too_many = tuple(f"p{index}.md" for index in range(64))
    with pytest.raises(IntakePathError):
        _collect(repo, store, task_path="t.md", policy_paths=too_many)
    assert not (store.root / "sha256").exists()


def test_symlink_directory_and_fifo_paths_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "real.md", b"real\n")
    (repo / "link.md").symlink_to(repo / "real.md")
    _write(repo, "realdir/a.md", b"x\n")
    (repo / "docs").symlink_to(repo / "realdir", target_is_directory=True)
    (repo / "dir.md").mkdir()
    os.mkfifo(repo / "fifo.md")
    store = ArtifactStore(tmp_path / "artifacts")
    for path in ("link.md", "docs/a.md", "dir.md", "fifo.md"):
        with pytest.raises(IntakePathError):
            _collect(repo, store, task_path=path)
    assert not (store.root / "sha256").exists()


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xfe",
        b"hello\x00world",
        b"---\ntitle: x\n",
        b"---\ntitle: a\ntitle: b\n---\n",
        b"---\nTitle: x\n---\n",
        b"---\n1title: x\n---\n",
        b"---\ntitle-hyphen: x\n---\n",
        b"---\n title: x\n---\n",
        b"---\n\tindent: x\n---\n",
        b"---\n- title: x\n---\n",
        b"---\n# comment\n---\n",
        b"---\ntitle:\n---\n",
        b"---\ntitle:   \n---\n",
        b"---\ntitle: |\n---\n",
        b"---\ntitle: >\n---\n",
        b"---\ntitle: |-\n---\n",
    ],
)
def test_malformed_document_rejected(tmp_path, data):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", data)
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    assert not (store.root / "sha256").exists()


@pytest.mark.parametrize(
    "line",
    [
        "title: [a, b]",
        "title: {a: b}",
    ],
)
def test_inline_yaml_flow_collections_rejected(tmp_path, line):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", f"---\n{line}\n---\nbody\n".encode("utf-8"))
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    assert not (store.root / "sha256").exists()


def test_excessive_frontmatter_items_and_bytes_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    items = "".join(f"k{index}: v{index}\n" for index in range(65))
    _write(repo, "task.md", b"---\n" + items.encode() + b"---\nbody\n")
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    big = b"---\ntitle: " + b"x" * 17000 + b"\n---\n"
    _write(repo, "task.md", big)
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    assert not (store.root / "sha256").exists()


def test_empty_and_duplicate_checkbox_criteria_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    _write(repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ]\n")
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    _write(
        repo,
        "task.md",
        b"---\ntitle: T\nowner: o\n---\n- [ ] same\n- [x] same\n",
    )
    with pytest.raises(IntakeFormatError):
        _collect(repo, store, task_path="task.md")
    assert not (store.root / "sha256").exists()


def test_per_file_and_aggregate_size_limits_fail_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    secret = b"size-limit-secret-7f2c"
    big = secret * ((1024 * 1024 + 1024) // len(secret) + 1)
    _write(repo, "big.md", big)
    with pytest.raises(IntakePathError) as excinfo:
        _collect(repo, store, task_path=None, policy_paths=("big.md",))
    assert secret.decode("ascii") not in str(excinfo.value)
    assert not (store.root / "sha256").exists()
    (repo / "big.md").unlink()
    chunk = b"x" * 900_000
    for index in range(5):
        _write(repo, f"f{index}.md", chunk)
    with pytest.raises(IntakePathError):
        _collect(
            repo,
            store,
            task_path=None,
            policy_paths=tuple(f"f{index}.md" for index in range(5)),
        )
    assert not (store.root / "sha256").exists()


def test_read_regular_file_read_is_explicitly_bounded_to_max_file_bytes_plus_one(
    tmp_path, monkeypatch
):
    target = tmp_path / "task.md"
    target.write_bytes(b"x" * (intake_module._MAX_FILE_BYTES + 1))
    original_fdopen = os.fdopen
    read_calls = []

    def recording_fdopen(fd, *args, **kwargs):
        handle = original_fdopen(fd, *args, **kwargs)
        original_read = handle.read

        def recording_read(*read_args, **read_kwargs):
            read_calls.append((read_args, read_kwargs))
            return original_read(*read_args, **read_kwargs)

        handle.read = recording_read
        return handle

    monkeypatch.setattr(os, "fdopen", recording_fdopen)
    raw = intake_module._read_regular_file(target)
    assert read_calls == [((intake_module._MAX_FILE_BYTES + 1,), {})]
    assert len(raw) == intake_module._MAX_FILE_BYTES + 1


def test_lstat_change_during_collection_fails_closed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ] ok\n")
    store = ArtifactStore(tmp_path / "artifacts")
    original = intake_module._read_regular_file

    def mutate(path):
        raw = original(path)
        path.write_bytes(raw + b"changed\n")
        return raw

    monkeypatch.setattr(intake_module, "_read_regular_file", mutate)
    with pytest.raises(IntakeChangedError, match="task.md"):
        _collect(repo, store, task_path="task.md")
    assert not (store.root / "sha256").exists()


def test_fingerprint_change_with_same_lstat_fails_closed(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = _write(
        repo, "task.md", b"---\ntitle: T\nowner: o\n---\n- [ ] ok\n"
    )
    store = ArtifactStore(tmp_path / "artifacts")
    original = intake_module._read_regular_file

    def mutate_fingerprint(path):
        raw = original(path)
        stat_result = path.stat()
        path.write_bytes(raw.replace(b"ok", b"no"))
        os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns))
        return raw

    monkeypatch.setattr(intake_module, "_read_regular_file", mutate_fingerprint)
    with pytest.raises(IntakeChangedError, match="content"):
        _collect(repo, store, task_path="task.md")
    assert target.read_bytes() == b"---\ntitle: T\nowner: o\n---\n- [ ] no\n"
    assert not (store.root / "sha256").exists()


def test_collection_is_read_only_and_does_not_write_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    files = {
        "task.md": b"---\ntitle: T\nowner: o\n---\n- [ ] a\n",
        "policy.md": b"policy\n",
        "adr.md": b"---\nstatus: accepted\n---\nadr\n",
    }
    for rel, data in files.items():
        _write(repo, rel, data)
    before = {rel: (repo / rel).read_bytes() for rel in files}
    before_files = sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file()
    )
    store = ArtifactStore(tmp_path / "artifacts")
    _collect(
        repo,
        store,
        task_path="task.md",
        policy_paths=("policy.md",),
        adr_paths=("adr.md",),
    )
    after = {rel: (repo / rel).read_bytes() for rel in files}
    after_files = sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file()
    )
    assert before == after
    assert before_files == after_files
    assert not list(repo.rglob("sha256"))


def test_source_inspection_no_subprocess_network_model_or_glob():
    source = Path(intake_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    forbidden = {
        "subprocess",
        "glob",
        "socket",
        "urllib",
        "httpx",
        "requests",
        "openai",
        "anthropic",
        "torch",
        "transformers",
        "importlib",
    }
    assert not (imported & forbidden)
    for token in ("os.system", "os.popen", "eval(", "exec(", "__import__"):
        assert token not in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in {
                    "system",
                    "popen",
                    "glob",
                    "eval",
                    "exec",
                    "__import__",
                    "compile",
                }
            elif isinstance(func, ast.Attribute):
                assert func.attr not in {"system", "popen", "glob"}
    assert "http://" not in source
    assert "https://" not in source
