"""Read-only local entry point for persisted Assurance projections."""

from __future__ import annotations

from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Iterator

from web.assurance_store import AssuranceWebRepository

from .artifacts import ArtifactStore
from .live_freshness import LiveFreshnessChecker


class LocalAssuranceEntry:
    """Compose the local, read-only Assurance surface.

    This is intentionally a small adapter around the server-owned repository.
    Gate, freshness, allowed-action, and passport values are returned from the
    repository projection without being recomputed here.
    """

    def __init__(
        self,
        database: str | PathLike[str] | Path | None = None,
        artifact_root: str | PathLike[str] | Path | None = None,
        workspace_root: str | PathLike[str] | Path | None = None,
        *,
        db_path: str | PathLike[str] | Path | None = None,
    ) -> None:
        if database is None:
            database = db_path
        elif db_path is not None:
            raise ValueError("database and db_path are mutually exclusive")
        if database is None or artifact_root is None or workspace_root is None:
            raise ValueError("database, artifact_root, and workspace_root are required")
        self.database = _existing_file(database, "database")
        self.artifact_root = _existing_directory(artifact_root, "artifact_root")
        self.workspace_root = _existing_directory(workspace_root, "workspace_root")

        self.artifact_store = ArtifactStore(self.artifact_root)
        self.freshness_checker = LiveFreshnessChecker(
            workspace_root=self.workspace_root
        )
        self.repository = AssuranceWebRepository(
            self.database,
            freshness_checker=self.freshness_checker,
            live_required=True,
        )
        self._closed = False

    def close(self) -> None:
        """Close this entry; repeated close calls are harmless."""

        self._closed = True

    def __enter__(self) -> "LocalAssuranceEntry":
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> "LocalAssuranceEntry":
        self._ensure_open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def context(self) -> Iterator["LocalAssuranceEntry"]:
        """Provide an explicit context-manager spelling for callers."""

        with self:
            yield self

    def gate(self, case_id: str) -> dict[str, object]:
        """Return the authoritative server projection for one case."""

        self._ensure_open()
        return self.repository.get_change(case_id)

    get_gate = gate

    def passport(self, case_id: str, format: str = "json") -> dict | str:
        """Return the authoritative passport in ``json`` or ``markdown`` form."""

        self._ensure_open()
        if format not in {"json", "markdown"}:
            raise ValueError("format must be json or markdown")
        passport = self.repository.get_passport(case_id)
        return passport["canonical" if format == "json" else "markdown"]

    get_passport = passport

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("local assurance entry is closed")


def _path(value: str | PathLike[str] | Path, label: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} is invalid") from None
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _existing_file(value: str | PathLike[str] | Path, label: str) -> Path:
    path = _path(value, label)
    if not path.is_file():
        raise ValueError(f"{label} does not exist")
    return path


def _existing_directory(value: str | PathLike[str] | Path, label: str) -> Path:
    path = _path(value, label)
    if not path.is_dir():
        raise ValueError(f"{label} does not exist")
    return path


__all__ = ["LocalAssuranceEntry"]
