"""Focused contract tests for assurance.manifest (V2-P2-06)."""

import ast
from collections import UserList
import hashlib
import inspect
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    ArtifactStore,
    Evidence,
    EvidenceManifest,
    EvidenceManifestArtifactError,
    EvidenceManifestBuilder,
    EvidenceManifestEntry,
    EvidenceManifestError,
    EvidenceManifestInput,
    EvidenceManifestInputError,
    EvidenceManifestPersistenceError,
    EvidenceManifestResult,
    EvidenceManifestSubjectError,
)
from assurance import manifest as manifest_module


SUBJECT = "sha256:" + "0" * 64
OTHER_SUBJECT = "sha256:" + "1" * 64
FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
FIXED_TIME_ISO = "2026-08-25T08:00:00+00:00"
LATER_TIME = datetime(2026, 8, 25, 8, 5, 0, tzinfo=timezone.utc)
LATER_TIME_ISO = "2026-08-25T08:05:00+00:00"
EARLIER_TIME = datetime(2026, 8, 25, 7, 55, 0, tzinfo=timezone.utc)
EARLIER_TIME_ISO = "2026-08-25T07:55:00+00:00"

SECRET_MARKER = "S3CR3T-TOKEN-7f3a"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


DIGEST_A = _sha256(b"artifact-a")
DIGEST_B = _sha256(b"artifact-b")
DIGEST_OTHER = _sha256(b"other")
REF_BYTES_A = b"artifact-a"
REF_BYTES_B = b"artifact-b"

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
    "IntakeDocument",
    "IntakeNotice",
    "IntakeSnapshot",
    "IntakeResult",
    "TaskPolicyCollector",
    "IntakeCollectionError",
    "IntakePathError",
    "IntakeFormatError",
    "IntakeChangedError",
    "CommandSpec",
    "CommandObservation",
    "CommandBatchSnapshot",
    "CommandBatchResult",
    "DeterministicCommandCollector",
    "CommandCollectionError",
    "CommandSpecError",
    "CommandLaunchError",
    "CommandExecutionError",
    "GenericEvidenceImporter",
    "GenericEvidenceEnvelope",
    "GenericEvidenceImportReceipt",
    "GenericEvidenceImportResult",
    "SignatureMetadata",
    "GenericEvidenceImportError",
    "GenericEvidencePayloadError",
    "GenericEvidenceSubjectMismatch",
    "GenericEvidenceArtifactError",
    "AuthorAgentReceiptCost",
    "GenericAuthorReceiptEnvelope",
    "AuthorAgentReceipt",
    "AuthorAgentReceiptResult",
    "AuthorAgentReceiptNormalizer",
    "AuthorAgentReceiptError",
    "AuthorAgentReceiptPayloadError",
    "AuthorAgentReceiptSubjectMismatch",
    "AuthorAgentReceiptArtifactError",
}

NEW_PUBLIC_NAMES = {
    "EvidenceManifestInput",
    "EvidenceManifestEntry",
    "EvidenceManifest",
    "EvidenceManifestResult",
    "EvidenceManifestBuilder",
    "EvidenceManifestError",
    "EvidenceManifestInputError",
    "EvidenceManifestSubjectError",
    "EvidenceManifestArtifactError",
    "EvidenceManifestPersistenceError",
}

ALL_MODELS = (
    EvidenceManifestInput,
    EvidenceManifestEntry,
    EvidenceManifest,
    EvidenceManifestResult,
)


def _evidence(**overrides) -> dict:
    data = {
        "schema_version": "v1",
        "evidence_id": "ev-1",
        "subject_digest": SUBJECT,
        "kind": "test_kind",
        "producer": "test_producer",
        "artifact_digest": DIGEST_A,
        "source_ref": "command_batch:sha256:" + "a" * 64,
        "trace_id": None,
        "status": "success",
        "trust_level": "observed",
        "collected_at": FIXED_TIME_ISO,
    }
    data.update(overrides)
    return data


def _input_dict(**overrides) -> dict:
    data = {
        "schema_version": "v1",
        "evidence": _evidence(),
        "fresh_until": None,
        "redaction_status": "not_applicable",
    }
    data.update(overrides)
    return data


def _entry_dict(**overrides) -> dict:
    data = {
        "schema_version": "v1",
        "evidence_id": "ev-1",
        "kind": "test_kind",
        "trust_level": "observed",
        "producer": "test_producer",
        "subject_digest": SUBJECT,
        "artifact_digest": DIGEST_A,
        "source_ref": "command_batch:sha256:" + "a" * 64,
        "status": "success",
        "collected_at": FIXED_TIME_ISO,
        "fresh_until": None,
        "freshness": "unknown",
        "redaction_status": "not_applicable",
    }
    data.update(overrides)
    return data


def _manifest_payload(entries=None, **overrides) -> dict:
    if entries is None:
        entries = (_entry_dict(),)
    if entries and not isinstance(entries[0], EvidenceManifestEntry):
        entries = tuple(EvidenceManifestEntry.model_validate(e) for e in entries)
    incomplete = any(
        e.status in ("error", "timeout", "cancelled", "truncated")
        for e in entries
    )
    stale = any(e.freshness == "stale" for e in entries)
    unknown = any(e.freshness == "unknown" for e in entries)
    unredacted = any(
        e.redaction_status == "contains_unredacted_content" for e in entries
    )
    unassessed = any(e.redaction_status == "not_assessed" for e in entries)
    has_gaps = incomplete or stale or unknown or unredacted or unassessed
    raw_evaluated = overrides.get("evaluated_at", FIXED_TIME)
    provisional_evaluated = (
        datetime.fromisoformat(raw_evaluated)
        if isinstance(raw_evaluated, str)
        else raw_evaluated
    )
    provisional = EvidenceManifest.model_construct(
        schema_version="v1",
        manifest_id="em_" + "0" * 32,
        subject_digest=SUBJECT,
        evaluated_at=provisional_evaluated,
        entries=entries,
        evidence_count=len(entries),
        completeness_status="has_gaps" if has_gaps else "complete",
        has_incomplete_evidence=incomplete,
        has_stale_evidence=stale,
        has_unknown_freshness=unknown,
        has_unredacted_content=unredacted,
        has_unassessed_redaction=unassessed,
        canonical_digest="sha256:" + "0" * 64,
        artifact_digest="sha256:" + "0" * 64,
    )
    body = manifest_module._canonical_body(provisional)
    digest = _sha256(body)
    manifest_id = "em_" + hashlib.sha256(
        (SUBJECT + digest).encode("utf-8")
    ).hexdigest()[:32]
    data = {
        "schema_version": "v1",
        "manifest_id": manifest_id,
        "subject_digest": SUBJECT,
        "evaluated_at": FIXED_TIME_ISO,
        "entries": [e.model_dump(mode="json") for e in entries],
        "evidence_count": len(entries),
        "completeness_status": "has_gaps" if has_gaps else "complete",
        "has_incomplete_evidence": incomplete,
        "has_stale_evidence": stale,
        "has_unknown_freshness": unknown,
        "has_unredacted_content": unredacted,
        "has_unassessed_redaction": unassessed,
        "canonical_digest": digest,
        "artifact_digest": digest,
    }
    data.update(overrides)
    return data


