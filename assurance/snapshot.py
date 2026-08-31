"""Git 工作区只读快照收集器（V2-P2-01B）。"""

import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence
from .digests import (
    SubjectDigestInput,
    compute_subject_digest,
    normalize_repo_path,
    normalize_repository_identity,
)


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_READ_CHUNK_SIZE = 64 * 1024
_MAX_METADATA_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_IGNORED_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 4096
_DEFAULT_MAX_DIFF_BYTES = 1_048_576
_GIT_PREFIX = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
)
_DIFF_TRUNCATION_MARKER = b"\n=== CODEMESH GIT SNAPSHOT DIFF TRUNCATED ===\n"
_OMISSION_ORDER = (
    "diff_truncated",
    "files_truncated",
    "large_file",
    "submodule",
    "unmerged",
    "ignored_scan_truncated",
    "concurrent_change",
)
_RENAME_STATUSES = frozenset({"renamed", "copied"})


class GitSnapshotError(Exception):
    """Base error for Git snapshot collection failures."""


class GitRepositoryError(GitSnapshotError):
    """Raised when the repository path or base revision is invalid."""


class GitCommandError(GitSnapshotError):
    """Raised when a read-only Git command fails or violates bounds."""


class GitWorktreeChangedError(GitSnapshotError):
    """Raised when the worktree changes during collection (P2-01B)."""


@dataclass(frozen=True)
class _BoundedCommandResult:
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    returncode: int


class _BoundedPipeReader(threading.Thread):
    """Drain one pipe concurrently while retaining at most limit bytes."""

    def __init__(self, stream, limit: int, on_cap) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._on_cap = on_cap
        self.data = b""
        self.truncated = False
        self.error: BaseException | None = None

    def run(self) -> None:
        collected = bytearray()
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                if self.truncated:
                    continue
                remaining = self._limit - len(collected)
                if remaining <= 0:
                    self.truncated = True
                    self._call_cap()
                    continue
                take = min(len(chunk), remaining)
                collected += chunk[:take]
                if take < len(chunk):
                    self.truncated = True
                    self._call_cap()
        except BaseException as exc:
            self.error = exc
        finally:
            self.data = bytes(collected)

    def _call_cap(self) -> None:
        if self._on_cap is None:
            return
        try:
            self._on_cap()
        except BaseException as exc:
            self.error = exc


