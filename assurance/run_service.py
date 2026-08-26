"""GP-02 change-to-case orchestration.

This module is deliberately a composition seam.  The existing collectors,
reviewer and policy gate remain the only implementations of their respective
contracts; :class:`AssuranceRunService` only validates the caller intent,
connects their immutable results, applies the final freshness fence and sends
one bundle to a commit port.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .commands import (
    CommandBatchResult,
    CommandSpec,
    DeterministicCommandCollector,
)
from .contracts import (
    AcceptanceCase,
    ChangeSubject,
    Evidence,
    ExecutionReceipt,
    ExecutionStep,
    Finding,
)
from .digests import normalize_repo_path, normalize_repository_identity
from .intake import IntakeResult, TaskPolicyCollector
from .manifest import (
    EvidenceManifestBuilder,
    EvidenceManifestInput,
    EvidenceManifestResult,
)
from .policy import PolicyEvaluationInput, PolicyGate, PolicyGateResult
from .risk import (
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
    RiskDeclarations,
)
from .single_reviewer import (
    ReviewerEvidenceContext,
    ReviewQuestion,
    SingleReviewerInput,
    SingleReviewerInvocation,
    SingleReviewerNormalizationInput,
    SingleReviewerPayloadError,
    SingleReviewerSubjectMismatchError,
    SingleReviewerResult,
    SingleStrongReviewer,
)
from .snapshot import GitSnapshotCollector, GitSnapshotResult
from .state_machine import (
    AcceptanceBinding,
    AcceptanceEvent,
    AcceptanceMachineState,
    apply_acceptance_event,
)


_SHA256_RE = r"^sha256:[0-9a-f]{64}$"
_MAX_COMMANDS = 16
_DEFAULT_POLICY_VERSION = "gate.v0"
_DEFAULT_RUBRIC_VERSION = "single_general.v0"
_DEFAULT_ORCHESTRATION_VERSION = "golden.v1"
_DEFAULT_ROUTING_RULE = "single_general.v0:shared_invocation"
_REVIEWER_ROLES = ("intent", "architecture", "operability")
_SAFE_REDACTION = frozenset({"declared_redacted", "not_applicable"})
_REVIEWER_FAILURE_CODES = {
    "failure": "REVIEWER_PROVIDER_FAILURE",
    "timeout": "REVIEWER_TIMEOUT",
    "cancelled": "REVIEWER_CANCELLED",
    "budget_exceeded": "REVIEWER_BUDGET_EXCEEDED",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _latency_ms(started: datetime, completed: datetime) -> int:
    delta = completed - started
    micros = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return micros // 1000


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


def _strip_datetimes(value: Any) -> Any:
    """Remove collection timestamps while comparing two fence snapshots."""

    if isinstance(value, Mapping):
        return {
            key: _strip_datetimes(item)
            for key, item in value.items()
            if key not in {"collected_at", "evaluated_at", "created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_strip_datetimes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_datetimes(item) for item in value)
    return value


class AssuranceRunError(Exception):
    """Base error for a run that cannot produce a committed bundle."""


class AssuranceRunValidationError(AssuranceRunError, ValueError):
    """Caller intent or immutable run configuration is invalid."""


class AssuranceRunStaleError(AssuranceRunError):
    """A source or artifact changed after it was collected."""


class AssuranceRunRedactionError(AssuranceRunError):
    """The redaction adapter failed; fail closed without committing."""


class IdempotencyConflictError(AssuranceRunError):
    """A commit port found the key bound to another request digest."""


class RedactionDisposition(str, Enum):
    """The only dispositions understood by the manifest builder."""

    DECLARED_REDACTED = "declared_redacted"
    NOT_APPLICABLE = "not_applicable"
    CONTAINS_UNREDACTED_CONTENT = "contains_unredacted_content"
    NOT_ASSESSED = "not_assessed"


class ReviewerRoute(BaseModel):
    """Construction-time reviewer route; HTTP cannot override this value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    timeout_seconds: StrictInt = Field(gt=0)
    token_budget: StrictInt | None = Field(default=None, ge=0)
    routing_rule: str = Field(default=_DEFAULT_ROUTING_RULE, min_length=1)
    tool_grants: tuple[str, ...] = ()

    @field_validator("provider", "model_ref", "routing_rule")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("route text must not be blank")
        return value

    @field_validator("tool_grants")
    @classmethod
    def _empty_tool_grants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError("reviewer route must not grant tools")
        return value


@dataclass(frozen=True, init=False)
class AssuranceRunConfig:
    """Frozen composition configuration owned by the service constructor."""

    workspace_root: Path
    orchestration_version: str
    redaction_policy_version: str
    policy_version: str
    rubric_version: str
    allowed_commands: tuple[CommandSpec, ...]
    freshness_ttl_seconds: int
    reviewer_route: ReviewerRoute

    def __init__(
        self,
        *,
        workspace_root: Path,
        allowed_commands: tuple[CommandSpec, ...],
        redaction_policy_version: str,
        orchestration_version: str = _DEFAULT_ORCHESTRATION_VERSION,
        policy_version: str = _DEFAULT_POLICY_VERSION,
        rubric_version: str = _DEFAULT_RUBRIC_VERSION,
        freshness_ttl_seconds: int = 300,
        reviewer_route: ReviewerRoute | Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be a pathlib.Path")
        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if type(orchestration_version) is not str or not orchestration_version.strip():
            raise ValueError("orchestration_version must be nonblank")
        if type(redaction_policy_version) is not str or not redaction_policy_version.strip():
            raise ValueError("redaction_policy_version must be nonblank")
        if type(policy_version) is not str or not policy_version.strip():
            raise ValueError("policy_version must be nonblank")
        if type(rubric_version) is not str or not rubric_version.strip():
            raise ValueError("rubric_version must be nonblank")
        if type(allowed_commands) is not tuple:
            raise TypeError("allowed_commands must be an exact tuple")
        if not allowed_commands:
            raise ValueError("allowed_commands must contain at least one CommandSpec")
        if len(allowed_commands) > _MAX_COMMANDS:
            raise ValueError("allowed_commands must contain at most 16 commands")
        for item in allowed_commands:
            if type(item) is not CommandSpec:
                raise TypeError("allowed_commands must contain CommandSpec values")
        if type(freshness_ttl_seconds) is not int or isinstance(
            freshness_ttl_seconds, bool
        ) or freshness_ttl_seconds <= 0:
            raise ValueError("freshness_ttl_seconds must be a positive int")
        route = reviewer_route
        if route is None:
            route = ReviewerRoute(
                provider="configured",
                model_ref="configured",
                timeout_seconds=60,
            )
        elif type(route) is not ReviewerRoute:
            route = ReviewerRoute.model_validate(route)
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "orchestration_version", orchestration_version)
        object.__setattr__(self, "redaction_policy_version", redaction_policy_version)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "rubric_version", rubric_version)
        object.__setattr__(self, "allowed_commands", allowed_commands)
        object.__setattr__(self, "freshness_ttl_seconds", freshness_ttl_seconds)
        object.__setattr__(self, "reviewer_route", route)


