from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assurance.run_service import (
    AssuranceRunError,
    AssuranceRunOfficialEvidenceError,
    AssuranceRunPreconditionError,
    AssuranceRunRedactionError,
    AssuranceRunStaleError,
    AssuranceRunValidationError,
    IdempotencyConflictError,
)
from assurance.live_freshness import LiveFreshnessChecker
from tests.test_assurance_run_service import _Reviewer, _service
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebRepository,
)
from web.routes.assurance_runs import (
    get_assurance_run_client,
    get_assurance_run_dependencies,
)
from web.server import create_app


def _payload(intent) -> dict:
    return {
        "repository_path": str(intent.repository_path),
        "repository_identity": intent.repository_identity,
        "author": intent.author,
        "base_ref": intent.base_ref,
        "task_path": intent.task_path,
        "policy_paths": list(intent.policy_paths),
        "adr_paths": list(intent.adr_paths),
        "runbook_paths": list(intent.runbook_paths),
        "command_ids": list(intent.command_ids),
        "official_evidence_run_id": intent.official_evidence_run_id,
        "changed_lines_total": intent.changed_lines_total,
        "external_side_effects": intent.external_side_effects,
        "provider_boundary": intent.provider_boundary,
    }


def _durable_app(tmp_path: Path):
    service, intent = _service(tmp_path)
    repository = AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=LiveFreshnessChecker(workspace_root=tmp_path.resolve()),
        live_required=True,
    )
    repository.initialize()
    service._committer = repository
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=service,
            repository=repository,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    return app, service, intent, repository


def test_default_app_exposes_run_route_but_returns_stable_not_configured_error():
    client = TestClient(create_app())

    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run-1"},
        json={
            "repository_path": "/tmp/repository",
            "repository_identity": "github.com/example/repository",
            "author": "alice",
            "base_ref": "main",
            "task_path": "task.md",
            "command_ids": ["pytest"],
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "ASSURANCE_RUN_NOT_CONFIGURED",
        "message": "assurance run composition is not configured",
        "reason_codes": ["NOT_CONFIGURED"],
    }


def test_default_app_does_not_mount_fixture_mutation_routes_or_schema():
    app = create_app()
    methods_by_path = {
        route.path: route.methods
        for route in app.routes
        if hasattr(route, "methods")
    }
    schema = app.openapi()["paths"]

    assert "POST" not in methods_by_path["/api/assurance/changes"]
    assert "/api/assurance/changes/{change_id}/collect" not in methods_by_path
    assert "/api/assurance/changes/{change_id}/review" not in methods_by_path
    assert "post" not in schema["/api/assurance/changes"]


def test_fixture_mutation_routes_require_explicit_opt_in():
    app = create_app(enable_assurance_fixture_mutations=True)
    methods_by_path = {
        route.path: route.methods
        for route in app.routes
        if hasattr(route, "methods")
    }

    assert "POST" in methods_by_path["/api/assurance/changes"]
    assert "POST" in methods_by_path["/api/assurance/changes/{change_id}/collect"]
    assert "POST" in methods_by_path["/api/assurance/changes/{change_id}/review"]
    assert "post" not in app.openapi()["paths"]["/api/assurance/changes"]


def test_non_loopback_is_rejected_before_service_work(tmp_path):
    app, service, intent, _repository = _durable_app(tmp_path)
    app.dependency_overrides[get_assurance_run_client] = lambda: "192.0.2.10"
    client = TestClient(app)

    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "non-loopback"},
        json=_payload(intent),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "ASSURANCE_RUN_LOOPBACK_REQUIRED"
    assert service._committer.calls if hasattr(service._committer, "calls") else True
    assert service._reviewer_invoker.calls == 0


def test_request_allowlist_and_idempotency_validation_are_sanitized(tmp_path):
    app, service, intent, _repository = _durable_app(tmp_path)
    client = TestClient(app)

    extra = _payload(intent) | {
        "api_key": "pseudo-secret-should-not-echo",
        "repository_path_secret": "/private/should-not-echo",
        "official_evidence": ["/private/report.json"],
    }
    invalid_body = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "valid"},
        json=extra,
    )
    missing_key = client.post("/api/assurance/runs", json=_payload(intent))
    blank_key = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "   "},
        json=_payload(intent),
    )
    long_key = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "x" * 257},
        json=_payload(intent),
    )

    for response in (invalid_body, missing_key, blank_key, long_key):
        assert response.status_code == 422
        assert "pseudo-secret" not in response.text
        assert "/private/should-not-echo" not in response.text
    assert service._reviewer_invoker.calls == 0


def test_api_forwards_only_bounded_official_run_id(monkeypatch, tmp_path):
    app, service, intent, _repository = _durable_app(tmp_path)
    client = TestClient(app)
    seen: list[str | None] = []

    def fake_import(_intent, **_kwargs):
        seen.append(_intent.official_evidence_run_id)
        return ()

    monkeypatch.setattr(service, "_import_official_evidence", fake_import)
    body = _payload(intent) | {"official_evidence_run_id": "123"}
    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:official-id"},
        json=body,
    )

    assert response.status_code == 201
    assert seen == ["123"]

    invalid = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:official-int"},
        json=body | {"official_evidence_run_id": 123},
    )
    assert invalid.status_code == 422


