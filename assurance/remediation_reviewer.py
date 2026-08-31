"""Workspace-scoped reviewer reruns for bounded remediation.

The adapter is intentionally a thin composition seam.  It derives a fresh
``AssuranceRunIntent`` from the complete baseline run, delegates the complete
collector/reviewer pipeline to the public ``AssuranceRunService.prepare``
method, and returns the exact in-memory bundle only when every subject and
provenance binding still agrees with the controller's new subject.
"""

from __future__ import annotations

import inspect
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance.contracts import ExecutionReceipt, Finding
from assurance.digests import (
    AcceptanceScopeDigestInput,
    SubjectDigestInput,
    compute_acceptance_scope_digest,
    compute_subject_digest,
)
from assurance.remediation_workspace import IsolatedWorkspace
from assurance.run_service import (
    AssuranceRunBundle,
    AssuranceRunIntent,
    AssuranceRunService,
    FreshnessSourceBinding,
    ReviewerRunRecord,
)
from assurance.snapshot import GitSnapshotCollector

if TYPE_CHECKING:
    from assurance.remediation import RemediationRequest


ReviewerRole = Literal["intent", "architecture", "operability"]


def _valid_prepared_bundle(
    bundle: AssuranceRunBundle, reviewer_role: ReviewerRole
) -> bool:
    """Check the exact successful facts needed by a remediation receipt."""

    if type(bundle) is not AssuranceRunBundle:
        return False
    reviewer = bundle.reviewer
    receipt = bundle.execution_receipt
    if reviewer.status != "success" or receipt.overall_result != "success":
        return False
    if (
        receipt.run_id != bundle.run_id
        or receipt.subject_digest != bundle.subject.subject_digest
    ):
        return False
    if len(receipt.steps) != 3 or tuple(
        step.planned_role for step in receipt.steps
    ) != ("intent", "architecture", "operability"):
        return False
    if any(
        value is None
        for value in (
            reviewer.prompt_id,
            reviewer.prompt_digest,
            reviewer.raw_response_artifact_digest,
            reviewer.canonical_response_digest,
            reviewer.result_id,
            reviewer.result_digest,
            reviewer.actual_provider,
            reviewer.actual_model_ref,
        )
    ):
        return False
    selected = tuple(
        step for step in receipt.steps if step.planned_role == reviewer_role
    )
    if len(selected) != 1:
        return False
    selected_step = selected[0]
    if (
        selected_step.result != "success"
        or selected_step.actual_role != reviewer_role
        or selected_step.provider != reviewer.actual_provider
        or selected_step.model_ref != reviewer.actual_model_ref
        or selected_step.schema_status != reviewer.schema_status
    ):
        return False
    route = reviewer.planned_route
    if (
        reviewer.actual_provider != route.provider
        or reviewer.actual_model_ref != route.model_ref
    ):
        return False
    for step in receipt.steps:
        if (
            step.result != "success"
            or step.actual_role != step.planned_role
            or step.provider != reviewer.actual_provider
            or step.model_ref != reviewer.actual_model_ref
            or step.schema_status != reviewer.schema_status
            or step.routing_rule != route.routing_rule
            or step.timeout_seconds != route.timeout_seconds
            or step.token_budget != route.token_budget
            or step.tool_grants != route.tool_grants
        ):
            return False
    if reviewer.usage_status == "measured":
        return (
            receipt.input_tokens == reviewer.input_tokens
            and receipt.output_tokens == reviewer.output_tokens
            and receipt.cost_usd == reviewer.cost_usd
        )
    return (
        reviewer.input_tokens is None
        and reviewer.output_tokens is None
        and reviewer.cost_usd is None
        and receipt.input_tokens == 0
        and receipt.output_tokens == 0
        and receipt.cost_usd == 0.0
    )


