from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import assurance.snapshot as snapshot_module
import pytest

from assurance.contracts import Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.remediation import (
    RemediationController,
    RemediationPolicy,
    RemediationRequest,
    RemediationStatus,
    PreparedRemediationHandoff,
)
from assurance.remediation_reviewer import (
    AssuranceRemediationReviewer,
    PreparedReviewerRerun,
)
from assurance.remediation_validation import ValidationStatus
from assurance.remediation_workspace import IsolatedWorkspace, WorkspaceGrant
from assurance.remediation_workspace import PublicWorkspaceView
from assurance.lifecycle_store import SQLiteAssuranceLifecycleStore
from assurance.run_service import (
    AssuranceRunBundle,
    AssuranceRunConfig,
    ReviewerInvocationResponse,
)
from assurance.store import StoreConflictError
from web.assurance_run_committer import (
    AssuranceRunConflictError,
    _commit_run_in_transaction,
)
from web.assurance_store import AssuranceWebRepository

from tests.test_assurance_remediation import _FakeExecutor, _validation
from tests.test_assurance_run_service import _service


class _FindingReviewer:
    async def invoke(self, prompt, *, run_id, route):
        evidence_id = prompt.input.contexts[0].evidence_id
        payload = {
            "schema_version": "v1",
            "subject_digest": prompt.input.subject.subject_digest,
            "rubric_hash": prompt.rubric_hash,
            "findings": [
                {
                    "reviewer_role": "architecture",
                    "claim": "the selected change needs repair",
                    "evidence_refs": [evidence_id],
                    "severity": "high",
                    "confidence": 1.0,
                }
            ],
            "questions": [],
        }
        return ReviewerInvocationResponse(
            status="success",
            provider=route.provider,
            model_ref=route.model_ref,
            raw_response=json.dumps(payload, separators=(",", ":")).encode(),
            started_at=prompt.input.evaluated_at,
            completed_at=prompt.input.evaluated_at,
            schema_status="unverified",
            usage_status="unavailable",
        )


def _subject() -> SubjectDigestInput:
    return SubjectDigestInput(
        repository="repo",
        base_revision="base",
        head_revision="head",
        normalized_diff_digest="sha256:" + "1" * 64,
        task_digest="sha256:" + "2" * 64,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )


def test_legacy_three_field_mapping_and_duck_are_not_a_prepared_rerun() -> None:
    digest = compute_subject_digest(_subject())

    assert RemediationController._reviewer_receipt(
        {
            "reviewer_role": "architecture",
            "subject_digest": digest,
            "accepted": True,
        },
        "architecture",
        digest,
    ) is None

    class Duck:
        reviewer_role = "architecture"
        subject_digest = digest
        accepted = True

    assert RemediationController._reviewer_receipt(
        Duck(), "architecture", digest
    ) is None
    assert PreparedReviewerRerun is not None


def test_lifecycle_has_no_public_caller_facts_remediation_seam(tmp_path: Path) -> None:
    store = SQLiteAssuranceLifecycleStore(tmp_path / "assurance.sqlite")

    assert not hasattr(store, "commit_remediation")


def _grant() -> WorkspaceGrant:
    return WorkspaceGrant(
        allowed_paths=("TASK.md", "POLICY.md", "changed.txt"),
        max_files=10,
        max_bytes=4096,
    )


def _reconfigure_service(service: object, root: Path) -> None:
    config = service._config  # type: ignore[attr-defined]
    service._config = AssuranceRunConfig(  # type: ignore[attr-defined]
        workspace_root=root,
        allowed_commands=config.allowed_commands,
        orchestration_version=config.orchestration_version,
        redaction_policy_version=config.redaction_policy_version,
        policy_version=config.policy_version,
        rubric_version=config.rubric_version,
        freshness_ttl_seconds=config.freshness_ttl_seconds,
        reviewer_route=config.reviewer_route,
    )


