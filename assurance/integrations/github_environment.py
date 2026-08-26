"""Offline GitHub pending-deployment intents for CodeMesh Change Assurance.

This module only constructs deterministic GET/POST intents for GitHub Actions
pending deployments.  It never performs HTTP, provider, subprocess, or
credential I/O.  The POST intent can only be created from an explicit human
decision; a later transport is responsible for authentication and publication.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GitHubEnvironmentExportError(ValueError):
    """Base error for invalid offline environment intent input."""


class GitHubEnvironmentDecisionError(GitHubEnvironmentExportError):
    """The required human approval/rejection is missing or malformed."""


def _nonblank_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be exactly a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _safe_path_part(value: object, field_name: str) -> str:
    result = _nonblank_text(value, field_name)
    if result in {".", ".."} or any(
        character in result for character in "/?#%\\"
    ):
        raise ValueError(f"{field_name} contains an unsafe path character")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{field_name} contains a control character")
    return result


class GitHubEnvironmentTarget(BaseModel):
    """The repository and workflow run to which a pending-deployment intent is bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str
    repo: str
    run_id: int = Field(strict=True, gt=0)

    @field_validator("owner", "repo", mode="before")
    @classmethod
    def validate_path_parts(cls, value: object, info) -> str:
        return _safe_path_part(value, info.field_name)


class GitHubEnvironmentDecision(BaseModel):
    """An explicit human decision required for a pending-deployment POST."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["approved", "rejected"]
    actor: str
    comment: str
    environment_ids: tuple[int, ...]

    @field_validator("actor", "comment", mode="before")
    @classmethod
    def validate_text(cls, value: object, info) -> str:
        return _nonblank_text(value, info.field_name)

    @field_validator("environment_ids", mode="before")
    @classmethod
    def validate_environment_ids(cls, value: object) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("environment_ids must be a non-empty list")
        result: list[int] = []
        for item in value:
            if type(item) is not int or item <= 0:
                raise ValueError("environment_ids must contain positive integers")
            if item in result:
                raise ValueError("environment_ids must be unique")
            result.append(item)
        return tuple(result)


class GitHubPendingDeploymentGetIntent(BaseModel):
    """A provider-shaped GET request that has not been published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET"] = "GET"
    endpoint: str
    body: None = None


class GitHubPendingDeploymentPostBody(BaseModel):
    """The exact provider body authorized by the human decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_ids: tuple[int, ...]
    state: Literal["approved", "rejected"]
    comment: str

    @field_validator("environment_ids", mode="before")
    @classmethod
    def validate_environment_ids(cls, value: object) -> tuple[int, ...]:
        return GitHubEnvironmentDecision.validate_environment_ids(value)

    @field_validator("comment", mode="before")
    @classmethod
    def validate_comment(cls, value: object) -> str:
        return _nonblank_text(value, "comment")


class GitHubPendingDeploymentPostIntent(BaseModel):
    """A provider-shaped POST request plus the human decision that authorized it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["POST"] = "POST"
    endpoint: str
    body: GitHubPendingDeploymentPostBody
    human_decision: GitHubEnvironmentDecision


class GitHubPendingDeploymentReceipt(BaseModel):
    """A non-publication receipt for one pending-deployment intent pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    owner: str
    repo: str
    run_id: int = Field(strict=True, gt=0)
    get_endpoint: str
    post_endpoint: str
    human_decision: Literal["approved", "rejected"] | None = None
    human_actor: str | None = None
    published: Literal[False] = False
    actor_basis: Literal["caller_declared"] = "caller_declared"

    @field_validator("owner", "repo", mode="before")
    @classmethod
    def validate_target_parts(cls, value: object, info) -> str:
        return _safe_path_part(value, info.field_name)

    @model_validator(mode="after")
    def validate_decision_binding(self) -> "GitHubPendingDeploymentReceipt":
        if self.human_decision is None and self.human_actor is not None:
            raise ValueError("human_actor requires a human_decision")
        if self.human_decision is not None and self.human_actor is None:
            raise ValueError("human_decision requires a human_actor")
        return self


class GitHubEnvironmentExportResult(BaseModel):
    """GET-only or human-authorized GET/POST pending-deployment intents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    get: GitHubPendingDeploymentGetIntent
    post: GitHubPendingDeploymentPostIntent | None = None
    receipt: GitHubPendingDeploymentReceipt

    @model_validator(mode="after")
    def validate_receipt_binding(self) -> "GitHubEnvironmentExportResult":
        expected_endpoint = _endpoint(
            GitHubEnvironmentTarget(
                owner=self.receipt.owner,
                repo=self.receipt.repo,
                run_id=self.receipt.run_id,
            )
        )
        if self.get.endpoint != expected_endpoint:
            raise ValueError("GET endpoint must match receipt target")
        if self.receipt.get_endpoint != expected_endpoint:
            raise ValueError("receipt GET endpoint must match target")
        if self.receipt.post_endpoint != expected_endpoint:
            raise ValueError("receipt POST endpoint must match target")
        if self.post is None:
            if (
                self.receipt.human_decision is not None
                or self.receipt.human_actor is not None
            ):
                raise ValueError("GET-only result must not contain a decision")
            return self
        if self.post.endpoint != expected_endpoint:
            raise ValueError("POST endpoint must match receipt target")
        if self.receipt.human_decision != self.post.human_decision.decision:
            raise ValueError("receipt decision must equal POST decision")
        if self.receipt.human_actor != self.post.human_decision.actor:
            raise ValueError("receipt actor must equal POST actor")
        expected_body = GitHubPendingDeploymentPostBody(
            environment_ids=self.post.human_decision.environment_ids,
            state=self.post.human_decision.decision,
            comment=self.post.human_decision.comment,
        )
        if self.post.body != expected_body:
            raise ValueError("POST body must exactly equal the human decision")
        return self