class PreparedReviewerRerun(BaseModel):
    """Exact, successful rerun bundle held only in memory during remediation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: ReviewerRole
    bundle: AssuranceRunBundle

    @model_validator(mode="before")
    @classmethod
    def _require_exact_bundle(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("prepared reviewer rerun must validate from a mapping")
        if type(data.get("bundle")) is not AssuranceRunBundle:
            raise ValueError("bundle must be an exact AssuranceRunBundle")
        return data

    @model_validator(mode="after")
    def _require_successful_rerun(self) -> "PreparedReviewerRerun":
        if not _valid_prepared_bundle(self.bundle, self.reviewer_role):
            raise ValueError("prepared reviewer rerun facts are incomplete")
        return self


class AssuranceRemediationReviewer:
    """Delegate one selected-role rerun to a workspace-scoped run service."""

    def __init__(
        self,
        *,
        baseline_bundle: AssuranceRunBundle | None = None,
        service_factory: Callable[[Path], Any],
        baseline: AssuranceRunBundle | None = None,
    ) -> None:
        candidate = baseline_bundle if baseline_bundle is not None else baseline
        if type(candidate) is not AssuranceRunBundle:
            raise TypeError("baseline_bundle must be an exact AssuranceRunBundle")
        if not callable(service_factory):
            raise TypeError("service_factory must be callable")
        self._baseline = candidate
        self._service_factory = service_factory

    async def rerun(
        self,
        *,
        reviewer_role: ReviewerRole,
        subject_input: SubjectDigestInput,
        subject_digest: str,
        workspace: IsolatedWorkspace,
        request: "RemediationRequest",
        selected_finding: Finding,
    ) -> PreparedReviewerRerun | None:
        """Prepare one exact rerun, or return ``None`` to fail closed."""

        from assurance.remediation import RemediationRequest

        if type(workspace) is not IsolatedWorkspace:
            return None
        if type(subject_input) is not SubjectDigestInput:
            return None
        if type(subject_digest) is not str:
            return None
        if type(request) is not RemediationRequest:
            return None
        if type(selected_finding) is not Finding:
            return None
        try:
            baseline = self._revalidated_bundle(self._baseline)
            if baseline is None or not self._request_matches_baseline(
                request, selected_finding, reviewer_role
            ):
                return None
            if baseline.freshness_source_binding.subject_identity_version != "v2":
                # Legacy v1 runs remain readable, but cannot safely derive a
                # scope-bound remediation subject.
                return None
            if not self._scope_matches_bundle(
                baseline.freshness_source_binding, baseline
            ):
                return None
            if compute_subject_digest(subject_input) != subject_digest:
                return None
            intent = self._intent_for_workspace(workspace.root)
            if intent is None or not self._subject_matches_baseline(subject_input):
                return None

            service = self._service_factory(workspace.root)
            if inspect.isawaitable(service):
                service = await service
            prepare = getattr(service, "prepare", None)
            if not callable(prepare):
                return None
            if type(service) is AssuranceRunService and not self._service_is_scoped(
                service, workspace.root
            ):
                return None
            idempotency_key = self._idempotency_key(reviewer_role, subject_digest)
            bundle = prepare(intent, idempotency_key=idempotency_key)
            if inspect.isawaitable(bundle):
                bundle = await bundle
            rebound = self._bundle_matches(
                bundle,
                subject_input,
                subject_digest,
                reviewer_role,
                workspace.root,
                intent,
                service,
            )
            if rebound is None:
                return None
            return PreparedReviewerRerun(
                reviewer_role=reviewer_role,
                bundle=rebound,
            )
        except Exception:
            return None

    async def __call__(self, **kwargs: Any) -> PreparedReviewerRerun | None:
        return await self.rerun(**kwargs)

    @staticmethod
    def _revalidated_bundle(bundle: Any) -> AssuranceRunBundle | None:
        """Re-run the complete bundle contract after an injected seam returns it."""

        if type(bundle) is not AssuranceRunBundle:
            return None
        if type(bundle.reviewer) is not ReviewerRunRecord:
            return None
        if type(bundle.execution_receipt) is not ExecutionReceipt:
            return None
        try:
            reviewer = ReviewerRunRecord.model_validate(
                bundle.reviewer.model_dump(mode="python")
            )
            receipt = ExecutionReceipt.model_validate(
                bundle.execution_receipt.model_dump(mode="python")
            )
            data = {
                name: getattr(bundle, name)
                for name in AssuranceRunBundle.model_fields
            }
            data["reviewer"] = reviewer
            data["execution_receipt"] = receipt
            rebound = AssuranceRunBundle.model_validate(data)
        except Exception:
            return None
        return rebound if type(rebound) is AssuranceRunBundle else None

    def _request_matches_baseline(
        self,
        request: "RemediationRequest",
        selected_finding: Finding,
        reviewer_role: ReviewerRole,
    ) -> bool:
        baseline = self._baseline
        if (
            baseline.case.case_id != request.old_case_id
            or baseline.draft_case.case_id != request.old_case_id
            or baseline.subject.subject_digest != request.old_subject_digest
            or baseline.case.subject_digest != request.old_subject_digest
            or baseline.draft_case.subject_digest != request.old_subject_digest
        ):
            return False
        if (
            selected_finding.finding_id != request.human_selected_finding_id
            or selected_finding.subject_digest != request.old_subject_digest
            or selected_finding.reviewer_role != reviewer_role
        ):
            return False
        matching = tuple(
            finding
            for finding in baseline.findings
            if finding.finding_id == selected_finding.finding_id
        )
        return len(matching) == 1 and matching[0] == selected_finding

    def _intent_for_workspace(self, workspace_root: Path) -> AssuranceRunIntent | None:
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            return None
        source = self._baseline.freshness_source_binding
        if type(source) is not FreshnessSourceBinding:
            return None
        if source.attachment_digests:
            # AssuranceRunIntent v1 has no attachment field; do not silently
            # drop source facts when deriving a rerun request.
            return None
        observations = self._baseline.commands.snapshot.commands
        declarations = self._baseline.risk.input.declarations
        try:
            return AssuranceRunIntent(
                repository_path=workspace_root,
                repository_identity=source.repository_identity,
                author=source.author,
                author_provenance=source.author_provenance,
                base_ref=source.requested_base_ref,
                task_path=source.task_path,
                policy_paths=source.policy_paths,
                adr_paths=source.adr_paths,
                runbook_paths=source.runbook_paths,
                command_ids=tuple(item.command_id for item in observations),
                changed_lines_total=declarations.changed_lines_total,
                external_side_effects=declarations.external_side_effects,
                provider_boundary=declarations.provider_boundary,
            )
        except (TypeError, ValueError):
            return None

    def _subject_matches_baseline(self, subject_input: SubjectDigestInput) -> bool:
        source = self._baseline.freshness_source_binding
        subject = self._baseline.subject
        if subject_input.schema_version != source.subject_identity_version:
            return False
        if source.subject_identity_version == "v2":
            try:
                scope_digest = compute_acceptance_scope_digest(
                    AcceptanceScopeDigestInput(
                        task_path=source.task_path,
                        policy_paths=source.policy_paths,
                        adr_paths=source.adr_paths,
                        runbook_paths=source.runbook_paths,
                    )
                )
            except (TypeError, ValueError):
                return False
            if subject_input.acceptance_scope_digest != scope_digest:
                return False
        elif subject_input.acceptance_scope_digest is not None:
            return False
        return (
            not subject_input.attachment_digests
            and subject_input.repository == source.repository_identity == subject.repository
            and subject_input.base_revision
            == source.resolved_base_revision
            == subject.base_revision
            and subject_input.task_digest == subject.task_digest
            and subject_input.policy_version
            == source.policy_version
            == subject.policy_version
            and subject_input.rubric_version
            == source.rubric_version
            == self._baseline.binding.rubric_version
            == self._baseline.reviewer.rubric_version
        )

    @staticmethod
    def _scope_matches_bundle(
        source: FreshnessSourceBinding, bundle: AssuranceRunBundle
    ) -> bool:
        if source.subject_identity_version != "v2":
            return source.subject_identity_version == "v1"
        try:
            task_digest = bundle.intake.snapshot.task_digest
            if task_digest is None:
                return False
            collector = GitSnapshotCollector(
                **source.git_collector_profile.model_dump(exclude={"schema_version"})
            )
            scope_digest = compute_acceptance_scope_digest(
                AcceptanceScopeDigestInput(
                    task_path=source.task_path,
                    policy_paths=source.policy_paths,
                    adr_paths=source.adr_paths,
                    runbook_paths=source.runbook_paths,
                )
            )
            subject_input = collector.build_subject_input(
                bundle.git.snapshot,
                task_digest=task_digest,
                policy_version=source.policy_version,
                rubric_version=source.rubric_version,
                attachment_digests=source.attachment_digests,
                acceptance_scope_digest=scope_digest,
            )
            return compute_subject_digest(subject_input) == bundle.subject.subject_digest
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _service_is_scoped(service: AssuranceRunService, root: Path) -> bool:
        config = getattr(service, "_config", None)
        return (
            config is not None
            and config.workspace_root == root
        )

    def _bundle_matches(
        self,
        bundle: Any,
        subject_input: SubjectDigestInput,
        subject_digest: str,
        reviewer_role: ReviewerRole,
        workspace_root: Path,
        intent: AssuranceRunIntent,
        service: Any,
    ) -> AssuranceRunBundle | None:
        rebound = self._revalidated_bundle(bundle)
        if rebound is None:
            return None
        bundle = rebound
        if not _valid_prepared_bundle(bundle, reviewer_role):
            return None
        expected_key = self._idempotency_key(reviewer_role, subject_digest)
        if bundle.idempotency_key != expected_key:
            return None
        if bundle.run_id != self._run_id(bundle.request_digest, expected_key):
            return None
        if type(service) is AssuranceRunService:
            request_digest = getattr(service, "_request_digest", None)
            if not callable(request_digest):
                return None
            try:
                if bundle.request_digest != request_digest(intent):
                    return None
            except Exception:
                return None
        subject = bundle.subject
        source = bundle.freshness_source_binding
        if type(source) is not FreshnessSourceBinding:
            return False
        baseline_source = self._baseline.freshness_source_binding
        baseline_subject = self._baseline.subject
        baseline_declarations = self._baseline.risk.input.declarations
        if source.subject_identity_version != baseline_source.subject_identity_version:
            return None
        if subject_input.schema_version != source.subject_identity_version:
            return None
        if source.subject_identity_version == "v2":
            try:
                expected_scope_digest = compute_acceptance_scope_digest(
                    AcceptanceScopeDigestInput(
                        task_path=source.task_path,
                        policy_paths=source.policy_paths,
                        adr_paths=source.adr_paths,
                        runbook_paths=source.runbook_paths,
                    )
                )
            except (TypeError, ValueError):
                return None
            if subject_input.acceptance_scope_digest != expected_scope_digest:
                return None
        elif subject_input.acceptance_scope_digest is not None:
            return None
        if not self._scope_matches_bundle(source, bundle):
            return None
        if (
            subject.subject_digest != subject_digest
            or subject.repository != subject_input.repository
            or subject.base_revision != subject_input.base_revision
            or subject.head_revision != subject_input.head_revision
            or subject.task_digest != subject_input.task_digest
            or subject.policy_version != subject_input.policy_version
            or bundle.binding.policy_version != subject_input.policy_version
            or bundle.binding.rubric_version != subject_input.rubric_version
            or bundle.reviewer.rubric_version != subject_input.rubric_version
            or bundle.reviewer.planned_route != self._baseline.reviewer.planned_route
        ):
            return None
        if (
            source.repository_path != workspace_root
            or source.repository_identity != subject_input.repository
            or source.author != baseline_source.author
            or source.author_provenance != baseline_source.author_provenance
            or source.resolved_base_revision != baseline_source.resolved_base_revision
            or source.git_collector_profile != baseline_source.git_collector_profile
            or source.requested_base_ref != intent.base_ref
            or source.requested_base_ref != baseline_source.requested_base_ref
            or source.task_path != intent.task_path
            or source.policy_paths != intent.policy_paths
            or source.adr_paths != intent.adr_paths
            or source.runbook_paths != intent.runbook_paths
            or source.policy_version != subject_input.policy_version
            or source.rubric_version != subject_input.rubric_version
            or source.subject != subject
            or source.attachment_digests != subject_input.attachment_digests
        ):
            return None
        if (
            intent.repository_identity != baseline_source.repository_identity
            or intent.author != baseline_source.author
            or intent.author_provenance != baseline_source.author_provenance
            or intent.base_ref != baseline_source.requested_base_ref
            or intent.task_path != baseline_source.task_path
            or intent.policy_paths != baseline_source.policy_paths
            or intent.adr_paths != baseline_source.adr_paths
            or intent.runbook_paths != baseline_source.runbook_paths
            or intent.changed_lines_total != baseline_declarations.changed_lines_total
            or intent.external_side_effects != baseline_declarations.external_side_effects
            or intent.provider_boundary != baseline_declarations.provider_boundary
        ):
            return None
        command_ids = tuple(
            item.command_id for item in bundle.commands.snapshot.commands
        )
        if command_ids != intent.command_ids:
            return None
        baseline_commands = tuple(
            (item.command_id, item.kind, item.argv, item.cwd)
            for item in self._baseline.commands.snapshot.commands
        )
        output_commands = tuple(
            (item.command_id, item.kind, item.argv, item.cwd)
            for item in bundle.commands.snapshot.commands
        )
        if output_commands != baseline_commands:
            return None
        output_declarations = bundle.risk.input.declarations
        if output_declarations != baseline_declarations:
            return None
        return bundle

    @staticmethod
    def _run_id(request_digest: str, idempotency_key: str) -> str:
        return "run_" + hashlib.sha256(
            (request_digest + "|" + idempotency_key).encode("utf-8")
        ).hexdigest()[:32]

    def _idempotency_key(self, reviewer_role: ReviewerRole, subject_digest: str) -> str:
        return (
            "remediation-review:"
            + self._baseline.run_id
            + ":"
            + reviewer_role
            + ":"
            + subject_digest
        )


__all__ = ("AssuranceRemediationReviewer", "PreparedReviewerRerun")
