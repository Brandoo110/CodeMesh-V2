"""Focused contract tests for the manual/import-only release observation atom."""

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance.artifacts import ArtifactStore
from assurance.release_observation import (
    AlertRecord,
    ReleaseMetrics,
    ReleaseObservation,
    ReleaseObservationImportReceipt,
    ReleaseObservationImportResult,
    ReleaseObservationImporter,
    ReleaseObservationPayloadError,
    ReleaseObservationSubjectMismatch,
    ReleaseWindow,
    RollbackRecord,
)


SUBJECT = "sha256:" + "a" * 64
ARTIFACT = "sha256:" + "b" * 64
OTHER_SUBJECT = "sha256:" + "c" * 64
RECORDED_AT = "2026-08-26T09:00:00+00:00"
WINDOW_START = "2026-08-26T08:00:00+00:00"
WINDOW_END = "2026-08-26T08:30:00+00:00"


def _window(**overrides):
    values = {
        "schema_version": "v1",
        "started_at": WINDOW_START,
        "ended_at": WINDOW_END,
        "completeness": "complete",
    }
    values.update(overrides)
    return values


def _metrics(**overrides):
    values = {
        "schema_version": "v1",
        "slo_status": "met",
        "error_rate_delta": 0.001,
        "latency_p95_delta_ms": 2.5,
        "cost_delta_usd": 0.0004,
    }
    values.update(overrides)
    return values


def _alert(**overrides):
    values = {
        "schema_version": "v1",
        "state": "clear",
        "ref": None,
    }
    values.update(overrides)
    return values


def _rollback(**overrides):
    values = {
        "schema_version": "v1",
        "state": "not_executed",
        "ref": None,
    }
    values.update(overrides)
    return values


def _observation(**overrides):
    values = {
        "schema_version": "v1",
        "observation_id": "obs-001",
        "subject_digest": SUBJECT,
        "artifact_digest": ARTIFACT,
        "environment": "production",
        "deployment_id": "deploy-001",
        "cohort": "both",
        "control_deployment_id": "deploy-control-001",
        "window": _window(),
        "metrics": _metrics(),
        "alert": _alert(),
        "rollback": _rollback(),
        "outcome": "CONFIRMED",
        "source": "manual",
        "trust_level": "declared",
        "recorded_by": "release-owner",
        "recorded_at": RECORDED_AT,
    }
    values.update(overrides)
    return ReleaseObservation(**values)


def _import_payload(**overrides) -> bytes:
    values = _observation(source="import").model_dump(mode="json")
    values.update(overrides)
    return json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_nested_contracts_are_frozen_and_extra_forbid():
    models = (
        ReleaseWindow,
        ReleaseMetrics,
        AlertRecord,
        RollbackRecord,
        ReleaseObservation,
        ReleaseObservationImportReceipt,
        ReleaseObservationImportResult,
    )
    for model in models:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"

    observation = _observation()
    with pytest.raises(ValidationError):
        observation.environment = "staging"
    with pytest.raises(ValidationError):
        ReleaseObservation.model_validate(
            {**observation.model_dump(), "unexpected": True}
        )


def test_confirmed_observation_requires_the_complete_release_facts():
    observation = _observation()
    assert observation.outcome == "CONFIRMED"
    assert observation.trust_level == "declared"
    assert ReleaseObservation.model_validate(
        observation.model_dump(mode="json")
    ) == observation

    invalid_confirmed = (
        {"window": _window(completeness="partial")},
        {"cohort": "canary", "control_deployment_id": None},
        {"metrics": _metrics(slo_status="breached")},
        {"metrics": _metrics(error_rate_delta=None)},
        {"metrics": _metrics(latency_p95_delta_ms=None)},
        {"metrics": _metrics(cost_delta_usd=None)},
        {"alert": _alert(state="active", ref="alert-001")},
        {"rollback": _rollback(state="unknown", ref="rollback-unknown")},
    )
    for override in invalid_confirmed:
        with pytest.raises(ValidationError):
            _observation(**override)