class AssuranceRunIntent(BaseModel):
    """The caller-supplied intent; all domain facts are server-derived."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_path: Path
    repository_identity: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    policy_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    runbook_paths: tuple[str, ...] = ()
    command_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_COMMANDS)
    changed_lines_total: StrictInt | None = Field(default=None, ge=0)
    external_side_effects: Literal["none_declared", "present_declared", "unknown"] = (
        "unknown"
    )
    provider_boundary: Literal[
        "within_declared_boundary", "crosses_declared_boundary", "unknown"
    ] = "unknown"

    @field_validator("repository_identity", "base_ref")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("intent text must not be blank")
        return value

    @field_validator("base_ref")
    @classmethod
    def _base_ref_syntax(cls, value: str) -> str:
        if value.startswith("-") or any(char.isspace() for char in value) or "\x00" in value:
            raise ValueError("base_ref contains forbidden characters")
        return value

    @field_validator("task_path")
    @classmethod
    def _task_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_path must not be blank")
        try:
            normalized = normalize_repo_path(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("task_path is invalid") from exc
        if normalized != value or not value.endswith(".md"):
            raise ValueError("task_path must be a canonical .md path")
        return value

    @field_validator("command_ids", mode="before")
    @classmethod
    def _command_ids(cls, value: object) -> tuple[str, ...]:
        if type(value) not in (tuple, list):
            raise ValueError("command_ids must be a tuple or list")
        items = tuple(value)
        if not 1 <= len(items) <= _MAX_COMMANDS:
            raise ValueError("command_ids must contain 1..16 unique values")
        seen: set[str] = set()
        for item in items:
            if type(item) is not str or not item.strip():
                raise ValueError("command_ids must contain nonblank strings")
            if item in seen:
                raise ValueError("command_ids must be unique")
            seen.add(item)
        return items

    @field_validator("policy_paths", "adr_paths", "runbook_paths", mode="before")
    @classmethod
    def _repo_paths(cls, value: object) -> tuple[str, ...]:
        if type(value) not in (tuple, list):
            raise ValueError("declared paths must be a tuple or list")
        result = []
        seen: set[str] = set()
        for item in value:
            if type(item) is not str:
                raise ValueError("declared paths must contain strings")
            try:
                normalized = normalize_repo_path(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("declared path is invalid") from exc
            if normalized != item or not item.endswith(".md"):
                raise ValueError("declared paths must be canonical .md paths")
            if item in seen:
                raise ValueError("declared paths must be unique")
            seen.add(item)
            result.append(item)
        return tuple(result)


class ReviewerContextPlanEntry(BaseModel):
    """One redaction decision and optional safe context for one Evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    artifact_digest: str = Field(pattern=_SHA256_RE)
    disposition: RedactionDisposition
    content: str | None = None
    truncated: StrictBool = False

    @model_validator(mode="after")
    def _safe_context_requirements(self) -> "ReviewerContextPlanEntry":
        if self.disposition in _SAFE_REDACTION and self.content is None:
            raise ValueError("safe redaction dispositions require context content")
        if self.disposition not in _SAFE_REDACTION and self.content is not None:
            raise ValueError("unsafe redaction dispositions must not expose content")
        return self


class ReviewerContextPlan(BaseModel):
    """A one-to-one redaction assessment for the three base Evidence items."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ReviewerContextPlanEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_entries(self) -> "ReviewerContextPlan":
        ids = [item.evidence_id for item in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("redaction plan evidence IDs must be unique")
        return self


class ReviewerInvocationResponse(BaseModel):
    """Facts returned by the sole external reviewer invocation seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "failure", "timeout", "cancelled", "budget_exceeded"]
    provider: str | None = Field(default=None, min_length=1)
    model_ref: str | None = Field(default=None, min_length=1)
    usage_status: Literal["measured", "unavailable"] = "unavailable"
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    raw_response: bytes | None = None
    schema_status: Literal["valid", "repaired", "invalid", "not_produced"] = (
        "not_produced"
    )
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)

    @field_validator("raw_response", mode="before")
    @classmethod
    def _raw_bytes(cls, value: object) -> object:
        if value is None or type(value) is bytes:
            return value
        raise ValueError("raw_response must be exact bytes or None")

    @model_validator(mode="after")
    def _success_facts(self) -> "ReviewerInvocationResponse":
        if self.status == "success" and self.raw_response is None:
            raise ValueError("success response requires raw_response")
        if self.status == "success" and self.schema_status not in ("valid", "repaired"):
            raise ValueError("success response requires a valid or repaired schema")
        if self.status != "success" and self.schema_status in ("valid", "repaired"):
            raise ValueError("failed response must not claim a valid schema")
        if self.usage_status == "measured" and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.cost_usd is None
        ):
            raise ValueError("measured usage requires token and cost facts")
        return self