def _baseline_and_changed_subject(
    tmp_path: Path, monkeypatch, *, repository_identity: str = "example/service"
):
    service, intent = _service(tmp_path, reviewer=_FindingReviewer())
    intent = intent.model_copy(update={"repository_identity": repository_identity})
    baseline = asyncio.run(service.prepare(intent, idempotency_key="baseline"))
    workspace = IsolatedWorkspace.prepare(intent.repository_path, _grant())
    workspace.write_text("changed.txt", "repaired\n")
    _reconfigure_service(service, workspace.root)
    captured = []
    original = snapshot_module.compute_subject_digest

    def capture(value):
        captured.append(value)
        return original(value)

    monkeypatch.setattr(snapshot_module, "compute_subject_digest", capture)
    changed_intent = intent.model_copy(update={"repository_path": workspace.root})
    probe_bundle = asyncio.run(
        service.prepare(changed_intent, idempotency_key="probe")
    )
    subject_input = next(
        value
        for value in captured
        if original(value) == probe_bundle.subject.subject_digest
    )
    subject_digest = compute_subject_digest(subject_input)
    canonical_key = (
        "remediation-review:"
        + baseline.run_id
        + ":architecture:"
        + subject_digest
    )
    changed_bundle = asyncio.run(
        service.prepare(changed_intent, idempotency_key=canonical_key)
    )
    return (
        service,
        intent,
        baseline,
        workspace,
        changed_bundle,
        subject_input,
        baseline.findings[0],
    )


def _request_for_baseline(baseline: AssuranceRunBundle):
    finding = baseline.findings[0]
    request = RemediationRequest(
        remediation_id="remediation-1",
        old_case_id=baseline.case.case_id,
        old_subject_digest=baseline.subject.subject_digest,
        human_selected_finding_id=finding.finding_id,
        requested_by="human-owner",
        requested_at=baseline.started_at,
        workspace_grant=_grant(),
        policy=RemediationPolicy(
            max_attempts=1,
            max_agent_iterations=3,
            max_validation_calls_per_attempt=1,
            total_wall_time_s=10.0,
            authoritative_check_id="authoritative",
        ),
    )
    return request, finding


class _RecordingPrepareService:
    def __init__(self, bundle: AssuranceRunBundle) -> None:
        self.bundle = bundle
        self.calls: list[tuple[object, str]] = []

    async def prepare(self, intent, *, idempotency_key):
        self.calls.append((intent, idempotency_key))
        return self.bundle


def _rerun_with_bundle(
    tmp_path: Path, monkeypatch, mutate
):
    _, _, baseline, workspace, changed_bundle, subject_input, selected_finding = (
        _baseline_and_changed_subject(tmp_path, monkeypatch)
    )
    tampered_bundle = mutate(changed_bundle)
    request, _ = _request_for_baseline(baseline)
    recording = _RecordingPrepareService(tampered_bundle)
    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=lambda _root: recording,
    )
    return asyncio.run(
        adapter.rerun(
            reviewer_role="architecture",
            subject_input=subject_input,
            subject_digest=compute_subject_digest(subject_input),
            workspace=workspace,
            request=request,
            selected_finding=selected_finding,
        )
    )


def test_adapter_derives_exact_intent_and_calls_public_prepare_once(
    tmp_path: Path, monkeypatch
) -> None:
    service, intent, baseline, workspace, changed_bundle, subject_input, finding = (
        _baseline_and_changed_subject(tmp_path, monkeypatch)
    )
    request, selected_finding = _request_for_baseline(baseline)
    recording = _RecordingPrepareService(changed_bundle)
    factory_paths: list[Path] = []

    def service_factory(root: Path):
        factory_paths.append(root)
        assert root is workspace.root
        return recording

    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=service_factory,
    )
    prepared = asyncio.run(
        adapter.rerun(
            reviewer_role="architecture",
            subject_input=subject_input,
            subject_digest=compute_subject_digest(subject_input),
            workspace=workspace,
            request=request,
            selected_finding=selected_finding,
        )
    )

    assert type(prepared) is PreparedReviewerRerun
    assert prepared.bundle == changed_bundle
    assert prepared.bundle is not changed_bundle
    assert factory_paths == [workspace.root]
    assert len(recording.calls) == 1
    derived, idempotency_key = recording.calls[0]
    source = baseline.freshness_source_binding
    declarations = baseline.risk.input.declarations
    assert derived.repository_path == workspace.root
    assert derived.repository_identity == source.repository_identity
    assert derived.author == source.author
    assert derived.author_provenance == source.author_provenance
    assert derived.base_ref == source.requested_base_ref
    assert derived.task_path == source.task_path
    assert derived.policy_paths == source.policy_paths
    assert derived.adr_paths == source.adr_paths
    assert derived.runbook_paths == source.runbook_paths
    assert derived.command_ids == tuple(
        item.command_id for item in baseline.commands.snapshot.commands
    )
    assert derived.changed_lines_total == declarations.changed_lines_total
    assert derived.external_side_effects == declarations.external_side_effects
    assert derived.provider_boundary == declarations.provider_boundary
    assert idempotency_key


