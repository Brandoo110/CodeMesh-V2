"""Focused offline contract tests for the V2-P8-02A CI Evidence Adapter."""

import hashlib
import json
from pathlib import Path

import pytest

from assurance import ArtifactStore
from assurance.integrations.ci import (
    CIArtifactError,
    CIEvidenceAdapter,
    CIImportError,
    CIPayloadError,
    CIReport,
    CIReceipt,
    CIResult,
    CISubjectMismatch,
)


FIXTURES = Path(__file__).parent / "fixtures"
SUBJECT = "sha256:" + "1" * 64
OTHER_SUBJECT = "sha256:" + "2" * 64
SUCCESS_ARTIFACT = b"ci success artifact bytes\n"
FAILURE_ARTIFACT = b"ci failure artifact bytes\n"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _store(tmp_path, artifact: bytes) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "artifacts")
    store.put_bytes(artifact)
    return store


def _files(store: ArtifactStore) -> set[str]:
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def _success_data() -> dict[str, object]:
    return json.loads(_payload("assurance_ci_success.json"))


def test_models_are_frozen_and_forbid_extra_fields():
    for model in (CIReport, CIReceipt, CIResult):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_success_import_binds_subject_artifacts_and_source_ref(tmp_path):
    payload = _payload("assurance_ci_success.json")
    store = _store(tmp_path, SUCCESS_ARTIFACT)

    result = CIEvidenceAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )

    raw_digest = _sha256(payload)
    assert result.report.provider == "github_actions"
    assert result.report.status == "completed"
    assert result.report.conclusion == "success"
    assert result.receipt.raw_payload_artifact_digest == raw_digest
    assert result.receipt.referenced_artifact_verified is True
    assert result.receipt.claimed_trust_level == "observed"
    assert result.receipt.effective_trust_level == "declared"
    assert result.evidence.kind == "ci_run"
    assert result.evidence.status == "success"
    assert result.evidence.trust_level == "declared"
    assert result.evidence.artifact_digest == _sha256(SUCCESS_ARTIFACT)
    assert result.evidence.source_ref == (
        f"ci_run:github_actions:run-success-001:{raw_digest}"
    )
    assert store.get_bytes(raw_digest) == payload


def test_failure_import_maps_to_failure_and_downgrades_trust(tmp_path):
    result = CIEvidenceAdapter.import_bytes(
        _payload("assurance_ci_failure.json"),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path, FAILURE_ARTIFACT),
    )

    assert result.evidence.status == "failure"
    assert result.receipt.claimed_trust_level == "observed"
    assert result.receipt.effective_trust_level == "declared"


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "failure", "failure"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "timed_out", "error"),
    ],
)
def test_non_success_conclusions_have_explicit_evidence_status(
    tmp_path, status, conclusion, expected
):
    data = _success_data()
    data["status"] = status
    data["conclusion"] = conclusion
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    result = CIEvidenceAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path, SUCCESS_ARTIFACT),
    )

    assert result.evidence.status == expected


def test_subject_mismatch_fails_closed_without_raw_persistence(tmp_path):
    payload = _payload("assurance_ci_success.json")
    store = _store(tmp_path, SUCCESS_ARTIFACT)
    before = _files(store)

    with pytest.raises(CISubjectMismatch):
        CIEvidenceAdapter.import_bytes(
            payload,
            expected_subject_digest=OTHER_SUBJECT,
            artifact_store=store,
        )

    assert _files(store) == before


def test_missing_referenced_artifact_fails_closed_without_raw_persistence(
    tmp_path,
):
    payload = _payload("assurance_ci_success.json")
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(CIArtifactError):
        CIEvidenceAdapter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )

    assert not store.root.exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.replace(
            b'"provider":"github_actions",',
            b'"provider":"github_actions","provider":"github_actions",',
        ),
        lambda payload: payload.replace(b'"artifact_name":"ci-results.zip"', b'"artifact_name":NaN'),
        lambda payload: json.dumps(
            {**json.loads(payload), "unexpected": "forbidden"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    ],
    ids=["duplicate-key", "nan", "extra-field"],
)
def test_malformed_payloads_fail_closed_without_raw_persistence(tmp_path, mutator):
    payload = mutator(_payload("assurance_ci_success.json"))
    store = _store(tmp_path, SUCCESS_ARTIFACT)
    before = _files(store)

    with pytest.raises((CIPayloadError, CIImportError)):
        CIEvidenceAdapter.import_bytes(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )

    assert _files(store) == before


@pytest.mark.parametrize(
    "claimed_trust_level",
    ["declared", "observed", "deterministic", "inferred", "human_attested"],
)
def test_claimed_trust_never_elevates_effective_trust(
    tmp_path, claimed_trust_level
):
    data = _success_data()
    data["claimed_trust_level"] = claimed_trust_level
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    result = CIEvidenceAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path, SUCCESS_ARTIFACT),
    )

    assert result.receipt.claimed_trust_level == claimed_trust_level
    assert result.receipt.effective_trust_level == "declared"
    assert result.evidence.trust_level == "declared"


def test_replay_is_byte_and_model_stable(tmp_path):
    payload = _payload("assurance_ci_success.json")
    store = _store(tmp_path, SUCCESS_ARTIFACT)

    first = CIEvidenceAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    before = _files(store)
    second = CIEvidenceAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )

    assert second == first
    assert _files(store) == before
    assert store.get_bytes(first.receipt.raw_payload_artifact_digest) == payload


def test_result_rejects_forged_receipt_claimed_trust_level(tmp_path):
    result = CIEvidenceAdapter.import_bytes(
        _payload("assurance_ci_success.json"),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path, SUCCESS_ARTIFACT),
    )
    forged_receipt = result.receipt.model_copy(
        update={"claimed_trust_level": "declared"}
    )

    with pytest.raises(ValueError, match="claimed trust"):
        CIResult(
            report=result.report,
            receipt=forged_receipt,
            evidence=result.evidence,
        )


def test_result_rejects_forged_report_status_projection(tmp_path):
    result = CIEvidenceAdapter.import_bytes(
        _payload("assurance_ci_failure.json"),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path, FAILURE_ARTIFACT),
    )
    forged_receipt = result.receipt.model_copy(
        update={"evidence_status": "success"}
    )
    forged_evidence = result.evidence.model_copy(update={"status": "success"})

    with pytest.raises(ValueError, match="evidence status"):
        CIResult(
            report=result.report,
            receipt=forged_receipt,
            evidence=forged_evidence,
        )
