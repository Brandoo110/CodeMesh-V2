"""Focused Repository tests for the C5a2c remediation read seams."""

from __future__ import annotations

import asyncio
import json
import hashlib
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from assurance.contracts import PolicyDecision
from assurance.remediation import (
    PreparedRemediationHandoff,
    RemediationResult,
    RemediationStatus,
)
from web.assurance_remediation import (
    AssuranceRemediationRequest,
    AssuranceRemediationNotAppliedError,
    AssuranceRemediationService,
)
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebNotFoundError,
)
from web.routes.assurance_runs import get_assurance_run_client
from web.routes.assurance_lifecycle import _map_remediation_exception
from web.server import create_app
from tests.test_web.test_assurance_run_store import (
    _db_rows,
    _ExplodingFreshness,
    _fresh_repository,
    _prepared_success,
)


def _seed_remediation(tmp_path, monkeypatch):
    baseline, changed_bundle, request, handoff = _prepared_success(
        tmp_path, monkeypatch
    )
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    repository._store.append_policy_decision(
        baseline.case.case_id,
        PolicyDecision(
            decision_id="policy-remediation-blocked",
            subject_digest=baseline.subject.subject_digest,
            policy_version=baseline.subject.policy_version,
            rules_digest=baseline.policy.decision.rules_digest,
            outcome="BLOCKED",
            reason_codes=("REVIEW_REQUIRED",),
            evaluated_at=baseline.completed_at + timedelta(seconds=1),
        ),
    )
    return repository, baseline, changed_bundle, request, handoff


def _append_policy(repository, baseline, *, outcome, required_human_role=None):
    repository._store.append_policy_decision(
        baseline.case.case_id,
        PolicyDecision(
            decision_id=f"policy-current-{outcome.lower()}",
            subject_digest=baseline.subject.subject_digest,
            policy_version=baseline.subject.policy_version,
            rules_digest=baseline.policy.decision.rules_digest,
            outcome=outcome,
            reason_codes=("REVIEW_REQUIRED",)
            if outcome in {"STALE", "BLOCKED", "NEEDS_HUMAN"}
            else (),
            required_human_role=required_human_role,
            evaluated_at=baseline.completed_at + timedelta(seconds=2),
        ),
    )