def test_exact_prepared_success_binds_receipt_and_hides_bundle_from_result(
    tmp_path: Path, monkeypatch
) -> None:
    service, intent, baseline, expected_workspace, changed_bundle, subject_input, finding = (
        _baseline_and_changed_subject(tmp_path, monkeypatch)
    )
    old_digest = baseline.subject.subject_digest
    request, finding = _request_for_baseline(baseline)
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])
    factory_paths: list[Path] = []

    def service_factory(root: Path):
        factory_paths.append(root)
        _reconfigure_service(service, root)
        return service

    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=service_factory,
    )
    controller_workspace: list[PublicWorkspaceView] = []
    reviewer_workspace: list[IsolatedWorkspace] = []

    async def agent(*, workspace: PublicWorkspaceView, **_: object) -> None:
        controller_workspace.append(workspace)
        workspace.write_text("changed.txt", "repaired\n")

    async def reviewer(**kwargs: object):
        reviewer_workspace.append(kwargs["workspace"])  # type: ignore[arg-type]
        return await adapter.rerun(
            reviewer_role=kwargs["reviewer_role"],
            subject_input=kwargs["subject_input"],
            subject_digest=kwargs["subject_digest"],
            workspace=kwargs["workspace"],
            request=kwargs["request"],
            selected_finding=kwargs["selected_finding"],
        )

    controller = RemediationController(
        request=request,
        selected_finding=finding,
        seed_root=intent.repository_path,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda _patch_digest: subject_input,
        reviewer_rerunner=reviewer,
    )
    handoff = asyncio.run(controller.prepare(agent))
    assert type(handoff) is PreparedRemediationHandoff
    assert type(handoff.bundle) is AssuranceRunBundle
    result = handoff.result

    assert result.status is RemediationStatus.SUCCEEDED
    assert type(controller_workspace[0]) is PublicWorkspaceView
    assert type(reviewer_workspace[0]) is IsolatedWorkspace
    assert factory_paths and factory_paths[0] is reviewer_workspace[0].root
    assert result.reviewer_receipts[0].reviewer.status == "success"
    assert result.reviewer_receipts[0].reviewer == handoff.bundle.reviewer
    assert (
        result.reviewer_receipts[0].execution_receipt
        == handoff.bundle.execution_receipt
    )
    assert result.reviewer_receipts[0].execution_receipt.overall_result == "success"
    assert result.reviewer_receipts[0].execution_receipt.subject_digest == (
        result.new_subject_digest
    )
    selected_step = next(
        step
        for step in result.reviewer_receipts[0].execution_receipt.steps
        if step.planned_role == "architecture"
    )
    assert selected_step.actual_role == "architecture"
    assert "bundle" not in result.model_dump(mode="json")
    assert "accepted" not in result.model_dump(mode="json")
    tampered_results = (
        result.model_copy(update={"transition_state": "not_prepared"}),
        result.model_copy(update={"status": RemediationStatus.NOOP}),
        result.model_copy(
            update={
                "reviewer_receipts": (
                    result.reviewer_receipts[0].model_copy(
                        update={
                            "execution_receipt": handoff.bundle.execution_receipt.model_copy(
                                update={"run_id": "forged-run"}
                            )
                        }
                    ),
                )
            }
        ),
    )
    for tampered in tampered_results:
        with pytest.raises(ValueError):
            PreparedRemediationHandoff(result=tampered, bundle=handoff.bundle)
    with pytest.raises(ValueError):
        PreparedRemediationHandoff(result=result)
    bad_binding = handoff.bundle.binding.model_copy(
        update={"subject_digest": "sha256:" + "f" * 64}
    )
    with pytest.raises(ValueError):
        PreparedRemediationHandoff(
            result=result,
            bundle=handoff.bundle.model_copy(update={"binding": bad_binding}),
        )

    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    with repository._store._transaction() as unit_of_work:
        unit_of_work.create_case(baseline.draft_case, baseline.binding)
        lifecycle_sql: list[str] = []
        unit_of_work.connection.set_trace_callback(lifecycle_sql.append)
        try:
            remediation_receipt = (
                repository._store._commit_prepared_remediation_in_transaction(
                    unit_of_work,
                    request=request,
                    handoff=handoff,
                    selected_finding=finding,
                )
            )
        finally:
            unit_of_work.connection.set_trace_callback(None)
        assert not any(
            statement.lstrip().upper().split(maxsplit=1)[0]
            in {"BEGIN", "COMMIT", "ROLLBACK"}
            for statement in lifecycle_sql
        )
        assert (
            repository._store._commit_prepared_remediation_in_transaction(
                unit_of_work,
                request=request,
                handoff=handoff,
                selected_finding=finding,
            )
            == remediation_receipt
        )
        run_sql: list[str] = []
        unit_of_work.connection.set_trace_callback(run_sql.append)
        try:
            committed = _commit_run_in_transaction(
                unit_of_work,
                handoff.bundle,
                idempotency_key=handoff.bundle.idempotency_key,
                request_digest=handoff.bundle.request_digest,
            )
        finally:
            unit_of_work.connection.set_trace_callback(None)
        assert not any(
            statement.lstrip().upper().split(maxsplit=1)[0]
            in {"BEGIN", "COMMIT", "ROLLBACK"}
            for statement in run_sql
        )
        replay = _commit_run_in_transaction(
            unit_of_work,
            handoff.bundle,
            idempotency_key=handoff.bundle.idempotency_key,
            request_digest=handoff.bundle.request_digest,
        )
        assert remediation_receipt.new_case_id == handoff.bundle.draft_case.case_id
        assert committed.cached is False
        assert replay.cached is True
        assert unit_of_work.load_case(remediation_receipt.new_case_id).case.state == "DRAFT"

        with pytest.raises(StoreConflictError):
            repository._store._commit_prepared_remediation_in_transaction(
                unit_of_work,
                request=request,
                handoff=handoff,
                selected_finding=finding.model_copy(update={"claim": "forged"}),
            )

    conn = sqlite3.connect(tmp_path / "assurance.sqlite")
    try:
        rows = conn.execute(
            "SELECT case_id, bundle_json FROM assurance_web_runs"
        ).fetchone()
        assert rows is not None
        stored_case_id, stored_bundle_json = rows
        stored_bundle = json.loads(stored_bundle_json)
        assert stored_case_id == handoff.bundle.case.case_id
        assert stored_case_id == handoff.bundle.draft_case.case_id
        assert stored_bundle["case"]["case_id"] == stored_case_id
        assert stored_bundle["draft_case"]["case_id"] == stored_case_id
        assert stored_bundle["draft_case"]["state"] == "DRAFT"
        assert stored_bundle["case"]["state"] in {"EVIDENCE_COLLECTED", "NEEDS_EVIDENCE"}
        assert "freshness_source_binding" not in stored_bundle
    finally:
        conn.close()