def _result_payload(manifest_data=None, **evidence_overrides) -> dict:
    manifest = EvidenceManifest.model_validate(
        _manifest_payload() if manifest_data is None else manifest_data
    )
    evidence_data = _evidence(
        evidence_id=manifest_module._evidence_id(manifest),
        subject_digest=manifest.subject_digest,
        kind="evidence_manifest",
        producer="builder.evidence_manifest",
        artifact_digest=manifest.artifact_digest,
        source_ref=f"evidence_manifest:{manifest.manifest_id}",
        status=(
            "success"
            if manifest.completeness_status == "complete"
            else "truncated"
        ),
        trust_level="deterministic",
        collected_at=manifest.evaluated_at,
    )
    evidence_data.update(evidence_overrides)
    return {
        "schema_version": "v1",
        "manifest": manifest.model_dump(mode="json"),
        "evidence": evidence_data,
    }


def _raw_evaluated_payload(raw) -> dict:
    """Build a digest-consistent manifest payload for a raw evaluated_at."""
    parsed = (
        datetime.fromtimestamp(float(raw), tz=timezone.utc)
        if isinstance(raw, str)
        else datetime.fromtimestamp(raw, tz=timezone.utc)
    )
    payload = _manifest_payload(
        entries=(_entry_dict(collected_at=parsed.isoformat()),),
        evaluated_at=parsed.isoformat(),
    )
    payload["evaluated_at"] = raw
    return payload


def _raw_result_payload(raw) -> dict:
    """Build a binding-consistent result payload with a raw evidence datetime."""
    parsed = datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return _result_payload(
        manifest_data=_manifest_payload(
            entries=(_entry_dict(collected_at=parsed.isoformat()),),
            evaluated_at=parsed.isoformat(),
        ),
        collected_at=raw,
    )


def _input_obj(
    evidence_id="ev-1",
    *,
    fresh_until=None,
    redaction_status="not_applicable",
    validate_evidence=True,
    **evidence_overrides,
) -> EvidenceManifestInput:
    evidence_data = _evidence(
        evidence_id=evidence_id,
        **evidence_overrides,
    )
    return EvidenceManifestInput(
        schema_version="v1",
        evidence=(
            Evidence.model_validate(evidence_data)
            if validate_evidence
            else Evidence.model_construct(**evidence_data)
        ),
        fresh_until=fresh_until,
        redaction_status=redaction_status,
    )


def _inputs(n, *, start=0, **overrides) -> tuple[EvidenceManifestInput, ...]:
    return tuple(
        _input_obj(
            f"ev-{start + index:03d}",
            **overrides,
        )
        for index in range(n)
    )


def _store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


def _file_set(store: ArtifactStore) -> set[str]:
    if not store.root.exists():
        return set()
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def _populated_store(tmp_path) -> ArtifactStore:
    store = _store(tmp_path)
    store.put_bytes(REF_BYTES_A)
    store.put_bytes(REF_BYTES_B)
    return store


def _build(
    tmp_path,
    items=None,
    *,
    subject_digest=SUBJECT,
    evaluated_at=FIXED_TIME,
    artifact_store=None,
):
    if items is None:
        items = _inputs(1)
    return EvidenceManifestBuilder.build(
        items,
        subject_digest=subject_digest,
        evaluated_at=evaluated_at,
        artifact_store=(
            _populated_store(tmp_path)
            if artifact_store is None
            else artifact_store
        ),
    )


def test_public_imports():
    assert assurance.EvidenceManifest is manifest_module.EvidenceManifest
    assert assurance.EvidenceManifestBuilder is manifest_module.EvidenceManifestBuilder


def test_package_exports_preserve_prior_names_and_add_manifest_api():
    assert PRIOR_PUBLIC_NAMES | NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.EvidenceManifest is manifest_module.EvidenceManifest


def test_error_hierarchy_is_simple():
    assert issubclass(EvidenceManifestError, Exception)
    assert issubclass(EvidenceManifestInputError, EvidenceManifestError)
    assert issubclass(EvidenceManifestSubjectError, EvidenceManifestError)
    assert issubclass(EvidenceManifestArtifactError, EvidenceManifestError)
    assert issubclass(
        EvidenceManifestPersistenceError, EvidenceManifestError
    )


def test_builder_has_only_build_public_method():
    public_methods = sorted(
        name
        for name in vars(EvidenceManifestBuilder)
        if not name.startswith("_")
    )
    assert public_methods == ["build"]


def test_all_models_are_v1_frozen_extra_forbid():
    for model in ALL_MODELS:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_input_field_order_v1_only_and_roundtrip():
    assert list(EvidenceManifestInput.model_fields) == [
        "schema_version",
        "evidence",
        "fresh_until",
        "redaction_status",
    ]
    model = EvidenceManifestInput.model_validate(_input_dict())
    restored = EvidenceManifestInput.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        EvidenceManifestInput.model_validate(
            _input_dict(schema_version="v2")
        )
    with pytest.raises(ValidationError):
        EvidenceManifestInput.model_validate(
            {**_input_dict(), "freshness": "unknown"}
        )
    with pytest.raises(ValidationError):
        EvidenceManifestInput.model_validate(
            {**_input_dict(), "extra": 1}
        )


