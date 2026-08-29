from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assurance.contracts import Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.remediation import (
    RemediationAttempt,
    RemediationController,
    RemediationPolicy,
    RemediationRequest,
    RemediationResult,
    RemediationStatus,
    ReviewerRerunReceipt,
    PreparedRemediationHandoff,
)
from assurance.remediation_agent import RemediationAgent
from assurance.remediation_validation import (
    ValidationCheck,
    ValidationExecutor,
    ValidationResult,
    ValidationStatus,
)
from assurance.remediation_workspace import (
    IsolatedWorkspace,
    PublicWorkspaceView,
    WorkspaceGrant,
    WorkspaceViolation,
)


OLD_DIGEST = "sha256:" + "1" * 64
TASK_DIGEST = "sha256:" + "2" * 64
RUBRIC_DIGEST = "sha256:" + "3" * 64


def _subject(*, diff: str = "4" * 64, head: str = "head") -> SubjectDigestInput:
    return SubjectDigestInput(
        repository="repo",
        base_revision="base",
        head_revision=head,
        normalized_diff_digest="sha256:" + diff,
        task_digest=TASK_DIGEST,
        policy_version="policy-v1",
        rubric_version="rubric-v1",
    )


def _finding() -> Finding:
    return Finding(
        finding_id="finding-1",
        subject_digest=OLD_DIGEST,
        reviewer_role="architecture",
        claim="the selected change needs repair",
        evidence_refs=("evidence-1",),
        basis="deterministic",
        severity="high",
        confidence=1.0,
        rubric_hash=RUBRIC_DIGEST,
        model_ref="reviewer-v1",
        status="open",
    )


def _request(grant: WorkspaceGrant, policy: RemediationPolicy) -> RemediationRequest:
    return RemediationRequest(
        remediation_id="remediation-1",
        old_case_id="case-1",
        old_subject_digest=OLD_DIGEST,
        human_selected_finding_id="finding-1",
        requested_by="human-owner",
        requested_at=datetime.now(timezone.utc),
        workspace_grant=grant,
        policy=policy,
    )


def _policy(**overrides: object) -> RemediationPolicy:
    values: dict[str, object] = {
        "max_attempts": 2,
        "max_agent_iterations": 3,
        "max_validation_calls_per_attempt": 1,
        "total_wall_time_s": 10.0,
        "authoritative_check_id": "authoritative",
    }
    values.update(overrides)
    return RemediationPolicy(**values)


def _grant(*paths: str) -> WorkspaceGrant:
    return WorkspaceGrant(allowed_paths=paths, max_files=10, max_bytes=4096)


def _validation(
    check_id: str,
    status: ValidationStatus,
    *,
    reason: str = "fixture",
) -> ValidationResult:
    return ValidationResult(
        check_id=check_id,
        status=status,
        reason_code=reason,
        exit_code=0 if status is ValidationStatus.PASSED else 1,
        duration_ms=1,
        stdout_tail="",
        stderr_tail="",
        truncated=False,
        failure_fingerprint=hashlib.sha256(reason.encode()).hexdigest()[:16],
    )


class _FakeExecutor:
    def __init__(self, statuses: list[ValidationStatus]) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str]] = []

    async def validate(self, check_id: str, *, actor: str) -> ValidationResult:
        self.calls.append((check_id, actor))
        status = self.statuses.pop(0)
        return _validation(check_id, status)


def _run(controller: RemediationController, agent: object):
    return asyncio.run(controller.run(agent))