def test_uow_run_helper_conflict_and_outer_rollback(tmp_path: Path, monkeypatch) -> None:
    _, _, baseline, _, changed_bundle, _, _ = _baseline_and_changed_subject(
        tmp_path, monkeypatch
    )
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    with repository._store._transaction() as unit_of_work:
        unit_of_work.create_case(changed_bundle.draft_case, changed_bundle.binding)
        _commit_run_in_transaction(
            unit_of_work,
            changed_bundle,
            idempotency_key=changed_bundle.idempotency_key,
            request_digest=changed_bundle.request_digest,
        )

    forged = changed_bundle.model_copy(
        update={"completed_at": changed_bundle.completed_at + timedelta(seconds=1)}
    )
    with repository._store._transaction() as unit_of_work:
        with pytest.raises(AssuranceRunConflictError):
            _commit_run_in_transaction(
                unit_of_work,
                forged,
                idempotency_key=changed_bundle.idempotency_key,
                request_digest=changed_bundle.request_digest,
            )

    try:
        with repository._store._transaction() as unit_of_work:
            draft = baseline.draft_case
            unit_of_work.create_case(draft, baseline.binding)
            _commit_run_in_transaction(
                unit_of_work,
                baseline,
                idempotency_key=baseline.idempotency_key,
                request_digest=baseline.request_digest,
            )
            raise RuntimeError("outer rollback")
    except RuntimeError:
        pass

    conn = sqlite3.connect(tmp_path / "assurance.sqlite")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM assurance_cases WHERE case_id = ?",
            (baseline.draft_case.case_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM assurance_web_runs WHERE idempotency_key = ?",
            (baseline.idempotency_key,),
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_role_digest_and_provenance_mismatches_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    service, intent, baseline, workspace, changed_bundle, subject_input, finding = (
        _baseline_and_changed_subject(tmp_path, monkeypatch)
    )
    request, selected_finding = _request_for_baseline(baseline)
    digest = compute_subject_digest(subject_input)
    recording = _RecordingPrepareService(changed_bundle)
    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=lambda _root: recording,
    )
    prepared = asyncio.run(
        adapter.rerun(
            reviewer_role="architecture",
            subject_input=subject_input,
            subject_digest=digest,
            workspace=workspace,
            request=request,
            selected_finding=selected_finding,
        )
    )
    assert type(prepared) is PreparedReviewerRerun
    assert RemediationController._reviewer_receipt(
        prepared, "intent", digest
    ) is None
    assert RemediationController._reviewer_receipt(
        prepared, "architecture", "sha256:" + "f" * 64
    ) is None

    bad_source = changed_bundle.freshness_source_binding.model_copy(
        update={"repository_path": workspace.root.parent}
    )
    bad_bundle = changed_bundle.model_copy(
        update={"freshness_source_binding": bad_source}
    )
    bad_recording = _RecordingPrepareService(bad_bundle)
    bad_adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=lambda _root: bad_recording,
    )
    assert (
        asyncio.run(
            bad_adapter.rerun(
                reviewer_role="architecture",
                subject_input=subject_input,
                subject_digest=digest,
                workspace=workspace,
                request=request,
                selected_finding=selected_finding,
            )
        )
        is None
    )