def test_input_freshness_relation_redaction_and_strict_time():
    EvidenceManifestInput.model_validate(
        _input_dict(fresh_until=LATER_TIME_ISO)
    )
    for overrides in (
        {"fresh_until": EARLIER_TIME_ISO},
        {"fresh_until": "2026-08-25T08:00:00"},
        {"fresh_until": "not-a-date"},
        {"redaction_status": "unknown"},
        {"redaction_status": "redacted"},
        {"redaction_status": 1},
    ):
        with pytest.raises(ValidationError):
            EvidenceManifestInput.model_validate(_input_dict(**overrides))


@pytest.mark.parametrize(
    "bad",
    (
        True,
        1,
        1.0,
        "1",
        "-1",
        "+1",
        "1.5",
        ".5",
        "1.",
        "1e3",
        "1E-3",
        "0",
        "-0",
    ),
)
def test_input_rejects_numeric_evidence_collected_at(bad):
    data = _input_dict()
    data["evidence"]["collected_at"] = bad
    with pytest.raises(ValidationError):
        EvidenceManifestInput.model_validate(data)


@pytest.mark.parametrize(
    "bad",
    (
        True,
        False,
        1,
        1.0,
        -1,
        "1",
        "1.0",
        "1e3",
        " 1 ",
    ),
)
def test_input_rejects_numeric_fresh_until(bad):
    parsed = datetime.fromtimestamp(float(bad), tz=timezone.utc).isoformat()
    with pytest.raises(ValidationError):
        EvidenceManifestInput.model_validate(
            _input_dict(
                evidence={**_evidence(), "collected_at": parsed},
                fresh_until=bad,
            )
        )


def test_input_evidence_datetime_accepts_aware_and_rejects_naive():
    for good in (FIXED_TIME, FIXED_TIME_ISO):
        model = EvidenceManifestInput.model_validate(
            _input_dict(evidence={**_evidence(), "collected_at": good})
        )
        assert model.evidence.collected_at == FIXED_TIME
    restored = EvidenceManifestInput.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    for bad in (
        datetime(2026, 8, 25, 8, 0),
        "2026-08-25T08:00:00",
    ):
        with pytest.raises(ValidationError):
            EvidenceManifestInput.model_validate(
                _input_dict(evidence={**_evidence(), "collected_at": bad})
            )


def test_input_preserves_validated_evidence_object():
    evidence = Evidence.model_validate(_evidence())
    model = EvidenceManifestInput.model_validate(
        {
            "schema_version": "v1",
            "evidence": evidence,
            "fresh_until": None,
            "redaction_status": "not_applicable",
        }
    )
    assert model.evidence is evidence


def test_entry_field_order_v1_only_and_roundtrip():
    assert list(EvidenceManifestEntry.model_fields) == [
        "schema_version",
        "evidence_id",
        "kind",
        "trust_level",
        "producer",
        "subject_digest",
        "artifact_digest",
        "source_ref",
        "status",
        "collected_at",
        "fresh_until",
        "freshness",
        "redaction_status",
    ]
    model = EvidenceManifestEntry.model_validate(_entry_dict())
    restored = EvidenceManifestEntry.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        EvidenceManifestEntry.model_validate(_entry_dict(schema_version="v2"))
    with pytest.raises(ValidationError):
        EvidenceManifestEntry.model_validate(
            {**_entry_dict(), "trace_id": None}
        )
    with pytest.raises(ValidationError):
        EvidenceManifestEntry.model_validate(
            {**_entry_dict(), "extra": 1}
        )


def test_entry_digest_text_source_freshness_rules():
    for source_ref in (
        "command_batch:sha256:" + "b" * 64,
        "generic_import:sha256:" + "c" * 64,
        "intake_documents:sha256:" + "d" * 64,
        "author_agent_receipt:sha256:" + "e" * 64,
        "git:sha256:" + "f" * 64,
        "git_snapshot:https://github.com/org/repo:<base>:<head>:base_to_worktree",
    ):
        EvidenceManifestEntry.model_validate(
            _entry_dict(source_ref=source_ref)
        )
    for overrides in (
        {"evidence_id": ""},
        {"evidence_id": "   "},
        {"evidence_id": "x\x00y"},
        {"evidence_id": "e" * 257},
        {"kind": ""},
        {"kind": "  "},
        {"kind": "x\x00y"},
        {"kind": "k" * 257},
        {"trust_level": "unknown"},
        {"trust_level": "verified"},
        {"producer": ""},
        {"producer": "   "},
        {"producer": "x\x00y"},
        {"producer": "p" * 257},
        {"subject_digest": SUBJECT.upper()},
        {"subject_digest": SUBJECT[:-1]},
        {"subject_digest": 1},
        {"artifact_digest": "sha256:XYZ"},
        {"status": "unknown"},
        {"collected_at": "2026-08-25T08:00:00"},
        {"collected_at": "not-a-date"},
        {"fresh_until": "2026-08-25T08:05:00"},
        {"fresh_until": "not-a-date"},
        {"freshness": "unknown", "fresh_until": LATER_TIME_ISO},
        {"freshness": "fresh", "fresh_until": None},
        {"freshness": "stale", "fresh_until": None},
        {"freshness": "maybe"},
        {
            "fresh_until": EARLIER_TIME_ISO,
            "freshness": "stale",
        },
        {"redaction_status": "unknown"},
        {"redaction_status": "not_appraised"},
    ):
        with pytest.raises(ValidationError):
            EvidenceManifestEntry.model_validate(_entry_dict(**overrides))


@pytest.mark.parametrize(
    "bad",
    (
        True,
        1,
        1.0,
        "1",
        "-1",
        "+1",
        "1.5",
        ".5",
        "1.",
        "1e3",
        "1E-3",
        "0",
        "-0",
    ),
)
def test_entry_collected_at_rejects_numeric_datetime(bad):
    with pytest.raises(ValidationError):
        EvidenceManifestEntry.model_validate(
            _entry_dict(collected_at=bad)
        )


