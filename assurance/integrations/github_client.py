"""Live GitHub Check Run publisher with exact readback.

``assurance.integrations.github`` remains an offline payload exporter.  This
module is the explicitly separate transport boundary used by the real CI
entry.  Every create/replay path reads GitHub's authoritative Check Run back
and verifies the Case, passport digest, conclusion, and head SHA binding.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .github import (
    GitHubAnnotation,
    GitHubExporter,
    GitHubTarget,
    canonical_passport_digest,
)


_CHECK_NAME = "CodeMesh Change Assurance"
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class GitHubPublishError(RuntimeError):
    """A GitHub publish or authoritative readback failure."""


class GitHubPublishResult(BaseModel):
    """Safe, path-free receipt returned after a verified Check Run."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    check_id: StrictInt = Field(gt=0)
    check_url: StrictStr | None = None
    owner: StrictStr
    repo: StrictStr
    head_sha: StrictStr
    case_id: StrictStr
    subject_digest: StrictStr
    passport_digest: StrictStr
    status: StrictStr
    conclusion: StrictStr
    reused: StrictBool


def _nonblank(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be nonblank text")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains NUL")
    return value


def _validate_api_url(value: object) -> str:
    raw = _nonblank(value, "api_url").rstrip("/")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:  # pragma: no cover - defensive parser boundary
        raise ValueError("api_url must be a valid HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("api_url must be an HTTP URL")
    return raw


def _published_summary(
    *,
    case_id: str,
    subject_digest: str,
    passport_digest: str,
    state: str,
    gate: str,
    offline_summary: object,
) -> str:
    if type(offline_summary) is not str or not offline_summary.strip():
        raise GitHubPublishError("GitHub exporter returned an invalid summary")
    marker = (
        f"codemesh-case:{case_id};subject:{subject_digest};"
        f"passport:{passport_digest}"
    )
    summary = offline_summary.replace("Offline payload, not published.", "")
    return "\n".join(
        (
            marker,
            "Published by CodeMesh Change Assurance.",
            f"State: {state}",
            f"Gate: {gate}",
            summary.strip(),
        )
    )


def _stable_passport_digest(passport: Mapping[str, object]) -> str:
    """Digest passport facts while excluding only live probe wall-clock time."""

    stable = copy.deepcopy(dict(passport))
    freshness = stable.get("freshness")
    if isinstance(freshness, Mapping):
        stable_freshness = dict(freshness)
        stable_freshness.pop("checked_at", None)
        stable["freshness"] = stable_freshness
    return canonical_passport_digest(stable)


class GitHubCheckPublisher:
    """Publish one Check Run and verify the provider-owned result."""

    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = _nonblank(token, "token")
        self.api_url = _validate_api_url(api_url)
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("timeout must be positive")
        parsed = httpx.URL(self.api_url)
        if transport is None and parsed.scheme != "https":
            raise ValueError("GitHub API URL must use HTTPS")
        self._client = httpx.Client(
            base_url=self.api_url,
            timeout=float(timeout),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codemesh-assurance",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubCheckPublisher":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        expected: set[int],
        json_payload: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                endpoint,
                json=dict(json_payload) if json_payload is not None else None,
                params=dict(params or {}),
            )
        except httpx.RequestError as exc:
            raise GitHubPublishError("GitHub API is unavailable") from exc
        if response.status_code not in expected:
            raise GitHubPublishError(
                f"GitHub API request failed (HTTP {response.status_code})"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise GitHubPublishError("GitHub API returned invalid JSON") from exc

    @staticmethod
    def _validate_target(owner: object, repo: object, head_sha: object) -> tuple[str, str, str]:
        owner_text = _nonblank(owner, "owner")
        repo_text = _nonblank(repo, "repo")
        if any(character in owner_text for character in "/?#"):
            raise ValueError("owner contains an unsafe path character")
        if any(character in repo_text for character in "/?#"):
            raise ValueError("repo contains an unsafe path character")
        sha_text = _nonblank(head_sha, "head_sha")
        if _SHA1_RE.fullmatch(sha_text) is None:
            raise ValueError("head_sha must be a lowercase 40-character SHA")
        return owner_text, repo_text, sha_text

    @staticmethod
    def _check_summary(value: Mapping[str, object]) -> str | None:
        output = value.get("output")
        if not isinstance(output, Mapping):
            return None
        summary = output.get("summary")
        return summary if type(summary) is str else None

    def _readback_check(
        self,
        payload: object,
        *,
        owner: str,
        repo: str,
        head_sha: str,
        case_id: str,
        subject_digest: str,
        passport_digest: str,
        expected_state: str,
        expected_gate: str,
        expected_conclusion: str,
    ) -> GitHubPublishResult:
        if not isinstance(payload, Mapping):
            raise GitHubPublishError("GitHub returned an invalid Check Run")
        check_id = payload.get("id")
        if type(check_id) is not int or check_id <= 0:
            raise GitHubPublishError("GitHub returned an invalid Check Run id")
        if payload.get("name") != _CHECK_NAME:
            raise GitHubPublishError("GitHub Check Run name did not match")
        if payload.get("head_sha") != head_sha:
            raise GitHubPublishError("GitHub Check Run SHA did not match")
        if payload.get("status") != "completed":
            raise GitHubPublishError("GitHub Check Run was not completed")
        if payload.get("conclusion") != expected_conclusion:
            raise GitHubPublishError("GitHub Check Run conclusion did not match")
        summary = self._check_summary(payload)
        marker = (
            f"codemesh-case:{case_id};subject:{subject_digest};"
            f"passport:{passport_digest}"
        )
        if summary is None or marker not in summary:
            raise GitHubPublishError("GitHub Check Run passport binding did not match")
        binding_prefix = "\n".join(
            (
                marker,
                "Published by CodeMesh Change Assurance.",
                f"State: {expected_state}",
                f"Gate: {expected_gate}",
            )
        )
        if not summary.startswith(binding_prefix):
            raise GitHubPublishError("GitHub Check Run state binding did not match")
        check_url = payload.get("html_url")
        if check_url is not None:
            if type(check_url) is not str:
                raise GitHubPublishError("GitHub Check Run URL was invalid")
            try:
                parsed_url = httpx.URL(check_url)
            except Exception as exc:
                raise GitHubPublishError("GitHub Check Run URL was invalid") from exc
            if (
                parsed_url.scheme != "https"
                or not parsed_url.host
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise GitHubPublishError("GitHub Check Run URL was invalid")
        return GitHubPublishResult(
            check_id=check_id,
            check_url=check_url,
            owner=owner,
            repo=repo,
            head_sha=head_sha,
            case_id=case_id,
            subject_digest=subject_digest,
            passport_digest=passport_digest,
            status="completed",
            conclusion=expected_conclusion,
            reused=False,
        )

    def _find_existing(
        self,
        *,
        owner: str,
        repo: str,
        head_sha: str,
        case_id: str,
        subject_digest: str,
        passport_digest: str,
    ) -> Mapping[str, object] | None:
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            expected={200},
            params={"check_name": _CHECK_NAME},
        )
        payload = self._json(response)
        if not isinstance(payload, Mapping):
            raise GitHubPublishError("GitHub returned an invalid Check Run list")
        checks = payload.get("check_runs")
        if not isinstance(checks, list):
            raise GitHubPublishError("GitHub returned an invalid Check Run list")
        marker = (
            f"codemesh-case:{case_id};subject:{subject_digest};"
            f"passport:{passport_digest}"
        )
        matches: list[Mapping[str, object]] = []
        for check in checks:
            if not isinstance(check, Mapping):
                raise GitHubPublishError("GitHub returned an invalid Check Run entry")
            summary = self._check_summary(check)
            if (
                check.get("name") == _CHECK_NAME
                and check.get("head_sha") == head_sha
                and summary is not None
                and marker in summary
            ):
                matches.append(check)
        if len(matches) > 1:
            raise GitHubPublishError("GitHub returned multiple matching Check Runs")
        return matches[0] if matches else None

    def publish(
        self,
        passport: Mapping[str, object],
        *,
        owner: str,
        repo: str,
        head_sha: str,
        annotations: Sequence[GitHubAnnotation | Mapping[str, object]] | None = None,
    ) -> GitHubPublishResult:
        """Create or safely reuse a Check Run, then GET it back from GitHub."""

        owner_text, repo_text, sha_text = self._validate_target(owner, repo, head_sha)
        try:
            target = GitHubTarget(
                owner=owner_text,
                repo=repo_text,
                head_sha=sha_text,
                pr_number=1,
            )
            exported = GitHubExporter.export(passport, target, annotations=annotations)
        except Exception as exc:
            # Keep exporter details out of a user-facing transport boundary; the
            # offline exporter remains responsible for its own focused errors.
            raise GitHubPublishError("passport could not be prepared for GitHub") from exc

        bound_passport = dict(passport)
        case_id = bound_passport.get("case_id")
        subject_digest = bound_passport.get("subject_digest")
        state = bound_passport.get("state")
        gate = bound_passport.get("gate")
        if type(case_id) is not str or not case_id.strip():
            raise GitHubPublishError("passport case binding was invalid")
        if type(subject_digest) is not str or not subject_digest.strip():
            raise GitHubPublishError("passport subject binding was invalid")
        if type(state) is not str or not state.strip() or type(gate) is not str or not gate.strip():
            raise GitHubPublishError("passport state binding was invalid")
        passport_digest = _stable_passport_digest(bound_passport)
        expected_conclusion = exported.check.body.get("conclusion")
        if type(expected_conclusion) is not str:
            raise GitHubPublishError("passport gate conclusion was invalid")

        existing = self._find_existing(
            owner=owner_text,
            repo=repo_text,
            head_sha=sha_text,
            case_id=case_id,
            subject_digest=subject_digest,
            passport_digest=passport_digest,
        )
        if existing is not None:
            check_id = existing.get("id")
            if type(check_id) is not int or check_id <= 0:
                raise GitHubPublishError("GitHub returned an invalid existing Check Run id")
            detail_response = self._request(
                "GET",
                f"/repos/{owner_text}/{repo_text}/check-runs/{check_id}",
                expected={200},
            )
            verified = self._readback_check(
                self._json(detail_response),
                owner=owner_text,
                repo=repo_text,
                head_sha=sha_text,
                case_id=case_id,
                subject_digest=subject_digest,
                passport_digest=passport_digest,
                expected_state=state,
                expected_gate=gate,
                expected_conclusion=expected_conclusion,
            )
            return verified.model_copy(update={"reused": True})

        body = copy.deepcopy(exported.check.body)
        output = body.get("output")
        if not isinstance(output, dict):
            raise GitHubPublishError("GitHub exporter returned an invalid Check payload")
        output["summary"] = _published_summary(
            case_id=case_id,
            subject_digest=subject_digest,
            passport_digest=passport_digest,
            state=state,
            gate=gate,
            offline_summary=output.get("summary"),
        )
        body["output"] = output
        created_response = self._request(
            "POST",
            f"/repos/{owner_text}/{repo_text}/check-runs",
            expected={201},
            json_payload=body,
        )
        created = self._json(created_response)
        if not isinstance(created, Mapping):
            raise GitHubPublishError("GitHub returned an invalid created Check Run")
        check_id = created.get("id")
        if type(check_id) is not int or check_id <= 0:
            raise GitHubPublishError("GitHub returned an invalid created Check Run id")
        detail_response = self._request(
            "GET",
            f"/repos/{owner_text}/{repo_text}/check-runs/{check_id}",
            expected={200},
        )
        return self._readback_check(
            self._json(detail_response),
            owner=owner_text,
            repo=repo_text,
            head_sha=sha_text,
            case_id=case_id,
            subject_digest=subject_digest,
            passport_digest=passport_digest,
            expected_state=state,
            expected_gate=gate,
            expected_conclusion=expected_conclusion,
        )


__all__ = ["GitHubCheckPublisher", "GitHubPublishError", "GitHubPublishResult"]