def test_adapter_rejects_cross_case_baseline_request(
    tmp_path: Path, monkeypatch
) -> None:
    first = _baseline_and_changed_subject(tmp_path, monkeypatch)
    other_root = tmp_path / "other-case"
    other_root.mkdir()
    second = _baseline_and_changed_subject(
        other_root, monkeypatch, repository_identity="example/other"
    )
    _, _, baseline, workspace, _, subject_input, _ = first
    _, _, other_baseline, _, _, _, other_finding = second
    request, _ = _request_for_baseline(other_baseline)
    recording = _RecordingPrepareService(first[4])
    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=lambda _root: recording,
    )

    prepared = asyncio.run(
        adapter.rerun(
            reviewer_role="architecture",
            subject_input=subject_input,
            subject_digest=compute_subject_digest(subject_input),
            workspace=workspace,
            request=request,
            selected_finding=other_finding,
        )
    )

    assert prepared is None
    assert recording.calls == []


def test_adapter_rejects_model_copy_tampered_bundle_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, baseline, workspace, changed_bundle, subject_input, selected_finding = (
        _baseline_and_changed_subject(tmp_path, monkeypatch)
    )
    request, _ = _request_for_baseline(baseline)
    digest = compute_subject_digest(subject_input)
    tampered_bundles = (
        changed_bundle.model_copy(update={"idempotency_key": "forged-key"}),
        changed_bundle.model_copy(
            update={
                "freshness_source_binding": changed_bundle.freshness_source_binding.model_copy(
                    update={"author": "forged-author"}
                )
            }
        ),
        changed_bundle.model_copy(
            update={
                "execution_receipt": changed_bundle.execution_receipt.model_copy(
                    update={"run_id": "forged-run"}
                )
            }
        ),
        changed_bundle.model_copy(
            update={
                "reviewer": changed_bundle.reviewer.model_copy(
                    update={"actual_model_ref": "forged-model"}
                )
            }
        ),
    )

    for tampered in tampered_bundles:
        recording = _RecordingPrepareService(tampered)
        adapter = AssuranceRemediationReviewer(
            baseline_bundle=baseline,
            service_factory=lambda _root, recording=recording: recording,
        )
        assert (
            asyncio.run(
                adapter.rerun(
                    reviewer_role="architecture",
                    subject_input=subject_input,
                    subject_digest=digest,
                    workspace=workspace,
                    request=request,
                    selected_finding=selected_finding,
                )
            )
            is None
        )


