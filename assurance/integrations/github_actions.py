"""GitHub Actions transport and CI-only Bundle import adapter.

The local side owns only the temporary content-addressed ref and the lifecycle
readback.  The CI side imports verified bytes, never opens SQLite, never calls a
reviewer/provider, and uses the Actions App-backed ``GITHUB_TOKEN`` only for the
existing Check publisher and its final authoritative Check update/readback.
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import os
import re
import stat
import subprocess
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from ..case_publication import (
    IdempotencyConflict,
    PublicationRemoteError,
    RemotePublication,
)
from ..evidence_bundle import (
    BuiltEvidenceBundle,
    VerifiedEvidenceBundle,
    canonical_json_bytes,
    verify_evidence_bundle,
)
from .github_client import GitHubCheckPublisher


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s?#]+/[^/\s?#]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_BUNDLE_REF_RE = re.compile(r"^refs/heads/codex/evidence/([0-9a-f]{40})$")
_IMPORTED_CHECK_NAME = "CodeMesh Imported Authoritative Case"
_WORKFLOW_FILE = "codemesh-assurance.yml"
_BUNDLE_FILE = "bundle.json"
_MAX_ZIP_BYTES = 4 * 1024 * 1024
_MAX_ZIP_UNCOMPRESSED_BYTES = 8 * 1024 * 1024


class GitHubActionsError(PublicationRemoteError):
    """A GitHub or Actions operation failed."""

    def __init__(self, message: str, *, status_code: int | None = None, unknown: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.unknown = unknown


@dataclass(frozen=True)
class _RemoteRef:
    ref: str
    commit_sha: str
    created: bool


@dataclass(frozen=True)
class _WorkflowFacts:
    run_id: str
    job_id: str
    run_attempt: int
    artifact_id: str
    artifact_zip: bytes


def _nonblank(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise GitHubActionsError(f"{field} is invalid")
    return value


def _sha1(value: object, field: str) -> str:
    text = _nonblank(value, field)
    if _SHA1_RE.fullmatch(text) is None:
        raise GitHubActionsError(f"{field} is invalid")
    return text


def _repository(value: object) -> tuple[str, str, str]:
    text = _nonblank(value, "repository")
    if _REPOSITORY_RE.fullmatch(text) is None:
        raise GitHubActionsError("repository is invalid")
    owner, repo = text.split("/", 1)
    return text, owner, repo


def _json(response: httpx.Response, *, label: str) -> object:
    try:
        return response.json()
    except (ValueError, TypeError, binascii.Error) as exc:
        raise GitHubActionsError(f"GitHub returned invalid {label} JSON") from exc


def _safe_url(value: object, *, field: str = "url") -> str:
    text = _nonblank(value, field)
    try:
        parsed = httpx.URL(text)
    except Exception as exc:
        raise GitHubActionsError(f"{field} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubActionsError(f"{field} is invalid")
    return text


class _GitHubApi:
    """Small authenticated REST adapter with no token-bearing errors."""

    def __init__(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = _nonblank(token, "token")
        self.owner = _nonblank(owner, "owner")
        self.repo = _nonblank(repo, "repo")
        if any(character in self.owner for character in "/?#") or any(
            character in self.repo for character in "/?#"
        ):
            raise GitHubActionsError("GitHub owner or repo is invalid")
        try:
            parsed = httpx.URL(_nonblank(api_url, "api_url").rstrip("/"))
        except Exception as exc:
            raise GitHubActionsError("GitHub API URL is invalid") from exc
        if parsed.scheme != "https" or not parsed.host:
            raise GitHubActionsError("GitHub API URL must use HTTPS")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise GitHubActionsError("timeout is invalid")
        self.api_url = str(parsed).rstrip("/")
        self._client = httpx.Client(
            base_url=self.api_url,
            timeout=float(timeout),
            follow_redirects=True,
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

    def __enter__(self) -> "_GitHubApi":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        expected: set[int],
        json_payload: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                endpoint,
                json=dict(json_payload) if json_payload is not None else None,
                params=dict(params or {}),
            )
        except httpx.RequestError as exc:
            raise GitHubActionsError("GitHub API is unavailable", unknown=True) from exc
        if response.status_code not in expected:
            raise GitHubActionsError(
                f"GitHub API request failed (HTTP {response.status_code})",
                status_code=response.status_code,
                unknown=response.status_code >= 500,
            )
        if not binary and response.content and response.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return response
        return response

    def optional_get(self, endpoint: str, *, params: Mapping[str, str] | None = None) -> httpx.Response | None:
        try:
            return self.request("GET", endpoint, expected={200}, params=params)
        except GitHubActionsError as exc:
            if exc.status_code == 404:
                return None
            raise

    def repo_path(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"


def _ref_endpoint(ref: str) -> str:
    match = _BUNDLE_REF_RE.fullmatch(ref)
    if match is None:
        raise GitHubActionsError("temporary ref is invalid")
    return "/git/ref/heads/codex/evidence/" + match.group(1)


def _decode_base64(value: object, *, label: str) -> bytes:
    if type(value) is not str:
        raise GitHubActionsError(f"GitHub returned invalid {label} bytes")
    try:
        # GitHub's Git Blobs endpoint may wrap base64 at 60/76 columns.  The
        # Bundle itself remains strict canonical JSON; only this provider
        # response adapter accepts ASCII whitespace around the encoded bytes.
        normalized = "".join(value.split())
        return base64.b64decode(normalized, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise GitHubActionsError(f"GitHub returned invalid {label} bytes") from exc


def _object_data(verified: VerifiedEvidenceBundle, digest: object) -> bytes:
    if type(digest) is not str:
        raise GitHubActionsError("Bundle object reference is invalid")
    for item in verified.document["objects"]:
        if item.get("digest") == digest:
            return _decode_base64(item.get("data_base64"), label="Bundle object")
    raise GitHubActionsError("Bundle object reference is missing")


def _real_directory(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GitHubActionsError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise GitHubActionsError(f"{label} must be a real directory")
    return path.resolve(strict=True)


def _write_canonical(path: Path, value: Mapping[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise GitHubActionsError("CI output path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _validate_event_repository(repository: str, transport_head: str) -> None:
    event_path = (os.getenv("GITHUB_EVENT_PATH") or "").strip()
    if not event_path:
        return
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubActionsError("GitHub event payload is unavailable") from exc
    if not isinstance(event, Mapping):
        raise GitHubActionsError("GitHub event payload is invalid")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, Mapping):
        raise GitHubActionsError("Bundle import requires a pull_request event")
    head = pull_request.get("head")
    if not isinstance(head, Mapping) or head.get("sha") != transport_head:
        raise GitHubActionsError("transport head did not match pull_request event")
    event_repo = event.get("repository")
    full_name = event_repo.get("full_name") if isinstance(event_repo, Mapping) else None
    if full_name != repository:
        raise GitHubActionsError("GitHub event repository did not match Bundle")


def _is_ancestor(repository_root: Path, ancestor: str, descendant: str) -> None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", ancestor, descendant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubActionsError("transport ancestry could not be verified") from exc
    if completed.returncode != 0:
        raise GitHubActionsError("producer head is not an ancestor of transport head")


def _load_bundle_passport(verified: VerifiedEvidenceBundle) -> dict[str, Any]:
    data = _object_data(verified, verified.document["passport_object"])
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubActionsError("Bundle Passport is invalid") from exc
    if not isinstance(value, dict):
        raise GitHubActionsError("Bundle Passport is invalid")
    return value


def _imported_summary(
    *,
    bundle: Mapping[str, object],
    ci_run_id: str,
    check_marker: str,
) -> str:
    return "\n".join(
        (
            check_marker,
            "Imported authoritative case",
            "origin: local_authoritative_bundle",
            f"transport_id: {bundle['transport_id']}",
            f"ci_run_id: {ci_run_id}",
            f"producer_head: {bundle['producer_head']}",
            f"transport_head: {bundle['transport_head']}",
            f"Case: {bundle['case_id']}",
            f"Run: {bundle['run_id']}",
            f"Subject: {bundle['subject_digest']}",
            f"Passport digest: {bundle['passport_digest']}",
        )
    )


def _check_marker(bundle: Mapping[str, object]) -> str:
    return (
        f"codemesh-case:{bundle['case_id']};subject:{bundle['subject_digest']};"
        f"passport:{bundle['passport_digest']}"
    )


def _check_url(payload: Mapping[str, object]) -> str:
    value = payload.get("html_url")
    return _safe_url(value, field="check_url")


def _validate_imported_check(
    payload: object,
    *,
    bundle: Mapping[str, object],
    ci_run_id: str,
    expected_conclusion: str,
) -> tuple[int, str, str]:
    if not isinstance(payload, Mapping):
        raise GitHubActionsError("GitHub returned an invalid imported Check")
    check_id = payload.get("id")
    if type(check_id) is not int or check_id <= 0:
        raise GitHubActionsError("GitHub returned an invalid imported Check id")
    if payload.get("name") != _IMPORTED_CHECK_NAME:
        raise GitHubActionsError("imported Check name did not match")
    if payload.get("head_sha") != bundle["producer_head"]:
        raise GitHubActionsError("imported Check producer head did not match")
    if payload.get("status") != "completed" or payload.get("conclusion") != expected_conclusion:
        raise GitHubActionsError("imported Check conclusion did not match")
    output = payload.get("output")
    summary = output.get("summary") if isinstance(output, Mapping) else None
    marker = _check_marker(bundle)
    if type(summary) is not str or not summary.startswith(marker + "\nImported authoritative case\n"):
        raise GitHubActionsError("imported Check summary binding did not match")
    required_lines = (
        "origin: local_authoritative_bundle",
        f"transport_id: {bundle['transport_id']}",
        f"ci_run_id: {ci_run_id}",
        f"producer_head: {bundle['producer_head']}",
        f"transport_head: {bundle['transport_head']}",
        f"Case: {bundle['case_id']}",
        f"Run: {bundle['run_id']}",
        f"Subject: {bundle['subject_digest']}",
        f"Passport digest: {bundle['passport_digest']}",
    )
    if any(line not in summary.splitlines() for line in required_lines):
        raise GitHubActionsError("imported Check summary was incomplete")
    return check_id, _check_url(payload), str(payload["conclusion"])


def _validate_mapping_fields(
    value: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    for field, wanted in expected.items():
        if value.get(field) != wanted:
            raise GitHubActionsError(f"authoritative {label} {field} did not match Bundle")


def _materialize_import(
    verified: VerifiedEvidenceBundle,
    *,
    output_dir: Path,
    transport_ref_commit: str,
    ci_run_id: str,
    ci_job_id: str,
    run_attempt: int,
    check_id: int,
    check_url: str,
    conclusion: str,
) -> dict[str, Any]:
    output = output_dir
    if output.exists():
        _real_directory(output, label="CI output directory")
        if any(output.iterdir()):
            raise GitHubActionsError("CI output directory must be empty")
    else:
        output.mkdir(parents=True, exist_ok=False)
    document = verified.document
    case = json.loads(_object_data(verified, document["case_object"]).decode("utf-8"))
    run = json.loads(_object_data(verified, document["run_object"]).decode("utf-8"))
    passport = json.loads(_object_data(verified, document["passport_object"]).decode("utf-8"))
    lineage = copy.deepcopy(document["lineage"])
    if not isinstance(lineage, dict):
        raise GitHubActionsError("Bundle lineage is invalid")
    lineage.update(
        {
            "bundle_digest": verified.bundle_digest,
            "transport_ref_commit": transport_ref_commit,
            "ci_run_id": ci_run_id,
            "ci_job_id": ci_job_id,
            "run_attempt": run_attempt,
            "check_id": check_id,
            "check_url": check_url,
            "check_conclusion": conclusion,
        }
    )
    check = {
        "schema_version": "v1",
        "name": _IMPORTED_CHECK_NAME,
        "origin": document["origin"],
        "transport_id": document["transport_id"],
        "bundle_digest": verified.bundle_digest,
        "repository": document["repository"],
        "target_pr": document["target_pr"],
        "case_id": document["case_id"],
        "run_id": document["run_id"],
        "subject_digest": document["subject_digest"],
        "producer_head": document["producer_head"],
        "transport_head": document["transport_head"],
        "passport_digest": document["passport_digest"],
        "ci_run_id": ci_run_id,
        "check_id": check_id,
        "check_url": check_url,
        "status": "completed",
        "conclusion": conclusion,
    }
    workbench = {
        "schema_version": "v1",
        "view": "ci_authoritative_case_workbench",
        "origin": document["origin"],
        "transport_id": document["transport_id"],
        "bundle_digest": verified.bundle_digest,
        "repository": document["repository"],
        "target_pr": document["target_pr"],
        "case_id": document["case_id"],
        "run_id": document["run_id"],
        "subject_digest": document["subject_digest"],
        "passport_digest": document["passport_digest"],
        "producer_head": document["producer_head"],
        "transport_head": document["transport_head"],
        "transport_ref": document["transport_ref"],
        "transport_ref_commit": transport_ref_commit,
        "ci_run_id": ci_run_id,
        "ci_job_id": ci_job_id,
        "run_attempt": run_attempt,
        "object_closure": document["object_closure"],
        "lineage": lineage,
        "case": case,
        "run": run,
        "passport": passport,
        "evidence": document["evidence"],
        "check": check,
    }
    receipt = {
        "schema_version": "v1",
        "origin": document["origin"],
        "transport_id": document["transport_id"],
        "bundle_digest": verified.bundle_digest,
        "transport_ref": document["transport_ref"],
        "transport_ref_commit": transport_ref_commit,
        "transport_head": document["transport_head"],
        "producer_head": document["producer_head"],
        "repository": document["repository"],
        "target_pr": document["target_pr"],
        "case_id": document["case_id"],
        "run_id": document["run_id"],
        "subject_digest": document["subject_digest"],
        "passport_digest": document["passport_digest"],
        "ci_run_id": ci_run_id,
        "ci_job_id": ci_job_id,
        "run_attempt": run_attempt,
        "check_id": check_id,
        "check_url": check_url,
        "conclusion": conclusion,
        "assurance_run_invocations": 0,
        "provider_calls": 0,
        "case_writes": 0,
    }
    publication = {
        "schema_version": "v1",
        "origin": document["origin"],
        "transport_id": document["transport_id"],
        "bundle_digest": verified.bundle_digest,
        "repository": document["repository"],
        "target_pr": document["target_pr"],
        "transport_ref": document["transport_ref"],
        "transport_ref_commit": transport_ref_commit,
        "case_id": document["case_id"],
        "run_id": document["run_id"],
        "subject_digest": document["subject_digest"],
        "passport_digest": document["passport_digest"],
        "producer_head": document["producer_head"],
        "transport_head": document["transport_head"],
        "ci_run_id": ci_run_id,
        "ci_job_id": ci_job_id,
        "run_attempt": run_attempt,
        "check_id": check_id,
        "check_url": check_url,
        "conclusion": conclusion,
        "case_writes": 0,
        "provider_calls": 0,
        "assurance_run_invocations": 0,
    }
    _write_canonical(output / "case.json", case)
    _write_canonical(output / "run.json", run)
    _write_canonical(output / "passport.json", passport)
    passport_data = _object_data(verified, document["passport_markdown_object"])
    (output / "passport.md").write_bytes(passport_data)
    _write_canonical(output / "lineage.json", lineage)
    _write_canonical(output / "check.json", check)
    _write_canonical(output / "bundle-receipt.json", receipt)
    _write_canonical(output / "publication.json", publication)
    _write_canonical(output / "workbench.json", workbench)
    for item in document["objects"]:
        role = item["role"]
        if role == "evidence_index":
            name = PurePosixPath(str(item["name"]))
            if len(name.parts) != 2 or name.parts[0] != "evidence-index":
                raise GitHubActionsError("Bundle evidence index name is invalid")
            path = output / "evidence" / "index" / name.parts[1]
        elif role == "evidence_artifact":
            digest = str(item["digest"])
            path = output / "evidence" / "artifacts" / digest.removeprefix("sha256:")
        else:
            continue
        if path.exists() or path.is_symlink():
            raise GitHubActionsError("duplicate CI artifact output")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_decode_base64(item["data_base64"], label="Bundle object"))
    return workbench


def import_authoritative_bundle(
    bundle_bytes: bytes,
    *,
    output_dir: Path,
    repository_root: Path,
    token: str,
    repository: str,
    transport_head: str,
    transport_ref_commit: str,
    ci_run_id: str,
    ci_job_id: str,
    run_attempt: int,
    api_url: str = "https://api.github.com",
    api_transport: httpx.BaseTransport | None = None,
) -> RemotePublication:
    """Verify and materialize a Bundle, then publish the CI-only Check."""

    verified = verify_evidence_bundle(bundle_bytes)
    document = verified.document
    repo_text, owner, repo = _repository(repository)
    transport_head = _sha1(transport_head, "transport_head")
    transport_ref_commit = _sha1(transport_ref_commit, "transport_ref_commit")
    ci_run_id = _nonblank(ci_run_id, "ci_run_id")
    ci_job_id = _nonblank(ci_job_id, "ci_job_id")
    if type(run_attempt) is not int or run_attempt <= 0:
        raise GitHubActionsError("run_attempt is invalid")
    if repo_text != document["repository"] or transport_head != document["transport_head"]:
        raise GitHubActionsError("CI event binding did not match Bundle")
    _validate_event_repository(repo_text, transport_head)
    root = _real_directory(repository_root, label="checked-out repository")
    _is_ancestor(root, str(document["producer_head"]), transport_head)
    with _GitHubApi(
        token=token,
        owner=owner,
        repo=repo,
        api_url=api_url,
        transport=api_transport,
    ) as api:
        ref_response = api.request(
            "GET",
            api.repo_path(_ref_endpoint(str(document["transport_ref"]))),
            expected={200},
        )
        ref_payload = _json(ref_response, label="temporary ref readback")
        ref_object = ref_payload.get("object") if isinstance(ref_payload, Mapping) else None
        ref_sha = _sha1(
            ref_object.get("sha") if isinstance(ref_object, Mapping) else None,
            "temporary ref readback commit",
        )
        if ref_sha != transport_ref_commit:
            raise GitHubActionsError("temporary ref commit did not match CI input")
        target_pr = document["target_pr"]
        response = api.request(
            "GET",
            api.repo_path(f"/pulls/{target_pr}"),
            expected={200},
        )
        target = _json(response, label="pull request")
        if not isinstance(target, Mapping):
            raise GitHubActionsError("GitHub pull request response is invalid")
        head = target.get("head")
        head_repo = head.get("repo") if isinstance(head, Mapping) else None
        if (
            target.get("number") != target_pr
            or not isinstance(head, Mapping)
            or head.get("sha") != document["producer_head"]
            or not isinstance(head_repo, Mapping)
            or head_repo.get("full_name") != repo_text
        ):
            raise GitHubActionsError("target pull request head did not match producer_head")
        passport = _load_bundle_passport(verified)
        try:
            with GitHubCheckPublisher(
                token=token,
                api_url=api.api_url,
            ) as publisher:
                published = publisher.publish(
                    passport,
                    owner=owner,
                    repo=repo,
                    head_sha=str(document["producer_head"]),
                )
        except Exception as exc:
            raise GitHubActionsError("Actions Check publisher failed") from exc
        if published.passport_digest != document["passport_digest"]:
            raise GitHubActionsError("publisher Passport digest did not match Bundle")
        marker = _check_marker(document)
        summary = _imported_summary(bundle=document, ci_run_id=ci_run_id, check_marker=marker)
        update = api.request(
            "PATCH",
            api.repo_path(f"/check-runs/{published.check_id}"),
            expected={200},
            json_payload={
                "name": _IMPORTED_CHECK_NAME,
                "output": {
                    "title": _IMPORTED_CHECK_NAME,
                    "summary": summary,
                    "annotations": [],
                },
            },
        )
        _json(update, label="updated Check")
        detail = api.request(
            "GET",
            api.repo_path(f"/check-runs/{published.check_id}"),
            expected={200},
        )
        detail_payload = _json(detail, label="Check readback")
        check_id, check_url, conclusion = _validate_imported_check(
            detail_payload,
            bundle=document,
            ci_run_id=ci_run_id,
            expected_conclusion=published.conclusion,
        )
    workbench = _materialize_import(
        verified,
        output_dir=output_dir,
        transport_ref_commit=transport_ref_commit,
        ci_run_id=ci_run_id,
        ci_job_id=ci_job_id,
        run_attempt=run_attempt,
        check_id=check_id,
        check_url=check_url,
        conclusion=conclusion,
    )
    return RemotePublication(
        transport_ref=str(document["transport_ref"]),
        transport_ref_commit=transport_ref_commit,
        transport_head=transport_head,
        ci_run_id=ci_run_id,
        ci_job_id=ci_job_id,
        run_attempt=run_attempt,
        artifact_id="pending-upload",
        check_id=check_id,
        check_url=check_url,
        conclusion=conclusion,
        case_id=str(document["case_id"]),
        run_id=str(document["run_id"]),
        subject_digest=str(document["subject_digest"]),
        producer_head=str(document["producer_head"]),
        passport_digest=str(document["passport_digest"]),
        transport_id=str(document["transport_id"]),
        origin="local_authoritative_bundle",
        workbench=workbench,
    )


def _ref_matches(
    api: _GitHubApi,
    *,
    ref: str,
    bundle: BuiltEvidenceBundle,
) -> tuple[str, bool] | None:
    response = api.optional_get(api.repo_path(_ref_endpoint(ref)))
    if response is None:
        return None
    payload = _json(response, label="temporary ref")
    if not isinstance(payload, Mapping):
        raise GitHubActionsError("temporary ref response is invalid")
    obj = payload.get("object")
    commit_sha = obj.get("sha") if isinstance(obj, Mapping) else None
    commit_sha = _sha1(commit_sha, "temporary ref commit")
    commit_response = api.request(
        "GET",
        api.repo_path(f"/git/commits/{commit_sha}"),
        expected={200},
    )
    commit = _json(commit_response, label="temporary ref commit")
    tree = commit.get("tree") if isinstance(commit, Mapping) else None
    tree_sha = tree.get("sha") if isinstance(tree, Mapping) else None
    tree_sha = _sha1(tree_sha, "temporary ref tree")
    tree_response = api.request(
        "GET",
        api.repo_path(f"/git/trees/{tree_sha}"),
        expected={200},
        params={"recursive": "1"},
    )
    tree_payload = _json(tree_response, label="temporary ref tree")
    entries = tree_payload.get("tree") if isinstance(tree_payload, Mapping) else None
    if not isinstance(entries, list) or (isinstance(tree_payload, Mapping) and tree_payload.get("truncated")):
        raise GitHubActionsError("temporary ref tree is invalid")
    if len(entries) != 1 or not isinstance(entries[0], Mapping):
        raise IdempotencyConflict("temporary ref contains more than one file")
    entry = entries[0]
    if entry.get("path") != _BUNDLE_FILE or entry.get("type") != "blob":
        raise IdempotencyConflict("temporary ref does not contain only bundle.json")
    blob_sha = _sha1(entry.get("sha"), "temporary bundle blob")
    blob_response = api.request(
        "GET",
        api.repo_path(f"/git/blobs/{blob_sha}"),
        expected={200},
    )
    blob = _json(blob_response, label="temporary bundle blob")
    content = _decode_base64(blob.get("content") if isinstance(blob, Mapping) else None, label="temporary bundle")
    if content != bundle.bundle_bytes:
        raise IdempotencyConflict("temporary ref contains a different bundle")
    return commit_sha, True


class GitHubActionsTransport:
    """Remote-owned adapter for one temporary ref and one pull-request run."""

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        transport_branch: str = "codex/authoritative-publication",
        base_branch: str = "codex/local-acceptance-vertical",
        workflow_file: str = _WORKFLOW_FILE,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        poll_interval_seconds: float = 5.0,
        poll_timeout_seconds: float = 900.0,
        api_transport: httpx.BaseTransport | None = None,
    ) -> None:
        repo_text, owner, repo = _repository(repository)
        if not _BRANCH_RE.fullmatch(transport_branch) or not _BRANCH_RE.fullmatch(base_branch):
            raise GitHubActionsError("transport branch is invalid")
        if type(poll_interval_seconds) not in (int, float) or poll_interval_seconds <= 0:
            raise GitHubActionsError("poll interval is invalid")
        if type(poll_timeout_seconds) not in (int, float) or poll_timeout_seconds <= 0:
            raise GitHubActionsError("poll timeout is invalid")
        self.repository = repo_text
        self.owner = owner
        self.repo = repo
        self.transport_branch = transport_branch
        self.base_branch = base_branch
        self.workflow_file = _nonblank(workflow_file, "workflow_file")
        self._poll_interval = float(poll_interval_seconds)
        self._poll_timeout = float(poll_timeout_seconds)
        self._api = _GitHubApi(
            token=token,
            owner=owner,
            repo=repo,
            api_url=api_url,
            timeout=timeout,
            transport=api_transport,
        )
        self._active_ref: tuple[str, str] | None = None

    def close(self) -> None:
        self._api.close()

    def __enter__(self) -> "GitHubActionsTransport":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_ref(self, bundle: BuiltEvidenceBundle) -> _RemoteRef:
        ref = bundle.transport_ref
        existing = _ref_matches(self._api, ref=ref, bundle=bundle)
        if existing is not None:
            commit_sha, _ = existing
            return _RemoteRef(ref=ref, commit_sha=commit_sha, created=False)
        blob_response = self._api.request(
            "POST",
            self._api.repo_path("/git/blobs"),
            expected={201},
            json_payload={
                "content": base64.b64encode(bundle.bundle_bytes).decode("ascii"),
                "encoding": "base64",
            },
        )
        blob = _json(blob_response, label="Bundle blob")
        blob_sha = _sha1(blob.get("sha") if isinstance(blob, Mapping) else None, "Bundle blob")
        tree_response = self._api.request(
            "POST",
            self._api.repo_path("/git/trees"),
            expected={201},
            json_payload={
                "tree": [
                    {"path": _BUNDLE_FILE, "mode": "100644", "type": "blob", "sha": blob_sha}
                ]
            },
        )
        tree = _json(tree_response, label="Bundle tree")
        tree_sha = _sha1(tree.get("sha") if isinstance(tree, Mapping) else None, "Bundle tree")
        commit_response = self._api.request(
            "POST",
            self._api.repo_path("/git/commits"),
            expected={201},
            json_payload={
                "message": f"CodeMesh authoritative evidence {bundle.transport_id}",
                "tree": tree_sha,
                "parents": [],
            },
        )
        commit = _json(commit_response, label="Bundle commit")
        commit_sha = _sha1(commit.get("sha") if isinstance(commit, Mapping) else None, "Bundle commit")
        try:
            self._api.request(
                "POST",
                self._api.repo_path("/git/refs"),
                expected={201},
                json_payload={"ref": ref, "sha": commit_sha},
            )
        except GitHubActionsError as exc:
            raced = _ref_matches(self._api, ref=ref, bundle=bundle)
            if raced is not None:
                raise IdempotencyConflict("temporary ref was created by another publisher") from exc
            if exc.unknown:
                raise GitHubActionsError("temporary ref creation result is unknown", unknown=True) from exc
            raise
        readback = _ref_matches(self._api, ref=ref, bundle=bundle)
        if readback is None:
            raise GitHubActionsError("temporary ref readback was missing", unknown=True)
        readback_sha, _ = readback
        if readback_sha != commit_sha:
            raise IdempotencyConflict("temporary ref commit did not match created commit")
        return _RemoteRef(ref=ref, commit_sha=commit_sha, created=True)

    def _find_transport_pr(self, *, transport_head: str) -> Mapping[str, object]:
        response = self._api.request(
            "GET",
            self._api.repo_path("/pulls"),
            expected={200},
            params={
                "state": "open",
                "head": f"{self.owner}:{self.transport_branch}",
                "base": self.base_branch,
                "per_page": "100",
            },
        )
        payload = _json(response, label="transport pull request list")
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], Mapping):
            raise GitHubActionsError("exactly one open transport pull request is required")
        pr = payload[0]
        head = pr.get("head")
        base = pr.get("base")
        head_repo = head.get("repo") if isinstance(head, Mapping) else None
        if (
            type(pr.get("number")) is not int
            or pr["number"] <= 0
            or pr.get("state") != "open"
            or not isinstance(head, Mapping)
            or head.get("sha") != transport_head
            or not isinstance(head_repo, Mapping)
            or head_repo.get("full_name") != self.repository
            or not isinstance(base, Mapping)
            or base.get("ref") != self.base_branch
        ):
            raise GitHubActionsError("transport pull request binding did not match")
        return pr

    def _workflow_runs(self, *, transport_head: str) -> list[Mapping[str, object]]:
        response = self._api.request(
            "GET",
            self._api.repo_path(f"/actions/workflows/{quote(self.workflow_file, safe='')}/runs"),
            expected={200},
            params={
                "event": "pull_request",
                "branch": self.transport_branch,
                "per_page": "100",
            },
        )
        payload = _json(response, label="workflow run list")
        runs = payload.get("workflow_runs") if isinstance(payload, Mapping) else None
        if not isinstance(runs, list):
            raise GitHubActionsError("workflow run list is invalid")
        result = [
            run
            for run in runs
            if isinstance(run, Mapping) and run.get("head_sha") == transport_head
        ]
        result.sort(key=lambda run: int(run.get("id", 0)) if type(run.get("id")) is int else 0)
        return result

    def _run(self, run_id: str) -> Mapping[str, object]:
        response = self._api.request(
            "GET",
            self._api.repo_path(f"/actions/runs/{run_id}"),
            expected={200},
        )
        payload = _json(response, label="workflow run")
        if not isinstance(payload, Mapping):
            raise GitHubActionsError("workflow run response is invalid")
        return payload

    def _wait_for_run(
        self,
        *,
        initial_id: int,
        initial_attempt: int,
        transport_head: str,
    ) -> Mapping[str, object]:
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            candidates: list[Mapping[str, object]] = []
            current = self._run(str(initial_id))
            if (
                current.get("head_sha") == transport_head
                and current.get("head_branch") == self.transport_branch
                and current.get("event") == "pull_request"
                and type(current.get("run_attempt")) is int
                and current["run_attempt"] > initial_attempt
            ):
                candidates.append(current)
            if not candidates:
                runs = self._workflow_runs(transport_head=transport_head)
                candidates = [
                    run
                    for run in runs
                    if type(run.get("id")) is int
                    and (
                        run["id"] > initial_id
                        or (
                            run["id"] == initial_id
                            and type(run.get("run_attempt")) is int
                            and run["run_attempt"] > initial_attempt
                        )
                    )
                ]
            if candidates:
                candidate = candidates[-1]
                run_id = str(candidate["id"])
                current = self._run(run_id)
                if (
                    current.get("head_sha") != transport_head
                    or current.get("head_branch") != self.transport_branch
                    or current.get("event") != "pull_request"
                ):
                    raise GitHubActionsError("transport workflow result binding did not match")
                status = current.get("status")
                if status in {"queued", "in_progress", "waiting", "requested", "pending"}:
                    time.sleep(self._poll_interval)
                    continue
                if status != "completed" or current.get("conclusion") != "success":
                    raise GitHubActionsError("transport workflow did not succeed")
                return current
            time.sleep(self._poll_interval)
        raise GitHubActionsError("transport workflow result is unknown", unknown=True)

    def _rerun_and_wait(self, *, transport_head: str, transport_pr_number: object) -> Mapping[str, object]:
        runs = self._workflow_runs(transport_head=transport_head)
        if not runs:
            raise GitHubActionsError("initial transport workflow run is missing")
        matching = []
        for run in runs:
            pull_requests = run.get("pull_requests")
            if isinstance(pull_requests, list) and any(
                isinstance(item, Mapping) and item.get("number") == transport_pr_number
                for item in pull_requests
            ):
                matching.append(run)
            elif (
                not isinstance(pull_requests, list)
                and run.get("event") == "pull_request"
                and run.get("head_branch") == self.transport_branch
            ):
                matching.append(run)
        if not matching:
            raise GitHubActionsError("transport workflow run was not bound to transport PR")
        initial = matching[0]
        initial_id = initial.get("id")
        if type(initial_id) is not int or initial_id <= 0:
            raise GitHubActionsError("initial workflow run id is invalid")
        initial_attempt = initial.get("run_attempt")
        if type(initial_attempt) is not int or initial_attempt <= 0:
            initial_attempt = 1
        response = self._api.request(
            "POST",
            self._api.repo_path(f"/actions/runs/{initial_id}/rerun"),
            expected={201, 202},
        )
        return self._wait_for_run(
            initial_id=initial_id,
            initial_attempt=initial_attempt,
            transport_head=transport_head,
        )

    def _download_workflow_artifact(self, *, run: Mapping[str, object]) -> _WorkflowFacts:
        run_id = run.get("id")
        attempt = run.get("run_attempt")
        if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
            raise GitHubActionsError("workflow run facts are invalid")
        job_response = self._api.request(
            "GET",
            self._api.repo_path(f"/actions/runs/{run_id}/jobs"),
            expected={200},
            params={"per_page": "100"},
        )
        jobs_payload = _json(job_response, label="workflow jobs")
        jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, Mapping) else None
        if not isinstance(jobs, list):
            raise GitHubActionsError("workflow jobs response is invalid")
        assurance_jobs = [
            job for job in jobs if isinstance(job, Mapping) and job.get("name") == "assurance"
        ]
        if len(assurance_jobs) != 1 or assurance_jobs[0].get("conclusion") != "success":
            raise GitHubActionsError("assurance job did not complete successfully")
        job_id = assurance_jobs[0].get("id")
        if type(job_id) is not int or job_id <= 0:
            raise GitHubActionsError("assurance job id is invalid")
        artifacts_response = self._api.request(
            "GET",
            self._api.repo_path(f"/actions/runs/{run_id}/artifacts"),
            expected={200},
            params={"per_page": "100"},
        )
        artifacts_payload = _json(artifacts_response, label="workflow artifacts")
        artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, Mapping) else None
        if not isinstance(artifacts, list):
            raise GitHubActionsError("workflow artifacts response is invalid")
        name = f"codemesh-authoritative-case-{run_id}-{attempt}"
        matches = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, Mapping) and artifact.get("name") == name
        ]
        if len(matches) != 1 or matches[0].get("expired") is True:
            raise GitHubActionsError("authoritative Workbench artifact is missing")
        artifact_id = matches[0].get("id")
        if type(artifact_id) is not int or artifact_id <= 0:
            raise GitHubActionsError("authoritative artifact id is invalid")
        response = self._api.request(
            "GET",
            self._api.repo_path(f"/actions/artifacts/{artifact_id}/zip"),
            expected={200},
            binary=True,
        )
        if len(response.content) > _MAX_ZIP_BYTES:
            raise GitHubActionsError("authoritative Workbench artifact is too large")
        return _WorkflowFacts(
            run_id=str(run_id),
            job_id=str(job_id),
            run_attempt=attempt,
            artifact_id=str(artifact_id),
            artifact_zip=response.content,
        )

    @staticmethod
    def _zip_files(data: bytes) -> dict[str, bytes]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except (OSError, zipfile.BadZipFile) as exc:
            raise GitHubActionsError("authoritative Workbench artifact is not a valid ZIP") from exc
        files: dict[str, bytes] = {}
        total_uncompressed = 0
        try:
            for info in archive.infolist():
                name = info.filename
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or not name or name.endswith("/"):
                    raise GitHubActionsError("authoritative artifact contains an unsafe path")
                if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                    raise GitHubActionsError("authoritative artifact contains a symlink")
                if name in files:
                    raise GitHubActionsError("authoritative artifact contains duplicate paths")
                if info.file_size < 0 or total_uncompressed + info.file_size > _MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise GitHubActionsError("authoritative artifact is too large")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise GitHubActionsError("authoritative artifact entry size changed")
                total_uncompressed += len(data)
                files[name] = data
        finally:
            archive.close()
        return files

    def _verify_artifact(
        self,
        *,
        bundle: BuiltEvidenceBundle,
        workflow: _WorkflowFacts,
        transport_ref_commit: str,
    ) -> Mapping[str, Any]:
        verified = verify_evidence_bundle(bundle.bundle_bytes)
        files = self._zip_files(workflow.artifact_zip)
        required = {
            "case.json",
            "run.json",
            "passport.json",
            "passport.md",
            "lineage.json",
            "workbench.json",
            "check.json",
            "bundle-receipt.json",
            "publication.json",
        }
        if not required.issubset(files):
            raise GitHubActionsError("authoritative Workbench artifact is incomplete")
        expected_files = set(required)
        for item in verified.document["objects"]:
            role = item["role"]
            if role == "evidence_index":
                raw_name = PurePosixPath(str(item["name"]))
                expected_files.add("evidence/index/" + raw_name.name)
            elif role == "evidence_artifact":
                expected_files.add(
                    "evidence/artifacts/" + str(item["digest"]).removeprefix("sha256:")
                )
        if set(files) != expected_files:
            raise GitHubActionsError("authoritative Workbench artifact file closure did not match Bundle")
        expected_direct = {
            "case.json": _object_data(verified, verified.document["case_object"]),
            "run.json": _object_data(verified, verified.document["run_object"]),
            "passport.json": _object_data(verified, verified.document["passport_object"]),
            "passport.md": _object_data(verified, verified.document["passport_markdown_object"]),
        }
        for name, expected in expected_direct.items():
            if files[name] != expected:
                raise GitHubActionsError(f"authoritative artifact {name} did not match Bundle")
        try:
            receipt = json.loads(files["bundle-receipt.json"])
            workbench = json.loads(files["workbench.json"])
            check = json.loads(files["check.json"])
            lineage = json.loads(files["lineage.json"])
            publication = json.loads(files["publication.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubActionsError("authoritative Workbench JSON is invalid") from exc
        if not all(
            isinstance(value, Mapping)
            for value in (receipt, workbench, check, lineage, publication)
        ):
            raise GitHubActionsError("authoritative Workbench JSON is invalid")
        bindings = {
            "origin": "local_authoritative_bundle",
            "transport_id": bundle.transport_id,
            "bundle_digest": bundle.bundle_digest,
            "transport_ref": bundle.transport_ref,
            "transport_head": bundle.transport_head,
            "producer_head": bundle.producer_head,
            "repository": self.repository,
            "case_id": bundle.case_id,
            "run_id": bundle.run_id,
            "subject_digest": bundle.subject_digest,
            "passport_digest": bundle.passport_digest,
        }
        # Keep the verified Bundle document as the source for the complete
        # projection, including the target PR binding.
        target_pr = verified.document["target_pr"]
        bindings["target_pr"] = target_pr
        _validate_mapping_fields(workbench, bindings, label="Workbench")
        _validate_mapping_fields(receipt, bindings, label="Bundle receipt")
        runtime_bindings = {
            "transport_ref_commit": transport_ref_commit,
            "ci_run_id": workflow.run_id,
            "ci_job_id": workflow.job_id,
            "run_attempt": workflow.run_attempt,
        }
        _validate_mapping_fields(workbench, runtime_bindings, label="Workbench")
        _validate_mapping_fields(receipt, runtime_bindings, label="Bundle receipt")
        _validate_mapping_fields(
            lineage,
            {
                **bindings,
                "object_closure": verified.document["object_closure"],
                **runtime_bindings,
            },
            label="lineage",
        )
        _validate_mapping_fields(
            publication,
            {
                **bindings,
                **runtime_bindings,
            },
            label="publication receipt",
        )
        if workbench.get("lineage") != lineage or workbench.get("check") != check:
            raise GitHubActionsError("authoritative Workbench nested projections did not match")
        if workbench.get("evidence") != verified.document["evidence"]:
            raise GitHubActionsError("authoritative Workbench evidence did not match Bundle")
        if (
            check.get("schema_version") != "v1"
            or check.get("name") != _IMPORTED_CHECK_NAME
            or check.get("origin") != "local_authoritative_bundle"
            or check.get("transport_id") != bundle.transport_id
            or check.get("bundle_digest") != bundle.bundle_digest
            or check.get("repository") != self.repository
            or check.get("target_pr") != target_pr
            or check.get("case_id") != bundle.case_id
            or check.get("run_id") != bundle.run_id
            or check.get("subject_digest") != bundle.subject_digest
            or check.get("producer_head") != bundle.producer_head
            or check.get("transport_head") != bundle.transport_head
            or check.get("passport_digest") != bundle.passport_digest
            or check.get("ci_run_id") != workflow.run_id
            or type(check.get("check_id")) is not int
            or check.get("check_id") <= 0
            or check.get("status") != "completed"
            or type(check.get("conclusion")) is not str
            or not check["conclusion"].strip()
        ):
            raise GitHubActionsError("authoritative Check artifact binding did not match")
        if _safe_url(check.get("check_url"), field="check_url") != check.get("check_url"):
            raise GitHubActionsError("authoritative Check URL did not match")
        for item in verified.document["objects"]:
            role = item["role"]
            if role == "evidence_index":
                raw_name = PurePosixPath(str(item["name"]))
                name = "evidence/index/" + raw_name.name
            elif role == "evidence_artifact":
                name = "evidence/artifacts/" + str(item["digest"]).removeprefix("sha256:")
            else:
                continue
            expected = _decode_base64(item["data_base64"], label="Bundle object")
            if files.get(name) != expected:
                raise GitHubActionsError(f"authoritative artifact {name} did not match Bundle")
        return workbench

    def publish(
        self,
        *,
        bundle: BuiltEvidenceBundle,
        target_pr: int,
        producer_head: str,
        transport_head: str,
    ) -> RemotePublication:
        if (
            target_pr <= 0
            or target_pr != bundle.target_pr
            or producer_head != bundle.producer_head
            or transport_head != bundle.transport_head
        ):
            raise GitHubActionsError("publication inputs did not match Bundle")
        ref = self._ensure_ref(bundle)
        self._active_ref = (ref.ref, ref.commit_sha)
        transport_pr = self._find_transport_pr(transport_head=transport_head)
        transport_pr_number = transport_pr.get("number")
        if type(transport_pr_number) is not int or transport_pr_number <= 0:
            raise GitHubActionsError("transport pull request number is invalid")
        run = self._rerun_and_wait(
            transport_head=transport_head,
            transport_pr_number=transport_pr_number,
        )
        workflow = self._download_workflow_artifact(run=run)
        workbench = self._verify_artifact(
            bundle=bundle,
            workflow=workflow,
            transport_ref_commit=ref.commit_sha,
        )
        check_id = workbench.get("check", {}).get("check_id") if isinstance(workbench.get("check"), Mapping) else None
        if type(check_id) is not int or check_id <= 0:
            raise GitHubActionsError("Workbench Check id is invalid")
        check_response = self._api.request(
            "GET",
            self._api.repo_path(f"/check-runs/{check_id}"),
            expected={200},
        )
        check_payload = _json(check_response, label="product Check")
        if not isinstance(check_payload, Mapping):
            raise GitHubActionsError("product Check readback is invalid")
        check_url = _check_url(check_payload)
        if (
            check_payload.get("name") != _IMPORTED_CHECK_NAME
            or check_payload.get("head_sha") != bundle.producer_head
            or check_payload.get("status") != "completed"
            or check_payload.get("conclusion") != workbench.get("check", {}).get("conclusion")
        ):
            raise GitHubActionsError("product Check readback did not match Workbench")
        return RemotePublication(
            transport_ref=bundle.transport_ref,
            transport_ref_commit=ref.commit_sha,
            transport_head=transport_head,
            ci_run_id=workflow.run_id,
            ci_job_id=workflow.job_id,
            run_attempt=workflow.run_attempt,
            artifact_id=workflow.artifact_id,
            check_id=check_id,
            check_url=check_url,
            conclusion=str(check_payload["conclusion"]),
            case_id=bundle.case_id,
            run_id=bundle.run_id,
            subject_digest=bundle.subject_digest,
            producer_head=bundle.producer_head,
            passport_digest=bundle.passport_digest,
            transport_id=bundle.transport_id,
            origin="local_authoritative_bundle",
            workbench=workbench,
            cleanup_allowed=ref.created,
        )

    def cleanup(self, *, ref: str, commit_sha: str) -> None:
        if self._active_ref != (ref, commit_sha):
            raise GitHubActionsError("cleanup target was not created by this transport")
        try:
            response = self._api.request(
                "DELETE",
                self._api.repo_path(_ref_endpoint(ref)),
                expected={204},
            )
        except GitHubActionsError as exc:
            if not exc.unknown:
                raise
            # A timeout/5xx after DELETE is not success and must not be
            # retried blindly.  A 404 readback proves the exact ref is gone;
            # anything else remains unconfirmed for an operator to inspect.
            if self._api.optional_get(self._api.repo_path(_ref_endpoint(ref))) is None:
                self._active_ref = None
                return
            raise GitHubActionsError("temporary ref cleanup result is unknown", unknown=True) from exc
        if response.content:
            raise GitHubActionsError("temporary ref deletion returned unexpected content")
        if self._api.optional_get(self._api.repo_path(_ref_endpoint(ref))) is not None:
            raise GitHubActionsError("temporary ref still exists after cleanup")
        self._active_ref = None


def import_from_environment(
    *,
    bundle_path: Path,
    output_dir: Path,
    repository_root: Path,
    token: str,
    transport_head: str,
    transport_ref_commit: str,
    ci_run_id: str,
    ci_job_id: str,
    run_attempt: int,
    repository: str | None = None,
    api_url: str = "https://api.github.com",
) -> RemotePublication:
    repo_text = repository or (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if not repo_text:
        raise GitHubActionsError("GITHUB_REPOSITORY is missing")
    try:
        bundle_bytes = bundle_path.read_bytes()
    except OSError as exc:
        raise GitHubActionsError("Bundle file is unavailable") from exc
    return import_authoritative_bundle(
        bundle_bytes,
        output_dir=output_dir,
        repository_root=repository_root,
        token=token,
        repository=repo_text,
        transport_head=transport_head,
        transport_ref_commit=transport_ref_commit,
        ci_run_id=ci_run_id,
        ci_job_id=ci_job_id,
        run_attempt=run_attempt,
        api_url=api_url,
    )


def _cli_main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m assurance.integrations.github_actions")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--repository")
    parser.add_argument("--transport-head", required=True)
    parser.add_argument("--transport-ref-commit", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-job-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--api-url", default="https://api.github.com")
    args = parser.parse_args(argv)
    token = (os.getenv(args.token_env) or "").strip()
    if not token:
        print("authoritative bundle import failed", file=os.sys.stderr)
        return 1
    try:
        result = import_from_environment(
            bundle_path=Path(args.bundle),
            output_dir=Path(args.output_dir),
            repository_root=Path(args.repository_root),
            token=token,
            repository=args.repository,
            transport_head=args.transport_head,
            transport_ref_commit=args.transport_ref_commit,
            ci_run_id=args.ci_run_id,
            ci_job_id=args.ci_job_id,
            run_attempt=args.run_attempt,
            api_url=args.api_url,
        )
    except Exception:
        print("authoritative bundle import failed", file=os.sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema_version": "v1",
                "origin": result.origin,
                "transport_id": result.transport_id,
                "case_id": result.case_id,
                "run_id": result.run_id,
                "subject_digest": result.subject_digest,
                "producer_head": result.producer_head,
                "transport_head": result.transport_head,
                "ci_run_id": result.ci_run_id,
                "ci_job_id": result.ci_job_id,
                "check_id": result.check_id,
                "check_url": result.check_url,
                "conclusion": result.conclusion,
                "case_writes": 0,
                "provider_calls": 0,
                "assurance_run_invocations": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())


__all__ = [
    "GitHubActionsError",
    "GitHubActionsTransport",
    "import_authoritative_bundle",
    "import_from_environment",
]
