"""V2-P7-01A bounded remediation controller.

This atomic deliberately stops at ``transition_state='prepared'``.  It does
not write SQLite, invalidate the old Case, or create the new Case.  A later
atomic may persist the prepared result after its own transaction checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from assurance.contracts import ExecutionReceipt, Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.remediation_reviewer import PreparedReviewerRerun
from assurance.remediation_validation import (
    BudgetedValidationExecutor,
    ValidationResult,
    ValidationStatus,
    make_validation_tool_registry,
)
from assurance.remediation_workspace import (
    IsolatedWorkspace,
    WorkspaceGrant,
    WorkspaceViolation,
)
from assurance.run_service import AssuranceRunBundle, ReviewerRunRecord


_SHA256_PREFIX = "sha256:"
_SHA256_HEX_LENGTH = 64


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_SHA256_PREFIX)
        and len(value) == len(_SHA256_PREFIX) + _SHA256_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in value[len(_SHA256_PREFIX) :])
    )


class RemediationStatus(str, Enum):
    NOOP = "NOOP"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class RemediationPolicy(BaseModel):
    """Frozen upper-bounded policy; the agent cannot change these values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=3, strict=True, ge=1, le=3)
    max_agent_iterations: int = Field(default=15, strict=True, ge=1, le=15)
    max_validation_calls_per_attempt: int = Field(
        default=2,
        strict=True,
        ge=1,
        le=2,
    )
    total_wall_time_s: float = Field(default=60.0, strict=True, gt=0, le=60.0)
    authoritative_check_id: str = Field(default="authoritative", min_length=1)

    @field_validator("authoritative_check_id")
    @classmethod
    def _non_blank_check_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("authoritative_check_id must not be blank")
        return value


