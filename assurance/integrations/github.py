"""Offline GitHub payload exporter for CodeMesh Change Assurance.

This module only constructs deterministic request payloads.  It performs no
provider, network, subprocess, or credential I/O; a later transport may
publish the returned Check Run and issue-comment payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CHECK_NAME = "CodeMesh Change Assurance"
_MAX_ANNOTATIONS = 50
_REQUIRED_AUTH = "GitHub App checks:write / issue comment permission"
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GATE_CONCLUSIONS: dict[str, str] = {
    "ACCEPTED": "success",
    "PASS": "success",
    "INVALIDATED": "stale",
    "STALE": "stale",
    "REJECTED": "failure",
    "BLOCKED": "failure",
}
_ANNOTATION_LEVELS = Literal["notice", "warning", "failure"]


class GitHubExportError(ValueError):
    """Base error for invalid offline GitHub export input."""


class GitHubPassportError(GitHubExportError):
    """The assurance passport is not a valid canonical binding."""


class GitHubAnnotationError(GitHubExportError):
    """Annotations are malformed or exceed GitHub's per-request limit."""


def _nonblank_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be exactly a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise GitHubPassportError("passport is not canonical JSON") from exc


def canonical_passport_digest(passport: Mapping[str, object]) -> str:
    """Return the stable SHA-256 digest of a canonical passport mapping."""

    if not isinstance(passport, Mapping):
        raise GitHubPassportError("passport must be a mapping")
    if any(type(key) is not str for key in passport):
        raise GitHubPassportError("passport keys must be strings")
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(passport)).hexdigest()


def _validate_passport(passport: object) -> dict[str, object]:
    if not isinstance(passport, Mapping):
        raise GitHubPassportError("passport must be a canonical mapping")

    required = ("case_id", "subject_digest", "state", "gate", "revision")
    missing = [field for field in required if field not in passport]
    if missing:
        raise GitHubPassportError(
            "passport is missing required fields: " + ", ".join(missing)
        )

    values = dict(passport)
    for field in ("case_id", "state", "gate"):
        try:
            _nonblank_text(values[field], field)
        except ValueError as exc:
            raise GitHubPassportError(str(exc)) from exc

    subject_digest = values["subject_digest"]
    if (
        type(subject_digest) is not str
        or _SHA256_DIGEST_RE.fullmatch(subject_digest) is None
    ):
        raise GitHubPassportError(
            "subject_digest must be a lowercase sha256:<64 hex> digest"
        )

    revision = values["revision"]
    if type(revision) is not int or revision < 0:
        raise GitHubPassportError("revision must be a non-negative integer")

    try:
        _canonical_json_bytes(values)
    except GitHubPassportError:
        raise
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise GitHubPassportError("passport is not canonical JSON") from exc
    return values


class GitHubTarget(BaseModel):
    """The repository and revision to which offline payloads are bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str
    repo: str
    head_sha: str
    pr_number: int = Field(strict=True, gt=0)

    @field_validator("owner", "repo", "head_sha", mode="before")
    @classmethod
    def validate_text(cls, value: object, info) -> str:
        try:
            result = _nonblank_text(value, info.field_name)
        except ValueError:
            raise
        if info.field_name in {"owner", "repo"} and any(
            character in result for character in "/?#"
        ):
            raise ValueError(f"{info.field_name} contains an unsafe path character")
        return result


class GitHubAnnotation(BaseModel):
    """One explicitly positioned Check Run annotation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    start_line: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    level: _ANNOTATION_LEVELS
    message: str

    @field_validator("path", "message", mode="before")
    @classmethod
    def validate_text(cls, value: object, info) -> str:
        return _nonblank_text(value, info.field_name)

    @model_validator(mode="after")
    def validate_range(self) -> "GitHubAnnotation":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class GitHubCheckPayload(BaseModel):
    """An offline Check Run endpoint and JSON body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str
    body: dict[str, Any]


class GitHubCommentPayload(BaseModel):
    """An offline pull-request issue-comment endpoint and JSON body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str
    body: dict[str, Any]


class GitHubExportReceipt(BaseModel):
    """A non-publication receipt for deterministic GitHub payload generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    subject_digest: str
    passport_digest: str
    check_endpoint: str
    comment_endpoint: str
    published: Literal[False] = False
    required_auth: Literal[_REQUIRED_AUTH] = _REQUIRED_AUTH


class GitHubExportResult(BaseModel):
    """The two offline payloads and their non-publication receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: GitHubCheckPayload
    comment: GitHubCommentPayload
    receipt: GitHubExportReceipt


def _collection(passport: Mapping[str, object], field_name: str) -> list[object]:
    value = passport.get(field_name, [])
    if not isinstance(value, list):
        raise GitHubPassportError(f"passport field {field_name} must be a list")
    return value


def _summary(label: str, values: list[object]) -> str:
    if not values:
        return f"{label}: none"

    status_values: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            status = value.get("status", value.get("outcome", "declared"))
            status_values.append(str(status))
        else:
            status_values.append("invalid")
    counts: dict[str, int] = {}
    for status in status_values:
        counts[status] = counts.get(status, 0) + 1
    detail = ", ".join(
        f"{status}={count}" for status, count in sorted(counts.items())
    )
    return f"{label}: {len(values)} ({detail})"