def _require_positive_int(value, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _require_positive_finite_float(value, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be an exact float")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    return value


class GitChange(BaseModel):
    """一次 Git 工作区变更的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    path: str
    old_path: str | None = None
    status: Literal[
        "added",
        "modified",
        "deleted",
        "renamed",
        "copied",
        "type_changed",
        "unmerged",
        "untracked",
    ]
    current_size: int | None = Field(default=None, strict=True, ge=0)
    current_digest: str | None = None
    binary: bool = Field(default=False, strict=True)
    large_file: bool = Field(default=False, strict=True)
    submodule: bool = Field(default=False, strict=True)

    @field_validator("path", mode="before")
    @classmethod
    def _canonical_path(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a str")
        normalized = normalize_repo_path(value)
        if normalized != value:
            raise ValueError("path must be canonical")
        return value

    @field_validator("old_path", mode="before")
    @classmethod
    def _canonical_old_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("old_path must be a str or None")
        normalized = normalize_repo_path(value)
        if normalized != value:
            raise ValueError("old_path must be canonical")
        return value

    @field_validator("current_digest")
    @classmethod
    def _validate_current_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "GitChange":
        if self.status == "deleted":
            if self.current_size is not None or self.current_digest is not None:
                raise ValueError(
                    "deleted changes require current_size and current_digest None"
                )
            if self.binary or self.large_file or self.submodule:
                raise ValueError(
                    "deleted changes must not set binary/large_file/submodule"
                )
        if self.status in _RENAME_STATUSES:
            if self.old_path is None:
                raise ValueError("old_path is required for renamed/copied changes")
        elif self.old_path is not None:
            raise ValueError(
                "old_path is prohibited for non-renamed/copied changes"
            )
        if self.submodule:
            if self.current_digest is not None:
                raise ValueError("submodule changes must not include a digest")
        if self.large_file and self.submodule:
            raise ValueError("large_file and submodule are mutually exclusive")
        if (
            self.status not in ("deleted", "unmerged")
            and not self.large_file
            and not self.submodule
        ):
            if self.current_size is None or self.current_digest is None:
                raise ValueError(
                    "normal existing files require current_size and current_digest"
                )
        return self


class GitSnapshot(BaseModel):
    """一个 base 提交到当前工作区的不可变 Git 快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    repository: str
    base_revision: str
    head_revision: str
    scope: Literal["base_to_worktree"] = "base_to_worktree"
    worktree_dirty: bool = Field(strict=True)
    changes: tuple[GitChange, ...] = ()
    changed_files_total: int = Field(strict=True, ge=0)
    diff_artifact_digest: str
    diff_bytes: int = Field(strict=True, ge=0)
    diff_truncated: bool = Field(strict=True)
    files_truncated: bool = Field(strict=True)
    ignored_files_lower_bound: int = Field(strict=True, ge=0)
    ignored_scan_truncated: bool = Field(strict=True)
    large_file_paths: tuple[str, ...] = ()
    submodule_paths: tuple[str, ...] = ()
    omissions: tuple[
        Literal[
            "diff_truncated",
            "files_truncated",
            "large_file",
            "submodule",
            "unmerged",
            "ignored_scan_truncated",
            "concurrent_change",
        ],
        ...,
    ] = ()
    complete: bool = Field(strict=True)
    collected_at: AwareDatetime

    @field_validator("subject_digest", "diff_artifact_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("repository", mode="before")
    @classmethod
    def _canonical_repository(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("repository must be a str")
        normalized = normalize_repository_identity(value)
        if normalized != value:
            raise ValueError("repository must be canonical")
        return value

    @field_validator("base_revision", "head_revision")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        if _FULL_SHA_RE.fullmatch(value) is None:
            raise ValueError("revision must be full lowercase 40 or 64 hex")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "GitSnapshot":
        paths = [change.path for change in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError("changes paths must be unique")
        keys = [
            (change.path, change.status, change.old_path or "")
            for change in self.changes
        ]
        if any(
            keys[index] >= keys[index + 1] for index in range(len(keys) - 1)
        ):
            raise ValueError(
                "changes must be strictly sorted by (path, status, old_path or empty)"
            )
        if self.changed_files_total < len(self.changes):
            raise ValueError("changed_files_total must be >= len(changes)")

        expected_large = tuple(
            sorted(change.path for change in self.changes if change.large_file)
        )
        expected_submodule = tuple(
            sorted(change.path for change in self.changes if change.submodule)
        )
        if self.large_file_paths != expected_large:
            raise ValueError(
                "large_file_paths must exactly match flagged large_file changes"
            )
        if self.submodule_paths != expected_submodule:
            raise ValueError(
                "submodule_paths must exactly match flagged submodule changes"
            )
        for path in self.large_file_paths + self.submodule_paths:
            if normalize_repo_path(path) != path:
                raise ValueError("large/submodule paths must be canonical")

        if len(set(self.omissions)) != len(self.omissions):
            raise ValueError("omissions must be unique")
        expected_omissions = set()
        if self.diff_truncated:
            expected_omissions.add("diff_truncated")
        if self.files_truncated:
            expected_omissions.add("files_truncated")
        if self.large_file_paths:
            expected_omissions.add("large_file")
        if self.submodule_paths:
            expected_omissions.add("submodule")
        if any(change.status == "unmerged" for change in self.changes):
            expected_omissions.add("unmerged")
        if self.ignored_scan_truncated:
            expected_omissions.add("ignored_scan_truncated")
        ordered = tuple(
            kind for kind in _OMISSION_ORDER if kind in expected_omissions
        )
        if "concurrent_change" in self.omissions:
            ordered = ordered + ("concurrent_change",)
        if self.omissions != ordered:
            raise ValueError(
                "omissions must be unique, exact, and in the fixed order"
            )
        expected_complete = not self.omissions
        if self.complete != expected_complete:
            raise ValueError(
                "complete must be true iff omissions is empty"
            )
        return self


class GitSnapshotResult(BaseModel):
    """快照及其确定性 Evidence 的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot: GitSnapshot
    evidence: Evidence

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "GitSnapshotResult":
        if self.snapshot.subject_digest != self.evidence.subject_digest:
            raise ValueError("snapshot and evidence subject digests must match")
        if (
            self.snapshot.diff_artifact_digest
            != self.evidence.artifact_digest
        ):
            raise ValueError(
                "snapshot and evidence artifact digests must match"
            )
        if self.evidence.kind != "git_snapshot":
            raise ValueError("evidence kind must be git_snapshot")
        if self.evidence.producer != "collector.git":
            raise ValueError("evidence producer must be collector.git")
        if self.evidence.trust_level != "deterministic":
            raise ValueError("evidence trust_level must be deterministic")
        expected_status = "success" if self.snapshot.complete else "truncated"
        if self.evidence.status != expected_status:
            raise ValueError(
                "evidence status must be success for complete snapshots "
                "and truncated for incomplete snapshots"
            )
        return self


class GitSnapshotCollector:
    """只读收集 base 提交到当前工作区的 Git 快照。"""

    def __init__(
        self,
        max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
        max_files: int = 500,
        max_file_bytes: int = 5_000_000,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        self.max_diff_bytes = _require_positive_int(
            max_diff_bytes, "max_diff_bytes"
        )
        self.max_files = _require_positive_int(max_files, "max_files")
        self.max_file_bytes = _require_positive_int(
            max_file_bytes, "max_file_bytes"
        )
        self.command_timeout_seconds = _require_positive_finite_float(
            command_timeout_seconds, "command_timeout_seconds"
        )

    def collect(
        self,
        repository_path: Path,
        *,
        repository_identity: str,
        base_ref: str,
        task_digest: str,
        policy_version: str,
        rubric_version: str,
        artifact_store: ArtifactStore,
        attachment_digests: tuple[str, ...] = (),
        collected_at: datetime | None = None,
    ) -> GitSnapshotResult:
        if not isinstance(repository_path, Path):
            raise TypeError("repository_path must be a pathlib.Path")
        if not isinstance(repository_identity, str):
            raise TypeError("repository_identity must be a str")
        if not isinstance(base_ref, str):
            raise TypeError("base_ref must be a str")
        if not isinstance(task_digest, str):
            raise TypeError("task_digest must be a str")
        if not isinstance(policy_version, str):
            raise TypeError("policy_version must be a str")
        if not isinstance(rubric_version, str):
            raise TypeError("rubric_version must be a str")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        if type(attachment_digests) is not tuple:
            raise TypeError("attachment_digests must be a tuple")
        if collected_at is None:
            collected_at = datetime.now(timezone.utc)
        elif not isinstance(collected_at, datetime):
            raise TypeError("collected_at must be a datetime")
        elif collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")

        if not base_ref.strip():
            raise ValueError("base_ref must not be blank")
        if base_ref.startswith("-"):
            raise ValueError("base_ref must not start with '-'")
        if any(character.isspace() for character in base_ref) or "\x00" in base_ref:
            raise ValueError("base_ref must not contain whitespace or NUL")
        if _SHA256_DIGEST_RE.fullmatch(task_digest) is None:
            raise ValueError("task_digest must be a lowercase sha256 digest")
        for digest in attachment_digests:
            if not isinstance(digest, str) or _SHA256_DIGEST_RE.fullmatch(
                digest
            ) is None:
                raise ValueError(
                    "attachment_digests must contain lowercase sha256 digests"
                )
        if not policy_version.strip():
            raise ValueError("policy_version must not be blank")
        if not rubric_version.strip():
            raise ValueError("rubric_version must not be blank")

        repository = normalize_repository_identity(repository_identity)
        root = self._resolve_repository_root(repository_path)
        base_revision = self._resolve_commit(root, base_ref, "base_ref")
        head_revision = self._resolve_commit(root, "HEAD", "HEAD")
        pre_head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        pre_head_value = pre_head.strip().decode("ascii")
        if pre_head_value != head_revision:
            raise GitWorktreeChangedError("git HEAD changed during collection")
        head_revision = pre_head_value

        pre_status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        worktree_dirty = bool(pre_status)

        name_status_output = self._git(
            root,
            "diff",
            "--name-status",
            "-z",
            "--find-renames=50%",
            base_revision,
        )
        changes = self._parse_name_status(name_status_output)
        unmerged_status_output = self._git(
            root,
            "diff",
            "--name-status",
            "-z",
        )
        for path, info in self._parse_name_status(
            unmerged_status_output
        ).items():
            if info["status"] != "unmerged":
                continue
            if path not in changes:
                raise GitCommandError(
                    f"unmerged path has no tracked destination in base diff: "
                    f"{path}"
                )
            changes[path] = {"status": "unmerged", "old_path": None}

        untracked_output = self._git(
            root, "ls-files", "--others", "--exclude-standard", "-z"
        )
        for path in self._parse_nul_paths(untracked_output):
            if path in changes:
                raise GitCommandError(
                    f"untracked path conflicts with a tracked change: {path}"
                )
            changes[path] = {"status": "untracked", "old_path": None}

        ignored_output, ignored_scan_truncated = self._git_ignored(root)
        ignored_files_lower_bound = ignored_output.count(b"\x00")

        stage_output = self._git(root, "ls-files", "--stage", "-z")
        submodule_paths = self._parse_stage_submodule_paths(stage_output)

        descriptors = sorted(
            (
                (path, info["status"], info["old_path"])
                for path, info in changes.items()
            ),
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
        changed_files_total = len(descriptors)
        files_truncated = changed_files_total > self.max_files
        selected = descriptors[: self.max_files]
        tracked_diff_paths: list[str] = []
        for path, status, old_path in selected:
            if status == "untracked":
                continue
            if path not in tracked_diff_paths:
                tracked_diff_paths.append(path)
            if (
                status in _RENAME_STATUSES
                and old_path is not None
                and old_path not in tracked_diff_paths
            ):
                tracked_diff_paths.append(old_path)

        first_changes = []
        for path, status, old_path in selected:
            if status in ("deleted", "unmerged"):
                first_changes.append(
                    GitChange(
                        path=path,
                        old_path=old_path,
                        status=status,
                        current_size=None,
                        current_digest=None,
                        binary=False,
                        large_file=False,
                        submodule=False,
                    )
                )
                continue
            if path in submodule_paths:
                first_changes.append(
                    GitChange(
                        path=path,
                        old_path=old_path,
                        status=status,
                        current_size=None,
                        current_digest=None,
                        binary=False,
                        large_file=False,
                        submodule=True,
                    )
                )
                continue
            size, digest, binary, large, _content = self._hash_worktree_file(
                root / path, False
            )
            first_changes.append(
                GitChange(
                    path=path,
                    old_path=old_path,
                    status=status,
                    current_size=size,
                    current_digest=digest,
                    binary=binary,
                    large_file=large,
                    submodule=False,
                )
            )
        first_changes.sort(
            key=lambda change: (
                change.path,
                change.status,
                change.old_path or "",
            )
        )
        changes_tuple = tuple(first_changes)

        tracked_patch, tracked_patch_truncated = self._git_diff(
            root, base_revision, tuple(tracked_diff_paths)
        )
        binary_markers = [
            self._marker(
                (
                    "binary-file",
                    f"path: {change.path}",
                    f"size: {change.current_size}",
                    f"sha256: {change.current_digest}",
                )
            )
            for change in changes_tuple
            if change.binary
            and change.status not in ("deleted", "untracked")
        ]
        if binary_markers:
            tracked_patch = (
                tracked_patch + b"\n" + b"\n".join(binary_markers)
            )

        marker = _DIFF_TRUNCATION_MARKER
        if len(marker) >= self.max_diff_bytes:
            body_budget = 0
        else:
            body_budget = self.max_diff_bytes - len(marker)
        combined = bytearray()
        diff_truncated = tracked_patch_truncated
        if len(tracked_patch) <= body_budget:
            combined += tracked_patch
        else:
            diff_truncated = True
            if body_budget > 0:
                combined += tracked_patch[:body_budget]

        post_head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        if post_head != pre_head:
            raise GitWorktreeChangedError("git HEAD changed during collection")
        post_status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if post_status != pre_status:
            raise GitWorktreeChangedError("git status changed during collection")
        post_stage = self._git(root, "ls-files", "--stage", "-z")
        if post_stage != stage_output:
            raise GitWorktreeChangedError("git stage changed during collection")

        for change in changes_tuple:
            if change.status in ("deleted", "unmerged") or change.submodule:
                continue
            need_content = (
                change.status == "untracked"
                and not diff_truncated
                and not change.large_file
                and not change.binary
            )
            size, digest, binary, large, content = self._hash_worktree_file(
                root / change.path, need_content
            )
            if (size, digest, binary, large) != (
                change.current_size,
                change.current_digest,
                change.binary,
                change.large_file,
            ):
                raise GitWorktreeChangedError(
                    "working-tree file changed during collection: "
                    f"{change.path}"
                )
            if change.status != "untracked" or diff_truncated:
                continue
            remaining = body_budget - len(combined)
            if remaining <= 0:
                diff_truncated = True
                continue
            patch = self._untracked_patch(change, content)
            if len(patch) <= remaining:
                combined += patch
            else:
                combined += patch[:remaining]
                diff_truncated = True

        if diff_truncated:
            if len(marker) >= self.max_diff_bytes:
                combined = bytearray(marker[: self.max_diff_bytes])
            else:
                combined = (
                    combined[: self.max_diff_bytes - len(marker)] + marker
                )
        artifact = bytes(combined)
        diff_bytes = len(artifact)
        diff_artifact_digest = artifact_store.put_bytes(artifact)

        large_file_paths = tuple(
            sorted(change.path for change in changes_tuple if change.large_file)
        )
        submodule_paths_out = tuple(
            sorted(change.path for change in changes_tuple if change.submodule)
        )
        omission_set = set()
        if diff_truncated:
            omission_set.add("diff_truncated")
        if files_truncated:
            omission_set.add("files_truncated")
        if large_file_paths:
            omission_set.add("large_file")
        if submodule_paths_out:
            omission_set.add("submodule")
        if any(change.status == "unmerged" for change in changes_tuple):
            omission_set.add("unmerged")
        if ignored_scan_truncated:
            omission_set.add("ignored_scan_truncated")
        omissions = tuple(
            kind for kind in _OMISSION_ORDER if kind in omission_set
        )
        complete = not omissions

        manifest = self._manifest_payload(
            repository=repository,
            base_revision=base_revision,
            head_revision=head_revision,
            scope="base_to_worktree",
            worktree_dirty=worktree_dirty,
            changes=changes_tuple,
            changed_files_total=changed_files_total,
            diff_artifact_digest=diff_artifact_digest,
            diff_bytes=diff_bytes,
            diff_truncated=diff_truncated,
            files_truncated=files_truncated,
            ignored_files_lower_bound=ignored_files_lower_bound,
            ignored_scan_truncated=ignored_scan_truncated,
            large_file_paths=large_file_paths,
            submodule_paths=submodule_paths_out,
            omissions=omissions,
            complete=complete,
        )
        manifest_digest = self._manifest_digest_from_payload(manifest)
        subject_digest = compute_subject_digest(
            SubjectDigestInput(
                repository=repository,
                base_revision=base_revision,
                head_revision=head_revision,
                normalized_diff_digest=manifest_digest,
                task_digest=task_digest,
                policy_version=policy_version,
                rubric_version=rubric_version,
                attachment_digests=attachment_digests,
            )
        )
        evidence_id = (
            "ev_git_"
            + hashlib.sha256(
                (subject_digest + diff_artifact_digest).encode("ascii")
            ).hexdigest()[:32]
        )
        source_ref = (
            f"git_snapshot:{repository}:{base_revision}:"
            f"{head_revision}:base_to_worktree"
        )

        snapshot = GitSnapshot(
            schema_version="v1",
            subject_digest=subject_digest,
            repository=repository,
            base_revision=base_revision,
            head_revision=head_revision,
            scope="base_to_worktree",
            worktree_dirty=worktree_dirty,
            changes=changes_tuple,
            changed_files_total=changed_files_total,
            diff_artifact_digest=diff_artifact_digest,
            diff_bytes=diff_bytes,
            diff_truncated=diff_truncated,
            files_truncated=files_truncated,
            ignored_files_lower_bound=ignored_files_lower_bound,
            ignored_scan_truncated=ignored_scan_truncated,
            large_file_paths=large_file_paths,
            submodule_paths=submodule_paths_out,
            omissions=omissions,
            complete=complete,
            collected_at=collected_at,
        )
        evidence = Evidence(
            schema_version="v1",
            evidence_id=evidence_id,
            subject_digest=subject_digest,
            kind="git_snapshot",
            producer="collector.git",
            artifact_digest=diff_artifact_digest,
            source_ref=source_ref,
            status="success" if complete else "truncated",
            trust_level="deterministic",
            collected_at=collected_at,
        )
        return GitSnapshotResult(
            schema_version="v1",
            snapshot=snapshot,
            evidence=evidence,
        )

    def build_subject_input(
        self,
        snapshot: GitSnapshot,
        *,
        task_digest: str,
        policy_version: str,
        rubric_version: str,
        attachment_digests: tuple[str, ...] = (),
    ) -> SubjectDigestInput:
        """Rebuild the exact subject input represented by one Git snapshot.

        The manifest is canonicalized through the same collector-owned helper
        used by ``collect``.  A snapshot whose persisted subject digest does
        not match those facts is rejected instead of being rewritten.
        """

        if type(snapshot) is not GitSnapshot:
            raise TypeError("snapshot must be an exact GitSnapshot")
        manifest_digest = self._manifest_digest(snapshot)
        subject_input = SubjectDigestInput(
            repository=snapshot.repository,
            base_revision=snapshot.base_revision,
            head_revision=snapshot.head_revision,
            normalized_diff_digest=manifest_digest,
            task_digest=task_digest,
            policy_version=policy_version,
            rubric_version=rubric_version,
            attachment_digests=attachment_digests,
        )
        if compute_subject_digest(subject_input) != snapshot.subject_digest:
            raise GitSnapshotError(
                "Git snapshot subject digest does not match its canonical facts"
            )
        return subject_input

    def _manifest_digest(self, snapshot: GitSnapshot) -> str:
        manifest = self._manifest_payload(
            repository=snapshot.repository,
            base_revision=snapshot.base_revision,
            head_revision=snapshot.head_revision,
            scope=snapshot.scope,
            worktree_dirty=snapshot.worktree_dirty,
            changes=snapshot.changes,
            changed_files_total=snapshot.changed_files_total,
            diff_artifact_digest=snapshot.diff_artifact_digest,
            diff_bytes=snapshot.diff_bytes,
            diff_truncated=snapshot.diff_truncated,
            files_truncated=snapshot.files_truncated,
            ignored_files_lower_bound=snapshot.ignored_files_lower_bound,
            ignored_scan_truncated=snapshot.ignored_scan_truncated,
            large_file_paths=snapshot.large_file_paths,
            submodule_paths=snapshot.submodule_paths,
            omissions=snapshot.omissions,
            complete=snapshot.complete,
        )
        return self._manifest_digest_from_payload(manifest)

    def _manifest_payload(
        self,
        *,
        repository: str,
        base_revision: str,
        head_revision: str,
        scope: str,
        worktree_dirty: bool,
        changes: tuple[GitChange, ...],
        changed_files_total: int,
        diff_artifact_digest: str,
        diff_bytes: int,
        diff_truncated: bool,
        files_truncated: bool,
        ignored_files_lower_bound: int,
        ignored_scan_truncated: bool,
        large_file_paths: tuple[str, ...],
        submodule_paths: tuple[str, ...],
        omissions: tuple[str, ...],
        complete: bool,
    ) -> dict[str, object]:
        """Build the one canonical Git manifest used for subject hashing."""

        return {
            "schema_version": "v1",
            "repository": repository,
            "base_revision": base_revision,
            "head_revision": head_revision,
            "scope": scope,
            "worktree_dirty": worktree_dirty,
            "changes": [
                change.model_dump(mode="json") for change in changes
            ],
            "changed_files_total": changed_files_total,
            "diff_artifact_digest": diff_artifact_digest,
            "diff_bytes": diff_bytes,
            "diff_truncated": diff_truncated,
            "files_truncated": files_truncated,
            "ignored_files_lower_bound": ignored_files_lower_bound,
            "ignored_scan_truncated": ignored_scan_truncated,
            "large_file_paths": list(large_file_paths),
            "submodule_paths": list(submodule_paths),
            "omissions": list(omissions),
            "complete": complete,
            "limits": {
                "max_diff_bytes": self.max_diff_bytes,
                "max_files": self.max_files,
                "max_file_bytes": self.max_file_bytes,
            },
        }

    @staticmethod
    def _manifest_digest_from_payload(manifest: dict[str, object]) -> str:
        return _sha256_bytes(
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )

    def _resolve_repository_root(self, repository_path: Path) -> Path:
        if not repository_path.is_dir():
            raise GitRepositoryError(
                "repository path must be an existing directory"
            )
        try:
            resolved = repository_path.resolve(strict=True)
        except OSError as exc:
            raise GitRepositoryError(
                f"repository path cannot be resolved: {exc}"
            ) from exc
        try:
            bare_output = self._git(
                repository_path, "rev-parse", "--is-bare-repository"
            )
        except GitCommandError as exc:
            if "not a git repository" in str(exc):
                raise GitRepositoryError(
                    "path is not inside a Git repository"
                ) from exc
            raise
        if bare_output.strip() == b"true":
            raise GitRepositoryError(
                "bare Git repositories are not supported"
            )
        try:
            top_output = self._git(
                repository_path, "rev-parse", "--show-toplevel"
            )
        except GitCommandError as exc:
            if "not a git repository" in str(exc):
                raise GitRepositoryError(
                    "path is not inside a Git repository"
                ) from exc
            raise
        top_bytes = top_output.strip()
        if not top_bytes:
            raise GitRepositoryError("git reported an empty top-level path")
        try:
            top = Path(os.fsdecode(top_bytes)).resolve(strict=True)
        except OSError as exc:
            raise GitRepositoryError(
                f"git top-level cannot be resolved: {exc}"
            ) from exc
        if top != resolved:
            raise GitRepositoryError(
                "repository_path must be the exact Git top-level; "
                "subdirectories and escaping symlinks are not allowed"
            )
        return resolved

    def _resolve_commit(self, root: Path, ref: str, label: str) -> str:
        try:
            output = self._git(
                root, "rev-parse", "--verify", f"{ref}^{{commit}}"
            )
        except GitCommandError as exc:
            if getattr(exc, "git_exit_code", None) == 128:
                raise GitRepositoryError(
                    f"cannot resolve {label} to a commit"
                ) from exc
            raise
        revision = output.strip().decode("ascii")
        if _FULL_SHA_RE.fullmatch(revision) is None:
            raise GitCommandError(
                f"{label} resolved to an unexpected revision format"
            )
        return revision

    def _git(self, repository_path: Path, *args: str) -> bytes:
        result = self._run_git(
            repository_path,
            args,
            stdout_limit=_MAX_METADATA_OUTPUT_BYTES,
        )
        return result.stdout

    def _git_diff(
        self,
        root: Path,
        base_revision: str,
        tracked_paths: tuple[str, ...],
    ) -> tuple[bytes, bool]:
        if not tracked_paths:
            return b"", False
        result = self._run_git(
            root,
            (
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--full-index",
                base_revision,
                "--",
                *tracked_paths,
            ),
            stdout_limit=self.max_diff_bytes + 1,
            stdout_cap_truncates=True,
        )
        return result.stdout, result.stdout_truncated

    def _git_ignored(self, root: Path) -> tuple[bytes, bool]:
        result = self._run_git(
            root,
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
            stdout_limit=_MAX_IGNORED_OUTPUT_BYTES,
            stdout_cap_truncates=True,
        )
        return result.stdout, result.stdout_truncated

    def _run_git(
        self,
        repository_path: Path,
        args: tuple[str, ...],
        *,
        stdout_limit: int,
        stdout_cap_truncates: bool = False,
    ) -> _BoundedCommandResult:
        command = ["git", *_GIT_PREFIX, *args]
        env = self._restricted_env()
        try:
            proc = subprocess.Popen(
                command,
                cwd=repository_path,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
        except OSError as exc:
            raise GitCommandError("failed to execute git") from exc

        def kill_on_cap() -> None:
            proc.terminate()
            proc.kill()

        stdout_reader = _BoundedPipeReader(
            proc.stdout, stdout_limit, kill_on_cap
        )
        stderr_reader = _BoundedPipeReader(
            proc.stderr, _MAX_STDERR_BYTES, kill_on_cap
        )
        stdout_reader.start()
        stderr_reader.start()
        try:
            try:
                proc.wait(timeout=self.command_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._cleanup_process(proc, (stdout_reader, stderr_reader))
                raise GitCommandError("git command timed out") from exc
            stdout_reader.join()
            stderr_reader.join()
            self._close_streams(proc)
            if stdout_reader.error is not None:
                raise GitCommandError(
                    "git stdout pipe reader failed"
                ) from stdout_reader.error
            if stderr_reader.error is not None:
                raise GitCommandError(
                    "git stderr pipe reader failed"
                ) from stderr_reader.error
            if stderr_reader.truncated:
                raise GitCommandError(
                    "git command stderr exceeded the configured safety bound"
                )
            if stdout_reader.truncated and not stdout_cap_truncates:
                raise GitCommandError(
                    "git command output exceeded the configured safety bound"
                )
            if proc.returncode != 0 and not stdout_reader.truncated:
                error = GitCommandError(
                    f"git command failed (exit {proc.returncode}): "
                    f"{self._safe_stderr(stderr_reader.data)}"
                )
                error.git_exit_code = proc.returncode
                error.git_stderr = self._safe_stderr(stderr_reader.data)
                raise error
            return _BoundedCommandResult(
                stdout=stdout_reader.data,
                stderr=stderr_reader.data,
                stdout_truncated=stdout_reader.truncated,
                stderr_truncated=stderr_reader.truncated,
                returncode=proc.returncode,
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            stdout_reader.join()
            stderr_reader.join()
            self._close_streams(proc)

    def _cleanup_process(self, proc, readers) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for reader in readers:
            reader.join()
        self._close_streams(proc)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    @staticmethod
    def _close_streams(proc) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _restricted_env() -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C",
        }

    def _safe_stderr(self, data: bytes) -> str:
        if not data:
            return "no stderr"
        if len(data) > _MAX_STDERR_BYTES:
            data = data[: _MAX_STDERR_BYTES] + b"...(truncated)"
        return data.decode("utf-8", "replace").strip()

    def _decode_git_output(self, data: bytes) -> str:
        return data.decode("utf-8", "surrogateescape")

    def _valid_unicode(self, value: str) -> str:
        try:
            return value.encode("utf-8", "surrogateescape").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitCommandError(
                "git output contained a path that is not valid UTF-8"
            ) from exc

    def _canonical_git_path(self, raw: str) -> str:
        value = self._valid_unicode(raw)
        if normalize_repo_path(value) != value:
            raise GitCommandError(f"noncanonical git path: {value!r}")
        return value

    def _split_nul(self, text: str) -> list[str]:
        parts = text.split("\x00")
        if parts and parts[-1] == "":
            parts.pop()
        return parts

    def _parse_name_status(self, data: bytes) -> dict[str, dict[str, str | None]]:
        tokens = self._split_nul(self._decode_git_output(data))
        changes: dict[str, dict[str, str | None]] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                raise GitCommandError(
                    "empty status token in name-status output"
                )
            letter = token[0]
            status_map = {
                "A": "added",
                "M": "modified",
                "D": "deleted",
                "R": "renamed",
                "C": "copied",
                "T": "type_changed",
                "U": "unmerged",
            }
            status = status_map.get(letter)
            if status is None:
                raise GitCommandError(
                    f"unknown status token in name-status output: {token!r}"
                )
            if letter in ("R", "C"):
                if token[1:] and not token[1:].isdigit():
                    raise GitCommandError(
                        f"invalid rename/copy status token: {token!r}"
                    )
                if index + 2 > len(tokens):
                    raise GitCommandError(
                        "truncated rename/copy record in name-status output"
                    )
                old_path = self._canonical_git_path(tokens[index])
                path = self._canonical_git_path(tokens[index + 1])
                index += 2
            else:
                if token[1:]:
                    raise GitCommandError(
                        f"invalid status token: {token!r}"
                    )
                if index >= len(tokens):
                    raise GitCommandError(
                        "truncated name-status record"
                    )
                path = self._canonical_git_path(tokens[index])
                index += 1
                old_path = None
            if path in changes:
                if status == "unmerged" or changes[path]["status"] == "unmerged":
                    changes[path] = {"status": "unmerged", "old_path": None}
                else:
                    raise GitCommandError(
                        f"duplicate path in name-status output: {path}"
                    )
            else:
                changes[path] = {"status": status, "old_path": old_path}
        return changes

    def _parse_nul_paths(self, data: bytes) -> list[str]:
        text = self._decode_git_output(data)
        return [self._canonical_git_path(token) for token in self._split_nul(text)]

    def _parse_stage_submodule_paths(self, data: bytes) -> set[str]:
        text = self._decode_git_output(data)
        paths: set[str] = set()
        for record in self._split_nul(text):
            if not record:
                continue
            if "\t" not in record:
                raise GitCommandError("malformed ls-files --stage record")
            metadata, _, path_token = record.partition("\t")
            fields = metadata.split(" ")
            if len(fields) != 3 or not all(fields):
                raise GitCommandError(
                    "malformed ls-files --stage metadata"
                )
            mode, _blob, _stage = fields
            path = self._canonical_git_path(path_token)
            if mode == "160000":
                paths.add(path)
        return paths

    def _hash_worktree_file(
        self, full_path: Path, need_content: bool
    ) -> tuple[int, str | None, bool, bool, bytes | None]:
        try:
            file_stat = full_path.lstat()
        except OSError as exc:
            raise GitRepositoryError(
                f"cannot stat working-tree path {full_path.name}: {exc}"
            ) from exc
        if stat.S_ISLNK(file_stat.st_mode):
            try:
                target = os.readlink(full_path)
            except OSError as exc:
                raise GitRepositoryError(
                    f"cannot read symlink {full_path.name}: {exc}"
                ) from exc
            raw = os.fsencode(target)
            digest = _sha256_bytes(raw)
            return (
                len(raw),
                digest,
                False,
                False,
                raw if need_content else None,
            )
        if not stat.S_ISREG(file_stat.st_mode):
            raise GitCommandError(
                f"unsupported working-tree file type at {full_path.name}"
            )
        size = file_stat.st_size
        if size > self.max_file_bytes:
            return size, None, False, True, None
        hasher = hashlib.sha256()
        binary = False
        collected = bytearray() if need_content else None
        try:
            with full_path.open("rb") as fh:
                first = fh.read(8192)
                if first:
                    if b"\x00" in first:
                        binary = True
                        if collected is not None:
                            collected = None
                    hasher.update(first)
                    if collected is not None:
                        collected.extend(first)
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    if b"\x00" in chunk and collected is not None:
                        collected = None
                    hasher.update(chunk)
                    if collected is not None:
                        collected.extend(chunk)
        except OSError as exc:
            raise GitRepositoryError(
                f"failed to read working-tree file {full_path.name}: {exc}"
            ) from exc
        return (
            size,
            "sha256:" + hasher.hexdigest(),
            binary,
            False,
            bytes(collected) if collected is not None else None,
        )

    def _untracked_patch(
        self, change: GitChange, content: bytes | None
    ) -> bytes:
        if change.large_file:
            return self._marker(
                (
                    "large-file",
                    f"path: {change.path}",
                    f"size: {change.current_size}",
                )
            )
        if change.binary:
            return self._marker(
                (
                    "binary-file",
                    f"path: {change.path}",
                    f"size: {change.current_size}",
                    f"sha256: {change.current_digest}",
                )
            )
        if content is None:
            raise GitCommandError(
                f"untracked text file content is unavailable: {change.path}"
            )
        text = content.decode("utf-8", "surrogateescape")
        lines = text.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            [],
            lines,
            fromfile="/dev/null",
            tofile=f"b/{change.path}",
            lineterm="",
        )
        return "".join(diff_lines).encode("utf-8", "surrogateescape")

    @staticmethod
    def _marker(lines: tuple[str, ...]) -> bytes:
        return ("\n".join(lines) + "\n").encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
