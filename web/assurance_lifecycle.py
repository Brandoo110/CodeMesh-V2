"""Web-facing P7 lifecycle repository with no production side effects."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from assurance.artifacts import ArtifactStore
from assurance.lifecycle_store import SQLiteAssuranceLifecycleStore
from assurance.release_observation import (
    ReleaseObservation,
    ReleaseObservationImporter,
)


class AssuranceLifecycleRepository:
    """Small API seam for release evidence and read-only remediation lineage."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        resolved_db = Path(
            db_path or Path.home() / ".codemesh" / "assurance.sqlite"
        )
        self.store = SQLiteAssuranceLifecycleStore(resolved_db)
        self.artifact_store = ArtifactStore(
            artifact_root or resolved_db.parent / "assurance-artifacts"
        )

    def initialize(self) -> None:
        self.store.initialize()

    def record_manual(self, case_id: str, observation: ReleaseObservation) -> dict:
        record = self.store.append_release_observation(case_id, observation)
        return record.model_dump(mode="json")

    def import_payload(self, case_id: str, payload: bytes) -> dict:
        state = self.store.load_case(case_id)
        imported = ReleaseObservationImporter.import_bytes(
            payload,
            expected_subject_digest=state.case.subject_digest,
            artifact_store=self.artifact_store,
        )
        record = self.store.append_release_observation(
            case_id,
            imported,
            artifact_store=self.artifact_store,
        )
        return record.model_dump(mode="json")

    def list_release_observations(self, case_id: str) -> list[dict]:
        return [
            item.model_dump(mode="json")
            for item in self.store.list_release_observations(case_id)
        ]

    def list_remediations(self, case_id: str) -> list[dict]:
        self.store.load_case(case_id)
        return [
            item.model_dump(mode="json")
            for item in self.store.list_remediations(case_id)
        ]


@lru_cache(maxsize=1)
def get_assurance_lifecycle_repository() -> AssuranceLifecycleRepository:
    repository = AssuranceLifecycleRepository()
    repository.initialize()
    return repository


__all__ = [
    "AssuranceLifecycleRepository",
    "get_assurance_lifecycle_repository",
]
