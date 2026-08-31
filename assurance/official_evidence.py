"""Remote, typed import of the authoritative P-C GitHub Actions artifact.

The caller supplies only a positive GitHub Actions run id.  This module reads
the run, job, artifact metadata, and artifact ZIP from GitHub's read-only API,
then binds the two typed reports to the exact local Git head without writing
anything into the repository.  A local path is never accepted as official
evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence
from .digests import normalize_repo_path, normalize_repository_identity


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_REPORT_BYTES = 512 * 1024
_MAX_RESULT_BYTES = 8 * 1024 * 1024
_MAX_ZIP_BYTES = 4 * 1024 * 1024
_MAX_ZIP_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_FILES = 32
_MAX_PATH_BYTES = 512
_MAX_ID_BYTES = 256
_MAX_GITHUB_JSON_BYTES = 512 * 1024
_GITHUB_WORKFLOW_NAME = "P-C Handover Experience"
_GITHUB_WORKFLOW_PATH = ".github/workflows/p-c-handover.yml"
_GITHUB_JOB_NAME = "handover"
_GITHUB_API_URL = "https://api.github.com"
_OFFICIAL_ARTIFACT_PREFIX = "p-c-official-validation-"
_EXPECTED_ZIP_FILES = frozenset(
    {
        "dependency_audit.json",
        "ci_iac_validation.json",
        "dependency-audit-result.json",
        "ci-iac-result.json",
    }
)
_SAFE_PROVENANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_CREDENTIAL_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "ghp_",
    "github_pat_",
)
_SUCCESS_VALUES = frozenset({"success", "completed", "passed"})
_REQUIRED_CI_CHECKS = {
    "checkout": frozenset({"checkout", "exact_checkout", "exact-checkout"}),
    "install": frozenset({"install", "dependency_install", "dependency-install"}),
    "focused_checks": frozenset(
        {"focused_checks", "focused-checks", "focused checks", "checks"}
    ),
    "build": frozenset({"build", "focused_build", "focused-build"}),
    "browser_walkthrough": frozenset(
        {"browser_walkthrough", "browser-walkthrough", "browser walkthrough"}
    ),
}

OFFICIAL_EVIDENCE_KINDS = ("dependency_audit", "ci_iac_validation")
OFFICIAL_PRODUCERS = {
    "dependency_audit": "collector.dependency_audit",
    "ci_iac_validation": "collector.ci_iac_validation",
}
OFFICIAL_EVIDENCE_REASON_CODES = (
    "credential_missing_or_invalid",
    "github_transport",
    "lineage_mismatch",
    "artifact_structure_invalid",
    "digest_or_size_mismatch",
    "unknown",
)
_OFFICIAL_EVIDENCE_REASON_SET = frozenset(OFFICIAL_EVIDENCE_REASON_CODES)


def _normalize_reason_code(value: object) -> str:
    if type(value) is str and value in _OFFICIAL_EVIDENCE_REASON_SET:
        return value
    return "unknown"


class OfficialEvidenceError(ValueError):
    """Stable path-free error for every official evidence failure."""

    message = "official evidence import failed"

    def __init__(
        self,
        *_args: object,
        reason_code: object = None,
        reason: object = None,
    ) -> None:
        candidate = reason_code if reason_code is not None else reason
        if candidate is None and len(_args) == 1:
            candidate = _args[0]
        self.reason_code = _normalize_reason_code(candidate)
        super().__init__()

    def __str__(self) -> str:
        return self.message

    @property
    def reason(self) -> str:
        """Compatibility view over the single internal failure reason."""

        return self.reason_code


def _error(reason_code: object = None) -> OfficialEvidenceError:
    return OfficialEvidenceError(reason_code=reason_code)


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _error() from None


def _validate_digest(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("invalid sha256 digest")
    return value


def _validate_nonblank(value: object, *, max_bytes: int = _MAX_ID_BYTES) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("value must be nonblank")
    if any(
        (ord(character) < 32 and character not in "\t")
        or ord(character) == 127
        or 128 <= ord(character) <= 159
        for character in value
    ):
        raise ValueError("value contains control bytes")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("value is too long")
    return value


def _validate_provenance_id(value: object) -> str:
    text = _validate_nonblank(value)
    lowered = text.lower()
    if (
        _SAFE_PROVENANCE_ID_RE.fullmatch(text) is None
        or any(marker in lowered for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValueError("provenance ID is invalid")
    return text


def _validate_run_id(value: object) -> str:
    if type(value) is not str or _RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("run id must be a positive numeric string")
    return value


def _validate_revision(value: object) -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        raise ValueError("head revision must be a full lowercase SHA")
    return value


def _validate_repo_identity(value: object) -> str:
    try:
        normalized = normalize_repository_identity(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("repository identity is invalid") from exc
    if normalized != value or not normalized.strip():
        raise ValueError("repository identity is invalid")
    lowered = normalized.lower()
    parsed = urlsplit(normalized)
    if (
        "@" in normalized
        or normalized.startswith(("/", "\\", "~", "./", "../"))
        or lowered.startswith("file:")
        or "?" in normalized
        or "#" in normalized
        or parsed.scheme
        or parsed.netloc
        or any(marker in lowered for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValueError("repository identity is not logical")
    parts = normalized.split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("repository identity must be owner/repository")
    return normalized


def _validate_repo_relative_path(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("path must be nonblank")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError("path is too long")
    lowered = value.lower()
    if (
        "\x00" in value
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        or lowered.startswith(("file:", "~", "/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value) is not None
        or "\\\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("path must be repository-relative")
    try:
        normalized = normalize_repo_path(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("path must be repository-relative") from exc
    if normalized != value:
        raise ValueError("path must be canonical")
    return value


class OfficialEvidenceSource(BaseModel):
    """One tracked Git blob whose exact bytes were used by the workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    path: str
    digest: str
    byte_size: StrictInt = Field(ge=0, le=_MAX_RESULT_BYTES)

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("digest", mode="before")
    @classmethod
    def _digest(cls, value: object) -> str:
        return _validate_digest(value)


