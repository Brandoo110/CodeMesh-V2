"""Focused offline contract tests for the P8-07 external tool adapters."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance.artifacts import ArtifactStore
from assurance.integrations.external_tools import (
    CodeQLAdapter,
    ExternalToolArtifactError,
    ExternalToolImportReceipt,
    ExternalToolPayloadError,
    ExternalToolResult,
    ExternalToolSubjectMismatch,
    CortexAdapter,
    HarnessAdapter,
    SonarAdapter,
    ToolFindingSummary,
)


FIXTURES = Path(__file__).parent / "fixtures"
SUBJECT = "sha256:" + "1" * 64
OTHER_SUBJECT = "sha256:" + "2" * 64


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _files(store: ArtifactStore) -> set[str]:
    if not store.root.exists():
        return set()
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def test_models_are_frozen_and_forbid_extra_fields():
    for model in (ToolFindingSummary, ExternalToolImportReceipt, ExternalToolResult):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        ToolFindingSummary(
            provider="sonar",
            provider_finding_id="issue-1",
            rule_id="python:S123",
            severity="high",
            message="claim",
            provider_status="open",
            extra="forbidden",
        )


def test_sonar_import_parses_minimum_issue_fields_and_binds_subject(tmp_path):
    store = _store(tmp_path)
    payload = _payload("assurance_sonar.json")

    result = SonarAdapter.import_bytes(
        payload, expected_subject_digest=SUBJECT, artifact_store=store
    )

    assert result.report.provider == "sonar"
    assert result.report.run_id == "sonar-analysis-001"
    assert result.report.source_ref == "sonar:analysis:sonar-analysis-001"
    assert result.report.status == "success"
    assert result.report.provider_format == "sonar_minimum_v1"
    assert result.report.tool_name == "Sonar"
    assert result.report.project_ref == "codemesh-demo"
    assert result.report.subject_binding_basis == "caller_declared"
    assert result.report.findings[0].rule_id == "python:S123"
    assert result.report.findings[0].file_path == "src/app.py"
    assert result.report.findings[0].start_line == 12
    assert result.findings[0].reviewer_role == "architecture"
    assert result.findings[0].basis == "inferred"
    assert result.findings[0].evidence_refs == (result.evidence.evidence_id,)
    assert result.evidence.subject_digest == SUBJECT
    assert result.evidence.kind == "external_tool_report"
    assert result.evidence.producer == "adapter.external.sonar"
    assert result.evidence.trust_level == "declared"
    assert result.evidence.artifact_digest == _sha256(payload)
    assert result.receipt.effective_trust_level == "declared"
    assert result.receipt.status_semantics == "analysis_execution_only"
    assert store.get_bytes(result.receipt.raw_payload_artifact_digest) == payload


def test_codeql_import_requires_sarif_2_1_and_parses_location(tmp_path):
    result = CodeQLAdapter.import_bytes(
        _payload("assurance_codeql.sarif"),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path),
    )

    assert result.report.provider == "codeql"
    assert result.report.run_id == "codeql-run-001"
    assert result.report.source_ref == "codeql:run:codeql-run-001"
    assert result.report.status == "success"
    assert result.report.provider_format == "sarif_2.1.0"
    assert result.report.tool_name == "CodeQL"
    assert result.report.tool_version == "2.17.0"
    assert result.report.subject_binding_basis == "caller_declared"
    sql_finding = next(
        item for item in result.report.findings if item.rule_id == "py/sql-injection"
    )
    assert sql_finding.severity == "critical"
    assert sql_finding.file_path == "src/app.py"
    assert sql_finding.start_line == 42
    assert result.evidence.status == "success"


@pytest.mark.parametrize(
    ("adapter", "fixture", "provider", "run_id"),
    [
        (HarnessAdapter, "assurance_harness.json", "harness", "harness-run-001"),
        (CortexAdapter, "assurance_cortex.json", "cortex", "cortex-run-001"),
    ],
)
def test_codemesh_envelopes_are_strict_and_unified(
    tmp_path, adapter, fixture, provider, run_id
):
    result = adapter.import_bytes(
        _payload(fixture),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path),
    )

    assert result.report.provider == provider
    assert result.report.run_id == run_id
    assert result.report.status == "success"
    assert result.report.subject_binding_basis == "embedded_declared"
    assert result.receipt.source_ref == result.report.source_ref
    assert result.evidence.producer == f"adapter.external.{provider}"
    assert result.evidence.trust_level == "declared"
    assert all(finding.subject_digest == SUBJECT for finding in result.findings)


def test_import_is_byte_stable_and_replay_does_not_create_new_artifacts(tmp_path):
    payload = _payload("assurance_codeql.sarif")
    store = _store(tmp_path)
    first = CodeQLAdapter.import_bytes(
        payload, expected_subject_digest=SUBJECT, artifact_store=store
    )
    before = _files(store)
    second = CodeQLAdapter.import_bytes(
        payload, expected_subject_digest=SUBJECT, artifact_store=store
    )

    assert second == first
    assert _files(store) == before
    assert store.get_bytes(first.receipt.raw_payload_artifact_digest) == payload


def test_subject_mismatch_does_not_persist_raw_payload(tmp_path):
    store = _store(tmp_path)
    before = _files(store)

    with pytest.raises(ExternalToolSubjectMismatch, match="subject"):
        HarnessAdapter.import_bytes(
            _payload("assurance_harness.json"),
            expected_subject_digest=OTHER_SUBJECT,
            artifact_store=store,
        )

    assert _files(store) == before


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.replace(
            b'"analysisId": "sonar-analysis-001"',
            b'"analysisId": "sonar-analysis-001", "analysisId": "sonar-analysis-001"',
        ),
        lambda payload: payload.replace(b'"line": 12', b'"line": NaN'),
        lambda payload: json.dumps(
            {**json.loads(payload), "unexpected": True},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ],
    ids=["duplicate-key", "nan", "extra-field"],
)
def test_sonar_malformed_payload_fails_closed_without_raw_persistence(
    tmp_path, mutator
):
    store = _store(tmp_path)
    before = _files(store)

    with pytest.raises(ExternalToolPayloadError):
        SonarAdapter.import_bytes(
            mutator(_payload("assurance_sonar.json")),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )

    assert _files(store) == before


def test_unknown_provider_status_and_sarif_version_fail_closed(tmp_path):
    sonar = json.loads(_payload("assurance_sonar.json"))
    sonar["status"] = "RUNNING"
    with pytest.raises(ExternalToolPayloadError):
        SonarAdapter.import_bytes(
            json.dumps(sonar, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path / "sonar"),
        )

    codeql = json.loads(_payload("assurance_codeql.sarif"))
    codeql["version"] = "2.0.0"
    with pytest.raises(ExternalToolPayloadError):
        CodeQLAdapter.import_bytes(
            json.dumps(codeql, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path / "codeql"),
        )


def test_non_codeql_sarif_cannot_be_relabelled_as_codeql(tmp_path):
    data = json.loads(_payload("assurance_codeql.sarif"))
    data["runs"][0]["tool"]["driver"]["name"] = "Not-CodeQL"

    with pytest.raises(ExternalToolPayloadError, match="CodeQL"):
        CodeQLAdapter.import_bytes(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path),
        )


def test_codeql_identity_is_stable_when_result_order_changes(tmp_path):
    original = json.loads(_payload("assurance_codeql.sarif"))
    reordered = json.loads(_payload("assurance_codeql.sarif"))
    reordered["runs"][0]["results"].reverse()

    first = CodeQLAdapter.import_bytes(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode(),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path / "first"),
    )
    second = CodeQLAdapter.import_bytes(
        json.dumps(reordered, sort_keys=True, separators=(",", ":")).encode(),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path / "second"),
    )

    assert [item.provider_finding_id for item in first.report.findings] == [
        item.provider_finding_id for item in second.report.findings
    ]

def test_claimed_trust_never_elevates_external_evidence(tmp_path):
    data = json.loads(_payload("assurance_harness.json"))
    data["claimed_trust_level"] = "human_attested"
    result = HarnessAdapter.import_bytes(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path),
    )

    assert result.receipt.claimed_trust_level == "human_attested"
    assert result.receipt.effective_trust_level == "declared"
    assert result.evidence.trust_level == "declared"


def test_provider_status_cannot_close_local_finding_lifecycle(tmp_path):
    sonar = SonarAdapter.import_bytes(
        _payload("assurance_sonar.json"),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path / "sonar"),
    )
    resolved = next(
        item for item in sonar.report.findings if item.provider_status == "resolved"
    )
    canonical = next(
        item for item in sonar.findings if resolved.message == item.claim
    )
    assert canonical.status == "open"

    harness_data = json.loads(_payload("assurance_harness.json"))
    harness_data["findings"][0]["status"] = "dismissed"
    harness = HarnessAdapter.import_bytes(
        json.dumps(harness_data, sort_keys=True, separators=(",", ":")).encode(),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path / "harness"),
    )
    assert harness.report.findings[0].provider_status == "dismissed"
    assert harness.findings[0].status == "open"


def test_native_report_subject_binding_is_explicitly_caller_declared(tmp_path):
    payload = _payload("assurance_sonar.json")
    first = SonarAdapter.import_bytes(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path / "first"),
    )
    second = SonarAdapter.import_bytes(
        payload,
        expected_subject_digest=OTHER_SUBJECT,
        artifact_store=_store(tmp_path / "second"),
    )

    assert first.report.subject_binding_basis == "caller_declared"
    assert second.report.subject_binding_basis == "caller_declared"
    assert first.evidence.trust_level == second.evidence.trust_level == "declared"
    assert first.evidence.subject_digest == SUBJECT
    assert second.evidence.subject_digest == OTHER_SUBJECT


def test_provider_cannot_claim_acceptance_and_source_binding_is_strict(tmp_path):
    data = json.loads(_payload("assurance_harness.json"))
    data["gate_outcome"] = "ACCEPTED"
    with pytest.raises(ExternalToolPayloadError):
        HarnessAdapter.import_bytes(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path / "gate"),
        )

    data = json.loads(_payload("assurance_harness.json"))
    data["source_ref"] = "cortex:run:harness-run-001"
    with pytest.raises(ExternalToolPayloadError):
        HarnessAdapter.import_bytes(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path / "source"),
        )


def test_envelope_provider_mismatch_cannot_be_rebound_by_adapter(tmp_path):
    data = json.loads(_payload("assurance_harness.json"))
    data["provider"] = "cortex"
    with pytest.raises(ExternalToolPayloadError):
        HarnessAdapter.import_bytes(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode(),
            expected_subject_digest=SUBJECT,
            artifact_store=_store(tmp_path),
        )


def test_result_rejects_forged_receipt_or_finding_bindings(tmp_path):
    store = _store(tmp_path)
    result = SonarAdapter.import_bytes(
        _payload("assurance_sonar.json"),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    forged_receipt = result.receipt.model_copy(
        update={"canonical_report_digest": "sha256:" + "d" * 64}
    )
    with pytest.raises(ValidationError):
        ExternalToolResult(
            report=result.report,
            receipt=forged_receipt,
            evidence=result.evidence,
            findings=result.findings,
        )

    result.verify_against_store(store)
    with pytest.raises(ExternalToolArtifactError):
        result.verify_against_store(_store(tmp_path / "empty"))

    forged_finding = result.findings[0].model_copy(
        update={"evidence_refs": ("ev_forged",)}
    )
    with pytest.raises(ValidationError):
        ExternalToolResult(
            report=result.report,
            receipt=result.receipt,
            evidence=result.evidence,
            findings=(forged_finding,) + result.findings[1:],
        )


def test_no_network_or_subprocess_imports_in_adapter():
    source = Path(__file__).parents[1].joinpath(
        "assurance", "integrations", "external_tools.py"
    ).read_text(encoding="utf-8")
    assert "http" not in source.lower()
    assert "subprocess" not in source
    assert "urllib" not in source
