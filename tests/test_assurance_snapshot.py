"""Focused contract and collector tests for assurance.snapshot (V2-P2-01A/B)."""

import hashlib
import inspect
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    ArtifactStore,
    GitChange,
    GitCommandError,
    GitRepositoryError,
    GitSnapshot,
    GitSnapshotCollector,
    GitSnapshotError,
    GitSnapshotResult,
    GitWorktreeChangedError,
)
from assurance import snapshot as snapshot_module
from assurance.contracts import Evidence
from assurance.digests import (
    SubjectDigestInput,
    compute_subject_digest,
)


IDENTITY = "codemesh/fixture"
TASK = "sha256:" + "1" * 64
ATTACHMENT = "sha256:" + "2" * 64
POLICY = "policy-v1"
RUBRIC = "rubric-v1"
FIXED_TIME = datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(repo, *args):
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"fixture git {args!r} failed: "
            f"{proc.stderr.decode('utf-8', errors='replace')}"
        )
    return proc.stdout


def _init_repo(tmp_path, files=None, message="init"):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    if files:
        for name, content in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", message)
    return repo


def _collect(repo, store, collector=None, collected_at=FIXED_TIME, **overrides):
    collector = collector or GitSnapshotCollector()
    kwargs = {
        "repository_path": repo,
        "repository_identity": IDENTITY,
        "base_ref": "HEAD",
        "task_digest": TASK,
        "policy_version": POLICY,
        "rubric_version": RUBRIC,
        "artifact_store": store,
        "collected_at": collected_at,
    }
    kwargs.update(overrides)
    return collector.collect(**kwargs)


def _artifact_path(store, digest):
    hex_digest = digest[7:]
    return store.root / "sha256" / hex_digest[:2] / hex_digest[2:]