def _target_model(target: GitHubEnvironmentTarget | Mapping[str, object]) -> GitHubEnvironmentTarget:
    if isinstance(target, GitHubEnvironmentTarget):
        return target
    if isinstance(target, Mapping):
        try:
            return GitHubEnvironmentTarget.model_validate(target)
        except Exception as exc:
            raise GitHubEnvironmentExportError("invalid GitHub environment target") from exc
    raise GitHubEnvironmentExportError("target must be a GitHubEnvironmentTarget")


def _decision_model(
    decision: GitHubEnvironmentDecision | Mapping[str, object] | None,
) -> GitHubEnvironmentDecision:
    if decision is None:
        raise GitHubEnvironmentDecisionError(
            "POST intent requires an explicit human decision"
        )
    if isinstance(decision, GitHubEnvironmentDecision):
        return decision
    if isinstance(decision, Mapping):
        try:
            return GitHubEnvironmentDecision.model_validate(decision)
        except Exception as exc:
            raise GitHubEnvironmentDecisionError(
                "invalid explicit human decision"
            ) from exc
    raise GitHubEnvironmentDecisionError(
        "human decision must be a GitHubEnvironmentDecision or mapping"
    )


def _endpoint(target: GitHubEnvironmentTarget) -> str:
    return (
        f"/repos/{target.owner}/{target.repo}/actions/runs/"
        f"{target.run_id}/pending_deployments"
    )


class GitHubEnvironmentExporter:
    """Build offline pending-deployment GET/POST intents."""

    @staticmethod
    def export(
        target: GitHubEnvironmentTarget | Mapping[str, object],
        human_decision: GitHubEnvironmentDecision
        | Mapping[str, object]
        | None = None,
    ) -> GitHubEnvironmentExportResult:
        bound_target = _target_model(target)
        endpoint = _endpoint(bound_target)
        get = GitHubPendingDeploymentGetIntent(endpoint=endpoint)
        if human_decision is None:
            receipt = GitHubPendingDeploymentReceipt(
                owner=bound_target.owner,
                repo=bound_target.repo,
                run_id=bound_target.run_id,
                get_endpoint=endpoint,
                post_endpoint=endpoint,
            )
            return GitHubEnvironmentExportResult(get=get, receipt=receipt)

        decision = _decision_model(human_decision)
        post = GitHubPendingDeploymentPostIntent(
            endpoint=endpoint,
            body=GitHubPendingDeploymentPostBody(
                environment_ids=decision.environment_ids,
                state=decision.decision,
                comment=decision.comment,
            ),
            human_decision=decision,
        )
        receipt = GitHubPendingDeploymentReceipt(
            owner=bound_target.owner,
            repo=bound_target.repo,
            run_id=bound_target.run_id,
            get_endpoint=endpoint,
            post_endpoint=endpoint,
            human_decision=decision.decision,
            human_actor=decision.actor,
        )
        return GitHubEnvironmentExportResult(get=get, post=post, receipt=receipt)

    @staticmethod
    def get(
        target: GitHubEnvironmentTarget | Mapping[str, object],
    ) -> GitHubEnvironmentExportResult:
        return GitHubEnvironmentExporter.export(target)

    @staticmethod
    def post(
        target: GitHubEnvironmentTarget | Mapping[str, object],
        human_decision: GitHubEnvironmentDecision
        | Mapping[str, object]
        | None = None,
    ) -> GitHubEnvironmentExportResult:
        decision = _decision_model(human_decision)
        return GitHubEnvironmentExporter.export(target, decision)


def export(
    target: GitHubEnvironmentTarget | Mapping[str, object],
    human_decision: GitHubEnvironmentDecision
    | Mapping[str, object]
    | None = None,
) -> GitHubEnvironmentExportResult:
    """Convenience wrapper around :meth:`GitHubEnvironmentExporter.export`."""

    return GitHubEnvironmentExporter.export(target, human_decision)


__all__ = [
    "GitHubEnvironmentDecision",
    "GitHubEnvironmentDecisionError",
    "GitHubEnvironmentExportError",
    "GitHubEnvironmentExportResult",
    "GitHubEnvironmentExporter",
    "GitHubEnvironmentTarget",
    "GitHubPendingDeploymentGetIntent",
    "GitHubPendingDeploymentPostBody",
    "GitHubPendingDeploymentPostIntent",
    "GitHubPendingDeploymentReceipt",
    "export",
]