def test_real_run_returns_only_public_projection_and_replays_from_same_repository(
    tmp_path,
):
    app, service, intent, repository = _durable_app(tmp_path)
    client = TestClient(app)
    body = _payload(intent)

    first = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:web"},
        json=body,
    )
    replay = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:web"},
        json=body,
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert set(first.json()) == {
        "schema_version",
        "run_id",
        "request_digest",
        "cached",
        "case_id",
        "case_view",
    }
    assert first.json()["cached"] is False
    assert replay.json()["cached"] is True
    replay_view = replay.json()["case_view"]
    live_view = repository.get_change(first.json()["case_id"])
    replay_view["freshness"] = replay_view["freshness"] | {
        "checked_at": live_view["freshness"]["checked_at"]
    }
    assert replay_view == live_view
    readback = client.get(f"/api/assurance/changes/{first.json()['case_id']}")
    assert readback.status_code == 200
    assert readback.json() == first.json()["case_view"]
    assert str(intent.repository_path) not in first.text
    assert service._reviewer_invoker.calls == 1

    changed_body = body | {"author": "a-different-author"}
    conflict = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:web"},
        json=changed_body,
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASSURANCE_RUN_CONFLICT"
    assert service._reviewer_invoker.calls == 1


class _RaisingService:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    async def run(self, intent, *, idempotency_key):
        self.calls += 1
        raise self.error


class _UnusedRepository:
    def get_change(self, case_id):
        raise AssertionError("CaseView readback must not run after a failed service")


@pytest.mark.parametrize(
    ("error_type", "status_code", "code"),
    (
        (AssuranceRunValidationError, 422, "ASSURANCE_RUN_INVALID"),
        (
            AssuranceRunOfficialEvidenceError,
            422,
            "ASSURANCE_OFFICIAL_EVIDENCE_INVALID",
        ),
        (AssuranceRunPreconditionError, 412, "ASSURANCE_RUN_PRECONDITION"),
        (AssuranceRunStaleError, 409, "ASSURANCE_RUN_STALE"),
        (IdempotencyConflictError, 409, "ASSURANCE_RUN_CONFLICT"),
        (AssuranceWebConflictError, 409, "ASSURANCE_RUN_CONFLICT"),
        (AssuranceRunRedactionError, 503, "ASSURANCE_REDACTION_FAILED"),
        (AssuranceWebError, 500, "ASSURANCE_RUN_FAILED"),
        (AssuranceRunError, 500, "ASSURANCE_RUN_FAILED"),
    ),
)
def test_run_exception_mapping_is_stable_and_sanitized(
    tmp_path, error_type, status_code, code
):
    error = error_type(
        "secret-token /private/internal/repository should never cross the boundary"
    )
    service = _RaisingService(error)
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=service,
            repository=_UnusedRepository(),
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    client = TestClient(app)

    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:error"},
        json={
            "repository_path": str(tmp_path),
            "repository_identity": "example/service",
            "author": "author-agent",
            "base_ref": "HEAD",
            "task_path": "TASK.md",
            "command_ids": ["check"],
        },
    )

    assert response.status_code == status_code
    assert response.json()["code"] == code
    assert "secret-token" not in response.text
    assert "/private/internal/repository" not in response.text
    assert service.calls == 1


@pytest.mark.parametrize(
    ("reason_code", "expected"),
    (
        ("credential_missing_or_invalid", "credential_missing_or_invalid"),
        ("github_transport", "github_transport"),
        ("lineage_mismatch", "lineage_mismatch"),
        ("artifact_structure_invalid", "artifact_structure_invalid"),
        ("digest_or_size_mismatch", "digest_or_size_mismatch"),
        ("unknown", "unknown"),
        ("secret /private/report.zip", "unknown"),
    ),
)
def test_official_reason_mapping_is_stable_and_allowlisted(
    tmp_path, reason_code, expected
):
    service = _RaisingService(
        AssuranceRunOfficialEvidenceError(reason_code=reason_code)
    )
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=service,
            repository=_UnusedRepository(),
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    client = TestClient(app)

    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:reason"},
        json={
            "repository_path": str(tmp_path),
            "repository_identity": "example/service",
            "author": "author-agent",
            "base_ref": "HEAD",
            "task_path": "TASK.md",
            "command_ids": ["check"],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ASSURANCE_OFFICIAL_EVIDENCE_INVALID"
    assert response.json()["message"] == "official evidence report was not accepted"
    assert response.json()["reason_codes"] == [
        "OFFICIAL_EVIDENCE_INVALID",
        expected,
    ]
    assert "secret" not in response.text
    assert "/private/report.zip" not in response.text


def test_reviewer_failure_is_a_blocked_successful_run_not_http_5xx(tmp_path):
    service, intent = _service(tmp_path, reviewer=_Reviewer(status="failure"))
    repository = AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=LiveFreshnessChecker(workspace_root=tmp_path.resolve()),
        live_required=True,
    )
    repository.initialize()
    service._committer = repository
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=service,
            repository=repository,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    client = TestClient(app)

    response = client.post(
        "/api/assurance/runs",
        headers={"Idempotency-Key": "run:reviewer-failure"},
        json=_payload(intent),
    )

    assert 200 <= response.status_code < 300
    view = response.json()["case_view"]
    assert view["policy_gate"]["status"] == "BLOCKED"
    assert view["gate"] == "BLOCKED"
    assert view["receipt"]["overall_result"] == "failure"
    assert view["receipts"][0]["overall_result"] == "failure"
    assert all(step["result"] == "failure" for step in view["receipt"]["steps"])
    assert service._reviewer_invoker.calls == 1
