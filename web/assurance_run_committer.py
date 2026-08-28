"""Atomic GP-03 persistence adapter for complete assurance runs.

The adapter intentionally knows only the synchronous SQLite boundary.  All
Git, artifact, reviewer and network work happens before :meth:`commit_run` is
called; this module performs validation and one short ``BEGIN IMMEDIATE``
transaction over the lifecycle store and the additive Web run tables.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from assurance.run_service import (
    AssuranceRunBundle,
    AssuranceRunResult,
    FreshnessSourceBinding,
    ReviewerRunRecord,
)
from assurance.contracts import (
    AcceptanceCase,
    ChangeSubject,
    Evidence,
    ExecutionReceipt,
    Finding,
    HumanDecision,
    PolicyDecision,
)
from assurance.commands import CommandBatchResult
from assurance.intake import IntakeResult, IntakeSnapshot
from assurance.manifest import EvidenceManifest, EvidenceManifestResult
from assurance.policy import PolicyEvaluationInput, PolicyGateResult
from assurance.risk import (
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskDeclarations,
)
from assurance.snapshot import GitSnapshot, GitSnapshotResult
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from assurance.single_reviewer import ReviewQuestion


RUN_SCHEMA_VERSION = 1
_RUN_MIGRATION_TABLE = "assurance_run_schema_migrations"
_RUN_TABLE = "assurance_web_runs"
_RUN_POINTER_OPERATION = "run"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AssuranceRunPersistenceError(Exception):
    """The run schema or an immutable stored run is not trustworthy."""


class AssuranceRunMigrationError(AssuranceRunPersistenceError):
    """Run migration history is missing, corrupt, or newer than this code."""


class AssuranceRunConflictError(AssuranceRunPersistenceError):
    """A key, digest, or deterministic case is already bound elsewhere."""


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_canonical_json(raw: str, label: str) -> Any:
    try:
        value = json.loads(raw)
        canonical = _canonical_json(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AssuranceRunPersistenceError(f"invalid stored {label} JSON") from exc
    if canonical != raw:
        raise AssuranceRunPersistenceError(f"stored {label} JSON is not canonical")
    return value


def _ensure_run_schema(conn: sqlite3.Connection) -> None:
    """Apply/verify only the additive GP-03 migration on an open connection."""

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_RUN_MIGRATION_TABLE} ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    _validate_migration_table(conn)
    rows = conn.execute(
        f"SELECT version FROM {_RUN_MIGRATION_TABLE} ORDER BY version ASC"
    ).fetchall()
    for expected, row in enumerate(rows, start=1):
        version = row["version"]
        if version != expected:
            raise AssuranceRunMigrationError(
                "run migration history must be a contiguous prefix"
            )
    version = len(rows)
    if version > RUN_SCHEMA_VERSION:
        raise AssuranceRunMigrationError(
            f"run schema {version} is newer than supported {RUN_SCHEMA_VERSION}"
        )
    if version < 1:
        conn.execute(
            f"CREATE TABLE {_RUN_TABLE} ("
            "idempotency_key TEXT PRIMARY KEY,"
            "request_digest TEXT NOT NULL,"
            "run_id TEXT NOT NULL UNIQUE,"
            "case_id TEXT NOT NULL,"
            "subject_digest TEXT NOT NULL,"
            "bundle_json TEXT NOT NULL,"
            "source_binding_json TEXT NOT NULL,"
            "committed_at TEXT NOT NULL,"
            "FOREIGN KEY(case_id) REFERENCES assurance_cases(case_id))"
        )
        conn.execute(
            f"CREATE INDEX assurance_web_runs_case ON {_RUN_TABLE}"
            "(case_id, committed_at, run_id)"
        )
        conn.execute(
            f"INSERT INTO {_RUN_MIGRATION_TABLE} (version, applied_at)"
            " VALUES (1, datetime('now'))"
        )
    _validate_run_schema_objects(conn)


def _validate_run_schema_objects(conn: sqlite3.Connection) -> None:
    _validate_migration_table(conn)
    _validate_idempotency_primary_key(conn)
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (_RUN_TABLE,),
    ).fetchone()
    if table is None:
        raise AssuranceRunMigrationError("run schema migration is missing the run table")
    expected_columns = [
        ("idempotency_key", "TEXT", 0, None, 1),
        ("request_digest", "TEXT", 1, None, 0),
        ("run_id", "TEXT", 1, None, 0),
        ("case_id", "TEXT", 1, None, 0),
        ("subject_digest", "TEXT", 1, None, 0),
        ("bundle_json", "TEXT", 1, None, 0),
        ("source_binding_json", "TEXT", 1, None, 0),
        ("committed_at", "TEXT", 1, None, 0),
    ]
    _validate_table_columns(conn, _RUN_TABLE, expected_columns)

    indexes = conn.execute(f"PRAGMA index_list({_RUN_TABLE})").fetchall()
    if len(indexes) != 3:
        raise AssuranceRunMigrationError("run table indexes do not match migration v1")
    found = set()
    for index in indexes:
        name = index["name"]
        columns = _index_columns(conn, name)
        unique = index["unique"]
        partial = index["partial"]
        origin = index["origin"]
        if name == "assurance_web_runs_case":
            if unique != 0 or partial != 0 or columns != (
                "case_id",
                "committed_at",
                "run_id",
            ):
                raise AssuranceRunMigrationError(
                    "run case index constraints do not match migration v1"
                )
            _validate_index_xinfo(
                conn, name, ("case_id", "committed_at", "run_id")
            )
            found.add("case")
        elif unique == 1 and partial == 0 and origin == "pk" and columns == (
            "idempotency_key",
        ):
            _validate_index_xinfo(conn, name, ("idempotency_key",))
            found.add("idempotency")
        elif unique == 1 and partial == 0 and origin == "u" and columns == (
            "run_id",
        ):
            _validate_index_xinfo(conn, name, ("run_id",))
            found.add("run")
        else:
            raise AssuranceRunMigrationError("run table indexes do not match migration v1")
    if found != {"case", "idempotency", "run"}:
        raise AssuranceRunMigrationError("run table indexes do not match migration v1")

    foreign_keys = conn.execute(f"PRAGMA foreign_key_list({_RUN_TABLE})").fetchall()
    expected_foreign_key = (
        "assurance_cases",
        "case_id",
        "case_id",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    )
    actual_foreign_keys = tuple(
        (
            row["table"],
            row["from"],
            row["to"],
            row["on_update"],
            row["on_delete"],
            row["match"],
        )
        for row in foreign_keys
    )
    if actual_foreign_keys != (expected_foreign_key,):
        raise AssuranceRunMigrationError(
            "run table foreign key does not match migration v1"
        )


def _validate_idempotency_primary_key(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name = 'assurance_web_idempotency'"
    ).fetchone()
    if table is None:
        raise AssuranceRunMigrationError(
            "web idempotency table is missing its primary key"
        )
    indexes = conn.execute("PRAGMA index_list(assurance_web_idempotency)").fetchall()
    primary_keys = [
        row
        for row in indexes
        if row["unique"] == 1 and row["origin"] == "pk" and row["partial"] == 0
    ]
    if len(primary_keys) != 1:
        raise AssuranceRunMigrationError(
            "web idempotency primary key does not match the required schema"
        )
    index = primary_keys[0]
    if _index_columns(conn, index["name"]) != ("idempotency_key",):
        raise AssuranceRunMigrationError(
            "web idempotency primary key does not match the required schema"
        )
    _validate_index_xinfo(conn, index["name"], ("idempotency_key",))


def _validate_migration_table(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (_RUN_MIGRATION_TABLE,),
    ).fetchone()
    if table is None:
        raise AssuranceRunMigrationError(
            "run store is not initialized; call initialize()"
        )
    _validate_table_columns(
        conn,
        _RUN_MIGRATION_TABLE,
        [
            ("version", "INTEGER", 0, None, 1),
            ("applied_at", "TEXT", 1, None, 0),
        ],
    )
    indexes = conn.execute(
        f"PRAGMA index_list({_RUN_MIGRATION_TABLE})"
    ).fetchall()
    if indexes:
        raise AssuranceRunMigrationError(
            "run migration table indexes do not match migration v1"
        )


def _validate_table_columns(
    conn: sqlite3.Connection,
    table: str,
    expected: list[tuple[str, str, int, str | None, int]],
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    actual = [
        (
            row["name"],
            str(row["type"]).upper(),
            row["notnull"],
            row["dflt_value"],
            row["pk"],
        )
        for row in rows
    ]
    if actual != expected:
        raise AssuranceRunMigrationError(
            f"{table} columns do not match migration v1"
        )


def _index_columns(conn: sqlite3.Connection, name: str) -> tuple[str, ...]:
    escaped = name.replace("'", "''")
    rows = conn.execute(f"PRAGMA index_info('{escaped}')").fetchall()
    return tuple(row["name"] for row in sorted(rows, key=lambda item: item["seqno"]))


def _validate_index_xinfo(
    conn: sqlite3.Connection, name: str, expected_columns: tuple[str, ...]
) -> None:
    escaped = name.replace("'", "''")
    rows = sorted(
        conn.execute(f"PRAGMA index_xinfo('{escaped}')").fetchall(),
        key=lambda item: item["seqno"],
    )
    if len(rows) != len(expected_columns) + 1:
        raise AssuranceRunMigrationError(
            "run index key columns do not match migration v1"
        )
    expected_cids = {
        "idempotency_key": 0,
        "run_id": 2,
        "case_id": 3,
        "committed_at": 7,
    }
    for row, column in zip(rows[:-1], expected_columns):
        if (
            row["name"] != column
            or row["cid"] != expected_cids[column]
            or row["desc"] != 0
            or row["coll"] != "BINARY"
            or row["key"] != 1
        ):
            raise AssuranceRunMigrationError(
                "run index key columns do not match migration v1"
            )
    tail = rows[-1]
    if (
        tail["cid"] != -1
        or tail["name"] is not None
        or tail["desc"] != 0
        or tail["coll"] != "BINARY"
        or tail["key"] != 0
    ):
        raise AssuranceRunMigrationError(
            "run index rowid suffix does not match migration v1"
        )


def initialize_run_schema(conn: sqlite3.Connection) -> None:
    """Public migration helper used by ``AssuranceWebRepository.initialize``."""

    _ensure_run_schema(conn)


def _validate_run_schema(conn: sqlite3.Connection) -> None:
    """Verify an already-initialized run schema without writing to it."""

    _validate_migration_table(conn)
    rows = conn.execute(
        f"SELECT version FROM {_RUN_MIGRATION_TABLE} ORDER BY version ASC"
    ).fetchall()
    for expected, row in enumerate(rows, start=1):
        if row["version"] != expected:
            raise AssuranceRunMigrationError(
                "run migration history must be a contiguous prefix"
            )
    version = len(rows)
    if version > RUN_SCHEMA_VERSION:
        raise AssuranceRunMigrationError(
            f"run schema {version} is newer than supported {RUN_SCHEMA_VERSION}"
        )
    if version < RUN_SCHEMA_VERSION:
        raise AssuranceRunMigrationError(
            "run store is not initialized; call initialize()"
        )
    _validate_run_schema_objects(conn)


class AssuranceRunStoreAdapter:
    """Small named adapter kept as the only bridge to the Web repository."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def lookup_run(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | None:
        return self._repository._lookup_run_in_transaction_boundary(
            idempotency_key, request_digest
        )

    def commit_run(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        return self._repository._commit_run_in_transaction_boundary(
            bundle,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

    def _commit_run_in_transaction(
        self,
        connection_or_unit_of_work: Any,
        *args: Any,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        return _commit_run_in_transaction(
            connection_or_unit_of_work,
            *args,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )


class AssuranceRunCommitter:
    """RunCommitter implementation backed by ``AssuranceWebRepository``."""

    def __init__(self, repository: Any) -> None:
        self._adapter = AssuranceRunStoreAdapter(repository)

    def lookup_run(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | None:
        return self._adapter.lookup_run(idempotency_key, request_digest)

    def commit_run(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        return self._adapter.commit_run(
            bundle,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

    def _commit_run_in_transaction(
        self,
        connection_or_unit_of_work: Any,
        *args: Any,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        return self._adapter._commit_run_in_transaction(
            connection_or_unit_of_work,
            *args,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

    # The GP-02 protocol uses the short names.  Keep both spellings so the
    # persistence adapter can be passed directly to AssuranceRunService.
    def lookup(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | None:
        return self.lookup_run(idempotency_key, request_digest)

    def commit(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        return self.commit_run(
            bundle,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )


class SQLiteAssuranceRunCommitter(AssuranceRunCommitter):
    """Descriptive alias for callers that select adapters by storage type."""


def _validate_run_arguments(
    bundle: AssuranceRunBundle,
    idempotency_key: str,
    request_digest: str,
) -> None:
    if type(bundle) is not AssuranceRunBundle:
        raise TypeError("bundle must be an exact AssuranceRunBundle")
    if type(idempotency_key) is not str or not idempotency_key.strip():
        raise ValueError("idempotency_key must be nonblank")
    if type(request_digest) is not str or not request_digest.startswith("sha256:"):
        raise ValueError("request_digest must be a sha256 digest")
    if bundle.idempotency_key != idempotency_key:
        raise AssuranceRunConflictError("bundle idempotency key does not match call")
    if bundle.request_digest != request_digest:
        raise AssuranceRunConflictError("bundle request digest does not match call")
    if type(bundle.freshness_source_binding) is not FreshnessSourceBinding:
        raise AssuranceRunPersistenceError("bundle freshness source is invalid")
    if type(bundle.reviewer) is not ReviewerRunRecord:
        raise AssuranceRunPersistenceError("bundle reviewer record is invalid")
    if type(bundle.execution_receipt) is not ExecutionReceipt:
        raise AssuranceRunPersistenceError("bundle execution receipt is invalid")
    try:
        payload = {
            name: getattr(bundle, name) for name in AssuranceRunBundle.model_fields
        }
        rebound = AssuranceRunBundle.model_validate(payload)
    except (ValidationError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise AssuranceRunPersistenceError("bundle contract is invalid") from exc
    if type(rebound) is not AssuranceRunBundle or rebound != bundle:
        raise AssuranceRunPersistenceError("bundle contract is not exact")


def _public_bundle_json(bundle: AssuranceRunBundle) -> str:
    value = bundle.model_dump(mode="json")
    # Field(exclude=True) is the privacy boundary.  Keep this assertion close
    # to serialization so a future refactor cannot silently re-expose paths.
    if "freshness_source_binding" in value:
        raise AssuranceRunPersistenceError("freshness source leaked into public bundle")
    return _canonical_json(value)


def _source_binding_json(source: FreshnessSourceBinding) -> str:
    return _canonical_json(source.model_dump(mode="json"))


def _json_digest(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _result_pointer(
    bundle: AssuranceRunBundle,
    *,
    bundle_json: str | None = None,
    source_binding_json: str | None = None,
) -> dict[str, str]:
    if bundle_json is None:
        bundle_json = _public_bundle_json(bundle)
    if source_binding_json is None:
        source_binding_json = _source_binding_json(bundle.freshness_source_binding)
    source_digest = _json_digest(source_binding_json)
    if source_digest != bundle.freshness_source_binding_digest:
        raise AssuranceRunPersistenceError(
            "source binding digest does not match bundle anchor"
        )
    return {
        "schema_version": "v1",
        "idempotency_key": bundle.idempotency_key,
        "case_id": bundle.case.case_id,
        "request_digest": bundle.request_digest,
        "run_id": bundle.run_id,
        "subject_digest": bundle.subject.subject_digest,
        "bundle_digest": _json_digest(bundle_json),
        "source_binding_digest": source_digest,
    }


def _commit_run_in_transaction(
    connection_or_unit_of_work: Any,
    *args: Any,
    idempotency_key: str,
    request_digest: str,
) -> AssuranceRunResult:
    """Persist a run row and pointer on an already-open assurance UOW.

    Accepted call forms are ``(unit_of_work, bundle)`` and
    ``(connection, unit_of_work, bundle)``.  This helper deliberately does
    not connect, begin, commit, or roll back; its caller owns that boundary.
    """

    if len(args) == 1:
        unit_of_work = connection_or_unit_of_work
        bundle = args[0]
        conn = getattr(unit_of_work, "connection", None)
    elif len(args) == 2:
        conn = connection_or_unit_of_work
        unit_of_work, bundle = args
        if getattr(unit_of_work, "connection", None) is not conn:
            raise TypeError("connection and unit_of_work must be the same UOW")
    else:
        raise TypeError(
            "run transaction helper requires (unit_of_work, bundle) or"
            " (connection, unit_of_work, bundle)"
        )
    if conn is None or not callable(getattr(conn, "execute", None)):
        raise TypeError("an existing SQLite connection/UOW is required")
    if not all(
        callable(getattr(unit_of_work, name, None))
        for name in ("load_case", "get_binding")
    ):
        raise TypeError("unit_of_work does not expose case replay operations")

    _validate_run_schema(conn)
    _validate_run_arguments(bundle, idempotency_key, request_digest)
    public_json = _public_bundle_json(bundle)
    source_json = _source_binding_json(bundle.freshness_source_binding)

    existing = conn.execute(
        f"SELECT * FROM {_RUN_TABLE} WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    pointer = conn.execute(
        "SELECT operation, payload_digest, result_json"
        " FROM assurance_web_idempotency WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise AssuranceRunConflictError(
                "idempotency key is bound to another request digest"
            )
        winner = _load_bundle_from_row(existing)
        _assert_row_columns(existing, winner)
        _load_pointer(conn, idempotency_key, winner)
        _assert_canonical_draft(unit_of_work, winner)
        if winner != bundle:
            raise AssuranceRunConflictError(
                "idempotency key is bound to different run content"
            )
        return AssuranceRunResult(
            run_id=winner.run_id,
            request_digest=winner.request_digest,
            cached=True,
            bundle=winner,
        )
    if pointer is not None:
        if (
            pointer["operation"] != _RUN_POINTER_OPERATION
            or pointer["payload_digest"] != request_digest
        ):
            raise AssuranceRunConflictError(
                "idempotency key is already used by another operation"
            )
        raise AssuranceRunPersistenceError(
            "run idempotency pointer exists without its run row"
        )

    _assert_canonical_draft(unit_of_work, bundle)
    # The Bundle contract makes this one case key equal to the evidence-gated
    # ``case`` key.  The canonical ``assurance_cases`` row remains the DRAFT
    # projection checked above; no evidence-gated Case is written here.
    canonical_case_id = bundle.draft_case.case_id
    existing_case_run = conn.execute(
        f"SELECT idempotency_key FROM {_RUN_TABLE} WHERE case_id = ?",
        (canonical_case_id,),
    ).fetchone()
    if existing_case_run is not None:
        raise AssuranceRunConflictError(
            "canonical case is already bound to another run"
        )

    committed_at = bundle.completed_at.isoformat()
    try:
        conn.execute(
            f"INSERT INTO {_RUN_TABLE} (idempotency_key, request_digest,"
            " run_id, case_id, subject_digest, bundle_json,"
            " source_binding_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                idempotency_key,
                request_digest,
                bundle.run_id,
                canonical_case_id,
                bundle.subject.subject_digest,
                public_json,
                source_json,
                committed_at,
            ),
        )
        pointer_data = _result_pointer(
            bundle,
            bundle_json=public_json,
            source_binding_json=source_json,
        )
        conn.execute(
            "INSERT INTO assurance_web_idempotency"
            " (idempotency_key, operation, payload_digest, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                idempotency_key,
                _RUN_POINTER_OPERATION,
                request_digest,
                _canonical_json(pointer_data),
                committed_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise AssuranceRunConflictError(
            f"run persistence conflict: {exc}"
        ) from exc

    row = conn.execute(
        f"SELECT * FROM {_RUN_TABLE} WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        raise AssuranceRunPersistenceError("run row disappeared during transaction")
    stored = _load_bundle_from_row(row)
    _assert_row_columns(row, stored)
    _load_pointer(conn, idempotency_key, stored)
    if stored != bundle:
        raise AssuranceRunPersistenceError(
            "stored run does not equal exact prepared bundle"
        )
    return AssuranceRunResult(
        run_id=bundle.run_id,
        request_digest=request_digest,
        cached=False,
        bundle=bundle,
    )


def _assert_canonical_draft(unit_of_work: Any, bundle: AssuranceRunBundle) -> None:
    canonical_state = unit_of_work.load_case(bundle.draft_case.case_id)
    if canonical_state.case != bundle.draft_case:
        raise AssuranceRunConflictError(
            "canonical case does not equal bundle draft_case"
        )
    if canonical_state.case.state != "DRAFT":
        raise AssuranceRunConflictError(
            "canonical case must remain the bundle DRAFT case"
        )
    if unit_of_work.get_binding(bundle.draft_case.case_id) != bundle.binding:
        raise AssuranceRunConflictError(
            "canonical binding does not equal bundle binding"
        )


def _load_bundle_from_row(row: sqlite3.Row) -> AssuranceRunBundle:
    bundle_data = _parse_canonical_json(row["bundle_json"], "bundle")
    source_data = _parse_canonical_json(row["source_binding_json"], "source binding")
    if not isinstance(bundle_data, dict) or not isinstance(source_data, dict):
        raise AssuranceRunPersistenceError("stored run JSON must be objects")
    if "freshness_source_binding" in bundle_data:
        raise AssuranceRunPersistenceError("stored public bundle contains local source")
    try:
        source = FreshnessSourceBinding.model_validate(source_data)
        # A few domain contracts intentionally require exact nested model
        # instances at raw validation.  Rehydrate those layers explicitly
        # before asking the bundle to perform its full cross-field replay
        # validation; this is stricter than accepting a loose JSON dict.
        subject = ChangeSubject.model_validate_json(_canonical_json(bundle_data["subject"]))
        draft_case = AcceptanceCase.model_validate_json(
            _canonical_json(bundle_data["draft_case"])
        )
        case = AcceptanceCase.model_validate_json(_canonical_json(bundle_data["case"]))
        binding = AcceptanceBinding.model_validate_json(
            _canonical_json(bundle_data["binding"])
        )
        git = GitSnapshotResult.model_validate_json(_canonical_json(bundle_data["git"]))
        intake = IntakeResult.model_validate_json(_canonical_json(bundle_data["intake"]))
        commands = CommandBatchResult.model_validate_json(
            _canonical_json(bundle_data["commands"])
        )
        manifest = EvidenceManifestResult.model_validate_json(
            _canonical_json(bundle_data["manifest"])
        )
        risk_data = bundle_data["risk"]
        risk_input_data = risk_data["input"]
        risk_input = RiskClassificationInput.model_validate(
            {
                "schema_version": risk_input_data.get("schema_version", "v1"),
                "snapshot": GitSnapshot.model_validate_json(
                    _canonical_json(risk_input_data["snapshot"])
                ),
                "intake": IntakeSnapshot.model_validate_json(
                    _canonical_json(risk_input_data["intake"])
                ),
                "manifest": EvidenceManifest.model_validate_json(
                    _canonical_json(risk_input_data["manifest"])
                ),
                "declarations": RiskDeclarations.model_validate_json(
                    _canonical_json(risk_input_data["declarations"])
                ),
            }
        )
        risk = RiskClassificationResult.model_validate(
            {
                "schema_version": risk_data.get("schema_version", "v1"),
                "input": risk_input,
                "classification": RiskClassification.model_validate_json(
                    _canonical_json(risk_data["classification"])
                ),
            }
        )
        receipt = ExecutionReceipt.model_validate_json(
            _canonical_json(bundle_data["execution_receipt"])
        )
        policy_data = bundle_data["policy"]
        policy_input_data = policy_data["input"]
        policy_input = PolicyEvaluationInput.model_validate(
            {
                "schema_version": policy_input_data.get("schema_version", "v1"),
                "subject": subject,
                "risk_result": risk,
                "findings": tuple(
                    Finding.model_validate_json(_canonical_json(item))
                    for item in policy_input_data.get("findings", [])
                ),
                "execution_receipts": (receipt,),
                "human_decisions": tuple(
                    HumanDecision.model_validate_json(_canonical_json(item))
                    for item in policy_input_data.get("human_decisions", [])
                ),
                "evaluated_at": policy_input_data["evaluated_at"],
            }
        )
        policy = PolicyGateResult.model_validate(
            {
                "schema_version": policy_data.get("schema_version", "v1"),
                "input": policy_input,
                "decision": PolicyDecision.model_validate_json(
                    _canonical_json(policy_data["decision"])
                ),
            }
        )
        payload = dict(bundle_data)
        payload.update(
            {
                "subject": subject,
                "draft_case": draft_case,
                "case": case,
                "binding": binding,
                "git": git,
                "intake": intake,
                "commands": commands,
                "manifest": manifest,
                "risk": risk,
                "evidence": tuple(
                    Evidence.model_validate_json(_canonical_json(item))
                    for item in bundle_data.get("evidence", [])
                ),
                "findings": tuple(
                    Finding.model_validate_json(_canonical_json(item))
                    for item in bundle_data.get("findings", [])
                ),
                "questions": tuple(
                    ReviewQuestion.model_validate_json(_canonical_json(item))
                    for item in bundle_data.get("questions", [])
                ),
                "reviewer": ReviewerRunRecord.model_validate_json(
                    _canonical_json(bundle_data["reviewer"])
                ),
                "execution_receipt": receipt,
                "policy": policy,
                "events": tuple(
                    AcceptanceEvent.model_validate_json(_canonical_json(item))
                    for item in bundle_data.get("events", [])
                ),
                "freshness_source_binding": source,
            }
        )
        bundle = AssuranceRunBundle.model_validate(payload)
    except (
        ValidationError,
        TypeError,
        ValueError,
        KeyError,
        IndexError,
        AttributeError,
    ) as exc:
        raise AssuranceRunPersistenceError("stored run contract is invalid") from exc
    if _public_bundle_json(bundle) != row["bundle_json"]:
        raise AssuranceRunPersistenceError("stored bundle does not round-trip canonically")
    if _source_binding_json(source) != row["source_binding_json"]:
        raise AssuranceRunPersistenceError("stored source binding does not round-trip canonically")
    expected = {
        "idempotency_key": bundle.idempotency_key,
        "request_digest": bundle.request_digest,
        "run_id": bundle.run_id,
        "case_id": bundle.case.case_id,
        "subject_digest": bundle.subject.subject_digest,
        "committed_at": bundle.completed_at.isoformat(),
    }
    for column, value in expected.items():
        if row[column] != value:
            raise AssuranceRunPersistenceError(
                f"stored run column {column!r} does not match bundle"
            )
    try:
        committed_at = datetime.fromisoformat(row["committed_at"])
    except (TypeError, ValueError) as exc:
        raise AssuranceRunPersistenceError("stored run committed_at is invalid") from exc
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise AssuranceRunPersistenceError("stored run committed_at must be timezone-aware")
    return bundle


def _load_pointer(conn: sqlite3.Connection, key: str, bundle: AssuranceRunBundle) -> None:
    row = conn.execute(
        "SELECT idempotency_key, operation, payload_digest, result_json FROM assurance_web_idempotency"
        " WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise AssuranceRunPersistenceError("run idempotency pointer is missing")
    run_row = conn.execute(
        "SELECT idempotency_key, bundle_json, source_binding_json FROM assurance_web_runs"
        " WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if run_row is None:
        raise AssuranceRunPersistenceError("run idempotency row is missing")
    if (
        key != row["idempotency_key"]
        or key != run_row["idempotency_key"]
        or key != bundle.idempotency_key
    ):
        raise AssuranceRunPersistenceError(
            "run idempotency key binding does not match winner"
        )
    expected = _result_pointer(
        bundle,
        bundle_json=run_row["bundle_json"],
        source_binding_json=run_row["source_binding_json"],
    )
    try:
        pointer = _parse_canonical_json(row["result_json"], "run idempotency pointer")
    except AssuranceRunPersistenceError:
        raise
    if (
        row["operation"] != _RUN_POINTER_OPERATION
        or row["payload_digest"] != bundle.request_digest
        or pointer != expected
    ):
        raise AssuranceRunPersistenceError("run idempotency pointer does not match winner")


def _assert_row_columns(row: sqlite3.Row, bundle: AssuranceRunBundle) -> None:
    for column, value in (
        ("idempotency_key", bundle.idempotency_key),
        ("request_digest", bundle.request_digest),
        ("run_id", bundle.run_id),
        ("case_id", bundle.case.case_id),
        ("subject_digest", bundle.subject.subject_digest),
    ):
        if row[column] != value:
            raise AssuranceRunPersistenceError(
                f"stored run column {column!r} does not match winner"
            )


__all__ = [
    "AssuranceRunCommitter",
    "AssuranceRunConflictError",
    "AssuranceRunMigrationError",
    "AssuranceRunPersistenceError",
    "AssuranceRunStoreAdapter",
    "RUN_SCHEMA_VERSION",
    "SQLiteAssuranceRunCommitter",
    "_validate_run_schema",
    "initialize_run_schema",
]
