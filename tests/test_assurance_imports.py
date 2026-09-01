"""Focused contract and importer tests for assurance.imports (V2-P2-04)."""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
from assurance import ArtifactStore
from assurance import imports as import_module
from assurance.imports import (
    GenericEvidenceArtifactError,
    GenericEvidenceEnvelope,
    GenericEvidenceImportError,
    GenericEvidenceImportReceipt,
    GenericEvidenceImportResult,
    GenericEvidenceImporter,
    GenericEvidencePayloadError,
    GenericEvidenceSubjectMismatch,
    SignatureMetadata,
)


SUBJECT = "sha256:" + "0" * 64


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


REF_BYTES = b"referenced artifact bytes for generic evidence import"
REF_DIGEST = _sha256(REF_BYTES)
OTHER_DIGEST = _sha256(b"other bytes for forged generic evidence")
SIG_DIGEST = _sha256(b"signature metadata payload")
FIXED_TIME_ISO = "2026-08-25T08:00:00+00:00"

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
}

NEW_PUBLIC_NAMES = {
    "GenericEvidenceImporter",
    "GenericEvidenceEnvelope",
    "GenericEvidenceImportReceipt",
    "GenericEvidenceImportResult",
    "SignatureMetadata",
    "GenericEvidenceImportError",
    "GenericEvidencePayloadError",
    "GenericEvidenceSubjectMismatch",
    "GenericEvidenceArtifactError",
}