def test_adapter_rejects_model_copy_receipt_missing_role_step(
    tmp_path: Path, monkeypatch
) -> None:
    def mutate(bundle: AssuranceRunBundle) -> AssuranceRunBundle:
        receipt = bundle.execution_receipt.model_copy(
            update={"steps": bundle.execution_receipt.steps[:-1]}
        )
        return bundle.model_copy(update={"execution_receipt": receipt})

    assert _rerun_with_bundle(tmp_path, monkeypatch, mutate) is None


def test_adapter_rejects_synchronized_short_receipt_model_copy(
    tmp_path: Path, monkeypatch
) -> None:
    def mutate(bundle: AssuranceRunBundle) -> AssuranceRunBundle:
        receipt = bundle.execution_receipt.model_copy(
            update={"steps": bundle.execution_receipt.steps[:-1]}
        )
        policy_input = bundle.policy.input.model_copy(
            update={"execution_receipts": (receipt,)}
        )
        policy = bundle.policy.model_copy(update={"input": policy_input})
        return bundle.model_copy(
            update={"execution_receipt": receipt, "policy": policy}
        )

    assert _rerun_with_bundle(tmp_path, monkeypatch, mutate) is None


def test_adapter_rejects_model_copy_invalid_success_schema(
    tmp_path: Path, monkeypatch
) -> None:
    def mutate(bundle: AssuranceRunBundle) -> AssuranceRunBundle:
        reviewer = bundle.reviewer.model_copy(
            update={"schema_status": "invalid"}
        )
        receipt = bundle.execution_receipt.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(update={"schema_status": "invalid"})
                    for step in bundle.execution_receipt.steps
                )
            }
        )
        return bundle.model_copy(
            update={"reviewer": reviewer, "execution_receipt": receipt}
        )

    assert _rerun_with_bundle(tmp_path, monkeypatch, mutate) is None


def test_adapter_rejects_model_copy_actual_route_off_configured_route(
    tmp_path: Path, monkeypatch
) -> None:
    def mutate(bundle: AssuranceRunBundle) -> AssuranceRunBundle:
        reviewer = bundle.reviewer.model_copy(
            update={
                "actual_provider": "forged-provider",
                "actual_model_ref": "forged-model",
            }
        )
        receipt = bundle.execution_receipt.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(
                        update={
                            "provider": "forged-provider",
                            "model_ref": "forged-model",
                        }
                    )
                    for step in bundle.execution_receipt.steps
                )
            }
        )
        return bundle.model_copy(
            update={"reviewer": reviewer, "execution_receipt": receipt}
        )

    assert _rerun_with_bundle(tmp_path, monkeypatch, mutate) is None


def test_adapter_rejects_model_copy_success_error_code(
    tmp_path: Path, monkeypatch
) -> None:
    def mutate(bundle: AssuranceRunBundle) -> AssuranceRunBundle:
        reviewer = bundle.reviewer.model_copy(
            update={"error_code": "REVIEWER_PROVIDER_FAILURE"}
        )
        return bundle.model_copy(update={"reviewer": reviewer})

    assert _rerun_with_bundle(tmp_path, monkeypatch, mutate) is None