def test_rolled_back_requires_executed_rollback_reference():
    observation = _observation(
        outcome="ROLLED_BACK",
        rollback=_rollback(state="executed", ref="rollback-001"),
    )
    assert observation.outcome == "ROLLED_BACK"
    with pytest.raises(ValidationError):
        _observation(
            outcome="ROLLED_BACK",
            rollback=_rollback(state="not_executed"),
        )
    with pytest.raises(ValidationError):
        _observation(
            outcome="CONFIRMED",
            rollback=_rollback(state="executed", ref="rollback-001"),
        )


def test_inconclusive_is_the_only_safe_outcome_when_facts_are_missing():
    observation = _observation(
        cohort="canary",
        control_deployment_id=None,
        window=_window(completeness="partial"),
        metrics=_metrics(
            slo_status="unknown",
            error_rate_delta=None,
            latency_p95_delta_ms=None,
            cost_delta_usd=None,
        ),
        alert=_alert(state="unknown", ref="alert-unknown"),
        rollback=_rollback(state="unknown", ref="rollback-unknown"),
        outcome="INCONCLUSIVE",
    )
    assert observation.outcome == "INCONCLUSIVE"


def test_recorded_at_must_not_precede_the_observation_window_end():
    with pytest.raises(ValidationError):
        _observation(recorded_at=WINDOW_START)


def test_importer_persists_raw_payload_and_replays_canonical_digests(tmp_path):
    payload = _import_payload()
    store = ArtifactStore(Path(tmp_path) / "artifacts")

    first = ReleaseObservationImporter.import_bytes(
        payload, expected_subject_digest=SUBJECT, artifact_store=store
    )
    second = ReleaseObservationImporter.import_bytes(
        payload, expected_subject_digest=SUBJECT, artifact_store=store
    )

    assert first == second
    assert first.observation.source == "import"
    assert first.receipt.payload_digest == _sha256(payload)
    canonical = json.dumps(
        first.observation.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert first.receipt.canonical_payload_digest == _sha256(canonical)
    assert first.receipt.artifact_digest == ARTIFACT
    assert first.receipt.artifact_digest != first.receipt.payload_digest
    assert store.get_bytes(first.receipt.payload_digest) == payload


def test_import_result_recomputes_and_binds_canonical_payload_digest(tmp_path):
    store = ArtifactStore(Path(tmp_path) / "artifacts")
    result = ReleaseObservationImporter.import_bytes(
        _import_payload(), expected_subject_digest=SUBJECT, artifact_store=store
    )
    forged_receipt = result.receipt.model_copy(
        update={"canonical_payload_digest": "sha256:" + "d" * 64}
    )
    with pytest.raises(ValidationError):
        ReleaseObservationImportResult(
            observation=result.observation,
            receipt=forged_receipt,
        )


def test_importer_rejects_stale_subject_and_receipt_cannot_replace_artifact_digest(
    tmp_path,
):
    store = ArtifactStore(Path(tmp_path) / "artifacts")
    with pytest.raises(ReleaseObservationSubjectMismatch):
        ReleaseObservationImporter.import_bytes(
            _import_payload(),
            expected_subject_digest=OTHER_SUBJECT,
            artifact_store=store,
        )

    result = ReleaseObservationImporter.import_bytes(
        _import_payload(), expected_subject_digest=SUBJECT, artifact_store=store
    )
    forged_receipt = result.receipt.model_copy(
        update={"artifact_digest": result.receipt.payload_digest}
    )
    with pytest.raises(ValidationError):
        ReleaseObservationImportResult(
            observation=result.observation,
            receipt=forged_receipt,
        )


def test_importer_rejects_extra_json_fields_and_forbidden_external_primitives():
    payload = json.loads(_import_payload())
    payload["unexpected"] = True
    with pytest.raises(ReleaseObservationPayloadError):
        ReleaseObservationImporter.import_bytes(
            json.dumps(payload).encode("utf-8"),
            expected_subject_digest=SUBJECT,
            artifact_store=ArtifactStore(Path("/tmp/unused-release-observation")),
        )

    source = Path("assurance/release_observation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"requests", "httpx", "urllib", "subprocess", "boto3"}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    assert imported.isdisjoint(forbidden)
