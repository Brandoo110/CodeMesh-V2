"""Focused tests for the pure Assurance CaseView state contract."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from assurance.contracts import AcceptanceCase, Finding, HumanDecision, PolicyDecision
from assurance.lifecycle_store import StoredReleaseObservation
from assurance.release_observation import (
    AlertRecord,
    ReleaseMetrics,
    ReleaseObservation,
    ReleaseWindow,
    RollbackRecord,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_case_view import (
    build_case_view,
    derive_allowed_actions,
    resolve_action,
)
from web.assurance_demo import NEW_CASE_ID, OLD_CASE_ID, seed_assurance_demo
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebRepository,
    get_assurance_repository,
)
from web.server import create_app


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "1" * 64
ARTIFACT = "sha256:" + "2" * 64
RULES = "sha256:" + "3" * 64


def _policy_gate(
    status: str, *, required_human_role: str | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "required_human_role": required_human_role,
    }


def _codes(actions: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(action["code"] for action in actions)


def _finding(
    *,
    finding_id: str = "finding-eligible",
    subject_digest: str = SUBJECT,
    severity: str = "high",
    basis: str = "deterministic",
    status: str = "open",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        subject_digest=subject_digest,
        reviewer_role="architecture",
        claim="the selected change needs repair",
        evidence_refs=("evidence-1",),
        basis=basis,
        severity=severity,
        confidence=1.0,
        rubric_hash=RULES,
        model_ref="reviewer-model",
        status=status,
    )


def _policy(
    *,
    decision_id: str,
    outcome: str,
    evaluated_at: datetime,
    required_human_role: str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        decision_id=decision_id,
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rules_digest=RULES,
        outcome=outcome,
        reason_codes=(
            ("REVIEW_REQUIRED",)
            if outcome in {"STALE", "BLOCKED", "NEEDS_HUMAN"}
            else ()
        ),
        required_human_role=required_human_role,
        waiver_ref="waiver-existing" if outcome == "PASS_WITH_WAIVER" else None,
        evaluated_at=evaluated_at,
    )


def _stored_observation(
    *, observation_id: str, outcome: str, recorded_at: datetime
) -> StoredReleaseObservation:
    observation = ReleaseObservation(
        observation_id=observation_id,
        subject_digest=SUBJECT,
        artifact_digest=ARTIFACT,
        environment="production-canary",
        deployment_id=f"deployment-{observation_id}",
        cohort="both",
        control_deployment_id="deployment-control",
        window=ReleaseWindow(
            started_at=recorded_at - timedelta(minutes=10),
            ended_at=recorded_at - timedelta(minutes=1),
            completeness="complete",
        ),
        metrics=ReleaseMetrics(
            slo_status="met",
            error_rate_delta=0.0,
            latency_p95_delta_ms=1.0,
            cost_delta_usd=0.01,
        ),
        alert=AlertRecord(state="clear"),
        rollback=(
            RollbackRecord(state="executed", ref="rollback://declared")
            if outcome == "ROLLED_BACK"
            else RollbackRecord(state="not_executed")
        ),
        outcome=outcome,
        source="manual",
        recorded_by="release-owner",
        recorded_at=recorded_at,
    )
    return StoredReleaseObservation(
        case_id="case-view",
        observation=observation,
        stored_at=recorded_at,
    )


def _seed_repository_case(
    repository: AssuranceWebRepository,
    *,
    policy_outcome: str,
    risk: str = "medium",
    author: str = "author-agent",
    required_human_role: str | None = None,
    release_status: str = "not-released",
) -> None:
    repository.initialize()
    case = AcceptanceCase(
        case_id="case-view",
        subject_digest=SUBJECT,
        state="DRAFT",
        created_at=NOW,
        updated_at=NOW,
    )
    repository.create_change(
        case,
        AcceptanceBinding(
            subject_digest=SUBJECT,
            policy_version="policy-v1",
            rubric_version="rubric-v1",
        ),
        {
            "author": author,
            "risk": risk,
            "release_status": release_status,
        },
        "seed:create",
        {"case_id": case.case_id},
    )
    repository._store.append_event(
        case.case_id,
        AcceptanceEvent(
            event_id="seed:collect",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=("evidence-1",),
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )
    repository._store.append_policy_decision(
        case.case_id,
        _policy(
            decision_id="policy-latest",
            outcome=policy_outcome,
            required_human_role=required_human_role,
            evaluated_at=NOW + timedelta(minutes=2),
        ),
    )


@pytest.mark.parametrize(
    ("acceptance_state", "policy_status", "expected"),
    (
        ("DRAFT", "NEEDS_HUMAN", ("download_passport",)),
        ("NEEDS_EVIDENCE", "PASS", ("download_passport",)),
        ("ACCEPTED", "PASS", ("download_passport",)),
        ("REJECTED", "BLOCKED", ("download_passport",)),
        ("INVALIDATED", "BLOCKED", ("download_passport",)),
        (
            "EVIDENCE_COLLECTED",
            "NOT_EVALUATED",
            ("download_passport", "reject"),
        ),
        (
            "EVIDENCE_COLLECTED",
            "STALE",
            ("download_passport", "reject"),
        ),
        (
            "EVIDENCE_COLLECTED",
            "BLOCKED",
            ("download_passport", "reject"),
        ),
        (
            "EVIDENCE_COLLECTED",
            "PASS",
            (
                "download_passport",
                "reject",
                "approve",
                "approve_with_conditions",
            ),
        ),
        (
            "EVIDENCE_COLLECTED",
            "NEEDS_HUMAN",
            (
                "download_passport",
                "reject",
                "approve",
                "approve_with_conditions",
                "waiver",
            ),
        ),
        (
            "EVIDENCE_COLLECTED",
            "PASS_WITH_WAIVER",
            ("download_passport", "reject"),
        ),
        (
            "CONFLICTED",
            "PASS",
            ("download_passport", "reject", "approve_with_conditions"),
        ),
        (
            "CONFLICTED",
            "NEEDS_HUMAN",
            (
                "download_passport",
                "reject",
                "approve_with_conditions",
                "waiver",
            ),
        ),
        (
            "CONDITIONAL_ACCEPTED",
            "PASS",
            (
                "download_passport",
                "reject",
                "approve",
                "approve_with_conditions",
            ),
        ),
    ),
)
def test_allowed_actions_follow_three_axis_matrix(
    acceptance_state, policy_status, expected
):
    actions = derive_allowed_actions(
        acceptance_state=acceptance_state,
        policy_gate=_policy_gate(policy_status),
        digest_freshness=True,
        risk="medium",
    )

    assert _codes(actions) == expected


def test_stale_digest_removes_every_decision_action():
    actions = derive_allowed_actions(
        acceptance_state="EVIDENCE_COLLECTED",
        policy_gate=_policy_gate(
            "NEEDS_HUMAN", required_human_role="security_owner"
        ),
        digest_freshness=False,
        risk="critical",
    )

    assert _codes(actions) == ("download_passport",)


def test_approval_action_flags_are_the_post_decision_rules():
    actions = derive_allowed_actions(
        acceptance_state="EVIDENCE_COLLECTED",
        policy_gate=_policy_gate(
            "NEEDS_HUMAN", required_human_role="security_owner"
        ),
        digest_freshness=True,
        risk="high",
    )

    for code in ("approve", "approve_with_conditions", "waiver"):
        action = resolve_action(actions, code)
        assert action == {
            "code": code,
            "required_human_role": "security_owner",
            "self_approval_forbidden": True,
            "high_risk_confirmation_required": True,
        }

    assert resolve_action(actions, "reject") == {
        "code": "reject",
        "required_human_role": None,
        "self_approval_forbidden": False,
        "high_risk_confirmation_required": False,
    }
    assert resolve_action(actions, "download_passport") == {
        "code": "download_passport",
        "required_human_role": None,
        "self_approval_forbidden": False,
        "high_risk_confirmation_required": False,
    }
    assert resolve_action(actions, "not-an-action") is None


def test_case_view_has_explicit_not_evaluated_and_not_observed_axes():
    view = build_case_view(
        case_id="case-view",
        subject_digest=SUBJECT,
        revision=0,
        acceptance_state="DRAFT",
        decisions=(),
        release_observations=(),
        digest_freshness=True,
        risk="medium",
    )

    assert view == {
        "schema_version": "v1",
        "case_id": "case-view",
        "subject_digest": SUBJECT,
        "revision": 0,
        "digest_freshness": True,
        "policy_gate": {
            "status": "NOT_EVALUATED",
            "decision_id": None,
            "reason_codes": [],
            "required_human_role": None,
            "waiver_ref": None,
            "evaluated_at": None,
        },
        "acceptance_state": "DRAFT",
        "release_state": {
            "status": "NOT_OBSERVED",
            "observation_id": None,
            "environment": None,
            "deployment_id": None,
            "source": None,
            "trust_level": None,
            "recorded_at": None,
        },
        "allowed_actions": [
            {
                "code": "download_passport",
                "required_human_role": None,
                "self_approval_forbidden": False,
                "high_risk_confirmation_required": False,
            }
        ],
    }


def test_case_view_uses_latest_policy_and_release_declarations():
    first_policy = _policy(
        decision_id="policy-first",
        outcome="NEEDS_HUMAN",
        required_human_role="security_owner",
        evaluated_at=NOW,
    )
    latest_policy = _policy(
        decision_id="policy-latest",
        outcome="PASS_WITH_WAIVER",
        evaluated_at=NOW + timedelta(minutes=1),
    )
    first_observation = _stored_observation(
        observation_id="first",
        outcome="CONFIRMED",
        recorded_at=NOW + timedelta(minutes=20),
    )
    latest_observation = _stored_observation(
        observation_id="latest",
        outcome="ROLLED_BACK",
        recorded_at=NOW + timedelta(minutes=30),
    )

    view = build_case_view(
        case_id="case-view",
        subject_digest=SUBJECT,
        revision=4,
        acceptance_state="EVIDENCE_COLLECTED",
        decisions=(first_policy, latest_policy),
        release_observations=(
            first_observation.observation,
            latest_observation.observation,
        ),
        digest_freshness=True,
        risk="critical",
    )

    assert view["policy_gate"] == {
        "status": "PASS_WITH_WAIVER",
        "decision_id": "policy-latest",
        "reason_codes": [],
        "required_human_role": None,
        "waiver_ref": "waiver-existing",
        "evaluated_at": latest_policy.model_dump(mode="json")["evaluated_at"],
    }
    assert view["release_state"] == {
        "status": "ROLLED_BACK",
        "observation_id": "latest",
        "environment": "production-canary",
        "deployment_id": "deployment-latest",
        "source": "manual",
        "trust_level": "declared",
        "recorded_at": latest_observation.observation.model_dump(mode="json")[
            "recorded_at"
        ],
    }
    assert _codes(view["allowed_actions"]) == (
        "download_passport",
        "reject",
    )


@pytest.mark.parametrize(
    ("acceptance_state", "policy_status", "digest_freshness", "finding", "expected"),
    (
        ("EVIDENCE_COLLECTED", "BLOCKED", True, _finding(), True),
        ("NEEDS_EVIDENCE", "STALE", True, _finding(basis="inferred", severity="critical"), True),
        ("CONFLICTED", "NEEDS_HUMAN", True, _finding(), True),
        ("CONDITIONAL_ACCEPTED", "BLOCKED", True, _finding(), True),
        ("REJECTED", "STALE", True, _finding(), True),
        ("DRAFT", "BLOCKED", True, _finding(), False),
        ("ACCEPTED", "BLOCKED", True, _finding(), False),
        ("INVALIDATED", "BLOCKED", True, _finding(), False),
        ("EVIDENCE_COLLECTED", "PASS", True, _finding(), False),
        ("EVIDENCE_COLLECTED", "NOT_EVALUATED", True, _finding(), False),
        ("EVIDENCE_COLLECTED", "BLOCKED", False, _finding(), False),
        ("EVIDENCE_COLLECTED", "BLOCKED", True, _finding(severity="medium"), False),
        ("EVIDENCE_COLLECTED", "BLOCKED", True, _finding(status="acknowledged"), False),
        ("EVIDENCE_COLLECTED", "BLOCKED", True, _finding(subject_digest="sha256:" + "f" * 64), False),
        ("EVIDENCE_COLLECTED", "BLOCKED", True, None, False),
    ),
)
def test_case_view_remediation_action_obeys_authoritative_eligibility_matrix(
    acceptance_state,
    policy_status,
    digest_freshness,
    finding,
    expected,
):
    view = build_case_view(
        case_id="case-view",
        subject_digest=SUBJECT,
        revision=1,
        acceptance_state=acceptance_state,
        decisions=(),
        release_observations=(),
        digest_freshness=digest_freshness,
        risk="high",
        findings=(finding,) if finding is not None else (),
    )
    policy_gate = view["policy_gate"]
    policy_gate["status"] = policy_status
    view["allowed_actions"] = derive_allowed_actions(
        acceptance_state=acceptance_state,
        policy_gate=policy_gate,
        digest_freshness=digest_freshness,
        risk="high",
        subject_digest=SUBJECT,
        findings=(finding,) if finding is not None else (),
    )

    assert (resolve_action(view["allowed_actions"], "remediate") is not None) is expected


def test_remediate_action_is_high_risk_but_not_a_decision_action():
    actions = derive_allowed_actions(
        acceptance_state="EVIDENCE_COLLECTED",
        policy_gate=_policy_gate("BLOCKED"),
        digest_freshness=True,
        risk="medium",
        subject_digest=SUBJECT,
        findings=(_finding(),),
    )

    assert resolve_action(actions, "remediate") == {
        "code": "remediate",
        "required_human_role": None,
        "self_approval_forbidden": False,
        "high_risk_confirmation_required": True,
    }


def test_web_projection_adds_case_view_and_ignores_metadata_release_status(
    tmp_path,
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_repository_case(
        repository,
        policy_outcome="PASS",
        release_status="CONFIRMED",
    )

    projection = repository.get_change("case-view")

    assert repository._store.lifecycle_schema_version() == 2
    assert projection["schema_version"] == "v1"
    assert projection["case_id"] == "case-view"
    assert projection["subject_digest"] == SUBJECT
    assert projection["acceptance_state"] == "EVIDENCE_COLLECTED"
    assert projection["policy_gate"]["status"] == "PASS"
    assert projection["release_state"]["status"] == "NOT_OBSERVED"
    assert projection["metadata"]["release_status"] == "CONFIRMED"
    assert projection["gate"] == "PASS"


def test_projection_reads_latest_declared_observation_on_its_transaction_connection(
    tmp_path, monkeypatch
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_repository_case(repository, policy_outcome="PASS")
    repository._store.append_release_observation(
        "case-view",
        _stored_observation(
            observation_id="first",
            outcome="CONFIRMED",
            recorded_at=NOW + timedelta(minutes=20),
        ).observation,
    )
    repository._store.append_release_observation(
        "case-view",
        _stored_observation(
            observation_id="latest",
            outcome="ROLLED_BACK",
            recorded_at=NOW + timedelta(minutes=30),
        ).observation,
    )
    original_connect = repository._store._connect
    connect_calls = 0

    def counted_connect():
        nonlocal connect_calls
        connect_calls += 1
        return original_connect()

    monkeypatch.setattr(repository._store, "_connect", counted_connect)

    projection = repository._projection("case-view")

    assert connect_calls == 1
    assert projection["release_state"] == {
        "status": "ROLLED_BACK",
        "observation_id": "latest",
        "environment": "production-canary",
        "deployment_id": "deployment-latest",
        "source": "manual",
        "trust_level": "declared",
        "recorded_at": _stored_observation(
            observation_id="latest",
            outcome="ROLLED_BACK",
            recorded_at=NOW + timedelta(minutes=30),
        ).observation.model_dump(mode="json")["recorded_at"],
    }


def test_needs_human_actions_drive_post_guards_and_disappear_after_signing(
    tmp_path,
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_repository_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
        risk="high",
        author="author-agent",
    )
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    client = TestClient(app)
    base = {
        "subject_digest": SUBJECT,
        "owner_role": "release_owner",
        "decision": "approve",
        "reason": "sign the reviewed case",
        "decided_at": (NOW + timedelta(minutes=3)).isoformat(),
        "high_risk_confirmed": True,
    }

    try:
        before = client.get("/api/assurance/changes/case-view")
        approval = resolve_action(before.json()["allowed_actions"], "approve")
        assert approval == {
            "code": "approve",
            "required_human_role": "release_owner",
            "self_approval_forbidden": True,
            "high_risk_confirmation_required": True,
        }

        self_approval = client.post(
            "/api/assurance/changes/case-view/decisions",
            headers={"Idempotency-Key": "decision:self"},
            json=base | {"decision_id": "self", "owner": "author-agent"},
        )
        wrong_role = client.post(
            "/api/assurance/changes/case-view/decisions",
            headers={"Idempotency-Key": "decision:role"},
            json=base
            | {
                "decision_id": "role",
                "owner": "security-owner",
                "owner_role": "security_owner",
            },
        )
        unconfirmed = client.post(
            "/api/assurance/changes/case-view/decisions",
            headers={"Idempotency-Key": "decision:risk"},
            json=base
            | {
                "decision_id": "risk",
                "owner": "release-owner",
                "high_risk_confirmed": False,
            },
        )
        signed = client.post(
            "/api/assurance/changes/case-view/decisions",
            headers={"Idempotency-Key": "decision:ok"},
            json=base | {"decision_id": "ok", "owner": "release-owner"},
        )
    finally:
        app.dependency_overrides.clear()

    assert self_approval.status_code == 403
    assert self_approval.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    assert wrong_role.status_code == 403
    assert wrong_role.json()["detail"]["code"] == "POLICY_ROLE_REQUIRED"
    assert unconfirmed.status_code == 403
    assert unconfirmed.json()["detail"]["code"] == (
        "HIGH_RISK_CONFIRMATION_REQUIRED"
    )
    assert signed.status_code == 200
    assert signed.json()["acceptance_state"] == "ACCEPTED"
    assert signed.json()["release_state"]["status"] == "NOT_OBSERVED"
    assert _codes(signed.json()["allowed_actions"]) == ("download_passport",)


def test_pass_with_waiver_rejects_ordinary_approval_at_api_and_repository(
    tmp_path,
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_repository_case(repository, policy_outcome="PASS_WITH_WAIVER")
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    client = TestClient(app)

    try:
        response = client.post(
            "/api/assurance/changes/case-view/decisions",
            headers={"Idempotency-Key": "decision:api-pass-with-waiver"},
            json={
                "decision_id": "api-pass-with-waiver",
                "subject_digest": SUBJECT,
                "owner": "release-owner",
                "owner_role": "release_owner",
                "decision": "approve",
                "reason": "ordinary approval must stay closed",
                "decided_at": (NOW + timedelta(minutes=3)).isoformat(),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACTION_NOT_ALLOWED"

    human = HumanDecision(
        decision_id="repository-pass-with-waiver",
        subject_digest=SUBJECT,
        owner="release-owner",
        owner_role="release_owner",
        decision="approve",
        reason="repository cannot bypass the closed action",
        decided_at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(AssuranceWebConflictError, match="PASS_WITH_WAIVER"):
        repository.decide(
            "case-view",
            human,
            AcceptanceEvent(
                event_id="decision:repository-pass-with-waiver",
                subject_digest=SUBJECT,
                kind="ACCEPT",
                policy_decision_refs=("policy-latest",),
                human_decision_refs=(human.decision_id,),
                occurred_at=human.decided_at,
            ),
            "decision:repository-pass-with-waiver",
            {"decision_id": human.decision_id},
        )


def test_demo_and_accepted_case_keep_release_state_separate(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    seed_assurance_demo(db_path)
    repository = AssuranceWebRepository(db_path)
    repository.initialize()

    old_case = repository.get_change(OLD_CASE_ID)
    accepted_case = repository.get_change(NEW_CASE_ID)

    assert old_case["gate"] == "INVALIDATED"
    assert old_case["policy_gate"]["status"] == "BLOCKED"
    assert old_case["acceptance_state"] == "INVALIDATED"
    assert old_case["release_state"]["status"] == "NOT_OBSERVED"
    assert accepted_case["acceptance_state"] == "ACCEPTED"
    assert accepted_case["release_state"]["status"] == "NOT_OBSERVED"
