"""Focused behavior tests for the Acceptance Case decision boundary."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from assurance import store as store_module
from assurance.contracts import (
    AcceptanceCase,
    Evidence,
    ExecutionReceipt,
    ExecutionStep,
    HumanDecision,
    PolicyDecision,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebNotFoundError,
    AssuranceWebRepository,
    get_assurance_repository,
)
from web.server import create_app


NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "1" * 64
ARTIFACT = "sha256:" + "2" * 64
RULES = "sha256:" + "3" * 64


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _seed_reviewed_case(
    repository: AssuranceWebRepository,
    *,
    policy_outcome: str,
    required_human_role: str | None = None,
) -> None:
    repository.initialize()
    case = AcceptanceCase(
        case_id="case-policy-boundary",
        subject_digest=SUBJECT,
        state="DRAFT",
        created_at=_at(0),
        updated_at=_at(0),
    )
    repository.create_change(
        case,
        AcceptanceBinding(
            subject_digest=SUBJECT,
            policy_version="policy-v1",
            rubric_version="rubric-v1",
        ),
        {"author": "author-agent", "risk": "medium"},
        "seed:create",
        {"case_id": case.case_id},
    )
    evidence = Evidence(
        evidence_id="evidence-1",
        subject_digest=SUBJECT,
        kind="deterministic_test",
        producer="test-suite",
        artifact_digest=ARTIFACT,
        source_ref="test://policy-boundary",
        status="success",
        trust_level="deterministic",
        collected_at=_at(1),
    )
    repository.collect(
        case.case_id,
        AcceptanceEvent(
            event_id="event:collect",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            occurred_at=_at(1),
        ),
        evidence,
        "seed:collect",
        {"evidence_id": evidence.evidence_id},
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt-1",
        run_id="run-1",
        subject_digest=SUBJECT,
        steps=(
            ExecutionStep(
                sequence=0,
                planned_role="architecture",
                actual_role="architecture",
                model_ref="test-model",
                provider="local-test",
                routing_rule="test-only",
                timeout_seconds=1,
                result="success",
                schema_status="valid",
            ),
        ),
        overall_result="success",
        started_at=_at(2),
        completed_at=_at(3),
    )
    policy = PolicyDecision(
        decision_id="policy-1",
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rules_digest=RULES,
        outcome=policy_outcome,
        reason_codes=("HARD_POLICY_BLOCK",),
        required_human_role=required_human_role,
        evaluated_evidence_refs=(evidence.evidence_id,),
        evaluated_receipt_refs=(receipt.receipt_id,),
        evaluated_at=_at(3),
    )
    repository.review(
        case.case_id,
        AcceptanceEvent(
            event_id="event:review",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            execution_receipt_refs=(receipt.receipt_id,),
            policy_decision_refs=(policy.decision_id,),
            occurred_at=_at(3),
        ),
        (),
        receipt,
        policy,
        "seed:review",
        {"policy_decision_id": policy.decision_id},
    )


@pytest.mark.parametrize("policy_outcome", ("BLOCKED", "STALE"))
def test_blocking_policy_cannot_be_overridden_by_ordinary_approval(
    tmp_path, policy_outcome
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(repository, policy_outcome=policy_outcome)
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    client = TestClient(app)

    response = client.post(
        "/api/assurance/changes/case-policy-boundary/decisions",
        headers={"Idempotency-Key": "decision:approve"},
        json={
            "decision_id": "human-1",
            "subject_digest": SUBJECT,
            "owner": "release-owner",
            "owner_role": "release_owner",
            "decision": "approve",
            "reason": "release owner approval",
            "decided_at": _at(4).isoformat(),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "POLICY_BLOCKS_APPROVAL"
    assert response.json()["detail"]["reason_codes"] == [
        f"POLICY_{policy_outcome}"
    ]
    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "EVIDENCE_COLLECTED"
    assert projection["case"]["human_decision_refs"] == []
    assert [item["kind"] for item in projection["decisions"]] == ["policy"]
    app.dependency_overrides.clear()


def test_needs_human_policy_rejects_an_approver_with_the_wrong_role(tmp_path):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="security_owner",
    )
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    client = TestClient(app)

    response = client.post(
        "/api/assurance/changes/case-policy-boundary/decisions",
        headers={"Idempotency-Key": "decision:wrong-role"},
        json={
            "decision_id": "human-wrong-role",
            "subject_digest": SUBJECT,
            "owner": "release-owner",
            "owner_role": "release_owner",
            "decision": "approve",
            "reason": "release owner approval",
            "decided_at": _at(4).isoformat(),
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "POLICY_ROLE_REQUIRED"
    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "EVIDENCE_COLLECTED"
    assert projection["case"]["human_decision_refs"] == []
    app.dependency_overrides.clear()


def test_repository_cannot_bypass_a_blocking_policy(tmp_path):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(repository, policy_outcome="BLOCKED")
    human = HumanDecision(
        decision_id="human-direct",
        subject_digest=SUBJECT,
        owner="release-owner",
        owner_role="release_owner",
        decision="approve",
        reason="direct repository approval",
        decided_at=_at(4),
    )

    with pytest.raises(AssuranceWebConflictError, match="BLOCKED"):
        repository.decide(
            "case-policy-boundary",
            human,
            AcceptanceEvent(
                event_id="decision:human-direct",
                subject_digest=SUBJECT,
                kind="ACCEPT",
                policy_decision_refs=("policy-1",),
                human_decision_refs=(human.decision_id,),
                occurred_at=human.decided_at,
            ),
            "decision:direct",
            {"decision_id": human.decision_id},
        )

    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "EVIDENCE_COLLECTED"
    assert projection["case"]["human_decision_refs"] == []


def test_approval_after_policy_re_evaluation_references_only_latest_policy(
    tmp_path,
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
    )
    receipt = ExecutionReceipt(
        receipt_id="receipt-2",
        run_id="run-2",
        subject_digest=SUBJECT,
        steps=(
            ExecutionStep(
                sequence=0,
                planned_role="architecture",
                actual_role="architecture",
                model_ref="test-model",
                provider="local-test",
                routing_rule="policy-re-evaluation",
                timeout_seconds=1,
                result="success",
                schema_status="valid",
            ),
        ),
        overall_result="success",
        started_at=_at(4),
        completed_at=_at(5),
    )
    policy = PolicyDecision(
        decision_id="policy-2",
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rules_digest=RULES,
        outcome="NEEDS_HUMAN",
        reason_codes=("REQUIRED_HUMAN_MISSING",),
        required_human_role="release_owner",
        evaluated_evidence_refs=("evidence-1",),
        evaluated_receipt_refs=(receipt.receipt_id,),
        evaluated_at=_at(5),
    )
    repository.review(
        "case-policy-boundary",
        AcceptanceEvent(
            event_id="event:review-2",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=("evidence-1",),
            execution_receipt_refs=(receipt.receipt_id,),
            policy_decision_refs=(policy.decision_id,),
            occurred_at=_at(5),
        ),
        (),
        receipt,
        policy,
        "seed:review-2",
        {"policy_decision_id": policy.decision_id},
    )
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    client = TestClient(app)

    response = client.post(
        "/api/assurance/changes/case-policy-boundary/decisions",
        headers={"Idempotency-Key": "decision:latest-policy"},
        json={
            "decision_id": "human-latest-policy",
            "subject_digest": SUBJECT,
            "owner": "release-owner",
            "owner_role": "release_owner",
            "decision": "approve",
            "reason": "approve latest policy evaluation",
            "decided_at": _at(6).isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.json()["case"]["state"] == "ACCEPTED"
    decision_event = next(
        item
        for item in response.json()["timeline"]
        if item["type"] == "event"
        and item["id"] == "decision:human-latest-policy"
    )
    assert decision_event["policy_decision_refs"] == ["policy-2"]
    app.dependency_overrides.clear()


def test_failed_accept_event_does_not_leave_an_orphan_human_decision(
    tmp_path, monkeypatch
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
    )
    human = HumanDecision(
        decision_id="human-atomic",
        subject_digest=SUBJECT,
        owner="release-owner",
        owner_role="release_owner",
        decision="approve",
        reason="atomic decision",
        decided_at=_at(4),
    )
    event = AcceptanceEvent(
        event_id="decision:human-atomic",
        subject_digest=SUBJECT,
        kind="ACCEPT",
        policy_decision_refs=("policy-1",),
        human_decision_refs=(human.decision_id,),
        occurred_at=human.decided_at,
    )
    original_apply = store_module.apply_acceptance_event

    def fail_event(state, candidate):
        if candidate.event_id == event.event_id:
            raise RuntimeError("injected event failure")
        return original_apply(state, candidate)

    monkeypatch.setattr(store_module, "apply_acceptance_event", fail_event)
    with pytest.raises(RuntimeError, match="injected event failure"):
        repository.decide(
            "case-policy-boundary",
            human,
            event,
            "decision:atomic",
            {"decision_id": human.decision_id},
        )
    monkeypatch.setattr(store_module, "apply_acceptance_event", original_apply)

    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "EVIDENCE_COLLECTED"
    assert projection["case"]["human_decision_refs"] == []
    assert [item["kind"] for item in projection["decisions"]] == ["policy"]


def test_idempotency_key_cannot_replay_a_different_case(tmp_path):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    binding = AcceptanceBinding(
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )
    first = AcceptanceCase(
        case_id="case-idempotency-a",
        subject_digest=SUBJECT,
        state="DRAFT",
        created_at=_at(0),
        updated_at=_at(0),
    )
    second = first.model_copy(update={"case_id": "case-idempotency-b"})
    shared_payload = {"request": "same-body"}

    repository.create_change(
        first,
        binding,
        {"source": "test"},
        "create:shared-key",
        shared_payload,
    )

    with pytest.raises(AssuranceWebConflictError, match="different"):
        repository.create_change(
            second,
            binding,
            {"source": "test"},
            "create:shared-key",
            shared_payload,
        )

    assert repository.get_change(first.case_id)["case"]["case_id"] == first.case_id
    with pytest.raises(AssuranceWebNotFoundError):
        repository.get_change(second.case_id)


def test_projection_failure_rolls_back_decision_event_and_idempotency(
    tmp_path, monkeypatch
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
    )
    human = HumanDecision(
        decision_id="human-projection-failure",
        subject_digest=SUBJECT,
        owner="release-owner",
        owner_role="release_owner",
        decision="approve",
        reason="projection failure injection",
        decided_at=_at(4),
    )
    event = AcceptanceEvent(
        event_id="decision:projection-failure",
        subject_digest=SUBJECT,
        kind="ACCEPT",
        policy_decision_refs=("policy-1",),
        human_decision_refs=(human.decision_id,),
        occurred_at=human.decided_at,
    )
    original_render = repository._render_projection

    def fail_projection(*_args):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(repository, "_render_projection", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        repository.decide(
            "case-policy-boundary",
            human,
            event,
            "decision:projection-failure",
            {"decision_id": human.decision_id},
        )
    monkeypatch.setattr(repository, "_render_projection", original_render)

    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "EVIDENCE_COLLECTED"
    assert projection["case"]["human_decision_refs"] == []
    assert [item["kind"] for item in projection["decisions"]] == ["policy"]

    retried = repository.decide(
        "case-policy-boundary",
        human,
        event,
        "decision:projection-failure",
        {"decision_id": human.decision_id},
    )
    assert retried["case"]["state"] == "ACCEPTED"


def test_same_idempotency_key_serializes_competing_decisions(
    tmp_path, monkeypatch
):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    _seed_reviewed_case(
        repository,
        policy_outcome="NEEDS_HUMAN",
        required_human_role="release_owner",
    )
    original_begin = repository._begin_mutation
    first_checked = Event()
    competing_checked = Event()
    calls_lock = Lock()
    call_count = 0

    def overlap_after_idempotency_check(*args):
        nonlocal call_count
        result = original_begin(*args)
        with calls_lock:
            call_count += 1
            call_index = call_count
        if call_index == 1:
            first_checked.set()
            competing_checked.wait(timeout=0.25)
        else:
            competing_checked.set()
        return result

    monkeypatch.setattr(
        repository, "_begin_mutation", overlap_after_idempotency_check
    )

    def decide_once(index):
        human = HumanDecision(
            decision_id=f"human-concurrent-{index}",
            subject_digest=SUBJECT,
            owner=f"release-owner-{index}",
            owner_role="release_owner",
            decision="approve",
            reason=f"competing decision {index}",
            decided_at=_at(4),
        )
        event = AcceptanceEvent(
            event_id=f"decision:human-concurrent-{index}",
            subject_digest=SUBJECT,
            kind="ACCEPT",
            policy_decision_refs=("policy-1",),
            human_decision_refs=(human.decision_id,),
            occurred_at=human.decided_at,
        )
        try:
            return (
                "ok",
                repository.decide(
                    "case-policy-boundary",
                    human,
                    event,
                    "decision:concurrent",
                    {"decision_id": human.decision_id},
                ),
            )
        except Exception as exc:  # asserted precisely below
            return "error", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(decide_once, 1)
        assert first_checked.wait(timeout=1)
        second = pool.submit(decide_once, 2)
        results = (first.result(), second.result())

    successes = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], AssuranceWebConflictError)
    projection = repository.get_change("case-policy-boundary")
    assert projection["case"]["state"] == "ACCEPTED"
    assert len(projection["case"]["human_decision_refs"]) == 1
    assert [item["kind"] for item in projection["decisions"]] == [
        "policy",
        "human",
    ]


def test_corrupt_cached_idempotency_result_fails_as_storage_error(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    repository = AssuranceWebRepository(db_path)
    repository.initialize()
    case = AcceptanceCase(
        case_id="case-corrupt-idempotency",
        subject_digest=SUBJECT,
        state="DRAFT",
        created_at=_at(0),
        updated_at=_at(0),
    )
    binding = AcceptanceBinding(
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )
    payload = {"case_id": case.case_id}
    repository.create_change(
        case,
        binding,
        {"source": "test"},
        "create:corrupt-cache",
        payload,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE assurance_web_idempotency SET result_json = ? "
            "WHERE idempotency_key = ?",
            ("{", "create:corrupt-cache"),
        )

    with pytest.raises(AssuranceWebError, match="invalid cached"):
        repository.create_change(
            case,
            binding,
            {"source": "test"},
            "create:corrupt-cache",
            payload,
        )
