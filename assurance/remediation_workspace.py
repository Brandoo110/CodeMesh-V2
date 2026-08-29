"""P7 bounded-remediation workspace boundary.

The workspace is a temporary copy of a seed directory.  It is intentionally
host-process based: path and quota checks are enforced here, but this module
does not claim to be an OS or container sandbox.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assurance.digests import normalize_repo_path


CONTROLLER_PRIVATE_DIR = ".codemesh_eval"
_DURABLE_SNAPSHOT_DIR = "remediation_snapshots"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class WorkspaceViolation(ValueError):
    """The requested path is outside the granted public workspace."""


class WorkspaceGrant(BaseModel):
    """Immutable, canonical grant for the files an agent may access."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: tuple[str, ...]
    max_files: int = Field(default=100, strict=True, gt=0)
    max_bytes: int = Field(default=1024 * 1024, strict=True, gt=0)

    @field_validator("allowed_paths", mode="before")
    @classmethod
    def _canonicalize_paths(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("allowed_paths must be a tuple or list")
        canonical: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("allowed path must be a string")
            try:
                path = normalize_repo_path(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid allowed path: {raw!r}") from exc
            if _is_private_path(path):
                raise ValueError(
                    f"{CONTROLLER_PRIVATE_DIR} is reserved for the controller"
                )
            if path in seen:
                raise ValueError(f"duplicate allowed path: {path}")
            seen.add(path)
            canonical.append(path)
        if not canonical:
            raise ValueError("allowed_paths must not be empty")
        return tuple(sorted(canonical))


def _is_private_path(path: str | Path) -> bool:
    return any(
        part.casefold() == CONTROLLER_PRIVATE_DIR.casefold()
        for part in Path(path).parts
    )


class PublicWorkspaceView:
    """Agent-facing view with no controller path or root accessors."""

    __slots__ = ("_workspace",)

    def __init__(self, workspace: "IsolatedWorkspace") -> None:
        self._workspace = workspace

    def read_text(self, relative_path: str | Path) -> str:
        return self._workspace.read_text(relative_path)

    def write_text(self, relative_path: str | Path, content: str) -> Path:
        return self._workspace.write_text(relative_path, content)

    def public_paths(self) -> tuple[str, ...]:
        return self._workspace.public_paths()


@dataclass
class IsolatedWorkspace:
    """Temporary copy with exact-file public access and quota enforcement."""

    root: Path
    seed_root: Path
    grant: WorkspaceGrant
    _temporary: tempfile.TemporaryDirectory[str]
    _published_path: Path | None = field(default=None, init=False, repr=False)
    _published_identity: tuple[str, str] | None = field(
        default=None, init=False, repr=False
    )

    @classmethod
    def prepare(
        cls,
        seed_root: Path,
        grant: WorkspaceGrant,
        *,
        parent: Path | None = None,
    ) -> "IsolatedWorkspace":
        if not isinstance(grant, WorkspaceGrant):
            raise TypeError("grant must be a WorkspaceGrant")
        seed = Path(seed_root).resolve()
        if not seed.is_dir():
            raise FileNotFoundError(f"workspace seed not found: {seed_root}")
        temporary = tempfile.TemporaryDirectory(
            prefix="codemesh-remediation-",
            dir=str(parent) if parent is not None else None,
        )
        root = Path(temporary.name) / "workspace"
        try:
            shutil.copytree(seed, root, symlinks=True)
            root = root.resolve()
            (root / CONTROLLER_PRIVATE_DIR).mkdir(parents=True, exist_ok=True)
        except Exception:
            temporary.cleanup()
            raise
        return cls(
            root=root,
            seed_root=seed,
            grant=grant,
            _temporary=temporary,
        )

    @staticmethod
    def _ensure_real_directory(path: Path, *, create: bool = False) -> Path:
        """Validate one directory and every ancestor without following links."""

        if not isinstance(path, Path) or not path.is_absolute():
            raise WorkspaceViolation("durable workspace root must be absolute")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create or current != path:
                    raise WorkspaceViolation(
                        "durable workspace directory is unavailable"
                    ) from None
                try:
                    current.mkdir(mode=0o700)
                    info = current.lstat()
                except OSError as exc:
                    raise WorkspaceViolation(
                        "durable workspace directory cannot be created"
                    ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceViolation(
                    "durable workspace directory must be a real directory"
                )
        return path

    def publish(
        self, *, parent: Path, remediation_id: str, subject_digest: str
    ) -> Path:
        """Move the repaired workspace into a deterministic durable namespace.

        The target name is a content-free hash of the remediation identity and
        new subject.  The namespace is controller-owned and every existing
        path component is checked with ``lstat`` before the atomic move, so a
        collision or symlink can never cause an overwrite or an escape.
        """

        if type(remediation_id) is not str or not remediation_id.strip():
            raise WorkspaceViolation("remediation_id must be nonblank")
        if type(subject_digest) is not str or _SHA256_RE.fullmatch(subject_digest) is None:
            raise WorkspaceViolation("subject_digest must be a sha256 digest")
        identity = (remediation_id, subject_digest)
        if self._published_path is not None:
            if self._published_identity == identity and self._published_path.is_dir():
                return self._published_path
            raise WorkspaceViolation("workspace has already been published")
        parent = self._ensure_real_directory(parent)
        controller_dir = self._ensure_real_directory(
            parent / CONTROLLER_PRIVATE_DIR, create=True
        )
        snapshot_dir = self._ensure_real_directory(
            controller_dir / _DURABLE_SNAPSHOT_DIR, create=True
        )
        source = self._ensure_real_directory(self.root)
        token = hashlib.sha256(
            (remediation_id + "\0" + subject_digest).encode("utf-8")
        ).hexdigest()
        target = snapshot_dir / token
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise WorkspaceViolation("durable workspace target already exists")

        try:
            source.rename(target)
        except OSError as exc:
            raise WorkspaceViolation("durable workspace publish failed") from exc
        self.root = target
        self._published_path = target
        self._published_identity = identity
        return target

    @staticmethod
    def _canonical(relative_path: str | Path) -> str:
        raw = str(relative_path)
        try:
            return normalize_repo_path(raw)
        except (TypeError, ValueError) as exc:
            raise WorkspaceViolation(f"invalid workspace path: {relative_path}") from exc

    def _resolve_inside(
        self,
        relative_path: str | Path,
        *,
        must_exist: bool,
        public: bool,
    ) -> Path:
        canonical = self._canonical(relative_path)
        if public:
            if _is_private_path(canonical):
                raise WorkspaceViolation(
                    f"{CONTROLLER_PRIVATE_DIR} is reserved for the controller"
                )
            if canonical not in self.grant.allowed_paths:
                raise WorkspaceViolation(
                    f"path is not in the allowed_paths grant: {canonical}"
                )
        candidate = (self.root / canonical).resolve(strict=must_exist)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes workspace: {relative_path}")
        if public and _is_private_path(candidate.relative_to(self.root)):
            raise WorkspaceViolation(
                f"{CONTROLLER_PRIVATE_DIR} is reserved for the controller"
            )
        return candidate

    def resolve(self, relative_path: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve a controller path while retaining the workspace boundary."""

        return self._resolve_inside(
            relative_path,
            must_exist=must_exist,
            public=False,
        )

    def resolve_public(
        self,
        relative_path: str | Path,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve one exact allowlisted path for agent-visible operations."""

        return self._resolve_inside(
            relative_path,
            must_exist=must_exist,
            public=True,
        )

    def _usage(self) -> tuple[int, int]:
        count = 0
        total = 0
        allowed = set(self.grant.allowed_paths)
        for path in self.root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.relative_to(self.root)
                if relative.as_posix() not in allowed:
                    continue
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(self.root):
                    continue
                count += 1
                total += resolved.stat().st_size
            except OSError as exc:
                raise WorkspaceViolation(
                    f"cannot measure workspace quota: {exc}"
                ) from exc
        return count, total

    def read_text(self, relative_path: str | Path) -> str:
        path = self.resolve_public(relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceViolation(f"not a regular file: {relative_path}")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspaceViolation(f"cannot read workspace file: {relative_path}") from exc

    def write_text(self, relative_path: str | Path, content: str) -> Path:
        if not isinstance(content, str):
            raise TypeError("workspace content must be a string")
        path = self.resolve_public(relative_path)
        file_count, total_bytes = self._usage()
        previous_size = path.stat().st_size if path.is_file() else 0
        next_files = file_count if path.is_file() else file_count + 1
        next_bytes = total_bytes - previous_size + len(content.encode("utf-8"))
        if next_files > self.grant.max_files or next_bytes > self.grant.max_bytes:
            raise WorkspaceViolation(
                "workspace quota exceeded: "
                f"files={next_files}/{self.grant.max_files}, "
                f"bytes={next_bytes}/{self.grant.max_bytes}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents so a pre-existing symlink cannot
        # redirect the write outside the temporary workspace.
        path = self.resolve_public(relative_path)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise WorkspaceViolation(f"cannot write workspace file: {relative_path}") from exc
        return path

    def snapshot(self) -> dict[str, bytes]:
        """Return deterministic bytes for existing allowlisted regular files."""

        snapshot: dict[str, bytes] = {}
        for relative in self.grant.allowed_paths:
            try:
                path = self.resolve_public(relative, must_exist=True)
            except (FileNotFoundError, WorkspaceViolation):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[relative] = path.read_bytes()
            except OSError as exc:
                raise WorkspaceViolation(f"cannot snapshot workspace file: {relative}") from exc
        return snapshot

    def public_paths(self) -> tuple[str, ...]:
        """List existing public files without exposing controller-private paths."""

        paths: list[str] = []
        for relative in self.grant.allowed_paths:
            try:
                path = self.resolve_public(relative, must_exist=True)
            except (FileNotFoundError, WorkspaceViolation):
                continue
            if path.is_file():
                paths.append(relative)
        return tuple(paths)

    def public_view(self) -> PublicWorkspaceView:
        return PublicWorkspaceView(self)

    def controller_path(self, relative_path: str | Path) -> Path:
        """Resolve a controller-only path under the private directory."""

        canonical = self._canonical(relative_path)
        if not _is_private_path(canonical):
            canonical = f"{CONTROLLER_PRIVATE_DIR}/{canonical}"
        return self._resolve_inside(canonical, must_exist=False, public=False)

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> "IsolatedWorkspace":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
