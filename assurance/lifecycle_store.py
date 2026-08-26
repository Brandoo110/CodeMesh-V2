"""Additive SQLite lifecycle store for P7 remediation and release evidence.

The lifecycle schema is deliberately separate from the stable V1/V2 core
schema.  Remediation still uses one SQLite transaction across the existing
case/event tables and the new immutable remediation row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, model_validator

from .artifacts import ArtifactStore
from .contracts import AcceptanceCase, Finding
from .release_observation import (
    ReleaseObservation,
    ReleaseObservationImportReceipt,
    ReleaseObservationImportResult,
    ReleaseObservationImporter,
)
from .remediation import (
    RemediationRequest,
    RemediationResult,
    RemediationStatus,
)
from .state_machine import AcceptanceBinding, AcceptanceEvent, apply_acceptance_event
from .store import (
    CaseNotFoundError,
    ProjectionIntegrityError,
    SQLiteAssuranceStore,
    StoreConflictError,
    StoreMigrationError,
    StorePersistenceError,
)


_LIFECYCLE_SCHEMA_VERSION = 2


class LifecycleProjectionError(ProjectionIntegrityError):
    """Stored lifecycle rows cannot be reconstructed without ambiguity."""


class RemediationCommitReceipt(BaseModel):
    """Immutable receipt for an atomic old-case/new-case transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    remediation_id: str
    old_case_id: str
    new_case_id: str
    old_subject_digest: str
    new_subject_digest: str
    human_selected_finding_id: str
    invalidation_event_id: str
    result_digest: str
    committed_at: AwareDatetime
    committed: Literal[True] = True


