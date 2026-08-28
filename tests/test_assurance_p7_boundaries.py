"""Focused architecture tests for the P7-03 production boundary contract."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from assurance.contracts import AcceptanceCase
from assurance.release_observation import (
    AlertRecord,
    ReleaseMetrics,
    ReleaseObservation,
    ReleaseWindow,
    RollbackRecord,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_lifecycle import (
    AssuranceLifecycleRepository,
    get_assurance_lifecycle_repository,
)
from web.routes import assurance as assurance_routes
from web.routes import assurance_lifecycle
from web.server import create_app


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "a" * 64
ARTIFACT = "sha256:" + "b" * 64


def _observation(*, source: str = "manual", **overrides) -> ReleaseObservation:
    values = {
        "observation_id": "p7-boundary-observation",
        "subject_digest": SUBJECT,
        "artifact_digest": ARTIFACT,
        "environment": "production-canary",
        "deployment_id": "deployment-declared-only",
        "cohort": "both",
        "control_deployment_id": "control-declared-only",
        "window": ReleaseWindow(
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=10),
            completeness="complete",
        ),
        "metrics": ReleaseMetrics(
            slo_status="met",
            error_rate_delta=0.0,
            latency_p95_delta_ms=1.0,
            cost_delta_usd=0.01,
        ),
        "alert": AlertRecord(state="clear"),
        "rollback": RollbackRecord(state="not_executed"),
        "outcome": "CONFIRMED",
        "source": source,
        "recorded_by": "release-owner",
        "recorded_at": NOW + timedelta(minutes=11),
    }
    values.update(overrides)
    return ReleaseObservation(**values)


def _route_signatures(router) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if hasattr(route, "path") and hasattr(route, "methods")
    }


def _create_accepted_case(repository: AssuranceLifecycleRepository) -> str:
    """Create one accepted case through the public lifecycle store seam."""

    case_id = "p7-accepted-without-release-observation"
    repository.store.create_case(
        AcceptanceCase(
            case_id=case_id,
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
    repository.store.append_event(
        case_id,
        AcceptanceEvent(
            event_id="p7-collect-evidence",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=("evidence-1",),
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )
    repository.store.append_event(
        case_id,
        AcceptanceEvent(
            event_id="p7-accept",
            subject_digest=SUBJECT,
            kind="ACCEPT",
            policy_decision_refs=("policy-1",),
            human_decision_refs=("human-1",),
            occurred_at=NOW + timedelta(minutes=2),
        ),
    )
    assert repository.store.load_case(case_id).case.state == "ACCEPTED"
    return case_id


def test_p7_router_exposes_only_declared_observation_and_read_routes():
    lifecycle_routes = _route_signatures(assurance_lifecycle.router)
    assert lifecycle_routes == {
        (
            "/assurance/changes/{case_id}/release-observations/manual",
            ("POST",),
        ),
        (
            "/assurance/changes/{case_id}/release-observations/import",
            ("POST",),
        ),
        (
            "/assurance/changes/{case_id}/release-observations",
            ("GET",),
        ),
        ("/assurance/changes/{case_id}/remediations", ("GET",)),
        ("/assurance/changes/{case_id}/remediations", ("POST",)),
    }

    operation_words = (
        "deploy",
        "rollback",
        "production-operation",
        "production_operation",
    )
    all_assurance_routes = (
        _route_signatures(assurance_routes.router)
        | lifecycle_routes
    )
    assert all(
        not any(word in path.lower() for word in operation_words)
        for path, _ in all_assurance_routes
    )


def test_accepted_case_does_not_create_release_observation(tmp_path):
    repository = AssuranceLifecycleRepository(
        tmp_path / "assurance.sqlite",
        artifact_root=tmp_path / "artifacts",
    )
    repository.initialize()
    case_id = _create_accepted_case(repository)

    app = create_app()
    app.dependency_overrides[get_assurance_lifecycle_repository] = lambda: repository
    try:
        response = TestClient(app).get(
            f"/api/assurance/changes/{case_id}/release-observations"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []
    assert repository.list_release_observations(case_id) == []


def test_release_observation_source_and_trust_are_closed_types():
    assert get_args(ReleaseObservation.model_fields["source"].annotation) == (
        "manual",
        "import",
    )
    assert get_args(
        ReleaseObservation.model_fields["trust_level"].annotation
    ) == ("declared",)

    for source in ("manual", "import"):
        observation = _observation(source=source)
        assert observation.source == source
        assert observation.trust_level == "declared"

    with pytest.raises(ValidationError):
        _observation(source="monitoring")
    with pytest.raises(ValidationError):
        _observation(trust_level="observed")


def test_p7_boundary_modules_do_not_import_external_operation_clients():
    # This is deliberately scoped to the P7-03 release-observation seam. The
    # separate bounded remediation validator has its own host-process contract.
    module_paths = (
        ROOT / "assurance" / "release_observation.py",
        ROOT / "assurance" / "lifecycle_store.py",
        ROOT / "web" / "assurance_lifecycle.py",
        ROOT / "web" / "assurance_remediation.py",
        ROOT / "web" / "routes" / "assurance_lifecycle.py",
    )
    forbidden_roots = {
        "aiohttp",
        "ansible",
        "azure",
        "boto3",
        "botocore",
        "docker",
        "fabric",
        "github",
        "gitlab",
        "google",
        "grpc",
        "helm",
        "httpx",
        "kubernetes",
        "paramiko",
        "pulumi",
        "requests",
        "socket",
        "subprocess",
        "terraform",
        "urllib",
    }

    for path in module_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        assert imported_roots.isdisjoint(forbidden_roots), (
            f"{path} imports forbidden external operation client(s): "
            f"{sorted(imported_roots & forbidden_roots)}"
        )
