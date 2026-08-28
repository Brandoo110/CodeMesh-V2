"""Server-owned live freshness checks for persisted Assurance runs.

The public seam is intentionally small: callers provide the persisted source
binding and the last committed Git/Intake snapshots, and receive one immutable
``LiveFreshness`` result.  Collection limits, path checks, scratch artifacts,
and fail-closed error mapping stay inside this module.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .artifacts import ArtifactStore
from .intake import (
    IntakeChangedError,
    IntakeCollectionError,
    IntakePathError,
    IntakeResult,
    IntakeSnapshot,
    TaskPolicyCollector,
)
from .run_service import FreshnessSourceBinding
from .snapshot import GitSnapshot, GitSnapshotCollector, GitSnapshotError


class FreshnessStatus(str, Enum):
    """The only statuses exposed by a live freshness check."""

    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


# A discoverable alias for callers that prefer the longer name.
LiveFreshnessStatus = FreshnessStatus


@dataclass(frozen=True)
class LiveFreshness:
    """Immutable, path-free result of one server-side freshness evaluation."""

    status: FreshnessStatus
    reason_code: str
    checked_at: datetime
    expected_subject_digest: str | None = None
    observed_subject_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FreshnessStatus):
            raise TypeError("status must be a FreshnessStatus")
        if type(self.reason_code) is not str or not self.reason_code:
            raise ValueError("reason_code must be a nonblank string")
        if not isinstance(self.checked_at, datetime):
            raise TypeError("checked_at must be a datetime")
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        for name, value in (
            ("expected_subject_digest", self.expected_subject_digest),
            ("observed_subject_digest", self.observed_subject_digest),
        ):
            if value is not None and (
                type(value) is not str
                or len(value) != 71
                or not value.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in value[7:])
            ):
                raise ValueError(f"{name} must be a sha256 digest or None")

    @property
    def is_fresh(self) -> bool:
        """Return whether this result permits a decision write."""

        return self.status is FreshnessStatus.FRESH

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        """Provide a small serialization surface for web projections/tests."""

        del mode
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "checked_at": self.checked_at.isoformat(),
            "expected_subject_digest": self.expected_subject_digest,
            "observed_subject_digest": self.observed_subject_digest,
        }


class LiveFreshnessCheckerProtocol(Protocol):
    """Replaceable test seam for repository projections and decision fences."""

    def check(
        self,
        binding: FreshnessSourceBinding,
        baseline_git: GitSnapshot,
        baseline_intake: IntakeSnapshot,
    ) -> LiveFreshness:
        """Evaluate the persisted source against its current local state."""


@dataclass(frozen=True)
class _SourcePaths:
    """Validated local paths used only inside the checker implementation."""

    workspace_root: Path
    repository_root: Path


class LiveFreshnessChecker:
    """Deep module hiding bounded Git/Intake collection and fail-closed mapping."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        git_collector_factory: Callable[..., GitSnapshotCollector] | None = None,
        intake_collector: TaskPolicyCollector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be a pathlib.Path")
        self._workspace_root = workspace_root
        self._git_collector_factory = git_collector_factory or GitSnapshotCollector
        self._intake_collector = intake_collector or TaskPolicyCollector()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def check(
        self,
        binding: FreshnessSourceBinding,
        baseline_git: GitSnapshot,
        baseline_intake: IntakeSnapshot,
    ) -> LiveFreshness:
        """Check one persisted run without writing public evidence or state."""

        checked_at = self._checked_at()
        expected_subject = self._expected_subject(binding, baseline_git, baseline_intake)
        if expected_subject is None:
            return self._unavailable("BASELINE_CORRUPT", checked_at)
        expected_git, expected_intake = expected_subject
        try:
            paths = self._validate_paths(binding)
        except Exception:
            return self._unavailable("REPOSITORY_PATH_INVALID", checked_at, expected_git.subject_digest)

        if not expected_git.complete:
            return self._unavailable(
                "BASELINE_GIT_TRUNCATED", checked_at, expected_git.subject_digest
            )
        if not expected_intake.complete:
            return self._unavailable(
                "BASELINE_INTAKE_INCOMPLETE", checked_at, expected_git.subject_digest
            )

        scratch_path: str | None = None
        try:
            # Both collectors require an exact ArtifactStore.  The temporary
            # root never enters the repository's configured evidence store.
            scratch_path = tempfile.mkdtemp(prefix="codemesh-live-freshness-")
            scratch_store = ArtifactStore(Path(scratch_path))

            task_digest = self._probe_task_digest(paths.repository_root, binding)
            git_collector = self._git_collector(binding)
            self._assert_exact_git_root(git_collector, paths.repository_root)
            git_result = git_collector.collect(
                paths.repository_root,
                repository_identity=binding.repository_identity,
                base_ref=binding.requested_base_ref,
                task_digest=task_digest,
                policy_version=binding.policy_version,
                rubric_version=binding.rubric_version,
                artifact_store=scratch_store,
                attachment_digests=binding.attachment_digests,
                collected_at=checked_at,
            )
            observed_git = git_result.snapshot
            if not observed_git.complete:
                return self._unavailable(
                    "GIT_SNAPSHOT_TRUNCATED",
                    checked_at,
                    expected_git.subject_digest,
                    observed_git.subject_digest,
                )

            intake_result = self._intake_collector.collect(
                paths.repository_root,
                subject_digest=observed_git.subject_digest,
                artifact_store=scratch_store,
                task_path=binding.task_path,
                policy_paths=binding.policy_paths,
                adr_paths=binding.adr_paths,
                runbook_paths=binding.runbook_paths,
                collected_at=checked_at,
            )
            observed_intake = intake_result.snapshot
            if not observed_intake.complete:
                return self._unavailable(
                    "LIVE_INTAKE_UNAVAILABLE",
                    checked_at,
                    expected_git.subject_digest,
                    observed_git.subject_digest,
                )

            if (
                self._semantic_dump(expected_git)
                != self._semantic_dump(observed_git)
                or self._semantic_dump(expected_intake)
                != self._semantic_dump(observed_intake)
            ):
                return self._stale(
                    "FRESHNESS_MISMATCH",
                    checked_at,
                    expected_git.subject_digest,
                    observed_git.subject_digest,
                )
            return LiveFreshness(
                status=FreshnessStatus.FRESH,
                reason_code="FRESHNESS_MATCH",
                checked_at=checked_at,
                expected_subject_digest=expected_git.subject_digest,
                observed_subject_digest=observed_git.subject_digest,
            )
        except _KnownStale:
            return self._stale(
                "FRESHNESS_MISMATCH",
                checked_at,
                expected_git.subject_digest,
            )
        except IntakeChangedError:
            return self._unavailable(
                "LIVE_INTAKE_UNAVAILABLE",
                checked_at,
                expected_git.subject_digest,
            )
        except _GitRootUnavailable:
            return self._unavailable(
                "GIT_ROOT_UNAVAILABLE",
                checked_at,
                expected_git.subject_digest,
            )
        except (IntakeCollectionError, GitSnapshotError, OSError, ValueError, TypeError):
            return self._unavailable(
                "LIVE_COLLECTION_FAILED",
                checked_at,
                expected_git.subject_digest,
            )
        except Exception:
            # The public contract deliberately does not depend on collector
            # implementation exception classes or include their text.
            return self._unavailable(
                "LIVE_COLLECTION_FAILED",
                checked_at,
                expected_git.subject_digest,
            )
        finally:
            if scratch_path is not None:
                self._remove_scratch(Path(scratch_path))

    def _checked_at(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        return value

    @staticmethod
    def _expected_subject(
        binding: FreshnessSourceBinding,
        baseline_git: GitSnapshot,
        baseline_intake: IntakeSnapshot,
    ) -> tuple[GitSnapshot, IntakeSnapshot] | None:
        if type(binding) is not FreshnessSourceBinding:
            return None
        if type(baseline_git) is not GitSnapshot:
            return None
        if type(baseline_intake) is not IntakeSnapshot:
            return None
        try:
            if (
                binding.subject.subject_digest != baseline_git.subject_digest
                or baseline_git.subject_digest != baseline_intake.subject_digest
                or binding.subject.repository != baseline_git.repository
                or binding.subject.base_revision != baseline_git.base_revision
                or binding.subject.head_revision != baseline_git.head_revision
                or binding.subject.task_digest != baseline_intake.task_digest
                or binding.subject.policy_version != binding.policy_version
                or binding.resolved_base_revision != baseline_git.base_revision
                or binding.repository_identity != baseline_git.repository
            ):
                return None
        except (AttributeError, TypeError, ValueError):
            return None
        return baseline_git, baseline_intake

    def _validate_paths(self, binding: FreshnessSourceBinding) -> _SourcePaths:
        workspace = self._validate_real_directory(self._workspace_root)
        repository = binding.repository_path
        if not isinstance(repository, Path) or not repository.is_absolute():
            raise ValueError("repository path is invalid")
        repository = self._validate_real_directory(repository)
        try:
            if os.path.commonpath((str(workspace), str(repository))) != str(workspace):
                raise ValueError("repository path is outside workspace")
        except ValueError:
            raise ValueError("repository path is outside workspace") from None
        return _SourcePaths(workspace_root=workspace, repository_root=repository)

    @staticmethod
    def _validate_real_directory(path: Path) -> Path:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if current == path:
                    raise _KnownStale from None
                raise
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("path contains a symlink")
            if current == path and not stat.S_ISDIR(info.st_mode):
                raise ValueError("path is not a directory")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            raise _KnownStale from None
        if resolved != path:
            raise ValueError("path resolves through a symlink")
        return resolved

    def _probe_task_digest(
        self, repository_root: Path, binding: FreshnessSourceBinding
    ) -> str:
        try:
            return self._intake_collector.probe_task_digest(
                repository_root, task_path=binding.task_path
            )
        except IntakePathError:
            task_path = repository_root / binding.task_path
            try:
                info = task_path.lstat()
            except FileNotFoundError:
                raise _KnownStale from None
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("task path contains a symlink")
            raise

    def _git_collector(self, binding: FreshnessSourceBinding) -> GitSnapshotCollector:
        profile = binding.git_collector_profile
        values = profile.model_dump(exclude={"schema_version"})
        return self._git_collector_factory(**values)

    @staticmethod
    def _assert_exact_git_root(collector: object, repository_root: Path) -> None:
        resolver = getattr(collector, "_resolve_repository_root", None)
        if not callable(resolver):
            raise _GitRootUnavailable
        try:
            resolved = resolver(repository_root)
        except Exception:
            raise _GitRootUnavailable from None
        if not isinstance(resolved, Path) or resolved != repository_root:
            raise _GitRootUnavailable

    @staticmethod
    def _semantic_dump(value: object) -> object:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {
                key: LiveFreshnessChecker._semantic_dump(item)
                for key, item in value.items()
                if key not in {"collected_at", "evaluated_at", "created_at", "updated_at"}
            }
        if isinstance(value, list):
            return [LiveFreshnessChecker._semantic_dump(item) for item in value]
        if isinstance(value, tuple):
            return tuple(LiveFreshnessChecker._semantic_dump(item) for item in value)
        return value

    @staticmethod
    def _remove_scratch(path: Path) -> None:
        # Scratch files are bounded collector outputs.  Remove only the exact
        # temporary root allocated by this check; no configured path is touched.
        for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if child.is_dir() and not child.is_symlink():
                    child.rmdir()
                else:
                    child.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError:
            pass

    @staticmethod
    def _stale(
        reason_code: str,
        checked_at: datetime,
        expected_subject_digest: str | None = None,
        observed_subject_digest: str | None = None,
    ) -> LiveFreshness:
        return LiveFreshness(
            status=FreshnessStatus.STALE,
            reason_code=reason_code,
            checked_at=checked_at,
            expected_subject_digest=expected_subject_digest,
            observed_subject_digest=observed_subject_digest,
        )

    @staticmethod
    def _unavailable(
        reason_code: str,
        checked_at: datetime,
        expected_subject_digest: str | None = None,
        observed_subject_digest: str | None = None,
    ) -> LiveFreshness:
        return LiveFreshness(
            status=FreshnessStatus.UNAVAILABLE,
            reason_code=reason_code,
            checked_at=checked_at,
            expected_subject_digest=expected_subject_digest,
            observed_subject_digest=observed_subject_digest,
        )


class _KnownStale(Exception):
    """Internal marker for a provable source disappearance."""


class _GitRootUnavailable(Exception):
    """Internal marker for an unverifiable exact Git top-level."""


__all__ = [
    "FreshnessStatus",
    "LiveFreshnessStatus",
    "LiveFreshness",
    "LiveFreshnessCheckerProtocol",
    "LiveFreshnessChecker",
]