class ReviewerRunRecord(BaseModel):
    """Auditable reviewer route/result summary without raw response bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "success",
        "failure",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "blocked_redaction",
        "invalid_json",
    ]
    planned_route: ReviewerRoute
    rubric_version: str = Field(min_length=1)
    prompt_id: str | None = Field(default=None, pattern=r"^srp_[0-9a-f]{32}$")
    prompt_digest: str | None = Field(default=None, pattern=_SHA256_RE)
    actual_provider: str | None = None
    actual_model_ref: str | None = None
    schema_status: Literal["valid", "repaired", "invalid", "not_produced"]
    raw_response_artifact_digest: str | None = Field(
        default=None, pattern=_SHA256_RE
    )
    canonical_response_digest: str | None = Field(
        default=None, pattern=_SHA256_RE
    )
    result_id: str | None = Field(default=None, pattern=r"^srr_[0-9a-f]{32}$")
    result_digest: str | None = Field(default=None, pattern=_SHA256_RE)
    error_code: str | None = None


class AssuranceRunBundle(BaseModel):
    """Complete in-memory Golden Path result handed to one commit call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    request_digest: str = Field(pattern=_SHA256_RE)
    subject: ChangeSubject
    draft_case: AcceptanceCase
    case: AcceptanceCase
    binding: AcceptanceBinding
    git: GitSnapshotResult
    intake: IntakeResult
    commands: CommandBatchResult
    manifest: EvidenceManifestResult
    risk: RiskClassificationResult
    evidence: tuple[Evidence, ...] = Field(min_length=4)
    findings: tuple[Finding, ...] = ()
    questions: tuple[ReviewQuestion, ...] = ()
    reviewer: ReviewerRunRecord
    execution_receipt: ExecutionReceipt
    policy: PolicyGateResult
    events: tuple[AcceptanceEvent, ...] = Field(min_length=1)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _bind_all_results(self) -> "AssuranceRunBundle":
        subject_digest = self.subject.subject_digest
        if (
            self.subject.repository != self.git.snapshot.repository
            or self.subject.base_revision != self.git.snapshot.base_revision
            or self.subject.head_revision != self.git.snapshot.head_revision
            or self.subject.task_digest != self.intake.snapshot.task_digest
            or self.subject.policy_version != self.binding.policy_version
            or self.subject.created_at != self.started_at
        ):
            raise ValueError("subject facts must match collected results")
        if self.draft_case.state != "DRAFT":
            raise ValueError("draft_case must remain DRAFT")
        if self.case.case_id != self.draft_case.case_id:
            raise ValueError("case and draft_case must share case_id")
        if self.binding.subject_digest != subject_digest:
            raise ValueError("binding must bind to subject")
        if self.binding.policy_version != self.subject.policy_version:
            raise ValueError("binding policy_version must match subject")
        if self.binding.rubric_version != self.reviewer.rubric_version:
            raise ValueError("binding rubric_version must match reviewer rubric")
        if self.execution_receipt.run_id != self.run_id:
            raise ValueError("receipt must bind to bundle run_id")
        if self.git.snapshot.subject_digest != subject_digest:
            raise ValueError("git result must bind to subject")
        if self.intake.snapshot.subject_digest != subject_digest:
            raise ValueError("intake result must bind to subject")
        if self.commands.snapshot.subject_digest != subject_digest:
            raise ValueError("command result must bind to subject")
        if self.manifest.manifest.subject_digest != subject_digest:
            raise ValueError("manifest result must bind to subject")
        if self.risk.classification.subject_digest != subject_digest:
            raise ValueError("risk classification must bind to subject")
        if self.risk.input.snapshot.subject_digest != subject_digest:
            raise ValueError("risk input snapshot must bind to subject")
        if self.risk.input.intake.subject_digest != subject_digest:
            raise ValueError("risk input intake must bind to subject")
        if self.risk.input.manifest.subject_digest != subject_digest:
            raise ValueError("risk input manifest must bind to subject")
        if self.risk.input.snapshot != self.git.snapshot:
            raise ValueError("risk input snapshot must match bundle git snapshot")
        if self.risk.input.intake != self.intake.snapshot:
            raise ValueError("risk input intake must match bundle intake snapshot")
        if self.risk.input.manifest != self.manifest.manifest:
            raise ValueError("risk input manifest must match bundle manifest")
        expected = (
            self.git.evidence,
            self.intake.evidence,
            self.commands.evidence,
            self.manifest.evidence,
        )
        if self.evidence != expected:
            raise ValueError("bundle evidence must use the fixed collector order")
        if self.execution_receipt.subject_digest != subject_digest:
            raise ValueError("receipt must bind to subject")
        if self.policy.decision.subject_digest != subject_digest:
            raise ValueError("policy decision must bind to subject")
        if self.policy.input.subject != self.subject:
            raise ValueError("policy input subject must match bundle subject")
        if self.policy.input.risk_result != self.risk:
            raise ValueError("policy input risk must match bundle risk")
        if self.policy.input.findings != self.findings:
            raise ValueError("policy input findings must match bundle findings")
        if self.policy.input.execution_receipts != (self.execution_receipt,):
            raise ValueError("policy input receipt must match bundle receipt")
        if self.case.subject_digest != subject_digest:
            raise ValueError("case must bind to subject")
        if self.draft_case.subject_digest != subject_digest:
            raise ValueError("draft case must bind to subject")
        question_ids = {item.question_id for item in self.questions}
        if question_ids:
            if self.case.state != "NEEDS_EVIDENCE":
                raise ValueError("question-bearing bundles must need evidence")
            expected_missing = tuple(
                sorted("review_question:" + item for item in question_ids)
            )
            if self.case.missing_evidence != expected_missing:
                raise ValueError("question missing evidence refs must be stable")
        elif self.case.state == "ACCEPTED":
            raise ValueError("a run must never auto-accept a case")
        if self.case.state not in {"EVIDENCE_COLLECTED", "NEEDS_EVIDENCE"}:
            raise ValueError("run case must end in an evidence-gated state")
        if self.reviewer.status == "success":
            if self.reviewer.result_id is None or self.reviewer.result_digest is None:
                raise ValueError("successful reviewer must bind SingleReviewerResult")
        elif self.reviewer.result_id is not None or self.reviewer.result_digest is not None:
            raise ValueError("failed reviewer must not bind a success result")
        if self.reviewer.status == "blocked_redaction":
            if (
                self.reviewer.prompt_id is not None
                or self.reviewer.prompt_digest is not None
            ):
                raise ValueError("redaction-blocked reviewer must not claim a prompt")
        elif (
            self.reviewer.prompt_id is None
            or self.reviewer.prompt_digest is None
        ):
            raise ValueError("invoked reviewer must bind its deterministic prompt")
        state = AcceptanceMachineState(
            schema_version="v1",
            case=self.draft_case,
            applied_events=(),
        )
        try:
            for event in self.events:
                state = apply_acceptance_event(state, event)
        except Exception as exc:
            raise ValueError("bundle events cannot replay from draft_case") from exc
        if state.case != self.case:
            raise ValueError("bundle case must equal exact event replay")
        return self


