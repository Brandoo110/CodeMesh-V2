"""Server-owned orchestration for the bounded Assurance remediation boundary.

This module deliberately contains no repair runtime.  A configured caller may
inject a preparation callback, while the repository remains the authority for
the old Case, the selected Finding, replay, freshness, and the final atomic
commit.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from assurance.contracts import AcceptanceCase
from assurance.lifecycle_store import RemediationCommitReceipt
from assurance.remediation import (
    PreparedRemediationHandoff,
    RemediationPolicy,
    RemediationRequest,
    RemediationStatus,
)
from assurance.remediation_workspace import WorkspaceGrant
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebRepository,
    RemediationContext,
)


class AssuranceRemediationRequest(BaseModel):
    """The small, human-controlled portion of a remediation request.

    Workspace grants, policy, the old Case identity, and the old subject
    digest are intentionally absent.  Those values are server-owned and are
    supplied by :class:`AssuranceRemediationService` after loading immutable
    repository context.
    """

    model_config = ConfigDict(extra="forbid")

    remediation_id: str = Field(min_length=1, max_length=256)
    human_selected_finding_id: str = Field(min_length=1, max_length=256)
    requested_by: str = Field(min_length=1, max_length=256)
    requested_at: AwareDatetime

    @field_validator(
        "remediation_id", "human_selected_finding_id", "requested_by"
    )
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request identity must not be blank")
        return value


@dataclass(frozen=True)
class AssuranceRemediationConfig:
    """Trusted server configuration used to build a domain request.

    The values are supplied by the composition root.  HTTP input cannot
    replace either the immutable workspace grant or the bounded policy.
    """

    workspace_grant: WorkspaceGrant
    policy: RemediationPolicy
    requested_at_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if type(self.workspace_grant) is not WorkspaceGrant:
            raise TypeError("workspace_grant must be an exact WorkspaceGrant")
        if type(self.policy) is not RemediationPolicy:
            raise TypeError("policy must be an exact RemediationPolicy")
        if not callable(self.requested_at_factory):
            raise TypeError("requested_at_factory must be callable")


class RemediationRequestFactory(Protocol):
    def __call__(
        self,
        context: RemediationContext,
        intent: AssuranceRemediationRequest,
        *,
        idempotency_key: str,
        config: AssuranceRemediationConfig | None,
    ) -> RemediationRequest: ...


class RemediationPrepareCallback(Protocol):
    def __call__(
        self,
        request: RemediationRequest,
        *,
        context: RemediationContext,
    ) -> PreparedRemediationHandoff: ...


@dataclass(frozen=True)
class AssuranceRemediationResult:
    """Publicly projectable result of one service orchestration."""

    receipt: RemediationCommitReceipt
    case_view: dict[str, Any]
    cached: bool


class AssuranceRemediationError(Exception):
    """Base error for the server-owned remediation service."""


class AssuranceRemediationNotConfiguredError(AssuranceRemediationError):
    """No repair preparation callback was installed by the composition root."""


class AssuranceRemediationValidationError(
    AssuranceRemediationError, ValueError
):
    """The service boundary or a trusted factory returned invalid facts."""


class AssuranceRemediationPreparationError(AssuranceRemediationError):
    """The injected preparation seam failed before any repository write."""


class AssuranceRemediationNotAppliedError(AssuranceRemediationError):
    """Preparation completed without a successful bundle to persist."""

    def __init__(self, *, status: RemediationStatus, reason_code: str) -> None:
        self.status = status
        self.reason_code = reason_code
        super().__init__("assurance remediation was not applied")


def _filtered_kwargs(
    function: Callable[..., Any], values: dict[str, Any]
) -> dict[str, Any]:
    """Pass only supported named values to small test/composition callbacks."""

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return values
    if any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()):
        return values
    return {
        name: value
        for name, value in values.items()
        if name in parameters
        and parameters[name].kind
        not in (parameters[name].POSITIONAL_ONLY,)
    }


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke(function: Callable[..., Any], values: dict[str, Any]) -> Any:
    return await _await_if_needed(function(**_filtered_kwargs(function, values)))


def _server_request_factory(
    context: RemediationContext,
    intent: AssuranceRemediationRequest,
    *,
    config: AssuranceRemediationConfig | None,
    **_: Any,
) -> RemediationRequest:
    """Build the exact domain request from trusted server facts."""

    if config is None:
        raise AssuranceRemediationNotConfiguredError(
            "assurance remediation request factory is not configured"
        )
    return RemediationRequest(
        remediation_id=intent.remediation_id,
        old_case_id=context.old_case_id,
        old_subject_digest=context.old_subject_digest,
        human_selected_finding_id=intent.human_selected_finding_id,
        requested_by=intent.requested_by,
        requested_at=intent.requested_at,
        workspace_grant=config.workspace_grant,
        policy=config.policy,
    )


class AssuranceRemediationService:
    """Orchestrate context, replay, one preparation, and one final commit.

    The service has no default agent/provider.  ``prepare_callback`` is an
    explicit seam for tests or a later server-owned runtime composition.
    """

    def __init__(
        self,
        repository: AssuranceWebRepository,
        request_factory: RemediationRequestFactory | Callable[..., Any] | None = None,
        prepare_callback: RemediationPrepareCallback | Callable[..., Any] | None = None,
        *,
        config: AssuranceRemediationConfig | None = None,
        prepare: RemediationPrepareCallback | Callable[..., Any] | None = None,
    ) -> None:
        required_methods = (
            "load_remediation_context",
            "lookup_remediation_replay",
            "commit_prepared_remediation",
            "get_change",
        )
        if repository is None or any(
            not callable(getattr(repository, name, None)) for name in required_methods
        ):
            raise TypeError("repository must expose the remediation repository seam")
        if config is not None and type(config) is not AssuranceRemediationConfig:
            raise TypeError("config must be an exact AssuranceRemediationConfig")
        if prepare_callback is not None and prepare is not None:
            raise TypeError("provide only one prepare callback")
        callback = prepare_callback if prepare_callback is not None else prepare
        if callback is not None and not callable(callback):
            raise TypeError("prepare_callback must be callable")
        if request_factory is not None and not callable(request_factory):
            raise TypeError("request_factory must be callable")

        self.repository = repository
        self.config = config
        self.request_factory = request_factory or _server_request_factory
        self.prepare_callback = callback
        # Once a remediation wins, its old Case is intentionally invalidated.
        # Keep the exact server-built request long enough to recover a replay
        # in this process before consulting the durable replay seam.
        self._request_cache: dict[tuple[str, str], RemediationRequest] = {}

    @staticmethod
    def _validate_inputs(
        case_id: str,
        intent: AssuranceRemediationRequest,
        idempotency_key: str,
    ) -> None:
        if type(case_id) is not str or not case_id.strip():
            raise AssuranceRemediationValidationError("case_id must be nonblank")
        if type(intent) is not AssuranceRemediationRequest:
            raise AssuranceRemediationValidationError(
                "intent must be an exact AssuranceRemediationRequest"
            )
        if type(idempotency_key) is not str or not idempotency_key.strip():
            raise AssuranceRemediationValidationError(
                "idempotency_key must be nonblank"
            )
        try:
            too_long = len(idempotency_key.encode("utf-8")) > 256
        except UnicodeEncodeError as exc:
            raise AssuranceRemediationValidationError(
                "idempotency_key must be valid UTF-8"
            ) from exc
        if too_long:
            raise AssuranceRemediationValidationError("idempotency_key is too long")

    async def remediate(
        self,
        case_id: str,
        intent: AssuranceRemediationRequest,
        *,
        idempotency_key: str,
    ) -> AssuranceRemediationResult:
        """Run the bounded orchestration with preparation outside the UOW."""

        self._validate_inputs(case_id, intent, idempotency_key)
        if self.prepare_callback is None:
            raise AssuranceRemediationNotConfiguredError(
                "assurance remediation service is not configured"
            )

        try:
            context = await _invoke(
                self.repository.load_remediation_context,
                {
                    "case_id": case_id,
                    "human_selected_finding_id": intent.human_selected_finding_id,
                },
            )
        except AssuranceWebConflictError as context_error:
            # A committed remediation invalidates the old Case.  The public
            # context seam correctly rejects new work on that Case, so an
            # in-process exact retry uses the request already built by this
            # service and still lets the repository perform authoritative
            # replay validation.  No prepare/commit is attempted here.
            request = self._request_cache.get((case_id, idempotency_key))
            if request is None:
                # A new service instance has no in-memory request cache.  Use
                # the public Case projection only to reconstruct a candidate
                # request; the durable lookup below remains authoritative and
                # rejects any projection or request drift.
                try:
                    projection = await _invoke(
                        self.repository.get_change,
                        {"case_id": case_id},
                    )
                    case = AcceptanceCase.model_validate(projection["case"])
                    fallback_context = RemediationContext(
                        old_case=case,
                        baseline_binding=None,
                        baseline_bundle=None,
                        selected_finding=None,
                        source_binding=None,
                    )
                    request = await _invoke(
                        self.request_factory,
                        {
                            "context": fallback_context,
                            "intent": intent,
                            "body": intent,
                            "request": intent,
                            "idempotency_key": idempotency_key,
                            "config": self.config,
                        },
                    )
                    if type(request) is not RemediationRequest:
                        raise AssuranceRemediationValidationError(
                            "request factory must return an exact RemediationRequest"
                        )
                    if (
                        request.old_case_id != case_id
                        or request.old_subject_digest != case.subject_digest
                        or request.human_selected_finding_id
                        != intent.human_selected_finding_id
                    ):
                        raise AssuranceRemediationValidationError(
                            "request factory returned context-mismatched remediation facts"
                        )
                except (ValidationError, TypeError, ValueError, KeyError):
                    raise context_error
            replay = await _invoke(
                self.repository.lookup_remediation_replay,
                {"request": request, "idempotency_key": idempotency_key},
            )
            if replay is None:
                raise
            if type(replay) is not RemediationCommitReceipt:
                raise AssuranceRemediationValidationError(
                    "repository returned an invalid cached remediation receipt"
                )
            case_view = await _invoke(
                self.repository.get_change,
                {"case_id": replay.new_case_id},
            )
            if not isinstance(case_view, dict):
                raise AssuranceRemediationValidationError(
                    "repository returned an invalid remediation CaseView"
                )
            return AssuranceRemediationResult(
                receipt=replay,
                case_view=case_view,
                cached=True,
            )
        if not isinstance(context, RemediationContext):
            raise AssuranceRemediationValidationError(
                "repository returned invalid remediation context"
            )

        request = await _invoke(
            self.request_factory,
            {
                "context": context,
                "intent": intent,
                "body": intent,
                "request": intent,
                "idempotency_key": idempotency_key,
                "config": self.config,
            },
        )
        if type(request) is not RemediationRequest:
            raise AssuranceRemediationValidationError(
                "request factory must return an exact RemediationRequest"
            )
        if (
            request.old_case_id != context.old_case_id
            or request.old_subject_digest != context.old_subject_digest
            or request.human_selected_finding_id
            != intent.human_selected_finding_id
        ):
            raise AssuranceRemediationValidationError(
                "request factory returned context-mismatched remediation facts"
            )
        self._request_cache[(case_id, idempotency_key)] = request

        replay = await _invoke(
            self.repository.lookup_remediation_replay,
            {"request": request, "idempotency_key": idempotency_key},
        )
        if replay is not None:
            if type(replay) is not RemediationCommitReceipt:
                raise AssuranceRemediationValidationError(
                    "repository returned an invalid cached remediation receipt"
                )
            case_view = await _invoke(
                self.repository.get_change,
                {"case_id": replay.new_case_id},
            )
            if not isinstance(case_view, dict):
                raise AssuranceRemediationValidationError(
                    "repository returned an invalid remediation CaseView"
                )
            return AssuranceRemediationResult(
                receipt=replay,
                case_view=case_view,
                cached=True,
            )

        try:
            prepared = await _invoke(
                self.prepare_callback,
                {
                    "request": request,
                    "context": context,
                    "selected_finding": context.selected_finding,
                    "baseline_bundle": context.baseline_bundle,
                },
            )
        except AssuranceRemediationPreparationError:
            raise
        except Exception as exc:
            raise AssuranceRemediationPreparationError(
                "assurance remediation preparation failed"
            ) from exc
        if type(prepared) is not PreparedRemediationHandoff:
            raise AssuranceRemediationValidationError(
                "prepare callback must return an exact PreparedRemediationHandoff"
            )
        if (
            prepared.result.status is not RemediationStatus.SUCCEEDED
            or prepared.bundle is None
        ):
            raise AssuranceRemediationNotAppliedError(
                status=prepared.result.status,
                reason_code=prepared.result.reason_code,
            )

        receipt = await _invoke(
            self.repository.commit_prepared_remediation,
            {
                "request": request,
                "handoff": prepared,
                "idempotency_key": idempotency_key,
            },
        )
        if type(receipt) is not RemediationCommitReceipt:
            raise AssuranceRemediationValidationError(
                "repository returned an invalid remediation receipt"
            )
        case_view = await _invoke(
            self.repository.get_change,
            {"case_id": receipt.new_case_id},
        )
        if not isinstance(case_view, dict):
            raise AssuranceRemediationValidationError(
                "repository returned an invalid remediation CaseView"
            )
        return AssuranceRemediationResult(
            receipt=receipt,
            case_view=case_view,
            cached=False,
        )

    async def run(
        self,
        case_id: str,
        intent: AssuranceRemediationRequest,
        *,
        idempotency_key: str,
    ) -> AssuranceRemediationResult:
        """Compatibility alias for callers that name the operation ``run``."""

        return await self.remediate(
            case_id,
            intent,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "AssuranceRemediationConfig",
    "AssuranceRemediationError",
    "AssuranceRemediationNotConfiguredError",
    "AssuranceRemediationNotAppliedError",
    "AssuranceRemediationPreparationError",
    "AssuranceRemediationRequest",
    "AssuranceRemediationResult",
    "AssuranceRemediationService",
    "AssuranceRemediationValidationError",
    "RemediationPrepareCallback",
    "RemediationRequestFactory",
]