def test_prepare_returns_non_persistable_handoff_and_run_delegates_once(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy())
    executor = _FakeExecutor([ValidationStatus.PASSED, ValidationStatus.PASSED])
    calls: list[str] = []

    async def agent(**_: object) -> None:
        calls.append("agent")
        raise AssertionError("agent must not run after a passing baseline")

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: _subject(
            diff=patch_digest.removeprefix("sha256:"), head="new-head"
        ),
        reviewer_rerunner=lambda *_: None,
    )

    handoff = asyncio.run(controller.prepare(agent))
    assert type(handoff) is PreparedRemediationHandoff
    assert handoff.result.status is RemediationStatus.NOOP
    assert handoff.bundle is None
    assert "bundle" not in handoff.model_dump(mode="json")
    assert '"bundle"' not in handoff.model_dump_json()

    prepare_calls: list[object] = []
    original_prepare = controller.prepare

    async def counted_prepare(agent: object) -> PreparedRemediationHandoff:
        prepare_calls.append(agent)
        return await original_prepare(agent)

    controller.prepare = counted_prepare  # type: ignore[method-assign]
    second = asyncio.run(controller.run(agent))
    assert second == handoff.result
    assert prepare_calls == [agent]
    assert calls == []
    assert executor.calls == [
        ("authoritative", "controller"),
        ("authoritative", "controller"),
    ]


def test_workspace_rejects_escape_duplicate_and_symlink(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "fix.py").write_text("old", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    (seed / "src" / "link.py").symlink_to(outside)

    with pytest.raises(ValueError):
        WorkspaceGrant(allowed_paths=("src/fix.py", "src/./fix.py"))

    grant = _grant("src/fix.py", "src/link.py")
    with IsolatedWorkspace.prepare(seed, grant) as workspace:
        with pytest.raises(WorkspaceViolation):
            workspace.read_text("../outside.py")
        with pytest.raises(WorkspaceViolation):
            workspace.read_text("src/other.py")
        with pytest.raises(WorkspaceViolation):
            workspace.read_text("src/link.py")


def test_workspace_publishes_repaired_root_after_temp_cleanup_without_overwrite(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("repaired", encoding="utf-8")
    durable_root = tmp_path / "workspace-root"
    durable_root.mkdir()
    grant = _grant("fix.py")

    with IsolatedWorkspace.prepare(seed, grant, parent=durable_root) as workspace:
        published = workspace.publish(
            parent=durable_root,
            remediation_id="remediation-1",
            subject_digest="sha256:" + "a" * 64,
        )
        assert published.is_relative_to(durable_root)
        assert published.is_dir()
        assert (published / "fix.py").read_text(encoding="utf-8") == "repaired"
        assert workspace.root == published

    assert published.is_dir()
    assert (published / "fix.py").read_text(encoding="utf-8") == "repaired"

    with IsolatedWorkspace.prepare(seed, grant, parent=durable_root) as workspace:
        with pytest.raises(WorkspaceViolation):
            workspace.publish(
                parent=durable_root,
                remediation_id="remediation-1",
                subject_digest="sha256:" + "a" * 64,
            )


def test_controller_publishes_before_reviewer_and_keeps_durable_root(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    durable_root = tmp_path / "workspace-root"
    durable_root.mkdir()
    request = _request(_grant("fix.py"), _policy(max_attempts=1))
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])
    reviewer_roots: list[Path] = []

    async def agent(*, workspace: PublicWorkspaceView, **_: object) -> None:
        workspace.write_text("fix.py", "repaired")

    async def reviewer(**kwargs: object) -> None:
        reviewer_roots.append(kwargs["workspace"].root)  # type: ignore[union-attr]
        return None

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: _subject(
            diff=patch_digest.removeprefix("sha256:"), head="new-head"
        ),
        reviewer_rerunner=reviewer,
        workspace_parent=durable_root,
    )

    result = _run(controller, agent)

    assert result.status is RemediationStatus.BLOCKED
    assert result.reason_code == "reviewer_subject_mismatch"
    assert len(reviewer_roots) == 1
    published = reviewer_roots[0]
    assert published.is_relative_to(durable_root)
    assert published.is_dir()
    assert (published / "fix.py").read_text(encoding="utf-8") == "repaired"
    assert not tuple(durable_root.glob("codemesh-remediation-*"))