def test_default_import_does_not_load_experimental_graph():
    probe = """
import sys
import assurance

experimental_modules = {
    "assurance.review_council",
    "assurance.model_routing",
    "assurance.execution_receipt",
    "assurance.adjudicator",
    "assurance.council_report",
}
loaded = sorted(experimental_modules & set(sys.modules))
assert not loaded, loaded
assert not {
    "ReviewCouncil",
    "ModelRouter",
    "CouncilExecutionReceipt",
    "Adjudicator",
    "CouncilReport",
} & set(assurance.__all__)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

ALL_MODELS = (
    SignatureMetadata,
    GenericEvidenceEnvelope,
    GenericEvidenceImportReceipt,
    GenericEvidenceImportResult,
)

def _envelope(**overrides):
    data = {
        "schema_version": "v1",
        "producer": "producer",
        "kind": "test",
        "subject_digest": SUBJECT,
        "status": "success",
        "artifact_digest": REF_DIGEST,
        "collected_at": FIXED_TIME_ISO,
        "claimed_trust_level": "declared",
        "result": "ok",
    }
    data.update(overrides)
    return data


def _signature(**overrides):
    data = {
        "schema_version": "v1",
        "scheme": "sha256",
        "key_id": "key-001",
        "signature_digest": SIG_DIGEST,
    }
    data.update(overrides)
    return data


def _payload(**overrides) -> bytes:
    return json.dumps(
        _envelope(**overrides),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_dict(**overrides):
    data = {
        "schema_version": "v1",
        "import_payload_artifact_digest": _sha256(b"raw"),
        "canonical_payload_digest": _sha256(b"canonical"),
        "referenced_artifact_digest": REF_DIGEST,
        "referenced_artifact_verified": True,
        "claimed_trust_level": "declared",
        "effective_trust_level": "declared",
        "signature_status": "absent",
    }
    data.update(overrides)
    return data


def _result_dict():
    raw_digest = _sha256(b"raw")
    envelope = GenericEvidenceEnvelope.model_validate(_envelope())
    canonical_digest = _sha256(_canonical_bytes(envelope))
    receipt = _receipt_dict(
        import_payload_artifact_digest=raw_digest,
        canonical_payload_digest=canonical_digest,
    )
    evidence_id = "ev_import_" + hashlib.sha256(
        (raw_digest + canonical_digest + REF_DIGEST).encode("ascii")
    ).hexdigest()[:32]
    return {
        "schema_version": "v1",
        "envelope": envelope.model_dump(mode="json"),
        "receipt": receipt,
        "evidence": {
            "schema_version": "v1",
            "evidence_id": evidence_id,
            "subject_digest": SUBJECT,
            "kind": "test",
            "producer": "producer",
            "artifact_digest": REF_DIGEST,
            "source_ref": f"generic_import:{raw_digest}",
            "trace_id": None,
            "status": "success",
            "trust_level": "declared",
            "collected_at": FIXED_TIME_ISO,
        },
    }


def _refresh_result_digests(data):
    envelope = GenericEvidenceEnvelope.model_validate(data["envelope"])
    canonical_digest = _sha256(_canonical_bytes(envelope))
    data["receipt"]["canonical_payload_digest"] = canonical_digest
    raw_digest = data["receipt"]["import_payload_artifact_digest"]
    referenced_digest = data["receipt"]["referenced_artifact_digest"]
    data["evidence"]["evidence_id"] = "ev_import_" + hashlib.sha256(
        (raw_digest + canonical_digest + referenced_digest).encode("ascii")
    ).hexdigest()[:32]


FORGED_RESULT_CASES = (
    pytest.param(
        lambda data: data["envelope"].update(subject_digest=OTHER_DIGEST),
        id="envelope.subject_digest vs evidence.subject_digest",
    ),
    pytest.param(
        lambda data: data["envelope"].update(producer="other-producer"),
        id="envelope.producer vs evidence.producer",
    ),
    pytest.param(
        lambda data: data["envelope"].update(kind="other-kind"),
        id="envelope.kind vs evidence.kind",
    ),
    pytest.param(
        lambda data: data["envelope"].update(status="failure"),
        id="envelope.status vs evidence.status",
    ),
    pytest.param(
        lambda data: data["envelope"].update(artifact_digest=OTHER_DIGEST),
        id="envelope.artifact_digest vs evidence.artifact_digest",
    ),
    pytest.param(
        lambda data: data["envelope"].update(
            collected_at="2026-08-25T09:00:00+00:00"
        ),
        id="envelope.collected_at vs evidence.collected_at",
    ),
    pytest.param(
        lambda data: data["evidence"].update(trace_id="trace-forged"),
        id="envelope.trace_id None vs evidence.trace_id set",
    ),
    pytest.param(
        lambda data: data["envelope"].update(trace_id="trace-forged"),
        id="envelope.trace_id set vs evidence.trace_id None",
    ),
    pytest.param(
        lambda data: (
            data["envelope"].update(trace_id="trace-a"),
            data["evidence"].update(trace_id="trace-b"),
        ),
        id="envelope.trace_id vs evidence.trace_id different",
    ),
    pytest.param(
        lambda data: data["evidence"].update(trust_level="observed"),
        id="evidence.trust_level other than declared",
    ),
    pytest.param(
        lambda data: data["evidence"].update(
            source_ref="generic_import:" + OTHER_DIGEST
        ),
        id="evidence.source_ref not exact receipt import digest",
    ),
    pytest.param(
        lambda data: data["receipt"].update(
            referenced_artifact_digest=OTHER_DIGEST
        ),
        id="receipt.referenced_artifact_digest vs envelope/evidence",
    ),
    pytest.param(
        lambda data: data["evidence"].update(artifact_digest=OTHER_DIGEST),
        id="evidence.artifact_digest vs receipt.referenced_artifact_digest",
    ),
    pytest.param(
        lambda data: data["receipt"].update(claimed_trust_level="observed"),
        id="receipt.claimed_trust_level vs envelope.claimed_trust_level",
    ),
    pytest.param(
        lambda data: data["envelope"].update(signature=_signature()),
        id="signature present but signature_status absent",
    ),
    pytest.param(
        lambda data: data["receipt"].update(
            signature_status="unverified_metadata"
        ),
        id="signature absent but signature_status unverified_metadata",
    ),
    pytest.param(
        lambda data: data["evidence"].update(
            evidence_id="ev_import_" + "0" * 32
        ),
        id="evidence.evidence_id not derived from receipt digests",
    ),
)


def _make_store(tmp_path) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "store")
    store.put_bytes(REF_BYTES)
    return store


def _file_set(store):
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def _canonical_bytes(envelope) -> bytes:
    return json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def test_imports_public_api_exists():
    assert import_module.GenericEvidenceImporter is GenericEvidenceImporter
    assert import_module.GenericEvidenceEnvelope is GenericEvidenceEnvelope
    assert import_module.GenericEvidenceImportReceipt is GenericEvidenceImportReceipt
    assert import_module.GenericEvidenceImportResult is GenericEvidenceImportResult
    assert import_module.SignatureMetadata is SignatureMetadata
    assert import_module.GenericEvidenceImportError is GenericEvidenceImportError
    assert import_module.GenericEvidencePayloadError is GenericEvidencePayloadError
    assert (
        import_module.GenericEvidenceSubjectMismatch
        is GenericEvidenceSubjectMismatch
    )
    assert (
        import_module.GenericEvidenceArtifactError
        is GenericEvidenceArtifactError
    )


def test_package_exports_preserve_prior_names_and_add_import_api():
    assert set(PRIOR_PUBLIC_NAMES) | NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.__all__ != list(PRIOR_PUBLIC_NAMES)


def test_error_hierarchy_is_simple():
    assert issubclass(GenericEvidenceImportError, Exception)
    assert issubclass(GenericEvidencePayloadError, GenericEvidenceImportError)
    assert issubclass(GenericEvidenceSubjectMismatch, GenericEvidenceImportError)
    assert issubclass(GenericEvidenceArtifactError, GenericEvidenceImportError)


def test_importer_has_no_extra_public_knobs():
    public_methods = sorted(
        name for name in vars(GenericEvidenceImporter) if not name.startswith("_")
    )
    assert public_methods == ["import_bytes"]


def test_all_models_are_v1_frozen_extra_forbid():
    for model in ALL_MODELS:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_signature_metadata_field_order_frozen_roundtrip():
    assert list(SignatureMetadata.model_fields) == [
        "schema_version",
        "scheme",
        "key_id",
        "signature_digest",
    ]
    model = SignatureMetadata.model_validate(_signature())
    restored = SignatureMetadata.model_validate(model.model_dump(mode="json"))
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()


def test_signature_metadata_v1_only_and_strict_text():
    with pytest.raises(ValidationError):
        SignatureMetadata.model_validate(_signature(schema_version="v2"))
    for overrides in (
        {"scheme": 123},
        {"key_id": None},
        {"scheme": " "},
        {"key_id": "\t"},
        {"signature_digest": "sha256:XYZ"},
        {"signature_digest": SIG_DIGEST.upper()},
        {"signature_digest": SIG_DIGEST[:-1]},
        {**_signature(), "unexpected": 1},
    ):
        with pytest.raises(ValidationError):
            SignatureMetadata.model_validate(overrides)


def test_envelope_field_order_v1_frozen_extra_forbid_minimal_roundtrip():
    assert list(GenericEvidenceEnvelope.model_fields) == [
        "schema_version",
        "producer",
        "kind",
        "subject_digest",
        "status",
        "artifact_digest",
        "collected_at",
        "claimed_trust_level",
        "command",
        "result",
        "trace_id",
        "signature",
    ]
    model = GenericEvidenceEnvelope.model_validate(_envelope())
    assert model.command is None
    assert model.trace_id is None
    assert model.signature is None
    restored = GenericEvidenceEnvelope.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        GenericEvidenceEnvelope.model_validate(
            {**_envelope(), "unexpected": 1}
        )
    with pytest.raises(ValidationError):
        GenericEvidenceEnvelope.model_validate(
            _envelope(schema_version="v2")
        )


def test_envelope_complete_roundtrip_command_tuple_and_copy_safety():
    command_items = ["python", "-c", "print('ok')"]
    model = GenericEvidenceEnvelope.model_validate(
        _envelope(
            command=command_items,
            trace_id="trace-1",
            signature=_signature(),
        )
    )
    assert model.command == ("python", "-c", "print('ok')")
    assert type(model.command) is tuple
    command_items.append("mutated")
    assert model.command == ("python", "-c", "print('ok')")
    dumped = model.model_dump(mode="json")
    assert dumped["command"] == ["python", "-c", "print('ok')"]
    restored = GenericEvidenceEnvelope.model_validate(dumped)
    assert restored == model
    assert type(restored.command) is tuple


def test_envelope_strict_primitives_missing_and_literals():
    invalid_overrides = (
        {"producer": 123},
        {"kind": None},
        {"result": 42},
        {"trace_id": 123},
        {"subject_digest": 1},
        {"collected_at": "not-a-date"},
        {"status": "unknown"},
        {"claimed_trust_level": "verified"},
        {"command": [1]},
        {"command": ["ok", None]},
        {"command": ["ok", b"bytes"]},
    )
    for overrides in invalid_overrides:
        with pytest.raises(ValidationError):
            GenericEvidenceEnvelope.model_validate(_envelope(**overrides))
    missing_result = _envelope()
    del missing_result["result"]
    with pytest.raises(ValidationError):
        GenericEvidenceEnvelope.model_validate(missing_result)
    missing_producer = _envelope()
    del missing_producer["producer"]
    with pytest.raises(ValidationError):
        GenericEvidenceEnvelope.model_validate(missing_producer)


def test_envelope_blank_and_max_length_boundaries():
    for overrides in (
        {"producer": " "},
        {"kind": "\n"},
        {"result": ""},
        {"result": "   "},
        {"trace_id": " "},
        {"trace_id": ""},
    ):
        with pytest.raises(ValidationError):
            GenericEvidenceEnvelope.model_validate(_envelope(**overrides))
    GenericEvidenceEnvelope.model_validate(
        _envelope(producer="x" * 128, kind="y" * 128, result="z" * 4096)
    )
    GenericEvidenceEnvelope.model_validate(_envelope(trace_id="t" * 256))
    for overrides in (
        {"producer": "x" * 129},
        {"kind": "y" * 129},
        {"result": "z" * 4097},
        {"trace_id": "t" * 257},
    ):
        with pytest.raises(ValidationError):
            GenericEvidenceEnvelope.model_validate(_envelope(**overrides))


def test_envelope_command_boundaries():
    GenericEvidenceEnvelope.model_validate(_envelope(command=["single"]))
    GenericEvidenceEnvelope.model_validate(
        _envelope(command=tuple(f"item-{i}" for i in range(32)))
    )
    for overrides in (
        {"command": []},
        {"command": [""]},
        {"command": [" " * 3]},
        {"command": ["a\x00b"]},
        {"command": tuple(f"item-{i}" for i in range(33))},
    ):
        with pytest.raises(ValidationError):
            GenericEvidenceEnvelope.model_validate(_envelope(**overrides))


def test_envelope_command_utf8_byte_boundaries():
    ok_items = tuple("é" * 64 for _ in range(32))
    assert sum(len(item.encode("utf-8")) for item in ok_items) == 4096
    GenericEvidenceEnvelope.model_validate(_envelope(command=ok_items))
    too_many_bytes = tuple("é" * 64 for _ in range(31)) + ("é" * 64 + "a",)
    assert sum(len(item.encode("utf-8")) for item in too_many_bytes) == 4097
    with pytest.raises(ValidationError):
        GenericEvidenceEnvelope.model_validate(
            _envelope(command=too_many_bytes)
        )


def test_envelope_naive_datetime_and_bad_digests():
    for overrides in (
        {"collected_at": "2026-08-25T08:00:00"},
        {"subject_digest": "sha256:abc"},
        {"subject_digest": REF_DIGEST.upper()},
        {"artifact_digest": "sha256:" + "A" * 64},
        {"artifact_digest": "md5:" + "0" * 64},
        {"artifact_digest": REF_DIGEST[:-1]},
    ):
        with pytest.raises(ValidationError):
            GenericEvidenceEnvelope.model_validate(_envelope(**overrides))


def test_receipt_field_order_v1_frozen_extra_forbid_roundtrip():
    assert list(GenericEvidenceImportReceipt.model_fields) == [
        "schema_version",
        "import_payload_artifact_digest",
        "canonical_payload_digest",
        "referenced_artifact_digest",
        "referenced_artifact_verified",
        "claimed_trust_level",
        "effective_trust_level",
        "signature_status",
    ]
    model = GenericEvidenceImportReceipt.model_validate(_receipt_dict())
    restored = GenericEvidenceImportReceipt.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        GenericEvidenceImportReceipt.model_validate(
            {**_receipt_dict(), "unexpected": 1}
        )
    with pytest.raises(ValidationError):
        GenericEvidenceImportReceipt.model_validate(
            _receipt_dict(schema_version="v2")
        )


def test_receipt_literals_are_forced():
    for overrides in (
        {"referenced_artifact_verified": False},
        {"effective_trust_level": "observed"},
        {"signature_status": "verified"},
        {"claimed_trust_level": "verified"},
        {"import_payload_artifact_digest": "bad"},
    ):
        with pytest.raises(ValidationError):
            GenericEvidenceImportReceipt.model_validate(
                _receipt_dict(**overrides)
            )


def test_result_field_order_v1_frozen_extra_forbid_roundtrip():
    assert list(GenericEvidenceImportResult.model_fields) == [
        "schema_version",
        "envelope",
        "receipt",
        "evidence",
    ]
    model = GenericEvidenceImportResult.model_validate(_result_dict())
    restored = GenericEvidenceImportResult.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        GenericEvidenceImportResult.model_validate(
            {**_result_dict(), "unexpected": 1}
        )
    with pytest.raises(ValidationError):
        GenericEvidenceImportResult.model_validate(
            {**_result_dict(), "schema_version": "v2"}
        )


@pytest.mark.parametrize("mutate", FORGED_RESULT_CASES)
def test_result_rejects_forged_cross_bindings(mutate):
    data = _result_dict()
    mutate(data)
    with pytest.raises(ValidationError):
        GenericEvidenceImportResult.model_validate(data)


def test_result_signed_roundtrip_accepts_unverified_metadata():
    data = _result_dict()
    data["envelope"]["signature"] = _signature()
    data["receipt"]["signature_status"] = "unverified_metadata"
    _refresh_result_digests(data)
    model = GenericEvidenceImportResult.model_validate(data)
    restored = GenericEvidenceImportResult.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    assert model.envelope.signature is not None
    assert model.receipt.signature_status == "unverified_metadata"


def test_result_dict_canonical_digest_matches_validated_envelope():
    data = _result_dict()
    model = GenericEvidenceImportResult.model_validate(data)
    assert (
        model.receipt.canonical_payload_digest
        == _sha256(_canonical_bytes(model.envelope))
    )


def test_result_rejects_forged_canonical_digest_with_recomputed_evidence_id():
    data = _result_dict()
    forged_canonical = _sha256(b"forged canonical digest")
    data["receipt"]["canonical_payload_digest"] = forged_canonical
    raw_digest = data["receipt"]["import_payload_artifact_digest"]
    referenced_digest = data["receipt"]["referenced_artifact_digest"]
    data["evidence"]["evidence_id"] = "ev_import_" + hashlib.sha256(
        (raw_digest + forged_canonical + referenced_digest).encode("ascii")
    ).hexdigest()[:32]
    with pytest.raises(ValidationError):
        GenericEvidenceImportResult.model_validate(data)


def test_result_rejects_envelope_only_semantic_change_not_forwarded():
    data = _result_dict()
    data["envelope"]["result"] = "forged result not forwarded to evidence"
    with pytest.raises(ValidationError):
        GenericEvidenceImportResult.model_validate(data)


def test_valid_minimal_import_bindings_and_three_digest_layers(tmp_path):
    store = _make_store(tmp_path)
    payload = _payload()
    result = GenericEvidenceImporter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    raw_digest = result.receipt.import_payload_artifact_digest
    canonical_digest = result.receipt.canonical_payload_digest
    assert raw_digest == _sha256(payload)
    assert canonical_digest == _sha256(_canonical_bytes(result.envelope))
    assert result.receipt.referenced_artifact_digest == REF_DIGEST
    assert result.receipt.referenced_artifact_verified is True
    assert result.receipt.effective_trust_level == "declared"
    assert result.receipt.claimed_trust_level == "declared"
    assert result.receipt.signature_status == "absent"
    assert store.exists(raw_digest) is True
    assert store.verify(raw_digest) is True
    assert store.get_bytes(raw_digest) == payload
    assert store.verify(REF_DIGEST) is True
    assert store.get_bytes(REF_DIGEST) == REF_BYTES
    assert result.evidence.artifact_digest == REF_DIGEST
    assert result.evidence.subject_digest == SUBJECT
    assert result.evidence.trust_level == "declared"


def test_complete_import_command_trace_signature(tmp_path):
    store = _make_store(tmp_path)
    payload = _payload(
        command=["check", "--strict"],
        trace_id="trace-42",
        signature=_signature(key_id="signer-b"),
    )
    result = GenericEvidenceImporter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert result.envelope.command == ("check", "--strict")
    assert result.envelope.trace_id == "trace-42"
    assert result.envelope.signature is not None
    assert result.envelope.signature.scheme == "sha256"
    assert result.envelope.signature.key_id == "signer-b"
    assert result.receipt.signature_status == "unverified_metadata"
    assert result.evidence.trace_id == "trace-42"


@pytest.mark.parametrize(
    "claimed",
    ["declared", "observed", "deterministic", "inferred", "human_attested"],
)
def test_every_claimed_trust_yields_declared_effective_trust(tmp_path, claimed):
    store = _make_store(tmp_path)
    result = GenericEvidenceImporter.import_bytes(
        _payload(claimed_trust_level=claimed),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert result.envelope.claimed_trust_level == claimed
    assert result.receipt.claimed_trust_level == claimed
    assert result.receipt.effective_trust_level == "declared"
    assert result.evidence.trust_level == "declared"


@pytest.mark.parametrize(
    "claimed", ["deterministic", "human_attested"]
)
def test_signature_never_verifies_or_elevates_trust(tmp_path, claimed):
    store = _make_store(tmp_path)
    result = GenericEvidenceImporter.import_bytes(
        _payload(
            claimed_trust_level=claimed,
            signature=_signature(
                scheme="untrusted-scheme",
                key_id="untrusted-key",
                signature_digest=SIG_DIGEST,
            ),
        ),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert result.receipt.signature_status == "unverified_metadata"
    assert result.evidence.trust_level == "declared"
    assert "verified" not in GenericEvidenceImportReceipt.model_fields
    assert "verified" not in SignatureMetadata.model_fields


def test_envelope_fields_forwarded_exactly_to_evidence(tmp_path):
    store = _make_store(tmp_path)
    result = GenericEvidenceImporter.import_bytes(
        _payload(
            producer="forward-producer",
            kind="unit-test",
            status="failure",
            trace_id="forward-trace",
            collected_at="2026-08-25T09:30:15.123456+08:00",
        ),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    evidence = result.evidence
    envelope = result.envelope
    assert evidence.producer == envelope.producer == "forward-producer"
    assert evidence.kind == envelope.kind == "unit-test"
    assert evidence.status == envelope.status == "failure"
    assert evidence.artifact_digest == envelope.artifact_digest == REF_DIGEST
    assert evidence.subject_digest == envelope.subject_digest == SUBJECT
    assert evidence.trace_id == envelope.trace_id == "forward-trace"
    assert evidence.collected_at == envelope.collected_at
    assert evidence.collected_at.tzinfo is not None


def test_evidence_id_and_source_ref_are_exact(tmp_path):
    store = _make_store(tmp_path)
    result = GenericEvidenceImporter.import_bytes(
        _payload(),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    raw_digest = result.receipt.import_payload_artifact_digest
    canonical_digest = result.receipt.canonical_payload_digest
    referenced_digest = result.receipt.referenced_artifact_digest
    id_input = (raw_digest + canonical_digest + referenced_digest).encode(
        "ascii"
    )
    expected_id = "ev_import_" + hashlib.sha256(id_input).hexdigest()[:32]
    assert result.evidence.evidence_id == expected_id
    assert result.evidence.evidence_id.startswith("ev_import_")
    assert len(result.evidence.evidence_id) == len("ev_import_") + 32
    assert result.evidence.source_ref == f"generic_import:{raw_digest}"


def test_raw_key_order_and_whitespace_variants(tmp_path):
    store = _make_store(tmp_path)
    data = _envelope()
    payload_a = json.dumps(
        data, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload_b = json.dumps(
        {key: data[key] for key in reversed(list(data))},
        indent=4,
        ensure_ascii=False,
    ).encode("utf-8")
    assert payload_a != payload_b
    result_a = GenericEvidenceImporter.import_bytes(
        payload_a,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    result_b = GenericEvidenceImporter.import_bytes(
        payload_b,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert result_a.envelope == result_b.envelope
    assert (
        result_a.receipt.canonical_payload_digest
        == result_b.receipt.canonical_payload_digest
    )
    assert (
        result_a.receipt.import_payload_artifact_digest
        != result_b.receipt.import_payload_artifact_digest
    )
    assert result_a.evidence.source_ref != result_b.evidence.source_ref
    assert result_a.evidence.evidence_id != result_b.evidence.evidence_id


def test_repeated_import_is_equal_and_artifact_idempotent(tmp_path):
    store = _make_store(tmp_path)
    payload = _payload()
    first = GenericEvidenceImporter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    before = _file_set(store)
    second = GenericEvidenceImporter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert first == second
    assert _file_set(store) == before


@pytest.mark.parametrize(
    "payload",
    [
        bytearray(b"{}"),
        memoryview(b"{}"),
        "{}",
        123,
        None,
        [b"{}"],
    ],
)
def test_import_bytes_rejects_non_bytes_payload(tmp_path, payload):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


def test_import_bytes_rejects_empty_and_oversized_payload(tmp_path):
    store = _make_store(tmp_path)
    for payload in (b"", b" " * (1024 * 1024 + 1)):
        with pytest.raises(GenericEvidencePayloadError):
            GenericEvidenceImporter.import_bytes(
                payload,
                expected_subject_digest=SUBJECT,
                artifact_store=store,
            )


@pytest.mark.parametrize(
    "payload",
    [
        b"\xef\xbb\xbf" + b'{"result": "ok"}',
        b'{"result": "a\x00b"}',
        b'{"result": "\xff"}',
        b"\xc3\x28",
    ],
)
def test_import_bytes_rejects_bom_nul_invalid_utf8(tmp_path, payload):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"result": NaN}',
        b'{"result": Infinity}',
        b'{"result": -Infinity}',
    ],
)
def test_import_bytes_rejects_nan_infinity_neg_infinity(tmp_path, payload):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"producer": "a", "producer": "b"}',
        b'{"signature": {"scheme": "a", "scheme": "b"}}',
    ],
)
def test_import_bytes_rejects_duplicate_keys_at_every_level(
    tmp_path, payload
):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"[1, 2, 3]",
        b'"scalar"',
        b"42",
        b"null",
        b"true",
    ],
)
def test_import_bytes_rejects_non_object_top_level(tmp_path, payload):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


@pytest.mark.parametrize(
    "expected",
    [None, 123, "sha256:xyz", "sha256:" + "A" * 64, "sha256:" + "g" * 64, ""],
)
def test_invalid_expected_subject_digest_is_rejected(tmp_path, expected):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=expected,
            artifact_store=store,
        )


def test_expected_subject_mismatch(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(GenericEvidenceSubjectMismatch):
        GenericEvidenceImporter.import_bytes(
            _payload(subject_digest="sha256:" + "1" * 64),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )


def test_artifact_store_must_be_exact_type(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(TypeError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=object(),
        )
    class FakeStore(ArtifactStore):
        pass

    with pytest.raises(TypeError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=FakeStore(store.root),
        )


def test_missing_referenced_artifact_fails_without_growth(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    before = _file_set(store)
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_corrupt_referenced_artifact_fails_without_growth(tmp_path):
    store = _make_store(tmp_path)
    before = _file_set(store)
    ref_hex = REF_DIGEST[7:]
    target = store.root / "sha256" / ref_hex[:2] / ref_hex[2:]
    target.write_bytes(b"tampered referenced bytes")
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before
    assert target.read_bytes() == b"tampered referenced bytes"


def test_monkeypatched_exists_false_fails_without_growth(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    before = _file_set(store)
    monkeypatch.setattr(store, "exists", lambda digest: False)
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_monkeypatched_verify_false_fails_without_growth(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    before = _file_set(store)
    monkeypatch.setattr(store, "verify", lambda digest: False)
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_monkeypatched_verify_error_fails_without_growth(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    before = _file_set(store)

    def fail_verify(digest):
        raise OSError("simulated verify failure")

    monkeypatch.setattr(store, "verify", fail_verify)
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_monkeypatched_get_bytes_error_fails_without_growth(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    before = _file_set(store)

    def fail_get_bytes(digest):
        raise OSError("simulated get_bytes failure")

    monkeypatch.setattr(store, "get_bytes", fail_get_bytes)
    with pytest.raises(GenericEvidenceArtifactError):
        GenericEvidenceImporter.import_bytes(
            _payload(),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_payload_validation_failure_does_not_grow_store(tmp_path):
    store = _make_store(tmp_path)
    before = _file_set(store)
    payload = json.dumps(_envelope(status="bogus")).encode("utf-8")
    with pytest.raises(GenericEvidencePayloadError):
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_subject_mismatch_does_not_grow_store(tmp_path):
    store = _make_store(tmp_path)
    before = _file_set(store)
    with pytest.raises(GenericEvidenceSubjectMismatch):
        GenericEvidenceImporter.import_bytes(
            _payload(subject_digest="sha256:" + "2" * 64),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == before


def test_canonical_serialization_failure_is_sanitized_payload_error_no_growth(
    tmp_path, monkeypatch
):
    store = _make_store(tmp_path)
    before = _file_set(store)
    marker = "UNIQUE_CANONICAL_PAYLOAD_9e8f7d"
    payload = _payload(result=marker)

    def fail_canonical(envelope):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        import_module, "_canonical_envelope_bytes", fail_canonical
    )
    with pytest.raises(GenericEvidencePayloadError) as excinfo:
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert marker not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert _file_set(store) == before


def test_payload_error_message_never_leaks_marker(tmp_path):
    store = _make_store(tmp_path)
    marker = "UNIQUE_PAYLOAD_SECRET_7f31a9"
    payload = json.dumps(
        _envelope(result=marker, status="bogus")
    ).encode("utf-8")
    with pytest.raises(GenericEvidencePayloadError) as excinfo:
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert marker not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_artifact_error_message_never_leaks_marker(tmp_path):
    store = _make_store(tmp_path)
    marker = "UNIQUE_ARTIFACT_SECRET_92c1d0"
    ref_digest = _sha256(marker.encode("utf-8"))
    payload = json.dumps(_envelope(artifact_digest=ref_digest)).encode("utf-8")
    with pytest.raises(GenericEvidenceArtifactError) as excinfo:
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert marker not in str(excinfo.value)
    assert ref_digest not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_subject_mismatch_error_message_never_leaks_marker(tmp_path):
    store = _make_store(tmp_path)
    marker = "UNIQUE_SUBJECT_SECRET_4b1c2d"
    payload = json.dumps(
        _envelope(subject_digest="sha256:" + "3" * 64, producer=marker)
    ).encode("utf-8")
    with pytest.raises(GenericEvidenceSubjectMismatch) as excinfo:
        GenericEvidenceImporter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert marker not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_referenced_bytes_never_appear_in_models(tmp_path):
    marker = b"UNIQUE_REFERENCED_BYTES_0a1b2c3d"
    ref_digest = _sha256(marker)
    store = ArtifactStore(tmp_path / "store")
    store.put_bytes(marker)
    result = GenericEvidenceImporter.import_bytes(
        json.dumps(_envelope(artifact_digest=ref_digest)).encode("utf-8"),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    serialized = result.model_dump_json()
    assert marker.decode("utf-8") not in serialized
    assert "0a1b2c3d" not in serialized


def test_command_and_result_never_produce_sentinel_side_effect(tmp_path):
    store = _make_store(tmp_path)
    marker = tmp_path / "sentinel-created-by-payload"
    payload = _payload(
        command=["touch", str(marker)],
        result=f"echo {marker} > {marker}",
    )
    GenericEvidenceImporter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert not marker.exists()
    assert not list(tmp_path.glob("sentinel-*"))


def test_source_audit_no_forbidden_imports_io_or_execution():
    source = inspect.getsource(import_module)
    tree = ast.parse(source)
    imported_roots = set()
    forbidden_imports = {
        "subprocess",
        "socket",
        "httpx",
        "openai",
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
    }
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
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
    }
    forbidden_names = {
        "os",
        "sys",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "pathlib",
        "requests",
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