def _normalise_annotations(
    annotations: Sequence[GitHubAnnotation | Mapping[str, object]] | None,
) -> tuple[GitHubAnnotation, ...]:
    if annotations is None:
        return ()
    if isinstance(annotations, (str, bytes, bytearray)):
        raise GitHubAnnotationError("annotations must be a sequence of annotations")
    if len(annotations) > _MAX_ANNOTATIONS:
        raise GitHubAnnotationError("GitHub allows at most 50 annotations per request")

    result: list[GitHubAnnotation] = []
    for annotation in annotations:
        if isinstance(annotation, GitHubAnnotation):
            result.append(annotation)
            continue
        if isinstance(annotation, Mapping):
            try:
                result.append(GitHubAnnotation.model_validate(annotation))
            except Exception as exc:
                raise GitHubAnnotationError("invalid GitHub annotation") from exc
            continue
        raise GitHubAnnotationError("annotations must contain GitHubAnnotation values")
    return tuple(result)


def _conclusion(gate: str) -> str:
    # Exact matching keeps malformed or future provider states fail-closed.
    return _GATE_CONCLUSIONS.get(gate, "action_required")


def _comment_text(
    passport: Mapping[str, object],
    passport_digest: str,
) -> str:
    case_id = passport["case_id"]
    subject_digest = passport["subject_digest"]
    evidence = _collection(passport, "evidence")
    findings = _collection(passport, "findings")
    decisions = _collection(passport, "policy_decisions") + _collection(
        passport, "human_decisions"
    )
    marker = f"<!-- codemesh-change-assurance:{case_id}:{subject_digest} -->"
    return "\n".join(
        (
            marker,
            "",
            "## CodeMesh Change Assurance",
            "",
            "offline payload, not published",
            "",
            f"Case: {case_id}",
            f"Subject: {subject_digest}",
            f"State: {passport['state']}",
            f"Gate: {passport['gate']}",
            f"Revision: {passport['revision']}",
            f"Passport digest: {passport_digest}",
            "",
            _summary("Evidence", evidence),
            _summary("Findings", findings),
            _summary("Decisions", decisions),
        )
    )


class GitHubExporter:
    """Build deterministic Check Run and pull-request comment payloads."""

    @staticmethod
    def export(
        passport: Mapping[str, object],
        target: GitHubTarget,
        *,
        annotations: Sequence[GitHubAnnotation | Mapping[str, object]] | None = None,
    ) -> GitHubExportResult:
        if not isinstance(target, GitHubTarget):
            raise GitHubExportError("target must be a GitHubTarget")

        bound_passport = _validate_passport(passport)
        passport_digest = canonical_passport_digest(bound_passport)
        normalised_annotations = _normalise_annotations(annotations)
        conclusion = _conclusion(bound_passport["gate"])

        evidence = _collection(bound_passport, "evidence")
        findings = _collection(bound_passport, "findings")
        decisions = _collection(bound_passport, "policy_decisions") + _collection(
            bound_passport, "human_decisions"
        )
        summary = "\n".join(
            (
                "Offline payload, not published.",
                f"Case {bound_passport['case_id']} gate={bound_passport['gate']}.",
                _summary("Evidence", evidence),
                _summary("Findings", findings),
                _summary("Decisions", decisions),
            )
        )

        check_endpoint = f"/repos/{target.owner}/{target.repo}/check-runs"
        comment_endpoint = (
            f"/repos/{target.owner}/{target.repo}/issues/{target.pr_number}/comments"
        )
        check_body = {
            "name": _CHECK_NAME,
            "head_sha": target.head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": _CHECK_NAME,
                "summary": summary,
                "annotations": [
                    annotation.model_dump(mode="json")
                    for annotation in normalised_annotations
                ],
            },
        }
        comment_body = {"body": _comment_text(bound_passport, passport_digest)}
        check = GitHubCheckPayload(endpoint=check_endpoint, body=check_body)
        comment = GitHubCommentPayload(endpoint=comment_endpoint, body=comment_body)
        receipt = GitHubExportReceipt(
            case_id=bound_passport["case_id"],
            subject_digest=bound_passport["subject_digest"],
            passport_digest=passport_digest,
            check_endpoint=check_endpoint,
            comment_endpoint=comment_endpoint,
        )
        return GitHubExportResult(check=check, comment=comment, receipt=receipt)


def export(
    passport: Mapping[str, object],
    target: GitHubTarget,
    *,
    annotations: Sequence[GitHubAnnotation | Mapping[str, object]] | None = None,
) -> GitHubExportResult:
    """Convenience wrapper around :meth:`GitHubExporter.export`."""

    return GitHubExporter.export(passport, target, annotations=annotations)


__all__ = [
    "GitHubAnnotation",
    "GitHubAnnotationError",
    "GitHubCheckPayload",
    "GitHubCommentPayload",
    "GitHubExportError",
    "GitHubExportReceipt",
    "GitHubExportResult",
    "GitHubExporter",
    "GitHubPassportError",
    "GitHubTarget",
    "canonical_passport_digest",
    "export",
]
