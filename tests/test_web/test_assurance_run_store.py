"""Focused GP-03 persistence tests for complete assurance runs."""

import asyncio
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.test_assurance_run_service import _Reviewer, _service
from web.assurance_store import (
    AssuranceWebError,
    AssuranceWebRepository,
)


def _durable_service(tmp_path):
    service, intent = _service(tmp_path)
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    service._committer = repository
    return service, intent, repository


def _db_rows(repository: AssuranceWebRepository, query: str):
    conn = sqlite3.connect(repository._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


def test_commit_lookup_projects_run_and_keeps_local_source_private(tmp_path):
    service, intent, repository = _durable_service(tmp_path)

    result = asyncio.run(service.run(intent, idempotency_key="run:happy"))
    assert result.cached is False
    assert result.bundle.freshness_source_binding.repository_path == intent.repository_path

    public_bundle = result.bundle.model_dump(mode="json")
    assert "freshness_source_binding" not in public_bundle
    public_json = json.dumps(public_bundle, ensure_ascii=False, sort_keys=True)
    assert str(intent.repository_path) not in public_json

    run_row = _db_rows(
        repository,
        "SELECT bundle_json, source_binding_json FROM assurance_web_runs",
    )[0]
    stored_public = json.loads(run_row["bundle_json"])
    stored_source = json.loads(run_row["source_binding_json"])
    assert "freshness_source_binding" not in stored_public
    assert str(intent.repository_path) not in run_row["bundle_json"]
    assert stored_source["repository_path"] == str(intent.repository_path)
    assert stored_source["author"] == "author-agent"
    assert stored_source["author_provenance"] == "caller_declared"

    projection = repository.get_change(result.bundle.case.case_id)
    assert len(projection["evidence"]) == 4
    assert projection["questions"] == []
    assert [item["run_id"] for item in projection["reviewer_runs"]] == [
        result.bundle.run_id
    ]
    assert [item["receipt_id"] for item in projection["receipts"]] == [
        result.bundle.execution_receipt.receipt_id
    ]
    assert projection["metadata"]["author"] == "author-agent"
    assert str(intent.repository_path) not in json.dumps(projection, ensure_ascii=False)

    looked_up = repository.lookup_run("run:happy", result.request_digest)
    assert looked_up is not None
    assert looked_up.cached is True
    assert looked_up.bundle == result.bundle


def test_lookup_requires_initialized_additive_schema_without_migrating(tmp_path):
    database = tmp_path / "uninitialized-web.sqlite"
    repository = AssuranceWebRepository(database)
    repository._store.initialize()

    with pytest.raises(AssuranceWebError, match="not initialized"):
        repository.lookup_run("run:missing", "sha256:" + "a" * 64)

    conn = sqlite3.connect(database)
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'assurance_run_schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_schema_validator_rejects_pseudo_v1_constraints_and_index(tmp_path):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute("DROP TABLE assurance_web_runs")
        conn.execute(
            "CREATE TABLE assurance_web_runs ("
            "idempotency_key TEXT, request_digest TEXT, run_id TEXT,"
            "case_id TEXT, subject_digest TEXT, bundle_json TEXT,"
            "source_binding_json TEXT, committed_at TEXT)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX assurance_web_runs_case ON assurance_web_runs"
            "(run_id, case_id, committed_at) WHERE case_id IS NOT NULL"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="columns|constraint|index|schema"):
        repository.lookup_run("run:missing", "sha256:" + "a" * 64)


def test_schema_validator_rejects_nocase_idempotency_primary_key(tmp_path):
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute("DROP TABLE assurance_web_runs")
        conn.execute(
            "CREATE TABLE assurance_web_runs ("
            "idempotency_key TEXT COLLATE NOCASE PRIMARY KEY,"
            "request_digest TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,"
            "case_id TEXT NOT NULL, subject_digest TEXT NOT NULL,"
            "bundle_json TEXT NOT NULL, source_binding_json TEXT NOT NULL,"
            "committed_at TEXT NOT NULL,"
            "FOREIGN KEY(case_id) REFERENCES assurance_cases(case_id))"
        )
        conn.execute(
            "CREATE INDEX assurance_web_runs_case ON assurance_web_runs"
            "(case_id, committed_at, run_id)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="index|collation|schema"):
        repository.lookup_run("run:missing", "sha256:" + "a" * 64)


def test_idempotency_keys_remain_case_sensitive(tmp_path):
    service, intent, repository = _durable_service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="Run:Case"))

    assert repository.lookup_run("run:case", result.request_digest) is None


