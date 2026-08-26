"""Focused persistence tests for P7 remediation and release observations."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assurance.artifacts import ArtifactStore
from assurance.contracts import AcceptanceCase, Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.lifecycle_store import (
    LifecycleProjectionError,
    SQLiteAssuranceLifecycleStore,
)
from assurance.release_observation import (
    AlertRecord,
    ReleaseMetrics,
    ReleaseObservation,
    ReleaseObservationImporter,
    ReleaseWindow,
    RollbackRecord,
)
from assurance.remediation import (
    RemediationAttempt,
    RemediationPolicy,
    RemediationRequest,
    RemediationResult,
    RemediationStatus,
    ReviewerRerunReceipt,
)
from assurance.remediation_validation import ValidationResult, ValidationStatus
from assurance.remediation_workspace import WorkspaceGrant
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from assurance.store import SQLiteAssuranceStore, StoreConflictError


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
OLD_SUBJECT = "sha256:" + "1" * 64
TASK_DIGEST = "sha256:" + "2" * 64
RUBRIC_DIGEST = "sha256:" + "3" * 64
ARTIFACT_DIGEST = "sha256:" + "4" * 64
PATCH_DIGEST = "sha256:" + "5" * 64


def _new_subject_input() -> SubjectDigestInput:
    return SubjectDigestInput(
        repository="repo",
        base_revision="base",
        head_revision="repaired",
        normalized_diff_digest=PATCH_DIGEST,
        task_digest=TASK_DIGEST,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )


def _old_case() -> AcceptanceCase:
    return AcceptanceCase(
        case_id="case-old",
        subject_digest=OLD_SUBJECT,
        state="DRAFT",
        created_at=NOW,
        updated_at=NOW,
    )


def _binding(subject_digest: str) -> AcceptanceBinding:
    return AcceptanceBinding(
        subject_digest=subject_digest,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )


def _request() -> RemediationRequest:
    return RemediationRequest(
        remediation_id="remediation-1",
        old_case_id="case-old",
        old_subject_digest=OLD_SUBJECT,
        human_selected_finding_id="finding-1",
        requested_by="human-owner",
        requested_at=NOW + timedelta(minutes=1),
        workspace_grant=WorkspaceGrant(allowed_paths=("src/fix.py",)),
        policy=RemediationPolicy(),
    )


def _finding() -> Finding:
    return Finding(
        finding_id="finding-1",
        subject_digest=OLD_SUBJECT,
        reviewer_role="architecture",
        claim="repair the selected architecture issue",
        evidence_refs=("evidence-1",),
        basis="deterministic",
        severity="high",
        confidence=1.0,
        rubric_hash=RUBRIC_DIGEST,
        model_ref="reviewer-v1",
        status="open",
    )


def _passed_validation() -> ValidationResult:
    return ValidationResult(
        check_id="authoritative",
        status=ValidationStatus.PASSED,
        reason_code="validation_passed",
        exit_code=0,
        duration_ms=1,
        stdout_tail="",
        stderr_tail="",
        truncated=False,
        failure_fingerprint="passed",
    )


def _result() -> RemediationResult:
    subject = _new_subject_input()
    digest = compute_subject_digest(subject)
    validation = _passed_validation()
    attempt = RemediationAttempt(
        attempt=1,
        changed=True,
        patch_digest=PATCH_DIGEST,
        validation_receipts=(validation,),
        status="changed",
    )
    return RemediationResult(
        remediation_id="remediation-1",
        human_selected_finding_id="finding-1",
        status=RemediationStatus.SUCCEEDED,
        reason_code="prepared_new_subject",
        old_case_id="case-old",
        old_subject_digest=OLD_SUBJECT,
        attempts=1,
        validation_calls=2,
        attempt_receipts=(attempt,),
        patch_digests=(PATCH_DIGEST,),
        last_validation=validation,
        new_subject_input=subject,
        new_subject_digest=digest,
        rerun_roles=("architecture",),
        reviewer_receipts=(
            ReviewerRerunReceipt(
                reviewer_role="architecture",
                subject_digest=digest,
                accepted=True,
            ),
        ),
    )


def _transition_inputs():
    request = _request()
    result = _result()
    new_case = AcceptanceCase(
        case_id="case-new",
        subject_digest=result.new_subject_digest,
        state="DRAFT",
        created_at=NOW + timedelta(minutes=2),
        updated_at=NOW + timedelta(minutes=2),
    )
    invalidation = AcceptanceEvent(
        event_id="remediation:remediation-1:invalidate",
        subject_digest=OLD_SUBJECT,
        kind="INVALIDATE",
        reason="remediation:remediation-1:superseded_by:case-new",
        occurred_at=NOW + timedelta(minutes=2),
    )
    return request, result, _finding(), new_case, _binding(result.new_subject_digest), invalidation


def _store(tmp_path) -> SQLiteAssuranceLifecycleStore:
    store = SQLiteAssuranceLifecycleStore(tmp_path / "assurance.sqlite")
    store.initialize()
    store.create_case(_old_case(), _binding(OLD_SUBJECT))
    return store


def _observation(*, observation_id: str = "observation-1", source: str = "manual"):
    return ReleaseObservation(
        observation_id=observation_id,
        subject_digest=OLD_SUBJECT,
        artifact_digest=ARTIFACT_DIGEST,
        environment="production-canary",
        deployment_id="deployment-1",
        cohort="both",
        control_deployment_id="deployment-control",
        window=ReleaseWindow(
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=10),
            completeness="complete",
        ),
        metrics=ReleaseMetrics(
            slo_status="met",
            error_rate_delta=0.0,
            latency_p95_delta_ms=1.0,
            cost_delta_usd=0.01,
        ),
        alert=AlertRecord(state="clear"),
        rollback=RollbackRecord(state="not_executed"),
        outcome="CONFIRMED",
        source=source,
        recorded_by="release-owner",
        recorded_at=NOW + timedelta(minutes=11),
    )


def test_lifecycle_schema_is_additive_and_core_schema_stays_v2(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    core = SQLiteAssuranceStore(db_path)
    core.initialize()
    store = SQLiteAssuranceLifecycleStore(db_path)
    store.initialize()

    assert core.schema_version() == 2
    assert store.lifecycle_schema_version() == 2

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "assurance_remediations" in tables
    assert "assurance_release_observations" in tables


def test_commit_remediation_atomically_invalidates_old_and_creates_new(tmp_path):
    store = _store(tmp_path)
    request, result, finding, new_case, new_binding, invalidation = _transition_inputs()

    receipt = store.commit_remediation(
        request=request,
        result=result,
        selected_finding=finding,
        new_case=new_case,
        new_binding=new_binding,
        invalidation_event=invalidation,
    )

    assert receipt.committed is True
    assert receipt.old_case_id == "case-old"
    assert receipt.new_case_id == "case-new"
    assert store.load_case("case-old").case.state == "INVALIDATED"
    assert store.load_case("case-old").case.invalidation_reason == invalidation.reason
    assert store.load_case("case-new").case.state == "DRAFT"
    assert store.get_binding("case-new") == new_binding
    assert store.get_remediation("remediation-1") == receipt
    assert store.list_remediations("case-old") == (receipt,)

    assert store.commit_remediation(
        request=request,
        result=result,
        selected_finding=finding,
        new_case=new_case,
        new_binding=new_binding,
        invalidation_event=invalidation,
    ) == receipt
    assert len(store.load_case("case-old").applied_events) == 1


def test_invalid_transition_rolls_back_every_remediation_write(tmp_path):
    store = _store(tmp_path)
    request, result, finding, new_case, new_binding, invalidation = _transition_inputs()
    wrong_binding = _binding("sha256:" + "9" * 64)

    with pytest.raises(StoreConflictError):
        store.commit_remediation(
            request=request,
            result=result,
            selected_finding=finding,
            new_case=new_case,
            new_binding=wrong_binding,
            invalidation_event=invalidation,
        )

    assert store.load_case("case-old").case.state == "DRAFT"
    with pytest.raises(Exception):
        store.load_case("case-new")
    assert store.list_remediations("case-old") == ()


def test_remediation_rejects_unselected_or_rejected_finding(tmp_path):
    store = _store(tmp_path)
    request, result, finding, new_case, new_binding, invalidation = _transition_inputs()

    for invalid in (
        finding.model_copy(update={"finding_id": "other"}),
        finding.model_copy(update={"status": "dismissed"}),
        finding.model_copy(update={"reviewer_role": "operability"}),
    ):
        with pytest.raises(StoreConflictError):
            store.commit_remediation(
                request=request,
                result=result,
                selected_finding=invalid,
                new_case=new_case,
                new_binding=new_binding,
                invalidation_event=invalidation,
            )
    assert store.load_case("case-old").case.state == "DRAFT"


def test_manual_release_observation_is_append_only_and_idempotent(tmp_path):
    store = _store(tmp_path)
    observation = _observation()

    first = store.append_release_observation("case-old", observation)
    second = store.append_release_observation("case-old", observation)

    assert first == second
    assert first.case_id == "case-old"
    assert first.observation == observation
    assert first.import_receipt is None
    assert store.list_release_observations("case-old") == (first,)

    changed = observation.model_copy(update={"deployment_id": "different"})
    with pytest.raises(StoreConflictError):
        store.append_release_observation("case-old", changed)


def test_imported_observation_requires_verified_raw_artifact(tmp_path):
    store = _store(tmp_path)
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    imported_observation = _observation(source="import")
    payload = json.dumps(
        imported_observation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    imported = ReleaseObservationImporter.import_bytes(
        payload,
        expected_subject_digest=OLD_SUBJECT,
        artifact_store=artifact_store,
    )

    record = store.append_release_observation(
        "case-old", imported, artifact_store=artifact_store
    )
    assert record.import_receipt == imported.receipt

    missing_store = ArtifactStore(tmp_path / "missing-artifacts")
    with pytest.raises(StoreConflictError):
        store.append_release_observation(
            "case-old",
            imported.model_copy(
                update={
                    "observation": imported.observation.model_copy(
                        update={"observation_id": "observation-missing"}
                    )
                }
            ),
            artifact_store=missing_store,
        )


def test_release_observation_subject_and_corruption_fail_closed(tmp_path):
    store = _store(tmp_path)
    stale = _observation().model_copy(
        update={"subject_digest": "sha256:" + "9" * 64}
    )
    with pytest.raises(StoreConflictError):
        store.append_release_observation("case-old", stale)

    record = store.append_release_observation("case-old", _observation())
    conn = sqlite3.connect(tmp_path / "assurance.sqlite")
    try:
        conn.execute(
            "UPDATE assurance_release_observations SET observation_json='{}' "
            "WHERE observation_id=?",
            (record.observation.observation_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LifecycleProjectionError):
        store.list_release_observations("case-old")