@pytest.mark.parametrize(
    "bad",
    (
        True,
        False,
        1,
        1.0,
        -1,
        "1",
        "1.0",
        "1e3",
        " 1 ",
    ),
)
def test_entry_rejects_numeric_fresh_until(bad):
    parsed = datetime.fromtimestamp(float(bad), tz=timezone.utc).isoformat()
    with pytest.raises(ValidationError):
        EvidenceManifestEntry.model_validate(
            _entry_dict(
                collected_at=parsed,
                fresh_until=bad,
                freshness="fresh",
            )
        )


def test_entry_collected_at_accepts_aware_and_rejects_naive():
    for good in (FIXED_TIME, FIXED_TIME_ISO):
        entry = EvidenceManifestEntry.model_validate(
            _entry_dict(collected_at=good)
        )
        assert entry.collected_at == FIXED_TIME
    for bad in (
        datetime(2026, 8, 25, 8, 0),
        "2026-08-25T08:00:00",
    ):
        with pytest.raises(ValidationError):
            EvidenceManifestEntry.model_validate(
                _entry_dict(collected_at=bad)
            )


def test_entry_source_ref_local_and_control_fail_closed():
    for source_ref in (
        "git_snapshot:local:/Users/junjieli/secret",
        "x:file:///tmp/secret",
        "repo:~/secret",
        "git_snapshot:C:/secret",
        "git_snapshot:\\\\server\\share",
        "/Users/junjieli/x",
        "/home/user/x",
        "/etc/passwd",
        "file:///tmp/x",
        "~/x",
        "~user/x",
        "C:/x",
        "C:\\x",
        "\\\\server\\share",
        "//server/share",
        "",
        "   ",
        "a\x00b",
        "a\nb",
        "a\rb",
        "r" * 1025,
    ):
        with pytest.raises(ValidationError):
            EvidenceManifestEntry.model_validate(
                _entry_dict(source_ref=source_ref)
            )


def test_manifest_field_order_v1_only_and_roundtrip():
    assert list(EvidenceManifest.model_fields) == [
        "schema_version",
        "manifest_id",
        "subject_digest",
        "evaluated_at",
        "entries",
        "evidence_count",
        "completeness_status",
        "has_incomplete_evidence",
        "has_stale_evidence",
        "has_unknown_freshness",
        "has_unredacted_content",
        "has_unassessed_redaction",
        "canonical_digest",
        "artifact_digest",
    ]
    model = EvidenceManifest.model_validate(_manifest_payload())
    restored = EvidenceManifest.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    assert type(model.entries) is tuple
    with pytest.raises(ValidationError):
        model.entries[0].evidence_id = "changed"
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(
            _manifest_payload(schema_version="v2")
        )
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(
            {**_manifest_payload(), "extra": 1}
        )


def test_manifest_deep_immutable_entries_and_strict_flags():
    model = EvidenceManifest.model_validate(_manifest_payload())
    assert type(model.entries) is tuple
    for overrides in (
        {"evidence_count": True},
        {"evidence_count": 1.5},
        {"has_incomplete_evidence": 1},
        {"has_stale_evidence": "yes"},
        {"completeness_status": "partial"},
        {"manifest_id": "ar_" + "0" * 32},
        {"manifest_id": "em_" + "Z" * 32},
        {"subject_digest": OTHER_SUBJECT},
        {"canonical_digest": "sha256:XYZ"},
        {"artifact_digest": "sha256:XYZ"},
        {"evaluated_at": "2026-08-25T08:00:00"},
        {"entries": []},
    ):
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(
                _manifest_payload(**overrides)
            )


@pytest.mark.parametrize(
    "bad",
    (
        True,
        1,
        1.0,
        "1",
        "-1",
        "+1",
        "1.5",
        ".5",
        "1.",
        "1e3",
        "1E-3",
        "0",
        "-0",
    ),
)
def test_manifest_rejects_numeric_evaluated_at(bad):
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(_raw_evaluated_payload(bad))


def test_manifest_evaluated_at_accepts_aware_and_rejects_naive():
    assert (
        EvidenceManifest.model_validate(_manifest_payload()).evaluated_at
        == FIXED_TIME
    )
    for bad in (
        datetime(2026, 8, 25, 8, 0),
        "2026-08-25T08:00:00",
    ):
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(
                _manifest_payload(evaluated_at=bad)
            )


def test_manifest_rejects_unsorted_duplicate_count_subject_and_time():
    unsorted = _manifest_payload(
        entries=(
            _entry_dict(evidence_id="ev-2", status="failure"),
            _entry_dict(evidence_id="ev-1"),
        )
    )
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(unsorted)

    duplicate = _manifest_payload(
        entries=(
            _entry_dict(evidence_id="ev-1"),
            _entry_dict(evidence_id="ev-1"),
        )
    )
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(duplicate)

    for overrides in (
        {"evidence_count": 2},
        {"evidence_count": 0},
        {
            "entries": (
                _entry_dict(subject_digest=OTHER_SUBJECT),
            )
        },
        {"evaluated_at": EARLIER_TIME_ISO},
    ):
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(_manifest_payload(**overrides))


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        ("failure", "complete"),
        ("error", "has_gaps"),
        ("timeout", "has_gaps"),
        ("cancelled", "has_gaps"),
        ("truncated", "has_gaps"),
        ("success", "complete"),
    ],
)
def test_manifest_incomplete_status_semantics(status, expected_status):
    payload = _manifest_payload(
        entries=(
            _entry_dict(
                status=status,
                fresh_until=LATER_TIME_ISO,
                freshness="fresh",
                redaction_status="not_applicable",
            ),
        )
    )
    model = EvidenceManifest.model_validate(payload)
    assert model.completeness_status == expected_status
    assert model.has_incomplete_evidence == (
        status in ("error", "timeout", "cancelled", "truncated")
    )