class AssuranceRunResult(BaseModel):
    """Service return envelope; replay is explicit in ``cached``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    run_id: str = Field(min_length=1)
    request_digest: str = Field(pattern=_SHA256_RE)
    cached: StrictBool = False
    bundle: AssuranceRunBundle

    @model_validator(mode="after")
    def _bind_envelope(self) -> "AssuranceRunResult":
        if self.run_id != self.bundle.run_id:
            raise ValueError("result run_id must match bundle run_id")
        if self.request_digest != self.bundle.request_digest:
            raise ValueError("result request_digest must match bundle request_digest")
        return self

@runtime_checkable
class ReviewerInvoker(Protocol):
    async def invoke(
        self, prompt: Any, *, run_id: str, route: ReviewerRoute
    ) -> ReviewerInvocationResponse:
        """Invoke the configured external reviewer exactly once."""


@runtime_checkable
class ReviewerContextBuilder(Protocol):
    def prepare(
        self,
        evidences: tuple[Evidence, ...],
        *,
        artifact_store: ArtifactStore,
        subject_digest: str,
    ) -> ReviewerContextPlan:
        """Assess and optionally expose redacted content for each Evidence."""


@runtime_checkable
class RunCommitter(Protocol):
    def lookup(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | AssuranceRunBundle | None:
        """Return an exact replay or ``None`` before external work."""

    def commit(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult | AssuranceRunBundle:
        """Persist one complete bundle atomically."""


class AssuranceRunService:
    """Run the frozen GP-02 sequence with no I/O in the commit boundary."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        reviewer_invoker: ReviewerInvoker,
        committer: RunCommitter,
        context_builder: ReviewerContextBuilder,
        config: AssuranceRunConfig,
        git_collector: Any | None = None,
        intake_collector: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        if reviewer_invoker is None:
            raise TypeError("reviewer_invoker is required")
        if committer is None:
            raise TypeError("committer is required")
        if context_builder is None:
            raise TypeError("context_builder is required")
        if type(config) is not AssuranceRunConfig:
            raise TypeError("config must be an exact AssuranceRunConfig")
        self._artifact_store = artifact_store
        self._config = config
        self._reviewer_invoker = reviewer_invoker
        self._committer = committer
        self._context_builder = context_builder
        self._git_collector = git_collector or GitSnapshotCollector()
        self._intake_collector = intake_collector or TaskPolicyCollector()
        self._command_collector = DeterministicCommandCollector(
            config.allowed_commands
        )
        self._allowed_command_ids = frozenset(
            item.command_id for item in config.allowed_commands
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(
        self, intent: AssuranceRunIntent, *, idempotency_key: str
    ) -> AssuranceRunResult:
        """Execute one Golden Path run and commit only after the final fence."""

        self._validate_intent(intent, idempotency_key)
        request_digest = self._request_digest(intent)
        cached = await asyncio.to_thread(
            self._committer.lookup, idempotency_key, request_digest
        )
        cached_result = self._coerce_result(cached, request_digest, cached=True)
        if cached_result is not None:
            return cached_result

        await asyncio.to_thread(self._validate_workspace, intent)
        started_at = self._now()
        task_digest = await asyncio.to_thread(
            self._intake_collector.probe_task_digest,
            intent.repository_path,
            task_path=intent.task_path,
        )
        if type(task_digest) is not str or not task_digest.startswith("sha256:"):
            raise AssuranceRunValidationError("probe did not return a sha256 task digest")

        git_result = await asyncio.to_thread(
            self._git_collector.collect,
            intent.repository_path,
            repository_identity=intent.repository_identity,
            base_ref=intent.base_ref,
            task_digest=task_digest,
            policy_version=self._config.policy_version,
            rubric_version=self._config.rubric_version,
            artifact_store=self._artifact_store,
            attachment_digests=(),
            collected_at=started_at,
        )
        self._require_type(git_result, GitSnapshotResult, "git collector result")
        subject_digest = git_result.snapshot.subject_digest
        if git_result.snapshot.repository != normalize_repository_identity(
            intent.repository_identity
        ):
            raise AssuranceRunStaleError("Git repository identity does not match intent")

        intake_result = await asyncio.to_thread(
            self._intake_collector.collect,
            intent.repository_path,
            subject_digest=subject_digest,
            artifact_store=self._artifact_store,
            task_path=intent.task_path,
            policy_paths=intent.policy_paths,
            adr_paths=intent.adr_paths,
            runbook_paths=intent.runbook_paths,
            collected_at=started_at,
        )
        self._require_type(intake_result, IntakeResult, "intake collector result")
        if intake_result.snapshot.task_digest != task_digest:
            raise AssuranceRunStaleError("task changed between probe and intake")
        self._require_subjects(subject_digest, intake_result)

        command_result = await asyncio.to_thread(
            self._command_collector.collect,
            intent.repository_path,
            subject_digest=subject_digest,
            artifact_store=self._artifact_store,
            command_ids=intent.command_ids,
            collected_at=started_at,
        )
        self._require_type(command_result, CommandBatchResult, "command collector result")
        self._require_subjects(subject_digest, command_result)

        evidences = (
            git_result.evidence,
            intake_result.evidence,
            command_result.evidence,
        )
        context_plan = await asyncio.to_thread(
            self._prepare_context, evidences, subject_digest
        )
        manifest_result = await asyncio.to_thread(
            self._build_manifest,
            evidences,
            context_plan,
            subject_digest,
            started_at,
        )
        self._require_type(manifest_result, EvidenceManifestResult, "manifest result")

        declarations = RiskDeclarations(
            schema_version="v1",
            changed_lines_total=intent.changed_lines_total,
            external_side_effects=intent.external_side_effects,
            provider_boundary=intent.provider_boundary,
        )
        risk_input = RiskClassificationInput(
            schema_version="v1",
            snapshot=git_result.snapshot,
            intake=intake_result.snapshot,
            manifest=manifest_result.manifest,
            declarations=declarations,
        )
        risk_result = RiskClassifier.classify(risk_input)

        subject = self._build_subject(
            git_result,
            intake_result,
            started_at,
        )
        binding = AcceptanceBinding(
            schema_version="v1",
            subject_digest=subject.subject_digest,
            policy_version=self._config.policy_version,
            rubric_version=self._config.rubric_version,
        )
        draft_case = AcceptanceCase(
            schema_version="v1",
            case_id=self._case_id(subject.subject_digest),
            subject_digest=subject.subject_digest,
            state="DRAFT",
            created_at=started_at,
            updated_at=started_at,
        )

        reviewer_record, findings, questions, receipt = await self._review(
            subject=subject,
            risk_result=risk_result,
            context_plan=context_plan,
            run_id=self._run_id(request_digest, idempotency_key),
            evaluated_at=self._now(),
        )
        policy_input = PolicyEvaluationInput(
            schema_version="v1",
            subject=subject,
            risk_result=risk_result,
            findings=findings,
            execution_receipts=(receipt,),
            human_decisions=(),
            evaluated_at=max(self._now(), receipt.completed_at),
        )
        policy_result = PolicyGate.evaluate(policy_input)

        fence_collection_at = max(self._now(), policy_result.decision.evaluated_at)
        fence_at = await asyncio.to_thread(
            self._final_fence,
            intent,
            task_digest,
            subject_digest,
            git_result,
            intake_result,
            manifest_result,
            fence_collection_at,
        )

        case, events = self._build_case_and_events(
            draft_case=draft_case,
            subject_digest=subject_digest,
            evidence=evidences + (manifest_result.evidence,),
            findings=findings,
            questions=questions,
            receipt=receipt,
            policy=policy_result,
            occurred_at=fence_at,
        )
        bundle = AssuranceRunBundle(
            schema_version="v1",
            run_id=self._run_id(request_digest, idempotency_key),
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            subject=subject,
            draft_case=draft_case,
            case=case,
            binding=binding,
            git=git_result,
            intake=intake_result,
            commands=command_result,
            manifest=manifest_result,
            risk=risk_result,
            evidence=evidences + (manifest_result.evidence,),
            findings=findings,
            questions=questions,
            reviewer=reviewer_record,
            execution_receipt=receipt,
            policy=policy_result,
            events=events,
            started_at=started_at,
            completed_at=fence_at,
        )
        committed = await asyncio.to_thread(
            self._committer.commit,
            bundle,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if committed is None:
            raise AssuranceRunError("committer.commit did not return a persisted result")
        result = self._coerce_result(committed, request_digest, cached=None)
        if result is None:
            raise AssuranceRunError("committer.commit did not return a persisted result")
        return result

    def _validate_intent(self, intent: AssuranceRunIntent, idempotency_key: str) -> None:
        if type(intent) is not AssuranceRunIntent:
            raise AssuranceRunValidationError("intent must be an exact AssuranceRunIntent")
        if type(idempotency_key) is not str or not idempotency_key.strip():
            raise AssuranceRunValidationError("idempotency_key must be nonblank")
        if len(idempotency_key.encode("utf-8")) > 256:
            raise AssuranceRunValidationError("idempotency_key is too long")
        if not intent.repository_path.is_absolute():
            raise AssuranceRunValidationError("repository_path must be absolute")
        try:
            normalize_repository_identity(intent.repository_identity)
        except (TypeError, ValueError) as exc:
            raise AssuranceRunValidationError("repository_identity is invalid") from exc
        if any(item not in self._allowed_command_ids for item in intent.command_ids):
            raise AssuranceRunValidationError("command_ids must use the configured allowlist")

    def _validate_workspace(self, intent: AssuranceRunIntent) -> None:
        root = self._config.workspace_root
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise AssuranceRunValidationError("workspace_root cannot be inspected") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise AssuranceRunValidationError("workspace_root must be a real directory")
        repository = intent.repository_path
        try:
            repo_stat = repository.lstat()
        except OSError as exc:
            raise AssuranceRunValidationError("repository_path cannot be inspected") from exc
        if stat.S_ISLNK(repo_stat.st_mode) or not stat.S_ISDIR(repo_stat.st_mode):
            raise AssuranceRunValidationError("repository_path must be a real directory")
        try:
            root_resolved = root.resolve(strict=True)
            repo_resolved = repository.resolve(strict=True)
            if os.path.commonpath((str(root_resolved), str(repo_resolved))) != str(
                root_resolved
            ):
                raise AssuranceRunValidationError(
                    "repository_path must be within workspace_root"
                )
        except (OSError, ValueError) as exc:
            if isinstance(exc, AssuranceRunValidationError):
                raise
            raise AssuranceRunValidationError("repository_path is outside workspace") from exc

    def _request_digest(self, intent: AssuranceRunIntent) -> str:
        payload = {
            "schema_version": "v1",
            "intent": _jsonable(intent),
            "config": {
                "workspace_root": str(self._config.workspace_root),
                "orchestration_version": self._config.orchestration_version,
                "redaction_policy_version": self._config.redaction_policy_version,
                "policy_version": self._config.policy_version,
                "rubric_version": self._config.rubric_version,
                "allowed_commands": _jsonable(self._config.allowed_commands),
                "freshness_ttl_seconds": self._config.freshness_ttl_seconds,
                "reviewer_route": _jsonable(self._config.reviewer_route),
            },
        }
        return _sha256(_canonical_bytes(payload))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AssuranceRunValidationError("clock must return an aware datetime")
        return value

    @staticmethod
    def _require_type(value: Any, expected: type, label: str) -> None:
        if type(value) is not expected:
            raise AssuranceRunError(f"{label} must be an exact {expected.__name__}")

    @staticmethod
    def _require_subjects(subject_digest: str, value: Any) -> None:
        nested = []
        if hasattr(value, "snapshot"):
            nested.append(value.snapshot.subject_digest)
        if hasattr(value, "manifest"):
            nested.append(value.manifest.subject_digest)
        if any(item != subject_digest for item in nested):
            raise AssuranceRunStaleError("collector result subject digest mismatch")

    def _prepare_context(
        self, evidences: tuple[Evidence, ...], subject_digest: str
    ) -> ReviewerContextPlan:
        try:
            value = self._context_builder.prepare(
                evidences,
                artifact_store=self._artifact_store,
                subject_digest=subject_digest,
            )
        except Exception as exc:
            raise AssuranceRunRedactionError("redaction assessment failed") from exc
        if type(value) is not ReviewerContextPlan:
            raise AssuranceRunRedactionError("redaction adapter returned an invalid plan")
        expected = {item.evidence_id: item for item in evidences}
        actual = {item.evidence_id: item for item in value.entries}
        if set(expected) != set(actual):
            raise AssuranceRunRedactionError("redaction plan must cover every base Evidence")
        for evidence_id, evidence in expected.items():
            entry = actual[evidence_id]
            if entry.kind != evidence.kind or entry.artifact_digest != evidence.artifact_digest:
                raise AssuranceRunRedactionError("redaction plan Evidence binding mismatch")
        return ReviewerContextPlan(
            entries=tuple(sorted(value.entries, key=lambda item: item.evidence_id))
        )

    def _build_manifest(
        self,
        evidences: tuple[Evidence, ...],
        context_plan: ReviewerContextPlan,
        subject_digest: str,
        evaluated_at: datetime,
    ) -> EvidenceManifestResult:
        redactions = {item.evidence_id: item.disposition.value for item in context_plan.entries}
        items = tuple(
            EvidenceManifestInput(
                schema_version="v1",
                evidence=evidence,
                fresh_until=evidence.collected_at
                + timedelta(seconds=self._config.freshness_ttl_seconds),
                redaction_status=redactions[evidence.evidence_id],
            )
            for evidence in evidences
        )
        return EvidenceManifestBuilder.build(
            items,
            subject_digest=subject_digest,
            evaluated_at=evaluated_at,
            artifact_store=self._artifact_store,
        )

    def _build_subject(
        self,
        git_result: GitSnapshotResult,
        intake_result: IntakeResult,
        created_at: datetime,
    ) -> ChangeSubject:
        snapshot = git_result.snapshot
        task_digest = intake_result.snapshot.task_digest
        if task_digest is None:
            raise AssuranceRunStaleError("task evidence is missing")
        change_id = "chg_" + hashlib.sha256(
            snapshot.subject_digest.encode("ascii")
        ).hexdigest()[:32]
        return ChangeSubject(
            schema_version="v1",
            change_id=change_id,
            subject_digest=snapshot.subject_digest,
            repository=snapshot.repository,
            base_revision=snapshot.base_revision,
            head_revision=snapshot.head_revision,
            task_digest=task_digest,
            policy_version=self._config.policy_version,
            created_at=created_at,
        )

    async def _review(
        self,
        *,
        subject: ChangeSubject,
        risk_result: RiskClassificationResult,
        context_plan: ReviewerContextPlan,
        run_id: str,
        evaluated_at: datetime,
    ) -> tuple[ReviewerRunRecord, tuple[Finding, ...], tuple[ReviewQuestion, ...], ExecutionReceipt]:
        safe_entries = tuple(
            item
            for item in context_plan.entries
            if item.disposition.value in _SAFE_REDACTION
        )
        if len(safe_entries) != len(context_plan.entries):
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status="blocked_redaction",
                route=self._config.reviewer_route,
                started_at=evaluated_at,
                completed_at=evaluated_at,
                actual_provider=None,
                actual_model_ref=None,
            )
            return (
                ReviewerRunRecord(
                    status="blocked_redaction",
                    planned_route=self._config.reviewer_route,
                    rubric_version=self._config.rubric_version,
                    schema_status="not_produced",
                    error_code="REDACTION_UNSAFE",
                ),
                (),
                (),
                receipt,
            )
        try:
            contexts = tuple(
                ReviewerEvidenceContext(
                    schema_version="v1",
                    evidence_id=item.evidence_id,
                    kind=item.kind,
                    artifact_digest=item.artifact_digest,
                    content=item.content or "",
                    content_digest=_sha256((item.content or "").encode("utf-8")),
                    truncated=item.truncated,
                    redaction_status=item.disposition.value,
                )
                for item in safe_entries
            )
            reviewer_input = SingleReviewerInput(
                schema_version="v1",
                subject=subject,
                risk_result=risk_result,
                contexts=contexts,
                evaluated_at=evaluated_at,
            )
            prompt = await asyncio.to_thread(SingleStrongReviewer.prepare, reviewer_input)
        except Exception as exc:
            raise AssuranceRunRedactionError("safe context could not be bound") from exc

        invoke_started = self._now()
        response = self._reviewer_invoker.invoke(
            prompt,
            run_id=run_id,
            route=self._config.reviewer_route,
        )
        if inspect.isawaitable(response):
            response = await response
        response = self._coerce_invocation_response(response)

        actual_provider = response.provider or self._config.reviewer_route.provider
        actual_model_ref = response.model_ref or self._config.reviewer_route.model_ref
        completed = response.completed_at or self._now()
        started = response.started_at or invoke_started
        if completed < started:
            completed = started
        if response.status != "success":
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status=response.status,
                route=self._config.reviewer_route,
                started_at=started,
                completed_at=completed,
                actual_provider=actual_provider,
                actual_model_ref=actual_model_ref,
            )
            return (
                ReviewerRunRecord(
                    status=response.status,
                    planned_route=self._config.reviewer_route,
                    rubric_version=self._config.rubric_version,
                    prompt_id=prompt.prompt_id,
                    prompt_digest=prompt.prompt_digest,
                    actual_provider=actual_provider,
                    actual_model_ref=actual_model_ref,
                    schema_status="not_produced",
                    error_code=_REVIEWER_FAILURE_CODES[response.status],
                ),
                (),
                (),
                receipt,
            )

        raw = response.raw_response
        if type(raw) is not bytes:
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status="failure",
                route=self._config.reviewer_route,
                started_at=started,
                completed_at=completed,
                actual_provider=actual_provider,
                actual_model_ref=actual_model_ref,
            )
            return (
                ReviewerRunRecord(
                    status="failure",
                    planned_route=self._config.reviewer_route,
                    rubric_version=self._config.rubric_version,
                    prompt_id=prompt.prompt_id,
                    prompt_digest=prompt.prompt_digest,
                    actual_provider=actual_provider,
                    actual_model_ref=actual_model_ref,
                    schema_status="not_produced",
                    error_code="REVIEWER_RESPONSE_MISSING",
                ),
                (),
                (),
                receipt,
            )
        try:
            invocation = SingleReviewerInvocation(
                schema_version="v1",
                run_id=run_id,
                model_ref=actual_model_ref,
                provider=actual_provider,
                usage_status=response.usage_status,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                started_at=started,
                completed_at=completed,
                latency_ms=max(0, _latency_ms(started, completed)),
                timeout_seconds=self._config.reviewer_route.timeout_seconds,
                result="success",
                schema_status=response.schema_status,
                fallback_reason=None,
                tool_grants=(),
            )
            normalization_input = SingleReviewerNormalizationInput(
                schema_version="v1",
                reviewer_input=reviewer_input,
                prompt=prompt,
                invocation=invocation,
                raw_response=raw,
            )
            normalized = await asyncio.to_thread(
                SingleStrongReviewer.normalize,
                normalization_input,
                self._artifact_store,
            )
            self._require_type(normalized, SingleReviewerResult, "reviewer normalization result")
            return (
                ReviewerRunRecord(
                    status="success",
                    planned_route=self._config.reviewer_route,
                    rubric_version=self._config.rubric_version,
                    prompt_id=prompt.prompt_id,
                    prompt_digest=prompt.prompt_digest,
                    actual_provider=actual_provider,
                    actual_model_ref=actual_model_ref,
                    schema_status=invocation.schema_status,
                    raw_response_artifact_digest=normalized.raw_response_artifact_digest,
                    canonical_response_digest=normalized.canonical_response_digest,
                    result_id=normalized.result_id,
                    result_digest=normalized.result_digest,
                ),
                normalized.findings,
                normalized.questions,
                normalized.execution_receipt,
            )
        except (SingleReviewerPayloadError, SingleReviewerSubjectMismatchError):
            status: Literal["invalid_json"] = "invalid_json"
            raw_artifact_digest = _sha256(raw)
            if await asyncio.to_thread(
                self._artifact_store.verify, raw_artifact_digest
            ) is not True:
                raise AssuranceRunError(
                    "invalid reviewer response artifact was not persisted"
                )
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status=status,
                route=self._config.reviewer_route,
                started_at=started,
                completed_at=completed,
                actual_provider=actual_provider,
                actual_model_ref=actual_model_ref,
            )
            return (
                ReviewerRunRecord(
                    status=status,
                    planned_route=self._config.reviewer_route,
                    rubric_version=self._config.rubric_version,
                    prompt_id=prompt.prompt_id,
                    prompt_digest=prompt.prompt_digest,
                    actual_provider=actual_provider,
                    actual_model_ref=actual_model_ref,
                    schema_status="invalid" if status == "invalid_json" else "not_produced",
                    raw_response_artifact_digest=raw_artifact_digest,
                    error_code="REVIEWER_INVALID_JSON",
                ),
                (),
                (),
                receipt,
            )

    @staticmethod
    def _coerce_invocation_response(value: Any) -> ReviewerInvocationResponse:
        if type(value) is ReviewerInvocationResponse:
            return value
        if isinstance(value, Mapping):
            return ReviewerInvocationResponse.model_validate(value)
        raise ValueError("reviewer invoker must return ReviewerInvocationResponse")

    def _failure_receipt(
        self,
        *,
        run_id: str,
        subject_digest: str,
        status: str,
        route: ReviewerRoute,
        started_at: datetime,
        completed_at: datetime,
        actual_provider: str | None,
        actual_model_ref: str | None,
    ) -> ExecutionReceipt:
        if status == "blocked_redaction":
            steps = tuple(
                ExecutionStep(
                    sequence=index,
                    planned_role=role,
                    actual_role=None,
                    model_ref=None,
                    provider=None,
                    tool_grants=(),
                    routing_rule=route.routing_rule,
                    fallback_reason="redaction_unsafe",
                    token_budget=route.token_budget,
                    timeout_seconds=route.timeout_seconds,
                    result="blocked",
                    schema_status="not_produced",
                )
                for index, role in enumerate(_REVIEWER_ROLES)
            )
            overall: Literal["blocked", "failure", "cancelled"] = "blocked"
        else:
            result: Literal["failure", "timeout", "cancelled"] = (
                "timeout"
                if status == "timeout"
                else "cancelled"
                if status == "cancelled"
                else "failure"
            )
            schema_status: Literal["invalid", "not_produced"] = (
                "invalid" if status == "invalid_json" else "not_produced"
            )
            steps = tuple(
                ExecutionStep(
                    sequence=index,
                    planned_role=role,
                    actual_role=role,
                    model_ref=actual_model_ref or route.model_ref,
                    provider=actual_provider or route.provider,
                    tool_grants=(),
                    routing_rule=route.routing_rule,
                    fallback_reason=None,
                    token_budget=route.token_budget,
                    timeout_seconds=route.timeout_seconds,
                    result=result,
                    schema_status=schema_status,
                )
                for index, role in enumerate(_REVIEWER_ROLES)
            )
            overall = "cancelled" if result == "cancelled" else "failure"
        placeholder = ExecutionReceipt(
            schema_version="v1",
            receipt_id="exr_" + "0" * 32,
            run_id=run_id,
            subject_digest=subject_digest,
            steps=steps,
            overall_result=overall,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            started_at=started_at,
            completed_at=completed_at,
        )
        data = placeholder.model_dump(mode="json")
        data.pop("receipt_id")
        receipt_id = "exr_" + hashlib.sha256(_canonical_bytes(data)).hexdigest()[:32]
        return placeholder.model_copy(update={"receipt_id": receipt_id})

    def _final_fence(
        self,
        intent: AssuranceRunIntent,
        task_digest: str,
        subject_digest: str,
        initial_git: GitSnapshotResult,
        initial_intake: IntakeResult,
        manifest: EvidenceManifestResult,
        collected_at: datetime,
    ) -> datetime:
        try:
            final_git = self._git_collector.collect(
                intent.repository_path,
                repository_identity=intent.repository_identity,
                base_ref=intent.base_ref,
                task_digest=task_digest,
                policy_version=self._config.policy_version,
                rubric_version=self._config.rubric_version,
                artifact_store=self._artifact_store,
                attachment_digests=(),
                collected_at=collected_at,
            )
            final_intake = self._intake_collector.collect(
                intent.repository_path,
                subject_digest=subject_digest,
                artifact_store=self._artifact_store,
                task_path=intent.task_path,
                policy_paths=intent.policy_paths,
                adr_paths=intent.adr_paths,
                runbook_paths=intent.runbook_paths,
                collected_at=collected_at,
            )
        except Exception as exc:
            raise AssuranceRunStaleError("final source freshness fence failed") from exc
        if type(final_git) is not GitSnapshotResult or type(final_intake) is not IntakeResult:
            raise AssuranceRunStaleError("final fence collector returned invalid result")
        if _strip_datetimes(_jsonable(final_git)) != _strip_datetimes(_jsonable(initial_git)):
            raise AssuranceRunStaleError("Git changed during reviewer execution")
        if _strip_datetimes(_jsonable(final_intake)) != _strip_datetimes(_jsonable(initial_intake)):
            raise AssuranceRunStaleError("intake documents changed during reviewer execution")
        for entry in manifest.manifest.entries:
            try:
                if self._artifact_store.verify(entry.artifact_digest) is not True:
                    raise AssuranceRunStaleError("manifest artifact is missing")
            except AssuranceRunStaleError:
                raise
            except Exception as exc:
                raise AssuranceRunStaleError("manifest artifact verification failed") from exc
        try:
            if self._artifact_store.verify(manifest.manifest.artifact_digest) is not True:
                raise AssuranceRunStaleError("manifest artifact is missing")
        except AssuranceRunStaleError:
            raise
        except Exception as exc:
            raise AssuranceRunStaleError("manifest artifact verification failed") from exc
        fence_at = self._now()
        for entry in manifest.manifest.entries:
            if entry.fresh_until is None or fence_at > entry.fresh_until:
                raise AssuranceRunStaleError("evidence freshness TTL expired")
        return fence_at

    @staticmethod
    def _build_case_and_events(
        *,
        draft_case: AcceptanceCase,
        subject_digest: str,
        evidence: tuple[Evidence, ...],
        findings: tuple[Finding, ...],
        questions: tuple[ReviewQuestion, ...],
        receipt: ExecutionReceipt,
        policy: PolicyGateResult,
        occurred_at: datetime,
    ) -> tuple[AcceptanceCase, tuple[AcceptanceEvent, ...]]:
        collect_event = AcceptanceEvent(
            schema_version="v1",
            event_id="evt_collect_"
            + hashlib.sha256(
                (subject_digest + receipt.receipt_id).encode("ascii")
            ).hexdigest()[:32],
            subject_digest=subject_digest,
            kind="COLLECT_EVIDENCE",
            evidence_refs=tuple(item.evidence_id for item in evidence),
            finding_refs=tuple(item.finding_id for item in findings),
            execution_receipt_refs=(receipt.receipt_id,),
            policy_decision_refs=(policy.decision.decision_id,),
            occurred_at=occurred_at,
        )
        state = AcceptanceMachineState(
            schema_version="v1",
            case=draft_case,
            applied_events=(),
        )
        state = apply_acceptance_event(state, collect_event)
        events = [collect_event]
        if questions:
            request_event = AcceptanceEvent(
                schema_version="v1",
                event_id="evt_questions_"
                + hashlib.sha256(
                    (subject_digest + "|" + "|".join(item.question_id for item in questions)).encode("ascii")
                ).hexdigest()[:32],
                subject_digest=subject_digest,
                kind="REQUEST_EVIDENCE",
                missing_evidence=tuple(
                    sorted("review_question:" + item.question_id for item in questions)
                ),
                occurred_at=occurred_at,
            )
            state = apply_acceptance_event(state, request_event)
            events.append(request_event)
        return state.case, tuple(events)

    @staticmethod
    def _run_id(request_digest: str, idempotency_key: str) -> str:
        return "run_" + hashlib.sha256(
            (request_digest + "|" + idempotency_key).encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _case_id(subject_digest: str) -> str:
        return "case_" + hashlib.sha256(subject_digest.encode("ascii")).hexdigest()[:32]

    @staticmethod
    def _coerce_result(
        value: Any, request_digest: str, *, cached: bool | None
    ) -> AssuranceRunResult | None:
        if value is None:
            return None
        if type(value) is AssuranceRunResult:
            if value.request_digest != request_digest:
                raise IdempotencyConflictError("idempotency key is bound to another request digest")
            if cached is True:
                return value.model_copy(update={"cached": True})
            return value
        if type(value) is AssuranceRunBundle:
            if value.request_digest != request_digest:
                raise IdempotencyConflictError("idempotency key is bound to another request digest")
            return AssuranceRunResult(
                run_id=value.run_id,
                request_digest=request_digest,
                cached=False if cached is None else cached,
                bundle=value,
            )
        raise AssuranceRunError("committer returned an invalid result")


__all__ = [
    "AssuranceRunBundle",
    "AssuranceRunConfig",
    "AssuranceRunIntent",
    "AssuranceRunResult",
    "AssuranceRunService",
    "ReviewerContextBuilder",
    "ReviewerContextPlan",
    "ReviewerContextPlanEntry",
    "ReviewerInvocationResponse",
    "ReviewerInvoker",
    "ReviewerRoute",
    "RunCommitter",
]