class _FakeStream:
    """Deterministic in-memory pipe with bounded read requests."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._lock = threading.Lock()
        self.closed = False
        self.read_sizes = []

    def read(self, size):
        with self._lock:
            self.read_sizes.append(size)
            if self.closed or not self._chunks:
                return b""
            return self._chunks.pop(0)

    def close(self):
        with self._lock:
            self.closed = True


class _ExplodingStream:
    """Pipe stand-in whose read fails with a controlled payload."""

    def __init__(self, error_type, payload):
        self._error_type = error_type
        self._payload = payload
        self.closed = False

    def read(self, size):
        raise self._error_type(self._payload)

    def close(self):
        self.closed = True


class _FakeProcess:
    """Controllable Popen stand-in that records terminate/kill/wait/close."""

    def __init__(self, stdout, stderr, returncode=0, fail_wait=False):
        self.stdout = stdout if hasattr(stdout, "read") else _FakeStream(stdout)
        self.stderr = stderr if hasattr(stderr, "read") else _FakeStream(stderr)
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._fail_wait = fail_wait
        self._killed = False

    def poll(self):
        return self.returncode if self._killed else None

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._fail_wait and not self._killed:
            raise subprocess.TimeoutExpired("git", timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
        self._killed = True


def _expected_manifest_digest(snap, collector):
    payload = {
        "schema_version": "v1",
        "repository": snap.repository,
        "base_revision": snap.base_revision,
        "head_revision": snap.head_revision,
        "scope": snap.scope,
        "worktree_dirty": snap.worktree_dirty,
        "changes": [change.model_dump(mode="json") for change in snap.changes],
        "changed_files_total": snap.changed_files_total,
        "diff_artifact_digest": snap.diff_artifact_digest,
        "diff_bytes": snap.diff_bytes,
        "diff_truncated": snap.diff_truncated,
        "files_truncated": snap.files_truncated,
        "ignored_files_lower_bound": snap.ignored_files_lower_bound,
        "ignored_scan_truncated": snap.ignored_scan_truncated,
        "large_file_paths": list(snap.large_file_paths),
        "submodule_paths": list(snap.submodule_paths),
        "omissions": list(snap.omissions),
        "complete": snap.complete,
        "limits": {
            "max_diff_bytes": collector.max_diff_bytes,
            "max_files": collector.max_files,
            "max_file_bytes": collector.max_file_bytes,
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _change(**overrides):
    values = {
        "schema_version": "v1",
        "path": "a.txt",
        "old_path": None,
        "status": "modified",
        "current_size": 1,
        "current_digest": "sha256:" + "a" * 64,
        "binary": False,
        "large_file": False,
        "submodule": False,
    }
    values.update(overrides)
    return GitChange(**values)


def _snapshot(**overrides):
    change = _change(
        path="a.txt",
        status="added",
        current_size=1,
        current_digest="sha256:" + "b" * 64,
    )
    values = {
        "schema_version": "v1",
        "subject_digest": "sha256:" + "c" * 64,
        "repository": "acme/service",
        "base_revision": "a" * 40,
        "head_revision": "b" * 40,
        "scope": "base_to_worktree",
        "worktree_dirty": False,
        "changes": [change],
        "changed_files_total": 1,
        "diff_artifact_digest": "sha256:" + "d" * 64,
        "diff_bytes": 0,
        "diff_truncated": False,
        "files_truncated": False,
        "ignored_files_lower_bound": 0,
        "ignored_scan_truncated": False,
        "large_file_paths": [],
        "submodule_paths": [],
        "omissions": [],
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return GitSnapshot(**values)


def _evidence(snapshot, status="success", **overrides):
    values = {
        "schema_version": "v1",
        "evidence_id": "ev_git_" + "e" * 32,
        "subject_digest": snapshot.subject_digest,
        "kind": "git_snapshot",
        "producer": "collector.git",
        "artifact_digest": snapshot.diff_artifact_digest,
        "source_ref": (
            f"git_snapshot:{snapshot.repository}:{snapshot.base_revision}:"
            f"{snapshot.head_revision}:{snapshot.scope}"
        ),
        "status": status,
        "trust_level": "deterministic",
        "collected_at": snapshot.collected_at,
    }
    values.update(overrides)
    return Evidence(**values)


def _result(**overrides):
    snapshot = _snapshot()
    values = {
        "schema_version": "v1",
        "snapshot": snapshot,
        "evidence": _evidence(snapshot),
    }
    values.update(overrides)
    return GitSnapshotResult(**values)


PRIOR_PUBLIC_NAMES = [
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
]

NEW_PUBLIC_NAMES = {
    "GitChange",
    "GitSnapshot",
    "GitSnapshotResult",
    "GitSnapshotCollector",
    "GitSnapshotError",
    "GitRepositoryError",
    "GitCommandError",
    "GitWorktreeChangedError",
}


def test_package_exports_preserve_all_p1_names_and_add_snapshot_api():
    assert set(PRIOR_PUBLIC_NAMES) | NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.GitChange is snapshot_module.GitChange
    assert assurance.GitSnapshot is snapshot_module.GitSnapshot
    assert assurance.GitSnapshotResult is snapshot_module.GitSnapshotResult
    assert assurance.GitSnapshotCollector is snapshot_module.GitSnapshotCollector
    assert assurance.GitSnapshotError is snapshot_module.GitSnapshotError
    assert assurance.GitRepositoryError is snapshot_module.GitRepositoryError
    assert assurance.GitCommandError is snapshot_module.GitCommandError
    assert assurance.GitWorktreeChangedError is snapshot_module.GitWorktreeChangedError


def test_exception_hierarchy_is_simple():
    assert issubclass(GitRepositoryError, GitSnapshotError)
    assert issubclass(GitCommandError, GitSnapshotError)
    assert issubclass(GitWorktreeChangedError, GitSnapshotError)


def test_git_change_field_order_frozen_and_extra_forbid():
    assert list(GitChange.model_fields) == [
        "schema_version",
        "path",
        "old_path",
        "status",
        "current_size",
        "current_digest",
        "binary",
        "large_file",
        "submodule",
    ]
    assert GitChange.model_config["frozen"] is True
    assert GitChange.model_config["extra"] == "forbid"
    change = _change()
    with pytest.raises(ValidationError):
        change.path = "mutated.txt"
    with pytest.raises(ValidationError):
        GitChange.model_validate({**change.model_dump(), "unexpected": 1})


def test_git_change_statuses_schema_version_and_round_trip():
    assert _change().schema_version == "v1"
    with pytest.raises(ValidationError):
        _change(schema_version="v2")
    for status in (
        "added",
        "modified",
        "deleted",
        "renamed",
        "copied",
        "type_changed",
        "unmerged",
        "untracked",
    ):
        kwargs = {}
        if status in ("renamed", "copied"):
            kwargs["old_path"] = "old.txt"
        if status == "deleted":
            kwargs.update(current_size=None, current_digest=None)
        change = _change(path=f"{status}.txt", status=status, **kwargs)
        restored = GitChange.model_validate(change.model_dump())
        assert restored == change


def test_git_change_path_and_old_path_are_canonical():
    for bad in ("", "/abs", "a/../b", "./a", "a//b", "a/./b", "a\\b"):
        with pytest.raises(ValidationError):
            _change(path=bad)
    for status in ("renamed", "copied"):
        with pytest.raises(ValidationError):
            _change(path="new.txt", status=status)
        with pytest.raises(ValidationError):
            _change(path="new.txt", status=status, old_path="./old.txt")
    for status in ("added", "modified", "deleted", "untracked"):
        with pytest.raises(ValidationError):
            _change(status=status, old_path="old.txt")
    assert _change(
        path="new.txt", status="renamed", old_path="old.txt"
    ).old_path == "old.txt"


def test_git_change_size_digest_and_flag_rules():
    with pytest.raises(ValidationError):
        _change(status="deleted", current_size=0, current_digest=None)
    with pytest.raises(ValidationError):
        _change(status="deleted", current_size=None, current_digest="sha256:" + "a" * 64)
    with pytest.raises(ValidationError):
        _change(status="deleted", binary=True)
    large = _change(
        path="big.bin", current_size=10, current_digest=None, large_file=True
    )
    assert large.current_digest is None
    _change(
        path="big.bin",
        current_size=10,
        current_digest="sha256:" + "a" * 64,
        large_file=True,
    )
    submodule = _change(
        path="sub",
        status="modified",
        current_size=None,
        current_digest=None,
        submodule=True,
    )
    assert submodule.submodule is True
    with pytest.raises(ValidationError):
        _change(
            path="sub",
            status="modified",
            current_size=None,
            current_digest="sha256:" + "a" * 64,
            submodule=True,
        )
    with pytest.raises(ValidationError):
        _change(current_digest=None)
    with pytest.raises(ValidationError):
        _change(current_size=None)
    for bad in (True, 1.0, "1"):
        with pytest.raises(ValidationError):
            _change(current_size=bad)
    with pytest.raises(ValidationError):
        _change(current_digest="sha256:" + "A" * 64)
    with pytest.raises(ValidationError):
        _change(current_digest="a" * 64)
    for bad in (1, "true"):
        with pytest.raises(ValidationError):
            _change(binary=bad)
    for bad in (1, "false"):
        with pytest.raises(ValidationError):
            _change(large_file=bad)
    for bad in (0, "yes"):
        with pytest.raises(ValidationError):
            _change(submodule=bad)


def test_git_snapshot_field_order_frozen_and_basic_contract():
    assert list(GitSnapshot.model_fields) == [
        "schema_version",
        "subject_digest",
        "repository",
        "base_revision",
        "head_revision",
        "scope",
        "worktree_dirty",
        "changes",
        "changed_files_total",
        "diff_artifact_digest",
        "diff_bytes",
        "diff_truncated",
        "files_truncated",
        "ignored_files_lower_bound",
        "ignored_scan_truncated",
        "large_file_paths",
        "submodule_paths",
        "omissions",
        "complete",
        "collected_at",
    ]
    assert GitSnapshot.model_config["frozen"] is True
    assert GitSnapshot.model_config["extra"] == "forbid"
    snapshot = _snapshot()
    restored = GitSnapshot.model_validate(snapshot.model_dump())
    assert restored == snapshot
    with pytest.raises(ValidationError):
        _snapshot(schema_version="v2")
    with pytest.raises(ValidationError):
        _snapshot(repository="acme/service/")
    with pytest.raises(ValidationError):
        _snapshot(repository=" acme/service")
    for bad in (
        "A" * 40,
        "a" * 39,
        "a" * 41,
        "g" * 40,
        "a" * 40 + " ",
    ):
        with pytest.raises(ValidationError):
            _snapshot(base_revision=bad)
    assert _snapshot(head_revision="b" * 64).head_revision == "b" * 64
    with pytest.raises(ValidationError):
        _snapshot(scope="base_to_head")
    with pytest.raises(ValidationError):
        _snapshot(worktree_dirty=1)
    with pytest.raises(ValidationError):
        _snapshot(collected_at=datetime(2026, 8, 25, 6, 0))
    with pytest.raises(ValidationError):
        _snapshot(subject_digest="sha256:" + "A" * 64)
    with pytest.raises(ValidationError):
        _snapshot(diff_artifact_digest="md5:" + "a" * 64)
    with pytest.raises(ValidationError):
        _snapshot(changed_files_total=True)
    with pytest.raises(ValidationError):
        _snapshot(diff_bytes=-1)


def test_git_snapshot_cross_field_invariants():
    second = _change(
        path="b.txt",
        status="modified",
        current_size=1,
        current_digest="sha256:" + "a" * 64,
    )
    with pytest.raises(ValidationError):
        _snapshot(changes=[second, _change()])
    duplicate = _change(path="a.txt", status="untracked")
    with pytest.raises(ValidationError):
        _snapshot(changes=[_change(), duplicate])
    with pytest.raises(ValidationError):
        _snapshot(changed_files_total=0)

    large = _change(
        path="big.bin",
        status="untracked",
        current_size=10,
        current_digest=None,
        large_file=True,
    )
    with pytest.raises(ValidationError):
        _snapshot(changes=[large])
    incomplete = _snapshot(
        changes=[large],
        large_file_paths=["big.bin"],
        omissions=["large_file"],
        complete=False,
    )
    assert incomplete.complete is False
    with pytest.raises(ValidationError):
        _snapshot(
            changes=[large],
            large_file_paths=["big.bin", "extra.bin"],
            omissions=["large_file"],
            complete=False,
        )
    with pytest.raises(ValidationError):
        _snapshot(
            changes=[large],
            large_file_paths=["big.bin"],
            omissions=["large_file"],
            complete=True,
        )
    with pytest.raises(ValidationError):
        _snapshot(
            changes=[large],
            large_file_paths=["big.bin"],
            omissions=["diff_truncated", "large_file"],
            complete=False,
        )
    with pytest.raises(ValidationError):
        _snapshot(
            changes=[large],
            large_file_paths=["big.bin"],
            omissions=["large_file", "large_file"],
            complete=False,
        )
    with pytest.raises(ValidationError):
        _snapshot(omissions=["unknown"])
    with pytest.raises(ValidationError):
        _snapshot(complete=False)
    with pytest.raises(ValidationError):
        _snapshot(diff_truncated=True, complete=True)
    with pytest.raises(ValidationError):
        _snapshot(diff_truncated=True, omissions=["diff_truncated"], complete=True)
    truncated = _snapshot(
        diff_truncated=True, omissions=["diff_truncated"], complete=False
    )
    assert truncated.complete is False

    submodule = _change(
        path="sub",
        status="modified",
        current_size=None,
        current_digest=None,
        submodule=True,
    )
    sub_snapshot = _snapshot(
        changes=[submodule],
        submodule_paths=["sub"],
        omissions=["submodule"],
        complete=False,
    )
    assert sub_snapshot.submodule_paths == ("sub",)
    with pytest.raises(ValidationError):
        _snapshot(
            changes=[submodule],
            submodule_paths=["sub"],
            omissions=["submodule"],
            complete=True,
        )

    both = _snapshot(
        changes=[large, submodule],
        large_file_paths=["big.bin"],
        submodule_paths=["sub"],
        omissions=["large_file", "submodule"],
        changed_files_total=2,
        complete=False,
    )
    assert both.omissions == ("large_file", "submodule")


def test_git_snapshot_result_cross_field_invariants():
    assert list(GitSnapshotResult.model_fields) == [
        "schema_version",
        "snapshot",
        "evidence",
    ]
    assert GitSnapshotResult.model_config["frozen"] is True
    assert GitSnapshotResult.model_config["extra"] == "forbid"
    result = _result()
    assert GitSnapshotResult.model_validate(result.model_dump()) == result
    with pytest.raises(ValidationError):
        _result(snapshot=_snapshot(subject_digest="sha256:" + "f" * 64))
    with pytest.raises(ValidationError):
        _result(
            evidence=_evidence(
                _snapshot(), artifact_digest="sha256:" + "f" * 64
            )
        )
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(_snapshot(), kind="test-run"))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(_snapshot(), producer="other"))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(_snapshot(), trust_level="observed"))
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(_snapshot(), status="truncated"))

    incomplete = _snapshot(
        diff_truncated=True, omissions=["diff_truncated"], complete=False
    )
    ok = GitSnapshotResult(
        schema_version="v1",
        snapshot=incomplete,
        evidence=_evidence(incomplete, status="truncated"),
    )
    assert ok.evidence.status == "truncated"
    with pytest.raises(ValidationError):
        GitSnapshotResult(
            schema_version="v1",
            snapshot=incomplete,
            evidence=_evidence(incomplete, status="success"),
        )


def test_collector_constructor_strict_positive_finite_validation():
    collector = GitSnapshotCollector()
    assert (
        collector.max_diff_bytes,
        collector.max_files,
        collector.max_file_bytes,
        collector.command_timeout_seconds,
    ) == (262144, 500, 5_000_000, 10.0)
    bad_values = {
        "max_diff_bytes": [True, "10", 0, -1],
        "max_files": [True, "10", 0, -1, 10.0],
        "max_file_bytes": [True, "10", 0, -1],
        "command_timeout_seconds": [
            True,
            10,
            "10",
            0.0,
            -1.0,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    }
    for name, values in bad_values.items():
        for bad in values:
            with pytest.raises((TypeError, ValueError)):
                GitSnapshotCollector(**{name: bad})


def test_clean_repo_base_head_success_and_exact_bindings(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    assert snap.changes == ()
    assert snap.worktree_dirty is False
    assert snap.changed_files_total == 0
    assert snap.complete is True
    assert snap.diff_bytes == 0
    assert snap.diff_artifact_digest == _sha256(b"")
    assert store.get_bytes(snap.diff_artifact_digest) == b""
    assert snap.collected_at == FIXED_TIME

    evidence = result.evidence
    assert evidence.status == "success"
    assert evidence.kind == "git_snapshot"
    assert evidence.producer == "collector.git"
    assert evidence.trust_level == "deterministic"
    assert evidence.collected_at == FIXED_TIME
    assert evidence.source_ref == (
        f"git_snapshot:{IDENTITY}:{snap.base_revision}:"
        f"{snap.head_revision}:base_to_worktree"
    )
    assert str(repo) not in evidence.source_ref

    manifest_digest = _expected_manifest_digest(snap, collector)
    expected_subject = compute_subject_digest(
        SubjectDigestInput(
            repository=IDENTITY,
            base_revision=snap.base_revision,
            head_revision=snap.head_revision,
            normalized_diff_digest=manifest_digest,
            task_digest=TASK,
            policy_version=POLICY,
            rubric_version=RUBRIC,
        )
    )
    assert snap.subject_digest == expected_subject
    assert snap.subject_digest == evidence.subject_digest
    assert snap.diff_artifact_digest == evidence.artifact_digest
    expected_evidence_id = "ev_git_" + hashlib.sha256(
        (snap.subject_digest + snap.diff_artifact_digest).encode("ascii")
    ).hexdigest()[:32]
    assert evidence.evidence_id == expected_evidence_id


def test_tracked_modified_and_untracked_patches(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "b.txt").write_bytes(b"hello\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    assert [change.path for change in snap.changes] == ["a.txt", "b.txt"]
    assert [change.status for change in snap.changes] == ["modified", "untracked"]
    assert snap.changes[0].current_size == 4
    assert snap.changes[0].current_digest == _sha256(b"two\n")
    assert snap.changes[0].binary is False
    assert snap.changes[1].current_size == 6
    assert snap.changes[1].current_digest == _sha256(b"hello\n")
    assert snap.worktree_dirty is True
    assert snap.complete is True
    assert result.evidence.status == "success"
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert b"a.txt" in artifact
    assert b"b.txt" in artifact
    assert b"hello" in artifact


def test_staged_add_delete_and_rename(tmp_path):
    repo = _init_repo(
        tmp_path,
        {"a.txt": b"a\n", "b.txt": b"b\n", "c.txt": b"c\n"},
    )
    _git(repo, "mv", "c.txt", "d.txt")
    (repo / "new.txt").write_bytes(b"new\n")
    _git(repo, "add", "new.txt")
    _git(repo, "rm", "-q", "a.txt")
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    by_path = {change.path: change for change in snap.changes}
    assert [change.path for change in snap.changes] == ["a.txt", "d.txt", "new.txt"]
    assert by_path["a.txt"].status == "deleted"
    assert by_path["a.txt"].current_size is None
    assert by_path["a.txt"].current_digest is None
    assert by_path["d.txt"].status == "renamed"
    assert by_path["d.txt"].old_path == "c.txt"
    assert by_path["d.txt"].current_size == 2
    assert by_path["d.txt"].current_digest == _sha256(b"c\n")
    assert by_path["new.txt"].status == "added"
    assert by_path["new.txt"].current_size == 4
    assert by_path["new.txt"].current_digest == _sha256(b"new\n")
    assert snap.worktree_dirty is True
    assert snap.complete is True


def test_real_merge_conflict_is_unmerged_incomplete_and_truncated(tmp_path):
    repo = _init_repo(tmp_path, {"conflict.txt": b"base\n"}, message="base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "conflict.txt").write_bytes(b"feature\n")
    _git(repo, "commit", "-qam", "feature change")
    _git(repo, "checkout", "-q", "-")
    (repo / "conflict.txt").write_bytes(b"main\n")
    _git(repo, "commit", "-qam", "main change")
    merged = subprocess.run(
        ["git", "merge", "feature"], cwd=repo, capture_output=True
    )
    assert merged.returncode != 0
    assert b"U\x00conflict.txt" in _git(repo, "diff", "--name-status", "-z")

    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    by_path = {change.path: change for change in snap.changes}
    assert by_path["conflict.txt"].status == "unmerged"
    assert snap.omissions == ("unmerged",)
    assert snap.complete is False
    assert result.evidence.status == "truncated"


def test_unmerged_path_without_tracked_destination_fails_closed(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original_git = snapshot_module.GitSnapshotCollector._git

    def fake_git(self, repository_path, *args):
        if args == ("diff", "--name-status", "-z"):
            return b"U\x00ghost.txt\x00"
        return original_git(self, repository_path, *args)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_git", fake_git
    )
    with pytest.raises(GitCommandError, match="ghost.txt"):
        _collect(repo, store, collector=collector)


def test_base_previous_commit_clean_worktree_full_shas(tmp_path):
    repo = _init_repo(tmp_path, {"base.txt": b"one\n"}, message="first")
    (repo / "base.txt").write_bytes(b"two\n")
    (repo / "new.txt").write_bytes(b"n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store, base_ref="HEAD~1")
    snap = result.snapshot
    assert snap.worktree_dirty is False
    assert {change.path: change.status for change in snap.changes} == {
        "base.txt": "modified",
        "new.txt": "added",
    }
    assert len(snap.base_revision) == 40
    assert len(snap.head_revision) == 40
    assert snap.base_revision == _git(repo, "rev-parse", "HEAD~1").strip().decode()
    assert snap.head_revision == _git(repo, "rev-parse", "HEAD").strip().decode()
    assert snap.base_revision != snap.head_revision
    assert snap.complete is True


def test_fixed_time_repeated_collection_equal_and_artifact_idempotent(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    first = _collect(repo, store)
    target = _artifact_path(store, first.snapshot.diff_artifact_digest)
    before = target.stat().st_mtime_ns
    second = _collect(repo, store)
    after = target.stat().st_mtime_ns
    assert second == first
    assert second.snapshot.diff_artifact_digest == first.snapshot.diff_artifact_digest
    assert before == after
    assert target.read_bytes() == store.get_bytes(first.snapshot.diff_artifact_digest)


def test_subject_changes_for_inputs(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"}, message="first")
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "new.txt").write_bytes(b"n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    (repo / "a.txt").write_bytes(b"three\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    base = _collect(repo, store)
    variants = [
        {"task_digest": "sha256:" + "3" * 64},
        {"policy_version": "policy-v2"},
        {"rubric_version": "rubric-v2"},
        {"attachment_digests": (ATTACHMENT,)},
        {"repository_identity": "acme/other"},
        {"base_ref": "HEAD~1"},
    ]
    for overrides in variants:
        variant = _collect(repo, store, **overrides)
        assert variant.snapshot.subject_digest != base.snapshot.subject_digest
        assert variant.evidence.evidence_id != base.evidence.evidence_id
    (repo / "a.txt").write_bytes(b"two\n")
    diff_variant = _collect(repo, store)
    assert diff_variant.snapshot.subject_digest != base.snapshot.subject_digest
    assert diff_variant.snapshot.diff_artifact_digest != base.snapshot.diff_artifact_digest


@pytest.mark.parametrize(
    "base_ref",
    ["", "   ", "HEAD HEAD", "HEAD\x00x", "-n", "--output"],
)
def test_invalid_base_ref_rejected(tmp_path, base_ref):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    store = ArtifactStore(tmp_path / "artifact-store")
    with pytest.raises(ValueError):
        _collect(repo, store, base_ref=base_ref)


def test_repository_failures_fail_closed(tmp_path):
    store = ArtifactStore(tmp_path / "artifact-store")
    not_repo = tmp_path / "not-repo"
    not_repo.mkdir()
    with pytest.raises(GitRepositoryError):
        _collect(not_repo, store)

    repo = _init_repo(tmp_path / "nested", {"a.txt": b"one\n"})
    subdir = repo / "sub"
    subdir.mkdir()
    with pytest.raises(GitRepositoryError):
        _collect(subdir, store)

    bare = tmp_path / "bare.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    with pytest.raises(GitRepositoryError):
        _collect(bare, store)

    with pytest.raises(GitRepositoryError):
        _collect(repo, store, base_ref="no-such-ref")

    plain_file = tmp_path / "plain.txt"
    plain_file.write_text("x")
    with pytest.raises(GitRepositoryError):
        _collect(plain_file, store)

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link-to-outside"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(GitRepositoryError):
        _collect(link, store)


def test_non_path_and_non_store_inputs_rejected(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    store = ArtifactStore(tmp_path / "artifact-store")
    with pytest.raises(TypeError):
        _collect(str(repo), store)
    with pytest.raises(TypeError):
        _collect(repo, store, repository_identity=123)
    with pytest.raises(TypeError):
        _collect(repo, store, base_ref=123)
    with pytest.raises(TypeError):
        _collect(repo, store, task_digest=123)
    with pytest.raises(TypeError):
        _collect(repo, store, artifact_store=repo)
    with pytest.raises(TypeError):
        _collect(repo, store, attachment_digests=[ATTACHMENT])
    with pytest.raises(ValueError):
        _collect(repo, store, task_digest="sha256:" + "A" * 64)
    with pytest.raises(ValueError):
        _collect(repo, store, attachment_digests=(TASK, TASK))
    with pytest.raises(ValueError):
        _collect(repo, store, policy_version="   ")
    with pytest.raises(ValueError):
        _collect(repo, store, rubric_version="")
    with pytest.raises(ValueError):
        _collect(repo, store, collected_at=datetime(2026, 8, 25, 6, 0))


def test_diff_external_and_fsmonitor_sentinels_never_execute(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    sentinel = tmp_path / "sentinel-ran"
    script = tmp_path / "sentinel.sh"
    script.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    script.chmod(0o755)
    _git(repo, "config", "diff.external", str(script))
    _git(repo, "config", "core.fsmonitor", str(script))
    hook = repo / ".git" / "hooks" / "fsmonitor-watchman"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    store = ArtifactStore(tmp_path / "artifact-store")
    _collect(repo, store)
    assert not sentinel.exists()


def test_read_only_status_identical_and_no_repo_writes(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "untracked.txt").write_bytes(b"u\n")
    before = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    store = ArtifactStore(tmp_path / "artifact-store")
    _collect(repo, store)
    after = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    assert before == after
    assert not list(repo.rglob("sha256"))
    assert any(store.root.rglob("sha256"))


def test_subprocess_timeout_failure_and_huge_output_map_to_git_command_error(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    store = ArtifactStore(tmp_path / "artifact-store")
    monkeypatch.setenv("CODEMESH_SNAPSHOT_SECRET", "hunter2-secret")

    def timeout_popen(*args, **kwargs):
        return _FakeProcess([b""], [b""], fail_wait=True)

    monkeypatch.setattr(snapshot_module.subprocess, "Popen", timeout_popen)
    with pytest.raises(GitCommandError) as excinfo:
        _collect(repo, store)
    assert "hunter2-secret" not in str(excinfo.value)
    assert "GIT_CONFIG_GLOBAL" not in str(excinfo.value)

    def fail_popen(*args, **kwargs):
        return _FakeProcess([b""], [b"boom"], returncode=2)

    monkeypatch.setattr(snapshot_module.subprocess, "Popen", fail_popen)
    with pytest.raises(GitCommandError, match="boom") as excinfo:
        _collect(repo, store)
    assert "hunter2-secret" not in str(excinfo.value)

    monkeypatch.setattr(snapshot_module, "_MAX_METADATA_OUTPUT_BYTES", 1024)

    def huge_popen(*args, **kwargs):
        return _FakeProcess([b"y" * 2048], [b""], returncode=0)

    monkeypatch.setattr(snapshot_module.subprocess, "Popen", huge_popen)
    with pytest.raises(GitCommandError) as excinfo:
        _collect(repo, store)
    assert "hunter2-secret" not in str(excinfo.value)


def test_large_file_makes_incomplete_truncated_evidence(tmp_path):
    repo = _init_repo(tmp_path, {"small.txt": b"ok\n"})
    (repo / "big.bin").write_bytes(b"x" * 100)
    collector = GitSnapshotCollector(max_file_bytes=8)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    big = next(change for change in snap.changes if change.path == "big.bin")
    assert big.large_file is True
    assert big.current_size == 100
    assert big.current_digest is None
    assert snap.large_file_paths == ("big.bin",)
    assert snap.omissions == ("large_file",)
    assert snap.complete is False
    assert result.evidence.status == "truncated"


def test_files_and_diff_truncation_flags(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"line one\nline two\nline three\n")
    (repo / "b.txt").write_bytes(b"untracked content\n")
    files_collector = GitSnapshotCollector(max_files=1)
    store = ArtifactStore(tmp_path / "artifact-store")
    files_result = _collect(repo, store, collector=files_collector)
    snap = files_result.snapshot
    assert snap.files_truncated is True
    assert snap.changed_files_total == 2
    assert len(snap.changes) == 1
    assert snap.omissions == ("files_truncated",)
    assert snap.complete is False
    assert files_result.evidence.status == "truncated"

    diff_collector = GitSnapshotCollector(max_diff_bytes=128)
    diff_result = _collect(repo, store, collector=diff_collector)
    diff_snap = diff_result.snapshot
    assert diff_snap.diff_truncated is True
    assert diff_snap.omissions == ("diff_truncated",)
    artifact = store.get_bytes(diff_snap.diff_artifact_digest)
    assert artifact.endswith(b"TRUNCATED ===\n")
    assert diff_result.evidence.status == "truncated"


@pytest.mark.parametrize("max_diff_bytes", [1, 8, 32, 46, 64, 128])
def test_truncated_artifact_respects_max_diff_bytes_and_marker(
    tmp_path, max_diff_bytes
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"line one\nline two\nline three\n")
    (repo / "b.txt").write_bytes(b"untracked content\n")
    collector = GitSnapshotCollector(max_diff_bytes=max_diff_bytes)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert snap.diff_truncated is True
    assert snap.diff_bytes == len(artifact)
    assert len(artifact) <= max_diff_bytes
    marker = snapshot_module._DIFF_TRUNCATION_MARKER
    if max_diff_bytes < len(marker):
        assert artifact == marker[:max_diff_bytes]
    else:
        assert artifact.endswith(marker)
    assert result.evidence.status == "truncated"


def test_concurrent_change_omission_round_trip_and_complete_rule():
    snap = _snapshot(omissions=["concurrent_change"], complete=False)
    assert snap.omissions == ("concurrent_change",)
    assert snap.complete is False
    restored = GitSnapshot.model_validate(snap.model_dump())
    assert restored == snap
    with pytest.raises(ValidationError):
        _snapshot(omissions=["concurrent_change"], complete=True)
    with pytest.raises(ValidationError):
        _snapshot(
            diff_truncated=True,
            omissions=["concurrent_change"],
            complete=False,
        )


def test_gitlink_added_in_head_is_submodule_marker_and_truncated(tmp_path):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"}, message="base")
    module = repo / "vendor" / "module"
    module.mkdir(parents=True)
    _git(module, "init", "-q")
    _git(module, "config", "user.email", "module@example.com")
    _git(module, "config", "user.name", "Module")
    (module / "m.txt").write_bytes(b"module content\n")
    _git(module, "add", "-A")
    _git(module, "commit", "-qm", "module")
    module_sha = _git(module, "rev-parse", "HEAD").strip().decode("ascii")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{module_sha},vendor/module",
    )
    _git(repo, "commit", "-qm", "add gitlink")
    store = ArtifactStore(tmp_path / "artifact-store")
    before = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    result = _collect(repo, store, base_ref="HEAD~1")
    after = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    assert before == after
    snap = result.snapshot
    change = next(
        change for change in snap.changes if change.path == "vendor/module"
    )
    assert len(snap.changes) == 1
    assert change.status == "added"
    assert change.submodule is True
    assert change.current_size is None
    assert change.current_digest is None
    assert snap.worktree_dirty is False
    assert snap.submodule_paths == ("vendor/module",)
    assert snap.omissions == ("submodule",)
    assert snap.complete is False
    assert result.evidence.status == "truncated"


def test_product_snapshot_has_no_subprocess_run_reference():
    source = inspect.getsource(snapshot_module)
    assert "subprocess.run" not in source


def test_bounded_pipe_reader_retains_at_most_limit_and_discards(tmp_path):
    stream = _FakeStream([b"a" * 65536] * 8)
    caps = []
    reader = snapshot_module._BoundedPipeReader(
        stream, limit=100000, on_cap=lambda: caps.append(True)
    )
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert reader.truncated is True
    assert len(reader.data) == 100000
    assert caps == [True]
    assert all(size <= snapshot_module._READ_CHUNK_SIZE for size in stream.read_sizes)


def test_bounded_pipe_reader_keeps_all_bytes_below_limit(tmp_path):
    stream = _FakeStream([b"a" * 65536, b"b" * 1000])
    reader = snapshot_module._BoundedPipeReader(
        stream, limit=1_000_000, on_cap=None
    )
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert reader.truncated is False
    assert reader.data == b"a" * 65536 + b"b" * 1000


def test_git_runner_drains_large_stdout_and_stderr_concurrently(
    tmp_path, monkeypatch
):
    real_popen = subprocess.Popen
    script = (
        "import os\n"
        "chunk = b'x' * 4096\n"
        "err = b'y' * 4096\n"
        "for _ in range(2000):\n"
        "    os.write(1, chunk)\n"
        "    os.write(2, err)\n"
    )

    def fake_popen(command, **kwargs):
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(snapshot_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(snapshot_module, "_MAX_STDERR_BYTES", 9 * 1024 * 1024)
    collector = GitSnapshotCollector(command_timeout_seconds=30.0)
    result = collector._run_git(
        tmp_path,
        ("diff", "--internal-test"),
        stdout_limit=9 * 1024 * 1024,
        stdout_cap_truncates=True,
    )
    assert result.returncode == 0
    assert result.stdout == b"x" * (4096 * 2000)
    assert result.stderr == b"y" * (4096 * 2000)
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_run_git_metadata_stdout_cap_raises_without_partial_result(
    tmp_path, monkeypatch
):
    fake = _FakeProcess([b"z" * 65536] * 8, [b""])
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError, match="safety bound") as excinfo:
        collector._run_git(
            tmp_path,
            ("status", "--porcelain=v1", "-z"),
            stdout_limit=100000,
        )
    assert "zzz" not in str(excinfo.value)
    assert fake.terminate_calls >= 1
    assert fake.kill_calls >= 1
    assert fake.wait_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


def test_git_metadata_cap_raises_with_no_partial_result(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    monkeypatch.setattr(snapshot_module, "_MAX_METADATA_OUTPUT_BYTES", 128)
    fake = _FakeProcess([b"m" * 65536] * 4, [b""])
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError, match="safety bound"):
        collector._git(repo, "status", "--porcelain=v1", "-z")


def test_run_git_diff_stdout_cap_returns_bounded_truncated_result(
    tmp_path, monkeypatch
):
    fake = _FakeProcess([b"z" * 65536] * 8, [b""])
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector(max_diff_bytes=100000)
    result = collector._run_git(
        tmp_path,
        ("diff", "--no-color", "HEAD"),
        stdout_limit=collector.max_diff_bytes + 1,
        stdout_cap_truncates=True,
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) == 100001
    assert fake.terminate_calls >= 1
    assert fake.kill_calls >= 1
    assert fake.wait_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


def test_run_git_timeout_terminates_kills_reaps_and_closes(
    tmp_path, monkeypatch
):
    fake = _FakeProcess([b""], [b""], fail_wait=True)
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError, match="timed out"):
        collector._run_git(tmp_path, ("status",), stdout_limit=100000)
    assert fake.terminate_calls >= 1
    assert fake.kill_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


def test_run_git_nonzero_exit_uses_bounded_stderr_only(tmp_path, monkeypatch):
    fake = _FakeProcess([b"out"], [b"e" * 3000], returncode=7)
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError) as excinfo:
        collector._run_git(tmp_path, ("status",), stdout_limit=100000)
    assert "exit 7" in str(excinfo.value)
    assert excinfo.value.git_exit_code == 7
    assert excinfo.value.git_stderr == "e" * 3000
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


def test_run_git_stderr_cap_raises_bounded_error(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_module, "_MAX_STDERR_BYTES", 64)
    fake = _FakeProcess([b""], [b"S" * 10000])
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError, match="stderr") as excinfo:
        collector._run_git(tmp_path, ("status",), stdout_limit=100000)
    assert "S" * 10000 not in str(excinfo.value)
    assert fake.terminate_calls >= 1
    assert fake.kill_calls >= 1
    assert fake.wait_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_run_git_pipe_reader_error_fails_closed(
    tmp_path, monkeypatch, error_type
):
    secret = "reader-error-secret-9d21"
    fake = _FakeProcess(_ExplodingStream(error_type, secret), [b""])
    monkeypatch.setattr(
        snapshot_module.subprocess, "Popen", lambda *args, **kwargs: fake
    )
    collector = GitSnapshotCollector()
    with pytest.raises(GitCommandError, match="pipe reader") as excinfo:
        collector._run_git(tmp_path, ("status",), stdout_limit=100000)
    assert secret not in str(excinfo.value)
    assert fake.kill_calls >= 1
    assert fake.wait_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True


def test_many_untracked_patches_bounded_artifact_and_marker(tmp_path):
    repo = _init_repo(tmp_path, {"seed.txt": b"seed\n"})
    for index in range(8):
        (repo / f"u{index}.txt").write_bytes(
            (f"untracked content {index}\n").encode() * 20
        )
    collector = GitSnapshotCollector(max_diff_bytes=700)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert snap.diff_truncated is True
    assert snap.diff_bytes == len(artifact)
    assert len(artifact) <= 700
    assert artifact.endswith(snapshot_module._DIFF_TRUNCATION_MARKER)
    assert b"untracked content 7" not in artifact
    again = _collect(repo, store, collector=collector)
    assert again.snapshot.diff_artifact_digest == snap.diff_artifact_digest
    assert store.get_bytes(again.snapshot.diff_artifact_digest) == artifact


def test_tracked_and_multiple_untracked_patches_bounded_deterministic(tmp_path):
    repo = _init_repo(tmp_path, {"tracked.txt": b"seed\n"})
    (repo / "tracked.txt").write_bytes(b"tracked change\n" * 10)
    for index in range(5):
        (repo / f"u{index}.txt").write_bytes(
            f"untracked content {index}\n".encode() * 10
        )
    collector = GitSnapshotCollector(max_diff_bytes=700)
    store = ArtifactStore(tmp_path / "artifact-store")
    first = _collect(repo, store, collector=collector)
    snap = first.snapshot
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert snap.diff_truncated is True
    assert snap.diff_bytes == len(artifact)
    assert len(artifact) <= 700
    assert artifact.endswith(snapshot_module._DIFF_TRUNCATION_MARKER)
    assert b"tracked change" in artifact
    second = _collect(repo, store, collector=collector)
    assert second.snapshot.diff_artifact_digest == snap.diff_artifact_digest
    assert store.get_bytes(second.snapshot.diff_artifact_digest) == artifact


TRACKED_OMITTED_SECRET = "unique-tracked-omitted-secret-6e17"


def test_more_tracked_changes_than_max_files_omits_full_diff_secret(
    tmp_path, monkeypatch
):
    repo = _init_repo(
        tmp_path,
        {f"t{index}.txt": b"base\n" for index in range(5)},
        message="base",
    )
    for index in range(5):
        content = (
            f"changed tracked {index}\n".encode() * 12
            if index != 4
            else TRACKED_OMITTED_SECRET.encode() + b"\n"
        )
        (repo / f"t{index}.txt").write_bytes(content)
    collector = GitSnapshotCollector(max_files=3)
    store = ArtifactStore(tmp_path / "artifact-store")
    hashed = []
    original = snapshot_module.GitSnapshotCollector._hash_worktree_file

    def counting_hash(self, full_path, need_content):
        hashed.append(str(full_path))
        return original(self, full_path, need_content)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_hash_worktree_file", counting_hash
    )
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    assert snap.files_truncated is True
    assert snap.changed_files_total == 5
    assert len(snap.changes) == 3
    assert [change.path for change in snap.changes] == [
        "t0.txt",
        "t1.txt",
        "t2.txt",
    ]
    hashed_paths = {Path(path) for path in hashed}
    assert hashed_paths == {repo / f"t{index}.txt" for index in range(3)}
    snapshot_json = json.dumps(
        snap.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )
    assert TRACKED_OMITTED_SECRET not in snapshot_json
    assert "t4.txt" not in snapshot_json
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert TRACKED_OMITTED_SECRET.encode() not in artifact
    assert b"t4.txt" not in artifact
    assert b"changed tracked 0" in artifact


def test_tracked_diff_paths_limited_stable_and_include_rename_old_path(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"a\n", "old.txt": b"old\n"})
    _git(repo, "mv", "old.txt", "new.txt")
    (repo / "a.txt").write_bytes(b"a2\n")
    diff_calls = []
    original = snapshot_module.GitSnapshotCollector._run_git

    def spy_run(self, repository_path, args, **kwargs):
        if args[0] == "diff" and len(args) > 1 and args[1] == "--no-color":
            diff_calls.append(args)
        return original(self, repository_path, args, **kwargs)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_run_git", spy_run
    )
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    assert result.snapshot.complete is True
    assert len(diff_calls) == 1
    diff_args = diff_calls[0]
    assert diff_args[6] == "--"
    assert diff_args[7:] == ("a.txt", "new.txt", "old.txt")


def test_tracked_diff_skipped_when_no_selected_tracked_paths(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"seed.txt": b"seed\n"})
    (repo / "u.txt").write_bytes(b"untracked\n")
    diff_calls = []
    original = snapshot_module.GitSnapshotCollector._run_git

    def spy_run(self, repository_path, args, **kwargs):
        if args[0] == "diff" and len(args) > 1 and args[1] == "--no-color":
            diff_calls.append(args)
        return original(self, repository_path, args, **kwargs)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_run_git", spy_run
    )
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    assert diff_calls == []
    assert result.snapshot.changes[0].path == "u.txt"


def test_more_than_max_files_sliced_before_hashing_exact_total(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"tracked.txt": b"one\n"})
    for index in range(7):
        (repo / f"u{index}.txt").write_bytes(f"content {index}\n".encode())
    collector = GitSnapshotCollector(max_files=3)
    store = ArtifactStore(tmp_path / "artifact-store")
    hashed = []
    original = snapshot_module.GitSnapshotCollector._hash_worktree_file

    def counting_hash(self, full_path, need_content):
        hashed.append((str(full_path), need_content))
        return original(self, full_path, need_content)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_hash_worktree_file", counting_hash
    )
    result = _collect(repo, store, collector=collector)
    snap = result.snapshot
    assert snap.files_truncated is True
    assert snap.changed_files_total == 7
    assert len(snap.changes) == 3
    hashed_paths = {Path(path) for path, _ in hashed}
    assert len(hashed_paths) == 3
    expected = sorted(repo / f"u{index}.txt" for index in range(7))
    assert hashed_paths == set(expected[:3])


IGNORED_SECRET = "unique-ignored-secret-91f3"
IGNORED_PATH = "ignored-secret-dir/ignored-secret-file.txt"


def test_ignored_scan_complete_count_and_no_secret_disclosure(tmp_path):
    repo = _init_repo(tmp_path, {"tracked.txt": b"one\n"})
    (repo / ".gitignore").write_text("ignored-secret-dir/\n*.secret\n")
    ignored_dir = repo / "ignored-secret-dir"
    ignored_dir.mkdir()
    (ignored_dir / "ignored-secret-file.txt").write_bytes(IGNORED_SECRET.encode())
    (repo / "cache.secret").write_bytes(IGNORED_SECRET.encode())
    (repo / "normal.txt").write_bytes(b"normal\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    assert snap.ignored_files_lower_bound == 2
    assert snap.ignored_scan_truncated is False
    assert snap.complete is True
    assert result.evidence.status == "success"
    snapshot_json = json.dumps(
        snap.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )
    assert IGNORED_SECRET not in snapshot_json
    assert IGNORED_PATH not in snapshot_json
    assert IGNORED_SECRET not in result.evidence.model_dump_json()
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert IGNORED_SECRET.encode() not in artifact
    assert IGNORED_PATH.encode() not in artifact


def test_ignored_only_changes_do_not_make_worktree_dirty(tmp_path):
    repo = _init_repo(
        tmp_path, {"a.txt": b"one\n", ".gitignore": b"*.log\n"}
    )
    (repo / "app.log").write_text("noise\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    assert snap.worktree_dirty is False
    assert snap.changes == ()
    assert snap.complete is True
    assert snap.ignored_files_lower_bound == 1
    assert snap.ignored_scan_truncated is False


def test_ignored_scan_cap_lower_bound_partial_record_omission(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"tracked.txt": b"one\n"})
    (repo / ".gitignore").write_text("a/\n")
    ignored_dir = repo / "a"
    ignored_dir.mkdir()
    for name in ("one", "secret-91f3", "three"):
        (ignored_dir / name).write_bytes(IGNORED_SECRET.encode())
    monkeypatch.setattr(snapshot_module, "_MAX_IGNORED_OUTPUT_BYTES", 12)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    assert snap.ignored_scan_truncated is True
    assert "ignored_scan_truncated" in snap.omissions
    assert snap.complete is False
    assert result.evidence.status == "truncated"
    assert snap.ignored_files_lower_bound == 1
    snapshot_json = json.dumps(
        snap.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )
    assert IGNORED_SECRET not in snapshot_json
    assert "secret-91f3" not in snapshot_json
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert IGNORED_SECRET.encode() not in artifact
    assert b"secret-91f3" not in artifact


def test_untracked_binary_artifact_marker_only_no_raw_bytes(tmp_path):
    repo = _init_repo(tmp_path, {"seed.txt": b"seed\n"})
    payload = b"\x00BIN\x01PAYLOAD\x00" * 50
    (repo / "blob.bin").write_bytes(payload)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    change = next(change for change in snap.changes if change.path == "blob.bin")
    assert change.binary is True
    assert change.current_digest == _sha256(payload)
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert b"\x00" not in artifact
    assert b"binary-file" in artifact
    assert artifact.count(b"binary-file") == 1
    assert b"path: blob.bin" in artifact
    assert f"size: {len(payload)}".encode() in artifact
    assert change.current_digest.encode() in artifact


def test_tracked_binary_artifact_has_git_representation_and_current_marker(
    tmp_path,
):
    repo = _init_repo(tmp_path, {"blob.bin": b"OLD\x00data\n"})
    payload = b"NEW\x00data\n" * 20
    (repo / "blob.bin").write_bytes(payload)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    snap = result.snapshot
    change = next(change for change in snap.changes if change.path == "blob.bin")
    assert change.binary is True
    assert change.current_digest == _sha256(payload)
    artifact = store.get_bytes(snap.diff_artifact_digest)
    assert b"Binary files" in artifact
    assert b"binary-file" in artifact
    assert artifact.count(b"binary-file") == 1
    assert b"path: blob.bin" in artifact
    assert f"size: {len(payload)}".encode() in artifact
    assert change.current_digest.encode() in artifact
    assert b"\x00" not in artifact


def test_tracked_and_untracked_binary_markers_each_once(tmp_path):
    repo = _init_repo(tmp_path, {"tracked.bin": b"OLD\x00data\n"})
    tracked_payload = b"NEW\x00data\n" * 20
    untracked_payload = b"\x00UNTRACKED\x00\n"
    (repo / "tracked.bin").write_bytes(tracked_payload)
    (repo / "untracked.bin").write_bytes(untracked_payload)
    store = ArtifactStore(tmp_path / "artifact-store")
    result = _collect(repo, store)
    artifact = store.get_bytes(result.snapshot.diff_artifact_digest)
    assert artifact.count(b"binary-file") == 2
    assert artifact.count(b"path: tracked.bin") == 1
    assert artifact.count(b"path: untracked.bin") == 1


def test_large_file_never_opened(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, {"seed.txt": b"seed\n"})
    big = repo / "big.bin"
    big.write_bytes(b"L" * 1000)
    collector = GitSnapshotCollector(max_file_bytes=64)
    store = ArtifactStore(tmp_path / "artifact-store")
    original_open = Path.open
    opened = []

    def guarded_open(self, *args, **kwargs):
        opened.append(str(self))
        if self == big:
            raise AssertionError("large file must never be opened")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = _collect(repo, store, collector=collector)
    assert str(big) not in opened
    change = next(change for change in result.snapshot.changes if change.path == "big.bin")
    assert change.large_file is True
    assert change.current_size == 1000
    assert change.current_digest is None


def test_file_fingerprint_change_during_collection_fails_closed(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "b.txt").write_bytes(b"untracked\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._hash_worktree_file
    mutated = False

    def mutate_after_first(self, full_path, need_content):
        nonlocal mutated
        result = original(self, full_path, need_content)
        if not mutated:
            mutated = True
            if full_path.name == "a.txt":
                full_path.write_bytes(b"three\n")
        return result

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_hash_worktree_file", mutate_after_first
    )
    with pytest.raises(GitWorktreeChangedError, match="a.txt"):
        _collect(repo, store, collector=collector)
    assert not (store.root / "sha256").exists()
    assert _git(repo, "rev-parse", "HEAD").strip() == _git(
        repo, "rev-parse", "HEAD"
    ).strip()


def test_head_change_during_collection_fails_closed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"}, message="base")
    (repo / "a.txt").write_bytes(b"two\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._git
    head_calls = {"count": 0}

    def mutate_head(self, repository_path, *args):
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            head_calls["count"] += 1
            if head_calls["count"] == 4:
                _git(repository_path, "commit", "-qam", "concurrent head change")
        return original(self, repository_path, *args)

    monkeypatch.setattr(snapshot_module.GitSnapshotCollector, "_git", mutate_head)
    with pytest.raises(GitWorktreeChangedError, match="HEAD"):
        _collect(repo, store, collector=collector)
    assert not (store.root / "sha256").exists()


def test_head_change_between_initial_resolutions_fails_closed(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"}, message="base")
    (repo / "a.txt").write_bytes(b"two\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._git
    head_calls = {"count": 0}

    def mutate_head(self, repository_path, *args):
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            head_calls["count"] += 1
            if head_calls["count"] == 3:
                _git(repository_path, "commit", "-qam", "between head resolves")
        return original(self, repository_path, *args)

    monkeypatch.setattr(snapshot_module.GitSnapshotCollector, "_git", mutate_head)
    with pytest.raises(GitWorktreeChangedError, match="HEAD"):
        _collect(repo, store, collector=collector)
    assert not (store.root / "sha256").exists()


def test_status_change_during_collection_fails_closed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / ".gitignore").write_text("ignored-secret-91f3.log\n")
    (repo / "ignored-secret-91f3.log").write_bytes(IGNORED_SECRET.encode())
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._git
    status_calls = {"count": 0}

    def mutate_status(self, repository_path, *args):
        if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            status_calls["count"] += 1
            if status_calls["count"] == 2:
                (repository_path / "concurrent.txt").write_bytes(b"x\n")
        return original(self, repository_path, *args)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_git", mutate_status
    )
    with pytest.raises(GitWorktreeChangedError, match="status") as excinfo:
        _collect(repo, store, collector=collector)
    assert IGNORED_SECRET not in str(excinfo.value)
    assert "ignored-secret-91f3.log" not in str(excinfo.value)
    assert not (store.root / "sha256").exists()


def test_untracked_content_mutation_during_final_status_check_fails_closed(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "u.txt").write_bytes(b"first\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._git
    status_calls = {"count": 0}

    def mutate_status(self, repository_path, *args):
        if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            status_calls["count"] += 1
            if status_calls["count"] == 2:
                (repository_path / "u.txt").write_bytes(b"second\n")
        return original(self, repository_path, *args)

    monkeypatch.setattr(
        snapshot_module.GitSnapshotCollector, "_git", mutate_status
    )
    with pytest.raises(GitWorktreeChangedError, match="u.txt"):
        _collect(repo, store, collector=collector)
    assert not (store.root / "sha256").exists()


def test_real_stage_mutation_during_collection_fails_closed(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path, {"a.txt": b"one\n"})
    (repo / "b.txt").write_bytes(b"untracked\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    collector = GitSnapshotCollector()
    original = snapshot_module.GitSnapshotCollector._git
    stage_calls = {"count": 0}

    def mutate_stage(self, repository_path, *args):
        if args == ("ls-files", "--stage", "-z"):
            stage_calls["count"] += 1
            if stage_calls["count"] == 2:
                _git(repository_path, "add", "b.txt")
        return original(self, repository_path, *args)

    monkeypatch.setattr(snapshot_module.GitSnapshotCollector, "_git", mutate_stage)
    with pytest.raises(GitWorktreeChangedError, match="stage"):
        _collect(repo, store, collector=collector)
    assert not (store.root / "sha256").exists()


def test_repeated_stable_collection_equal_with_ignored_and_binary(tmp_path):
    repo = _init_repo(
        tmp_path, {"a.txt": b"one\n", ".gitignore": b"*.secret\n"}
    )
    (repo / "a.txt").write_bytes(b"two\n")
    (repo / "blob.bin").write_bytes(b"\x00bin\x00")
    (repo / "cache.secret").write_bytes(b"hidden\n")
    store = ArtifactStore(tmp_path / "artifact-store")
    first = _collect(repo, store)
    second = _collect(repo, store)
    assert second == first
    assert first.snapshot.ignored_files_lower_bound == 1
    assert first.snapshot.ignored_scan_truncated is False
