"""确定性命令收集器（V2-P2-03）合同与收集器测试。"""

import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    ArtifactStore,
    CommandBatchResult,
    CommandBatchSnapshot,
    CommandCollectionError,
    CommandExecutionError,
    CommandLaunchError,
    CommandObservation,
    CommandSpec,
    CommandSpecError,
    DeterministicCommandCollector,
)
from assurance import commands as commands_module
from assurance.contracts import Evidence


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "a" * 64
EXPECTED_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "PYTHONHASHSEED": "0",
    "LC_ALL": "C",
    "LANG": "C",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_NO_INPUT": "1",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _expected_fingerprint() -> str:
    return _sha256(
        json.dumps(
            EXPECTED_ENV,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def _spec(**overrides):
    values = {
        "schema_version": "v1",
        "command_id": "unit_test",
        "kind": "test",
        "argv": ("python", "-c", "print('ok')"),
        "cwd": ".",
        "timeout_seconds": 30.0,
        "max_output_bytes": 65536,
    }
    values.update(overrides)
    return CommandSpec(**values)


def _observation(**overrides):
    values = {
        "schema_version": "v1",
        "command_id": "unit_test",
        "kind": "test",
        "argv": ("python", "-c", "print('ok')"),
        "cwd": ".",
        "outcome": "success",
        "exit_code": 0,
        "duration_ms": 12,
        "stdout_artifact_digest": "sha256:" + "1" * 64,
        "stderr_artifact_digest": "sha256:" + "2" * 64,
        "stdout_bytes": 2,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    values.update(overrides)
    return CommandObservation(**values)


def _snapshot(**overrides):
    observation = _observation()
    values = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "commands": [observation],
        "environment_fingerprint": "sha256:" + "3" * 64,
        "manifest_artifact_digest": "sha256:" + "4" * 64,
        "complete": True,
        "all_passed": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return CommandBatchSnapshot(**values)


def _evidence(snapshot, status="success", **overrides):
    values = {
        "schema_version": "v1",
        "evidence_id": "ev_command_" + "e" * 32,
        "subject_digest": snapshot.subject_digest,
        "kind": "command_batch",
        "producer": "collector.command",
        "artifact_digest": snapshot.manifest_artifact_digest,
        "source_ref": f"command_batch:{snapshot.subject_digest}",
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
    return CommandBatchResult(**values)


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _collector(*specs):
    return DeterministicCommandCollector(tuple(specs))


def _collect(
    collector,
    ws,
    store,
    command_ids=None,
    subject=SUBJECT,
    collected_at=FIXED_TIME,
):
    return collector.collect(
        ws,
        subject_digest=subject,
        artifact_store=store,
        command_ids=(
            command_ids
            if command_ids is not None
            else tuple(spec.command_id for spec in collector._allowed_commands)
        ),
        collected_at=collected_at,
    )


def _many_specs(count):
    return tuple(
        _spec(command_id=f"cmd_{index:02d}") for index in range(count)
    )


def _manifest(result, store):
    return json.loads(store.get_bytes(result.snapshot.manifest_artifact_digest))


class _FakeStream:
    """Deterministic in-memory pipe stand-in."""

    def __init__(self, chunks=()):
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
    """Controllable Popen stand-in modeling wait/terminate/kill/poll."""

    def __init__(
        self,
        stdout=None,
        stderr=None,
        *,
        returncode=0,
        timeout_waits=0,
        wait_error=None,
        stubborn=False,
    ):
        self.pid = 4194304
        self.stdout = (
            stdout if hasattr(stdout, "read") else _FakeStream(stdout or ())
        )
        self.stderr = (
            stderr if hasattr(stderr, "read") else _FakeStream(stderr or ())
        )
        self.returncode = None
        self._final_returncode = returncode
        self._timeout_waits = timeout_waits
        self._wait_error = wait_error
        self._stubborn = stubborn
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_timeouts = []
        self.poll_calls = 0
        self._killed = False
        self._waited = False

    def poll(self):
        self.poll_calls += 1
        return self.returncode if (self._waited or self._killed) else None

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if self._wait_error is not None and not self._waited:
            error = self._wait_error
            self._wait_error = None
            raise error
        if (self._timeout_waits > 0 or self._stubborn) and not self._killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = self._final_returncode
        self._waited = True
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if not self._stubborn:
            self._killed = True

    def kill(self):
        self.kill_calls += 1
        self._killed = True


class _RecordingPopen(_FakeProcess):
    """Popen stand-in that records args and kwargs."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        super().__init__(stdout=(), stderr=(), returncode=0)


def _install_fake_popen(monkeypatch, fakes):
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return fakes[len(calls) - 1]

    monkeypatch.setattr(commands_module.subprocess, "Popen", fake_popen)
    return calls


def _install_recording_popen(monkeypatch):
    instances = []

    def fake_popen(args, **kwargs):
        fake = _RecordingPopen(args, **kwargs)
        instances.append(fake)
        return fake

    monkeypatch.setattr(commands_module.subprocess, "Popen", fake_popen)
    return instances


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
    "GitChange",
    "GitSnapshot",
    "GitSnapshotResult",
    "GitSnapshotCollector",
    "GitSnapshotError",
    "GitRepositoryError",
    "GitCommandError",
    "GitWorktreeChangedError",
    "IntakeDocument",
    "IntakeNotice",
    "IntakeSnapshot",
    "IntakeResult",
    "TaskPolicyCollector",
    "IntakeCollectionError",
    "IntakePathError",
    "IntakeFormatError",
    "IntakeChangedError",
]

NEW_PUBLIC_NAMES = {
    "CommandSpec",
    "CommandObservation",
    "CommandBatchSnapshot",
    "CommandBatchResult",
    "DeterministicCommandCollector",
    "CommandCollectionError",
    "CommandSpecError",
    "CommandLaunchError",
    "CommandExecutionError",
}


def test_package_exports_preserve_all_prior_names_and_add_commands_api():
    assert set(PRIOR_PUBLIC_NAMES) | NEW_PUBLIC_NAMES <= set(assurance.__all__)
    assert set(assurance.__all__) != set(PRIOR_PUBLIC_NAMES)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.CommandSpec is commands_module.CommandSpec
    assert assurance.CommandObservation is commands_module.CommandObservation
    assert assurance.CommandBatchSnapshot is commands_module.CommandBatchSnapshot
    assert assurance.CommandBatchResult is commands_module.CommandBatchResult
    assert (
        assurance.DeterministicCommandCollector
        is commands_module.DeterministicCommandCollector
    )
    assert assurance.CommandCollectionError is commands_module.CommandCollectionError
    assert assurance.CommandSpecError is commands_module.CommandSpecError
    assert assurance.CommandLaunchError is commands_module.CommandLaunchError
    assert (
        assurance.CommandExecutionError
        is commands_module.CommandExecutionError
    )


def test_exception_hierarchy_is_simple():
    assert issubclass(CommandSpecError, CommandCollectionError)
    assert issubclass(CommandLaunchError, CommandCollectionError)
    assert issubclass(CommandExecutionError, CommandCollectionError)


def test_command_spec_field_order_frozen_extra_forbid_and_roundtrip():
    assert list(CommandSpec.model_fields) == [
        "schema_version",
        "command_id",
        "kind",
        "argv",
        "cwd",
        "timeout_seconds",
        "max_output_bytes",
    ]
    assert CommandSpec.model_config["frozen"] is True
    assert CommandSpec.model_config["extra"] == "forbid"
    spec = _spec()
    dumped = spec.model_dump(mode="json")
    restored = CommandSpec.model_validate(dumped)
    assert restored == spec
    assert spec.model_dump_json() == spec.model_dump_json()
    assert spec.model_dump()["argv"] == ("python", "-c", "print('ok')")
    assert dumped["argv"] == ["python", "-c", "print('ok')"]
    with pytest.raises(ValidationError):
        spec.command_id = "mutated"
    with pytest.raises(ValidationError):
        CommandSpec.model_validate({**spec.model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        _spec(schema_version="v2")


def test_command_spec_argv_input_copy_safety_and_shell_data():
    source = ["echo", "a; touch sentinel", "$HOME", "`id`"]
    spec = _spec(argv=source)
    source.append("mutated")
    assert spec.argv == ("echo", "a; touch sentinel", "$HOME", "`id`")
    with pytest.raises(TypeError):
        spec.argv[0] = "changed"


def test_command_spec_command_id_regex():
    for value in ("a", "abc123", "a_b-c.d", "a" + "x" * 63):
        assert _spec(command_id=value).command_id == value
    for value in (
        "",
        "A",
        "1abc",
        "_abc",
        "a b",
        "a/b",
        "a\n",
        "é",
        "a" + "x" * 64,
    ):
        with pytest.raises(ValidationError):
            _spec(command_id=value)


def test_command_spec_kind_literal():
    for kind in ("test", "build", "lint", "static"):
        assert _spec(kind=kind).kind == kind
    for kind in ("unit", "TEST", "compile", None, 1):
        with pytest.raises(ValidationError):
            _spec(kind=kind)


def test_command_spec_argv_validation():
    with pytest.raises(ValidationError):
        _spec(argv=())
    with pytest.raises(ValidationError):
        _spec(argv=("",))
    with pytest.raises(ValidationError):
        _spec(argv=("   ",))
    with pytest.raises(ValidationError):
        _spec(argv=("a\x00b",))
    with pytest.raises(ValidationError):
        _spec(argv=(1,))
    with pytest.raises(ValidationError):
        _spec(argv=(None,))
    with pytest.raises(ValidationError):
        _spec(argv=("x",) * 33)
    assert len(_spec(argv=("x",) * 32).argv) == 32


def test_command_spec_argv_utf8_byte_boundary():
    assert _spec(argv=("a" * 4095, "b")).argv == ("a" * 4095, "b")
    assert len(("a" * 4095 + "b").encode("utf-8")) == 4096
    assert _spec(argv=("é", "a" * 4094)).argv == ("é", "a" * 4094)
    with pytest.raises(ValidationError):
        _spec(argv=("a" * 4095, "bb"))


def test_command_spec_cwd_canonical_validation():
    assert _spec(cwd=".").cwd == "."
    assert _spec(cwd="a/b").cwd == "a/b"
    for bad in (
        "",
        "/abs",
        "a/../b",
        "a//b",
        "a/./b",
        "a\\b",
        "a/",
        "./a",
        "C:/x",
        "//unc",
    ):
        with pytest.raises(ValidationError):
            _spec(cwd=bad)


def test_command_spec_timeout_strict_finite_range():
    assert _spec(timeout_seconds=0.001).timeout_seconds == 0.001
    assert _spec(timeout_seconds=300.0).timeout_seconds == 300.0
    assert _spec(timeout_seconds=30).timeout_seconds == 30.0
    for bad in (
        True,
        False,
        "30",
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -1.0,
        300.0001,
    ):
        with pytest.raises(ValidationError):
            _spec(timeout_seconds=bad)


def test_command_spec_max_output_strict_int_range():
    assert _spec(max_output_bytes=1).max_output_bytes == 1
    assert _spec(max_output_bytes=1048576).max_output_bytes == 1048576
    for bad in (True, False, "65536", 0, -1, 1048577, 1.5, None):
        with pytest.raises(ValidationError):
            _spec(max_output_bytes=bad)


def test_command_observation_field_order_frozen_and_roundtrip():
    assert list(CommandObservation.model_fields) == [
        "schema_version",
        "command_id",
        "kind",
        "argv",
        "cwd",
        "outcome",
        "exit_code",
        "duration_ms",
        "stdout_artifact_digest",
        "stderr_artifact_digest",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_truncated",
        "stderr_truncated",
    ]
    assert CommandObservation.model_config["frozen"] is True
    assert CommandObservation.model_config["extra"] == "forbid"
    observation = _observation()
    restored = CommandObservation.model_validate(
        observation.model_dump(mode="json")
    )
    assert restored == observation
    assert observation.model_dump_json() == observation.model_dump_json()
    with pytest.raises(ValidationError):
        observation.outcome = "failure"
    with pytest.raises(ValidationError):
        CommandObservation.model_validate(
            {**_observation().model_dump(), "unexpected": 1}
        )


def test_observation_has_no_redundant_max_field():
    assert "max_output_bytes" not in CommandObservation.model_fields


def test_command_observation_cross_invariants():
    with pytest.raises(ValidationError):
        _observation(outcome="success", exit_code=None)
    with pytest.raises(ValidationError):
        _observation(outcome="success", exit_code=1)
    _observation(outcome="success", exit_code=0)
    with pytest.raises(ValidationError):
        _observation(outcome="failure", exit_code=None)
    with pytest.raises(ValidationError):
        _observation(outcome="failure", exit_code=0)
    _observation(outcome="failure", exit_code=1)
    _observation(outcome="failure", exit_code=-1)
    with pytest.raises(ValidationError):
        _observation(outcome="timeout", exit_code=0)
    _observation(outcome="timeout", exit_code=None)
    with pytest.raises(ValidationError):
        _observation(outcome="output_limit", exit_code=0)
    with pytest.raises(ValidationError):
        _observation(
            outcome="output_limit",
            stdout_truncated=False,
            stderr_truncated=False,
        )
    _observation(
        outcome="output_limit", exit_code=None, stdout_truncated=True
    )
    _observation(
        outcome="output_limit", exit_code=None, stderr_truncated=True
    )
    for outcome in ("success", "failure", "timeout"):
        with pytest.raises(ValidationError):
            _observation(outcome=outcome, stdout_truncated=True)
        with pytest.raises(ValidationError):
            _observation(outcome=outcome, stderr_truncated=True)


def test_command_observation_strict_types_and_bytes_cap():
    for bad in (True, "0", 1.5, None):
        with pytest.raises(ValidationError):
            _observation(outcome="failure", exit_code=bad)
    for bad in (-1, True, "0", 1.5):
        with pytest.raises(ValidationError):
            _observation(duration_ms=bad)
    for field in ("stdout_bytes", "stderr_bytes"):
        for bad in (-1, True, "0", 1.5):
            with pytest.raises(ValidationError):
                _observation(**{field: bad})
        with pytest.raises(ValidationError):
            _observation(**{field: 1048577})
    for field in ("stdout_truncated", "stderr_truncated"):
        for bad in (1, 0, "true"):
            with pytest.raises(ValidationError):
                _observation(**{field: bad})
    for field in ("stdout_artifact_digest", "stderr_artifact_digest"):
        for bad in (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "md5:" + "a" * 64,
        ):
            with pytest.raises(ValidationError):
                _observation(**{field: bad})


def test_command_batch_snapshot_field_order_frozen_and_roundtrip():
    assert list(CommandBatchSnapshot.model_fields) == [
        "schema_version",
        "subject_digest",
        "commands",
        "environment_fingerprint",
        "manifest_artifact_digest",
        "complete",
        "all_passed",
        "collected_at",
    ]
    assert CommandBatchSnapshot.model_config["frozen"] is True
    assert CommandBatchSnapshot.model_config["extra"] == "forbid"
    snapshot = _snapshot()
    restored = CommandBatchSnapshot.model_validate(
        snapshot.model_dump(mode="json")
    )
    assert restored == snapshot
    assert snapshot.model_dump_json() == snapshot.model_dump_json()
    with pytest.raises(ValidationError):
        snapshot.commands = ()
    with pytest.raises(ValidationError):
        CommandBatchSnapshot.model_validate(
            {**_snapshot().model_dump(), "unexpected": 1}
        )


def test_command_batch_snapshot_requires_commands_and_unique_ids():
    values = _snapshot().model_dump()
    values["commands"] = []
    with pytest.raises(ValidationError):
        CommandBatchSnapshot.model_validate(values)
    with pytest.raises(ValidationError):
        _snapshot(commands=[_observation(), _observation()])
    unique = _snapshot(
        commands=[_observation(), _observation(command_id="second")]
    )
    assert [obs.command_id for obs in unique.commands] == [
        "unit_test",
        "second",
    ]


def test_command_batch_snapshot_complete_and_all_passed_invariants():
    failure = _observation(outcome="failure", exit_code=1)
    timeout = _observation(outcome="timeout", exit_code=None)
    with pytest.raises(ValidationError):
        _snapshot(commands=[failure], complete=False, all_passed=True)
    with pytest.raises(ValidationError):
        _snapshot(commands=[failure], complete=False, all_passed=False)
    failure_snapshot = _snapshot(
        commands=[failure], complete=True, all_passed=False
    )
    assert failure_snapshot.complete is True
    assert failure_snapshot.all_passed is False
    with pytest.raises(ValidationError):
        _snapshot(commands=[timeout], complete=True, all_passed=False)
    timeout_snapshot = _snapshot(
        commands=[timeout], complete=False, all_passed=False
    )
    assert timeout_snapshot.complete is False
    assert timeout_snapshot.all_passed is False
    output_limit = _observation(
        command_id="lim_out",
        outcome="output_limit",
        exit_code=None,
        stdout_truncated=True,
    )
    mixed = _snapshot(
        commands=[failure, output_limit],
        complete=False,
        all_passed=False,
    )
    assert mixed.complete is False
    assert mixed.all_passed is False


def test_command_batch_snapshot_strict_fields():
    for bad in (1, "true"):
        with pytest.raises(ValidationError):
            _snapshot(complete=bad)
        with pytest.raises(ValidationError):
            _snapshot(all_passed=bad)
    for field in (
        "subject_digest",
        "environment_fingerprint",
        "manifest_artifact_digest",
    ):
        for bad in ("sha256:" + "A" * 64, "a" * 64):
            with pytest.raises(ValidationError):
                _snapshot(**{field: bad})
    with pytest.raises(ValidationError):
        _snapshot(collected_at=datetime(2026, 8, 25, 8, 0))


def test_command_batch_result_field_order_frozen_and_roundtrip():
    assert list(CommandBatchResult.model_fields) == [
        "schema_version",
        "snapshot",
        "evidence",
    ]
    result = _result()
    restored = CommandBatchResult.model_validate(
        result.model_dump(mode="json")
    )
    assert restored == result
    with pytest.raises(ValidationError):
        result.snapshot = _snapshot()


def test_command_batch_result_cross_rules():
    snapshot = _snapshot()
    with pytest.raises(ValidationError):
        _result(
            snapshot=snapshot,
            evidence=_evidence(
                snapshot, subject_digest="sha256:" + "b" * 64
            ),
        )
    with pytest.raises(ValidationError):
        _result(
            evidence=_evidence(
                snapshot, artifact_digest="sha256:" + "c" * 64
            )
        )
    for bad_kwargs in (
        {"kind": "git_snapshot"},
        {"producer": "collector.git"},
        {"trust_level": "observed"},
        {"source_ref": "command_batch:other"},
    ):
        with pytest.raises(ValidationError):
            _result(evidence=_evidence(snapshot, **bad_kwargs))
    with pytest.raises(ValidationError):
        _result(
            evidence=_evidence(
                snapshot,
                collected_at=FIXED_TIME.replace(minute=1),
            )
        )
    with pytest.raises(ValidationError):
        _result(evidence=_evidence(snapshot, status="failure"))


def test_command_batch_result_evidence_status_precedence():
    failure = _observation(
        command_id="f_out", outcome="failure", exit_code=1
    )
    timeout = _observation(
        command_id="t_out", outcome="timeout", exit_code=None
    )
    output_limit = _observation(
        outcome="output_limit", exit_code=None, stdout_truncated=True
    )
    snap_success = _snapshot(commands=[_observation()])
    snap_failure = _snapshot(
        commands=[failure], complete=True, all_passed=False
    )
    snap_timeout = _snapshot(
        commands=[timeout], complete=False, all_passed=False
    )
    snap_output = _snapshot(
        commands=[output_limit], complete=False, all_passed=False
    )
    snap_mixed = _snapshot(
        commands=[failure, timeout], complete=False, all_passed=False
    )
    assert (
        _result(
            snapshot=snap_success,
            evidence=_evidence(snap_success, status="success"),
        ).evidence.status
        == "success"
    )
    assert (
        _result(
            snapshot=snap_failure,
            evidence=_evidence(snap_failure, status="failure"),
        ).evidence.status
        == "failure"
    )
    assert (
        _result(
            snapshot=snap_timeout,
            evidence=_evidence(snap_timeout, status="truncated"),
        ).evidence.status
        == "truncated"
    )
    assert (
        _result(
            snapshot=snap_output,
            evidence=_evidence(snap_output, status="truncated"),
        ).evidence.status
        == "truncated"
    )
    assert (
        _result(
            snapshot=snap_mixed,
            evidence=_evidence(snap_mixed, status="truncated"),
        ).evidence.status
        == "truncated"
    )
    with pytest.raises(ValidationError):
        _result(
            snapshot=snap_failure,
            evidence=_evidence(snap_failure, status="truncated"),
        )


def test_constructor_requires_exact_tuple_of_command_specs():
    spec = _spec()
    with pytest.raises(TypeError):
        DeterministicCommandCollector([spec])
    with pytest.raises(TypeError):
        DeterministicCommandCollector({spec.command_id: spec})
    with pytest.raises(TypeError):
        DeterministicCommandCollector((spec, "x"))
    with pytest.raises(TypeError):
        DeterministicCommandCollector((spec, {}))
    with pytest.raises(ValueError):
        DeterministicCommandCollector(())
    with pytest.raises(ValueError):
        DeterministicCommandCollector(_many_specs(17))
    with pytest.raises(ValueError):
        DeterministicCommandCollector(
            (
                _spec(command_id="dup"),
                _spec(command_id="dup"),
            )
        )


def test_constructor_immutable_lookup_keeps_tuple():
    specs = (_spec(), _spec(command_id="second"))
    collector = DeterministicCommandCollector(specs)
    assert collector._allowed_commands is specs
    assert isinstance(collector._lookup, MappingProxyType)
    assert collector._lookup["unit_test"] is specs[0]
    with pytest.raises(TypeError):
        collector._lookup["other"] = specs[0]


def test_collect_validation_types_and_values(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec())
    with pytest.raises(TypeError):
        collector.collect(
            "not-a-path",
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest="bad",
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(TypeError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=object(),
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(TypeError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=["unit_test"],
            collected_at=FIXED_TIME,
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=(),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test", "unit_test"),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unknown",),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(TypeError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test", 1),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=tuple(f"id_{index:02d}" for index in range(17)),
            collected_at=FIXED_TIME,
        )
    with pytest.raises(TypeError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at="2026-08-25T08:00:00+00:00",
        )
    with pytest.raises(ValueError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=datetime(2026, 8, 25, 8, 0),
        )


class _StrSubclass(str):
    pass


def test_collect_rejects_str_subclass_command_ids(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec())
    with pytest.raises(
        TypeError, match="command_ids must contain exact str values"
    ):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=(_StrSubclass("unit_test"),),
            collected_at=FIXED_TIME,
        )


def test_collect_defaults_collected_at_to_utc_now(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    result = _collect(collector, ws, store, collected_at=None)
    assert result.snapshot.collected_at.tzinfo is not None
    assert result.snapshot.collected_at.utcoffset() == timedelta(0)
    assert result.evidence.collected_at == result.snapshot.collected_at


def test_collect_preserves_explicit_aware_collected_at(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    aware_plus8 = datetime(2026, 8, 25, 16, 0, tzinfo=timezone(timedelta(hours=8)))
    result = _collect(collector, ws, store, collected_at=aware_plus8)
    assert result.snapshot.collected_at == aware_plus8


def test_collect_rejects_missing_workspace(tmp_path):
    ws = tmp_path / "missing"
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec())
    with pytest.raises(CommandSpecError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )


def test_collect_rejects_workspace_file(tmp_path):
    ws = tmp_path / "file"
    ws.write_text("x")
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec())
    with pytest.raises(CommandSpecError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )


def test_collect_rejects_workspace_final_symlink(tmp_path):
    ws = tmp_path / "link"
    ws.symlink_to(tmp_path / "target")
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec())
    with pytest.raises(CommandSpecError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )


def _workspace_with_dirs(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "sub").mkdir()
    (ws / "sub" / "deep").mkdir()
    (ws / "plain_file").write_text("x")
    (ws / "sub" / "link").symlink_to(ws / "sub" / "deep")
    (ws / "parent_link").symlink_to(ws)
    return ws


@pytest.mark.parametrize(
    "bad_cwd",
    ["missing", "plain_file", "sub/link", "parent_link/sub"],
)
def test_collect_rejects_invalid_command_cwd(tmp_path, bad_cwd):
    ws = _workspace_with_dirs(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    collector = _collector(_spec(cwd=bad_cwd))
    with pytest.raises(CommandSpecError):
        collector.collect(
            ws,
            subject_digest=SUBJECT,
            artifact_store=store,
            command_ids=("unit_test",),
            collected_at=FIXED_TIME,
        )


def test_collect_resolves_valid_command_cwd_under_root(
    tmp_path, monkeypatch
):
    ws = _workspace_with_dirs(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    instances = _install_recording_popen(monkeypatch)
    collector = _collector(_spec(cwd="sub/deep"))
    result = _collect(collector, ws, store)
    assert result.snapshot.commands[0].outcome == "success"
    assert Path(instances[0].kwargs["cwd"]) == ws / "sub" / "deep"


def test_restricted_environment_exact_eight_keys_and_fingerprint(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    instances = _install_recording_popen(monkeypatch)
    collector = _collector(_spec())
    result = _collect(collector, ws, store)
    env = instances[0].kwargs["env"]
    assert list(env) == list(EXPECTED_ENV)
    assert env == EXPECTED_ENV
    assert result.snapshot.environment_fingerprint == _expected_fingerprint()


def test_popen_kwargs_are_exact_and_shell_false(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    instances = _install_recording_popen(monkeypatch)
    spec = _spec(
        command_id="meta",
        argv=("echo", "a; touch sentinel", "$HOME", "`id`"),
    )
    collector = _collector(spec)
    _collect(collector, ws, store)
    args, kwargs = instances[0].args, instances[0].kwargs
    assert args == ["echo", "a; touch sentinel", "$HOME", "`id`"]
    assert isinstance(args, list)
    assert Path(kwargs["cwd"]) == ws
    assert kwargs["env"] == EXPECTED_ENV
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True


def test_shell_metacharacters_are_literal_data_no_sentinel(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="echo_meta",
        argv=("echo", "a; touch sentinel", "$HOME", "`id`"),
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "success"
    assert observation.stdout_bytes > 0
    assert not (ws / "sentinel").exists()
    assert [child.name for child in ws.iterdir()] == []


def test_fake_simultaneous_large_output_caps_both_streams(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=(b"x" * 65536, b"more"),
        stderr=(b"y" * 65536, b"more"),
        returncode=-15,
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "output_limit"
    assert observation.exit_code is None
    assert observation.stdout_truncated is True
    assert observation.stderr_truncated is True
    assert observation.stdout_bytes == 65536
    assert observation.stderr_bytes == 65536
    assert fake.terminate_calls >= 1
    assert fake.wait_calls >= 1
    assert fake.poll_calls >= 1
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True
    assert result.snapshot.complete is False
    assert result.snapshot.all_passed is False
    assert result.evidence.status == "truncated"


def test_fake_timeout_terminates_reaps_and_preserves_output(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=(b"partial",),
        stderr=(),
        returncode=-15,
        timeout_waits=1,
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec(timeout_seconds=0.01))
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "timeout"
    assert observation.exit_code is None
    assert store.get_bytes(observation.stdout_artifact_digest) == b"partial"
    assert observation.stdout_truncated is False
    assert observation.stderr_truncated is False
    assert fake.terminate_calls == 1
    assert fake.kill_calls == 0
    assert fake.wait_calls >= 2
    assert fake.stdout.closed is True
    assert fake.stderr.closed is True
    assert result.evidence.status == "truncated"


def test_fake_nonzero_failure(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(b"err",), returncode=3)
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "failure"
    assert observation.exit_code == 3
    assert store.get_bytes(observation.stdout_artifact_digest) == b"out"
    assert store.get_bytes(observation.stderr_artifact_digest) == b"err"
    assert observation.stdout_truncated is False
    assert observation.stderr_truncated is False
    assert fake.terminate_calls == 0
    assert fake.kill_calls == 0
    assert result.snapshot.complete is True
    assert result.snapshot.all_passed is False
    assert result.evidence.status == "failure"


def test_reader_exception_raises_execution_error_no_artifacts(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=_ExplodingStream(OSError, "pipe broken"),
        stderr=(),
        returncode=0,
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    with pytest.raises(CommandExecutionError):
        _collect(collector, ws, store)
    assert list(store.root.rglob("*")) == []


def test_bounded_reader_requests_at_most_remaining_plus_one_and_stops_after_cap():
    stream = _FakeStream(chunks=(b"x" * 16, b"more", b"tail"))
    event = threading.Event()
    reader = commands_module._BoundedPipeReader(stream, 16, event)
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert stream.read_sizes == [17, 1]
    assert all(size <= 17 for size in stream.read_sizes)
    assert reader.data == b"x" * 16
    assert reader.truncated is True
    assert event.is_set()


def test_cap_escalates_stubborn_process_within_cleanup_window(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=(b"x" * 64, b"more"),
        stderr=(),
        returncode=-9,
        stubborn=True,
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec(max_output_bytes=16, timeout_seconds=300))
    started = time.monotonic()
    result = _collect(collector, ws, store)
    elapsed = time.monotonic() - started
    observation = result.snapshot.commands[0]
    assert observation.outcome == "output_limit"
    assert observation.stdout_bytes == 16
    assert observation.stdout_truncated is True
    assert fake.terminate_calls == 1
    assert fake.kill_calls == 1
    assert fake.wait_calls >= 2
    assert all(
        timeout is None
        or timeout <= commands_module._TERMINATION_WAIT_SECONDS
        for timeout in fake.wait_timeouts
    )
    assert elapsed < 5.0
    assert result.snapshot.complete is False
    assert result.evidence.status == "truncated"


def test_pipe_error_escalates_stubborn_process_and_raises_no_artifacts(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=_ExplodingStream(OSError, "pipe broken"),
        stderr=(),
        returncode=0,
        stubborn=True,
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec(timeout_seconds=300))
    started = time.monotonic()
    with pytest.raises(CommandExecutionError):
        _collect(collector, ws, store)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert fake.terminate_calls == 1
    assert fake.kill_calls == 1
    assert all(
        timeout is None
        or timeout <= commands_module._TERMINATION_WAIT_SECONDS
        for timeout in fake.wait_timeouts
    )
    assert list(store.root.rglob("*")) == []


def test_join_readers_fails_closed_when_reader_remains_alive():
    class _LingeringReader:
        def __init__(self):
            self._stream = _FakeStream()

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    with pytest.raises(CommandExecutionError):
        commands_module.DeterministicCommandCollector._join_readers(
            (_LingeringReader(),)
        )


def test_launch_exception_raises_launch_error_no_artifacts(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")

    def boom(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(commands_module.subprocess, "Popen", boom)
    collector = _collector(_spec())
    with pytest.raises(CommandLaunchError):
        _collect(collector, ws, store)
    assert list(store.root.rglob("*")) == []


def test_batch_launch_error_writes_no_artifacts_after_earlier_success(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake_ok = _FakeProcess(stdout=(b"ok",), stderr=(), returncode=0)

    def fake_popen(args, **kwargs):
        if args[0] == "boom":
            raise OSError("denied")
        return fake_ok

    monkeypatch.setattr(commands_module.subprocess, "Popen", fake_popen)
    collector = _collector(
        _spec(command_id="first"),
        _spec(command_id="second", argv=("boom",)),
    )
    with pytest.raises(CommandLaunchError):
        _collect(
            collector,
            ws,
            store,
            command_ids=("first", "second"),
        )
    assert list(store.root.rglob("*")) == []


def test_wait_internal_error_raises_execution_error(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(
        stdout=(),
        stderr=(),
        returncode=0,
        wait_error=OSError("reap failed"),
    )
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    with pytest.raises(CommandExecutionError):
        _collect(collector, ws, store)
    assert list(store.root.rglob("*")) == []


SUCCESS_SCRIPT = (
    "import sys\n"
    "sys.stdout.write('hello out\\n')\n"
    "sys.stderr.write('hello err\\n')\n"
)
FAILURE_SCRIPT = "import sys; sys.stderr.write('boom\\n'); sys.exit(7)\n"
TIMEOUT_SCRIPT = (
    "import sys, time\n"
    "sys.stdout.write('early\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)
CAP_SCRIPT = (
    "import sys, time\n"
    "sys.stdout.write('x' * 200000)\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def test_real_success_stdout_and_stderr(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="real_ok",
        argv=(sys.executable, "-c", SUCCESS_SCRIPT),
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "success"
    assert observation.exit_code == 0
    assert store.get_bytes(observation.stdout_artifact_digest) == b"hello out\n"
    assert store.get_bytes(observation.stderr_artifact_digest) == b"hello err\n"
    assert observation.stdout_bytes == len(b"hello out\n")
    assert observation.stderr_bytes == len(b"hello err\n")
    assert observation.stdout_truncated is False
    assert observation.stderr_truncated is False
    assert store.get_bytes(observation.stdout_artifact_digest) == b"hello out\n"
    assert store.get_bytes(observation.stderr_artifact_digest) == b"hello err\n"
    assert result.snapshot.complete is True
    assert result.snapshot.all_passed is True
    assert result.evidence.status == "success"


def test_real_failure_nonzero_exit(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="real_fail",
        argv=(sys.executable, "-c", FAILURE_SCRIPT),
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "failure"
    assert observation.exit_code == 7
    assert store.get_bytes(observation.stderr_artifact_digest) == b"boom\n"
    assert store.get_bytes(observation.stderr_artifact_digest) == b"boom\n"
    assert result.snapshot.complete is True
    assert result.snapshot.all_passed is False
    assert result.evidence.status == "failure"


def test_real_timeout_preserves_early_output(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="real_timeout",
        argv=(sys.executable, "-c", TIMEOUT_SCRIPT),
        timeout_seconds=0.3,
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "timeout"
    assert observation.exit_code is None
    assert store.get_bytes(observation.stdout_artifact_digest) == b"early\n"
    assert observation.stdout_truncated is False
    assert observation.stderr_truncated is False
    assert result.snapshot.complete is False
    assert result.evidence.status == "truncated"


def test_real_output_cap_retains_exact_limit(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="real_cap",
        argv=(sys.executable, "-c", CAP_SCRIPT),
        max_output_bytes=1024,
        timeout_seconds=10,
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "output_limit"
    assert observation.exit_code is None
    assert observation.stdout_truncated is True
    assert observation.stdout_bytes == 1024
    assert (
        store.get_bytes(observation.stdout_artifact_digest) == b"x" * 1024
    )
    assert observation.stderr_bytes <= 1024
    assert result.snapshot.complete is False
    assert result.evidence.status == "truncated"


def _grandchild_script(pid_file: Path, cap: bool = False) -> str:
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
    )
    if cap:
        body += (
            "sys.stdout.write('x' * 200000)\n"
            "sys.stdout.flush()\n"
        )
    body += "time.sleep(60)\n"
    return body


def _wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.05)
    return False


def test_real_timeout_cleans_up_child_process_group(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    pid_file = ws / "child.pid"
    spec = _spec(
        command_id="real_chain_timeout",
        argv=(sys.executable, "-c", _grandchild_script(pid_file)),
        timeout_seconds=1.0,
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "timeout"
    assert _wait_for_file(pid_file, 3.0)
    child_pid = int(pid_file.read_text().strip())
    assert _wait_for_process_exit(child_pid, 5.0)


def test_real_output_cap_cleans_up_child_process_group(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    pid_file = ws / "child.pid"
    spec = _spec(
        command_id="real_chain_cap",
        argv=(sys.executable, "-c", _grandchild_script(pid_file, cap=True)),
        max_output_bytes=4096,
        timeout_seconds=10,
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    assert observation.outcome == "output_limit"
    assert _wait_for_file(pid_file, 3.0)
    child_pid = int(pid_file.read_text().strip())
    assert _wait_for_process_exit(child_pid, 5.0)


def test_manifest_omits_workspace_collected_at_and_environment_values(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(b"out",), stderr=(), returncode=0)
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec(argv=("echo", "ok")))
    result = _collect(collector, ws, store)
    payload = _manifest(result, store)
    text = store.get_bytes(
        result.snapshot.manifest_artifact_digest
    ).decode("utf-8")
    assert set(payload) == {
        "schema_version",
        "subject_digest",
        "observations",
        "environment_fingerprint",
        "complete",
        "all_passed",
        "limits",
    }
    assert payload["schema_version"] == "v1"
    assert payload["subject_digest"] == SUBJECT
    assert payload["environment_fingerprint"] == _expected_fingerprint()
    assert payload["limits"] == {
        "max_commands": 16,
        "read_chunk_bytes": 65536,
    }
    assert payload["complete"] is True
    assert payload["all_passed"] is True
    assert "PATH" not in text
    assert os.environ.get("PATH", "") not in text
    assert "PYTHONHASHSEED" not in text
    assert "LC_ALL" not in text
    assert str(ws) not in text
    assert FIXED_TIME.isoformat() not in text
    assert "collected_at" not in text
    assert "max_output_bytes" not in payload["observations"][0]
    assert payload["observations"][0]["command_id"] == "unit_test"


def test_requested_order_preserved_and_continue_after_failure_and_timeout(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fakes = [
        _FakeProcess(stdout=(b"f",), stderr=(), returncode=1),
        _FakeProcess(stdout=(b"o",), stderr=(), returncode=0),
        _FakeProcess(
            stdout=(b"t",),
            stderr=(),
            returncode=-15,
            timeout_waits=1,
        ),
        _FakeProcess(stdout=(b"s",), stderr=(), returncode=0),
    ]
    _install_fake_popen(monkeypatch, fakes)
    collector = _collector(
        _spec(command_id="a_fail"),
        _spec(command_id="b_ok"),
        _spec(command_id="c_timeout", timeout_seconds=0.01),
        _spec(command_id="d_ok2"),
    )
    result = _collect(
        collector,
        ws,
        store,
        command_ids=("a_fail", "b_ok", "c_timeout", "d_ok2"),
    )
    assert [obs.command_id for obs in result.snapshot.commands] == [
        "a_fail",
        "b_ok",
        "c_timeout",
        "d_ok2",
    ]
    assert [obs.outcome for obs in result.snapshot.commands] == [
        "failure",
        "success",
        "timeout",
        "success",
    ]
    assert result.snapshot.complete is False
    assert result.snapshot.all_passed is False
    assert result.evidence.status == "truncated"


def test_requested_order_preserved_and_continue_after_output_limit(
    tmp_path, monkeypatch
):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fakes = [
        _FakeProcess(
            stdout=(b"x" * 100,),
            stderr=(),
            returncode=0,
        ),
        _FakeProcess(stdout=(b"ok",), stderr=(), returncode=0),
    ]
    _install_fake_popen(monkeypatch, fakes)
    collector = _collector(
        _spec(command_id="cap_cmd", max_output_bytes=16),
        _spec(command_id="after_cap"),
    )
    result = _collect(
        collector,
        ws,
        store,
        command_ids=("cap_cmd", "after_cap"),
    )
    assert [obs.command_id for obs in result.snapshot.commands] == [
        "cap_cmd",
        "after_cap",
    ]
    assert [obs.outcome for obs in result.snapshot.commands] == [
        "output_limit",
        "success",
    ]
    assert result.snapshot.complete is False
    assert result.snapshot.all_passed is False
    assert result.evidence.status == "truncated"


def test_raw_artifact_bytes_counts_and_empty_artifact(tmp_path):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    spec = _spec(
        command_id="bytes_ok",
        argv=(sys.executable, "-c", SUCCESS_SCRIPT),
    )
    collector = _collector(spec)
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    stdout_raw = b"hello out\n"
    stderr_raw = b"hello err\n"
    assert observation.stdout_artifact_digest == _sha256(stdout_raw)
    assert observation.stderr_artifact_digest == _sha256(stderr_raw)
    assert observation.stdout_bytes == len(stdout_raw)
    assert observation.stderr_bytes == len(stderr_raw)
    assert store.get_bytes(observation.stdout_artifact_digest) == stdout_raw
    assert store.get_bytes(observation.stderr_artifact_digest) == stderr_raw


def test_empty_stream_artifacts_are_stored(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    store = ArtifactStore(tmp_path / "store")
    fake = _FakeProcess(stdout=(), stderr=(), returncode=0)
    _install_fake_popen(monkeypatch, [fake])
    collector = _collector(_spec())
    result = _collect(collector, ws, store)
    observation = result.snapshot.commands[0]
    empty = b""
    assert observation.stdout_bytes == 0
    assert observation.stderr_bytes == 0
    assert observation.stdout_artifact_digest == _sha256(empty)
    assert observation.stderr_artifact_digest == _sha256(empty)
    assert store.get_bytes(observation.stdout_artifact_digest) == empty
    assert store.get_bytes(observation.stderr_artifact_digest) == empty


def test_source_audit_no_forbidden_primitives_or_environment_passthrough():
    source = inspect.getsource(commands_module)
    for fragment in (
        "subprocess.run",
        "communicate(",
        "create_subprocess_shell",
        "import socket",
        "import httpx",
        "import openai",
        "git",
    ):
        assert fragment not in source, fragment
    assert source.count("os.environ") == 1