class RemediationRequest(BaseModel):
    """Human-selected, subject-bound remediation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    remediation_id: str = Field(min_length=1)
    old_case_id: str = Field(min_length=1)
    old_subject_digest: str
    human_selected_finding_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    requested_at: AwareDatetime
    workspace_grant: WorkspaceGrant
    policy: RemediationPolicy

    @field_validator(
        "remediation_id",
        "old_case_id",
        "human_selected_finding_id",
        "requested_by",
    )
    @classmethod
    def _non_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity fields must not be blank")
        return value

    @field_validator("old_subject_digest")
    @classmethod
    def _valid_old_digest(cls, value: str) -> str:
        if not _is_sha256_digest(value):
            raise ValueError("old_subject_digest must be sha256:<64 lowercase hex>")
        return value


class ReviewerRerunReceipt(BaseModel):
    """Auditable subject/role binding for one prepared reviewer rerun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    subject_digest: str
    reviewer: ReviewerRunRecord
    execution_receipt: ExecutionReceipt

    @field_validator("subject_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _is_sha256_digest(value):
            raise ValueError("subject_digest must be sha256:<64 lowercase hex>")
        return value

    @model_validator(mode="after")
    def _bind_successful_rerun(self) -> "ReviewerRerunReceipt":
        if type(self.reviewer) is not ReviewerRunRecord:
            raise ValueError("reviewer must be an exact ReviewerRunRecord")
        if type(self.execution_receipt) is not ExecutionReceipt:
            raise ValueError("execution_receipt must be an exact ExecutionReceipt")
        if self.reviewer.status != "success":
            raise ValueError("reviewer rerun must bind a successful reviewer")
        if self.execution_receipt.overall_result != "success":
            raise ValueError("reviewer rerun must bind a successful receipt")
        if self.execution_receipt.subject_digest != self.subject_digest:
            raise ValueError("reviewer rerun receipt subject does not match")
        steps = self.execution_receipt.steps
        selected = tuple(
            step for step in steps if step.planned_role == self.reviewer_role
        )
        if len(selected) != 1:
            raise ValueError("reviewer rerun role must have one receipt step")
        step = selected[0]
        if step.result != "success" or step.actual_role != self.reviewer_role:
            raise ValueError("selected reviewer role was not executed")
        if step.model_ref != self.reviewer.actual_model_ref:
            raise ValueError("reviewer model provenance does not match receipt")
        if step.provider != self.reviewer.actual_provider:
            raise ValueError("reviewer provider provenance does not match receipt")
        if step.schema_status != self.reviewer.schema_status:
            raise ValueError("reviewer schema provenance does not match receipt")
        if self.reviewer.usage_status == "measured":
            if (
                self.execution_receipt.input_tokens != self.reviewer.input_tokens
                or self.execution_receipt.output_tokens != self.reviewer.output_tokens
                or self.execution_receipt.cost_usd != self.reviewer.cost_usd
            ):
                raise ValueError("reviewer usage provenance does not match receipt")
        elif (
            self.execution_receipt.input_tokens != 0
            or self.execution_receipt.output_tokens != 0
            or self.execution_receipt.cost_usd != 0.0
        ):
            raise ValueError("unavailable reviewer usage must remain zero")
        return self

    @property
    def reviewer_record(self) -> ReviewerRunRecord:
        """Compatibility-free descriptive alias for the nested reviewer record."""

        return self.reviewer


class RemediationAttempt(BaseModel):
    """Immutable per-attempt evidence returned before persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    attempt: int = Field(strict=True, ge=1)
    changed: bool
    patch_digest: str | None = None
    validation_receipts: tuple[ValidationResult, ...] = ()
    status: Literal["changed", "no_change", "failed", "blocked"]

    @field_validator("patch_digest")
    @classmethod
    def _valid_patch_digest(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256_digest(value):
            raise ValueError("patch_digest must be sha256:<64 lowercase hex>")
        return value

    @model_validator(mode="after")
    def _validate_attempt_consistency(self) -> "RemediationAttempt":
        if self.changed != (self.patch_digest is not None):
            raise ValueError("changed and patch_digest must agree")
        if self.status == "no_change" and self.changed:
            raise ValueError("no_change attempt must not carry a patch digest")
        if self.status == "changed" and not self.changed:
            raise ValueError("changed attempt must carry a patch digest")
        return self


class RemediationResult(BaseModel):
    """Prepared result; no persistence transition is implied by this object."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    remediation_id: str
    human_selected_finding_id: str
    status: RemediationStatus
    reason_code: str
    transition_state: Literal["prepared"] = "prepared"
    old_case_id: str
    old_subject_digest: str
    attempts: int = Field(strict=True, ge=0)
    validation_calls: int = Field(strict=True, ge=0)
    attempt_receipts: tuple[RemediationAttempt, ...] = ()
    patch_digests: tuple[str, ...] = ()
    last_validation: ValidationResult | None = None
    new_subject_input: SubjectDigestInput | None = None
    new_subject_digest: str | None = None
    rerun_roles: tuple[Literal["intent", "architecture", "operability"], ...] = ()
    reviewer_receipts: tuple[ReviewerRerunReceipt, ...] = ()

    @field_validator("remediation_id", "human_selected_finding_id", "old_case_id")
    @classmethod
    def _non_blank_result_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result identity fields must not be blank")
        return value

    @field_validator("old_subject_digest", "new_subject_digest")
    @classmethod
    def _valid_result_digest(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256_digest(value):
            raise ValueError("result digest must be sha256:<64 lowercase hex>")
        return value

    @field_validator("patch_digests")
    @classmethod
    def _valid_patch_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            if not _is_sha256_digest(digest):
                raise ValueError("patch_digests must contain sha256 digests")
        return value

    @model_validator(mode="after")
    def _validate_result_consistency(self) -> "RemediationResult":
        if self.attempts != len(self.attempt_receipts):
            raise ValueError("attempts must equal attempt_receipts length")
        changed_digests = tuple(
            receipt.patch_digest
            for receipt in self.attempt_receipts
            if receipt.changed
        )
        if self.patch_digests != changed_digests:
            raise ValueError("patch_digests must match changed attempt receipts")

        if self.status is RemediationStatus.SUCCEEDED:
            if self.last_validation is None or _status(self.last_validation) is not ValidationStatus.PASSED:
                raise ValueError("successful remediation requires passed final validation")
            if self.new_subject_input is None or self.new_subject_digest is None:
                raise ValueError("successful remediation requires a new subject")
            if compute_subject_digest(self.new_subject_input) != self.new_subject_digest:
                raise ValueError("new subject digest does not match its input")
            if self.new_subject_digest == self.old_subject_digest:
                raise ValueError("new subject digest must differ from old subject")
            if len(self.rerun_roles) != 1 or len(self.reviewer_receipts) != 1:
                raise ValueError("successful remediation requires exactly one reviewer rerun")
            receipt = self.reviewer_receipts[0]
            if (
                receipt.reviewer_role != self.rerun_roles[0]
                or receipt.subject_digest != self.new_subject_digest
            ):
                raise ValueError("reviewer receipt is not bound to the new subject")
        else:
            if (
                self.new_subject_input is not None
                or self.new_subject_digest is not None
                or self.rerun_roles
                or self.reviewer_receipts
            ):
                raise ValueError("non-success remediation cannot carry a transition")
        return self


def _revalidate_bundle_receipts(bundle: AssuranceRunBundle) -> bool:
    """Re-validate receipt submodels before retaining a bundle in a handoff."""

    if type(bundle) is not AssuranceRunBundle:
        return False
    if type(bundle.reviewer) is not ReviewerRunRecord:
        return False
    if type(bundle.execution_receipt) is not ExecutionReceipt:
        return False
    try:
        reviewer = ReviewerRunRecord.model_validate(
            bundle.reviewer.model_dump(mode="python")
        )
        execution_receipt = ExecutionReceipt.model_validate(
            bundle.execution_receipt.model_dump(mode="python")
        )
        payload = {
            name: getattr(bundle, name) for name in AssuranceRunBundle.model_fields
        }
        payload["reviewer"] = reviewer
        payload["execution_receipt"] = execution_receipt
        rebound = AssuranceRunBundle.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return False
    return type(rebound) is AssuranceRunBundle and rebound == bundle


class PreparedRemediationHandoff(BaseModel):
    """Non-persistable handoff between remediation preparation and its UOW."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: RemediationResult
    bundle: AssuranceRunBundle | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _require_exact_models(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("remediation handoff must validate from a mapping")
        if type(data.get("result")) is not RemediationResult:
            raise ValueError("result must be an exact RemediationResult")
        bundle = data.get("bundle")
        if bundle is not None and type(bundle) is not AssuranceRunBundle:
            raise ValueError("bundle must be an exact AssuranceRunBundle")
        return data

    @model_validator(mode="after")
    def _bind_result_to_bundle(self) -> "PreparedRemediationHandoff":
        try:
            rebound_result = RemediationResult.model_validate(
                self.result.model_dump(mode="python", round_trip=True)
            )
        except (ValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
            raise ValueError("remediation result failed revalidation") from exc
        if type(rebound_result) is not RemediationResult or rebound_result != self.result:
            raise ValueError("remediation result is not an exact validated result")
        if rebound_result.transition_state != "prepared":
            raise ValueError("remediation result must remain prepared")
        if rebound_result.status is not RemediationStatus.SUCCEEDED:
            if self.bundle is not None:
                raise ValueError("non-success remediation must not carry a bundle")
            return self
        bundle = self.bundle
        if bundle is None:
            raise ValueError("successful remediation requires an exact bundle")
        if not _revalidate_bundle_receipts(bundle):
            raise ValueError("remediation bundle receipts are invalid")
        subject_input = self.result.new_subject_input
        if (
            type(subject_input) is not SubjectDigestInput
            or self.result.new_subject_digest is None
        ):
            raise ValueError("successful remediation must carry an exact new subject")
        if compute_subject_digest(subject_input) != bundle.subject.subject_digest:
            raise ValueError("remediation subject is not bound to bundle")
        if self.result.new_subject_digest != bundle.subject.subject_digest:
            raise ValueError("remediation digest is not bound to bundle")
        if bundle.binding.subject_digest != bundle.subject.subject_digest:
            raise ValueError("bundle binding is not bound to bundle subject")
        if bundle.binding.rubric_version != bundle.reviewer.rubric_version:
            raise ValueError("bundle binding rubric is not bound to reviewer")
        if len(self.result.reviewer_receipts) != 1:
            raise ValueError("successful remediation requires one reviewer receipt")
        reviewer_receipt = self.result.reviewer_receipts[0]
        if type(reviewer_receipt) is not ReviewerRerunReceipt:
            raise ValueError("successful remediation must carry an exact reviewer receipt")
        if (
            type(reviewer_receipt.reviewer) is not ReviewerRunRecord
            or type(reviewer_receipt.execution_receipt) is not ExecutionReceipt
        ):
            raise ValueError("reviewer receipt contains invalid nested receipts")
        if (
            self.result.rerun_roles != (reviewer_receipt.reviewer_role,)
            or reviewer_receipt.subject_digest != bundle.subject.subject_digest
            or reviewer_receipt.reviewer != bundle.reviewer
            or reviewer_receipt.execution_receipt != bundle.execution_receipt
            or reviewer_receipt.execution_receipt.run_id != bundle.run_id
            or reviewer_receipt.execution_receipt.subject_digest
            != bundle.subject.subject_digest
        ):
            raise ValueError("reviewer receipt is not bound to exact bundle facts")
        return self


@dataclass(frozen=True)
class AgentAttemptResult:
    """Optional structured return value for an injected agent."""

    summary: str = ""
    iterations: int | None = None


class RemediationAgent(Protocol):
    async def repair(self, **kwargs: Any) -> AgentAttemptResult | None: ...


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return value
    return value


async def _await_if_needed(value: Any) -> Any:
    awaited = _maybe_await(value)
    if inspect.isawaitable(awaited):
        return await awaited
    return awaited


def _status(result: Any) -> ValidationStatus | None:
    raw = getattr(result, "status", None)
    if isinstance(raw, ValidationStatus):
        return raw
    if isinstance(raw, str):
        try:
            return ValidationStatus(raw.lower())
        except ValueError:
            return None
    return None


def _blocked_status(status: ValidationStatus | None) -> bool:
    return status in {
        ValidationStatus.BLOCKED,
        ValidationStatus.INFRA_ERROR,
        ValidationStatus.TIMEOUT,
        ValidationStatus.UNSUPPORTED,
    }


def _snapshot_digest(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(before) | set(after)):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(before.get(path, b""))
        digest.update(b"\0")
        digest.update(after.get(path, b""))
        digest.update(b"\0")
    return _SHA256_PREFIX + digest.hexdigest()


def _aggregate_patch_digest(patches: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for patch in patches:
        digest.update(patch.encode("ascii"))
        digest.update(b"\0")
    return _SHA256_PREFIX + digest.hexdigest()


def _filtered_kwargs(function: Callable[..., Any], values: dict[str, Any]) -> dict[str, Any]:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return values
    if any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()):
        return values
    return {name: value for name, value in values.items() if name in parameters}


class RemediationController:
    """Run bounded repair in a temporary workspace and prepare a new subject."""

    def __init__(
        self,
        *,
        request: RemediationRequest,
        selected_finding: Finding,
        seed_root: Any,
        validation_executor: object,
        subject_builder: Callable[[str], SubjectDigestInput | tuple[SubjectDigestInput, str]],
        reviewer_rerunner: Callable[..., Any],
        workspace_parent: Any | None = None,
    ) -> None:
        if selected_finding.finding_id != request.human_selected_finding_id:
            raise ValueError("selected finding does not match human selection")
        if selected_finding.subject_digest != request.old_subject_digest:
            raise ValueError("selected finding is bound to another subject")
        if selected_finding.status != "open":
            raise ValueError("only an open Finding may be remediated")
        self.request = request
        self.selected_finding = selected_finding
        self.seed_root = seed_root
        self.validation_executor = validation_executor
        self.subject_builder = subject_builder
        self.reviewer_rerunner = reviewer_rerunner
        self.workspace_parent = workspace_parent

    def _executor_for_workspace(self, workspace: IsolatedWorkspace) -> object:
        candidate = self.validation_executor
        if hasattr(candidate, "validate"):
            bound_workspace = getattr(candidate, "workspace", None)
            if bound_workspace is not workspace:
                raise ValueError(
                    "validation_executor must be a factory or bound to the current workspace"
                )
            return candidate
        if callable(candidate):
            executor = candidate(workspace)
            if not hasattr(executor, "validate"):
                raise ValueError("validation executor factory returned an invalid executor")
            bound_workspace = getattr(executor, "workspace", None)
            if bound_workspace is not None and bound_workspace is not workspace:
                raise ValueError(
                    "validation executor factory returned an executor bound to another workspace"
                )
            return executor
        raise TypeError("validation_executor must expose validate or be a factory")

    @staticmethod
    def _agent_callable(agent: object) -> Callable[..., Any]:
        function = getattr(agent, "repair", None)
        if function is None:
            if not callable(agent):
                raise TypeError("agent must be callable or expose repair")
            function = agent
        return function

    async def _run_agent(
        self,
        agent: object,
        *,
        workspace: IsolatedWorkspace,
        tools: object,
        attempt: int,
        feedback: ValidationResult,
    ) -> Any:
        function = self._agent_callable(agent)
        values = {
            "request": self.request,
            "case_id": self.request.old_case_id,
            "finding_id": self.request.human_selected_finding_id,
            "selected_finding": self.selected_finding,
            "attempt": attempt,
            "workspace": workspace.public_view(),
            "tools": tools,
            "validation_feedback": feedback,
            "max_iterations": self.request.policy.max_agent_iterations,
        }
        return await _await_if_needed(function(**_filtered_kwargs(function, values)))

    async def _run_reviewer(
        self,
        role: str,
        subject: SubjectDigestInput,
        digest: str,
        *,
        workspace: IsolatedWorkspace,
    ) -> Any:
        function = getattr(self.reviewer_rerunner, "rerun", None)
        if not callable(function):
            function = self.reviewer_rerunner
        if not callable(function):
            raise TypeError("reviewer_rerunner must be callable or expose rerun")
        values = {
            "reviewer_role": role,
            "role": role,
            "subject_input": subject,
            "subject": subject,
            "subject_digest": digest,
            "request": self.request,
            "finding_id": self.request.human_selected_finding_id,
            "selected_finding": self.selected_finding,
            "workspace": workspace,
        }
        return await _await_if_needed(function(**_filtered_kwargs(function, values)))

    @staticmethod
    def _reviewer_receipt(result: Any, role: str, digest: str) -> ReviewerRerunReceipt | None:
        if type(result) is not PreparedReviewerRerun:
            return None
        if not _revalidate_bundle_receipts(result.bundle):
            return None
        if result.reviewer_role != role or result.bundle.subject.subject_digest != digest:
            return None
        try:
            return ReviewerRerunReceipt(
                reviewer_role=role,
                subject_digest=digest,
                reviewer=result.bundle.reviewer,
                execution_receipt=result.bundle.execution_receipt,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _new_subject(
        built: SubjectDigestInput | tuple[SubjectDigestInput, str],
    ) -> tuple[SubjectDigestInput, str] | None:
        if isinstance(built, SubjectDigestInput):
            return built, compute_subject_digest(built)
        if (
            isinstance(built, tuple)
            and len(built) == 2
            and isinstance(built[0], SubjectDigestInput)
            and isinstance(built[1], str)
        ):
            computed = compute_subject_digest(built[0])
            if built[1] != computed:
                return None
            return built[0], built[1]
        return None

    @staticmethod
    def _result(
        *,
        request: RemediationRequest,
        status: RemediationStatus,
        reason_code: str,
        attempts: int,
        validation_calls: int,
        attempt_receipts: list[RemediationAttempt],
        patch_digests: list[str],
        last_validation: ValidationResult | None,
        new_subject_input: SubjectDigestInput | None = None,
        new_subject_digest: str | None = None,
        rerun_roles: tuple[Literal["intent", "architecture", "operability"], ...] = (),
        reviewer_receipts: list[ReviewerRerunReceipt] | None = None,
    ) -> RemediationResult:
        return RemediationResult(
            remediation_id=request.remediation_id,
            human_selected_finding_id=request.human_selected_finding_id,
            status=status,
            reason_code=reason_code,
            old_case_id=request.old_case_id,
            old_subject_digest=request.old_subject_digest,
            attempts=attempts,
            validation_calls=validation_calls,
            attempt_receipts=tuple(attempt_receipts),
            patch_digests=tuple(patch_digests),
            last_validation=last_validation,
            new_subject_input=new_subject_input,
            new_subject_digest=new_subject_digest,
            rerun_roles=rerun_roles,
            reviewer_receipts=tuple(reviewer_receipts or ()),
        )

    async def _prepare_result(
        self, agent: object
    ) -> RemediationResult | tuple[RemediationResult, AssuranceRunBundle]:
        started = time.monotonic()
        policy = self.request.policy
        attempt_receipts: list[RemediationAttempt] = []
        patch_digests: list[str] = []
        validation_calls = 0
        last_validation: ValidationResult | None = None

        with IsolatedWorkspace.prepare(
            self.seed_root,
            self.request.workspace_grant,
            parent=self.workspace_parent,
        ) as workspace:
            executor = self._executor_for_workspace(workspace)
            baseline = await _await_if_needed(
                executor.validate(policy.authoritative_check_id, actor="controller")
            )
            validation_calls += 1
            last_validation = baseline
            baseline_status = _status(baseline)
            if baseline_status is ValidationStatus.PASSED:
                return self._result(
                    request=self.request,
                    status=RemediationStatus.NOOP,
                    reason_code="initial_validation_passed",
                    attempts=0,
                    validation_calls=validation_calls,
                    attempt_receipts=attempt_receipts,
                    patch_digests=patch_digests,
                    last_validation=last_validation,
                )
            if _blocked_status(baseline_status) or baseline_status is None:
                return self._result(
                    request=self.request,
                    status=RemediationStatus.BLOCKED,
                    reason_code=getattr(baseline, "reason_code", "baseline_invalid"),
                    attempts=0,
                    validation_calls=validation_calls,
                    attempt_receipts=attempt_receipts,
                    patch_digests=patch_digests,
                    last_validation=last_validation,
                )

            for attempt in range(1, policy.max_attempts + 1):
                if time.monotonic() - started >= policy.total_wall_time_s:
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.BLOCKED,
                        reason_code="total_wall_time_exhausted",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )
                before = workspace.snapshot()
                budgeted = BudgetedValidationExecutor(
                    executor,
                    policy.max_validation_calls_per_attempt,
                )
                tools = make_validation_tool_registry(workspace.public_view(), budgeted)
                remaining = max(0.1, policy.total_wall_time_s - (time.monotonic() - started))
                try:
                    agent_result = await asyncio.wait_for(
                        self._run_agent(
                            agent,
                            workspace=workspace,
                            tools=tools,
                            attempt=attempt,
                            feedback=last_validation,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.BLOCKED,
                        reason_code="agent_timeout",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls + budgeted.calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )
                except Exception as exc:
                    status = (
                        RemediationStatus.BLOCKED
                        if isinstance(exc, ValueError)
                        else RemediationStatus.FAILED
                    )
                    return self._result(
                        request=self.request,
                        status=status,
                        reason_code=f"agent_error:{type(exc).__name__}",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls + budgeted.calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )
                if isinstance(agent_result, AgentAttemptResult):
                    if (
                        agent_result.iterations is not None
                        and agent_result.iterations > policy.max_agent_iterations
                    ):
                        return self._result(
                            request=self.request,
                            status=RemediationStatus.BLOCKED,
                            reason_code="agent_iteration_budget_exhausted",
                            attempts=len(attempt_receipts),
                            validation_calls=validation_calls + budgeted.calls,
                            attempt_receipts=attempt_receipts,
                            patch_digests=patch_digests,
                            last_validation=last_validation,
                        )

                after = workspace.snapshot()
                if before == after:
                    attempt_receipts.append(
                        RemediationAttempt(
                            attempt=attempt,
                            changed=False,
                            status="no_change",
                            validation_receipts=(),
                        )
                    )
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.FAILED,
                        reason_code="no_workspace_change",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls + budgeted.calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )

                patch_digest = _snapshot_digest(before, after)
                patch_digests.append(patch_digest)
                post_validation = await _await_if_needed(
                    executor.validate(policy.authoritative_check_id, actor="controller")
                )
                validation_calls += budgeted.calls + 1
                last_validation = post_validation
                post_status = _status(post_validation)
                attempt_receipts.append(
                    RemediationAttempt(
                        attempt=attempt,
                        changed=True,
                        patch_digest=patch_digest,
                        validation_receipts=(post_validation,),
                        status="blocked" if _blocked_status(post_status) else "changed",
                    )
                )
                if post_status is ValidationStatus.PASSED:
                    break
                if _blocked_status(post_status) or post_status is None:
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.BLOCKED,
                        reason_code=getattr(post_validation, "reason_code", "validation_blocked"),
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )
                if attempt == policy.max_attempts:
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.BUDGET_EXHAUSTED,
                        reason_code="max_repair_attempts",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )

            built = self.subject_builder(_aggregate_patch_digest(tuple(patch_digests)))
            new_subject = self._new_subject(built)
            if new_subject is None:
                return self._result(
                    request=self.request,
                    status=RemediationStatus.BLOCKED,
                    reason_code="subject_builder_invalid",
                    attempts=len(attempt_receipts),
                    validation_calls=validation_calls,
                    attempt_receipts=attempt_receipts,
                    patch_digests=patch_digests,
                    last_validation=last_validation,
                )
            subject_input, new_digest = new_subject
            if new_digest == self.request.old_subject_digest:
                return self._result(
                    request=self.request,
                    status=RemediationStatus.BLOCKED,
                    reason_code="subject_digest_unchanged",
                    attempts=len(attempt_receipts),
                    validation_calls=validation_calls,
                    attempt_receipts=attempt_receipts,
                    patch_digests=patch_digests,
                    last_validation=last_validation,
                )

            if self.workspace_parent is not None:
                try:
                    workspace.publish(
                        parent=self.workspace_parent,
                        remediation_id=self.request.remediation_id,
                        subject_digest=new_digest,
                    )
                except (OSError, RuntimeError, WorkspaceViolation):
                    return self._result(
                        request=self.request,
                        status=RemediationStatus.BLOCKED,
                        reason_code="durable_workspace_publish_failed",
                        attempts=len(attempt_receipts),
                        validation_calls=validation_calls,
                        attempt_receipts=attempt_receipts,
                        patch_digests=patch_digests,
                        last_validation=last_validation,
                    )

            role = self.selected_finding.reviewer_role
            reviewer_output = await self._run_reviewer(
                role,
                subject_input,
                new_digest,
                workspace=workspace,
            )
            reviewer_receipt = self._reviewer_receipt(reviewer_output, role, new_digest)
            if reviewer_receipt is None:
                return self._result(
                    request=self.request,
                    status=RemediationStatus.BLOCKED,
                    reason_code="reviewer_subject_mismatch",
                    attempts=len(attempt_receipts),
                    validation_calls=validation_calls,
                    attempt_receipts=attempt_receipts,
                    patch_digests=patch_digests,
                    last_validation=last_validation,
                )
            result = self._result(
                request=self.request,
                status=RemediationStatus.SUCCEEDED,
                reason_code="prepared_new_subject",
                attempts=len(attempt_receipts),
                validation_calls=validation_calls,
                attempt_receipts=attempt_receipts,
                patch_digests=patch_digests,
                last_validation=last_validation,
                new_subject_input=subject_input,
                new_subject_digest=new_digest,
                rerun_roles=(role,),
                reviewer_receipts=[reviewer_receipt],
            )
            return result, reviewer_output.bundle

    async def prepare(self, agent: object) -> PreparedRemediationHandoff:
        """Prepare one remediation result and retain its exact reviewer bundle."""

        prepared = await self._prepare_result(agent)
        if isinstance(prepared, tuple):
            result, bundle = prepared
        else:
            result, bundle = prepared, None
        return PreparedRemediationHandoff(result=result, bundle=bundle)

    async def run(self, agent: object) -> RemediationResult:
        """Preserve the legacy result-only API over one preparation call."""

        handoff = await self.prepare(agent)
        return handoff.result