class StoredReleaseObservation(BaseModel):
    """Append-only release observation projected from SQLite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str
    observation: ReleaseObservation
    import_receipt: ReleaseObservationImportReceipt | None = None
    stored_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_source_receipt(self) -> "StoredReleaseObservation":
        if self.observation.source == "manual" and self.import_receipt is not None:
            raise ValueError("manual observation must not carry an import receipt")
        if self.observation.source == "import":
            if self.import_receipt is None:
                raise ValueError("import observation requires an import receipt")
            ReleaseObservationImportResult(
                observation=self.observation,
                receipt=self.import_receipt,
            )
        return self


def _canonical_model_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteAssuranceLifecycleStore(SQLiteAssuranceStore):
    """Deep P7 persistence seam layered additively over the core store."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self._lifecycle_db_path = Path(db_path)

    def initialize(self) -> None:
        """Initialize stable core tables, then additive lifecycle migrations."""

        super().initialize()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS assurance_lifecycle_schema_migrations ("
                "version INTEGER PRIMARY KEY,"
                "applied_at TEXT NOT NULL"
                ")"
            )
            version = self._validate_lifecycle_history(conn)
            if version < 1:
                self._create_remediation_table(conn)
                conn.execute(
                    "INSERT INTO assurance_lifecycle_schema_migrations"
                    " (version, applied_at) VALUES (1, datetime('now'))"
                )
            if version < 2:
                self._create_release_observation_table(conn)
                conn.execute(
                    "INSERT INTO assurance_lifecycle_schema_migrations"
                    " (version, applied_at) VALUES (2, datetime('now'))"
                )
            conn.commit()
        except StoreMigrationError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to initialize lifecycle store: {exc}"
            ) from exc
        finally:
            conn.close()

    def lifecycle_schema_version(self) -> int:
        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            return _LIFECYCLE_SCHEMA_VERSION
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to read lifecycle schema version: {exc}"
            ) from exc
        finally:
            conn.close()

    def commit_remediation(
        self,
        *,
        request: RemediationRequest,
        result: RemediationResult,
        selected_finding: Finding,
        new_case: AcceptanceCase,
        new_binding: AcceptanceBinding,
        invalidation_event: AcceptanceEvent,
    ) -> RemediationCommitReceipt:
        """Commit invalidation, new DRAFT case, and lineage in one transaction."""

        self._validate_remediation_inputs(
            request,
            result,
            selected_finding,
            new_case,
            new_binding,
            invalidation_event,
        )
        request_json = _canonical_model_json(request)
        result_json = _canonical_model_json(result)
        finding_json = _canonical_model_json(selected_finding)
        new_case_json = _canonical_model_json(new_case)
        new_binding_json = _canonical_model_json(new_binding)
        invalidation_json = _canonical_model_json(invalidation_event)

        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM assurance_remediations WHERE remediation_id = ?",
                (request.remediation_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "request_json": request_json,
                    "result_json": result_json,
                    "selected_finding_json": finding_json,
                    "new_case_json": new_case_json,
                    "new_binding_json": new_binding_json,
                    "invalidation_event_json": invalidation_json,
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise StoreConflictError(
                        f"remediation_id {request.remediation_id!r} already exists"
                        " with different content"
                    )
                receipt = self._remediation_from_row(existing)
                conn.commit()
                return receipt

            lineage_conflict = conn.execute(
                "SELECT remediation_id FROM assurance_remediations"
                " WHERE old_case_id = ? OR new_case_id = ?",
                (request.old_case_id, new_case.case_id),
            ).fetchone()
            if lineage_conflict is not None:
                raise StoreConflictError(
                    "old or new case already belongs to another remediation"
                )

            old_state = self._load_case(conn, request.old_case_id)
            if old_state.case.subject_digest != request.old_subject_digest:
                raise StoreConflictError("remediation old subject is stale")
            next_old_state = apply_acceptance_event(old_state, invalidation_event)
            new_row = conn.execute(
                "SELECT case_id FROM assurance_cases WHERE case_id = ?",
                (new_case.case_id,),
            ).fetchone()
            if new_row is not None:
                raise StoreConflictError(
                    f"new case_id {new_case.case_id!r} already exists"
                )

            sequence = len(old_state.applied_events) + 1
            conn.execute(
                "INSERT INTO assurance_case_events"
                " (case_id, sequence, event_id, subject_digest, event_json, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    request.old_case_id,
                    sequence,
                    invalidation_event.event_id,
                    invalidation_event.subject_digest,
                    invalidation_json,
                    invalidation_event.occurred_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO assurance_cases"
                " (case_id, subject_digest, initial_case_json, binding_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    new_case.case_id,
                    new_case.subject_digest,
                    new_case_json,
                    new_binding_json,
                    new_case.created_at.isoformat(),
                ),
            )
            receipt = RemediationCommitReceipt(
                remediation_id=request.remediation_id,
                old_case_id=request.old_case_id,
                new_case_id=new_case.case_id,
                old_subject_digest=request.old_subject_digest,
                new_subject_digest=new_case.subject_digest,
                human_selected_finding_id=request.human_selected_finding_id,
                invalidation_event_id=invalidation_event.event_id,
                result_digest=_sha256_text(result_json),
                committed_at=invalidation_event.occurred_at,
            )
            conn.execute(
                "INSERT INTO assurance_remediations"
                " (remediation_id, old_case_id, new_case_id, old_subject_digest,"
                " new_subject_digest, selected_finding_id, request_json, result_json,"
                " selected_finding_json, new_case_json, new_binding_json,"
                " invalidation_event_json, receipt_json, committed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.remediation_id,
                    request.old_case_id,
                    new_case.case_id,
                    request.old_subject_digest,
                    new_case.subject_digest,
                    request.human_selected_finding_id,
                    request_json,
                    result_json,
                    finding_json,
                    new_case_json,
                    new_binding_json,
                    invalidation_json,
                    _canonical_model_json(receipt),
                    receipt.committed_at.isoformat(),
                ),
            )
            if next_old_state.case.state != "INVALIDATED":
                raise StoreConflictError("old case was not invalidated")
            conn.commit()
            return receipt
        except StoreConflictError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise StoreConflictError(f"remediation persistence conflict: {exc}") from exc
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to commit remediation {request.remediation_id!r}: {exc}"
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_remediation(self, remediation_id: str) -> RemediationCommitReceipt:
        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            row = conn.execute(
                "SELECT * FROM assurance_remediations WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
            if row is None:
                raise CaseNotFoundError(
                    f"remediation {remediation_id!r} not found"
                )
            return self._remediation_from_row(row)
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to load remediation {remediation_id!r}: {exc}"
            ) from exc
        finally:
            conn.close()

    def list_remediations(
        self, case_id: str | None = None
    ) -> tuple[RemediationCommitReceipt, ...]:
        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            if case_id is None:
                rows = conn.execute(
                    "SELECT * FROM assurance_remediations"
                    " ORDER BY committed_at ASC, remediation_id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assurance_remediations"
                    " WHERE old_case_id = ? OR new_case_id = ?"
                    " ORDER BY committed_at ASC, remediation_id ASC",
                    (case_id, case_id),
                ).fetchall()
            return tuple(self._remediation_from_row(row) for row in rows)
        except sqlite3.Error as exc:
            raise StorePersistenceError(f"failed to list remediations: {exc}") from exc
        finally:
            conn.close()

    def append_release_observation(
        self,
        case_id: str,
        value: ReleaseObservation | ReleaseObservationImportResult,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> StoredReleaseObservation:
        """Append a declared manual observation or verified raw import."""

        observation, receipt = self._validated_observation(value, artifact_store)
        if not isinstance(case_id, str) or not case_id.strip():
            raise StoreConflictError("case_id must be a non-blank string")
        observation_json = _canonical_model_json(observation)
        receipt_json = _canonical_model_json(receipt) if receipt is not None else None

        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_case(conn, case_id)
            if observation.subject_digest != state.case.subject_digest:
                raise StoreConflictError("release observation subject is stale")
            row = conn.execute(
                "SELECT * FROM assurance_release_observations"
                " WHERE observation_id = ?",
                (observation.observation_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["case_id"] != case_id
                    or row["observation_json"] != observation_json
                    or row["import_receipt_json"] != receipt_json
                ):
                    raise StoreConflictError(
                        f"observation_id {observation.observation_id!r} already exists"
                        " with different content"
                    )
                record = self._observation_from_row(row)
                conn.commit()
                return record

            record = StoredReleaseObservation(
                case_id=case_id,
                observation=observation,
                import_receipt=receipt,
                stored_at=observation.recorded_at,
            )
            conn.execute(
                "INSERT INTO assurance_release_observations"
                " (observation_id, case_id, subject_digest, observation_json,"
                " import_receipt_json, stored_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    observation.observation_id,
                    case_id,
                    observation.subject_digest,
                    observation_json,
                    receipt_json,
                    record.stored_at.isoformat(),
                ),
            )
            conn.commit()
            return record
        except StoreConflictError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise StoreConflictError(
                f"release observation persistence conflict: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to append release observation: {exc}"
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_release_observations(
        self, case_id: str
    ) -> tuple[StoredReleaseObservation, ...]:
        conn = self._connect()
        try:
            self._ensure_lifecycle_initialized(conn)
            self._load_case(conn, case_id)
            rows = conn.execute(
                "SELECT * FROM assurance_release_observations WHERE case_id = ?"
                " ORDER BY stored_at ASC, observation_id ASC",
                (case_id,),
            ).fetchall()
            return tuple(self._observation_from_row(row) for row in rows)
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to list release observations for {case_id!r}: {exc}"
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _validate_remediation_inputs(
        request: RemediationRequest,
        result: RemediationResult,
        selected_finding: Finding,
        new_case: AcceptanceCase,
        new_binding: AcceptanceBinding,
        invalidation_event: AcceptanceEvent,
    ) -> None:
        exact = (
            (request, RemediationRequest, "request"),
            (result, RemediationResult, "result"),
            (selected_finding, Finding, "selected_finding"),
            (new_case, AcceptanceCase, "new_case"),
            (new_binding, AcceptanceBinding, "new_binding"),
            (invalidation_event, AcceptanceEvent, "invalidation_event"),
        )
        for value, model, name in exact:
            if type(value) is not model:
                raise StoreConflictError(f"{name} must be an exact {model.__name__}")
        try:
            request = RemediationRequest.model_validate(request.model_dump(mode="json"))
            result = RemediationResult.model_validate(result.model_dump(mode="json"))
            selected_finding = Finding.model_validate(
                selected_finding.model_dump(mode="json")
            )
        except ValidationError as exc:
            raise StoreConflictError("remediation contract is invalid") from exc
        if result.status is not RemediationStatus.SUCCEEDED:
            raise StoreConflictError("only a successful prepared remediation may commit")
        if (
            result.remediation_id != request.remediation_id
            or result.old_case_id != request.old_case_id
            or result.old_subject_digest != request.old_subject_digest
            or result.human_selected_finding_id
            != request.human_selected_finding_id
        ):
            raise StoreConflictError("remediation request/result binding mismatch")
        if (
            selected_finding.finding_id != request.human_selected_finding_id
            or selected_finding.subject_digest != request.old_subject_digest
            or selected_finding.status != "open"
        ):
            raise StoreConflictError("selected Finding is not the requested open Finding")
        if (
            result.rerun_roles != (selected_finding.reviewer_role,)
            or result.new_subject_digest is None
            or new_case.subject_digest != result.new_subject_digest
            or new_binding.subject_digest != result.new_subject_digest
        ):
            raise StoreConflictError("new subject or reviewer rerun binding mismatch")
        if new_case.state != "DRAFT" or new_case.case_id == request.old_case_id:
            raise StoreConflictError("remediation must create a distinct DRAFT case")
        if new_case.created_at != new_case.updated_at:
            raise StoreConflictError("new DRAFT case must start without hidden history")
        expected_reason = (
            f"remediation:{request.remediation_id}:superseded_by:{new_case.case_id}"
        )
        if (
            invalidation_event.kind != "INVALIDATE"
            or invalidation_event.subject_digest != request.old_subject_digest
            or invalidation_event.reason != expected_reason
            or invalidation_event.occurred_at != new_case.created_at
        ):
            raise StoreConflictError("invalidation event is not bound to the new case")
        if request.requested_at > invalidation_event.occurred_at:
            raise StoreConflictError("remediation cannot commit before it was requested")

    @staticmethod
    def _validated_observation(
        value: ReleaseObservation | ReleaseObservationImportResult,
        artifact_store: ArtifactStore | None,
    ) -> tuple[ReleaseObservation, ReleaseObservationImportReceipt | None]:
        try:
            if type(value) is ReleaseObservation:
                observation = ReleaseObservation.model_validate(
                    value.model_dump(mode="json")
                )
                if observation.source != "manual":
                    raise StoreConflictError(
                        "import observations require a verified import result"
                    )
                if artifact_store is not None:
                    raise StoreConflictError(
                        "manual observation must not supply an artifact store"
                    )
                return observation, None
            if type(value) is not ReleaseObservationImportResult:
                raise StoreConflictError(
                    "value must be an exact ReleaseObservation or import result"
                )
            imported = ReleaseObservationImportResult.model_validate(
                value.model_dump(mode="json")
            )
            if type(artifact_store) is not ArtifactStore:
                raise StoreConflictError(
                    "import observation requires its exact ArtifactStore"
                )
            try:
                raw = artifact_store.get_bytes(imported.receipt.payload_digest)
                replayed = ReleaseObservationImporter.import_bytes(
                    raw,
                    expected_subject_digest=imported.observation.subject_digest,
                    artifact_store=artifact_store,
                )
            except Exception as exc:
                raise StoreConflictError(
                    "import observation raw artifact is missing or invalid"
                ) from exc
            if replayed != imported:
                raise StoreConflictError(
                    "import observation does not match its raw artifact"
                )
            return imported.observation, imported.receipt
        except ValidationError as exc:
            raise StoreConflictError("release observation contract is invalid") from exc

    def _remediation_from_row(self, row: sqlite3.Row) -> RemediationCommitReceipt:
        try:
            request = RemediationRequest.model_validate_json(row["request_json"])
            result = RemediationResult.model_validate_json(row["result_json"])
            finding = Finding.model_validate_json(row["selected_finding_json"])
            new_case = AcceptanceCase.model_validate_json(row["new_case_json"])
            new_binding = AcceptanceBinding.model_validate_json(
                row["new_binding_json"]
            )
            invalidation = AcceptanceEvent.model_validate_json(
                row["invalidation_event_json"]
            )
            receipt = RemediationCommitReceipt.model_validate_json(row["receipt_json"])
            self._validate_remediation_inputs(
                request, result, finding, new_case, new_binding, invalidation
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise LifecycleProjectionError("invalid stored remediation JSON") from exc
        expected = {
            "remediation_id": receipt.remediation_id,
            "old_case_id": receipt.old_case_id,
            "new_case_id": receipt.new_case_id,
            "old_subject_digest": receipt.old_subject_digest,
            "new_subject_digest": receipt.new_subject_digest,
            "selected_finding_id": receipt.human_selected_finding_id,
            "committed_at": receipt.committed_at.isoformat(),
        }
        if any(row[column] != value for column, value in expected.items()):
            raise LifecycleProjectionError(
                "stored remediation columns do not match receipt"
            )
        if receipt.result_digest != _sha256_text(row["result_json"]):
            raise LifecycleProjectionError("stored remediation result digest mismatch")
        return receipt

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> StoredReleaseObservation:
        try:
            observation = ReleaseObservation.model_validate_json(
                row["observation_json"]
            )
            receipt = (
                ReleaseObservationImportReceipt.model_validate_json(
                    row["import_receipt_json"]
                )
                if row["import_receipt_json"] is not None
                else None
            )
            record = StoredReleaseObservation(
                case_id=row["case_id"],
                observation=observation,
                import_receipt=receipt,
                stored_at=row["stored_at"],
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise LifecycleProjectionError(
                "invalid stored release observation JSON"
            ) from exc
        if (
            row["observation_id"] != observation.observation_id
            or row["subject_digest"] != observation.subject_digest
        ):
            raise LifecycleProjectionError(
                "stored release observation columns do not match JSON"
            )
        return record

    def _ensure_lifecycle_initialized(self, conn: sqlite3.Connection) -> None:
        self._ensure_initialized(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='assurance_lifecycle_schema_migrations'"
        ).fetchone()
        if row is None or self._validate_lifecycle_history(conn) < _LIFECYCLE_SCHEMA_VERSION:
            raise StorePersistenceError(
                "lifecycle store is not initialized; call initialize()"
            )

    @staticmethod
    def _validate_lifecycle_history(conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT version FROM assurance_lifecycle_schema_migrations"
            " ORDER BY version ASC"
        ).fetchall()
        for expected, row in enumerate(rows, start=1):
            if row["version"] != expected:
                raise StoreMigrationError(
                    "lifecycle migration history must be a contiguous prefix"
                )
        current = len(rows)
        if current > _LIFECYCLE_SCHEMA_VERSION:
            raise StoreMigrationError(
                f"lifecycle schema {current} is newer than supported"
            )
        return current

    @staticmethod
    def _create_remediation_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE assurance_remediations ("
            "remediation_id TEXT PRIMARY KEY,"
            "old_case_id TEXT NOT NULL UNIQUE,"
            "new_case_id TEXT NOT NULL UNIQUE,"
            "old_subject_digest TEXT NOT NULL,"
            "new_subject_digest TEXT NOT NULL,"
            "selected_finding_id TEXT NOT NULL,"
            "request_json TEXT NOT NULL,"
            "result_json TEXT NOT NULL,"
            "selected_finding_json TEXT NOT NULL,"
            "new_case_json TEXT NOT NULL,"
            "new_binding_json TEXT NOT NULL,"
            "invalidation_event_json TEXT NOT NULL,"
            "receipt_json TEXT NOT NULL,"
            "committed_at TEXT NOT NULL,"
            "FOREIGN KEY(old_case_id) REFERENCES assurance_cases(case_id),"
            "FOREIGN KEY(new_case_id) REFERENCES assurance_cases(case_id)"
            ")"
        )

    @staticmethod
    def _create_release_observation_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE assurance_release_observations ("
            "observation_id TEXT PRIMARY KEY,"
            "case_id TEXT NOT NULL,"
            "subject_digest TEXT NOT NULL,"
            "observation_json TEXT NOT NULL,"
            "import_receipt_json TEXT,"
            "stored_at TEXT NOT NULL,"
            "FOREIGN KEY(case_id) REFERENCES assurance_cases(case_id)"
            ")"
        )


__all__ = [
    "LifecycleProjectionError",
    "RemediationCommitReceipt",
    "SQLiteAssuranceLifecycleStore",
    "StoredReleaseObservation",
]