def test_default_remediation_post_is_stably_not_configured():
    client = TestClient(create_app(), client=("127.0.0.1", 8000))

    response = client.post(
        "/api/assurance/changes/case-1/remediations",
        headers={"Idempotency-Key": "remediate:default"},
        json={
            "remediation_id": "remediation-1",
            "human_selected_finding_id": "finding-1",
            "requested_by": "alice",
            "requested_at": "2026-08-26T12:00:00Z",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "ASSURANCE_REMEDIATION_NOT_CONFIGURED",
        "message": "assurance remediation service is not configured",
        "reason_codes": ["NOT_CONFIGURED"],
    }


def test_configured_remediation_post_commits_and_replays_without_reprepare(
    tmp_path, monkeypatch
):
    repository, _, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    prepare_calls = []

    def request_factory(context, intent, **_):
        return request

    async def prepare_callback(request, context):
        prepare_calls.append(request.remediation_id)
        return handoff

    service = AssuranceRemediationService(
        repository,
        request_factory=request_factory,
        prepare_callback=prepare_callback,
    )
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=object(),
            repository=repository,
            remediation_service=service,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    payload = {
        "remediation_id": request.remediation_id,
        "human_selected_finding_id": request.human_selected_finding_id,
        "requested_by": request.requested_by,
        "requested_at": request.requested_at.isoformat(),
    }

    try:
        with TestClient(app) as client:
            first = client.post(
                f"/api/assurance/changes/{request.old_case_id}/remediations",
                headers={"Idempotency-Key": "remediate:web"},
                json=payload,
            )
            replay = client.post(
                f"/api/assurance/changes/{request.old_case_id}/remediations",
                headers={"Idempotency-Key": "remediate:web"},
                json=payload,
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert first.json()["cached"] is False
    assert first.json()["case_view"]["case"]["state"] == "DRAFT"
    assert replay.status_code == 200
    assert replay.json()["cached"] is True
    assert replay.json()["receipt"] == first.json()["receipt"]
    assert replay.json()["case_view"]["case"]["state"] == "DRAFT"
    assert prepare_calls == [request.remediation_id]


def test_non_success_remediation_post_is_not_applied_without_commit(
    tmp_path, monkeypatch
):
    repository, _, _, request, _ = _seed_remediation(tmp_path, monkeypatch)
    non_success = RemediationResult(
        remediation_id=request.remediation_id,
        human_selected_finding_id=request.human_selected_finding_id,
        status=RemediationStatus.FAILED,
        reason_code="secret:/tmp/internal",
        old_case_id=request.old_case_id,
        old_subject_digest=request.old_subject_digest,
        attempts=0,
        validation_calls=1,
    )
    handoff = PreparedRemediationHandoff(result=non_success)
    prepare_calls = []
    commit_calls = []
    original_commit = repository.commit_prepared_remediation

    def request_factory(context, intent, **_):
        return request

    async def prepare_callback(request, context):
        prepare_calls.append(request.remediation_id)
        return handoff

    def commit_prepared_remediation(*args, **kwargs):
        commit_calls.append((args, kwargs))
        return original_commit(*args, **kwargs)

    repository.commit_prepared_remediation = commit_prepared_remediation
    service = AssuranceRemediationService(
        repository,
        request_factory=request_factory,
        prepare_callback=prepare_callback,
    )
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=object(),
            repository=repository,
            remediation_service=service,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    payload = {
        "remediation_id": request.remediation_id,
        "human_selected_finding_id": request.human_selected_finding_id,
        "requested_by": request.requested_by,
        "requested_at": request.requested_at.isoformat(),
    }
    tables = (
        "assurance_cases",
        "assurance_case_events",
        "assurance_web_cases",
        "assurance_web_runs",
        "assurance_remediations",
        "assurance_web_idempotency",
    )

    def snapshot():
        return {
            table: [
                tuple(row)
                for row in _db_rows(repository, f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }

    before = snapshot()

    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/assurance/changes/{request.old_case_id}/remediations",
                headers={"Idempotency-Key": "remediate:not-applied"},
                json=payload,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "secret:/tmp/internal" not in response.text
    assert response.json() == {
        "code": "ASSURANCE_REMEDIATION_NOT_APPLIED",
        "message": "assurance remediation was not applied",
        "reason_codes": [
            "REMEDIATION_NOT_APPLIED",
            RemediationStatus.FAILED.value,
            "PREPARATION_NOT_APPLIED",
        ],
    }
    assert prepare_calls == [request.remediation_id]
    assert commit_calls == []
    assert snapshot() == before


@pytest.mark.parametrize(
    ("reason_code", "public_reason_code"),
    (
        (
            "agent_error:RemediationAgentBudgetError",
            "AGENT_BUDGET_ERROR",
        ),
        (
            "agent_error:RemediationAgentProtocolError",
            "AGENT_PROTOCOL_ERROR",
        ),
        ("agent_error:WorkspaceViolation", "WORKSPACE_ERROR"),
        ("agent_error:ValueError", "AGENT_VALUE_ERROR"),
        ("agent_error:TypeError", "AGENT_TYPE_ERROR"),
        ("agent_error:ValidationError", "AGENT_VALIDATION_ERROR"),
        ("agent_error:secret:/tmp/internal", "AGENT_ERROR"),
    ),
)
def test_remediation_agent_error_reason_codes_are_fixed_and_non_sensitive(
    reason_code, public_reason_code
):
    response = _map_remediation_exception(
        AssuranceRemediationNotAppliedError(
            status=RemediationStatus.FAILED,
            reason_code=reason_code,
        )
    )

    assert response.status_code == 409
    payload = json.loads(response.body.decode("utf-8"))
    assert payload == {
        "code": "ASSURANCE_REMEDIATION_NOT_APPLIED",
        "message": "assurance remediation was not applied",
        "reason_codes": [
            "REMEDIATION_NOT_APPLIED",
            RemediationStatus.FAILED.value,
            public_reason_code,
        ],
    }
    assert reason_code not in response.body.decode("utf-8")


def test_direct_remediation_post_rejects_policy_pass_before_prepare(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    _append_policy(repository, baseline, outcome="PASS")
    request_calls = []
    prepare_calls = []

    def request_factory(context, intent, **_):
        request_calls.append((context, intent))
        return request

    async def prepare_callback(request, context):
        prepare_calls.append(request.remediation_id)
        return handoff

    service = AssuranceRemediationService(
        repository,
        request_factory=request_factory,
        prepare_callback=prepare_callback,
    )
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=object(),
            repository=repository,
            remediation_service=service,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    payload = {
        "remediation_id": request.remediation_id,
        "human_selected_finding_id": request.human_selected_finding_id,
        "requested_by": request.requested_by,
        "requested_at": request.requested_at.isoformat(),
    }

    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/assurance/changes/{request.old_case_id}/remediations",
                headers={"Idempotency-Key": "remediate:policy-pass"},
                json=payload,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json() == {
        "code": "ASSURANCE_REMEDIATION_CONFLICT",
        "message": "assurance remediation conflicts with existing state",
        "reason_codes": ["REMEDIATION_CONFLICT"],
    }
    assert request_calls == []
    assert prepare_calls == []


def test_eligible_inferred_finding_with_needs_human_enters_prepared_flow(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    _append_policy(
        repository,
        baseline,
        outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
    )
    prepare_calls = []

    def request_factory(context, intent, **_):
        return request

    async def prepare_callback(request, context):
        prepare_calls.append(request.remediation_id)
        return handoff

    service = AssuranceRemediationService(
        repository,
        request_factory=request_factory,
        prepare_callback=prepare_callback,
    )
    app = create_app(
        assurance_run_dependencies=AssuranceRunWebDependencies(
            service=object(),
            repository=repository,
            remediation_service=service,
        )
    )
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    payload = {
        "remediation_id": request.remediation_id,
        "human_selected_finding_id": request.human_selected_finding_id,
        "requested_by": request.requested_by,
        "requested_at": request.requested_at.isoformat(),
    }

    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/assurance/changes/{request.old_case_id}/remediations",
                headers={"Idempotency-Key": "remediate:needs-human"},
                json=payload,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["cached"] is False
    assert prepare_calls == [request.remediation_id]


def test_commit_rechecks_current_policy_before_writing_after_context_load(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    repository.load_remediation_context(
        request.old_case_id, request.human_selected_finding_id
    )
    _append_policy(repository, baseline, outcome="PASS")

    with pytest.raises(AssuranceWebConflictError, match="eligible"):
        repository.commit_prepared_remediation(
            request,
            handoff,
            idempotency_key="remediate:policy-toctou",
        )

    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0


def test_replay_conflict_with_invalid_projection_reraises_conflict():
    class InvalidProjectionRepository:
        def load_remediation_context(self, case_id, human_selected_finding_id):
            raise AssuranceWebConflictError("old Case is already invalidated")

        def lookup_remediation_replay(self, request, idempotency_key):
            pytest.fail("invalid projection must not reach replay lookup")

        def commit_prepared_remediation(self, request, handoff, idempotency_key):
            pytest.fail("invalid projection must not reach commit")

        def get_change(self, case_id):
            return {"case": "not-an-acceptance-case"}

    prepare_calls = []

    def prepare_callback(**_):
        prepare_calls.append(True)

    service = AssuranceRemediationService(
        InvalidProjectionRepository(),
        prepare_callback=prepare_callback,
    )
    intent = AssuranceRemediationRequest(
        remediation_id="remediation-invalid-projection",
        human_selected_finding_id="finding-1",
        requested_by="alice",
        requested_at="2026-08-26T12:00:00Z",
    )

    with pytest.raises(AssuranceWebConflictError, match="already invalidated"):
        asyncio.run(
            service.remediate(
                "case-1",
                intent,
                idempotency_key="remediate:invalid-projection",
            )
        )
    assert prepare_calls == []


def test_load_remediation_context_uses_immutable_run_not_web_projection(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, _ = _seed_remediation(tmp_path, monkeypatch)
    forged = baseline.findings[0].model_copy(update={"claim": "forged projection"})
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute(
            "UPDATE assurance_web_cases SET findings_json = ? WHERE case_id = ?",
            (json.dumps([forged.model_dump(mode="json")]), baseline.case.case_id),
        )
        conn.commit()
    finally:
        conn.close()

    context = repository.load_remediation_context(
        request.old_case_id, request.human_selected_finding_id
    )

    assert context.old_subject_digest == baseline.subject.subject_digest
    assert context.baseline_bundle == baseline
    assert context.selected_finding == baseline.findings[0]
    assert context.source_binding == baseline.freshness_source_binding


def test_load_remediation_context_rejects_run_binding_drift_with_repaired_pointer(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, _ = _seed_remediation(tmp_path, monkeypatch)
    conn = sqlite3.connect(repository._db_path)
    try:
        run_row = conn.execute(
            "SELECT bundle_json FROM assurance_web_runs WHERE idempotency_key = ?",
            (baseline.idempotency_key,),
        ).fetchone()
        pointer_row = conn.execute(
            "SELECT result_json FROM assurance_web_idempotency WHERE idempotency_key = ?",
            (baseline.idempotency_key,),
        ).fetchone()
        bundle = json.loads(run_row[0])
        bundle["binding"]["waiver_id"] = "forged-waiver"
        bundle["binding"]["waiver_expires_at"] = "2030-01-01T00:00:00Z"
        encoded_bundle = json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        pointer = json.loads(pointer_row[0])
        pointer["bundle_digest"] = "sha256:" + hashlib.sha256(
            encoded_bundle.encode("utf-8")
        ).hexdigest()
        conn.execute(
            "UPDATE assurance_web_runs SET bundle_json = ? WHERE idempotency_key = ?",
            (encoded_bundle, baseline.idempotency_key),
        )
        conn.execute(
            "UPDATE assurance_web_idempotency SET result_json = ? WHERE idempotency_key = ?",
            (
                json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                baseline.idempotency_key,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError) as error:
        repository.load_remediation_context(
            request.old_case_id, request.human_selected_finding_id
        )
    assert str(repository._db_path) not in str(error.value)


@pytest.mark.parametrize("tamper", ("missing", "closed", "duplicate", "pointer"))
def test_load_remediation_context_rejects_missing_or_corrupt_authority(
    tmp_path, monkeypatch, tamper
):
    repository, baseline, _, request, _ = _seed_remediation(tmp_path, monkeypatch)
    if tamper == "missing":
        selected_finding_id = "finding-that-does-not-exist"
    else:
        selected_finding_id = request.human_selected_finding_id
        conn = sqlite3.connect(repository._db_path)
        try:
            if tamper == "pointer":
                conn.execute(
                    "DELETE FROM assurance_web_idempotency WHERE idempotency_key = ?",
                    (baseline.idempotency_key,),
                )
            else:
                row = conn.execute(
                    "SELECT bundle_json FROM assurance_web_runs WHERE idempotency_key = ?",
                    (baseline.idempotency_key,),
                ).fetchone()
                bundle = json.loads(row[0])
                if tamper == "closed":
                    bundle["findings"][0]["status"] = "closed"
                    bundle["policy"]["input"]["findings"][0]["status"] = "closed"
                else:
                    bundle["findings"].append(dict(bundle["findings"][0]))
                    bundle["policy"]["input"]["findings"].append(
                        dict(bundle["policy"]["input"]["findings"][0])
                    )
                conn.execute(
                    "UPDATE assurance_web_runs SET bundle_json = ? WHERE idempotency_key = ?",
                    (
                        json.dumps(
                            bundle,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        baseline.idempotency_key,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(AssuranceWebError):
        repository.load_remediation_context(
            request.old_case_id, selected_finding_id
        )


def test_lookup_prepared_remediation_replays_without_writes(
    tmp_path, monkeypatch
):
    repository, _, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    key = "remediate:lookup"
    receipt = repository.commit_prepared_remediation(
        request, handoff, idempotency_key=key
    )
    before = {
        name: _db_rows(repository, f"SELECT COUNT(*) AS count FROM {name}")[0]["count"]
        for name in (
            "assurance_cases",
            "assurance_case_events",
            "assurance_web_cases",
            "assurance_web_runs",
            "assurance_remediations",
            "assurance_web_idempotency",
        )
    }

    replay = repository.lookup_remediation_replay(request, idempotency_key=key)

    assert replay == receipt
    after = {
        name: _db_rows(repository, f"SELECT COUNT(*) AS count FROM {name}")[0]["count"]
        for name in before
    }
    assert after == before


def test_lookup_prepared_remediation_conflicts_on_request_or_operation(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:conflict"
    )

    with pytest.raises(AssuranceWebConflictError):
        repository.lookup_remediation_replay(
            request.model_copy(update={"requested_by": "other-owner"}),
            idempotency_key="remediate:conflict",
        )
    with pytest.raises(AssuranceWebConflictError):
        repository.lookup_remediation_replay(
            request, idempotency_key=baseline.idempotency_key
        )
    with pytest.raises(AssuranceWebConflictError):
        repository.lookup_remediation_replay(
            request.model_copy(update={"remediation_id": "other-remediation"}),
            idempotency_key="remediate:conflict",
        )


def test_lookup_prepared_remediation_returns_none_without_persisted_state(
    tmp_path, monkeypatch
):
    repository, _, _, request, _ = _seed_remediation(tmp_path, monkeypatch)

    assert (
        repository.lookup_remediation_replay(
            request, idempotency_key="remediate:missing"
        )
        is None
    )


def test_lookup_remediation_replay_does_not_call_freshness_checker(
    tmp_path, monkeypatch
):
    repository, _, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    key = "remediate:no-freshness"
    receipt = repository.commit_prepared_remediation(request, handoff, idempotency_key=key)
    readonly_repository = type(repository)(
        repository._db_path,
        freshness_checker=_ExplodingFreshness(),
        live_required=True,
    )

    assert readonly_repository.lookup_remediation_replay(request, idempotency_key=key) == receipt


def test_lookup_prepared_remediation_rejects_synchronized_cached_and_lineage_tamper(
    tmp_path, monkeypatch
):
    repository, _, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    key = "remediate:tamper"
    repository.commit_prepared_remediation(request, handoff, idempotency_key=key)

    conn = sqlite3.connect(repository._db_path)
    try:
        lineage = conn.execute(
            "SELECT receipt_json FROM assurance_remediations WHERE remediation_id = ?",
            (request.remediation_id,),
        ).fetchone()
        pointer = conn.execute(
            "SELECT result_json FROM assurance_web_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        forged = json.loads(lineage[0])
        forged["new_case_id"] = "forged-new-case"
        encoded = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE assurance_remediations SET new_case_id = ?, receipt_json = ?"
            " WHERE remediation_id = ?",
            (forged["new_case_id"], encoded, request.remediation_id),
        )
        pointer_data = json.loads(pointer[0])
        pointer_data["new_case_id"] = forged["new_case_id"]
        conn.execute(
            "UPDATE assurance_web_idempotency SET result_json = ? WHERE idempotency_key = ?",
            (json.dumps(pointer_data, sort_keys=True, separators=(",", ":")), key),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError):
        repository.lookup_remediation_replay(request, idempotency_key=key)


def test_list_remediations_reads_the_lifecycle_rows_from_the_same_database(
    tmp_path, monkeypatch
):
    repository, baseline, _, request, handoff = _seed_remediation(tmp_path, monkeypatch)
    receipt = repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:list"
    )
    second_repository = type(repository)(repository._db_path)

    assert second_repository.list_remediations(baseline.case.case_id) == [
        receipt.model_dump(mode="json")
    ]
    assert second_repository.list_remediations(receipt.new_case_id) == [
        receipt.model_dump(mode="json")
    ]
    with pytest.raises(AssuranceWebNotFoundError):
        second_repository.list_remediations("case-missing")
