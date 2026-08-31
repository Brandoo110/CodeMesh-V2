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
import io
import inspect
import json
import os
import stat
import subprocess
import zipfile
from types import MappingProxyType
from urllib.parse import urlsplit
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
    StrictFloat,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

from .artifacts import ArtifactStore
from .api_contract import ApiContractCollector, ApiContractResult
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
from .digests import (
    AcceptanceScopeDigestInput,
    SubjectDigestInput,
    compute_acceptance_scope_digest,
    compute_subject_digest,
    normalize_repo_path,
    normalize_repository_identity,
)
from .intake import IntakeResult, TaskPolicyCollector
from .manifest import (
    EvidenceManifestBuilder,
    EvidenceManifestInput,
    EvidenceManifestResult,
)
from .official_evidence import (
    OFFICIAL_EVIDENCE_KINDS,
    OFFICIAL_EVIDENCE_REASON_CODES,
    OfficialEvidenceImport,
    OfficialEvidenceSource,
    OfficialEvidenceError,
    OfficialEvidenceImporter,
    parse_official_evidence_receipt,
    parse_official_evidence_report,
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
from .snapshot import (
    GitSnapshotCollector,
    GitSnapshotResult,
    _DEFAULT_MAX_DIFF_BYTES,
)
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
SUPPORTED_COLLECTOR_EVIDENCE_KINDS = (
    "git_snapshot",
    "intake_documents",
    "command_batch",
    "evidence_manifest",
    "api_contract",
    "dependency_audit",
    "ci_iac_validation",
)
MAX_SUPPORTED_EVIDENCE = len(SUPPORTED_COLLECTOR_EVIDENCE_KINDS)
_REVIEWER_ROLES = ("intent", "architecture", "operability")
_SAFE_REDACTION = frozenset({"declared_redacted", "not_applicable"})
_REVIEWER_FAILURE_CODES = {
    "failure": "REVIEWER_PROVIDER_FAILURE",
    "timeout": "REVIEWER_TIMEOUT",
    "cancelled": "REVIEWER_CANCELLED",
    "budget_exceeded": "REVIEWER_BUDGET_EXCEEDED",
}
_REVIEWER_FAILURE_STAGE_CODES = MappingProxyType(
    {
        "process_launch": "REVIEWER_PROCESS_LAUNCH_FAILURE",
        "process_communication": "REVIEWER_PROCESS_COMMUNICATION_FAILURE",
        "nonzero_exit": "REVIEWER_PROCESS_NONZERO_EXIT",
        "event_stream_invalid": "REVIEWER_EVENT_STREAM_INVALID",
        "final_missing": "REVIEWER_RESPONSE_MISSING",
        "final_schema_invalid": "REVIEWER_RESPONSE_SCHEMA_INVALID",
    }
)
_REVIEWER_FAILURE_CATEGORIES = frozenset(
    {
        "auth",
        "rate_or_quota",
        "model_availability",
        "network_or_transport",
        "provider_or_server",
        "permission_or_policy",
        "unknown",
    }
)
_REVIEWER_CLASSIFIED_FAILURE_CATEGORIES = frozenset(
    _REVIEWER_FAILURE_CATEGORIES - {"unknown"}
)
_REVIEWER_FAILURE_CODE_FACTS = {
    code: ("REVIEWER_PROVIDER_FAILURE", stage)
    for stage, code in _REVIEWER_FAILURE_STAGE_CODES.items()
}
_REVIEWER_FAILURE_CODE_FACTS["REVIEWER_RESPONSE_MISSING"] = (
    "REVIEWER_RESPONSE_MISSING",
    "final_missing",
)
_REVIEWER_FAILURE_CODE_FACTS = MappingProxyType(_REVIEWER_FAILURE_CODE_FACTS)
_REVIEWER_TRANSPORT_FAILURE_CODES = frozenset(
    {"REVIEWER_PROVIDER_FAILURE", "REVIEWER_RESPONSE_MISSING"}
).union(_REVIEWER_FAILURE_CODE_FACTS)
_REVIEWER_ERROR_CODES = frozenset(
    {
        "REDACTION_UNSAFE",
        "OFFICIAL_EVIDENCE_MISSING",
        "REVIEWER_PROVIDER_FAILURE",
        "REVIEWER_TIMEOUT",
        "REVIEWER_CANCELLED",
        "REVIEWER_BUDGET_EXCEEDED",
        "REVIEWER_RESPONSE_MISSING",
        "REVIEWER_INVALID_JSON",
    }
)
_REVIEWER_STATUS_ERROR_CODES = {
    "failure": frozenset({"REVIEWER_PROVIDER_FAILURE", "REVIEWER_RESPONSE_MISSING"}),
    "timeout": frozenset({"REVIEWER_TIMEOUT"}),
    "cancelled": frozenset({"REVIEWER_CANCELLED"}),
    "budget_exceeded": frozenset({"REVIEWER_BUDGET_EXCEEDED"}),
    "blocked_redaction": frozenset({"REDACTION_UNSAFE"}),
    "blocked_evidence": frozenset({"OFFICIAL_EVIDENCE_MISSING"}),
    "invalid_json": frozenset({"REVIEWER_INVALID_JSON"}),
}
_DEFAULT_GIT_COLLECTOR_PROFILE = {
    "max_diff_bytes": _DEFAULT_MAX_DIFF_BYTES,
    "max_files": 500,
    "max_file_bytes": 5_000_000,
    "command_timeout_seconds": 10.0,
}
_CREDENTIAL_MARKERS = (
    "token=",
    "access_token=",
    "api_key=",
    "apikey=",
    "password=",
    "passwd=",
    "secret=",
    "private_key=",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "-----begin ",
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _api_evidence_id(
    subject_digest: str,
    head_revision: str,
    evidence: Evidence,
) -> str:
    material = "|".join(
        (
            subject_digest,
            head_revision,
            "contracts/openapi.json",
            evidence.artifact_digest,
            evidence.status,
        )
    ).encode("ascii")
    return "ev_api_contract_" + hashlib.sha256(material).hexdigest()[:32]


def _api_contract_required(snapshot: Any) -> bool:
    """Mirror the risk module's public-API path signal without I/O."""

    for change in snapshot.changes:
        segments = change.path.lower().split("/")
        if (
            any(segment in {"api", "routes", "openapi"} for segment in segments)
            or segments[-1] in {"openapi.json", "openapi.yaml", "openapi.yml"}
        ):
            return True
    return False


def _latency_ms(started: datetime, completed: datetime) -> int:
    delta = completed - started
    micros = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return micros // 1000


def _reviewer_failure_facts(
    error_code: str | None,
) -> tuple[str | None, str | None]:
    """Decode only the fixed transport failure grammar into durable facts."""

    return _REVIEWER_FAILURE_CODE_FACTS.get(error_code, (error_code, None))


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


class AssuranceRunPreconditionError(AssuranceRunError):
    """A required external-side-effect precondition was not satisfied."""


class AssuranceRunOfficialEvidenceError(AssuranceRunPreconditionError):
    """A supplied official GitHub run was missing, invalid, or drifted."""

    message = "official evidence precondition was not satisfied"

    def __init__(
        self,
        *_args: object,
        reason_code: object = None,
        reason: object = None,
    ) -> None:
        candidate = reason_code if reason_code is not None else reason
        if candidate is None and len(_args) == 1:
            candidate = _args[0]
        if type(candidate) is not str or candidate not in OFFICIAL_EVIDENCE_REASON_CODES:
            candidate = "unknown"
        self.reason_code = candidate
        super().__init__()

    def __str__(self) -> str:
        return self.message

    @property
    def reason(self) -> str:
        """Compatibility view over the single allowlisted failure reason."""

        return self.reason_code


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


def _validate_repository_identity(value: str) -> str:
    """Return a canonical logical identity, refusing credential-bearing URLs."""

    try:
        normalized = normalize_repository_identity(value)
    except (TypeError, ValueError):
        raise
    lowered = normalized.lower()
    windows_drive = (
        len(normalized) >= 2
        and normalized[0].isalpha()
        and normalized[1] == ":"
    )
    local_path_shape = (
        normalized.startswith(("/", "~", "./", "../"))
        or normalized in {".", ".."}
        or windows_drive
        or lowered.startswith("file:")
    )
    if "\x00" in normalized or "?" in normalized or "#" in normalized:
        raise ValueError("repository_identity must not contain NUL, query, or fragment")
    if local_path_shape:
        raise ValueError("repository_identity must be a logical identity")
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        raise ValueError("repository_identity must not contain credential-like material")
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("repository_identity must not contain URL userinfo")
    # ``urlsplit`` only exposes userinfo for URL-shaped values; reject a raw
    # at-sign as well so logical identities cannot smuggle an ambiguous URL.
    if "@" in normalized:
        raise ValueError("repository_identity must not contain URL userinfo")
    return normalized


class GitCollectorProfile(BaseModel):
    """The immutable limits actually used by the Git snapshot collector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    max_diff_bytes: StrictInt = Field(gt=0)
    max_files: StrictInt = Field(gt=0)
    max_file_bytes: StrictInt = Field(gt=0)
    command_timeout_seconds: StrictFloat = Field(gt=0)

    @field_validator("command_timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("command_timeout_seconds must be finite")
        return value


class FreshnessSourceBinding(BaseModel):
    """Local-only source facts for a committed run.

    This model is deliberately stored in a separate SQLite column.  Its
    repository path is useful for local freshness audits but must never cross
    the public bundle/projection boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    repository_path: Path
    repository_identity: str = Field(min_length=1)
    requested_base_ref: str = Field(min_length=1)
    resolved_base_revision: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    policy_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    runbook_paths: tuple[str, ...] = ()
    policy_version: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    attachment_digests: tuple[str, ...] = ()
    git_collector_profile: GitCollectorProfile
    subject: ChangeSubject
    author: str = Field(min_length=1)
    author_provenance: Literal["caller_declared"] = "caller_declared"
    subject_identity_version: Literal["v1", "v2"] = "v1"

    @field_validator("repository_path")
    @classmethod
    def _absolute_repository_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository_path must be absolute")
        return value

    @field_validator("repository_identity")
    @classmethod
    def _safe_identity(cls, value: str) -> str:
        return _validate_repository_identity(value)

    @field_validator("requested_base_ref")
    @classmethod
    def _base_ref(cls, value: str) -> str:
        if value.startswith("-") or any(char.isspace() for char in value) or "\x00" in value:
            raise ValueError("requested_base_ref contains forbidden characters")
        return value

    @field_validator("resolved_base_revision")
    @classmethod
    def _full_revision(cls, value: str) -> str:
        if len(value) not in (40, 64) or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("resolved_base_revision must be a full lowercase SHA")
        return value

    @field_validator("task_path", "policy_paths", "adr_paths", "runbook_paths", mode="before")
    @classmethod
    def _paths(cls, value: object, info) -> object:
        if info.field_name == "task_path":
            if type(value) is not str:
                raise ValueError("task_path must be a string")
            normalized = normalize_repo_path(value)
            if normalized != value or not value.endswith(".md"):
                raise ValueError("task_path must be a canonical .md path")
            return value
        if type(value) not in (tuple, list):
            raise ValueError(f"{info.field_name} must be a tuple or list")
        result = []
        seen = set()
        for item in value:
            if type(item) is not str:
                raise ValueError(f"{info.field_name} must contain strings")
            normalized = normalize_repo_path(item)
            if normalized != item or not item.endswith(".md"):
                raise ValueError(f"{info.field_name} must contain canonical .md paths")
            if item in seen:
                raise ValueError(f"{info.field_name} must be unique")
            seen.add(item)
            result.append(item)
        return tuple(result)

    @field_validator("attachment_digests", mode="before")
    @classmethod
    def _attachments(cls, value: object) -> tuple[str, ...]:
        if type(value) not in (tuple, list):
            raise ValueError("attachment_digests must be a tuple or list")
        result = tuple(value)
        if any(
            type(item) is not str
            or len(item) != 71
            or not item.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in item[7:])
            for item in result
        ):
            raise ValueError("attachment_digests must contain sha256 digests")
        if len(set(result)) != len(result):
            raise ValueError("attachment_digests must be unique")
        return result

    @field_validator("author")
    @classmethod
    def _author_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author must not be blank")
        return value

    @model_validator(mode="after")
    def _bind_source_facts(self) -> "FreshnessSourceBinding":
        if self.subject.repository != self.repository_identity:
            raise ValueError("source repository identity must match subject")
        if self.subject.base_revision != self.resolved_base_revision:
            raise ValueError("source base revision must match subject")
        if self.subject.policy_version != self.policy_version:
            raise ValueError("source policy version must match subject")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler) -> dict[str, object]:
        payload = handler(self)
        if self.subject_identity_version == "v1":
            payload.pop("subject_identity_version", None)
        return payload


