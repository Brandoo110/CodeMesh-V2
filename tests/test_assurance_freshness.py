"""Focused tests for the server-owned live Git and intake freshness seam."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from assurance.contracts import HumanDecision
from assurance.intake import IntakeChangedError
from assurance.live_freshness import (
    FreshnessStatus,
    LiveFreshnessChecker,
)
from assurance.snapshot import GitSnapshotError
from assurance.state_machine import AcceptanceEvent
from tests.test_assurance_run_service import _service
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebRepository,
    get_assurance_repository,
)
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.server import create_app
from fastapi.testclient import TestClient


def _run_bundle(tmp_path: Path):
    service, intent = _service(tmp_path)
    return service, intent, asyncio.run(
        service.run(intent, idempotency_key="freshness-baseline")
    ).bundle


def _durable_run(tmp_path: Path):
    repository = AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=LiveFreshnessChecker(workspace_root=tmp_path.resolve()),
        live_required=True,
    )
    repository.initialize()
    service, intent = _service(tmp_path, committer=repository)
    result = asyncio.run(service.run(intent, idempotency_key="durable-freshness"))
    return repository, result.bundle, tmp_path.resolve()


def _decision_args(bundle, *, suffix: str):
    human = HumanDecision(
        decision_id=f"human-live-{suffix}",
        subject_digest=bundle.subject.subject_digest,
        owner="release-owner",
        owner_role="release_owner",
        decision="approve",
        reason="live freshness fence test",
        decided_at=bundle.completed_at,
    )
    event = AcceptanceEvent(
        event_id=f"decision:human-live-{suffix}",
        subject_digest=bundle.subject.subject_digest,
        kind="ACCEPT",
        policy_decision_refs=(bundle.policy.decision.decision_id,),
        human_decision_refs=(human.decision_id,),
        occurred_at=bundle.completed_at,
    )
    return human, event


def _write_counts(repository: AssuranceWebRepository, case_id: str):
    with sqlite3.connect(repository._db_path) as conn:
        return tuple(
            conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM assurance_decisions WHERE case_id = ?), "
                "(SELECT COUNT(*) FROM assurance_case_events WHERE case_id = ?), "
                "(SELECT COUNT(*) FROM assurance_web_idempotency "
                " WHERE operation = 'decide' AND result_json LIKE ?)",
                (case_id, case_id, f"%{case_id}%"),
            ).fetchone()
        )


def test_live_freshness_is_fresh_when_git_and_intake_match(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.FRESH
    assert result.reason_code == "FRESHNESS_MATCH"
    assert result.expected_subject_digest == bundle.subject.subject_digest
    assert result.observed_subject_digest == bundle.subject.subject_digest


def test_live_freshness_is_stale_when_task_document_changes(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    task_path = bundle.freshness_source_binding.repository_path / "TASK.md"
    task_path.write_text(task_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.STALE
    assert result.reason_code == "FRESHNESS_MISMATCH"


@pytest.mark.parametrize("changed_file", ("changed.txt", "POLICY.md"))
def test_live_freshness_is_stale_when_worktree_or_policy_changes(
    tmp_path, changed_file
):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    changed_path = bundle.freshness_source_binding.repository_path / changed_file
    changed_path.write_text(
        changed_path.read_text(encoding="utf-8") + "\nchanged\n",
        encoding="utf-8",
    )

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.STALE
    assert result.reason_code == "FRESHNESS_MISMATCH"


def test_live_freshness_is_unavailable_when_binding_base_ref_is_invalid(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    binding = bundle.freshness_source_binding.model_copy(
        update={"requested_base_ref": "HEAD~1"}
    )

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "LIVE_COLLECTION_FAILED"


def test_live_freshness_is_unavailable_for_repository_symlink(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    real_repository = bundle.freshness_source_binding.repository_path
    symlink = tmp_path / "repository-link"
    symlink.symlink_to(real_repository, target_is_directory=True)
    binding = bundle.freshness_source_binding.model_copy(
        update={"repository_path": symlink}
    )

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "REPOSITORY_PATH_INVALID"
    assert str(real_repository) not in result.reason_code


def test_live_freshness_is_unavailable_for_corrupt_baseline(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    corrupt_git = bundle.git.snapshot.model_copy(
        update={"subject_digest": "sha256:" + "f" * 64}
    )

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        bundle.freshness_source_binding,
        corrupt_git,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "BASELINE_CORRUPT"


def test_live_freshness_is_unavailable_for_outside_repository(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    binding = bundle.freshness_source_binding.model_copy(
        update={"repository_path": tmp_path.parent}
    )

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "REPOSITORY_PATH_INVALID"


def test_live_freshness_is_unavailable_for_git_failure(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)

    class _FailingCollector:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def _resolve_repository_root(repository_root):
            return repository_root

        def collect(self, *_args, **_kwargs):
            raise GitSnapshotError("private git detail")

    result = LiveFreshnessChecker(
        workspace_root=tmp_path,
        git_collector_factory=_FailingCollector,
    ).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "LIVE_COLLECTION_FAILED"


def test_live_freshness_is_unavailable_for_baseline_truncation(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)
    truncated = bundle.git.snapshot.model_copy(update={"complete": False})

    result = LiveFreshnessChecker(workspace_root=tmp_path).check(
        bundle.freshness_source_binding,
        truncated,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "BASELINE_GIT_TRUNCATED"


@pytest.mark.parametrize("failure", ("incomplete", "concurrent"))
def test_live_freshness_treats_incomplete_or_concurrent_intake_as_unavailable(
    tmp_path, failure
):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)

    class _IntakeCollector:
        def probe_task_digest(self, *_args, **_kwargs):
            return bundle.intake.snapshot.task_digest

        def collect(self, *_args, **_kwargs):
            if failure == "concurrent":
                raise IntakeChangedError("private path changed during collection")
            incomplete_snapshot = bundle.intake.snapshot.model_copy(
                update={"complete": False}
            )
            return bundle.intake.model_copy(update={"snapshot": incomplete_snapshot})

    result = LiveFreshnessChecker(
        workspace_root=tmp_path,
        intake_collector=_IntakeCollector(),
    ).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "LIVE_INTAKE_UNAVAILABLE"


def test_live_freshness_requires_a_verifiable_git_top_level(tmp_path):
    _service_instance, _intent, bundle = _run_bundle(tmp_path)

    class _CollectorWithoutResolver:
        def __init__(self, **_kwargs):
            pass

        def collect(self, *_args, **_kwargs):
            return bundle.git

    result = LiveFreshnessChecker(
        workspace_root=tmp_path,
        git_collector_factory=_CollectorWithoutResolver,
    ).check(
        bundle.freshness_source_binding,
        bundle.git.snapshot,
        bundle.intake.snapshot,
    )

    assert result.status is FreshnessStatus.UNAVAILABLE
    assert result.reason_code == "GIT_ROOT_UNAVAILABLE"


def test_decision_idempotency_replay_rechecks_live_freshness(tmp_path):
    repository, bundle, root = _durable_run(tmp_path)
    human, event = _decision_args(bundle, suffix="replay")
    repository.decide(
        bundle.case.case_id,
        human,
        event,
        "decision-live-replay",
        {"decision_id": human.decision_id},
    )
    before = _write_counts(repository, bundle.case.case_id)
    task_path = root / "repo" / "TASK.md"
    task_path.write_text(
        task_path.read_text(encoding="utf-8") + "\nchanged before replay\n",
        encoding="utf-8",
    )

    with pytest.raises(AssuranceWebConflictError, match="FRESHNESS_MISMATCH"):
        repository.decide(
            bundle.case.case_id,
            human,
            event,
            "decision-live-replay",
            {"decision_id": human.decision_id},
        )

    assert _write_counts(repository, bundle.case.case_id) == (
        before[0],
        before[1] + 1,
        before[2],
    )
    assert repository.get_change(bundle.case.case_id)["case"]["state"] == "INVALIDATED"


def test_live_stale_detail_persists_idempotent_invalidation(tmp_path):
    repository, bundle, root = _durable_run(tmp_path)
    task_path = root / "repo" / "TASK.md"
    original_task = task_path.read_text(encoding="utf-8")
    task_path.write_text(original_task + "\nchanged before detail\n", encoding="utf-8")

    before = _write_counts(repository, bundle.case.case_id)
    detail = repository.get_change(bundle.case.case_id)
    after_detail = _write_counts(repository, bundle.case.case_id)

    assert after_detail == (before[0], before[1] + 1, before[2])
    assert detail["case"]["state"] == "INVALIDATED"
    assert detail["freshness"]["status"] == "STALE"
    assert detail["timeline"][-1]["kind"] == "INVALIDATE"

    passport = repository.get_passport(bundle.case.case_id)
    assert passport["canonical"]["state"] == "INVALIDATED"
    assert "State: **INVALIDATED**" in passport["markdown"]
    assert _write_counts(repository, bundle.case.case_id) == after_detail

    task_path.write_text(original_task, encoding="utf-8")
    recovered = repository.get_change(bundle.case.case_id)
    assert recovered["freshness"]["status"] == "FRESH"
    assert recovered["case"]["state"] == "INVALIDATED"
    assert _write_counts(repository, bundle.case.case_id) == after_detail


def test_decision_replay_does_not_resurrect_persisted_invalidation(tmp_path):
    repository, bundle, root = _durable_run(tmp_path)
    human, event = _decision_args(bundle, suffix="resurrection")
    repository.decide(
        bundle.case.case_id,
        human,
        event,
        "decision-live-resurrection",
        {"decision_id": human.decision_id},
    )

    task_path = root / "repo" / "TASK.md"
    original_task = task_path.read_text(encoding="utf-8")
    task_path.write_text(original_task + "\nchanged before replay\n", encoding="utf-8")
    stale = repository.get_change(bundle.case.case_id)
    assert stale["case"]["state"] == "INVALIDATED"
    before_replay = _write_counts(repository, bundle.case.case_id)

    task_path.write_text(original_task, encoding="utf-8")
    with pytest.raises(AssuranceWebConflictError, match="invalidated"):
        repository.decide(
            bundle.case.case_id,
            human,
            event,
            "decision-live-resurrection",
            {"decision_id": human.decision_id},
        )

    assert _write_counts(repository, bundle.case.case_id) == before_replay
    recovered = repository.get_change(bundle.case.case_id)
    assert recovered["freshness"]["status"] == "FRESH"
    assert recovered["case"]["state"] == "INVALIDATED"
    assert _write_counts(repository, bundle.case.case_id) == before_replay


def test_live_stale_invalidation_avoids_existing_event_id(tmp_path):
    repository, bundle, root = _durable_run(tmp_path)
    base_event_id = f"live-freshness:{bundle.subject.subject_digest}:invalidate"
    repository.collect(
        bundle.case.case_id,
        AcceptanceEvent(
            event_id=base_event_id,
            subject_digest=bundle.subject.subject_digest,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(bundle.evidence[0].evidence_id,),
            occurred_at=bundle.case.updated_at,
        ),
        None,
        "freshness-colliding-event",
        {"event_id": base_event_id},
    )
    task_path = root / "repo" / "TASK.md"
    task_path.write_text(
        task_path.read_text(encoding="utf-8") + "\nchanged after colliding event\n",
        encoding="utf-8",
    )

    before = _write_counts(repository, bundle.case.case_id)
    detail = repository.get_change(bundle.case.case_id)
    after = _write_counts(repository, bundle.case.case_id)

    assert after == (before[0], before[1] + 1, before[2])
    assert detail["case"]["state"] == "INVALIDATED"
    assert detail["timeline"][-1]["kind"] == "INVALIDATE"
    assert detail["timeline"][-1]["id"].startswith(base_event_id)
    assert detail["timeline"][-1]["id"] != base_event_id


def test_product_run_dependencies_reject_database_only_repository(tmp_path):
    service, _intent = _service(tmp_path)
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()

    with pytest.raises(ValueError, match="live-required"):
        AssuranceRunWebDependencies(service=service, repository=repository)


def test_product_repository_factory_requires_explicit_live_composition(tmp_path):
    with pytest.raises(AssuranceWebError, match="explicit live composition"):
        get_assurance_repository(tmp_path / "assurance.sqlite")


def test_live_required_list_does_not_scan_and_marks_freshness_not_checked(tmp_path):
    class _ExplodingChecker:
        def __init__(self):
            self.calls = 0

        def check(self, *_args):
            self.calls += 1
            raise AssertionError("list projection must not scan live sources")

    checker = _ExplodingChecker()
    repository = AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=checker,
        live_required=True,
    )
    repository.initialize()
    service, intent = _service(tmp_path, committer=repository)
    bundle = asyncio.run(service.run(intent, idempotency_key="list-not-checked")).bundle

    rows = repository.list_changes()

    assert rows[0]["freshness"]["status"] == "UNAVAILABLE"
    assert rows[0]["freshness"]["reason_code"] == "FRESHNESS_NOT_CHECKED"
    assert rows[0]["digest_freshness"] is False
    assert [item["code"] for item in rows[0]["allowed_actions"]] == [
        "download_passport"
    ]
    assert checker.calls == 0
    assert bundle.case.case_id == rows[0]["case_id"]


def test_live_detail_and_passport_share_the_same_freshness_overlay(tmp_path):
    repository, bundle, _root = _durable_run(tmp_path)

    detail = repository.get_change(bundle.case.case_id)
    passport = repository.get_passport(bundle.case.case_id)

    assert detail["freshness"]["status"] == "FRESH"
    assert detail["freshness"]["reason_code"] == "FRESHNESS_MATCH"
    assert {
        key: passport["canonical"]["freshness"][key]
        for key in ("status", "reason_code", "expected_subject_digest", "observed_subject_digest")
    } == {
        key: detail["freshness"][key]
        for key in ("status", "reason_code", "expected_subject_digest", "observed_subject_digest")
    }
    assert "Status: **FRESH**" in passport["markdown"]


@pytest.mark.parametrize("mutation", ("task", "checker"))
def test_live_decision_fence_returns_409_without_writes(tmp_path, mutation):
    repository, bundle, root = _durable_run(tmp_path)
    if mutation == "task":
        task_path = root / "repo" / "TASK.md"
        task_path.write_text(
            task_path.read_text(encoding="utf-8") + "\nchanged before decision\n",
            encoding="utf-8",
        )
        reason_code = "FRESHNESS_MISMATCH"
    else:
        repository._freshness_checker = None
        reason_code = "NO_FRESHNESS_CHECKER"

    before = _write_counts(repository, bundle.case.case_id)
    human, event = _decision_args(bundle, suffix=mutation)
    with pytest.raises(AssuranceWebConflictError, match=reason_code):
        repository.decide(
            bundle.case.case_id,
            human,
            event,
            f"decision-live-{mutation}",
            {"decision_id": human.decision_id},
        )
    after = _write_counts(repository, bundle.case.case_id)

    expected_event_delta = 1 if mutation == "task" else 0
    assert after == (before[0], before[1] + expected_event_delta, before[2])
    projection = repository.get_change(bundle.case.case_id)
    assert projection["freshness"]["reason_code"] == reason_code
    assert projection["case"]["human_decision_refs"] == []
    assert projection["case"]["state"] == (
        "INVALIDATED" if mutation == "task" else bundle.case.state
    )

    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    with TestClient(app) as client:
        response = client.post(
            f"/api/assurance/changes/{bundle.case.case_id}/decisions",
            headers={"Idempotency-Key": f"http-decision-live-{mutation}"},
            json={
                "decision_id": f"http-human-live-{mutation}",
                "subject_digest": bundle.subject.subject_digest,
                "owner": "release-owner",
                "owner_role": "release_owner",
                "decision": "approve",
                "reason": "live freshness HTTP fence test",
                "decided_at": bundle.completed_at.isoformat(),
            },
        )
    app.dependency_overrides.clear()
    assert response.status_code == 409
    assert str(root) not in response.text
