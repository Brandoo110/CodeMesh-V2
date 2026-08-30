"""CI-only, isolated target-user walkthrough for the P-C handover surface."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from assurance.contracts import (
    AcceptanceCase,
    Evidence,
    ExecutionReceipt,
    ExecutionStep,
    Finding,
    PolicyDecision,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_store import AssuranceWebRepository, get_assurance_repository
from web.server import create_app


CASE_ID = "p-c-walkthrough-case"
SUBJECT = "sha256:" + "a" * 64
ARTIFACT = "sha256:" + "b" * 64
RULES = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)


def _seed(repository: AssuranceWebRepository) -> None:
    repository.initialize()
    repository.create_change(
        AcceptanceCase(
            case_id=CASE_ID,
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
        {
            "change_id": CASE_ID,
            "title": "P-C handover walkthrough",
            "summary": "An isolated Case used to verify Queue to decision handover.",
            "owner": "release-owner",
            "owner_role": "release_owner",
            "author": "builder-agent",
            "risk": "high",
            "priority": 90,
            "value": 80,
            "release_status": "awaiting_release_owner",
            "policy_version": "policy-v1",
            "rubric_version": "rubric-v1",
            "intent_coverage": "handover scope",
            "architecture_impact": "no runtime change",
            "operational_readiness": "owner decision required",
            "knowledge_notes": "CI synthetic only",
            "ownership_notes": "release owner is separate from author",
        },
        "seed:create",
        {"case_id": CASE_ID},
    )

    evidence = Evidence(
        evidence_id=f"{CASE_ID}:evidence:check",
        subject_digest=SUBJECT,
        kind="builder_green_test",
        producer="ci-walkthrough",
        artifact_digest=ARTIFACT,
        source_ref="ci://p-c-walkthrough/check",
        trace_id="trace:p-c-walkthrough",
        status="success",
        trust_level="declared",
        collected_at=NOW + timedelta(minutes=1),
    )
    finding = Finding(
        finding_id=f"{CASE_ID}:finding:owner",
        subject_digest=SUBJECT,
        reviewer_role="operability",
        claim="Release owner must confirm the handover boundary.",
        evidence_refs=(evidence.evidence_id,),
        basis="deterministic",
        severity="high",
        confidence=1.0,
        rubric_hash=RULES,
        model_ref="ci-walkthrough",
        status="open",
    )
    receipt = ExecutionReceipt(
        receipt_id=f"{CASE_ID}:receipt:review",
        run_id=f"{CASE_ID}:run:review",
        subject_digest=SUBJECT,
        steps=(
            ExecutionStep(
                sequence=0,
                planned_role="operability",
                actual_role="operability",
                model_ref="ci-walkthrough",
                provider="offline-ci",
                routing_rule="p-c-walkthrough",
                token_budget=0,
                timeout_seconds=1,
                result="success",
                schema_status="valid",
            ),
        ),
        overall_result="success",
        started_at=NOW + timedelta(minutes=1),
        completed_at=NOW + timedelta(minutes=2),
    )
    policy = PolicyDecision(
        decision_id=f"{CASE_ID}:decision:policy",
        subject_digest=SUBJECT,
        policy_version="policy-v1",
        rules_digest=RULES,
        outcome="NEEDS_HUMAN",
        reason_codes=("RELEASE_OWNER_REQUIRED",),
        required_reviewers=("operability",),
        required_human_role="release_owner",
        evaluated_evidence_refs=(evidence.evidence_id,),
        evaluated_finding_refs=(finding.finding_id,),
        evaluated_receipt_refs=(receipt.receipt_id,),
        evaluated_at=NOW + timedelta(minutes=3),
    )
    repository._store.append_event(
        CASE_ID,
        AcceptanceEvent(
            event_id="seed:collect",
            subject_digest=SUBJECT,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            finding_refs=(finding.finding_id,),
            execution_receipt_refs=(receipt.receipt_id,),
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )
    repository._store.append_policy_decision(CASE_ID, policy)
    with repository._store._transaction() as unit_of_work:
        repository._touch_web_case(
            unit_of_work.connection,
            CASE_ID,
            NOW + timedelta(minutes=3),
            evidence=(evidence,),
            findings=(finding,),
            receipt=receipt,
        )


def _business_projection(value: dict) -> dict:
    result = json.loads(json.dumps(value, sort_keys=True))
    freshness = result.get("freshness")
    if isinstance(freshness, dict):
        freshness.pop("checked_at", None)
    return result


def run(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "isolated-assurance.sqlite"
    repository = AssuranceWebRepository(db_path)
    _seed(repository)
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    record: dict[str, object] = {
        "evidence_boundary": "isolated SQLite/TestClient synthetic only",
        "production_evidence": False,
        "case_id": CASE_ID,
    }
    try:
        with TestClient(app) as client:
            queue = client.get("/api/assurance/changes")
            queue.raise_for_status()
            assert [item["case_id"] for item in queue.json()] == [CASE_ID]
            record["queue_identified_case"] = True

            before = client.get(f"/api/assurance/changes/{CASE_ID}")
            before.raise_for_status()
            before_payload = before.json()
            assert before_payload["case_id"] == CASE_ID
            assert before_payload["policy_gate"]["status"] == "NEEDS_HUMAN"
            assert before_payload["freshness_mode"] == "database_only_fixture"
            assert before_payload["findings"]
            assert before_payload["case"]["evidence_refs"]
            record["passport_findings_evidence_freshness_lineage"] = True

            passport = client.get(f"/api/assurance/changes/{CASE_ID}/passport")
            passport.raise_for_status()
            passport_payload = passport.json()
            assert passport_payload["case_id"] == CASE_ID
            assert passport_payload["subject_digest"] == SUBJECT
            record["passport_read"] = True

            decision = {
                "decision_id": "p-c-human-approve",
                "subject_digest": SUBJECT,
                "owner": "release-owner",
                "owner_role": "release_owner",
                "decision": "approve",
                "reason": "Release owner reviewed the isolated handover case.",
                "conditions": [],
                "waiver_id": None,
                "expires_at": None,
                "decided_at": (NOW + timedelta(minutes=4)).isoformat(),
                "high_risk_confirmed": True,
            }
            posted = client.post(
                f"/api/assurance/changes/{CASE_ID}/decisions",
                headers={"Idempotency-Key": "p-c-decision-1"},
                json=decision,
            )
            posted.raise_for_status()
            readback = client.get(f"/api/assurance/changes/{CASE_ID}")
            readback.raise_for_status()
            assert _business_projection(posted.json()) == _business_projection(
                readback.json()
            )
            assert readback.json()["acceptance_state"] == "ACCEPTED"
            record["post_authoritative_get_exact_match"] = True

            stale = client.post(
                f"/api/assurance/changes/{CASE_ID}/decisions",
                headers={"Idempotency-Key": "p-c-stale"},
                json={**decision, "decision_id": "p-c-stale", "subject_digest": "sha256:" + "d" * 64},
            )
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == "STALE_SUBJECT"
            after_stale = client.get(f"/api/assurance/changes/{CASE_ID}").json()
            assert after_stale["case"]["human_decision_refs"] == ["p-c-human-approve"]
            record["stale_rejected_without_new_decision"] = True

            repository._store.append_event(
                CASE_ID,
                AcceptanceEvent(
                    event_id="seed:invalidate",
                    subject_digest=SUBJECT,
                    kind="INVALIDATE",
                    reason="walkthrough:invalidated-counterexample",
                    occurred_at=NOW + timedelta(minutes=5),
                ),
            )
            invalidated = client.get(f"/api/assurance/changes/{CASE_ID}")
            invalidated.raise_for_status()
            assert invalidated.json()["acceptance_state"] == "INVALIDATED"
            denied = client.post(
                f"/api/assurance/changes/{CASE_ID}/decisions",
                headers={"Idempotency-Key": "p-c-invalidated"},
                json={**decision, "decision_id": "p-c-invalidated"},
            )
            assert denied.status_code == 409
            assert denied.json()["detail"]["code"] == "ACTION_NOT_ALLOWED"
            final = client.get(f"/api/assurance/changes/{CASE_ID}").json()
            assert final["case"]["human_decision_refs"] == ["p-c-human-approve"]
            record["invalidated_rejected_without_new_decision"] = True
    finally:
        app.dependency_overrides.clear()

    (output_dir / "walkthrough.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "walkthrough.log").write_text(
        "P-C isolated target-user walkthrough: PASS\n"
        + "authoritative POST -> GET readback: exact business match\n"
        + "STALE and INVALIDATED counterexamples: no new decision\n",
        encoding="utf-8",
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    record = run(args.output_dir)
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