def test_failed_projection_rolls_back_case_run_and_idempotency_pointer(tmp_path):
    service, intent, repository = _durable_service(tmp_path)
    original_projection = repository._projection_in_transaction

    def fail_projection(*args, **kwargs):
        raise RuntimeError("injected projection failure")

    repository._projection_in_transaction = fail_projection
    with pytest.raises(RuntimeError, match="injected projection failure"):
        asyncio.run(service.run(intent, idempotency_key="run:rollback"))
    repository._projection_in_transaction = original_projection

    counts = {
        name: _db_rows(repository, f"SELECT COUNT(*) AS count FROM {name}")[0]["count"]
        for name in (
            "assurance_cases",
            "assurance_case_events",
            "assurance_decisions",
            "assurance_web_cases",
            "assurance_web_runs",
            "assurance_web_idempotency",
        )
    }
    assert counts == {
        "assurance_cases": 0,
        "assurance_case_events": 0,
        "assurance_decisions": 0,
        "assurance_web_cases": 0,
        "assurance_web_runs": 0,
        "assurance_web_idempotency": 0,
    }

    retry = asyncio.run(service.run(intent, idempotency_key="run:rollback"))
    assert retry.cached is False


def test_concurrent_same_key_has_one_durable_winner_and_cached_replays(tmp_path):
    service, intent = _service(tmp_path)
    staged = asyncio.run(service.run(intent, idempotency_key="run:race"))
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    barrier = Barrier(6)

    def commit_once():
        barrier.wait()
        return repository.commit_run(
            staged.bundle,
            idempotency_key="run:race",
            request_digest=staged.request_digest,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: commit_once(), range(6)))

    assert sum(not item.cached for item in results) == 1
    assert sum(item.cached for item in results) == 5
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_cases"
    )[0]["count"] == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_case_events"
    )[0]["count"] == len(staged.bundle.events)
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_decisions"
    )[0]["count"] == 1