def test_baseline_pass_is_noop_and_does_not_build_subject(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy())
    executor = _FakeExecutor([ValidationStatus.PASSED])
    built: list[str] = []

    async def agent(**_: object) -> None:
        raise AssertionError("agent must not run after a passing baseline")

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: built.append(patch_digest),
        reviewer_rerunner=lambda *_: None,
    )
    result = _run(controller, agent)

    assert result.status is RemediationStatus.NOOP
    assert result.transition_state == "prepared"
    assert result.new_subject_digest is None
    assert result.rerun_roles == ()
    assert built == []
    assert executor.calls == [("authoritative", "controller")]


def test_real_agent_mutation_returns_to_controller_validation_then_reviewer(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy(max_attempts=1))
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])

    class MutationAdapter:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, str]], str]] = []

        async def complete(
            self, messages: list[dict[str, str]], system: str = ""
        ) -> str:
            self.calls.append(([dict(message) for message in messages], system))
            return '{"action":"write","path":"fix.py","content":"repaired"}'

    adapter = MutationAdapter()
    reviewer_calls: list[dict[str, object]] = []

    def reviewer(**kwargs: object) -> None:
        reviewer_calls.append(kwargs)
        return None

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: _subject(
            diff=patch_digest.removeprefix("sha256:"), head="new-head"
        ),
        reviewer_rerunner=reviewer,
    )

    result = _run(controller, RemediationAgent(adapter))

    assert result.status is RemediationStatus.BLOCKED
    assert result.reason_code == "reviewer_subject_mismatch"
    assert len(adapter.calls) == 1
    assert executor.calls == [
        ("authoritative", "controller"),
        ("authoritative", "controller"),
    ]
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0]["reviewer_role"] == "architecture"
    assert reviewer_calls[0]["subject_digest"] == compute_subject_digest(
        reviewer_calls[0]["subject_input"]
    )


def test_reviewer_capability_prefers_explicit_rerun_over_broad_callable() -> None:
    request = _request(_grant("fix.py"), _policy())
    subject = _subject(head="new-head")
    subject_digest = compute_subject_digest(subject)
    received: list[dict[str, object]] = []

    class ReviewerCapability:
        async def rerun(self, *, reviewer_role: str, subject_digest: str) -> dict[str, str]:
            received.append(
                {
                    "reviewer_role": reviewer_role,
                    "subject_digest": subject_digest,
                }
            )
            return {
                "reviewer_role": reviewer_role,
                "subject_digest": subject_digest,
            }

        async def __call__(self, **kwargs: object) -> dict[str, str]:
            return await self.rerun(**kwargs)

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=Path("/tmp/seed"),
        validation_executor=lambda _workspace: None,
        subject_builder=lambda _patch_digest: subject,
        reviewer_rerunner=ReviewerCapability(),
    )

    result = asyncio.run(
        controller._run_reviewer(
            "architecture",
            subject,
            subject_digest,
            workspace=object(),
        )
    )

    assert result == {
        "reviewer_role": "architecture",
        "subject_digest": subject_digest,
    }
    assert received == [
        {
            "reviewer_role": "architecture",
            "subject_digest": subject_digest,
        }
    ]


def test_attempt_budget_is_fixed_and_does_not_prepare_transition(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy(max_attempts=2))
    executor = _FakeExecutor(
        [ValidationStatus.FAILED, ValidationStatus.FAILED, ValidationStatus.FAILED]
    )
    calls: list[int] = []

    async def agent(*, attempt: int, workspace: IsolatedWorkspace, **_: object) -> None:
        calls.append(attempt)
        workspace.write_text("fix.py", f"attempt-{attempt}")

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda _: _subject(),
        reviewer_rerunner=lambda *_: {"subject_digest": OLD_DIGEST},
    )
    result = _run(controller, agent)

    assert result.status is RemediationStatus.BUDGET_EXHAUSTED
    assert result.transition_state == "prepared"
    assert result.new_subject_digest is None
    assert calls == [1, 2]
    assert len(executor.calls) == 3