class OfficialEvidenceCheck(BaseModel):
    """One named successful workflow check in a CI report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    name: str = Field(min_length=1, max_length=128)
    status: Literal[
        "success",
        "completed",
        "passed",
        "failure",
        "error",
        "timeout",
        "cancelled",
        "unknown",
    ]
    conclusion: Literal[
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "unknown",
    ]

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        return _validate_nonblank(value, max_bytes=128)

    @model_validator(mode="after")
    def _status_conclusion(self) -> "OfficialEvidenceCheck":
        if self.status not in _SUCCESS_VALUES or self.conclusion != "success":
            raise ValueError("official check did not succeed")
        return self


class OfficialEvidenceReport(BaseModel):
    """Typed report emitted inside the official workflow artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    kind: Literal["dependency_audit", "ci_iac_validation"]
    repository_identity: str
    head_revision: str
    # A report is emitted only by workflow_dispatch after the caller supplies
    # the runtime subject for this exact checkout.
    subject_digest: str
    producer: str
    source_paths: tuple[OfficialEvidenceSource, ...] = Field(
        min_length=1, max_length=_MAX_SOURCE_FILES
    )
    workflow_name: str
    workflow_path: str
    event: Literal["workflow_dispatch"]
    pull_request_number: StrictInt = Field(gt=0)
    workflow_run_id: str
    workflow_run_attempt: StrictInt = Field(gt=0)
    # The workflow can know the stable job name.  The importer records the
    # provider's numeric job id in the trusted receipt.
    job_id: str
    job_name: str
    status: Literal[
        "success",
        "completed",
        "passed",
        "failure",
        "error",
        "timeout",
        "cancelled",
        "truncated",
        "unknown",
    ]
    conclusion: Literal[
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "unknown",
    ]
    result_path: str
    result_digest: str
    result_byte_size: StrictInt = Field(ge=0, le=_MAX_RESULT_BYTES)
    checks: tuple[OfficialEvidenceCheck, ...] = Field(default=(), max_length=16)
    evidence_mode: Literal["official"] = "official"
    audit_command: str | None = None

    @field_validator("repository_identity", mode="before")
    @classmethod
    def _repository(cls, value: object) -> str:
        return _validate_repo_identity(value)

    @field_validator("head_revision", mode="before")
    @classmethod
    def _head(cls, value: object) -> str:
        return _validate_revision(value)

    @field_validator("subject_digest", mode="before")
    @classmethod
    def _subject(cls, value: object) -> str:
        return _validate_digest(value)

    @field_validator("producer", "workflow_name", "job_name", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        return _validate_nonblank(value)

    @field_validator("workflow_path", "result_path", mode="before")
    @classmethod
    def _path(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("workflow_run_id", "job_id", mode="before")
    @classmethod
    def _provenance(cls, value: object) -> str:
        return _validate_provenance_id(value)

    @field_validator("workflow_run_id", mode="after")
    @classmethod
    def _run_id(cls, value: str) -> str:
        return _validate_run_id(value)

    @field_validator("result_digest", mode="before")
    @classmethod
    def _result_digest(cls, value: object) -> str:
        return _validate_digest(value)

    @field_validator("audit_command", mode="before")
    @classmethod
    def _audit_command(cls, value: object) -> str | None:
        return None if value is None else _validate_nonblank(value, max_bytes=1024)

    @model_validator(mode="after")
    def _report_contract(self) -> "OfficialEvidenceReport":
        if self.producer != OFFICIAL_PRODUCERS[self.kind]:
            raise ValueError("official producer does not match kind")
        if self.workflow_name != _GITHUB_WORKFLOW_NAME:
            raise ValueError("report workflow name does not match P-C")
        if self.workflow_path != _GITHUB_WORKFLOW_PATH:
            raise ValueError("report workflow path does not match P-C")
        if self.job_name != _GITHUB_JOB_NAME or self.job_id != _GITHUB_JOB_NAME:
            raise ValueError("report job does not match P-C")
        source_paths = [item.path for item in self.source_paths]
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("official source paths must be unique")
        if self.result_path in source_paths:
            raise ValueError("result path must be distinct from source paths")
        if self.kind == "dependency_audit":
            if self.audit_command != "pnpm audit --prod --audit-level=high --json":
                raise ValueError("dependency report must record the audit command")
            if self.checks:
                raise ValueError("dependency report must not contain CI checks")
            if self.result_path != "dependency-audit-result.json":
                raise ValueError("dependency result path is invalid")
        else:
            if self.audit_command is not None:
                raise ValueError("CI report must not contain an audit command")
            if _GITHUB_WORKFLOW_PATH not in source_paths:
                raise ValueError("CI report must bind the P-C workflow source")
            if self.result_path != "ci-iac-result.json":
                raise ValueError("CI result path is invalid")
            names = {item.name.strip().lower().replace("_", "-") for item in self.checks}
            if len(names) != len(self.checks):
                raise ValueError("CI report check names must be unique")
            for required, aliases in _REQUIRED_CI_CHECKS.items():
                aliases = {alias.replace("_", "-") for alias in aliases}
                if not any(alias in names for alias in aliases):
                    raise ValueError(f"CI report is missing {required}")
        if self.status not in _SUCCESS_VALUES or self.conclusion != "success":
            raise ValueError("official report did not succeed")
        return self


class OfficialEvidenceReceipt(BaseModel):
    """Trusted, subject-bound receipt persisted as the Evidence artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    kind: Literal["dependency_audit", "ci_iac_validation"]
    subject_digest: str
    repository_identity: str
    head_revision: str
    producer: str
    source_paths: tuple[OfficialEvidenceSource, ...]
    workflow_name: str
    workflow_path: str
    event: Literal["workflow_dispatch"]
    pull_request_number: StrictInt = Field(gt=0)
    workflow_run_id: str
    workflow_run_attempt: StrictInt = Field(gt=0)
    job_id: str
    job_name: str
    artifact_id: str
    artifact_name: str
    artifact_digest: str
    artifact_byte_size: StrictInt = Field(gt=0, le=_MAX_ZIP_BYTES)
    report_digest: str
    report_byte_size: StrictInt = Field(ge=0, le=_MAX_REPORT_BYTES)
    result_path: str
    result_digest: str
    result_byte_size: StrictInt = Field(ge=0, le=_MAX_RESULT_BYTES)
    report: OfficialEvidenceReport
    result: dict[str, Any] | list[Any]
    evidence_mode: Literal["official"] = "official"

    @field_validator("subject_digest", "producer", "repository_identity", mode="before")
    @classmethod
    def _required_text(cls, value: object, info) -> str:
        if info.field_name == "subject_digest":
            return _validate_digest(value)
        if info.field_name == "repository_identity":
            return _validate_repo_identity(value)
        return _validate_nonblank(value)

    @field_validator("head_revision", mode="before")
    @classmethod
    def _head(cls, value: object) -> str:
        return _validate_revision(value)

    @field_validator("source_paths")
    @classmethod
    def _source_paths(cls, value: tuple[OfficialEvidenceSource, ...]) -> tuple[OfficialEvidenceSource, ...]:
        if len({item.path for item in value}) != len(value):
            raise ValueError("receipt source paths must be unique")
        return value

    @field_validator("workflow_name", "job_name", "artifact_name", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        return _validate_nonblank(value)

    @field_validator("workflow_path", "result_path", mode="before")
    @classmethod
    def _path(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("job_id", "artifact_id", mode="before")
    @classmethod
    def _ids(cls, value: object) -> str:
        return _validate_provenance_id(value)

    @field_validator("workflow_run_id", mode="before")
    @classmethod
    def _run_id(cls, value: object) -> str:
        return _validate_run_id(value)

    @field_validator("artifact_digest", "report_digest", "result_digest", mode="before")
    @classmethod
    def _digests(cls, value: object) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def _bind_report(self) -> "OfficialEvidenceReceipt":
        report = self.report
        if (
            report.kind != self.kind
            or report.repository_identity != self.repository_identity
            or report.head_revision != self.head_revision
            or report.producer != self.producer
            or report.source_paths != self.source_paths
            or report.workflow_name != self.workflow_name
            or report.workflow_path != self.workflow_path
            or report.event != self.event
            or report.pull_request_number != self.pull_request_number
            or report.workflow_run_id != self.workflow_run_id
            or report.workflow_run_attempt != self.workflow_run_attempt
            or report.job_name != self.job_name
            or report.result_path != self.result_path
            or report.result_digest != self.result_digest
            or report.result_byte_size != self.result_byte_size
        ):
            raise ValueError("receipt report binding does not match")
        if report.subject_digest != self.subject_digest:
            raise ValueError("receipt report subject does not match")
        if (
            self.workflow_name != _GITHUB_WORKFLOW_NAME
            or self.workflow_path != _GITHUB_WORKFLOW_PATH
            or self.job_name != _GITHUB_JOB_NAME
        ):
            raise ValueError("receipt provenance does not match P-C")
        if self.artifact_name != _OFFICIAL_ARTIFACT_PREFIX + self.workflow_run_id:
            raise ValueError("receipt artifact name does not match run")
        if self.result_path in {item.path for item in self.source_paths}:
            raise ValueError("receipt result path overlaps source")
        return self


@dataclass(frozen=True)
class OfficialEvidenceImport:
    """Private immutable remote bytes plus the public Evidence result."""

    receipt: OfficialEvidenceReceipt
    evidence: Evidence
    receipt_bytes: bytes
    receipt_digest: str
    receipt_byte_size: int
    remote_zip_bytes: bytes
    remote_zip_digest: str
    remote_zip_byte_size: int
    source_bindings: tuple[OfficialEvidenceSource, ...]

    @property
    def report(self) -> OfficialEvidenceReport:
        """Compatibility view over the trusted receipt's typed report."""

        return self.receipt.report


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_json(data: bytes, *, max_bytes: int) -> object:
    if type(data) is not bytes or len(data) > max_bytes:
        raise _error()
    try:
        return json.loads(
            data.decode("utf-8", errors="strict"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise _error() from None


def _parse_report_bytes(data: bytes) -> OfficialEvidenceReport:
    try:
        raw = _parse_json(data, max_bytes=_MAX_REPORT_BYTES)
    except OfficialEvidenceError:
        raise _error("artifact_structure_invalid") from None
    if not isinstance(raw, dict):
        raise _error("artifact_structure_invalid")
    if isinstance(raw.get("checks"), dict):
        checks = []
        for name, status in raw["checks"].items():
            if type(name) is not str:
                raise _error("artifact_structure_invalid")
            if isinstance(status, dict):
                if "name" in status:
                    raise _error("artifact_structure_invalid")
                check = dict(status)
                check["name"] = name
                checks.append(check)
            else:
                checks.append({"name": name, "status": status, "conclusion": status})
        raw = dict(raw)
        raw["checks"] = checks
    try:
        return OfficialEvidenceReport.model_validate(raw)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise _error("artifact_structure_invalid") from None


def _parse_result_bytes(data: bytes) -> dict[str, Any] | list[Any]:
    try:
        raw = _parse_json(data, max_bytes=_MAX_RESULT_BYTES)
    except OfficialEvidenceError:
        raise _error("artifact_structure_invalid") from None
    if not isinstance(raw, (dict, list)):
        raise _error("artifact_structure_invalid")
    return raw


def parse_official_evidence_report(data: bytes) -> OfficialEvidenceReport:
    """Parse a report from a trusted receipt/artifact byte string."""

    return _parse_report_bytes(data)


def parse_official_evidence_receipt(data: bytes) -> OfficialEvidenceReceipt:
    """Parse the subject-bound canonical Evidence artifact."""

    try:
        raw = _parse_json(data, max_bytes=_MAX_REPORT_BYTES + _MAX_RESULT_BYTES)
    except OfficialEvidenceError:
        raise _error("artifact_structure_invalid") from None
    try:
        return OfficialEvidenceReceipt.model_validate(raw)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise _error("artifact_structure_invalid") from None


def _validate_api_url(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("GitHub API URL is invalid")
    try:
        parsed = httpx.URL(value.rstrip("/"))
    except Exception as exc:
        raise ValueError("GitHub API URL is invalid") from exc
    if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password:
        raise ValueError("GitHub API URL must use HTTPS")
    return str(parsed).rstrip("/")


def _zip_files(data: bytes) -> dict[str, bytes]:
    if type(data) is not bytes or len(data) > _MAX_ZIP_BYTES:
        raise _error("artifact_structure_invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile):
        raise _error("artifact_structure_invalid") from None
    files: dict[str, bytes] = {}
    total_uncompressed = 0
    try:
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            mode = (info.external_attr >> 16) & 0xFFFF
            if (
                not name
                or "\\" in name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or name.endswith("/")
                or name in files
                or info.flag_bits & 0x1
                or stat.S_ISLNK(mode)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or info.file_size < 0
                or info.compress_size < 0
                or total_uncompressed + info.file_size > _MAX_ZIP_UNCOMPRESSED_BYTES
            ):
                raise _error("artifact_structure_invalid")
            content = archive.read(info)
            if len(content) != info.file_size:
                raise _error("artifact_structure_invalid")
            total_uncompressed += len(content)
            files[name] = content
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        raise _error("artifact_structure_invalid") from None
    finally:
        archive.close()
    if set(files) != _EXPECTED_ZIP_FILES:
        raise _error("artifact_structure_invalid")
    return files


@dataclass(frozen=True)
class _RemoteRun:
    run_id: str
    repository_identity: str
    workflow_name: str
    workflow_path: str
    event: Literal["workflow_dispatch"]
    pull_request_number: int
    head_ref: str
    head_revision: str
    run_attempt: int
    job_id: str
    job_name: str
    artifact_id: str
    artifact_name: str
    artifact_digest: str
    artifact_byte_size: int


class OfficialEvidenceImporter:
    """Read one exact GitHub run and bind its official artifact in memory."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        repository_path: Path,
        repository_identity: str,
        head_revision: str,
        subject_digest: str,
        artifact_store: ArtifactStore,
        collected_at: datetime,
        github_token: str | None = None,
        github_api_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            raise TypeError("workspace_root must be an absolute pathlib.Path")
        if not isinstance(repository_path, Path) or not repository_path.is_absolute():
            raise TypeError("repository_path must be an absolute pathlib.Path")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        try:
            root_stat = workspace_root.lstat()
            repo_stat = repository_path.lstat()
            if (
                stat.S_ISLNK(root_stat.st_mode)
                or not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(repo_stat.st_mode)
                or not stat.S_ISDIR(repo_stat.st_mode)
            ):
                raise ValueError
            self._workspace_root = workspace_root.resolve(strict=True)
            self._repository_path = repository_path.resolve(strict=True)
            if os.path.commonpath(
                (str(self._workspace_root), str(self._repository_path))
            ) != str(self._workspace_root):
                raise ValueError
            store_root = Path(artifact_store.root).resolve(strict=False)
            if os.path.commonpath(
                (str(self._repository_path), str(store_root))
            ) == str(self._repository_path):
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise ValueError("official evidence roots are invalid") from None
        self._repository_identity = _validate_repo_identity(repository_identity)
        self._head_revision = _validate_revision(head_revision)
        self._subject_digest = _validate_digest(subject_digest)
        if (
            not isinstance(collected_at, datetime)
            or collected_at.tzinfo is None
            or collected_at.utcoffset() is None
        ):
            raise TypeError("collected_at must be timezone-aware")
        if github_token is not None:
            try:
                github_token = _validate_nonblank(github_token, max_bytes=512)
            except (TypeError, ValueError):
                raise _error("credential_missing_or_invalid") from None
        self._github_token = github_token
        self._github_api_url = _validate_api_url(
            github_api_url or _GITHUB_API_URL
        )
        if self._github_api_url != _GITHUB_API_URL:
            if transport is None or type(transport) is not httpx.MockTransport:
                raise ValueError("custom GitHub API origins are test-only")
        self._transport = transport
        self._artifact_store = artifact_store
        self._collected_at = collected_at

    def import_run(self, official_evidence_run_id: str) -> tuple[OfficialEvidenceImport, ...]:
        """Fetch and verify exactly one completed P-C run; never retry."""

        try:
            run_id = _validate_run_id(official_evidence_run_id)
        except (TypeError, ValueError):
            raise _error("unknown") from None
        token = self._github_token or os.getenv("GITHUB_TOKEN", "")
        if type(token) is not str or not token.strip():
            raise _error("credential_missing_or_invalid")
        try:
            _validate_nonblank(token, max_bytes=512)
        except (TypeError, ValueError):
            raise _error("credential_missing_or_invalid") from None
        owner, repo = self._repository_identity.split("/", 1)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codemesh-assurance",
        }
        try:
            with httpx.Client(
                base_url=self._github_api_url,
                headers=headers,
                timeout=20.0,
                follow_redirects=False,
                max_redirects=3,
                transport=self._transport,
            ) as client:
                run_payload = self._request_json(
                    client,
                    f"/repos/{owner}/{repo}/actions/runs/{run_id}",
                )
                run = self._verify_run(run_payload, run_id)
                jobs_payload = self._request_json(
                    client,
                    f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                    params={"per_page": "100"},
                )
                job = self._verify_job(jobs_payload, run)
                artifact_payload = self._request_json(
                    client,
                    f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
                    params={"per_page": "100"},
                )
                artifact = self._verify_artifact_metadata(artifact_payload, run, job)
                zip_bytes = self._download_artifact(
                    client,
                    f"/repos/{owner}/{repo}/actions/artifacts/{artifact.artifact_id}/zip",
                    artifact,
                )
        except OfficialEvidenceError:
            raise
        except httpx.HTTPError:
            raise _error("github_transport") from None
        except (OSError, TypeError, ValueError, ValidationError, RuntimeError):
            raise _error("unknown") from None

        try:
            files = _zip_files(zip_bytes)
        except OfficialEvidenceError:
            raise
        except Exception:
            raise _error("unknown") from None
        reports: dict[str, OfficialEvidenceReport] = {}
        for filename in ("dependency_audit.json", "ci_iac_validation.json"):
            try:
                report = _parse_report_bytes(files[filename])
            except OfficialEvidenceError:
                raise
            except Exception:
                raise _error("unknown") from None
            expected_kind = (
                "dependency_audit"
                if filename == "dependency_audit.json"
                else "ci_iac_validation"
            )
            if report.kind != expected_kind:
                raise _error("artifact_structure_invalid")
            if report.kind in reports:
                raise _error("artifact_structure_invalid")
            reports[report.kind] = report
        if set(reports) != set(OFFICIAL_EVIDENCE_KINDS):
            raise _error("artifact_structure_invalid")

        pull_request_numbers = {report.pull_request_number for report in reports.values()}
        if len(pull_request_numbers) != 1:
            raise _error("lineage_mismatch")
        pull_request_number = next(iter(pull_request_numbers))
        try:
            with httpx.Client(
                base_url=self._github_api_url,
                headers=headers,
                timeout=20.0,
                follow_redirects=False,
                max_redirects=3,
                transport=self._transport,
            ) as client:
                pull_request_payload = self._request_json(
                    client,
                    f"/repos/{owner}/{repo}/pulls/{pull_request_number}",
                )
        except OfficialEvidenceError:
            raise
        except httpx.HTTPError:
            raise _error("github_transport") from None
        except (OSError, TypeError, ValueError, ValidationError, RuntimeError):
            raise _error("unknown") from None
        try:
            run = self._verify_pull_request(
                pull_request_payload, artifact, pull_request_number
            )
        except OfficialEvidenceError:
            raise
        except (TypeError, ValueError, ValidationError):
            raise _error("unknown") from None
        for report in reports.values():
            self._verify_report_claims(report, run, job)
            expected_result = (
                "dependency-audit-result.json"
                if report.kind == "dependency_audit"
                else "ci-iac-result.json"
            )
            if report.result_path != expected_result:
                raise _error("artifact_structure_invalid")
            result_bytes = files[report.result_path]
            if (
                len(result_bytes) != report.result_byte_size
                or _digest_bytes(result_bytes) != report.result_digest
            ):
                raise _error("digest_or_size_mismatch")
            _parse_result_bytes(result_bytes)
            for source in report.source_paths:
                source_bytes = self._git_blob(source.path)
                if len(source_bytes) != source.byte_size or _digest_bytes(source_bytes) != source.digest:
                    raise _error("digest_or_size_mismatch")

        zip_digest = _digest_bytes(zip_bytes)
        self._store_verified_bytes(zip_bytes, zip_digest)
        imports: list[OfficialEvidenceImport] = []
        for kind in OFFICIAL_EVIDENCE_KINDS:
            result_name = (
                "dependency-audit-result.json"
                if kind == "dependency_audit"
                else "ci-iac-result.json"
            )
            try:
                imported = self._build_import(
                    report=reports[kind],
                    report_bytes=files[
                        "dependency_audit.json"
                        if kind == "dependency_audit"
                        else "ci_iac_validation.json"
                    ],
                    result_value=_parse_result_bytes(files[result_name]),
                    run=run,
                    zip_digest=zip_digest,
                    zip_byte_size=len(zip_bytes),
                    zip_bytes=zip_bytes,
                )
            except OfficialEvidenceError:
                raise
            except (OSError, TypeError, ValueError, ValidationError, RecursionError, RuntimeError):
                raise _error("unknown") from None
            imports.append(imported)
        return tuple(imports)

    def verify_import(self, imported: OfficialEvidenceImport) -> None:
        """Final local fence: re-read CAS and exact Git blobs, never the network."""

        if type(imported) is not OfficialEvidenceImport:
            raise _error("unknown")
        if imported.receipt.subject_digest != self._subject_digest:
            raise _error("lineage_mismatch")
        self._verify_stored_bytes(imported.receipt_digest, imported.receipt_bytes)
        self._verify_stored_bytes(imported.remote_zip_digest, imported.remote_zip_bytes)
        if parse_official_evidence_receipt(imported.receipt_bytes) != imported.receipt:
            raise _error("artifact_structure_invalid")
        try:
            current_head = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=self._repository_path,
                check=True,
                capture_output=True,
                timeout=5.0,
            ).stdout.strip().decode("ascii")
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            raise _error("lineage_mismatch") from None
        if current_head != self._head_revision:
            raise _error("lineage_mismatch")
        for source in imported.source_bindings:
            source_bytes = self._git_blob(source.path)
            if len(source_bytes) != source.byte_size or _digest_bytes(source_bytes) != source.digest:
                raise _error("digest_or_size_mismatch")
        if imported.evidence.artifact_digest != imported.receipt_digest:
            raise _error("digest_or_size_mismatch")
        if imported.evidence.trust_level != "observed":
            raise _error("artifact_structure_invalid")

    @classmethod
    def _request_json(
        cls,
        client: httpx.Client,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> object:
        try:
            with client.stream("GET", endpoint, params=dict(params or {})) as response:
                if response.status_code in {401, 403}:
                    raise _error("credential_missing_or_invalid")
                if response.status_code != 200:
                    raise _error("github_transport")
                content = cls._read_bounded_response(
                    response, max_bytes=_MAX_GITHUB_JSON_BYTES
                )
        except OfficialEvidenceError:
            raise
        except httpx.HTTPError:
            raise _error("github_transport") from None
        except (OSError, ValueError):
            raise _error("unknown") from None
        try:
            return _parse_json(content, max_bytes=_MAX_GITHUB_JSON_BYTES)
        except OfficialEvidenceError:
            raise _error("github_transport") from None

    @staticmethod
    def _read_bounded_response(
        response: httpx.Response, *, max_bytes: int, exact_bytes: int | None = None
    ) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                raise _error(
                    "digest_or_size_mismatch"
                    if exact_bytes is not None
                    else "github_transport"
                ) from None
            if declared < 0 or declared > max_bytes or (
                exact_bytes is not None and declared != exact_bytes
            ):
                raise _error(
                    "digest_or_size_mismatch"
                    if exact_bytes is not None
                    else "github_transport"
                )
        data = bytearray()
        try:
            for chunk in response.iter_bytes():
                if type(chunk) is not bytes:
                    raise _error("github_transport")
                if len(data) + len(chunk) > max_bytes:
                    raise _error(
                        "digest_or_size_mismatch"
                        if exact_bytes is not None
                        else "github_transport"
                    )
                data.extend(chunk)
        except OfficialEvidenceError:
            raise
        except (OSError, RuntimeError, httpx.HTTPError):
            raise _error("github_transport") from None
        content = bytes(data)
        if exact_bytes is not None and len(content) != exact_bytes:
            raise _error("digest_or_size_mismatch")
        return content

    def _download_artifact(
        self, client: httpx.Client, endpoint: str, artifact: _RemoteRun
    ) -> bytes:
        try:
            with client.stream("GET", endpoint, follow_redirects=True) as response:
                if response.status_code in {401, 403}:
                    raise _error("credential_missing_or_invalid")
                if response.status_code != 200:
                    raise _error("github_transport")
                content = self._read_bounded_response(
                    response,
                    max_bytes=_MAX_ZIP_BYTES,
                    exact_bytes=artifact.artifact_byte_size,
                )
        except OfficialEvidenceError:
            raise
        except (httpx.HTTPError, OSError, ValueError):
            raise _error("github_transport") from None
        if _digest_bytes(content) != artifact.artifact_digest:
            raise _error("digest_or_size_mismatch")
        return content

    def _store_verified_bytes(self, data: bytes, digest: str) -> None:
        try:
            if self._artifact_store.put_bytes(data) != digest:
                raise _error("digest_or_size_mismatch")
            if self._artifact_store.verify(digest) is not True:
                raise _error("digest_or_size_mismatch")
            if self._artifact_store.get_bytes(digest) != data:
                raise _error("digest_or_size_mismatch")
        except OfficialEvidenceError:
            raise
        except (OSError, TypeError, ValueError):
            raise _error("unknown") from None

    def _verify_stored_bytes(self, digest: str, expected: bytes) -> None:
        try:
            if self._artifact_store.verify(digest) is not True:
                raise _error("digest_or_size_mismatch")
            if self._artifact_store.get_bytes(digest) != expected:
                raise _error("digest_or_size_mismatch")
        except OfficialEvidenceError:
            raise
        except (OSError, TypeError, ValueError):
            raise _error("unknown") from None

    def _verify_run(self, payload: object, run_id: str) -> _RemoteRun:
        if not isinstance(payload, Mapping):
            raise _error("lineage_mismatch")
        if type(payload.get("id")) is not int or payload.get("id") != int(run_id):
            raise _error("lineage_mismatch")
        repository = payload.get("repository")
        head_repository = payload.get("head_repository")
        if not isinstance(repository, Mapping) or repository.get("full_name") != self._repository_identity:
            raise _error("lineage_mismatch")
        if not isinstance(head_repository, Mapping) or head_repository.get("full_name") != self._repository_identity:
            raise _error("lineage_mismatch")
        if (
            payload.get("name") != _GITHUB_WORKFLOW_NAME
            or payload.get("path") != _GITHUB_WORKFLOW_PATH
            or payload.get("event") != "workflow_dispatch"
            or payload.get("status") != "completed"
            or payload.get("conclusion") != "success"
        ):
            raise _error("lineage_mismatch")
        if payload.get("head_sha") != self._head_revision:
            raise _error("lineage_mismatch")
        head_ref = payload.get("head_branch")
        if type(head_ref) is not str or not head_ref.strip():
            raise _error("lineage_mismatch")
        try:
            _validate_nonblank(head_ref, max_bytes=_MAX_PATH_BYTES)
        except (TypeError, ValueError):
            raise _error("lineage_mismatch") from None
        attempt = payload.get("run_attempt")
        if type(attempt) is not int or attempt <= 0:
            raise _error("lineage_mismatch")
        return _RemoteRun(
            run_id=run_id,
            repository_identity=self._repository_identity,
            workflow_name=_GITHUB_WORKFLOW_NAME,
            workflow_path=_GITHUB_WORKFLOW_PATH,
            event="workflow_dispatch",
            pull_request_number=0,
            head_ref=head_ref,
            head_revision=self._head_revision,
            run_attempt=attempt,
            job_id="",
            job_name=_GITHUB_JOB_NAME,
            artifact_id="",
            artifact_name="",
            artifact_digest="",
            artifact_byte_size=0,
        )

    @staticmethod
    def _verify_job(payload: object, run: _RemoteRun) -> Mapping[str, object]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise _error("lineage_mismatch")
        matches = [
            job for job in payload["jobs"]
            if isinstance(job, Mapping) and job.get("name") == _GITHUB_JOB_NAME
        ]
        if len(matches) != 1:
            raise _error("lineage_mismatch")
        job = matches[0]
        if (
            type(job.get("id")) is not int
            or job["id"] <= 0
            or job.get("status") != "completed"
            or job.get("conclusion") != "success"
        ):
            raise _error("lineage_mismatch")
        job_run_id = job.get("run_id")
        if type(job_run_id) is not int or job_run_id != int(run.run_id):
            raise _error("lineage_mismatch")
        return job

    def _verify_artifact_metadata(
        self,
        payload: object,
        run: _RemoteRun,
        job: Mapping[str, object],
    ) -> _RemoteRun:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("artifacts"), list):
            raise _error("lineage_mismatch")
        name = _OFFICIAL_ARTIFACT_PREFIX + run.run_id
        matches = [
            item for item in payload["artifacts"]
            if isinstance(item, Mapping) and item.get("name") == name
        ]
        if len(matches) != 1:
            raise _error("lineage_mismatch")
        artifact = matches[0]
        if artifact.get("expired") is not False:
            raise _error("lineage_mismatch")
        artifact_run = artifact.get("workflow_run")
        if (
            not isinstance(artifact_run, Mapping)
            or type(artifact_run.get("id")) is not int
            or artifact_run.get("id") != int(run.run_id)
        ):
            raise _error("lineage_mismatch")
        artifact_id = artifact.get("id")
        size = artifact.get("size_in_bytes")
        digest = artifact.get("digest")
        if type(artifact_id) is not int or artifact_id <= 0:
            raise _error("lineage_mismatch")
        if (
            type(size) is not int
            or size <= 0
            or size > _MAX_ZIP_BYTES
        ):
            raise _error("digest_or_size_mismatch")
        if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise _error("digest_or_size_mismatch")
        return _RemoteRun(
            run_id=run.run_id,
            repository_identity=run.repository_identity,
            workflow_name=run.workflow_name,
            workflow_path=run.workflow_path,
            event=run.event,
            pull_request_number=run.pull_request_number,
            head_ref=run.head_ref,
            head_revision=run.head_revision,
            run_attempt=run.run_attempt,
            job_id=str(job["id"]),
            job_name=str(job["name"]),
            artifact_id=str(artifact_id),
            artifact_name=name,
            artifact_digest=digest,
            artifact_byte_size=size,
        )

    def _verify_pull_request(
        self,
        payload: object,
        run: _RemoteRun,
        pull_request_number: int,
    ) -> _RemoteRun:
        if not isinstance(payload, Mapping) or type(payload.get("number")) is not int:
            raise _error("lineage_mismatch")
        if payload.get("number") != pull_request_number or payload.get("state") != "open":
            raise _error("lineage_mismatch")
        base = payload.get("base")
        head = payload.get("head")
        if not isinstance(base, Mapping) or not isinstance(head, Mapping):
            raise _error("lineage_mismatch")
        base_repo = base.get("repo")
        head_repo = head.get("repo")
        if (
            not isinstance(base_repo, Mapping)
            or base_repo.get("full_name") != self._repository_identity
            or not isinstance(head_repo, Mapping)
            or head_repo.get("full_name") != self._repository_identity
            or head.get("sha") != self._head_revision
        ):
            raise _error("lineage_mismatch")
        base_ref = base.get("ref")
        base_sha = base.get("sha")
        head_ref = head.get("ref")
        if (
            type(base_ref) is not str
            or not base_ref.strip()
            or type(head_ref) is not str
            or not head_ref.strip()
            or head_ref != run.head_ref
        ):
            raise _error("lineage_mismatch")
        try:
            _validate_nonblank(base_ref, max_bytes=_MAX_PATH_BYTES)
            _validate_nonblank(head_ref, max_bytes=_MAX_PATH_BYTES)
        except (TypeError, ValueError):
            raise _error("lineage_mismatch") from None
        if type(base_sha) is not str or _REVISION_RE.fullmatch(base_sha) is None:
            raise _error("lineage_mismatch")
        return _RemoteRun(
            run_id=run.run_id,
            repository_identity=run.repository_identity,
            workflow_name=run.workflow_name,
            workflow_path=run.workflow_path,
            event=run.event,
            pull_request_number=pull_request_number,
            head_ref=run.head_ref,
            head_revision=run.head_revision,
            run_attempt=run.run_attempt,
            job_id=run.job_id,
            job_name=run.job_name,
            artifact_id=run.artifact_id,
            artifact_name=run.artifact_name,
            artifact_digest=run.artifact_digest,
            artifact_byte_size=run.artifact_byte_size,
        )

    def _verify_report_claims(
        self,
        report: OfficialEvidenceReport,
        run: _RemoteRun,
        job: Mapping[str, object],
    ) -> None:
        if (
            report.repository_identity != run.repository_identity
            or report.head_revision != run.head_revision
            or report.workflow_name != run.workflow_name
            or report.workflow_path != run.workflow_path
            or report.event != run.event
            or (
                run.pull_request_number != 0
                and report.pull_request_number != run.pull_request_number
            )
            or report.workflow_run_id != run.run_id
            or report.workflow_run_attempt != run.run_attempt
            or report.subject_digest != self._subject_digest
            or report.job_name != job.get("name")
            or report.job_id not in {str(job["id"]), str(job["name"])}
            or report.status not in _SUCCESS_VALUES
            or report.conclusion != "success"
        ):
            raise _error("lineage_mismatch")

    def _git_blob(self, relative_path: str) -> bytes:
        try:
            if _validate_repo_relative_path(relative_path) != relative_path:
                raise _error("artifact_structure_invalid")
        except OfficialEvidenceError:
            raise
        except (TypeError, ValueError):
            raise _error("artifact_structure_invalid") from None
        try:
            tree = subprocess.run(
                ("git", "ls-tree", "-z", self._head_revision, "--", relative_path),
                cwd=self._repository_path,
                check=True,
                capture_output=True,
                timeout=5.0,
            ).stdout
            entries = tree.split(b"\x00")
            if len(entries) != 2 or not entries[0]:
                raise _error("artifact_structure_invalid")
            header, path = entries[0].split(b"\t", 1)
            mode, object_type, object_id = header.split(b" ")
            if path.decode("utf-8", errors="strict") != relative_path:
                raise _error("artifact_structure_invalid")
            if object_type != b"blob" or mode not in {b"100644", b"100755"}:
                raise _error("artifact_structure_invalid")
            if re.fullmatch(rb"[0-9a-f]{40,64}", object_id) is None:
                raise _error("artifact_structure_invalid")
            blob = subprocess.run(
                ("git", "cat-file", "blob", object_id.decode("ascii")),
                cwd=self._repository_path,
                check=True,
                capture_output=True,
                timeout=5.0,
            ).stdout
            if len(blob) > _MAX_RESULT_BYTES:
                raise _error("digest_or_size_mismatch")
            return blob
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
            raise _error("artifact_structure_invalid") from None

    def _build_import(
        self,
        *,
        report: OfficialEvidenceReport,
        report_bytes: bytes,
        result_value: dict[str, Any] | list[Any],
        run: _RemoteRun,
        zip_digest: str,
        zip_byte_size: int,
        zip_bytes: bytes,
    ) -> OfficialEvidenceImport:
        receipt_payload = {
            "schema_version": "v1",
            "kind": report.kind,
            "subject_digest": self._subject_digest,
            "repository_identity": run.repository_identity,
            "head_revision": run.head_revision,
            "producer": report.producer,
            "source_paths": [item.model_dump(mode="json") for item in report.source_paths],
            "workflow_name": run.workflow_name,
            "workflow_path": run.workflow_path,
            "event": run.event,
            "pull_request_number": run.pull_request_number,
            "workflow_run_id": run.run_id,
            "workflow_run_attempt": run.run_attempt,
            "job_id": run.job_id,
            "job_name": run.job_name,
            "artifact_id": run.artifact_id,
            "artifact_name": run.artifact_name,
            "artifact_digest": zip_digest,
            "artifact_byte_size": zip_byte_size,
            "report_digest": _digest_bytes(report_bytes),
            "report_byte_size": len(report_bytes),
            "result_path": report.result_path,
            "result_digest": report.result_digest,
            "result_byte_size": report.result_byte_size,
            "report": report.model_dump(mode="json"),
            "result": result_value,
            "evidence_mode": "official",
        }
        try:
            receipt = OfficialEvidenceReceipt.model_validate(receipt_payload)
            receipt_bytes = _canonical_json(receipt.model_dump(mode="json"))
            receipt = OfficialEvidenceReceipt.model_validate(
                _parse_json(receipt_bytes, max_bytes=_MAX_REPORT_BYTES + _MAX_RESULT_BYTES)
            )
        except (TypeError, ValueError, ValidationError, RecursionError):
            raise _error("unknown") from None
        receipt_digest = _digest_bytes(receipt_bytes)
        self._store_verified_bytes(receipt_bytes, receipt_digest)
        evidence = Evidence(
            evidence_id="ev_official_"
            + hashlib.sha256((report.kind + ":" + receipt_digest).encode("utf-8")).hexdigest()[:32],
            subject_digest=self._subject_digest,
            kind=report.kind,
            producer=report.producer,
            artifact_digest=receipt_digest,
            source_ref=(
                f"github:official:{report.kind}:run:{run.run_id}:"
                f"artifact:{run.artifact_id}:success"
            ),
            trace_id=f"github:{run.run_id}:{run.run_attempt}:{run.job_id}",
            status="success",
            trust_level="observed",
            collected_at=self._collected_at,
        )
        return OfficialEvidenceImport(
            receipt=receipt,
            evidence=evidence,
            receipt_bytes=receipt_bytes,
            receipt_digest=receipt_digest,
            receipt_byte_size=len(receipt_bytes),
            remote_zip_bytes=zip_bytes,
            remote_zip_digest=zip_digest,
            remote_zip_byte_size=zip_byte_size,
            source_bindings=report.source_paths,
        )


def import_official_evidence(
    official_evidence_run_id: str,
    *,
    workspace_root: Path,
    repository_path: Path,
    repository_identity: str,
    head_revision: str,
    subject_digest: str,
    artifact_store: ArtifactStore,
    collected_at: datetime,
    github_token: str | None = None,
    github_api_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[OfficialEvidenceImport, ...]:
    """Functional wrapper for the bounded remote run import."""

    importer = OfficialEvidenceImporter(
        workspace_root=workspace_root,
        repository_path=repository_path,
        repository_identity=repository_identity,
        head_revision=head_revision,
        subject_digest=subject_digest,
        artifact_store=artifact_store,
        collected_at=collected_at,
        github_token=github_token,
        github_api_url=github_api_url,
        transport=transport,
    )
    return importer.import_run(official_evidence_run_id)


__all__ = [
    "OFFICIAL_EVIDENCE_KINDS",
    "OFFICIAL_PRODUCERS",
    "OFFICIAL_EVIDENCE_REASON_CODES",
    "OfficialEvidenceCheck",
    "OfficialEvidenceError",
    "OfficialEvidenceImport",
    "OfficialEvidenceImporter",
    "OfficialEvidenceReceipt",
    "OfficialEvidenceReport",
    "OfficialEvidenceSource",
    "import_official_evidence",
    "parse_official_evidence_receipt",
    "parse_official_evidence_report",
]
