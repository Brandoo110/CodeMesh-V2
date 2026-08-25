"""Synchronous SQLite-backed assurance case store (pure standard library)."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .contracts import AcceptanceCase, HumanDecision, PolicyDecision
from .state_machine import (
    AcceptanceBinding,
    AcceptanceEvent,
    AcceptanceMachineState,
    EventConflictError,
    InvalidTransitionError,
    StaleSubjectError,
    apply_acceptance_event,
)


class AssuranceStoreError(Exception):
    """Base error for assurance store failures."""


class StoreMigrationError(AssuranceStoreError):
    """Raised when migration history is invalid or newer than supported."""


class CaseNotFoundError(AssuranceStoreError):
    """Raised when a case_id does not exist in the store."""


class StoreConflictError(AssuranceStoreError):
    """Raised when immutable create/append data conflicts."""


class ProjectionIntegrityError(AssuranceStoreError):
    """Raised when stored JSON/columns/sequences cannot be replayed safely."""


class StorePersistenceError(AssuranceStoreError):
    """Raised for uninitialized access or ordinary SQLite failures."""


_SCHEMA_VERSION = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(model) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class SQLiteAssuranceStore:
    """Event- and decision-sourced assurance store with replay-only views."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def initialize(self) -> None:
        """Apply missing schema migrations sequentially in one transaction."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS assurance_schema_migrations ("
                "version INTEGER PRIMARY KEY,"
                "applied_at TEXT NOT NULL"
                ")"
            )
            version = self._validate_migration_history(conn)
            if version < 1:
                self._create_v1_tables(conn)
                conn.execute(
                    "INSERT INTO assurance_schema_migrations"
                    " (version, applied_at) VALUES (?, ?)",
                    (1, _now_iso()),
                )
            if version < 2:
                self._create_v2_table(conn)
                conn.execute(
                    "INSERT INTO assurance_schema_migrations"
                    " (version, applied_at) VALUES (?, ?)",
                    (2, _now_iso()),
                )
            conn.commit()
        except StoreMigrationError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to initialize assurance store: {exc}"
            ) from exc
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            return _SCHEMA_VERSION
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to read schema version: {exc}"
            ) from exc
        finally:
            conn.close()

    def create_case(
        self, case: AcceptanceCase, binding: AcceptanceBinding
    ) -> AcceptanceMachineState:
        if type(case) is not AcceptanceCase:
            raise StoreConflictError("case must be an exact AcceptanceCase")
        if type(binding) is not AcceptanceBinding:
            raise StoreConflictError(
                "binding must be an exact AcceptanceBinding"
            )
        if case.state != "DRAFT":
            raise StoreConflictError("create_case requires a DRAFT case")
        if case.subject_digest != binding.subject_digest:
            raise StoreConflictError(
                "case and binding subject digests must match"
            )
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT case_id, subject_digest, initial_case_json,"
                " binding_json, created_at FROM assurance_cases"
                " WHERE case_id = ?",
                (case.case_id,),
            ).fetchone()
            case_json = _canonical_json(case)
            binding_json = _canonical_json(binding)
            if row is not None:
                stored_case = self._case_from_row(row)
                stored_binding = self._binding_from_row(row)
                if (
                    _canonical_json(stored_case) != case_json
                    or _canonical_json(stored_binding) != binding_json
                ):
                    raise StoreConflictError(
                        f"case_id {case.case_id!r} already exists"
                        " with different content"
                    )
                conn.commit()
                return self._load_case(conn, case.case_id)
            conn.execute(
                "INSERT INTO assurance_cases"
                " (case_id, subject_digest, initial_case_json,"
                " binding_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    case.case_id,
                    case.subject_digest,
                    case_json,
                    binding_json,
                    case.created_at.isoformat(),
                ),
            )
            state = self._load_case(conn, case.case_id)
            conn.commit()
            return state
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to create case {case.case_id!r}: {exc}"
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_case(self, case_id: str) -> AcceptanceMachineState:
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            return self._load_case(conn, case_id)
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to load case {case_id!r}: {exc}"
            ) from exc
        finally:
            conn.close()

    def get_binding(self, case_id: str) -> AcceptanceBinding:
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            row = conn.execute(
                "SELECT case_id, subject_digest, binding_json"
                " FROM assurance_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                raise CaseNotFoundError(f"case {case_id!r} not found")
            return self._binding_from_row(row)
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to load binding for case {case_id!r}: {exc}"
            ) from exc
        finally:
            conn.close()

    def append_event(
        self, case_id: str, event: AcceptanceEvent
    ) -> AcceptanceMachineState:
        if type(event) is not AcceptanceEvent:
            raise TypeError("event must be an exact AcceptanceEvent")
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_case(conn, case_id)
            event_json = _canonical_json(event)
            for existing in state.applied_events:
                if existing.event_id == event.event_id:
                    if _canonical_json(existing) == event_json:
                        conn.commit()
                        return state
                    raise StoreConflictError(
                        f"event_id {event.event_id!r} already exists"
                        " with different content"
                    )
            new_state = apply_acceptance_event(state, event)
            sequence = len(state.applied_events) + 1
            conn.execute(
                "INSERT INTO assurance_case_events"
                " (case_id, sequence, event_id, subject_digest,"
                " event_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    sequence,
                    event.event_id,
                    event.subject_digest,
                    event_json,
                    _now_iso(),
                ),
            )
            conn.commit()
            return new_state
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to append event {event.event_id!r} to case"
                f" {case_id!r}: {exc}"
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_policy_decision(
        self, case_id: str, decision: PolicyDecision
    ) -> PolicyDecision:
        """Append an immutable policy decision to an existing case."""
        if type(decision) is not PolicyDecision:
            raise TypeError("decision must be an exact PolicyDecision")
        return self._append_decision(case_id, "policy", decision)

    def append_human_decision(
        self, case_id: str, decision: HumanDecision
    ) -> HumanDecision:
        """Append an immutable human decision to an existing case."""
        if type(decision) is not HumanDecision:
            raise TypeError("decision must be an exact HumanDecision")
        return self._append_decision(case_id, "human", decision)

    def list_decisions(
        self, case_id: str
    ) -> tuple[PolicyDecision | HumanDecision, ...]:
        """Return validated decisions in append order for a case."""
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            state = self._load_case(conn, case_id)
            return self._load_decisions(
                conn, case_id, state.case.subject_digest
            )
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to list decisions for case {case_id!r}: {exc}"
            ) from exc
        finally:
            conn.close()

    def list_cases(self) -> tuple[AcceptanceMachineState, ...]:
        """Return replayed states for all cases, newest update first."""
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            rows = conn.execute(
                "SELECT case_id FROM assurance_cases ORDER BY case_id ASC"
            ).fetchall()
            states = [self._load_case(conn, row["case_id"]) for row in rows]
            states.sort(
                key=lambda state: (
                    -state.case.updated_at.timestamp(),
                    state.case.case_id,
                )
            )
            return tuple(states)
        except sqlite3.Error as exc:
            raise StorePersistenceError(
                f"failed to list cases: {exc}"
            ) from exc
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = None
        try:
            conn = sqlite3.connect(self._db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise StorePersistenceError(
                f"failed to connect to assurance store: {exc}"
            ) from exc

    def _ensure_initialized(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name = 'assurance_schema_migrations'"
        ).fetchone()
        if row is None:
            raise StorePersistenceError(
                "assurance store is not initialized; call initialize()"
            )
        version = self._validate_migration_history(conn)
        if version < 2:
            raise StorePersistenceError(
                "assurance store is not initialized; call initialize()"
            )

    def _validate_migration_history(
        self, conn: sqlite3.Connection
    ) -> int:
        rows = conn.execute(
            "SELECT version FROM assurance_schema_migrations"
            " ORDER BY version ASC"
        ).fetchall()
        for expected, row in enumerate(rows, start=1):
            version = row["version"]
            if version != expected:
                raise StoreMigrationError(
                    "schema migration history must be a contiguous"
                    " positive integer prefix starting at 1;"
                    f" found version {version} at position {expected}"
                )
        current = len(rows)
        if current > _SCHEMA_VERSION:
            raise StoreMigrationError(
                f"schema version {current} is newer than supported"
                f" version {_SCHEMA_VERSION}; refusing to access"
            )
        return current

    def _create_v1_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS assurance_cases ("
            "case_id TEXT PRIMARY KEY,"
            "subject_digest TEXT NOT NULL,"
            "initial_case_json TEXT NOT NULL,"
            "binding_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS assurance_case_events ("
            "case_id TEXT NOT NULL,"
            "sequence INTEGER NOT NULL CHECK (sequence > 0),"
            "event_id TEXT NOT NULL,"
            "subject_digest TEXT NOT NULL,"
            "event_json TEXT NOT NULL,"
            "recorded_at TEXT NOT NULL,"
            "PRIMARY KEY (case_id, sequence),"
            "UNIQUE (case_id, event_id),"
            "FOREIGN KEY (case_id) REFERENCES assurance_cases (case_id)"
            ")"
        )

    def _create_v2_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE assurance_decisions ("
            "case_id TEXT NOT NULL,"
            "sequence INTEGER NOT NULL CHECK(sequence > 0),"
            "decision_kind TEXT NOT NULL"
            " CHECK(decision_kind IN ('policy','human')),"
            "decision_id TEXT NOT NULL,"
            "subject_digest TEXT NOT NULL,"
            "decision_json TEXT NOT NULL,"
            "recorded_at TEXT NOT NULL,"
            "PRIMARY KEY(case_id, sequence),"
            "UNIQUE(case_id, decision_kind, decision_id),"
            "FOREIGN KEY(case_id) REFERENCES assurance_cases(case_id)"
            ")"
        )

    def _append_decision(
        self, case_id: str, kind: str, decision
    ):
        conn = self._connect()
        try:
            self._ensure_initialized(conn)
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_case(conn, case_id)
            existing = self._load_decisions(
                conn, case_id, state.case.subject_digest
            )
            if decision.subject_digest != state.case.subject_digest:
                raise StoreConflictError(
                    f"decision subject does not match case {case_id!r}"
                )
            decision_json = _canonical_json(decision)
            for stored in existing:
                same_kind = (
                    isinstance(stored, PolicyDecision)
                    if kind == "policy"
                    else isinstance(stored, HumanDecision)
                )
                if not same_kind or stored.decision_id != decision.decision_id:
                    continue
                if _canonical_json(stored) == decision_json:
                    conn.commit()
                    return stored
                raise StoreConflictError(
                    f"decision_id {decision.decision_id!r} already exists"
                    f" for case {case_id!r} with different content"
                )
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    len(existing) + 1,
                    kind,
                    decision.decision_id,
                    decision.subject_digest,
                    decision_json,
                    _now_iso(),
                ),
            )
            conn.commit()
            return decision
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorePersistenceError(
                f"failed to append {kind} decision"
                f" {decision.decision_id!r} to case {case_id!r}: {exc}"
            ) from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _load_decisions(
        self,
        conn: sqlite3.Connection,
        case_id: str,
        case_subject_digest: str,
    ) -> tuple[PolicyDecision | HumanDecision, ...]:
        rows = conn.execute(
            "SELECT sequence, decision_kind, decision_id,"
            " subject_digest, decision_json"
            " FROM assurance_decisions WHERE case_id = ?"
            " ORDER BY sequence ASC",
            (case_id,),
        ).fetchall()
        decisions = []
        for expected, row in enumerate(rows, start=1):
            sequence = row["sequence"]
            if sequence != expected:
                raise ProjectionIntegrityError(
                    f"case {case_id!r}: decision sequence {sequence} is not"
                    f" contiguous, expected {expected}"
                )
            kind = row["decision_kind"]
            if kind == "policy":
                model_cls = PolicyDecision
            elif kind == "human":
                model_cls = HumanDecision
            else:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    f" invalid decision kind {kind!r}"
                )
            try:
                decision_data = json.loads(row["decision_json"])
                decision = model_cls.model_validate(decision_data)
            except (
                json.JSONDecodeError,
                ValidationError,
                TypeError,
                ValueError,
            ) as exc:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    f" invalid {kind} decision JSON"
                ) from exc
            if decision.decision_id != row["decision_id"]:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " decision JSON decision_id does not match stored column"
                )
            if decision.subject_digest != row["subject_digest"]:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " decision JSON subject does not match stored column"
                )
            if decision.subject_digest != case_subject_digest:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " decision subject does not match case subject"
                )
            decisions.append(decision)
        return tuple(decisions)

    def _load_case(
        self, conn: sqlite3.Connection, case_id: str
    ) -> AcceptanceMachineState:
        row = conn.execute(
            "SELECT case_id, subject_digest, initial_case_json,"
            " binding_json, created_at FROM assurance_cases"
            " WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise CaseNotFoundError(f"case {case_id!r} not found")
        case = self._case_from_row(row)
        binding = self._binding_from_row(row)
        if binding.subject_digest != case.subject_digest:
            raise ProjectionIntegrityError(
                f"case {case_id!r}: binding subject mismatch"
            )
        state = AcceptanceMachineState(
            schema_version="v1", case=case, applied_events=()
        )
        event_rows = conn.execute(
            "SELECT case_id, sequence, event_id, subject_digest, event_json"
            " FROM assurance_case_events WHERE case_id = ?"
            " ORDER BY sequence ASC",
            (case_id,),
        ).fetchall()
        for expected, event_row in enumerate(event_rows, start=1):
            sequence = event_row["sequence"]
            if sequence != expected:
                raise ProjectionIntegrityError(
                    f"case {case_id!r}: event sequence {sequence} is not"
                    f" contiguous, expected {expected}"
                )
            try:
                event_data = json.loads(event_row["event_json"])
                event = AcceptanceEvent.model_validate(event_data)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " invalid event JSON"
                ) from exc
            if event.event_id != event_row["event_id"]:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " event JSON event_id does not match stored column"
                )
            if event.subject_digest != event_row["subject_digest"]:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " event JSON subject does not match stored column"
                )
            if event.subject_digest != case.subject_digest:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " event subject does not match case subject"
                )
            try:
                state = apply_acceptance_event(state, event)
            except (
                InvalidTransitionError,
                StaleSubjectError,
                EventConflictError,
                ValueError,
            ) as exc:
                raise ProjectionIntegrityError(
                    f"case {case_id!r} sequence {sequence}:"
                    " replay rejected stored event"
                ) from exc
        return state

    def _case_from_row(self, row) -> AcceptanceCase:
        try:
            case_data = json.loads(row["initial_case_json"])
            case = AcceptanceCase.model_validate(case_data)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise ProjectionIntegrityError(
                f"case {row['case_id']!r}: invalid initial case JSON"
            ) from exc
        if case.case_id != row["case_id"]:
            raise ProjectionIntegrityError(
                f"case {row['case_id']!r}: initial case JSON case_id"
                " does not match stored column"
            )
        if case.subject_digest != row["subject_digest"]:
            raise ProjectionIntegrityError(
                f"case {row['case_id']!r}: initial case JSON subject"
                " does not match stored column"
            )
        return case

    def _binding_from_row(self, row) -> AcceptanceBinding:
        try:
            binding_data = json.loads(row["binding_json"])
            binding = AcceptanceBinding.model_validate(binding_data)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise ProjectionIntegrityError(
                f"case {row['case_id']!r}: invalid binding JSON"
            ) from exc
        if binding.subject_digest != row["subject_digest"]:
            raise ProjectionIntegrityError(
                f"case {row['case_id']!r}: binding subject mismatch"
            )
        return binding