def test_manifest_recomputes_every_summary_flag():
    cases = (
        {
            "entry": _entry_dict(
                fresh_until=LATER_TIME_ISO, freshness="fresh"
            ),
            "flag": "has_stale_evidence",
        },
        {
            "entry": _entry_dict(
                fresh_until="2026-08-25T08:02:00+00:00",
                freshness="stale",
            ),
            "flag": "has_stale_evidence",
        },
        {
            "entry": _entry_dict(
                redaction_status="contains_unredacted_content"
            ),
            "flag": "has_unredacted_content",
        },
        {
            "entry": _entry_dict(redaction_status="not_assessed"),
            "flag": "has_unassessed_redaction",
        },
    )
    for case in cases:
        payload = _manifest_payload(entries=(case["entry"],))
        payload[case["flag"]] = not payload[case["flag"]]
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(payload)
        forged_status = _manifest_payload(entries=(case["entry"],))
        forged_status["completeness_status"] = (
            "complete"
            if forged_status["completeness_status"] == "has_gaps"
            else "has_gaps"
        )
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(forged_status)


def test_manifest_recomputes_freshness_from_evaluated_at():
    for fresh_until, expected in (
        (None, "unknown"),
        (LATER_TIME_ISO, "fresh"),
        ("2026-08-25T08:02:00+00:00", "stale"),
    ):
        overrides = {}
        if expected == "stale":
            overrides["evaluated_at"] = LATER_TIME_ISO
        payload = _manifest_payload(
            entries=(
                _entry_dict(
                    fresh_until=fresh_until,
                    freshness=expected,
                ),
            ),
            **overrides,
        )
        assert EvidenceManifest.model_validate(payload).entries[0].freshness == expected
    forged = _manifest_payload(
        entries=(_entry_dict(fresh_until=LATER_TIME_ISO, freshness="stale"),)
    )
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(forged)


def test_manifest_rejects_forged_digests_and_manifest_id():
    for field in ("canonical_digest", "artifact_digest"):
        with pytest.raises(ValidationError):
            EvidenceManifest.model_validate(
                _manifest_payload(**{field: DIGEST_OTHER})
            )
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(
            _manifest_payload(manifest_id="em_" + "f" * 32)
        )