@dataclass(frozen=True)
class _OfficialEvidenceCommitProof:
    """Verified official bytes handed only to the internal commit seam."""

    evidence_id: str
    kind: str
    subject_digest: str
    evidence_mode: Literal["official"]
    workflow_run_id: str
    workflow_run_attempt: int
    job_id: str
    artifact_id: str
    artifact_digest: str
    artifact_byte_size: int
    artifact_bytes: bytes
    receipt_digest: str
    receipt_byte_size: int
    receipt_bytes: bytes
    report_digest: str
    report_byte_size: int
    report_bytes: bytes
    result_digest: str
    result_byte_size: int
    result_bytes: bytes
    source_bindings: tuple[tuple[OfficialEvidenceSource, bytes], ...]


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
    author: str = Field(min_length=1)
    author_provenance: Literal["caller_declared"] = "caller_declared"
    base_ref: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    policy_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    runbook_paths: tuple[str, ...] = ()
    command_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_COMMANDS)
    official_evidence_run_id: str | None = None
    changed_lines_total: StrictInt | None = Field(default=None, ge=0)
    external_side_effects: Literal["none_declared", "present_declared", "unknown"] = (
        "unknown"
    )
    provider_boundary: Literal[
        "within_declared_boundary", "crosses_declared_boundary", "unknown"
    ] = "unknown"

    @field_validator("repository_identity")
    @classmethod
    def _repository_identity(cls, value: str) -> str:
        return _validate_repository_identity(value)

    @field_validator("author")
    @classmethod
    def _author(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author must not be blank")
        return value

    @field_validator("base_ref")
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

    @field_validator("official_evidence_run_id", mode="before")
    @classmethod
    def _official_evidence_run_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("official_evidence_run_id must be a positive numeric string")
        if value == "0" or value.startswith("0") or len(value) > 19:
            raise ValueError("official_evidence_run_id must be a positive numeric string")
        return value


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
    """A one-to-one redaction assessment for every collected Evidence item."""

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
    schema_status: Literal[
        "valid", "repaired", "unverified", "invalid", "not_produced"
    ] = "not_produced"
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)
    failure_category: Literal[
        "auth",
        "rate_or_quota",
        "model_availability",
        "network_or_transport",
        "provider_or_server",
        "permission_or_policy",
        "unknown",
    ] | None = None

    @field_validator("raw_response", mode="before")
    @classmethod
    def _raw_bytes(cls, value: object) -> object:
        if value is None or type(value) is bytes:
            return value
        raise ValueError("raw_response must be exact bytes or None")

    @model_validator(mode="after")
    def _success_facts(self) -> "ReviewerInvocationResponse":
        expected_error_codes = {
            "failure": _REVIEWER_TRANSPORT_FAILURE_CODES,
            "timeout": {"REVIEWER_TIMEOUT"},
            "cancelled": {"REVIEWER_CANCELLED"},
            "budget_exceeded": {"REVIEWER_BUDGET_EXCEEDED"},
        }
        if self.status == "success":
            if self.schema_status != "unverified":
                raise ValueError("transport success must be unverified")
            if self.raw_response is None or not self.raw_response:
                raise ValueError("transport success requires nonempty raw_response")
            if (
                self.error_code is not None
                or self.error_message is not None
                or self.failure_category is not None
            ):
                raise ValueError("transport success must not carry error facts")
        else:
            if self.raw_response is not None:
                raise ValueError("failed response must not carry raw_response")
            if self.schema_status != "not_produced":
                raise ValueError("failed response must be not_produced")
            if self.error_code not in expected_error_codes[self.status]:
                raise ValueError("reviewer status and error_code do not match")
            if self.error_message is not None:
                raise ValueError("reviewer error_message is not a domain fact")
            if self.failure_category is not None:
                if self.status != "failure":
                    raise ValueError("failure category is only valid for nonzero failure")
                if self.error_code not in {
                    "REVIEWER_PROVIDER_FAILURE",
                    _REVIEWER_FAILURE_STAGE_CODES["nonzero_exit"],
                }:
                    raise ValueError("failure category requires a nonzero-exit code")
                if self.failure_category not in _REVIEWER_FAILURE_CATEGORIES:
                    raise ValueError("failure category is not a stable enum value")
        usage_values = (self.input_tokens, self.output_tokens, self.cost_usd)
        if self.usage_status == "measured" and any(
            value is None for value in usage_values
        ):
            raise ValueError("measured usage requires token and cost facts")
        if self.usage_status == "unavailable" and any(
            value is not None for value in usage_values
        ):
            raise ValueError("unavailable usage must not carry numeric facts")
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
        "blocked_evidence",
        "invalid_json",
    ]
    planned_route: ReviewerRoute
    rubric_version: str = Field(min_length=1)
    prompt_id: str | None = Field(default=None, pattern=r"^srp_[0-9a-f]{32}$")
    prompt_digest: str | None = Field(default=None, pattern=_SHA256_RE)
    actual_provider: str | None = Field(default=None, min_length=1)
    actual_model_ref: str | None = Field(default=None, min_length=1)
    schema_status: Literal["valid", "repaired", "invalid", "not_produced"]
    raw_response_artifact_digest: str | None = Field(
        default=None, pattern=_SHA256_RE
    )
    canonical_response_digest: str | None = Field(
        default=None, pattern=_SHA256_RE
    )
    result_id: str | None = Field(default=None, pattern=r"^srr_[0-9a-f]{32}$")
    result_digest: str | None = Field(default=None, pattern=_SHA256_RE)
    usage_status: Literal["measured", "unavailable"] = "unavailable"
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    error_code: Literal[
        "REDACTION_UNSAFE",
        "OFFICIAL_EVIDENCE_MISSING",
        "REVIEWER_PROVIDER_FAILURE",
        "REVIEWER_TIMEOUT",
        "REVIEWER_CANCELLED",
        "REVIEWER_BUDGET_EXCEEDED",
        "REVIEWER_RESPONSE_MISSING",
        "REVIEWER_INVALID_JSON",
    ] | None = None

    @model_validator(mode="after")
    def _state_and_usage_facts(self) -> "ReviewerRunRecord":
        if (self.prompt_id is None) != (self.prompt_digest is None):
            raise ValueError("reviewer prompt references must be provided together")
        if (self.actual_provider is None) != (self.actual_model_ref is None):
            raise ValueError("reviewer actual provider/model must be provided together")
        if self.actual_provider is not None and not self.actual_provider.strip():
            raise ValueError("reviewer actual provider must be nonblank")
        if self.actual_model_ref is not None and not self.actual_model_ref.strip():
            raise ValueError("reviewer actual model must be nonblank")
        if self.error_code is not None and self.error_code not in _REVIEWER_ERROR_CODES:
            raise ValueError("reviewer error_code is not a stable enum value")
        if self.status == "success":
            if self.error_code is not None:
                raise ValueError("successful reviewer must not carry an error code")
            expected_error_codes = ()
        else:
            expected_error_codes = _REVIEWER_STATUS_ERROR_CODES[self.status]
            if self.error_code not in expected_error_codes:
                raise ValueError("reviewer status and error_code do not match")

        if self.status == "success":
            success_refs = (
                self.prompt_id,
                self.prompt_digest,
                self.actual_provider,
                self.actual_model_ref,
                self.raw_response_artifact_digest,
                self.canonical_response_digest,
                self.result_id,
                self.result_digest,
            )
            if any(value is None for value in success_refs):
                raise ValueError("successful reviewer must contain complete result facts")
            if self.schema_status not in ("valid", "repaired"):
                raise ValueError("successful reviewer must have a valid schema")
        elif self.status == "invalid_json":
            if any(
                value is None
                for value in (
                    self.prompt_id,
                    self.prompt_digest,
                    self.actual_provider,
                    self.actual_model_ref,
                    self.raw_response_artifact_digest,
                )
            ):
                raise ValueError("invalid_json reviewer must retain prompt, route, and raw facts")
            if self.schema_status != "invalid":
                raise ValueError("invalid_json reviewer must have an invalid schema")
            if any(
                value is not None
                for value in (
                    self.canonical_response_digest,
                    self.result_id,
                    self.result_digest,
                )
            ):
                raise ValueError("invalid_json reviewer must not carry canonical or result facts")
        else:
            if self.status in {"blocked_redaction", "blocked_evidence"}:
                expected_facts = (
                    self.prompt_id,
                    self.prompt_digest,
                    self.actual_provider,
                    self.actual_model_ref,
                    self.raw_response_artifact_digest,
                    self.canonical_response_digest,
                    self.result_id,
                    self.result_digest,
                )
                if any(value is not None for value in expected_facts):
                    raise ValueError("redaction-blocked reviewer must not carry prompt, route, or result facts")
                if self.usage_status != "unavailable" or any(
                    value is not None
                    for value in (self.input_tokens, self.output_tokens, self.cost_usd)
                ):
                    raise ValueError("redaction-blocked reviewer usage must be unavailable")
            elif any(
                value is None
                for value in (
                    self.prompt_id,
                    self.prompt_digest,
                    self.actual_provider,
                    self.actual_model_ref,
                )
            ):
                raise ValueError("failed reviewer must retain prompt and route facts")
            if any(
                value is not None
                for value in (
                    self.raw_response_artifact_digest,
                    self.canonical_response_digest,
                    self.result_id,
                    self.result_digest,
                )
            ):
                raise ValueError("failed reviewer must not carry success result facts")
            if self.schema_status != "not_produced":
                raise ValueError("failed reviewer must not claim a produced schema")
            if self.status in {"blocked_redaction", "blocked_evidence"}:
                if self.schema_status != "not_produced":
                    raise ValueError("blocked reviewer must not claim a schema")
        if self.usage_status == "measured" and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.cost_usd is None
        ):
            raise ValueError("measured usage requires token and cost facts")
        if self.usage_status == "unavailable" and any(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.cost_usd)
        ):
            raise ValueError("unavailable usage must not carry numeric facts")
        return self


