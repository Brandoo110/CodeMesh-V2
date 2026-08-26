"""Focused contract tests for assurance.store (V2-P1-04C)."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import assurance
from assurance import (
    AcceptanceBinding,
    AcceptanceEvent,
    AssuranceStoreError,
    CaseNotFoundError,
    HumanDecision,
    InvalidTransitionError,
    PolicyDecision,
    ProjectionIntegrityError,
    SQLiteAssuranceStore,
    StaleSubjectError,
    StoreConflictError,
    StoreMigrationError,
    StorePersistenceError,
)
from assurance import store as store_module
from assurance.contracts import AcceptanceCase


_T0 = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)


def _digest(letter="a"):
    return "sha256:" + letter * 64


def _ts(minutes):
    return _T0 + timedelta(minutes=minutes)


def _case(**overrides):
    values = {
        "schema_version": "v1",
        "case_id": "case-001",
        "subject_digest": _digest(),
        "state": "DRAFT",
        "evidence_refs": (),
        "finding_refs": (),
        "execution_receipt_refs": (),
        "policy_decision_refs": (),
        "human_decision_refs": (),
        "conditions": (),
        "conflicts": (),
        "missing_evidence": (),
        "invalidation_reason": None,
        "created_at": _ts(0),
        "updated_at": _ts(0),
    }
    values.update(overrides)
    return AcceptanceCase(**values)


def _binding(**overrides):
    values = {
        "schema_version": "v1",
        "subject_digest": _digest(),
        "policy_version": "policy-1",
        "rubric_version": "rubric-1",
        "waiver_id": None,
        "waiver_expires_at": None,
    }
    values.update(overrides)
    return AcceptanceBinding(**values)


def _event(kind, event_id="event-001", at_minutes=1, **overrides):
    facts = {
        "COLLECT_EVIDENCE": {"evidence_refs": ("evidence-new",)},
        "REQUEST_EVIDENCE": {"missing_evidence": ("missing-new",)},
        "RECORD_CONFLICT": {"conflicts": ("conflict-new",)},
        "CONDITIONALLY_ACCEPT": {
            "conditions": ("condition-new",),
            "policy_decision_refs": ("policy-new",),
            "human_decision_refs": ("human-new",),
        },
        "ACCEPT": {
            "policy_decision_refs": ("policy-new",),
            "human_decision_refs": ("human-new",),
        },
        "REJECT": {"policy_decision_refs": ("policy-new",)},
        "INVALIDATE": {"reason": "invalidated by reason"},
    }
    values = {
        "schema_version": "v1",
        "event_id": event_id,
        "subject_digest": _digest(),
        "kind": kind,
        "occurred_at": _ts(at_minutes),
    }
    values.update(facts[kind])
    values.update(overrides)
    return AcceptanceEvent(**values)


def _policy_decision(decision_id="policy-001", at_minutes=5, **overrides):
    values = {
        "schema_version": "v1",
        "decision_id": decision_id,
        "subject_digest": _digest(),
        "policy_version": "policy-1",
        "rules_digest": _digest("b"),
        "outcome": "PASS",
        "reason_codes": (),
        "required_collectors": (),
        "required_reviewers": (),
        "required_human_role": None,
        "evaluated_evidence_refs": (),
        "evaluated_finding_refs": (),
        "evaluated_receipt_refs": (),
        "waiver_ref": None,
        "evaluated_at": _ts(at_minutes),
    }
    values.update(overrides)
    return PolicyDecision(**values)


def _human_decision(decision_id="human-001", at_minutes=6, **overrides):
    values = {
        "schema_version": "v1",
        "decision_id": decision_id,
        "subject_digest": _digest(),
        "actor_type": "human",
        "owner": "junjie",
        "owner_role": "owner",
        "decision": "approve",
        "reason": "approved",
        "conditions": (),
        "waiver_id": None,
        "expires_at": None,
        "decided_at": _ts(at_minutes),
    }
    values.update(overrides)
    return HumanDecision(**values)


def _raw(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _init_store(db_path):
    store = SQLiteAssuranceStore(db_path)
    store.initialize()
    return store


def _created_store(tmp_path):
    store = _init_store(tmp_path / "assurance.sqlite")
    store.create_case(_case(), _binding())
    return store


def _event_count(db_path):
    with closing(_raw(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM assurance_case_events"
        ).fetchone()[0]


def _decision_count(db_path):
    with closing(_raw(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM assurance_decisions"
        ).fetchone()[0]


def test_migration_v2_schema_columns_pk_unique_fk_and_idempotence(tmp_path):
    db_path = tmp_path / "nested" / "assurance.sqlite"
    store = _init_store(db_path)
    assert store.schema_version() == 2
    store.initialize()
    assert store.schema_version() == 2
    with closing(_raw(db_path)) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {
            "assurance_schema_migrations",
            "assurance_cases",
            "assurance_case_events",
            "assurance_decisions",
        }
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM assurance_schema_migrations"
            ).fetchone()[0]
            == 2
        )
        case_cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_cases)")
        ]
        assert case_cols == [
            "case_id",
            "subject_digest",
            "initial_case_json",
            "binding_json",
            "created_at",
        ]
        assert [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_cases)")
            if row["pk"]
        ] == ["case_id"]
        event_cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_case_events)")
        ]
        assert event_cols == [
            "case_id",
            "sequence",
            "event_id",
            "subject_digest",
            "event_json",
            "recorded_at",
        ]
        assert [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assurance_case_events)"
            )
            if row["pk"]
        ] == ["case_id", "sequence"]
        index_list = {
            (row["origin"], row["unique"])
            for row in conn.execute(
                "PRAGMA index_list(assurance_case_events)"
            )
        }
        assert index_list == {("pk", 1), ("u", 1)}
        fk_list = [
            (row["from"], row["table"], row["to"])
            for row in conn.execute(
                "PRAGMA foreign_key_list(assurance_case_events)"
            )
        ]
        assert fk_list == [("case_id", "assurance_cases", "case_id")]
        decision_cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_decisions)")
        ]
        assert decision_cols == [
            "case_id",
            "sequence",
            "decision_kind",
            "decision_id",
            "subject_digest",
            "decision_json",
            "recorded_at",
        ]
        assert [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_decisions)")
            if row["pk"]
        ] == ["case_id", "sequence"]
        decision_indexes = {
            (row["origin"], row["unique"])
            for row in conn.execute(
                "PRAGMA index_list(assurance_decisions)"
            )
        }
        assert decision_indexes == {("pk", 1), ("u", 1)}
        decision_fks = [
            (row["from"], row["table"], row["to"])
            for row in conn.execute(
                "PRAGMA foreign_key_list(assurance_decisions)"
            )
        ]
        assert decision_fks == [
            ("case_id", "assurance_cases", "case_id")
        ]
        user_indexes = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        assert user_indexes == []
        assert (
            conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
        )
        conn.execute(
            "INSERT INTO assurance_cases"
            " (case_id, subject_digest, initial_case_json,"
            " binding_json, created_at)"
            " VALUES ('raw-case', ?, '{}', '{}', 't')",
            (_digest(),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_case_events"
                " (case_id, sequence, event_id, subject_digest,"
                " event_json, recorded_at)"
                " VALUES ('raw-case', 0, 'e0', ?, '{}', 't')",
                (_digest(),),
            )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO assurance_decisions"
            " (case_id, sequence, decision_kind, decision_id,"
            " subject_digest, decision_json, recorded_at)"
            " VALUES ('raw-case', 1, 'policy', 'd1', ?, '{}', 't')",
            (_digest(),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES ('raw-case', 1, 'policy', 'd-other', ?, '{}', 't')",
                (_digest(),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES ('raw-case', 2, 'policy', 'd1', ?, '{}', 't')",
                (_digest(),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES ('raw-case', 0, 'policy', 'd0', ?, '{}', 't')",
                (_digest(),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES ('raw-case', 2, 'other', 'd2', ?, '{}', 't')",
                (_digest(),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assurance_decisions"
                " (case_id, sequence, decision_kind, decision_id,"
                " subject_digest, decision_json, recorded_at)"
                " VALUES ('missing-case', 2, 'policy', 'd3', ?, '{}', 't')",
                (_digest(),),
            )


def test_migration_v2_and_policy_decision_append(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    assert store.schema_version() == 2
    with closing(_raw(db_path)) as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM assurance_schema_migrations"
                " ORDER BY version ASC"
            )
        ]
        assert versions == [1, 2]
        table = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name = 'assurance_decisions'"
        ).fetchone()
        assert table is not None
    store.create_case(_case(), _binding())
    decision = _policy_decision()
    assert store.append_policy_decision("case-001", decision) == decision
    assert store.list_decisions("case-001") == (decision,)


def test_real_v1_db_upgrades_to_v2_without_changing_case_event_bytes(
    tmp_path,
):
    db_path = tmp_path / "legacy-v1.sqlite"
    case = _case()
    binding = _binding()
    event = _event(
        "COLLECT_EVIDENCE",
        event_id="e1",
        at_minutes=1,
        evidence_refs=("ev-1",),
    )
    with closing(_raw(db_path)) as conn:
        conn.execute(
            "CREATE TABLE assurance_schema_migrations ("
            "version INTEGER PRIMARY KEY,"
            "applied_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO assurance_schema_migrations"
            " (version, applied_at) VALUES (1, '2026-08-25T03:00:00+00:00')"
        )
        conn.execute(
            "CREATE TABLE assurance_cases ("
            "case_id TEXT PRIMARY KEY,"
            "subject_digest TEXT NOT NULL,"
            "initial_case_json TEXT NOT NULL,"
            "binding_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE assurance_case_events ("
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
        conn.execute(
            "INSERT INTO assurance_cases"
            " (case_id, subject_digest, initial_case_json,"
            " binding_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                case.case_id,
                case.subject_digest,
                json.dumps(
                    case.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                json.dumps(
                    binding.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                case.created_at.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO assurance_case_events"
            " (case_id, sequence, event_id, subject_digest,"
            " event_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                case.case_id,
                1,
                event.event_id,
                event.subject_digest,
                json.dumps(
                    event.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                "2026-08-25T03:01:00+00:00",
            ),
        )
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {
            "assurance_schema_migrations",
            "assurance_cases",
            "assurance_case_events",
        }
        assert (
            conn.execute(
                "SELECT MAX(version) FROM assurance_schema_migrations"
            ).fetchone()[0]
            == 1
        )
        before = conn.execute(
            "SELECT initial_case_json, binding_json FROM assurance_cases"
            " WHERE case_id = 'case-001'"
        ).fetchone()
        before_event = conn.execute(
            "SELECT event_json FROM assurance_case_events WHERE sequence = 1"
        ).fetchone()
        conn.commit()
    upgraded = SQLiteAssuranceStore(db_path)
    upgraded.initialize()
    assert upgraded.schema_version() == 2
    state = upgraded.load_case("case-001")
    assert state.case.case_id == case.case_id
    assert state.case.subject_digest == case.subject_digest
    assert state.case.state == "EVIDENCE_COLLECTED"
    assert state.case.evidence_refs == ("ev-1",)
    assert state.applied_events == (event,)
    assert upgraded.get_binding("case-001") == binding
    with closing(_raw(db_path)) as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM assurance_schema_migrations"
                " ORDER BY version ASC"
            )
        ]
        assert versions == [1, 2]
        after = conn.execute(
            "SELECT initial_case_json, binding_json FROM assurance_cases"
            " WHERE case_id = 'case-001'"
        ).fetchone()
        after_event = conn.execute(
            "SELECT event_json FROM assurance_case_events WHERE sequence = 1"
        ).fetchone()
        assert after == before
        assert after_event == before_event


def test_decisions_share_sequence_across_kinds_and_round_trip(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    p1 = _policy_decision(decision_id="p1", at_minutes=5)
    h1 = _human_decision(decision_id="h1", at_minutes=6)
    p2 = _policy_decision(decision_id="p2", at_minutes=7)
    assert store.append_policy_decision("case-001", p1) == p1
    assert store.append_human_decision("case-001", h1) == h1
    assert store.append_policy_decision("case-001", p2) == p2
    with closing(_raw(db_path)) as conn:
        rows = conn.execute(
            "SELECT sequence, decision_kind, decision_id, decision_json"
            " FROM assurance_decisions ORDER BY sequence ASC"
        ).fetchall()
        assert [
            (row["sequence"], row["decision_kind"], row["decision_id"])
            for row in rows
        ] == [
            (1, "policy", "p1"),
            (2, "human", "h1"),
            (3, "policy", "p2"),
        ]
        expected_compact = json.dumps(
            p1.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert rows[0]["decision_json"] == expected_compact
    decisions = SQLiteAssuranceStore(db_path).list_decisions("case-001")
    assert decisions == (p1, h1, p2)
    assert all(
        isinstance(decision, (PolicyDecision, HumanDecision))
        for decision in decisions
    )
    assert isinstance(decisions[0], PolicyDecision)
    assert isinstance(decisions[1], HumanDecision)
    assert isinstance(decisions[2], PolicyDecision)


def test_decision_retry_idempotent_conflict_and_cross_kind_same_id(
    tmp_path,
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    policy = _policy_decision(decision_id="shared-id", at_minutes=5)
    first = store.append_policy_decision("case-001", policy)
    again = store.append_policy_decision(
        "case-001", _policy_decision(decision_id="shared-id", at_minutes=5)
    )
    assert again == first
    assert _decision_count(db_path) == 1
    with pytest.raises(StoreConflictError):
        store.append_policy_decision(
            "case-001",
            _policy_decision(decision_id="shared-id", at_minutes=6),
        )
    assert _decision_count(db_path) == 1
    human = _human_decision(decision_id="shared-id", at_minutes=6)
    assert store.append_human_decision("case-001", human) == human
    assert _decision_count(db_path) == 2
    assert store.list_decisions("case-001") == (policy, human)


def test_decision_append_failures_write_zero_rows(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    assert store.list_decisions("case-001") == ()
    with pytest.raises(TypeError):
        store.append_policy_decision("case-001", _human_decision())
    with pytest.raises(TypeError):
        store.append_human_decision("case-001", {"decision_id": "x"})
    with pytest.raises(CaseNotFoundError):
        store.append_policy_decision("missing", _policy_decision())
    with pytest.raises(CaseNotFoundError):
        store.append_human_decision("missing", _human_decision())
    with pytest.raises(CaseNotFoundError):
        store.list_decisions("missing")
    with pytest.raises(StoreConflictError):
        store.append_policy_decision(
            "case-001", _policy_decision(subject_digest=_digest("b"))
        )
    with pytest.raises(StoreConflictError):
        store.append_human_decision(
            "case-001", _human_decision(subject_digest=_digest("b"))
        )
    assert _decision_count(db_path) == 0
    expected = _policy_decision()
    assert store.append_policy_decision("case-001", expected) == expected
    with closing(_raw(db_path)) as conn:
        sequences = [
            row["sequence"]
            for row in conn.execute(
                "SELECT sequence FROM assurance_decisions"
            )
        ]
        assert sequences == [1]


@pytest.mark.parametrize(
    "corruption",
    [
        "decision-json",
        "decision-kind",
        "decision-id-column",
        "decision-subject",
        "decision-gap",
    ],
)
def test_decision_corruption_fails_closed(tmp_path, corruption):
    db_path = tmp_path / f"{corruption}.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    store.append_policy_decision(
        "case-001", _policy_decision(decision_id="p1")
    )
    store.append_human_decision(
        "case-001", _human_decision(decision_id="h1")
    )
    with closing(_raw(db_path)) as conn:
        if corruption == "decision-json":
            conn.execute(
                "UPDATE assurance_decisions SET decision_json = '{broken'"
                " WHERE sequence = 1"
            )
        elif corruption == "decision-kind":
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                "UPDATE assurance_decisions SET decision_kind = 'tampered'"
                " WHERE sequence = 1"
            )
        elif corruption == "decision-id-column":
            conn.execute(
                "UPDATE assurance_decisions SET decision_id = 'tampered'"
                " WHERE sequence = 1"
            )
        elif corruption == "decision-subject":
            conn.execute(
                "UPDATE assurance_decisions SET subject_digest = ?"
                " WHERE sequence = 1",
                (_digest("z"),),
            )
        elif corruption == "decision-gap":
            conn.execute(
                "DELETE FROM assurance_decisions WHERE sequence = 1"
            )
        conn.commit()
    with pytest.raises(ProjectionIntegrityError) as excinfo:
        SQLiteAssuranceStore(db_path).list_decisions("case-001")
    assert "case-001" in str(excinfo.value)
    assert "sequence" in str(excinfo.value)
    with pytest.raises(ProjectionIntegrityError):
        SQLiteAssuranceStore(db_path).append_policy_decision(
            "case-001", _policy_decision(decision_id="p2")
        )
    assert _decision_count(db_path) == (
        1 if corruption == "decision-gap" else 2
    )


def test_case_event_projection_corruption_prevents_decision_append(
    tmp_path,
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    store.append_event(
        "case-001",
        _event(
            "COLLECT_EVIDENCE",
            event_id="e1",
            at_minutes=1,
            evidence_refs=("ev-1",),
        ),
    )
    with closing(_raw(db_path)) as conn:
        conn.execute(
            "UPDATE assurance_case_events SET event_json = '{broken'"
            " WHERE sequence = 1"
        )
        conn.commit()
    with pytest.raises(ProjectionIntegrityError):
        store.append_policy_decision("case-001", _policy_decision())
    with pytest.raises(ProjectionIntegrityError):
        store.list_decisions("case-001")
    assert _decision_count(db_path) == 0


class _CommitFailProxy:
    """Wraps a real connection and fails the first decision-insert commit."""

    def __init__(self, real_conn):
        self._conn = real_conn
        self._saw_decision_insert = False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        if sql.lstrip().upper().startswith(
            "INSERT INTO ASSURANCE_DECISIONS"
        ):
            self._saw_decision_insert = True
        return cursor

    def commit(self):
        if self._saw_decision_insert:
            raise sqlite3.OperationalError("simulated commit failure")
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def test_decision_commit_failure_rolls_back_and_next_append_uses_sequence_1(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _CommitFailProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(store_module.sqlite3, "connect", failing_connect)
    with pytest.raises(StorePersistenceError):
        store.append_policy_decision(
            "case-001", _policy_decision(decision_id="p1")
        )
    monkeypatch.undo()
    assert _decision_count(db_path) == 0
    assert SQLiteAssuranceStore(db_path).load_case(
        "case-001"
    ).applied_events == ()
    assert SQLiteAssuranceStore(db_path).list_decisions("case-001") == ()
    expected = _policy_decision(decision_id="p1")
    assert store.append_policy_decision("case-001", expected) == expected
    with closing(_raw(db_path)) as conn:
        rows = conn.execute(
            "SELECT sequence, decision_id FROM assurance_decisions"
        ).fetchall()
        assert [(row["sequence"], row["decision_id"]) for row in rows] == [
            (1, "p1")
        ]


class _MigrationFailProxy:
    """Fails migration-2 row insert after the decisions table was created."""

    def __init__(self, real_conn):
        self._conn = real_conn
        self._saw_create_decisions = False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql, params=()):
        stripped = sql.lstrip().upper()
        if stripped.startswith("CREATE TABLE ASSURANCE_DECISIONS"):
            self._saw_create_decisions = True
        if (
            self._saw_create_decisions
            and stripped.startswith(
                "INSERT INTO ASSURANCE_SCHEMA_MIGRATIONS"
            )
        ):
            raise sqlite3.OperationalError(
                "simulated migration-2 row failure"
            )
        return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def test_migration_v2_partial_failure_rolls_back_and_retry_upgrades(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    with closing(_raw(db_path)) as conn:
        conn.execute("DROP TABLE assurance_decisions")
        conn.execute(
            "DELETE FROM assurance_schema_migrations WHERE version = 2"
        )
        conn.commit()
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _MigrationFailProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(store_module.sqlite3, "connect", failing_connect)
    with pytest.raises(StorePersistenceError):
        store.initialize()
    monkeypatch.undo()
    with closing(_raw(db_path)) as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM assurance_schema_migrations"
                " ORDER BY version ASC"
            )
        ]
        assert versions == [1]
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='assurance_decisions'"
            ).fetchone()
            is None
        )
    store.initialize()
    assert store.schema_version() == 2
    assert store.load_case("case-001").case == _case()
    with closing(_raw(db_path)) as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM assurance_schema_migrations"
                " ORDER BY version ASC"
            )
        ]
        assert versions == [1, 2]
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='assurance_decisions'"
            ).fetchone()
            is not None
        )


@pytest.mark.parametrize("tamper", ["future", "gap", "non-positive"])
def test_invalid_migration_history_fails_closed_and_preserves_data(
    tmp_path, tamper
):
    db_path = tmp_path / f"{tamper}.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    with closing(_raw(db_path)) as conn:
        if tamper == "future":
            conn.execute(
                "INSERT INTO assurance_schema_migrations"
                " (version, applied_at)"
                " VALUES (3, '2026-08-25T04:00:00+00:00')"
            )
        elif tamper == "gap":
            conn.execute(
                "DELETE FROM assurance_schema_migrations WHERE version = 1"
            )
        elif tamper == "non-positive":
            conn.execute(
                "INSERT INTO assurance_schema_migrations"
                " (version, applied_at)"
                " VALUES (0, '2026-08-25T04:00:00+00:00')"
            )
        conn.commit()
        before_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    with pytest.raises(StoreMigrationError):
        store.schema_version()
    with pytest.raises(StoreMigrationError):
        store.load_case("case-001")
    with pytest.raises(StoreMigrationError):
        store.initialize()
    with closing(_raw(db_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM assurance_cases").fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM assurance_case_events"
            ).fetchone()[0]
            == 0
        )
        after_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert after_tables == before_tables


def test_create_and_load_draft_round_trip(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    case = _case()
    binding = _binding()
    created = store.create_case(case, binding)
    assert created.case == case
    assert created.applied_events == ()
    assert store.load_case(case.case_id) == created
    assert store.get_binding(case.case_id) == binding


def test_second_instance_loads_same_database_after_restart(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    first = _init_store(db_path)
    case = _case()
    binding = _binding()
    first.create_case(case, binding)
    second = SQLiteAssuranceStore(db_path)
    assert second.load_case("case-001") == first.load_case("case-001")
    assert second.get_binding("case-001") == binding


def test_append_collect_evidence_replays_exact_refs_and_no_snapshot(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    event = _event(
        "COLLECT_EVIDENCE", evidence_refs=("ev-1", "ev-2")
    )
    state = store.append_event("case-001", event)
    assert state.case.state == "EVIDENCE_COLLECTED"
    assert state.case.evidence_refs == ("ev-1", "ev-2")
    assert state.applied_events == (event,)
    reopened = SQLiteAssuranceStore(db_path).load_case("case-001")
    assert reopened == state
    with closing(_raw(db_path)) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "assurance_case_snapshots" not in tables
        case_cols = [
            row["name"]
            for row in conn.execute("PRAGMA table_info(assurance_cases)")
        ]
        assert "current_state_json" not in case_cols
        assert "projection_json" not in case_cols


def test_two_events_receive_sequences_1_and_2_and_replay(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    ev1 = _event(
        "COLLECT_EVIDENCE",
        event_id="e1",
        at_minutes=1,
        evidence_refs=("ev-1",),
    )
    ev2 = _event(
        "REQUEST_EVIDENCE",
        event_id="e2",
        at_minutes=2,
        missing_evidence=("missing-1",),
    )
    s1 = store.append_event("case-001", ev1)
    s2 = store.append_event("case-001", ev2)
    assert s1.case.evidence_refs == ("ev-1",)
    assert s2.case.state == "NEEDS_EVIDENCE"
    assert s2.case.evidence_refs == ("ev-1",)
    assert s2.case.missing_evidence == ("missing-1",)
    assert s2.applied_events == (ev1, ev2)
    with closing(_raw(db_path)) as conn:
        sequences = [
            row["sequence"]
            for row in conn.execute(
                "SELECT sequence FROM assurance_case_events"
                " ORDER BY sequence ASC"
            )
        ]
        assert sequences == [1, 2]
    assert SQLiteAssuranceStore(db_path).load_case("case-001") == s2


def test_exact_create_retry_is_idempotent_and_conflicts_preserve_original(
    tmp_path,
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    case = _case()
    binding = _binding()
    first = store.create_case(case, binding)
    assert store.create_case(_case(), _binding()) == first
    assert store.create_case(_case(), _binding()).case == case
    changed_case = _case(updated_at=_ts(1))
    with pytest.raises(StoreConflictError):
        store.create_case(changed_case, binding)
    assert store.load_case("case-001").case == case
    changed_binding = _binding(rubric_version="rubric-2")
    with pytest.raises(StoreConflictError):
        store.create_case(case, changed_binding)
    assert store.get_binding("case-001") == binding
    with closing(_raw(db_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM assurance_cases").fetchone()[0]
            == 1
        )


def test_create_case_retry_compares_canonical_models_not_raw_whitespace(
    tmp_path,
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    case = _case()
    binding = _binding()
    first = store.create_case(case, binding)
    pretty_case = json.dumps(
        case.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )
    pretty_binding = json.dumps(
        binding.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        indent=4,
    )
    with closing(_raw(db_path)) as conn:
        row = conn.execute(
            "SELECT initial_case_json, binding_json FROM assurance_cases"
        ).fetchone()
        assert row["initial_case_json"] != pretty_case
        assert row["binding_json"] != pretty_binding
        conn.execute(
            "UPDATE assurance_cases SET initial_case_json = ?,"
            " binding_json = ? WHERE case_id = ?",
            (pretty_case, pretty_binding, case.case_id),
        )
        conn.commit()
    retried = store.create_case(_case(), _binding())
    assert retried == first
    assert store.load_case(case.case_id) == first
    assert store.get_binding(case.case_id) == binding
    with closing(_raw(db_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM assurance_cases").fetchone()[0]
            == 1
        )
        row = conn.execute(
            "SELECT initial_case_json, binding_json FROM assurance_cases"
        ).fetchone()
        assert row["initial_case_json"] == pretty_case
        assert row["binding_json"] == pretty_binding
    changed = _case(updated_at=_ts(1))
    with pytest.raises(StoreConflictError):
        store.create_case(changed, _binding())
    assert store.load_case(case.case_id) == first


def test_create_case_precondition_rejections(tmp_path):
    store = _init_store(tmp_path / "assurance.sqlite")
    with pytest.raises(StoreConflictError):
        store.create_case({"case_id": "x"}, _binding())
    with pytest.raises(StoreConflictError):
        store.create_case(_case(), {"subject_digest": _digest()})
    non_draft = _case(
        state="EVIDENCE_COLLECTED", evidence_refs=("ev-1",)
    )
    with pytest.raises(StoreConflictError):
        store.create_case(non_draft, _binding())
    with pytest.raises(StoreConflictError):
        store.create_case(_case(subject_digest=_digest("b")), _binding())


def test_duplicate_event_idempotent_and_conflict_add_no_rows(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    event = _event(
        "COLLECT_EVIDENCE", event_id="e1", evidence_refs=("ev-1",)
    )
    state = store.append_event("case-001", event)
    again = store.append_event(
        "case-001",
        _event(
            "COLLECT_EVIDENCE", event_id="e1", evidence_refs=("ev-1",)
        ),
    )
    assert again == state
    assert _event_count(db_path) == 1
    with pytest.raises(StoreConflictError):
        store.append_event(
            "case-001",
            _event(
                "COLLECT_EVIDENCE",
                event_id="e1",
                evidence_refs=("ev-different",),
            ),
        )
    assert _event_count(db_path) == 1
    assert SQLiteAssuranceStore(db_path).load_case(
        "case-001"
    ).applied_events == (event,)


def test_rejected_events_add_no_row_and_later_sequence_stays_contiguous(
    tmp_path,
):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    with pytest.raises(InvalidTransitionError):
        store.append_event(
            "case-001",
            _event(
                "ACCEPT",
                event_id="bad-transition",
                policy_decision_refs=("p",),
                human_decision_refs=("h",),
            ),
        )
    with pytest.raises(StaleSubjectError):
        store.append_event(
            "case-001",
            _event(
                "COLLECT_EVIDENCE",
                event_id="bad-subject",
                subject_digest=_digest("b"),
                evidence_refs=("ev-1",),
            ),
        )
    with pytest.raises(InvalidTransitionError):
        store.append_event(
            "case-001",
            _event(
                "COLLECT_EVIDENCE",
                event_id="bad-time",
                at_minutes=-1,
                evidence_refs=("ev-1",),
            ),
        )
    assert _event_count(db_path) == 0
    ev1 = _event(
        "COLLECT_EVIDENCE",
        event_id="e1",
        at_minutes=1,
        evidence_refs=("ev-1",),
    )
    ev2 = _event(
        "REQUEST_EVIDENCE",
        event_id="e2",
        at_minutes=2,
        missing_evidence=("missing-1",),
    )
    store.append_event("case-001", ev1)
    state = store.append_event("case-001", ev2)
    with closing(_raw(db_path)) as conn:
        sequences = [
            row["sequence"]
            for row in conn.execute(
                "SELECT sequence FROM assurance_case_events"
                " ORDER BY sequence ASC"
            )
        ]
        assert sequences == [1, 2]
    assert SQLiteAssuranceStore(db_path).load_case("case-001") == state


def test_missing_case_and_binding_errors(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    event = _event(
        "COLLECT_EVIDENCE", event_id="e1", evidence_refs=("ev-1",)
    )
    with pytest.raises(CaseNotFoundError):
        store.load_case("missing")
    with pytest.raises(CaseNotFoundError):
        store.get_binding("missing")
    with pytest.raises(CaseNotFoundError):
        store.append_event("missing", event)
    store.create_case(_case(), _binding())
    assert store.append_event("case-001", event).case.state == (
        "EVIDENCE_COLLECTED"
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "initial-json",
        "initial-subject-column",
        "event-json",
        "event-id-column",
        "missing-sequence-1",
    ],
)
def test_corruption_fails_closed_as_projection_integrity(tmp_path, corruption):
    db_path = tmp_path / f"{corruption}.sqlite"
    store = _init_store(db_path)
    store.create_case(_case(), _binding())
    if corruption in ("event-json", "event-id-column", "missing-sequence-1"):
        store.append_event(
            "case-001",
            _event(
                "COLLECT_EVIDENCE",
                event_id="e1",
                at_minutes=1,
                evidence_refs=("ev-1",),
            ),
        )
        store.append_event(
            "case-001",
            _event(
                "REQUEST_EVIDENCE",
                event_id="e2",
                at_minutes=2,
                missing_evidence=("missing-1",),
            ),
        )
    with closing(_raw(db_path)) as conn:
        if corruption == "initial-json":
            conn.execute(
                "UPDATE assurance_cases SET initial_case_json = '{broken'"
                " WHERE case_id = 'case-001'"
            )
        elif corruption == "initial-subject-column":
            conn.execute(
                "UPDATE assurance_cases SET subject_digest = ?"
                " WHERE case_id = 'case-001'",
                (_digest("z"),),
            )
        elif corruption == "event-json":
            conn.execute(
                "UPDATE assurance_case_events SET event_json = '{broken'"
                " WHERE sequence = 1"
            )
        elif corruption == "event-id-column":
            conn.execute(
                "UPDATE assurance_case_events SET event_id = 'tampered'"
                " WHERE sequence = 1"
            )
        elif corruption == "missing-sequence-1":
            conn.execute(
                "DELETE FROM assurance_case_events WHERE sequence = 1"
            )
        conn.commit()
    with pytest.raises(ProjectionIntegrityError) as excinfo:
        SQLiteAssuranceStore(db_path).load_case("case-001")
    assert "case-001" in str(excinfo.value)
    if corruption in ("event-json", "event-id-column", "missing-sequence-1"):
        assert "sequence" in str(excinfo.value)


def test_uninitialized_operations_raise_store_persistence_error(tmp_path):
    db_path = tmp_path / "fresh.sqlite"
    store = SQLiteAssuranceStore(db_path)
    with pytest.raises(StorePersistenceError):
        store.schema_version()
    with pytest.raises(StorePersistenceError):
        store.create_case(_case(), _binding())
    with pytest.raises(StorePersistenceError):
        store.load_case("case-001")
    with pytest.raises(StorePersistenceError):
        store.get_binding("case-001")
    with pytest.raises(StorePersistenceError):
        store.append_event(
            "case-001",
            _event(
                "COLLECT_EVIDENCE", evidence_refs=("ev-1",)
            ),
        )
    with pytest.raises(StorePersistenceError):
        store.append_policy_decision("case-001", _policy_decision())
    with pytest.raises(StorePersistenceError):
        store.append_human_decision("case-001", _human_decision())
    with pytest.raises(StorePersistenceError):
        store.list_decisions("case-001")
    store.initialize()
    assert store.schema_version() == 2


def _call_public_operation(store, operation):
    if operation == "initialize":
        return store.initialize()
    if operation == "schema_version":
        return store.schema_version()
    if operation == "create_case":
        return store.create_case(_case(), _binding())
    if operation == "load_case":
        return store.load_case("case-001")
    if operation == "get_binding":
        return store.get_binding("case-001")
    if operation == "append_event":
        return store.append_event(
            "case-001",
            _event("COLLECT_EVIDENCE", evidence_refs=("ev-1",)),
        )
    if operation == "append_policy_decision":
        return store.append_policy_decision(
            "case-001", _policy_decision()
        )
    if operation == "append_human_decision":
        return store.append_human_decision("case-001", _human_decision())
    if operation == "list_decisions":
        return store.list_decisions("case-001")
    raise AssertionError(f"unknown operation {operation!r}")


@pytest.mark.parametrize(
    "operation",
    [
        "initialize",
        "schema_version",
        "create_case",
        "load_case",
        "get_binding",
        "append_event",
        "append_policy_decision",
        "append_human_decision",
        "list_decisions",
    ],
)
def test_db_connection_failures_are_store_persistence_error(
    tmp_path, operation
):
    db_path = tmp_path / "db-directory"
    db_path.mkdir()
    with pytest.raises(StorePersistenceError):
        _call_public_operation(SQLiteAssuranceStore(db_path), operation)


def test_stored_json_uses_canonical_compact_encoding(tmp_path):
    db_path = tmp_path / "assurance.sqlite"
    store = _init_store(db_path)
    case = _case()
    binding = _binding()
    store.create_case(case, binding)
    with closing(_raw(db_path)) as conn:
        row = conn.execute(
            "SELECT initial_case_json, binding_json FROM assurance_cases"
        ).fetchone()
    expected = json.dumps(
        case.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert row["initial_case_json"] == expected
    assert ", " not in row["initial_case_json"]
    assert ", " not in row["binding_json"]


def test_append_event_requires_exact_event_type(tmp_path):
    store = _created_store(tmp_path)
    with pytest.raises(TypeError):
        store.append_event("case-001", {"event_id": "x"})


def test_store_public_api_is_minimal():
    public_methods = sorted(
        name for name in vars(SQLiteAssuranceStore) if not name.startswith("_")
    )
    assert public_methods == [
        "append_event",
        "append_human_decision",
        "append_policy_decision",
        "create_case",
        "get_binding",
        "initialize",
        "list_cases",
        "list_decisions",
        "load_case",
        "schema_version",
    ]


def test_exception_hierarchy_is_simple():
    assert issubclass(AssuranceStoreError, Exception)
    for cls in (
        StoreMigrationError,
        CaseNotFoundError,
        StoreConflictError,
        ProjectionIntegrityError,
        StorePersistenceError,
    ):
        assert issubclass(cls, AssuranceStoreError)


PRIOR_PUBLIC_NAMES = [
    "AcceptanceCase",
    "ChangeSubject",
    "Evidence",
    "ExecutionReceipt",
    "ExecutionStep",
    "Finding",
    "HumanDecision",
    "PolicyDecision",
    "SubjectDigestInput",
    "canonical_subject_payload",
    "changed_subject_fields",
    "compute_normalized_diff_digest",
    "compute_subject_digest",
    "normalize_line_endings",
    "normalize_repo_path",
    "normalize_repository_identity",
    "AcceptanceEvent",
    "AcceptanceBinding",
    "AcceptanceMachineState",
    "InvalidTransitionError",
    "EventConflictError",
    "StaleSubjectError",
    "apply_acceptance_event",
    "allowed_event_kinds",
    "invalidation_reasons",
    "invalidate_if_needed",
    "ArtifactStore",
    "ArtifactDigestError",
    "ArtifactNotFoundError",
    "ArtifactIntegrityError",
]
NEW_PUBLIC_NAMES = {
    "SQLiteAssuranceStore",
    "AssuranceStoreError",
    "StoreMigrationError",
    "CaseNotFoundError",
    "StoreConflictError",
    "ProjectionIntegrityError",
    "StorePersistenceError",
}


def test_package_exports_preserve_all_prior_names_and_add_store_api():
    assert set(PRIOR_PUBLIC_NAMES) <= set(assurance.__all__)
    for name in PRIOR_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.SQLiteAssuranceStore is store_module.SQLiteAssuranceStore