def test_lookup_rejects_corrupted_public_bundle_without_exposing_source(tmp_path):
    service, intent, repository = _durable_service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run:corrupt"))
    run_row = _db_rows(
        repository,
        "SELECT bundle_json FROM assurance_web_runs WHERE idempotency_key = 'run:corrupt'",
    )[0]
    corrupted = json.loads(run_row["bundle_json"])
    corrupted["freshness_source_binding"] = {
        "repository_path": str(intent.repository_path),
    }
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute(
            "UPDATE assurance_web_runs SET bundle_json = ?"
            " WHERE idempotency_key = ?",
            (
                json.dumps(corrupted, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "run:corrupt",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="public bundle contains local source"):
        repository.lookup_run("run:corrupt", result.request_digest)


@pytest.mark.parametrize("legacy_field", ("metadata_json", "evidence_json"))
def test_projection_rejects_legacy_facts_drift_from_run_bundle(tmp_path, legacy_field):
    service, intent, repository = _durable_service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run:legacy-drift"))
    if legacy_field == "metadata_json":
        replacement = json.dumps(
            {
                "author": "forged-author",
                "author_provenance": "caller_declared",
                "risk": result.bundle.risk.classification.risk_level,
                "run_id": result.bundle.run_id,
            },
            sort_keys=True,
        )
    else:
        evidence = [item.model_dump(mode="json") for item in result.bundle.evidence[:-1]]
        replacement = json.dumps(evidence, sort_keys=True)
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute(
            f"UPDATE assurance_web_cases SET {legacy_field} = ? WHERE case_id = ?",
            (replacement, result.bundle.case.case_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="legacy|bundle|evidence|metadata"):
        repository.lookup_run("run:legacy-drift", result.request_digest)


def test_missing_mandatory_bundle_key_is_persistence_error(tmp_path):
    service, intent, repository = _durable_service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run:missing-key"))
    row = _db_rows(
        repository,
        "SELECT bundle_json FROM assurance_web_runs"
        " WHERE idempotency_key = 'run:missing-key'",
    )[0]
    bundle_data = json.loads(row["bundle_json"])
    bundle_data.pop("reviewer")
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute(
            "UPDATE assurance_web_runs SET bundle_json = ?"
            " WHERE idempotency_key = ?",
            (
                json.dumps(bundle_data, sort_keys=True, separators=(",", ":")),
                "run:missing-key",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="stored run contract|invalid"):
        repository.lookup_run("run:missing-key", result.request_digest)


@pytest.mark.parametrize("tamper_target", ("source", "bundle"))
def test_pointer_digests_reject_semantic_bundle_or_source_tamper(tmp_path, tamper_target):
    service, intent, repository = _durable_service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run:pointer-digest"))
    conn = sqlite3.connect(repository._db_path)
    try:
        if tamper_target == "source":
            row = conn.execute(
                "SELECT source_binding_json FROM assurance_web_runs"
                " WHERE idempotency_key = 'run:pointer-digest'"
            ).fetchone()
            source = json.loads(row[0])
            source["author"] = "forged-author"
            conn.execute(
                "UPDATE assurance_web_runs SET source_binding_json = ?"
                " WHERE idempotency_key = ?",
                (
                    json.dumps(source, sort_keys=True, separators=(",", ":")),
                    "run:pointer-digest",
                ),
            )
        else:
            row = conn.execute(
                "SELECT bundle_json FROM assurance_web_runs"
                " WHERE idempotency_key = 'run:pointer-digest'"
            ).fetchone()
            bundle = json.loads(row[0])
            bundle["reviewer"]["actual_provider"] = "forged-provider"
            conn.execute(
                "UPDATE assurance_web_runs SET bundle_json = ?"
                " WHERE idempotency_key = ?",
                (
                    json.dumps(bundle, sort_keys=True, separators=(",", ":")),
                    "run:pointer-digest",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="pointer|digest|contract"):
        repository.lookup_run("run:pointer-digest", result.request_digest)


def test_model_copy_reviewer_tamper_rolls_back_during_commit_roundtrip(tmp_path):
    service, intent = _service(tmp_path)
    staged = asyncio.run(service.run(intent, idempotency_key="run:reviewer-tamper"))
    forged_reviewer = staged.bundle.reviewer.model_copy(
        update={"error_code": "provider returned secret text"}
    )
    forged_bundle = staged.bundle.model_copy(update={"reviewer": forged_reviewer})
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()

    with pytest.raises(AssuranceWebError, match="stored run contract|invalid"):
        repository.commit_run(
            forged_bundle,
            idempotency_key="run:reviewer-tamper",
            request_digest=staged.request_digest,
        )
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 0


@pytest.mark.parametrize("source_update", ("author", "repository_path"))
def test_model_copy_source_tamper_rolls_back_during_commit_roundtrip(
    tmp_path, source_update
):
    service, intent = _service(tmp_path)
    staged = asyncio.run(service.run(intent, idempotency_key="run:source-tamper"))
    replacement = (
        "forged-author" if source_update == "author" else tmp_path
    )
    forged_source = staged.bundle.freshness_source_binding.model_copy(
        update={source_update: replacement}
    )
    forged_bundle = staged.bundle.model_copy(
        update={"freshness_source_binding": forged_source}
    )
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()

    with pytest.raises(AssuranceWebError, match="stored run contract|source|invalid"):
        repository.commit_run(
            forged_bundle,
            idempotency_key="run:source-tamper",
            request_digest=staged.request_digest,
        )
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 0


def test_model_copy_measured_receipt_tamper_rolls_back_during_commit_roundtrip(
    tmp_path,
):
    class _MeasuredReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            response = await super().invoke(prompt, run_id=run_id, route=route)
            return response.model_copy(
                update={
                    "usage_status": "measured",
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cost_usd": 0.25,
                }
            )

    service, intent = _service(tmp_path, reviewer=_MeasuredReviewer())
    staged = asyncio.run(service.run(intent, idempotency_key="run:receipt-tamper"))
    assert staged.bundle.reviewer.usage_status == "measured"
    assert staged.bundle.execution_receipt.input_tokens == 11
    forged_reviewer = staged.bundle.reviewer.model_copy(
        update={
            "usage_status": "measured",
            "input_tokens": 11,
            "output_tokens": 7,
            "cost_usd": 0.25,
        }
    )
    forged_receipt = staged.bundle.execution_receipt.model_copy(
        update={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    )
    forged_bundle = staged.bundle.model_copy(
        update={"reviewer": forged_reviewer, "execution_receipt": forged_receipt}
    )
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()

    with pytest.raises(AssuranceWebError, match="stored run contract|receipt|usage|invalid"):
        repository.commit_run(
            forged_bundle,
            idempotency_key="run:receipt-tamper",
            request_digest=staged.request_digest,
        )
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 0


def test_durable_winner_replay_is_not_blocked_by_candidate_source_move(tmp_path):
    service, intent = _service(tmp_path)
    staged = asyncio.run(service.run(intent, idempotency_key="run:source-moved"))
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    first = repository.commit_run(
        staged.bundle,
        idempotency_key="run:source-moved",
        request_digest=staged.request_digest,
    )
    assert first.cached is False
    shutil.rmtree(intent.repository_path)

    replay = repository.commit_run(
        staged.bundle,
        idempotency_key="run:source-moved",
        request_digest=staged.request_digest,
    )
    assert replay.cached is True


def test_questions_replay_as_additive_projection_and_keep_missing_refs(tmp_path):
    service, intent = _service(tmp_path, reviewer=_Reviewer(questions=True))
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    service._committer = repository

    result = asyncio.run(service.run(intent, idempotency_key="run:questions"))
    assert result.bundle.case.state == "NEEDS_EVIDENCE"
    assert len(result.bundle.questions) == 1
    question = result.bundle.questions[0]
    assert result.bundle.case.missing_evidence == (
        "review_question:" + question.question_id,
    )

    projection = repository.get_change(result.bundle.case.case_id)
    assert projection["questions"] == [question.model_dump(mode="json")]
    assert projection["case"]["missing_evidence"] == [
        "review_question:" + question.question_id
    ]
    replay = repository.lookup_run("run:questions", result.request_digest)
    assert replay is not None
    assert replay.cached is True
    assert replay.bundle.questions == (question,)