def test_success_builds_new_digest_and_reruns_selected_role(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy(max_attempts=2))
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])
    reruns: list[tuple[str, str]] = []

    async def agent(*, workspace: IsolatedWorkspace, **_: object) -> None:
        workspace.write_text("fix.py", "repaired")

    def build_subject(patch_digest: str) -> SubjectDigestInput:
        assert patch_digest.startswith("sha256:")
        return _subject(diff=patch_digest.removeprefix("sha256:"), head="new-head")

    async def rerun(role: str, subject_digest: str) -> dict[str, str]:
        reruns.append((role, subject_digest))
        return {
            "reviewer_role": role,
            "subject_digest": subject_digest,
            "accepted": True,
        }

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=build_subject,
        reviewer_rerunner=rerun,
    )
    result = _run(controller, agent)

    assert result.status is RemediationStatus.BLOCKED
    assert result.transition_state == "prepared"
    assert result.reason_code == "reviewer_subject_mismatch"
    assert result.new_subject_digest is None
    assert len(reruns) == 1
    assert reruns[0][0] == "architecture"
    assert reruns[0][1] != OLD_DIGEST
    assert result.rerun_roles == ()


def test_reviewer_subject_mismatch_fails_closed(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy())
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])

    async def agent(*, workspace: IsolatedWorkspace, **_: object) -> None:
        workspace.write_text("fix.py", "repaired")

    async def stale_rerun(role: str, subject_digest: str) -> dict[str, str]:
        return {"reviewer_role": role, "subject_digest": OLD_DIGEST}

    controller = RemediationController(
        request=request,
        selected_finding=_finding(),
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: _subject(
            diff=patch_digest.removeprefix("sha256:"), head="new-head"
        ),
        reviewer_rerunner=stale_rerun,
    )
    result = _run(controller, agent)

    assert result.status is RemediationStatus.BLOCKED
    assert result.reason_code == "reviewer_subject_mismatch"
    assert result.transition_state == "prepared"
    assert result.new_subject_digest is None


def test_validation_rejects_every_workspace_external_or_uri_argument(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    grant = _grant("check.py")
    with IsolatedWorkspace.prepare(seed, grant) as workspace:
        for argument in (
            str(tmp_path / "outside.py"),
            r"C:\\outside.py",
            "nested/../../outside.py",
            r"..\\outside.py",
            "../outside.py",
            "https://example.invalid/check.py",
        ):
            check = ValidationCheck(
                id="check",
                argv=(sys.executable, argument),
                visibility="controller",
            )
            executor = ValidationExecutor(workspace=workspace, checks=(check,))
            with pytest.raises(WorkspaceViolation):
                executor._validate_argv_paths(check)


def test_agent_receives_public_view_and_tools_hide_controller_workspace(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy())
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])
    seen: dict[str, bool] = {}
    received_findings: list[object] = []

    async def agent(
        *,
        workspace: object,
        tools: object,
        selected_finding: object | None = None,
        **_: object,
    ) -> None:
        received_findings.append(selected_finding)
        seen.update(
            {
                "resolve": hasattr(workspace, "resolve"),
                "root": hasattr(workspace, "root"),
                "controller_path": hasattr(workspace, "controller_path"),
                "tools_workspace": hasattr(tools, "workspace"),
            }
        )
        workspace.write_text("fix.py", "repaired")  # type: ignore[attr-defined]

    finding = _finding()
    controller = RemediationController(
        request=request,
        selected_finding=finding,
        seed_root=seed,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda patch_digest: _subject(
            diff=patch_digest.removeprefix("sha256:"), head="new-head"
        ),
        reviewer_rerunner=lambda role, subject_digest: {
            "reviewer_role": role,
            "subject_digest": subject_digest,
            "accepted": True,
        },
    )
    result = _run(controller, agent)

    assert result.status is RemediationStatus.BLOCKED
    assert seen == {
        "resolve": False,
        "root": False,
        "controller_path": False,
        "tools_workspace": False,
    }
    assert len(received_findings) == 1
    assert received_findings[0] is finding


