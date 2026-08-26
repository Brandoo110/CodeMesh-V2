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
    field_validator,
    model_validator,
)

from assurance.contracts import Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.remediation_validation import (
    BudgetedValidationExecutor,
    ValidationResult,
    ValidationStatus,
    make_validation_tool_registry,
)
from assurance.remediation_workspace import IsolatedWorkspace, WorkspaceGrant


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
    """Subject/role binding returned by the injected reviewer rerunner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_role: Literal["intent", "architecture", "operability"]
    subject_digest: str
    accepted: StrictBool = True

    @field_validator("subject_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _is_sha256_digest(value):
            raise ValueError("subject_digest must be sha256:<64 lowercase hex>")
        return value


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
                or receipt.accepted is not True
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
            "attempt": attempt,
            "workspace": workspace.public_view(),
            "tools": tools,
            "validation_feedback": feedback,
            "max_iterations": self.request.policy.max_agent_iterations,
        }
        return await _await_if_needed(function(**_filtered_kwargs(function, values)))

    async def _run_reviewer(self, role: str, subject: SubjectDigestInput, digest: str) -> Any:
        function = self.reviewer_rerunner
        values = {
            "reviewer_role": role,
            "role": role,
            "subject_input": subject,
            "subject": subject,
            "subject_digest": digest,
            "request": self.request,
            "finding_id": self.request.human_selected_finding_id,
        }
        return await _await_if_needed(function(**_filtered_kwargs(function, values)))

    @staticmethod
    def _reviewer_receipt(result: Any, role: str, digest: str) -> ReviewerRerunReceipt | None:
        if isinstance(result, ReviewerRerunReceipt):
            if result.accepted is not True:
                return None
            candidate_role = result.reviewer_role
            candidate_digest = result.subject_digest
        elif isinstance(result, Mapping):
            if result.get("accepted") is not True:
                return None
            candidate_role = result.get("reviewer_role", result.get("role"))
            candidate_digest = result.get("subject_digest")
        else:
            if getattr(result, "accepted", None) is not True:
                return None
            candidate_role = getattr(result, "reviewer_role", getattr(result, "role", None))
            candidate_digest = getattr(result, "subject_digest", None)
            findings = getattr(result, "findings", None)
            if (candidate_role is None or candidate_digest is None) and findings:
                for finding in findings:
                    finding_role = getattr(finding, "reviewer_role", None)
                    finding_digest = getattr(finding, "subject_digest", None)
                    if finding_role is not None and finding_digest is not None:
                        candidate_role, candidate_digest = finding_role, finding_digest
                        break
        if candidate_role != role or candidate_digest != digest:
            return None
        try:
            return ReviewerRerunReceipt(
                reviewer_role=role,
                subject_digest=digest,
            )
        except ValueError:
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

    async def run(self, agent: object) -> RemediationResult:
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

            role = self.selected_finding.reviewer_role
            reviewer_output = await self._run_reviewer(role, subject_input, new_digest)
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
            return result