def _validate_reviewer_receipt_binding(
    reviewer: ReviewerRunRecord, receipt: ExecutionReceipt
) -> None:
    """Require the auditable reviewer record and receipt to describe one call."""

    expected_results = {
        "success": ("success", "success"),
        "failure": ("failure", "failure"),
        "timeout": ("timeout", "failure"),
        "cancelled": ("cancelled", "cancelled"),
        "budget_exceeded": ("failure", "failure"),
        "blocked_redaction": ("blocked", "blocked"),
        "blocked_evidence": ("blocked", "blocked"),
        "invalid_json": ("failure", "failure"),
    }
    expected_step_result, expected_overall = expected_results[reviewer.status]
    if len(receipt.steps) != len(_REVIEWER_ROLES):
        raise ValueError("reviewer receipt must contain one step per reviewer role")
    if tuple(step.planned_role for step in receipt.steps) != _REVIEWER_ROLES:
        raise ValueError("reviewer receipt roles must use the configured order")
    route = reviewer.planned_route
    for step in receipt.steps:
        if (
            step.routing_rule != route.routing_rule
            or step.timeout_seconds != route.timeout_seconds
            or step.token_budget != route.token_budget
            or step.tool_grants != route.tool_grants
        ):
            raise ValueError("reviewer receipt route facts do not match planned route")
        if step.result != expected_step_result:
            raise ValueError("reviewer status does not match receipt step result")
        if step.schema_status != reviewer.schema_status:
            raise ValueError("reviewer schema status does not match receipt")
        if reviewer.status in {"blocked_redaction", "blocked_evidence"}:
            if (
                step.actual_role is not None
                or step.model_ref is not None
                or step.provider is not None
            ):
                raise ValueError("redaction-blocked receipt must not expose actual route")
        elif (
            step.actual_role != step.planned_role
            or step.model_ref != reviewer.actual_model_ref
            or step.provider != reviewer.actual_provider
        ):
            raise ValueError("reviewer receipt actual route does not match record")
    if receipt.overall_result != expected_overall:
        raise ValueError("reviewer status does not match receipt overall result")
    if reviewer.usage_status == "measured":
        if (
            receipt.input_tokens != reviewer.input_tokens
            or receipt.output_tokens != reviewer.output_tokens
            or receipt.cost_usd != reviewer.cost_usd
        ):
            raise ValueError("measured reviewer usage does not match receipt")
    elif (
        receipt.input_tokens != 0
        or receipt.output_tokens != 0
        or receipt.cost_usd != 0.0
    ):
        raise ValueError("unavailable reviewer usage must remain zero in receipt")


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
    evidence: tuple[Evidence, ...] = Field(
        min_length=4, max_length=MAX_SUPPORTED_EVIDENCE
    )
    findings: tuple[Finding, ...] = ()
    questions: tuple[ReviewQuestion, ...] = ()
    reviewer: ReviewerRunRecord
    execution_receipt: ExecutionReceipt
    policy: PolicyGateResult
    events: tuple[AcceptanceEvent, ...] = Field(min_length=1)
    freshness_source_binding_digest: str = Field(pattern=_SHA256_RE)
    freshness_source_binding: FreshnessSourceBinding = Field(
        exclude=True, repr=False
    )
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
        if self.evidence[:4] != expected:
            raise ValueError("bundle evidence must use the fixed collector order")
        tail_evidence = self.evidence[4:]
        api_evidence = tuple(
            item for item in tail_evidence if item.kind == "api_contract"
        )
        official_evidence = tuple(
            item for item in tail_evidence if item.kind in OFFICIAL_EVIDENCE_KINDS
        )
        if len(api_evidence) > 1:
            raise ValueError("bundle api_contract Evidence must be unique")
        if any(
            item.producer != "collector.api_contract"
            or item.trace_id is not None
            or item.status not in {"success", "truncated"}
            or item.trust_level != "deterministic"
            or item.subject_digest != subject_digest
            or item.source_ref != "api_contract:contracts/openapi.json"
            or item.evidence_id
            != _api_evidence_id(subject_digest, self.git.snapshot.head_revision, item)
            for item in api_evidence
        ):
            raise ValueError("bundle api_contract Evidence is invalid")
        if len(official_evidence) > len(OFFICIAL_EVIDENCE_KINDS):
            raise ValueError("bundle contains too many official Evidence items")
        if any(
            item.producer != f"collector.{item.kind}"
            or item.status != "success"
            or item.trust_level != "observed"
            or item.subject_digest != subject_digest
            for item in official_evidence
        ):
            raise ValueError("bundle official Evidence is invalid")
        if len({item.kind for item in official_evidence}) != len(official_evidence):
            raise ValueError("bundle official Evidence kinds must be unique")
        if len(api_evidence) + len(official_evidence) != len(tail_evidence):
            raise ValueError("bundle Evidence contains an unsupported collector kind")
        expected_tail = api_evidence + official_evidence
        if tail_evidence != expected_tail:
            raise ValueError("bundle Evidence must use the fixed collector order")
        bundle_evidence_ids = tuple(item.evidence_id for item in self.evidence)
        manifest_entry_ids = tuple(
            item.evidence_id for item in self.manifest.manifest.entries
        )
        if len(bundle_evidence_ids) != len(set(bundle_evidence_ids)):
            raise ValueError("bundle Evidence evidence_id values must be unique")
        if len(manifest_entry_ids) != len(set(manifest_entry_ids)):
            raise ValueError("manifest Evidence evidence_id values must be unique")
        expected_manifest_evidence_ids = {
            item.evidence_id
            for item in self.evidence
            if item != self.manifest.evidence
        }
        if {
            item.evidence_id for item in self.manifest.manifest.entries
        } != expected_manifest_evidence_ids:
            raise ValueError("manifest must cover every bundle Evidence")
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
        if self.freshness_source_binding.subject != self.subject:
            raise ValueError("freshness source must bind to bundle subject")
        if self.freshness_source_binding.repository_identity != self.subject.repository:
            raise ValueError("freshness source repository must bind to subject")
        if self.freshness_source_binding.author_provenance != "caller_declared":
            raise ValueError("author provenance must remain caller_declared")
        expected_source_digest = _sha256(
            _canonical_bytes(self.freshness_source_binding.model_dump(mode="json"))
        )
        if self.freshness_source_binding_digest != expected_source_digest:
            raise ValueError("freshness source binding digest does not match source")
        if self.reviewer.status == "success":
            if self.reviewer.result_id is None or self.reviewer.result_digest is None:
                raise ValueError("successful reviewer must bind SingleReviewerResult")
        elif self.reviewer.result_id is not None or self.reviewer.result_digest is not None:
            raise ValueError("failed reviewer must not bind a success result")
        if self.reviewer.status in {"blocked_redaction", "blocked_evidence"}:
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
        _validate_reviewer_receipt_binding(self.reviewer, self.execution_receipt)
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
        git_snapshot: Any | None = None,
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
        official_proofs: tuple[_OfficialEvidenceCommitProof, ...] = (),
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
        api_contract_collector: Any | None = None,
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
        self._api_contract_collector = (
            api_contract_collector or ApiContractCollector()
        )
        self._command_collector = DeterministicCommandCollector(
            config.allowed_commands
        )
        self._allowed_command_ids = frozenset(
            item.command_id for item in config.allowed_commands
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def prepare(
        self, intent: AssuranceRunIntent, *, idempotency_key: str
    ) -> AssuranceRunBundle:
        """Prepare one complete Golden Path bundle without persistence."""

        self._validate_intent(intent, idempotency_key)
        request_digest = self._request_digest(intent)
        return await self._prepare_bundle(
            intent,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

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

        prepared = await self._prepare_bundle(
            intent,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            with_proofs=True,
        )
        bundle, official_proofs = prepared
        commit_kwargs = {
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
        }
        if official_proofs:
            commit_kwargs["official_proofs"] = official_proofs
        committed = await asyncio.to_thread(
            self._committer.commit,
            bundle,
            **commit_kwargs,
        )
        if committed is None:
            raise AssuranceRunError("committer.commit did not return a persisted result")
        result = self._coerce_result(committed, request_digest, cached=None)
        if result is None:
            raise AssuranceRunError("committer.commit did not return a persisted result")
        return result

    async def _prepare_bundle(
        self,
        intent: AssuranceRunIntent,
        *,
        idempotency_key: str,
        request_digest: str,
        with_proofs: bool = False,
    ) -> AssuranceRunBundle | tuple[AssuranceRunBundle, tuple[_OfficialEvidenceCommitProof, ...]]:
        """Build the complete in-memory bundle shared by prepare and run."""

        await asyncio.to_thread(self._validate_workspace, intent)
        started_at = self._now()
        task_digest = await asyncio.to_thread(
            self._intake_collector.probe_task_digest,
            intent.repository_path,
            task_path=intent.task_path,
        )
        if type(task_digest) is not str or not task_digest.startswith("sha256:"):
            raise AssuranceRunValidationError("probe did not return a sha256 task digest")

        acceptance_scope_digest = compute_acceptance_scope_digest(
            AcceptanceScopeDigestInput(
                task_path=intent.task_path,
                policy_paths=intent.policy_paths,
                adr_paths=intent.adr_paths,
                runbook_paths=intent.runbook_paths,
            )
        )

        try:
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
                acceptance_scope_digest=acceptance_scope_digest,
                collected_at=started_at,
            )
        except TypeError as exc:
            raise AssuranceRunStaleError(
                "initial Git collector cannot bind acceptance scope"
            ) from exc
        subject_digest = self._verify_scoped_git_result(
            git_result,
            task_digest=task_digest,
            policy_version=self._config.policy_version,
            rubric_version=self._config.rubric_version,
            attachment_digests=(),
            acceptance_scope_digest=acceptance_scope_digest,
            phase="initial",
        )
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

        base_evidences = (
            git_result.evidence,
            intake_result.evidence,
            command_result.evidence,
        )
        api_result: ApiContractResult | None = None
        if _api_contract_required(git_result.snapshot):
            api_result = await asyncio.to_thread(
                self._api_contract_collector.collect,
                intent.repository_path,
                subject_digest=subject_digest,
                head_revision=git_result.snapshot.head_revision,
                artifact_store=self._artifact_store,
                collected_at=started_at,
            )
            self._require_type(api_result, ApiContractResult, "api contract collector result")
            self._require_subjects(subject_digest, api_result)
            # A missing fixed source has no Evidence to bind into the
            # manifest.  Keep the absence explicit to the policy gate rather
            # than fabricating a successful or truncated collector entry.
            if api_result.snapshot.omissions == ("source_missing",):
                api_result = None
        collector_evidences = base_evidences + (
            (api_result.evidence,) if api_result is not None else ()
        )
        official_imports = await asyncio.to_thread(
            self._import_official_evidence,
            intent,
            repository_identity=git_result.snapshot.repository,
            head_revision=git_result.snapshot.head_revision,
            subject_digest=subject_digest,
            collected_at=started_at,
        )
        official_evidences = tuple(item.evidence for item in official_imports)
        all_evidences = collector_evidences + official_evidences
        context_plan = await asyncio.to_thread(
            self._prepare_context,
            all_evidences,
            subject_digest,
            git_result.snapshot,
        )
        manifest_result = await asyncio.to_thread(
            self._build_manifest,
            all_evidences,
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

        run_id = self._run_id(request_digest, idempotency_key)
        required_gaps = self._required_collector_gaps(
            risk_result,
            base_evidences=base_evidences,
            manifest_evidence=manifest_result.evidence,
            api_evidence=api_result.evidence if api_result is not None else None,
            official_evidences=official_evidences,
        )
        redaction_unsafe = any(
            item.disposition == RedactionDisposition.CONTAINS_UNREDACTED_CONTENT
            for item in context_plan.entries
        )
        if redaction_unsafe:
            reviewer_record, findings, questions, receipt = await self._review(
                subject=subject,
                risk_result=risk_result,
                context_plan=context_plan,
                run_id=run_id,
                evaluated_at=self._now(),
            )
        elif required_gaps:
            blocked_reason = (
                "api_contract_missing"
                if required_gaps == ("api_contract",)
                else "official_evidence_missing"
            )
            reviewer_record, findings, questions, receipt = (
                self._blocked_evidence_review(
                    subject_digest=subject.subject_digest,
                    run_id=run_id,
                    evaluated_at=self._now(),
                    fallback_reason=blocked_reason,
                )
            )
        else:
            reviewer_record, findings, questions, receipt = await self._review(
                subject=subject,
                risk_result=risk_result,
                context_plan=context_plan,
                run_id=run_id,
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
            official_imports,
            api_result=api_result,
            acceptance_scope_digest=acceptance_scope_digest,
        )
        freshness_source_binding = await asyncio.to_thread(
            self._build_freshness_source_binding,
            intent,
            git_result,
            subject,
            subject_identity_version="v2",
        )

        case, events = self._build_case_and_events(
            draft_case=draft_case,
            subject_digest=subject_digest,
            evidence=(
                base_evidences
                + (manifest_result.evidence,)
                + ((api_result.evidence,) if api_result is not None else ())
                + official_evidences
            ),
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
            evidence=(
                base_evidences
                + (manifest_result.evidence,)
                + ((api_result.evidence,) if api_result is not None else ())
                + official_evidences
            ),
            findings=findings,
            questions=questions,
            reviewer=reviewer_record,
            execution_receipt=receipt,
            policy=policy_result,
            events=events,
            freshness_source_binding_digest=_sha256(
                _canonical_bytes(freshness_source_binding.model_dump(mode="json"))
            ),
            freshness_source_binding=freshness_source_binding,
            started_at=started_at,
            completed_at=fence_at,
        )
        official_proofs = self._build_official_commit_proofs(
            official_imports,
            repository_path=intent.repository_path,
            head_revision=git_result.snapshot.head_revision,
        )
        if with_proofs:
            return bundle, official_proofs
        return bundle

    @staticmethod
    def _build_official_commit_proofs(
        official_imports: tuple[OfficialEvidenceImport, ...],
        *,
        repository_path: Path,
        head_revision: str,
    ) -> tuple[_OfficialEvidenceCommitProof, ...]:
        """Materialize only bytes already fenced by the verified importer."""

        if not official_imports:
            return ()
        proofs = []
        expected_zip_files = {
            "dependency_audit": "dependency_audit.json",
            "ci_iac_validation": "ci_iac_validation.json",
        }
        expected_result_files = {
            "dependency_audit": "dependency-audit-result.json",
            "ci_iac_validation": "ci-iac-result.json",
        }
        for imported in official_imports:
            if type(imported) is not OfficialEvidenceImport:
                raise AssuranceRunOfficialEvidenceError(
                    reason_code="unknown"
                )
            receipt = imported.receipt
            evidence = imported.evidence
            receipt_bytes = imported.receipt_bytes
            artifact_bytes = imported.remote_zip_bytes
            if (
                type(receipt_bytes) is not bytes
                or type(artifact_bytes) is not bytes
                or imported.receipt_digest != _sha256(receipt_bytes)
                or imported.receipt_byte_size != len(receipt_bytes)
                or imported.remote_zip_digest != _sha256(artifact_bytes)
                or imported.remote_zip_byte_size != len(artifact_bytes)
                or receipt.artifact_digest != imported.remote_zip_digest
                or receipt.artifact_byte_size != imported.remote_zip_byte_size
                or evidence.artifact_digest != imported.receipt_digest
                or evidence.kind != receipt.kind
                or evidence.subject_digest != receipt.subject_digest
                or receipt.evidence_mode != "official"
            ):
                raise AssuranceRunOfficialEvidenceError(
                    reason_code="digest_or_size_mismatch"
                )
            try:
                parsed_receipt = parse_official_evidence_receipt(receipt_bytes)
                if parsed_receipt != receipt:
                    raise ValueError("receipt bytes do not reparse exactly")
                with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as archive:
                    names = tuple(info.filename for info in archive.infolist())
                    if set(names) != {
                        "dependency_audit.json",
                        "ci_iac_validation.json",
                        "dependency-audit-result.json",
                        "ci-iac-result.json",
                    } or len(names) != 4:
                        raise ValueError("official artifact file set is invalid")
                    report_bytes = archive.read(expected_zip_files[receipt.kind])
                    result_bytes = archive.read(expected_result_files[receipt.kind])
                parsed_report = parse_official_evidence_report(report_bytes)
                if parsed_report != receipt.report:
                    raise ValueError("report bytes do not reparse exactly")
                parsed_result = json.loads(
                    result_bytes.decode("utf-8"),
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("invalid JSON constant")
                    ),
                )
                if not isinstance(parsed_result, (dict, list)) or parsed_result != receipt.result:
                    raise ValueError("result bytes do not bind to receipt")
            except OfficialEvidenceError as exc:
                reason_code = getattr(exc, "reason_code", "unknown")
                if reason_code not in OFFICIAL_EVIDENCE_REASON_CODES:
                    reason_code = "unknown"
                raise AssuranceRunOfficialEvidenceError(
                    reason_code=reason_code
                ) from exc
            except (OSError, ValueError, TypeError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
                raise AssuranceRunOfficialEvidenceError(
                    reason_code="artifact_structure_invalid"
                ) from exc
            if (
                _sha256(report_bytes) != receipt.report_digest
                or len(report_bytes) != receipt.report_byte_size
                or _sha256(result_bytes) != receipt.result_digest
                or len(result_bytes) != receipt.result_byte_size
            ):
                raise AssuranceRunOfficialEvidenceError(
                    reason_code="digest_or_size_mismatch"
                )
            source_bindings = []
            for source in imported.source_bindings:
                if type(source) is not OfficialEvidenceSource:
                    raise AssuranceRunOfficialEvidenceError(
                        reason_code="artifact_structure_invalid"
                    )
                try:
                    blob = subprocess.run(
                        (
                            "git",
                            "cat-file",
                            "blob",
                            f"{head_revision}:{source.path}",
                        ),
                        cwd=repository_path,
                        check=True,
                        capture_output=True,
                        timeout=5.0,
                    ).stdout
                except (OSError, subprocess.SubprocessError) as exc:
                    raise AssuranceRunOfficialEvidenceError(
                        reason_code="artifact_structure_invalid"
                    ) from exc
                if len(blob) != source.byte_size or _sha256(blob) != source.digest:
                    raise AssuranceRunOfficialEvidenceError(
                        reason_code="digest_or_size_mismatch"
                    )
                source_bindings.append((source, blob))
            if tuple(source for source, _ in source_bindings) != receipt.source_paths:
                raise AssuranceRunOfficialEvidenceError(
                    reason_code="lineage_mismatch"
                )
            proofs.append(
                _OfficialEvidenceCommitProof(
                    evidence_id=evidence.evidence_id,
                    kind=receipt.kind,
                    subject_digest=receipt.subject_digest,
                    evidence_mode=receipt.evidence_mode,
                    workflow_run_id=receipt.workflow_run_id,
                    workflow_run_attempt=receipt.workflow_run_attempt,
                    job_id=receipt.job_id,
                    artifact_id=receipt.artifact_id,
                    artifact_digest=receipt.artifact_digest,
                    artifact_byte_size=receipt.artifact_byte_size,
                    artifact_bytes=artifact_bytes,
                    receipt_digest=imported.receipt_digest,
                    receipt_byte_size=imported.receipt_byte_size,
                    receipt_bytes=receipt_bytes,
                    report_digest=receipt.report_digest,
                    report_byte_size=receipt.report_byte_size,
                    report_bytes=report_bytes,
                    result_digest=receipt.result_digest,
                    result_byte_size=receipt.result_byte_size,
                    result_bytes=result_bytes,
                    source_bindings=tuple(source_bindings),
                )
            )
        return tuple(proofs)

    def _import_official_evidence(
        self,
        intent: AssuranceRunIntent,
        *,
        repository_identity: str,
        head_revision: str,
        subject_digest: str,
        collected_at: datetime,
    ) -> tuple[OfficialEvidenceImport, ...]:
        if intent.official_evidence_run_id is None:
            return ()
        try:
            importer = OfficialEvidenceImporter(
                workspace_root=self._config.workspace_root,
                repository_path=intent.repository_path,
                repository_identity=repository_identity,
                head_revision=head_revision,
                subject_digest=subject_digest,
                artifact_store=self._artifact_store,
                collected_at=collected_at,
                github_token=os.getenv("GITHUB_TOKEN") or None,
            )
            imports = importer.import_run(intent.official_evidence_run_id)
        except OfficialEvidenceError as exc:
            try:
                reason_code = getattr(exc, "reason_code", None)
            except Exception:
                reason_code = None
            if type(reason_code) is not str or reason_code not in OFFICIAL_EVIDENCE_REASON_CODES:
                reason_code = "unknown"
            raise AssuranceRunOfficialEvidenceError(
                reason_code=reason_code
            ) from exc
        except (TypeError, ValueError) as exc:
            raise AssuranceRunOfficialEvidenceError(
                reason_code="unknown"
            ) from exc
        if type(imports) is not tuple or any(
            type(item) is not OfficialEvidenceImport for item in imports
        ):
            raise AssuranceRunOfficialEvidenceError(
                "official run did not contain the complete typed evidence set"
            )
        kinds = tuple(item.receipt.kind for item in imports)
        if set(kinds) != set(OFFICIAL_EVIDENCE_KINDS) or len(kinds) != len(set(kinds)):
            raise AssuranceRunOfficialEvidenceError(
                "official run did not contain the complete typed evidence set"
            )
        order = {kind: index for index, kind in enumerate(OFFICIAL_EVIDENCE_KINDS)}
        return tuple(sorted(imports, key=lambda item: order[item.receipt.kind]))

    def _validate_intent(self, intent: AssuranceRunIntent, idempotency_key: str) -> None:
        if type(intent) is not AssuranceRunIntent:
            raise AssuranceRunValidationError("intent must be an exact AssuranceRunIntent")
        if getattr(intent, "provider_boundary", None) != "within_declared_boundary":
            raise AssuranceRunPreconditionError(
                "provider boundary must remain within the declared boundary"
            )
        if type(idempotency_key) is not str or not idempotency_key.strip():
            raise AssuranceRunValidationError("idempotency_key must be nonblank")
        if len(idempotency_key.encode("utf-8")) > 256:
            raise AssuranceRunValidationError("idempotency_key is too long")
        run_id = intent.official_evidence_run_id
        if run_id is not None and (
            type(run_id) is not str
            or not run_id.isascii()
            or not run_id.isdecimal()
            or run_id == "0"
            or run_id.startswith("0")
            or len(run_id) > 19
        ):
            raise AssuranceRunValidationError(
                "official_evidence_run_id must be a positive numeric string"
            )
        if not intent.repository_path.is_absolute():
            raise AssuranceRunValidationError("repository_path must be absolute")
        try:
            _validate_repository_identity(intent.repository_identity)
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

    def _verify_scoped_git_result(
        self,
        git_result: Any,
        *,
        task_digest: str,
        policy_version: str,
        rubric_version: str,
        attachment_digests: tuple[str, ...],
        acceptance_scope_digest: str,
        phase: str,
    ) -> str:
        """Rebuild the canonical v2 subject before accepting a Git result."""

        if type(git_result) is not GitSnapshotResult:
            raise AssuranceRunStaleError(
                f"{phase} Git collector returned an invalid result"
            )
        build_subject_input = getattr(self._git_collector, "build_subject_input", None)
        if not callable(build_subject_input):
            try:
                canonical_collector = GitSnapshotCollector(
                    max_diff_bytes=self._git_collector.max_diff_bytes,
                    max_files=self._git_collector.max_files,
                    max_file_bytes=self._git_collector.max_file_bytes,
                    command_timeout_seconds=self._git_collector.command_timeout_seconds,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise AssuranceRunStaleError(
                    f"{phase} Git collector cannot rebuild acceptance scope; "
                    "collector profile unavailable"
                ) from exc
            build_subject_input = canonical_collector.build_subject_input
        try:
            subject_input = build_subject_input(
                git_result.snapshot,
                task_digest=task_digest,
                policy_version=policy_version,
                rubric_version=rubric_version,
                attachment_digests=attachment_digests,
                acceptance_scope_digest=acceptance_scope_digest,
            )
        except Exception as exc:
            raise AssuranceRunStaleError(
                f"{phase} Git collector did not bind acceptance scope"
            ) from exc
        if (
            type(subject_input) is not SubjectDigestInput
            or subject_input.schema_version != "v2"
            or subject_input.acceptance_scope_digest != acceptance_scope_digest
        ):
            raise AssuranceRunStaleError(
                f"{phase} Git collector did not produce a v2 scoped subject"
            )
        subject_digest = compute_subject_digest(subject_input)
        if (
            subject_digest != git_result.snapshot.subject_digest
            or git_result.evidence.subject_digest != subject_digest
        ):
            raise AssuranceRunStaleError(
                f"{phase} Git result subject is not bound to acceptance scope"
            )
        return subject_digest

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
        self,
        evidences: tuple[Evidence, ...],
        subject_digest: str,
        git_snapshot: Any | None = None,
    ) -> ReviewerContextPlan:
        try:
            value = self._context_builder.prepare(
                evidences,
                artifact_store=self._artifact_store,
                subject_digest=subject_digest,
                git_snapshot=git_snapshot,
            )
        except Exception as exc:
            raise AssuranceRunRedactionError("redaction assessment failed") from exc
        if type(value) is not ReviewerContextPlan:
            raise AssuranceRunRedactionError("redaction adapter returned an invalid plan")
        expected = {item.evidence_id: item for item in evidences}
        actual = {item.evidence_id: item for item in value.entries}
        if set(expected) != set(actual):
            raise AssuranceRunRedactionError("redaction plan must cover every Evidence")
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

    def _build_freshness_source_binding(
        self,
        intent: AssuranceRunIntent,
        git_result: GitSnapshotResult,
        subject: ChangeSubject,
        *,
        subject_identity_version: Literal["v1", "v2"] = "v1",
    ) -> FreshnessSourceBinding:
        """Capture final-fence local source facts without exposing them publicly."""

        try:
            resolved_path = intent.repository_path.resolve(strict=True)
            path_stat = resolved_path.lstat()
        except OSError as exc:
            raise AssuranceRunStaleError(
                "repository_path disappeared after final freshness fence"
            ) from exc
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise AssuranceRunStaleError(
                "repository_path must remain a real directory after final fence"
            )
        if git_result.snapshot.repository != subject.repository:
            raise AssuranceRunStaleError("source repository identity does not match subject")
        collector = self._git_collector
        try:
            profile_data = {
                name: getattr(collector, name)
                for name in _DEFAULT_GIT_COLLECTOR_PROFILE
            }
        except AttributeError as exc:
            raise AssuranceRunStaleError(
                "Git collector profile limits are unavailable"
            ) from exc
        try:
            profile = GitCollectorProfile.model_validate(profile_data)
        except Exception as exc:
            raise AssuranceRunStaleError("Git collector profile is invalid") from exc
        return FreshnessSourceBinding(
            schema_version="v1",
            repository_path=resolved_path,
            repository_identity=subject.repository,
            requested_base_ref=intent.base_ref,
            resolved_base_revision=git_result.snapshot.base_revision,
            task_path=intent.task_path,
            policy_paths=intent.policy_paths,
            adr_paths=intent.adr_paths,
            runbook_paths=intent.runbook_paths,
            policy_version=self._config.policy_version,
            rubric_version=self._config.rubric_version,
            attachment_digests=(),
            git_collector_profile=profile,
            subject=subject,
            author=intent.author,
            author_provenance=intent.author_provenance,
            subject_identity_version=subject_identity_version,
        )

    @staticmethod
    def _required_collector_gaps(
        risk_result: RiskClassificationResult,
        *,
        base_evidences: tuple[Evidence, ...],
        manifest_evidence: Evidence,
        api_evidence: Evidence | None,
        official_evidences: tuple[Evidence, ...],
    ) -> tuple[str, ...]:
        """Return required collectors that are absent or not successful.

        A truncated or failed collector is never handed to the provider as a
        successful run.  The same gate applies to local and official evidence;
        the latter remains subject to its importer-specific proof fence.
        """

        by_name: dict[str, Evidence] = {
            "git_snapshot": base_evidences[0],
            "task_policy_adr": base_evidences[1],
            "deterministic_commands": base_evidences[2],
            "evidence_manifest": manifest_evidence,
        }
        if api_evidence is not None:
            by_name["api_contract"] = api_evidence
        by_name.update({item.kind: item for item in official_evidences})
        gaps = []
        for collector in risk_result.classification.required_collectors:
            evidence = by_name.get(collector)
            if evidence is None or evidence.status != "success":
                gaps.append(collector)
        return tuple(gaps)

    def _blocked_evidence_review(
        self,
        *,
        subject_digest: str,
        run_id: str,
        evaluated_at: datetime,
        fallback_reason: str = "official_evidence_missing",
    ) -> tuple[
        ReviewerRunRecord,
        tuple[Finding, ...],
        tuple[ReviewQuestion, ...],
        ExecutionReceipt,
    ]:
        """Record missing official collectors without calling the provider."""

        route = self._config.reviewer_route
        receipt = self._failure_receipt(
            run_id=run_id,
            subject_digest=subject_digest,
            status="blocked_evidence",
            route=route,
            started_at=evaluated_at,
            completed_at=evaluated_at,
            actual_provider=None,
            actual_model_ref=None,
            fallback_reason=fallback_reason,
        )
        reviewer = ReviewerRunRecord(
            status="blocked_evidence",
            planned_route=route,
            rubric_version=self._config.rubric_version,
            schema_status="not_produced",
            usage_status="unavailable",
            error_code="OFFICIAL_EVIDENCE_MISSING",
        )
        return reviewer, (), (), receipt

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
                    usage_status="unavailable",
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
            response_error_code, failure_stage = _reviewer_failure_facts(
                response.error_code
            )
            if response_error_code not in _REVIEWER_STATUS_ERROR_CODES[response.status]:
                response_error_code = _REVIEWER_FAILURE_CODES[response.status]
                failure_stage = None
            if response.failure_category in _REVIEWER_CLASSIFIED_FAILURE_CATEGORIES:
                failure_stage = response.failure_category
            elif (
                response.failure_category == "unknown"
                and response.error_code == "REVIEWER_PROVIDER_FAILURE"
            ):
                failure_stage = "nonzero_exit"
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status=response.status,
                route=self._config.reviewer_route,
                started_at=started,
                completed_at=completed,
                actual_provider=actual_provider,
                actual_model_ref=actual_model_ref,
                input_tokens=self._usage_value(response, "input_tokens") or 0,
                output_tokens=self._usage_value(response, "output_tokens") or 0,
                cost_usd=self._usage_value(response, "cost_usd") or 0.0,
                fallback_reason=failure_stage,
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
                    usage_status=self._usage_status(response),
                    input_tokens=self._usage_value(response, "input_tokens"),
                    output_tokens=self._usage_value(response, "output_tokens"),
                    cost_usd=self._usage_value(response, "cost_usd"),
                    error_code=response_error_code,
                ),
                (),
                (),
                receipt,
            )

        raw = response.raw_response
        if type(raw) is not bytes:
            response_error_code = response.error_code
            if response_error_code not in _REVIEWER_STATUS_ERROR_CODES["failure"]:
                response_error_code = "REVIEWER_RESPONSE_MISSING"
            receipt = self._failure_receipt(
                run_id=run_id,
                subject_digest=subject.subject_digest,
                status="failure",
                route=self._config.reviewer_route,
                started_at=started,
                completed_at=completed,
                actual_provider=actual_provider,
                actual_model_ref=actual_model_ref,
                input_tokens=self._usage_value(response, "input_tokens") or 0,
                output_tokens=self._usage_value(response, "output_tokens") or 0,
                cost_usd=self._usage_value(response, "cost_usd") or 0.0,
                fallback_reason="final_missing",
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
                    usage_status=self._usage_status(response),
                    input_tokens=self._usage_value(response, "input_tokens"),
                    output_tokens=self._usage_value(response, "output_tokens"),
                    cost_usd=self._usage_value(response, "cost_usd"),
                    error_code=response_error_code,
                ),
                (),
                (),
                receipt,
            )
        try:
            invocation_schema_status = (
                "valid" if response.schema_status == "unverified" else response.schema_status
            )
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
                schema_status=invocation_schema_status,
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
            if not normalized.findings:
                receipt = self._failure_receipt(
                    run_id=run_id,
                    subject_digest=subject.subject_digest,
                    status="failure",
                    route=self._config.reviewer_route,
                    started_at=started,
                    completed_at=completed,
                    actual_provider=actual_provider,
                    actual_model_ref=actual_model_ref,
                    input_tokens=self._usage_value(response, "input_tokens") or 0,
                    output_tokens=self._usage_value(response, "output_tokens") or 0,
                    cost_usd=self._usage_value(response, "cost_usd") or 0.0,
                    fallback_reason="empty_findings",
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
                        usage_status=self._usage_status(response),
                        input_tokens=self._usage_value(response, "input_tokens"),
                        output_tokens=self._usage_value(response, "output_tokens"),
                        cost_usd=self._usage_value(response, "cost_usd"),
                        error_code="REVIEWER_PROVIDER_FAILURE",
                    ),
                    (),
                    normalized.questions,
                    receipt,
                )
            receipt = self._bind_success_receipt_route(normalized.execution_receipt)
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
                    usage_status=self._usage_status(response),
                    input_tokens=self._usage_value(response, "input_tokens"),
                    output_tokens=self._usage_value(response, "output_tokens"),
                    cost_usd=self._usage_value(response, "cost_usd"),
                ),
                normalized.findings,
                normalized.questions,
                receipt,
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
                input_tokens=self._usage_value(response, "input_tokens") or 0,
                output_tokens=self._usage_value(response, "output_tokens") or 0,
                cost_usd=self._usage_value(response, "cost_usd") or 0.0,
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
                    usage_status=self._usage_status(response),
                    input_tokens=self._usage_value(response, "input_tokens"),
                    output_tokens=self._usage_value(response, "output_tokens"),
                    cost_usd=self._usage_value(response, "cost_usd"),
                    error_code="REVIEWER_INVALID_JSON",
                ),
                (),
                (),
                receipt,
            )

    @staticmethod
    def _coerce_invocation_response(value: Any) -> ReviewerInvocationResponse:
        if type(value) is ReviewerInvocationResponse:
            return ReviewerInvocationResponse.model_validate(
                value.model_dump(mode="python")
            )
        if isinstance(value, Mapping):
            return ReviewerInvocationResponse.model_validate(dict(value))
        raise ValueError("reviewer invoker must return ReviewerInvocationResponse")

    @staticmethod
    def _usage_status(response: ReviewerInvocationResponse) -> Literal[
        "measured", "unavailable"
    ]:
        if response.usage_status == "measured" and all(
            value is not None
            for value in (response.input_tokens, response.output_tokens, response.cost_usd)
        ):
            return "measured"
        return "unavailable"

    @classmethod
    def _usage_value(
        cls, response: ReviewerInvocationResponse, name: str
    ) -> int | float | None:
        if cls._usage_status(response) != "measured":
            return None
        return getattr(response, name)

    def _bind_success_receipt_route(
        self, receipt: ExecutionReceipt
    ) -> ExecutionReceipt:
        """Bind normalized success steps to the configured immutable route."""

        route = self._config.reviewer_route
        steps = tuple(
            step.model_copy(
                update={
                    "routing_rule": route.routing_rule,
                    "token_budget": route.token_budget,
                    "timeout_seconds": route.timeout_seconds,
                    "tool_grants": route.tool_grants,
                }
            )
            for step in receipt.steps
        )
        data = receipt.model_dump(mode="json")
        data["receipt_id"] = "exr_" + "0" * 32
        data["steps"] = [step.model_dump(mode="json") for step in steps]
        rebound = ExecutionReceipt.model_validate(data)
        body = rebound.model_dump(mode="json")
        body.pop("receipt_id")
        receipt_id = "exr_" + hashlib.sha256(_canonical_bytes(body)).hexdigest()[:32]
        return rebound.model_copy(update={"receipt_id": receipt_id})

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
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        fallback_reason: str | None = None,
    ) -> ExecutionReceipt:
        if status in {"blocked_redaction", "blocked_evidence"}:
            fallback_reason = fallback_reason or (
                "redaction_unsafe"
                if status == "blocked_redaction"
                else "official_evidence_missing"
            )
            steps = tuple(
                ExecutionStep(
                    sequence=index,
                    planned_role=role,
                    actual_role=None,
                    model_ref=None,
                    provider=None,
                    tool_grants=(),
                    routing_rule=route.routing_rule,
                    fallback_reason=fallback_reason,
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
                    fallback_reason=fallback_reason,
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
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
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
        official_imports: tuple[OfficialEvidenceImport, ...] = (),
        api_result: ApiContractResult | None = None,
        acceptance_scope_digest: str | None = None,
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
                acceptance_scope_digest=acceptance_scope_digest,
                collected_at=collected_at,
            )
        except TypeError as exc:
            raise AssuranceRunStaleError(
                "final Git collector cannot bind acceptance scope"
            ) from exc
        try:
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
        final_subject_digest = self._verify_scoped_git_result(
            final_git,
            task_digest=task_digest,
            policy_version=self._config.policy_version,
            rubric_version=self._config.rubric_version,
            attachment_digests=(),
            acceptance_scope_digest=acceptance_scope_digest,
            phase="final",
        )
        if (
            not initial_git.snapshot.complete
            or not final_git.snapshot.complete
            or final_subject_digest != subject_digest
        ):
            raise AssuranceRunStaleError("Git changed during reviewer execution")
        if type(final_intake) is not IntakeResult:
            raise AssuranceRunStaleError("final fence collector returned invalid result")
        if _strip_datetimes(_jsonable(final_intake)) != _strip_datetimes(_jsonable(initial_intake)):
            raise AssuranceRunStaleError("intake documents changed during reviewer execution")
        if api_result is not None:
            try:
                final_api = self._api_contract_collector.collect(
                    intent.repository_path,
                    subject_digest=subject_digest,
                    head_revision=initial_git.snapshot.head_revision,
                    artifact_store=self._artifact_store,
                    collected_at=collected_at,
                )
            except Exception as exc:
                raise AssuranceRunStaleError(
                    "API contract changed during reviewer execution"
                ) from exc
            if type(final_api) is not ApiContractResult:
                raise AssuranceRunStaleError(
                    "final API contract collector returned invalid result"
                )
            if _strip_datetimes(_jsonable(final_api)) != _strip_datetimes(_jsonable(api_result)):
                raise AssuranceRunStaleError(
                    "API contract changed during reviewer execution"
                )
        if official_imports:
            try:
                importer = OfficialEvidenceImporter(
                    workspace_root=self._config.workspace_root,
                    repository_path=intent.repository_path,
                    repository_identity=initial_git.snapshot.repository,
                    head_revision=initial_git.snapshot.head_revision,
                    subject_digest=subject_digest,
                    artifact_store=self._artifact_store,
                    collected_at=collected_at,
                )
                for imported in official_imports:
                    importer.verify_import(imported)
            except (OfficialEvidenceError, TypeError, ValueError) as exc:
                raise AssuranceRunStaleError(
                    "official evidence changed during reviewer execution"
                ) from exc
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
    "AssuranceRunOfficialEvidenceError",
    "AssuranceRunPreconditionError",
    "AssuranceRunResult",
    "AssuranceRunService",
    "FreshnessSourceBinding",
    "GitCollectorProfile",
    "ReviewerContextBuilder",
    "ReviewerContextPlan",
    "ReviewerContextPlanEntry",
    "ReviewerInvocationResponse",
    "ReviewerInvoker",
    "ReviewerRoute",
    "ReviewerRunRecord",
    "RunCommitter",
]
