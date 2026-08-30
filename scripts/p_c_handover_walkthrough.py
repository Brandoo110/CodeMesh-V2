"""CI-only target-user walkthrough over the real server-owned P-C composition."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandSpec
from assurance.live_freshness import LiveFreshnessChecker
from assurance.run_service import (
    AssuranceRunConfig,
    AssuranceRunIntent,
    AssuranceRunService,
    RedactionDisposition,
    ReviewerContextPlan,
    ReviewerContextPlanEntry,
    ReviewerInvocationResponse,
    ReviewerRoute,
)
from web.assurance_store import AssuranceWebRepository, get_assurance_repository
from web.server import create_app


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def _initial_changed_text() -> str:
    return "".join(
        f"synthetic working-tree change {index}\n" for index in range(1001)
    )


def _make_isolated_git_repo(
    workspace_root: Path, label: str
) -> tuple[Path, str, str]:
    repository = workspace_root / f"repo-{label}"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "p-c@example.invalid")
    _git(repository, "config", "user.name", "CodeMesh P-C")

    task_text = (
        "---\n"
        f"title: P-C {label} handover\n"
        "owner: p-c-builder\n"
        "version: v1\n"
        "status: active\n"
        "---\n"
        f"# P-C {label} handover\n\n"
        "## Acceptance\n\n"
        "- [ ] owner reviews the handover boundary\n"
    )
    (repository / "TASK.md").write_text(task_text, encoding="utf-8")
    (repository / "POLICY.md").write_text(
        "# P-C policy\n\nVersion: v1\n\nStatus: active\n",
        encoding="utf-8",
    )
    _git(repository, "add", "TASK.md", "POLICY.md")
    _git(repository, "commit", "-qm", "p-c baseline")
    changed_text = _initial_changed_text()
    (repository / "changed.txt").write_text(changed_text, encoding="utf-8")
    return repository, task_text, changed_text


class _PCHandoverContextBuilder:
    """Deterministic safe-context adapter; the run service remains authoritative."""

    def prepare(self, evidences, *, artifact_store, subject_digest):
        del artifact_store, subject_digest
        return ReviewerContextPlan(
            entries=tuple(
                ReviewerContextPlanEntry(
                    evidence_id=evidence.evidence_id,
                    kind=evidence.kind,
                    artifact_digest=evidence.artifact_digest,
                    disposition=RedactionDisposition.NOT_APPLICABLE,
                    content=f"P-C synthetic CI context: {evidence.kind}",
                    truncated=False,
                )
                for evidence in evidences
            )
        )


class _PCHandoverReviewer:
    """Deterministic reviewer protocol adapter with no repository or write access."""

    async def invoke(self, prompt, *, run_id, route):
        del run_id
        evidence_ref = prompt.input.contexts[0].evidence_id
        payload = {
            "schema_version": "v1",
            "subject_digest": prompt.input.subject.subject_digest,
            "rubric_hash": prompt.rubric_hash,
            "findings": [
                {
                    "reviewer_role": "operability",
                    "claim": "The handover owner must confirm the change boundary.",
                    "evidence_refs": [evidence_ref],
                    "severity": "medium",
                    "confidence": 1.0,
                }
            ],
            "questions": [],
        }
        return ReviewerInvocationResponse(
            status="success",
            provider=route.provider,
            model_ref=route.model_ref,
            raw_response=json.dumps(
                payload, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8"),
            started_at=prompt.input.evaluated_at,
            completed_at=prompt.input.evaluated_at,
            schema_status="unverified",
            usage_status="unavailable",
        )


def _build_fixture(*, db_path: Path, workspace_root: Path) -> dict:
    """Build two durable Cases through LiveFreshnessChecker + RunService."""

    db_path = db_path.resolve()
    workspace_root = workspace_root.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=False)

    fresh_repository, _fresh_task_text, _fresh_changed_text = _make_isolated_git_repo(
        workspace_root, "fresh"
    )
    unavailable_repository, unavailable_task_text, unavailable_changed_text = _make_isolated_git_repo(
        workspace_root, "unavailable"
    )

    freshness_checker = LiveFreshnessChecker(workspace_root=workspace_root)
    repository = AssuranceWebRepository(
        db_path,
        freshness_checker=freshness_checker,
        live_required=True,
    )
    repository.initialize()
    artifact_store = ArtifactStore(workspace_root / "artifacts")
    config = AssuranceRunConfig(
        workspace_root=workspace_root,
        redaction_policy_version="redaction.v0",
        policy_version="gate.v0",
        rubric_version="single_general.v0",
        allowed_commands=(
            CommandSpec(
                command_id="p-c-check",
                kind="test",
                argv=("python", "-c", "print('p-c deterministic check')"),
                cwd=".",
                timeout_seconds=5.0,
                max_output_bytes=4096,
            ),
        ),
        freshness_ttl_seconds=300,
        reviewer_route=ReviewerRoute(
            provider="p-c-synthetic",
            model_ref="p-c-deterministic-reviewer",
            timeout_seconds=5,
        ),
    )
    service = AssuranceRunService(
        artifact_store=artifact_store,
        reviewer_invoker=_PCHandoverReviewer(),
        committer=repository,
        context_builder=_PCHandoverContextBuilder(),
        config=config,
    )

    def run_case(repository_path: Path, label: str):
        intent = AssuranceRunIntent(
            repository_path=repository_path,
            repository_identity=f"example/p-c-{label}",
            author="p-c-builder",
            base_ref="HEAD",
            task_path="TASK.md",
            policy_paths=("POLICY.md",),
            command_ids=("p-c-check",),
            changed_lines_total=1001,
            external_side_effects="none_declared",
            provider_boundary="within_declared_boundary",
        )
        return asyncio.run(
            service.run(intent, idempotency_key=f"p-c-run-{label}")
        ).bundle

    fresh_bundle = run_case(fresh_repository, "fresh")
    unavailable_bundle = run_case(unavailable_repository, "unavailable")

    unavailable_task = unavailable_repository / "TASK.md"
    unavailable_task.unlink()
    unavailable_task.symlink_to("POLICY.md")

    return {
        "repository": repository,
        "fresh_bundle": fresh_bundle,
        "unavailable_bundle": unavailable_bundle,
        "unavailable_repository": unavailable_repository,
        "unavailable_task_text": unavailable_task_text,
        "unavailable_changed_text": unavailable_changed_text,
        "workspace_root": workspace_root,
    }


def _business_projection(value: dict) -> dict:
    result = json.loads(json.dumps(value, sort_keys=True))
    freshness = result.get("freshness")
    if isinstance(freshness, dict):
        freshness.pop("checked_at", None)
    return result


def _assert_no_authoritative_drift(before: dict, after: dict) -> None:
    for field in (
        "revision",
        "acceptance_state",
        "policy_gate",
        "digest_freshness",
        "allowed_actions",
    ):
        assert before[field] == after[field], field
    assert (
        before["case"]["human_decision_refs"]
        == after["case"]["human_decision_refs"]
    )
    assert _business_projection(before) == _business_projection(after)


def _error_code(response) -> str | None:
    body = response.json()
    detail = body.get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _decision_payload(
    *, subject_digest: str, decision_id: str, decision: str = "approve"
) -> dict:
    decided_at = datetime.now(timezone.utc)
    payload = {
        "decision_id": decision_id,
        "subject_digest": subject_digest,
        "owner": "handover-owner",
        "owner_role": "change_owner",
        "decision": decision,
        "reason": "The qualified owner reviewed the isolated handover Case.",
        "conditions": [],
        "waiver_id": None,
        "expires_at": None,
        "decided_at": decided_at.isoformat(),
        "high_risk_confirmed": True,
    }
    if decision == "waiver":
        payload["conditions"] = ["recheck the owner boundary"]
        payload["waiver_id"] = f"{decision_id}-waiver"
        payload["expires_at"] = (decided_at + timedelta(hours=1)).isoformat()
    return payload


def run(output_dir: Path) -> dict:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture = _build_fixture(
        db_path=output_dir / "isolated-assurance.sqlite",
        workspace_root=output_dir / "synthetic-git-workspace",
    )
    repository = fixture["repository"]
    fresh_bundle = fixture["fresh_bundle"]
    unavailable_bundle = fixture["unavailable_bundle"]
    fresh_case_id = fresh_bundle.case.case_id
    unavailable_case_id = unavailable_bundle.case.case_id
    fresh_subject = fresh_bundle.subject.subject_digest
    unavailable_subject = unavailable_bundle.subject.subject_digest

    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    record: dict[str, object] = {
        "evidence_boundary": "isolated SQLite + isolated temporary Git repositories; synthetic CI only",
        "production_evidence": False,
        "synthetic_ci_boundary": True,
        "real_dogfood_boundary": "This walkthrough is not CodeMesh production Reviewer/Case/Bundle evidence.",
        "composition": {
            "freshness_checker": "LiveFreshnessChecker",
            "repository": "AssuranceWebRepository(live_required=True)",
            "run_service_committer": "same AssuranceWebRepository",
        },
        "fresh_case_id": fresh_case_id,
        "unavailable_case_id": unavailable_case_id,
    }
    try:
        with TestClient(app) as client:
            queue_response = client.get("/api/assurance/changes")
            queue_response.raise_for_status()
            queue = queue_response.json()
            queue_ids = {item["case_id"] for item in queue}
            assert queue_ids == {fresh_case_id, unavailable_case_id}
            record["queue"] = {
                "identified_cases": sorted(queue_ids),
                "freshness_not_checked_allowed": all(
                    item["freshness"]["reason_code"] == "FRESHNESS_NOT_CHECKED"
                    and [action["code"] for action in item["allowed_actions"]]
                    == ["download_passport"]
                    for item in queue
                ),
            }

            before_response = client.get(
                f"/api/assurance/changes/{fresh_case_id}"
            )
            before_response.raise_for_status()
            before = before_response.json()
            assert before["case_id"] == fresh_case_id
            assert before["policy_gate"]["status"] == "NEEDS_HUMAN"
            assert before["acceptance_state"] == "EVIDENCE_COLLECTED"
            assert before["freshness_mode"] == "live_required"
            assert before["freshness"]["status"] == "FRESH"
            assert before["freshness"]["reason_code"] == "FRESHNESS_MATCH"
            assert before["freshness"]["expected_subject_digest"] == fresh_subject
            assert before["freshness"]["observed_subject_digest"] == fresh_subject
            assert before["digest_freshness"] is True
            assert before["findings"]
            assert before["evidence"]
            assert before["case"]["human_decision_refs"] == []

            passport_response = client.get(
                f"/api/assurance/changes/{fresh_case_id}/passport"
            )
            passport_response.raise_for_status()
            passport = passport_response.json()
            assert passport["case_id"] == fresh_case_id
            assert passport["subject_digest"] == fresh_subject

            approve_action = next(
                action
                for action in before["allowed_actions"]
                if action["code"] == "approve"
            )
            required_role = approve_action["required_human_role"]
            assert required_role
            assert approve_action["high_risk_confirmation_required"] is True
            decision = _decision_payload(
                subject_digest=fresh_subject,
                decision_id="p-c-human-approve",
            )
            decision["owner_role"] = required_role
            posted_response = client.post(
                f"/api/assurance/changes/{fresh_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-decision-1"},
                json=decision,
            )
            posted_response.raise_for_status()
            posted = posted_response.json()
            readback_response = client.get(
                f"/api/assurance/changes/{fresh_case_id}"
            )
            readback_response.raise_for_status()
            readback = readback_response.json()
            assert _business_projection(posted) == _business_projection(readback)
            assert readback["acceptance_state"] == "ACCEPTED"
            assert readback["case"]["human_decision_refs"] == [
                "p-c-human-approve"
            ]
            record["fresh_happy_path"] = {
                "fresh_before_decision": True,
                "freshness": before["freshness"],
                "passport_opened": True,
                "findings_evidence_lineage_opened": True,
                "allowed_action_from_authoritative_get": "approve",
                "required_human_role_from_authoritative_get": required_role,
                "submitted_owner_role": decision["owner_role"],
                "authoritative_post_get_exact_match": True,
                "pre_readback": _business_projection(before),
                "post_response": _business_projection(posted),
                "post_readback": _business_projection(readback),
            }

            unavailable_before_response = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            )
            unavailable_before_response.raise_for_status()
            unavailable_before = unavailable_before_response.json()
            assert unavailable_before["freshness"]["status"] == "UNAVAILABLE"
            assert unavailable_before["freshness"]["reason_code"] != "FRESHNESS_NOT_CHECKED"
            assert [
                action["code"] for action in unavailable_before["allowed_actions"]
            ] == ["download_passport"]
            assert unavailable_before["case"]["human_decision_refs"] == []
            unavailable_post_response = client.post(
                f"/api/assurance/changes/{unavailable_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-unavailable-decision"},
                json=_decision_payload(
                    subject_digest=unavailable_subject,
                    decision_id="p-c-unavailable-approve",
                ),
            )
            assert unavailable_post_response.status_code == 409
            assert _error_code(unavailable_post_response) == "ACTION_NOT_ALLOWED"
            unavailable_after = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            ).json()
            _assert_no_authoritative_drift(unavailable_before, unavailable_after)
            record["unavailable_counterexample"] = {
                "status": unavailable_before["freshness"]["status"],
                "reason_code": unavailable_before["freshness"]["reason_code"],
                "allowed_actions": unavailable_before["allowed_actions"],
                "post_status": unavailable_post_response.status_code,
                "post_code": _error_code(unavailable_post_response),
                "authoritative_no_drift": True,
                "pre_readback": _business_projection(unavailable_before),
                "post_readback": _business_projection(unavailable_after),
            }

            unavailable_task = fixture["unavailable_repository"] / "TASK.md"
            unavailable_task.unlink()
            unavailable_task.write_text(
                fixture["unavailable_task_text"], encoding="utf-8"
            )
            (fixture["unavailable_repository"] / "changed.txt").write_text(
                "synthetic working-tree change after baseline\n",
                encoding="utf-8",
            )
            stale_before = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            ).json()
            assert stale_before["freshness"]["status"] == "STALE"
            assert stale_before["acceptance_state"] == "INVALIDATED"
            assert stale_before["case"]["human_decision_refs"] == []
            stale_post_response = client.post(
                f"/api/assurance/changes/{unavailable_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-stale-decision"},
                json=_decision_payload(
                    subject_digest=unavailable_subject,
                    decision_id="p-c-stale-approve",
                ),
            )
            assert stale_post_response.status_code == 409
            assert _error_code(stale_post_response) in {
                "ACTION_NOT_ALLOWED",
                "STALE_SUBJECT",
            }
            stale_after = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            ).json()
            _assert_no_authoritative_drift(stale_before, stale_after)
            record["stale_counterexample"] = {
                "freshness": stale_before["freshness"],
                "server_owned_invalidation": stale_before["acceptance_state"]
                == "INVALIDATED",
                "post_status": stale_post_response.status_code,
                "post_code": _error_code(stale_post_response),
                "no_new_human_decision": True,
                "pre_readback": _business_projection(stale_before),
                "post_readback": _business_projection(stale_after),
            }

            unavailable_task.unlink()
            unavailable_task.write_text(
                fixture["unavailable_task_text"], encoding="utf-8"
            )
            (fixture["unavailable_repository"] / "changed.txt").write_text(
                fixture["unavailable_changed_text"], encoding="utf-8"
            )
            restored = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            ).json()
            assert restored["freshness"]["status"] == "FRESH"
            assert restored["freshness"]["reason_code"] == "FRESHNESS_MATCH"
            assert restored["freshness"]["expected_subject_digest"] == unavailable_subject
            assert restored["freshness"]["observed_subject_digest"] == unavailable_subject
            assert restored["acceptance_state"] == "INVALIDATED"
            assert restored["case"]["human_decision_refs"] == []
            assert [
                action["code"] for action in restored["allowed_actions"]
            ] == ["download_passport"]
            record["fresh_after_restore"] = True

            invalidated_before = restored
            invalidated_post_response = client.post(
                f"/api/assurance/changes/{unavailable_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-invalidated-decision"},
                json=_decision_payload(
                    subject_digest=unavailable_subject,
                    decision_id="p-c-invalidated-approve",
                ),
            )
            assert invalidated_post_response.status_code == 409
            assert _error_code(invalidated_post_response) == "ACTION_NOT_ALLOWED"
            invalidated_after = client.get(
                f"/api/assurance/changes/{unavailable_case_id}"
            ).json()
            _assert_no_authoritative_drift(invalidated_before, invalidated_after)
            record["invalidated_counterexample"] = {
                "fresh_after_restore": True,
                "freshness_before_post": invalidated_before["freshness"],
                "post_status": invalidated_post_response.status_code,
                "post_code": _error_code(invalidated_post_response),
                "no_new_human_decision": True,
                "pre_readback": _business_projection(invalidated_before),
                "post_readback": _business_projection(invalidated_after),
            }

            old_digest_before = readback
            old_digest_post_response = client.post(
                f"/api/assurance/changes/{fresh_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-old-digest"},
                json=_decision_payload(
                    subject_digest="sha256:" + "d" * 64,
                    decision_id="p-c-old-digest-decision",
                ),
            )
            assert old_digest_post_response.status_code == 409
            assert _error_code(old_digest_post_response) == "STALE_SUBJECT"
            old_digest_after = client.get(
                f"/api/assurance/changes/{fresh_case_id}"
            ).json()
            _assert_no_authoritative_drift(old_digest_before, old_digest_after)
            record["old_digest_counterexample"] = {
                "post_status": old_digest_post_response.status_code,
                "post_code": _error_code(old_digest_post_response),
                "no_new_human_decision": True,
                "pre_readback": _business_projection(old_digest_before),
                "post_readback": _business_projection(old_digest_after),
            }

            unauthorized_before = old_digest_after
            unauthorized_post_response = client.post(
                f"/api/assurance/changes/{fresh_case_id}/decisions",
                headers={"Idempotency-Key": "p-c-unauthorized-action"},
                json=_decision_payload(
                    subject_digest=fresh_subject,
                    decision_id="p-c-unauthorized-waiver",
                    decision="waiver",
                ),
            )
            assert unauthorized_post_response.status_code == 409
            assert _error_code(unauthorized_post_response) == "ACTION_NOT_ALLOWED"
            unauthorized_after = client.get(
                f"/api/assurance/changes/{fresh_case_id}"
            ).json()
            _assert_no_authoritative_drift(unauthorized_before, unauthorized_after)
            record["unauthorized_action_counterexample"] = {
                "attempted_action": "waiver",
                "post_status": unauthorized_post_response.status_code,
                "post_code": _error_code(unauthorized_post_response),
                "no_new_human_decision": True,
                "pre_readback": _business_projection(unauthorized_before),
                "post_readback": _business_projection(unauthorized_after),
            }
    finally:
        app.dependency_overrides.clear()

    (output_dir / "walkthrough.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "walkthrough.log").write_text(
        "P-C server-owned composition walkthrough: PASS\n"
        "FRESH before decision: FRESH / FRESHNESS_MATCH / live_required\n"
        "authoritative POST -> GET readback: exact business match\n"
        "UNAVAILABLE current-digest POST: fail closed; authoritative GET no drift\n"
        "STALE -> server-owned INVALIDATED: POST fail closed; no new decision\n"
        "INVALIDATED / old digest / unauthorized action: fail closed\n"
        "synthetic isolated CI evidence only; not real CodeMesh dogfood evidence\n",
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
