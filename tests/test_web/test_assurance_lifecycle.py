"""Focused API tests for manual/import release observations and P7 lineage."""

import base64
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from assurance.contracts import AcceptanceCase
from assurance.release_observation import (
    AlertRecord,
    ReleaseMetrics,
    ReleaseObservation,
    ReleaseWindow,
    RollbackRecord,
)
from assurance.state_machine import AcceptanceBinding
from web.assurance_lifecycle import (
    AssuranceLifecycleRepository,
    get_assurance_lifecycle_repository,
)
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.assurance_store import AssuranceWebRepository
from web.server import create_app


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "1" * 64
ARTIFACT = "sha256:" + "2" * 64


def _observation(*, source: str, observation_id: str) -> ReleaseObservation:
    return ReleaseObservation(
        observation_id=observation_id,
        subject_digest=SUBJECT,
        artifact_digest=ARTIFACT,
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


def _setup(tmp_path):
    repository = AssuranceLifecycleRepository(
        tmp_path / "assurance.sqlite",
        artifact_root=tmp_path / "artifacts",
    )
    repository.initialize()
    repository.store.create_case(
        AcceptanceCase(
            case_id="case-1",
            subject_digest=SUBJECT,
            state="DRAFT",
            created_at=NOW,
            updated_at=NOW,
        ),
        AcceptanceBinding(
            subject_digest=SUBJECT,
            policy_version="policy-v1",
            rubric_version="rubric-v1",
        ),
    )
    web_repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    web_repository.initialize()
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=object(),
            repository=web_repository,
        )
    )
    app.dependency_overrides[get_assurance_lifecycle_repository] = lambda: repository
    return repository, app, TestClient(app, client=("127.0.0.1", 8000))


def test_manual_observation_round_trip_and_exact_retry(tmp_path):
    _, app, client = _setup(tmp_path)
    observation = _observation(source="manual", observation_id="manual-1")

    first = client.post(
        "/api/assurance/changes/case-1/release-observations/manual",
        json=observation.model_dump(mode="json"),
    )
    retry = client.post(
        "/api/assurance/changes/case-1/release-observations/manual",
        json=observation.model_dump(mode="json"),
    )
    listed = client.get(
        "/api/assurance/changes/case-1/release-observations"
    )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert first.json() == retry.json()
    assert listed.status_code == 200
    assert listed.json() == [first.json()]
    app.dependency_overrides.clear()


def test_import_endpoint_persists_exact_raw_bytes_as_declared(tmp_path):
    repository, app, client = _setup(tmp_path)
    observation = _observation(source="import", observation_id="import-1")
    payload = json.dumps(
        observation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/api/assurance/changes/case-1/release-observations/import",
        json={"payload_base64": base64.b64encode(payload).decode("ascii")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["observation"]["source"] == "import"
    assert body["observation"]["trust_level"] == "declared"
    assert body["import_receipt"]["effective_trust_level"] == "declared"
    raw_digest = body["import_receipt"]["payload_digest"]
    assert repository.artifact_store.get_bytes(raw_digest) == payload
    app.dependency_overrides.clear()


def test_import_rejects_stale_subject_and_invalid_base64(tmp_path):
    _, app, client = _setup(tmp_path)
    stale = _observation(source="import", observation_id="stale").model_copy(
        update={"subject_digest": "sha256:" + "9" * 64}
    )
    payload = json.dumps(stale.model_dump(mode="json")).encode()

    stale_response = client.post(
        "/api/assurance/changes/case-1/release-observations/import",
        json={"payload_base64": base64.b64encode(payload).decode("ascii")},
    )
    invalid_response = client.post(
        "/api/assurance/changes/case-1/release-observations/import",
        json={"payload_base64": "%%%"},
    )

    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "STALE_SUBJECT"
    assert invalid_response.status_code == 422
    assert invalid_response.json()["detail"]["code"] == "INVALID_IMPORT_PAYLOAD"
    app.dependency_overrides.clear()


def test_remediation_lineage_endpoint_uses_composed_repository(tmp_path):
    _, app, client = _setup(tmp_path)

    response = client.get("/api/assurance/changes/case-1/remediations")
    unsupported = client.post(
        "/api/assurance/changes/case-1/remediations",
        headers={"Idempotency-Key": "remediate:unconfigured"},
        json={
            "remediation_id": "remediation-1",
            "human_selected_finding_id": "finding-1",
            "requested_by": "alice",
            "requested_at": "2026-08-26T12:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json() == []
    assert unsupported.status_code == 503
    app.dependency_overrides.clear()