def test_manifest_canonical_and_id_formulas():
    payload = _manifest_payload()
    model = EvidenceManifest.model_validate(payload)
    body = manifest_module._canonical_body(model)
    assert body == json.dumps(
        {
            key: value
            for key, value in model.model_dump(mode="json").items()
            if key
            not in ("manifest_id", "canonical_digest", "artifact_digest")
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert model.canonical_digest == _sha256(body)
    assert model.artifact_digest == model.canonical_digest
    assert model.manifest_id == "em_" + hashlib.sha256(
        (model.subject_digest + model.canonical_digest).encode("utf-8")
    ).hexdigest()[:32]


def test_result_field_order_v1_only_and_roundtrip():
    assert list(EvidenceManifestResult.model_fields) == [
        "schema_version",
        "manifest",
        "evidence",
    ]
    model = EvidenceManifestResult.model_validate(_result_payload())
    restored = EvidenceManifestResult.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        EvidenceManifestResult.model_validate(
            {**_result_payload(), "schema_version": "v2"}
        )
    with pytest.raises(ValidationError):
        EvidenceManifestResult.model_validate(
            {**_result_payload(), "extra": 1}
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["evidence"].update(kind="other_kind"),
        lambda d: d["evidence"].update(producer="other.producer"),
        lambda d: d["evidence"].update(subject_digest=OTHER_SUBJECT),
        lambda d: d["evidence"].update(artifact_digest=DIGEST_OTHER),
        lambda d: d["evidence"].update(
            source_ref="evidence_manifest:" + "0" * 32
        ),
        lambda d: d["evidence"].update(collected_at=LATER_TIME_ISO),
        lambda d: d["evidence"].update(trust_level="observed"),
        lambda d: d["evidence"].update(trace_id="forged"),
        lambda d: d["evidence"].update(status="failure"),
        lambda d: d["evidence"].update(
            evidence_id="ev_manifest_" + "f" * 32
        ),
    ],
)
def test_result_rejects_forged_cross_bindings(mutate):
    data = _result_payload()
    mutate(data)
    with pytest.raises(ValidationError):
        EvidenceManifestResult.model_validate(data)


@pytest.mark.parametrize(
    "bad",
    (
        True,
        False,
        1,
        1.0,
        -1,
        "1",
        "1.0",
        "1e3",
        " 1 ",
    ),
)
def test_result_rejects_numeric_evidence_collected_at(bad):
    with pytest.raises(ValidationError):
        EvidenceManifestResult.model_validate(_raw_result_payload(bad))


def test_result_rejects_recursive_manifest_entry():
    forged = _manifest_payload(entries=(_entry_dict(kind="evidence_manifest"),))
    with pytest.raises(ValidationError):
        EvidenceManifestResult.model_validate(_result_payload(forged))


def test_build_accepts_list_tuple_userlist_and_rejects_containers(tmp_path):
    items = _inputs(1, artifact_digest=DIGEST_A)
    for container in (
        list(items),
        tuple(items),
        UserList(items),
    ):
        result = _build(tmp_path, container)
        assert result.evidence.evidence_id.startswith("ev_manifest_")

    generator = (item for item in items)
    for bad in (
        "abc",
        b"abc",
        bytearray(b"abc"),
        memoryview(b"abc"),
        {"a": 1},
        {"ev-001"},
        frozenset({"ev-001"}),
        generator,
        iter(items),
        None,
        123,
    ):
        with pytest.raises(EvidenceManifestInputError) as exc:
            EvidenceManifestBuilder.build(
                bad,
                subject_digest=SUBJECT,
                evaluated_at=FIXED_TIME,
                artifact_store=_store(tmp_path),
            )
        assert str(exc.value) == "invalid evidence manifest input"
        assert exc.value.__cause__ is None


def test_build_count_boundaries(tmp_path):
    result = _build(
        tmp_path,
        _inputs(1, artifact_digest=DIGEST_A),
    )
    assert result.manifest.evidence_count == 1
    many = _inputs(256, artifact_digest=DIGEST_A)
    result = _build(tmp_path, many)
    assert result.manifest.evidence_count == 256
    for bad in (_inputs(0), _inputs(257, artifact_digest=DIGEST_A)):
        with pytest.raises(EvidenceManifestInputError):
            EvidenceManifestBuilder.build(
                bad,
                subject_digest=SUBJECT,
                evaluated_at=FIXED_TIME,
                artifact_store=_store(tmp_path),
            )


def test_build_rejects_bad_subject_evaluated_at_store_types(tmp_path):
    items = _inputs(1, artifact_digest=DIGEST_A)
    for bad_subject in (
        "sha256:XYZ",
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 65,
        "SHA256:" + "0" * 64,
        "sha256:" + "A" * 64,
        "",
        None,
        123,
    ):
        with pytest.raises(EvidenceManifestInputError):
            _build(tmp_path, items, subject_digest=bad_subject)
    for bad_time in (
        "2026-08-25T08:00:00+00:00",
        FIXED_TIME.replace(tzinfo=None),
        None,
    ):
        with pytest.raises(EvidenceManifestInputError):
            _build(tmp_path, items, evaluated_at=bad_time)

    class StoreSubclass(ArtifactStore):
        pass

    for bad_store in (
        None,
        {},
        "store",
        StoreSubclass(tmp_path / "sub"),
    ):
        with pytest.raises(EvidenceManifestInputError):
            EvidenceManifestBuilder.build(
                items,
                subject_digest=SUBJECT,
                evaluated_at=FIXED_TIME,
                artifact_store=bad_store,
            )


def test_build_rejects_non_exact_item_types(tmp_path):
    item = _input_obj(artifact_digest=DIGEST_A)

    class InputSubclass(EvidenceManifestInput):
        pass

    for bad in (
        item.evidence,
        item.model_dump(mode="json"),
        None,
        InputSubclass.model_validate(item.model_dump(mode="json")),
    ):
        with pytest.raises(EvidenceManifestInputError):
            _build(tmp_path, [bad])


def test_build_subject_mismatch_duplicate_ids_and_evaluated_ordering(tmp_path):
    mismatch = _inputs(1, artifact_digest=DIGEST_A, subject_digest=OTHER_SUBJECT)
    with pytest.raises(EvidenceManifestSubjectError) as exc:
        _build(tmp_path, mismatch)
    assert str(exc.value) == "evidence manifest subject digest mismatch"
    assert exc.value.__cause__ is None

    duplicate = (
        _input_obj("ev-1", artifact_digest=DIGEST_A),
        _input_obj("ev-1", artifact_digest=DIGEST_B),
    )
    with pytest.raises(EvidenceManifestInputError):
        _build(tmp_path, duplicate)

    late = _inputs(1, artifact_digest=DIGEST_A, collected_at=LATER_TIME_ISO)
    with pytest.raises(EvidenceManifestInputError):
        _build(tmp_path, late, evaluated_at=FIXED_TIME)


def test_build_sorts_entries_and_permutation_invariance(tmp_path):
    first = _inputs(2, artifact_digest=DIGEST_A)
    result = _build(tmp_path, [first[1], first[0]])
    assert result.manifest.entries[0].evidence_id == "ev-000"
    assert result.manifest.entries[1].evidence_id == "ev-001"

    store_a = _populated_store(tmp_path)
    result_a = EvidenceManifestBuilder.build(
        [first[1], first[0]],
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store_a,
    )
    store_b = _populated_store(tmp_path)
    result_b = EvidenceManifestBuilder.build(
        [first[0], first[1]],
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store_b,
    )
    assert result_a == result_b
    assert result_a.model_dump_json() == result_b.model_dump_json()


def test_build_freshness_mapping(tmp_path):
    items = (
        _input_obj("ev-000", artifact_digest=DIGEST_A, fresh_until=None),
        _input_obj(
            "ev-001",
            artifact_digest=DIGEST_A,
            fresh_until=LATER_TIME_ISO,
        ),
        _input_obj(
            "ev-002",
            artifact_digest=DIGEST_A,
            fresh_until="2026-08-25T08:02:00+00:00",
        ),
    )
    result = _build(tmp_path, items, evaluated_at=LATER_TIME)
    entries = result.manifest.entries
    assert entries[0].freshness == "unknown"
    assert entries[1].freshness == "fresh"
    assert entries[2].freshness == "stale"
    assert result.manifest.has_unknown_freshness is True
    assert result.manifest.has_stale_evidence is True


def test_build_source_ref_rules(tmp_path):
    for source_ref in (
        "command_batch:sha256:" + "b" * 64,
        "generic_import:sha256:" + "c" * 64,
        "intake_documents:sha256:" + "d" * 64,
        "author_agent_receipt:sha256:" + "e" * 64,
        "git:sha256:" + "f" * 64,
        "git_snapshot:https://github.com/org/repo:<base>:<head>:base_to_worktree",
    ):
        result = _build(
            tmp_path,
            _inputs(1, artifact_digest=DIGEST_A, source_ref=source_ref),
        )
        assert result.manifest.entries[0].source_ref == source_ref
    for source_ref in (
        "git_snapshot:local:/Users/junjieli/secret",
        "x:file:///tmp/secret",
        "repo:~/secret",
        "git_snapshot:C:/secret",
        "git_snapshot:\\\\server\\share",
    ):
        store = ArtifactStore(tmp_path / "invalid-source-store")
        store.put_bytes(REF_BYTES_A)
        store.put_bytes(REF_BYTES_B)
        with pytest.raises(EvidenceManifestInputError) as exc:
            EvidenceManifestBuilder.build(
                _inputs(
                    1,
                    artifact_digest=DIGEST_A,
                    source_ref=source_ref,
                ),
                subject_digest=SUBJECT,
                evaluated_at=FIXED_TIME,
                artifact_store=store,
            )
        assert str(exc.value) == "invalid evidence manifest input"
        assert exc.value.__cause__ is None
        assert _file_set(store) == {
            "sha256/" + DIGEST_A[7:][:2] + "/" + DIGEST_A[7:][2:],
            "sha256/" + DIGEST_B[7:][:2] + "/" + DIGEST_B[7:][2:],
        }
    for source_ref in (
        "/Users/junjieli/x",
        "/home/user/x",
        "/etc/passwd",
        "file:///tmp/x",
        "~/x",
        "~user/x",
        "C:/x",
        "C:\\x",
        "\\\\server\\share",
        "//server/share",
        "",
        "   ",
        "a\x00b",
        "a\nb",
        "a\rb",
        "r" * 1025,
    ):
        store = ArtifactStore(tmp_path / "invalid-source-store")
        store.put_bytes(REF_BYTES_A)
        store.put_bytes(REF_BYTES_B)
        with pytest.raises(EvidenceManifestInputError) as exc:
            EvidenceManifestBuilder.build(
                _inputs(
                    1,
                    artifact_digest=DIGEST_A,
                    source_ref=source_ref,
                    validate_evidence=False,
                ),
                subject_digest=SUBJECT,
                evaluated_at=FIXED_TIME,
                artifact_store=store,
            )
        assert str(exc.value) == "invalid evidence manifest input"
        assert exc.value.__cause__ is None
        assert _file_set(store) == {
            "sha256/" + DIGEST_A[7:][:2] + "/" + DIGEST_A[7:][2:],
            "sha256/" + DIGEST_B[7:][:2] + "/" + DIGEST_B[7:][2:],
        }


def test_build_result_bindings_and_evidence_formula(tmp_path):
    result = _build(
        tmp_path,
        _inputs(
            1,
            artifact_digest=DIGEST_A,
            fresh_until=LATER_TIME_ISO,
            redaction_status="not_applicable",
        ),
    )
    manifest = result.manifest
    evidence = result.evidence
    assert evidence.kind == "evidence_manifest"
    assert evidence.producer == "builder.evidence_manifest"
    assert evidence.subject_digest == manifest.subject_digest
    assert evidence.artifact_digest == manifest.artifact_digest
    assert evidence.source_ref == f"evidence_manifest:{manifest.manifest_id}"
    assert evidence.collected_at == manifest.evaluated_at
    assert evidence.trust_level == "deterministic"
    assert evidence.trace_id is None
    assert evidence.status == "success"
    assert manifest.completeness_status == "complete"
    assert evidence.evidence_id == "ev_manifest_" + hashlib.sha256(
        (manifest.manifest_id + manifest.artifact_digest).encode("utf-8")
    ).hexdigest()[:32]
    assert manifest.canonical_digest == _sha256(
        manifest_module._canonical_body(manifest)
    )
    assert manifest.artifact_digest == manifest.canonical_digest


def test_build_canonical_recursion_failure_sanitized_no_growth(
    tmp_path, monkeypatch
):
    store = _populated_store(tmp_path)
    before = _file_set(store)

    def boom(manifest):
        raise RecursionError(f"recursion {SECRET_MARKER}")

    monkeypatch.setattr(manifest_module, "_canonical_body", boom)
    with pytest.raises(EvidenceManifestInputError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_A),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "invalid evidence manifest input"
    assert exc.value.__cause__ is None
    assert SECRET_MARKER not in str(exc.value)
    assert _file_set(store) == before


def test_direct_model_canonical_recursion_is_sanitized(monkeypatch):
    payload = _manifest_payload()

    def boom(manifest):
        raise RecursionError(SECRET_MARKER)

    monkeypatch.setattr(manifest_module, "_canonical_body", boom)
    with pytest.raises(ValidationError) as exc:
        EvidenceManifest.model_validate(payload)
    assert SECRET_MARKER not in str(exc.value)


def test_build_missing_artifact_fails_closed_no_growth(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(EvidenceManifestArtifactError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_OTHER),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact validation failed"
    assert exc.value.__cause__ is None
    assert _file_set(store) == set()


def test_build_corrupt_artifact_fails_closed_no_growth(tmp_path):
    store = _store(tmp_path)
    digest = store.put_bytes(REF_BYTES_A)
    target = store.root / "sha256" / digest[7:][:2] / digest[7:][2:]
    target.write_bytes(b"tampered")
    before = _file_set(store)
    with pytest.raises(EvidenceManifestArtifactError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=digest),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact validation failed"
    assert exc.value.__cause__ is None
    assert _file_set(store) == before


def test_build_verify_false_and_get_failure_fail_closed(tmp_path, monkeypatch):
    store = _populated_store(tmp_path)
    monkeypatch.setattr(store, "verify", lambda digest: False)
    with pytest.raises(EvidenceManifestArtifactError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_A),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact validation failed"
    assert exc.value.__cause__ is None

    store = _populated_store(tmp_path)

    def boom_get(digest):
        raise OSError(f"read failed {SECRET_MARKER}")

    monkeypatch.setattr(store, "get_bytes", boom_get)
    with pytest.raises(EvidenceManifestArtifactError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_A),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact validation failed"
    assert exc.value.__cause__ is None
    assert SECRET_MARKER not in str(exc.value)


def test_build_wrong_get_bytes_digest_fails_closed_no_growth(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    referenced_digest = store.put_bytes(REF_BYTES_A)
    before = _file_set(store)
    original_get_bytes = store.get_bytes

    def get_bytes(digest):
        if digest == referenced_digest:
            return b"tampered-bytes"
        return original_get_bytes(digest)

    monkeypatch.setattr(store, "get_bytes", get_bytes)
    with pytest.raises(EvidenceManifestArtifactError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=referenced_digest),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact validation failed"
    assert exc.value.__cause__ is None
    assert _file_set(store) == before


def test_build_artifact_validation_failure_causes_no_store_growth(tmp_path):
    store = _store(tmp_path)
    store.put_bytes(REF_BYTES_A)
    before = _file_set(store)
    items = (
        _input_obj("ev-001", artifact_digest=DIGEST_A),
        _input_obj("ev-002", artifact_digest=DIGEST_B),
    )
    with pytest.raises(EvidenceManifestArtifactError):
        EvidenceManifestBuilder.build(
            items,
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_build_secret_in_referenced_bytes_never_leaks(tmp_path):
    secret_bytes = f"prefix {SECRET_MARKER} suffix".encode("utf-8")
    secret_digest = _sha256(secret_bytes)
    store = _store(tmp_path)
    store.put_bytes(secret_bytes)
    result = EvidenceManifestBuilder.build(
        _inputs(1, artifact_digest=secret_digest),
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store,
    )
    dumped = result.model_dump_json()
    body = manifest_module._canonical_body(result.manifest)
    assert SECRET_MARKER not in dumped
    assert SECRET_MARKER not in body.decode("utf-8")
    assert SECRET_MARKER not in json.dumps(result.model_dump(mode="json"))
    assert result.evidence.artifact_digest == result.manifest.artifact_digest


def test_build_persistence_put_failures_sanitized(tmp_path, monkeypatch):
    store = _populated_store(tmp_path)
    before = _file_set(store)

    def boom_put(data):
        raise OSError(f"disk full {SECRET_MARKER}")

    monkeypatch.setattr(store, "put_bytes", boom_put)
    with pytest.raises(EvidenceManifestPersistenceError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_A),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact persistence failed"
    assert exc.value.__cause__ is None
    assert SECRET_MARKER not in str(exc.value)
    assert _file_set(store) == before

    store = _populated_store(tmp_path)
    monkeypatch.setattr(store, "put_bytes", lambda data: DIGEST_OTHER)
    with pytest.raises(EvidenceManifestPersistenceError) as exc:
        EvidenceManifestBuilder.build(
            _inputs(1, artifact_digest=DIGEST_A),
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact persistence failed"
    assert exc.value.__cause__ is None


def test_build_persistence_verify_get_failures_sanitized_and_artifact_remains(
    tmp_path, monkeypatch
):
    items = _inputs(1, artifact_digest=DIGEST_A)

    store = _populated_store(tmp_path)
    verify_calls = {"count": 0}
    real_verify = store.verify

    def fake_verify(digest):
        verify_calls["count"] += 1
        if verify_calls["count"] == 2:
            return False
        return real_verify(digest)

    monkeypatch.setattr(store, "verify", fake_verify)
    with pytest.raises(EvidenceManifestPersistenceError) as exc:
        EvidenceManifestBuilder.build(
            items,
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact persistence failed"
    assert exc.value.__cause__ is None
    assert len(_file_set(store)) == 3  # two referenced artifacts + manifest

    store = _populated_store(tmp_path)
    get_calls = {"count": 0}
    real_get = store.get_bytes

    def wrong_get(digest):
        get_calls["count"] += 1
        if get_calls["count"] == 2:
            return b"corrupted manifest bytes"
        return real_get(digest)

    monkeypatch.setattr(store, "get_bytes", wrong_get)
    with pytest.raises(EvidenceManifestPersistenceError) as exc:
        EvidenceManifestBuilder.build(
            items,
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact persistence failed"
    assert exc.value.__cause__ is None
    assert len(_file_set(store)) == 3

    store = _populated_store(tmp_path)
    get_calls = {"count": 0}

    def boom_get(digest):
        get_calls["count"] += 1
        if get_calls["count"] == 2:
            raise OSError(f"read failed {SECRET_MARKER}")
        return real_get(digest)

    monkeypatch.setattr(store, "get_bytes", boom_get)
    with pytest.raises(EvidenceManifestPersistenceError) as exc:
        EvidenceManifestBuilder.build(
            items,
            subject_digest=SUBJECT,
            evaluated_at=FIXED_TIME,
            artifact_store=store,
        )
    assert str(exc.value) == "evidence manifest artifact persistence failed"
    assert exc.value.__cause__ is None
    assert SECRET_MARKER not in str(exc.value)


def test_build_idempotent_artifact_and_permutation(tmp_path):
    store = _populated_store(tmp_path)
    items = _inputs(2, artifact_digest=DIGEST_A)
    first = EvidenceManifestBuilder.build(
        [items[1], items[0]],
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store,
    )
    second = EvidenceManifestBuilder.build(
        [items[0], items[1]],
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store,
    )
    assert second == first
    assert second.model_dump_json() == first.model_dump_json()
    digest = first.manifest.artifact_digest
    body = manifest_module._canonical_body(first.manifest)
    assert digest == _sha256(body)
    assert store.get_bytes(digest) == body
    assert store.verify(digest) is True
    assert _file_set(store) == {
        "sha256/" + DIGEST_A[7:][:2] + "/" + DIGEST_A[7:][2:],
        "sha256/" + DIGEST_B[7:][:2] + "/" + DIGEST_B[7:][2:],
        "sha256/" + digest[7:][:2] + "/" + digest[7:][2:],
    }


def test_store_interaction_only_allowed_methods(tmp_path, monkeypatch):
    store = _populated_store(tmp_path)
    calls: list[str] = []
    for name in ("exists", "verify", "get_bytes", "put_bytes"):
        real = getattr(store, name)

        def make_wrapper(name=name, real=real):
            def wrapper(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)

            return wrapper

        monkeypatch.setattr(store, name, make_wrapper())
    EvidenceManifestBuilder.build(
        _inputs(1, artifact_digest=DIGEST_A),
        subject_digest=SUBJECT,
        evaluated_at=FIXED_TIME,
        artifact_store=store,
    )
    assert calls == [
        "exists",
        "verify",
        "get_bytes",
        "put_bytes",
        "verify",
        "get_bytes",
    ]


def test_source_audit_no_forbidden_imports_io_or_execution():
    source = inspect.getsource(manifest_module)
    tree = ast.parse(source)
    imported_roots = set()
    forbidden_imports = {
        "sqlite3",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "anthropic",
        "os",
        "sys",
        "pathlib",
        "urllib",
        "requests",
        "shlex",
        "pty",
        "signal",
        "tempfile",
        "pickle",
        "ctypes",
        "importlib",
        "glob",
        "shutil",
        "git",
        "torch",
        "transformers",
    }
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "system",
        "popen",
        "glob",
        "urlopen",
        "request",
    }
    forbidden_methods = {
        "read_bytes",
        "write_bytes",
        "read_text",
        "write_text",
        "unlink",
        "mkdir",
        "rmdir",
        "system",
        "popen",
        "spawn",
        "connect",
        "urlopen",
        "request",
        "decode",
    }
    forbidden_names = {
        "os",
        "sys",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "anthropic",
        "pathlib",
        "requests",
        "sqlite3",
        "Path",
        "eval",
        "exec",
        "compile",
        "__import__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in forbidden_calls, func.id
            elif isinstance(func, ast.Attribute):
                assert func.attr not in forbidden_methods, func.attr
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_methods, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, node.id
    assert imported_roots.isdisjoint(forbidden_imports)
    assert "http://" not in source
    assert "https://" not in source