def test_external_bound_executor_is_rejected(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    request = _request(_grant("fix.py"), _policy())
    with IsolatedWorkspace.prepare(seed, request.workspace_grant) as external_workspace:
        external_executor = _FakeExecutor([ValidationStatus.PASSED])
        external_executor.workspace = external_workspace  # type: ignore[attr-defined]
        controller = RemediationController(
            request=request,
            selected_finding=_finding(),
            seed_root=seed,
            validation_executor=external_executor,
            subject_builder=lambda _: _subject(),
            reviewer_rerunner=lambda *_: None,
        )
        with pytest.raises(ValueError, match="workspace"):
            _run(controller, lambda **_: None)


def test_reviewer_receipt_rejects_legacy_accepted_and_duck_values() -> None:
    digest = compute_subject_digest(_subject(head="new-head"))
    with pytest.raises(ValueError):
        ReviewerRerunReceipt(
            reviewer_role="architecture",
            subject_digest=digest,
            accepted=True,
        )
    assert RemediationController._reviewer_receipt(
        {
            "reviewer_role": "architecture",
            "subject_digest": digest,
            "accepted": True,
        },
        "architecture",
        digest,
    ) is None

    class Rejected:
        def __init__(self, value: str) -> None:
            self.reviewer_role = "architecture"
            self.subject_digest = value
            self.accepted = True

    assert RemediationController._reviewer_receipt(
        Rejected(digest), "architecture", digest
    ) is None


def test_contracts_fail_closed_for_forged_results() -> None:
    valid_patch = "sha256:" + "a" * 64
    with pytest.raises(ValueError):
        RemediationAttempt(attempt=1, changed=True, status="changed")
    with pytest.raises(ValueError):
        RemediationAttempt(
            attempt=1,
            changed=True,
            patch_digest="sha256:not-a-digest",
            status="changed",
        )
    with pytest.raises(ValueError):
        RemediationAttempt(
            attempt=1,
            changed=False,
            patch_digest=valid_patch,
            status="no_change",
        )

    request = _request(_grant("fix.py"), _policy())
    subject = _subject(head="new-head")
    digest = compute_subject_digest(subject)
    failed_validation = _validation("authoritative", ValidationStatus.FAILED)
    with pytest.raises(ValueError):
        RemediationResult(
            remediation_id=request.remediation_id,
            human_selected_finding_id=request.human_selected_finding_id,
            status=RemediationStatus.SUCCEEDED,
            reason_code="forged",
            old_case_id=request.old_case_id,
            old_subject_digest=request.old_subject_digest,
            attempts=0,
            validation_calls=0,
            last_validation=failed_validation,
        )
    with pytest.raises(ValueError):
        RemediationResult(
            remediation_id=request.remediation_id,
            human_selected_finding_id=request.human_selected_finding_id,
            status=RemediationStatus.FAILED,
            reason_code="forged",
            old_case_id=request.old_case_id,
            old_subject_digest=request.old_subject_digest,
            attempts=0,
            validation_calls=0,
            new_subject_input=subject,
            new_subject_digest=digest,
        )
    with pytest.raises(ValueError):
        RemediationResult(
            remediation_id=request.remediation_id,
            human_selected_finding_id=request.human_selected_finding_id,
            status=RemediationStatus.NOOP,
            reason_code="forged",
            old_case_id=request.old_case_id,
            old_subject_digest=request.old_subject_digest,
            attempts=1,
            validation_calls=0,
            attempt_receipts=(),
        )


def test_workspace_grant_rejects_private_and_quota_ignores_unallowed_seed(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        WorkspaceGrant(allowed_paths=(".codemesh_eval/secret",))

    seed = tmp_path / "large-seed"
    seed.mkdir()
    (seed / "fix.py").write_text("old", encoding="utf-8")
    for index in range(101):
        (seed / f"unallowed-{index}.txt").write_text("x", encoding="utf-8")
    grant = WorkspaceGrant(allowed_paths=("fix.py",), max_files=1, max_bytes=16)

    with IsolatedWorkspace.prepare(seed, grant) as workspace:
        workspace.write_text("fix.py", "repaired")
        assert workspace.read_text("fix.py") == "repaired"
