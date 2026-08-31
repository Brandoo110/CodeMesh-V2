"""Focused GP-03 persistence tests for complete assurance runs."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
import zipfile

import pytest
import assurance.run_service as run_service_module

from assurance.remediation import (
    PreparedRemediationHandoff,
    RemediationController,
)
from assurance.remediation_validation import ValidationStatus
from assurance.remediation_reviewer import AssuranceRemediationReviewer
from assurance.live_freshness import FreshnessStatus, LiveFreshness
from tests.test_assurance_run_service import _Reviewer, _service
from tests.test_assurance_run_service import _FakeOfficialImporter
from assurance.run_service import AssuranceRunResult
from assurance.official_evidence import parse_official_evidence_receipt
from tests.test_assurance_remediation import _FakeExecutor
from tests.test_assurance_remediation_reviewer import (
    _FindingReviewer,
    _baseline_and_changed_subject,
    _request_for_baseline,
    _reconfigure_service,
)
from web.assurance_store import (
    AssuranceWebConflictError,
    AssuranceWebError,
    AssuranceWebRepository,
)
from web.assurance_run_committer import (
    AssuranceRunPersistenceError,
    _json_digest,
    _source_binding_json,
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


def _bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _raw_external_official_fixture(bundle, proofs):
    """Make real-shaped non-canonical report/result members for validator tests."""

    raw_members = {}
    receipts = {}
    raw_payloads = {}
    receipt_digests = {}
    with zipfile.ZipFile(io.BytesIO(proofs[0].artifact_bytes)) as archive:
        members = {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
        }
    for proof in proofs:
        receipt = parse_official_evidence_receipt(proof.receipt_bytes)
        report_name = (
            "dependency_audit.json"
            if proof.kind == "dependency_audit"
            else "ci_iac_validation.json"
        )
        result_name = (
            "dependency-audit-result.json"
            if proof.kind == "dependency_audit"
            else "ci-iac-result.json"
        )
        raw_result = b"\n" + members[result_name]
        report = receipt.report.model_copy(
            update={
                "result_digest": _bytes_digest(raw_result),
                "result_byte_size": len(raw_result),
            }
        )
        raw_report = b"\n" + json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = receipt.model_copy(
            update={
                "report": report,
                "report_digest": _bytes_digest(raw_report),
                "report_byte_size": len(raw_report),
                "result_digest": _bytes_digest(raw_result),
                "result_byte_size": len(raw_result),
            }
        )
        receipts[proof.kind] = receipt
        raw_payloads[proof.kind] = (raw_report, raw_result)
        raw_members[report_name] = raw_report
        raw_members[result_name] = raw_result

    members.update(raw_members)
    artifact_buffer = io.BytesIO()
    with zipfile.ZipFile(
        artifact_buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as artifact:
        for name, data in members.items():
            artifact.writestr(name, data)
    artifact_bytes = artifact_buffer.getvalue()
    artifact_digest = _bytes_digest(artifact_bytes)
    raw_proofs = []
    for proof in proofs:
        raw_report, raw_result = raw_payloads[proof.kind]
        receipt = receipts[proof.kind].model_copy(
            update={
                "artifact_digest": artifact_digest,
                "artifact_byte_size": len(artifact_bytes),
            }
        )
        receipt_bytes = json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        raw_proofs.append(
            replace(
                proof,
                artifact_digest=artifact_digest,
                artifact_byte_size=len(artifact_bytes),
                artifact_bytes=artifact_bytes,
                receipt_digest=_bytes_digest(receipt_bytes),
                receipt_byte_size=len(receipt_bytes),
                receipt_bytes=receipt_bytes,
                report_digest=_bytes_digest(raw_report),
                report_byte_size=len(raw_report),
                report_bytes=raw_report,
                result_digest=_bytes_digest(raw_result),
                result_byte_size=len(raw_result),
                result_bytes=raw_result,
            )
        )
        receipt_digests[proof.evidence_id] = _bytes_digest(receipt_bytes)

    evidence = tuple(
        item.model_copy(
            update={"artifact_digest": receipt_digests[item.evidence_id]}
        )
        if item.evidence_id in receipt_digests
        else item
        for item in bundle.evidence
    )
    entries = tuple(
        entry.model_copy(
            update={"artifact_digest": receipt_digests[entry.evidence_id]}
        )
        if entry.evidence_id in receipt_digests
        else entry
        for entry in bundle.manifest.manifest.entries
    )
    manifest = bundle.manifest.model_copy(
        update={
            "manifest": bundle.manifest.manifest.model_copy(
                update={"entries": entries}
            )
        }
    )
    return bundle.model_copy(update={"evidence": evidence, "manifest": manifest}), tuple(
        raw_proofs
    )


def test_official_proof_validator_accepts_raw_external_members(tmp_path, monkeypatch):
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    prepared = asyncio.run(
        service._prepare_bundle(
            intent,
            idempotency_key="run:raw-external-members",
            request_digest=service._request_digest(intent),
            with_proofs=True,
        )
    )
    bundle, proofs = _raw_external_official_fixture(*prepared)

    assert all(
        proof.report_bytes.startswith(b"\n")
        and proof.result_bytes.startswith(b"\n")
        for proof in proofs
    )
    rows = repository._validate_official_commit_proofs(bundle, proofs)

    assert len(rows) == 2


class _FreshnessFixture:
    def __init__(self, status: FreshnessStatus):
        self.status = status

    def check(self, *_):
        return LiveFreshness(
            status=self.status,
            reason_code=self.status.value.upper(),
            checked_at=datetime.now(timezone.utc),
        )


class _ExplodingFreshness:
    def check(self, *_):
        raise RuntimeError("freshness exploded")


def _fresh_repository(tmp_path):
    return AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=_FreshnessFixture(FreshnessStatus.FRESH),
        live_required=False,
    )


def test_commit_lookup_projects_run_and_keeps_local_source_private(tmp_path):
    service, intent, repository = _durable_service(tmp_path)

    result = asyncio.run(service.run(intent, idempotency_key="run:happy"))
    assert result.cached is False
    expected_source_path = intent.repository_path.resolve()
    assert (
        result.bundle.freshness_source_binding.repository_path == expected_source_path
    )

    public_bundle = result.bundle.model_dump(mode="json")
    assert "freshness_source_binding" not in public_bundle
    public_json = json.dumps(public_bundle, ensure_ascii=False, sort_keys=True)
    assert str(expected_source_path) not in public_json

    run_row = _db_rows(
        repository,
        "SELECT bundle_json, source_binding_json FROM assurance_web_runs",
    )[0]
    stored_public = json.loads(run_row["bundle_json"])
    stored_source = json.loads(run_row["source_binding_json"])
    assert "freshness_source_binding" not in stored_public
    assert str(expected_source_path) not in run_row["bundle_json"]
    assert stored_source["repository_path"] == str(expected_source_path)
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


def test_same_subject_new_key_continues_blocked_case_with_authoritative_final(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )

    preflight = asyncio.run(service.run(intent, idempotency_key="run:preflight"))
    assert preflight.bundle.policy.decision.outcome == "BLOCKED"
    assert service._reviewer_invoker.calls == 0

    final_intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    final = asyncio.run(service.run(final_intent, idempotency_key="run:final"))

    assert final.cached is False
    assert final.bundle.case.case_id == preflight.bundle.case.case_id
    assert final.bundle.run_id != preflight.bundle.run_id
    assert final.bundle.policy.decision.outcome != "BLOCKED"
    assert service._reviewer_invoker.calls == 1
    assert _FakeOfficialImporter.calls == 1
    assert _FakeOfficialImporter.verified == 2

    projection = repository.get_change(final.bundle.case.case_id)
    assert projection["case"]["case_id"] == preflight.bundle.case.case_id
    assert projection["case"]["state"] == final.bundle.case.state
    assert projection["reviewer_runs"] == [
        {
            "run_id": preflight.bundle.run_id,
            "status": preflight.bundle.reviewer.status,
            "planned_route": preflight.bundle.reviewer.planned_route.model_dump(
                mode="json"
            ),
            "rubric_version": preflight.bundle.reviewer.rubric_version,
            "prompt_id": preflight.bundle.reviewer.prompt_id,
            "prompt_digest": preflight.bundle.reviewer.prompt_digest,
            "actual_provider": preflight.bundle.reviewer.actual_provider,
            "actual_model_ref": preflight.bundle.reviewer.actual_model_ref,
            "schema_status": preflight.bundle.reviewer.schema_status,
            "raw_response_artifact_digest": preflight.bundle.reviewer.raw_response_artifact_digest,
            "canonical_response_digest": preflight.bundle.reviewer.canonical_response_digest,
            "result_id": preflight.bundle.reviewer.result_id,
            "result_digest": preflight.bundle.reviewer.result_digest,
            "usage_status": preflight.bundle.reviewer.usage_status,
            "input_tokens": preflight.bundle.reviewer.input_tokens,
            "output_tokens": preflight.bundle.reviewer.output_tokens,
            "cost_usd": preflight.bundle.reviewer.cost_usd,
            "error_code": preflight.bundle.reviewer.error_code,
        },
        {
            "run_id": final.bundle.run_id,
            "status": final.bundle.reviewer.status,
            "planned_route": final.bundle.reviewer.planned_route.model_dump(
                mode="json"
            ),
            "rubric_version": final.bundle.reviewer.rubric_version,
            "prompt_id": final.bundle.reviewer.prompt_id,
            "prompt_digest": final.bundle.reviewer.prompt_digest,
            "actual_provider": final.bundle.reviewer.actual_provider,
            "actual_model_ref": final.bundle.reviewer.actual_model_ref,
            "schema_status": final.bundle.reviewer.schema_status,
            "raw_response_artifact_digest": final.bundle.reviewer.raw_response_artifact_digest,
            "canonical_response_digest": final.bundle.reviewer.canonical_response_digest,
            "result_id": final.bundle.reviewer.result_id,
            "result_digest": final.bundle.reviewer.result_digest,
            "usage_status": final.bundle.reviewer.usage_status,
            "input_tokens": final.bundle.reviewer.input_tokens,
            "output_tokens": final.bundle.reviewer.output_tokens,
            "cost_usd": final.bundle.reviewer.cost_usd,
            "error_code": final.bundle.reviewer.error_code,
        },
    ]
    assert len(projection["receipts"]) == 2

    replay = asyncio.run(service.run(intent, idempotency_key="run:preflight"))
    assert replay.cached is True
    assert replay.bundle.policy.decision.outcome == "BLOCKED"
    assert service._reviewer_invoker.calls == 1

    with pytest.raises(AssuranceWebConflictError, match="request digest"):
        asyncio.run(service.run(final_intent, idempotency_key="run:preflight"))
    assert _FakeOfficialImporter.calls == 1
    assert service._reviewer_invoker.calls == 1


def test_continuation_rejects_official_provenance_mismatch_without_partial_state(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    preflight = asyncio.run(service.run(intent, idempotency_key="run:preflight"))
    staged = asyncio.run(
        service.prepare(
            intent.model_copy(update={"official_evidence_run_id": "123"}),
            idempotency_key="run:forged",
        )
    )
    forged_evidence = tuple(
        item.model_copy(
            update={
                "source_ref": item.source_ref.replace(":run:123:", ":run:999:")
            }
        )
        if item.kind == "dependency_audit"
        else item
        for item in staged.evidence
    )
    forged = staged.model_copy(update={"evidence": forged_evidence})

    with pytest.raises(AssuranceWebError, match="provenance"):
        repository.commit_run(
            forged,
            idempotency_key=staged.idempotency_key,
            request_digest=staged.request_digest,
        )

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
        "assurance_cases": 1,
        "assurance_case_events": len(preflight.bundle.events),
        "assurance_decisions": 1,
        "assurance_web_cases": 1,
        "assurance_web_runs": 1,
        "assurance_web_idempotency": 1,
    }


def test_concurrent_same_subject_new_keys_have_one_progression_winner(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    preflight = asyncio.run(service.run(intent, idempotency_key="run:preflight"))
    final_intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    request_digest = service._request_digest(final_intent)
    staged = [
        asyncio.run(
            service._prepare_bundle(
                final_intent,
                idempotency_key=key,
                request_digest=request_digest,
                with_proofs=True,
            )
        )
        for key in ("run:final-a", "run:final-b")
    ]
    barrier = Barrier(2)

    def commit_once(prepared):
        bundle, official_proofs = prepared
        barrier.wait()
        try:
            return repository.commit_run(
                bundle,
                idempotency_key=bundle.idempotency_key,
                request_digest=bundle.request_digest,
                official_proofs=official_proofs,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit_once, (item for item in staged)))

    assert sum(isinstance(item, AssuranceWebError) for item in results) == 1
    assert sum(
        isinstance(item, AssuranceRunResult) and item.cached is False
        for item in results
    ) == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 2
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_case_events"
    )[0]["count"] == len(preflight.bundle.events) + 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_decisions"
    )[0]["count"] == 2


def test_continuation_projection_failure_rolls_back_without_automatic_provider_retry(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    preflight = asyncio.run(service.run(intent, idempotency_key="run:preflight"))
    final_intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    original_projection = repository._projection_in_transaction

    def fail_projection(*args, **kwargs):
        if kwargs.get("require_run_pointers") is False:
            raise RuntimeError("injected continuation projection failure")
        return original_projection(*args, **kwargs)

    repository._projection_in_transaction = fail_projection
    with pytest.raises(RuntimeError, match="continuation projection"):
        asyncio.run(service.run(final_intent, idempotency_key="run:final"))
    assert service._reviewer_invoker.calls == 1
    assert _FakeOfficialImporter.calls == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_case_events"
    )[0]["count"] == len(preflight.bundle.events)
    repository._projection_in_transaction = original_projection

    retry = asyncio.run(service.run(final_intent, idempotency_key="run:final"))
    assert retry.cached is False
    assert service._reviewer_invoker.calls == 2
    assert _FakeOfficialImporter.calls == 2


def test_continuation_locks_complete_freshness_source_binding_before_write(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )

    class _SourceTamperingCommitter:
        def lookup(self, key, digest):
            return repository.lookup_run(key, digest)

        def commit(self, bundle, *, idempotency_key, request_digest, official_proofs=()):
            if official_proofs:
                source = bundle.freshness_source_binding.model_copy(
                    update={"author": "forged-author"}
                )
                bundle = bundle.model_copy(
                    update={
                        "freshness_source_binding": source,
                        "freshness_source_binding_digest": _json_digest(
                            _source_binding_json(source)
                        ),
                    }
                )
            return repository.commit_run(
                bundle,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                official_proofs=official_proofs,
            )

    service._committer = _SourceTamperingCommitter()
    preflight = asyncio.run(service.run(intent, idempotency_key="run:source-base"))
    final_intent = intent.model_copy(update={"official_evidence_run_id": "123"})

    with pytest.raises(AssuranceWebError, match="freshness|source|provenance"):
        asyncio.run(service.run(final_intent, idempotency_key="run:source-drift"))

    assert service._reviewer_invoker.calls == 1
    assert _FakeOfficialImporter.calls == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 1
    assert repository.get_change(preflight.bundle.case.case_id)["metadata"]["author"] == (
        "author-agent"
    )


def test_continuation_requires_run_policy_to_be_referenced_by_its_events(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )

    class _PolicyForkingCommitter:
        def lookup(self, key, digest):
            return repository.lookup_run(key, digest)

        def commit(self, bundle, *, idempotency_key, request_digest, official_proofs=()):
            if official_proofs:
                forged_policy_id = "policy-forged"
                events = tuple(
                    event.model_copy(
                        update={
                            "policy_decision_refs": (
                                *event.policy_decision_refs,
                                forged_policy_id,
                            )
                        }
                    )
                    if event.kind == "COLLECT_EVIDENCE"
                    else event
                    for event in bundle.events
                )
                case = bundle.case.model_copy(
                    update={
                        "policy_decision_refs": (
                            *bundle.case.policy_decision_refs,
                            forged_policy_id,
                        )
                    }
                )
                bundle = bundle.model_copy(update={"case": case, "events": events})
            return repository.commit_run(
                bundle,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                official_proofs=official_proofs,
            )

    service._committer = _PolicyForkingCommitter()
    preflight = asyncio.run(service.run(intent, idempotency_key="run:policy-base"))
    final_intent = intent.model_copy(update={"official_evidence_run_id": "123"})

    with pytest.raises(AssuranceWebError, match="policy|event"):
        asyncio.run(service.run(final_intent, idempotency_key="run:policy-fork"))

    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_web_runs"
    )[0]["count"] == 1
    assert _db_rows(
        repository, "SELECT COUNT(*) AS count FROM assurance_case_events"
    )[0]["count"] == len(preflight.bundle.events)


@pytest.mark.parametrize("tamper", ("receipt", "report", "result", "source", "missing"))
def test_historical_official_proof_readback_fails_closed_on_byte_or_row_tamper(
    tmp_path, monkeypatch, tamper
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    result = asyncio.run(service.run(intent, idempotency_key="run:proof-history"))

    conn = sqlite3.connect(repository._db_path)
    try:
        row = conn.execute(
            "SELECT run_id, evidence_id, receipt_bytes, report_bytes, result_bytes, source_bindings_json"
            " FROM assurance_official_evidence_proofs ORDER BY evidence_id LIMIT 1"
        ).fetchone()
        assert row is not None
        if tamper == "missing":
            conn.execute(
                "DELETE FROM assurance_official_evidence_proofs WHERE run_id = ?",
                (row[0],),
            )
        elif tamper == "receipt":
            conn.execute(
                "UPDATE assurance_official_evidence_proofs SET receipt_bytes = ?"
                " WHERE run_id = ? AND evidence_id = ?",
                (row[2] + b"x", row[0], row[1]),
            )
        elif tamper == "report":
            conn.execute(
                "UPDATE assurance_official_evidence_proofs SET report_bytes = ?"
                " WHERE run_id = ? AND evidence_id = ?",
                (row[3] + b"x", row[0], row[1]),
            )
        elif tamper == "result":
            conn.execute(
                "UPDATE assurance_official_evidence_proofs SET result_bytes = ?"
                " WHERE run_id = ? AND evidence_id = ?",
                (row[4] + b"x", row[0], row[1]),
            )
        else:
            sources = json.loads(row[5])
            sources[0]["bytes"] = base64.b64encode(b"forged-source").decode("ascii")
            conn.execute(
                "UPDATE assurance_official_evidence_proofs SET source_bindings_json = ?"
                " WHERE run_id = ? AND evidence_id = ?",
                (json.dumps(sources, sort_keys=True, separators=(",", ":")), row[0], row[1]),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="proof|official|receipt|source|history"):
        repository.lookup_run("run:proof-history", result.request_digest)


def test_official_proof_rows_are_immutable_unique_and_foreign_key_bound(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent, repository = _durable_service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    result = asyncio.run(service.run(intent, idempotency_key="run:proof-constraints"))

    rows = _db_rows(
        repository,
        "SELECT run_id, evidence_id, kind FROM assurance_official_evidence_proofs",
    )
    assert len(rows) == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn = sqlite3.connect(repository._db_path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO assurance_official_evidence_proofs"
                " (run_id, evidence_id, kind, subject_digest, evidence_mode,"
                " workflow_run_id, workflow_run_attempt, job_id, artifact_id,"
                " artifact_digest, artifact_byte_size, receipt_digest, receipt_byte_size,"
                " receipt_bytes, report_digest, report_byte_size, report_bytes,"
                " result_digest, result_byte_size, result_bytes, source_bindings_json)"
                " SELECT run_id, evidence_id, kind, subject_digest, evidence_mode,"
                " workflow_run_id, workflow_run_attempt, job_id, artifact_id,"
                " artifact_digest, artifact_byte_size, receipt_digest, receipt_byte_size,"
                " receipt_bytes, report_digest, report_byte_size, report_bytes,"
                " result_digest, result_byte_size, result_bytes, source_bindings_json"
                " FROM assurance_official_evidence_proofs WHERE run_id = ? LIMIT 1",
                (result.bundle.run_id,),
            )
        finally:
            conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        conn = sqlite3.connect(repository._db_path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO assurance_official_evidence_proofs"
                " (run_id, evidence_id, kind, subject_digest, evidence_mode,"
                " workflow_run_id, workflow_run_attempt, job_id, artifact_id,"
                " artifact_digest, artifact_byte_size, receipt_digest, receipt_byte_size,"
                " receipt_bytes, report_digest, report_byte_size, report_bytes,"
                " result_digest, result_byte_size, result_bytes, source_bindings_json)"
                " SELECT 'missing-run', evidence_id, kind, subject_digest, evidence_mode,"
                " workflow_run_id, workflow_run_attempt, job_id, artifact_id,"
                " artifact_digest, artifact_byte_size, receipt_digest, receipt_byte_size,"
                " receipt_bytes, report_digest, report_byte_size, report_bytes,"
                " result_digest, result_byte_size, result_bytes, source_bindings_json"
                " FROM assurance_official_evidence_proofs WHERE run_id = ? LIMIT 1",
                (result.bundle.run_id,),
            )
        finally:
            conn.close()


def test_existing_v1_run_schema_additively_migrates_official_proof_table(tmp_path):
    database = tmp_path / "assurance.sqlite"
    repository = AssuranceWebRepository(database)
    repository._store.initialize()
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE assurance_run_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO assurance_run_schema_migrations(version, applied_at) VALUES (1, datetime('now'))"
        )
        conn.execute(
            "CREATE TABLE assurance_web_runs ("
            "idempotency_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,"
            "run_id TEXT NOT NULL UNIQUE, case_id TEXT NOT NULL, subject_digest TEXT NOT NULL,"
            "bundle_json TEXT NOT NULL, source_binding_json TEXT NOT NULL, committed_at TEXT NOT NULL,"
            "FOREIGN KEY(case_id) REFERENCES assurance_cases(case_id))"
        )
        conn.execute(
            "CREATE INDEX assurance_web_runs_case ON assurance_web_runs(case_id, committed_at, run_id)"
        )
        conn.commit()
    finally:
        conn.close()

    repository.initialize()
    assert [row[0] for row in _db_rows(
        repository, "SELECT version FROM assurance_run_schema_migrations ORDER BY version"
    )] == [1, 2]
    assert _db_rows(
        repository,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assurance_official_evidence_proofs'",
    )


def _prepared_success(tmp_path, monkeypatch):
    (
        service,
        intent,
        baseline,
        workspace,
        changed_bundle,
        subject_input,
        selected_finding,
    ) = _baseline_and_changed_subject(tmp_path, monkeypatch)
    request, _ = _request_for_baseline(baseline)
    executor = _FakeExecutor([ValidationStatus.FAILED, ValidationStatus.PASSED])

    def service_factory(root):
        _reconfigure_service(service, root)
        return service

    adapter = AssuranceRemediationReviewer(
        baseline_bundle=baseline,
        service_factory=service_factory,
    )

    async def reviewer(**kwargs):
        return await adapter.rerun(
            reviewer_role=kwargs["reviewer_role"],
            subject_input=kwargs["subject_input"],
            subject_digest=kwargs["subject_digest"],
            workspace=kwargs["workspace"],
            request=kwargs["request"],
            selected_finding=kwargs["selected_finding"],
        )

    async def agent(*, workspace, **_):
        workspace.write_text("changed.txt", "repaired\n")

    controller = RemediationController(
        request=request,
        selected_finding=selected_finding,
        seed_root=intent.repository_path,
        validation_executor=lambda _workspace: executor,
        subject_builder=lambda _patch_digest: subject_input,
        reviewer_rerunner=reviewer,
    )
    handoff = asyncio.run(controller.prepare(agent))
    assert type(handoff) is PreparedRemediationHandoff
    return baseline, changed_bundle, request, handoff


def test_commit_prepared_remediation_projects_authoritative_transition(
    tmp_path, monkeypatch
):
    baseline, changed_bundle, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )

    receipt = repository.commit_prepared_remediation(
        request,
        handoff,
        idempotency_key="remediate:happy",
    )

    assert receipt.old_case_id == baseline.case.case_id
    assert receipt.new_case_id == changed_bundle.draft_case.case_id


def test_commit_prepared_remediation_is_atomic_and_replays_exactly(
    tmp_path, monkeypatch
):
    baseline, changed_bundle, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )

    receipt = repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:atomic"
    )
    old_state = repository._store.load_case(request.old_case_id)
    new_state = repository._store.load_case(receipt.new_case_id)
    assert old_state.case.state == "INVALIDATED"
    assert old_state.applied_events[-1].event_id == receipt.invalidation_event_id
    assert new_state.case.state == "DRAFT"
    assert new_state.applied_events == ()
    assert repository._store.get_binding(receipt.new_case_id) == handoff.bundle.binding
    assert repository._store.get_remediation(request.remediation_id) == receipt

    projection = repository.get_change(receipt.new_case_id)
    assert projection["case"]["state"] == "DRAFT"
    assert projection["decisions"] == []
    assert projection["evidence"] == [
        item.model_dump(mode="json") for item in handoff.bundle.evidence
    ]
    assert projection["receipt"] == handoff.bundle.execution_receipt.model_dump(
        mode="json"
    )

    rows = {
        name: _db_rows(
            repository, f"SELECT * FROM {name}"
        )
        for name in (
            "assurance_cases",
            "assurance_case_events",
            "assurance_web_cases",
            "assurance_web_runs",
            "assurance_remediations",
            "assurance_web_idempotency",
        )
    }
    assert len(rows["assurance_cases"]) == 2
    assert len(rows["assurance_case_events"]) == len(baseline.events) + 1
    assert len(rows["assurance_web_cases"]) == 2
    assert len(rows["assurance_web_runs"]) == 2
    assert len(rows["assurance_remediations"]) == 1
    remediation_pointer = next(
        row
        for row in rows["assurance_web_idempotency"]
        if row["idempotency_key"] == "remediate:atomic"
    )
    assert remediation_pointer["operation"] == "remediate"
    assert json.loads(remediation_pointer["result_json"])["new_case_id"] == receipt.new_case_id
    stored_bundle = json.loads(
        next(
            row["bundle_json"]
            for row in rows["assurance_web_runs"]
            if row["idempotency_key"] == handoff.bundle.idempotency_key
        )
    )
    assert stored_bundle["draft_case"]["state"] == "DRAFT"
    assert stored_bundle["case"]["state"] in {"EVIDENCE_COLLECTED", "NEEDS_EVIDENCE"}

    before = {
        name: len(value)
        for name, value in rows.items()
        if name not in {"assurance_web_idempotency"}
    }
    replay = repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:atomic"
    )
    assert replay == receipt
    after = {
        name: len(_db_rows(repository, f"SELECT * FROM {name}"))
        for name in before
    }
    assert after == before


def test_commit_prepared_remediation_signature_rejects_caller_facts(
    tmp_path, monkeypatch
):
    _, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        repository.commit_prepared_remediation(
            request,
            handoff,
            idempotency_key="remediate:caller-facts",
            finding=object(),
        )


def test_commit_prepared_remediation_rejects_nested_case_id_tampering(
    tmp_path, monkeypatch
):
    baseline, changed_bundle, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )

    forged_draft_case = handoff.bundle.draft_case.model_copy(
        update={"case_id": "evil-case-id"}
    )
    forged_run_case = handoff.bundle.case.model_copy(
        update={"case_id": "evil-case-id"}
    )
    forged_bundle = handoff.bundle.model_copy(
        update={"draft_case": forged_draft_case, "case": forged_run_case}
    )
    forged_handoff = PreparedRemediationHandoff(
        result=handoff.result,
        bundle=forged_bundle,
    )

    with pytest.raises(AssuranceWebConflictError, match="case_id|derived|bundle"):
        repository.commit_prepared_remediation(
            request,
            forged_handoff,
            idempotency_key="remediate:evil-case",
        )
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (("reason_code", "forged"), ("human_selected_finding_id", "forged-finding")),
)
def test_remediation_result_success_facts_are_server_derived(
    tmp_path, monkeypatch, field, value
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    forged_result = handoff.result.model_copy(update={field: value})
    forged_handoff = PreparedRemediationHandoff(
        result=forged_result,
        bundle=handoff.bundle,
    )

    with pytest.raises(AssuranceWebConflictError, match="result|Finding|derived"):
        repository.commit_prepared_remediation(
            request,
            forged_handoff,
            idempotency_key=f"remediate:result:{field}",
        )
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0


def test_remediation_replay_rejects_receipt_tampering_even_when_jsons_agree(
    tmp_path, monkeypatch
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:receipt-tamper"
    )

    forged_case_id = "evil-replay-case"
    conn = sqlite3.connect(repository._db_path)
    try:
        lineage = conn.execute(
            "SELECT receipt_json FROM assurance_remediations WHERE remediation_id = ?",
            (request.remediation_id,),
        ).fetchone()
        pointer = conn.execute(
            "SELECT result_json FROM assurance_web_idempotency WHERE idempotency_key = ?",
            ("remediate:receipt-tamper",),
        ).fetchone()
        lineage_result = json.loads(lineage[0])
        pointer_result = json.loads(pointer[0])
        lineage_result["new_case_id"] = forged_case_id
        pointer_result["new_case_id"] = forged_case_id
        encoded_lineage = json.dumps(
            lineage_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded_pointer = json.dumps(
            pointer_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "UPDATE assurance_remediations SET new_case_id = ?, receipt_json = ?"
            " WHERE remediation_id = ?",
            (forged_case_id, encoded_lineage, request.remediation_id),
        )
        conn.execute(
            "UPDATE assurance_web_idempotency SET result_json = ? WHERE idempotency_key = ?",
            (encoded_pointer, "remediate:receipt-tamper"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="receipt|replay|lineage|case"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key="remediate:receipt-tamper"
        )


@pytest.mark.parametrize(
    "checker",
    (
        None,
        _FreshnessFixture(FreshnessStatus.STALE),
        _ExplodingFreshness(),
    ),
)
def test_remediation_freshness_failure_is_zero_write(
    tmp_path, monkeypatch, checker
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = AssuranceWebRepository(
        tmp_path / "assurance.sqlite",
        freshness_checker=checker,
        live_required=False,
    )
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    before = {
        name: _db_rows(repository, f"SELECT COUNT(*) AS count FROM {name}")[0]["count"]
        for name in (
            "assurance_cases",
            "assurance_case_events",
            "assurance_web_cases",
            "assurance_web_runs",
            "assurance_remediations",
            "assurance_web_idempotency",
        )
    }
    with pytest.raises(AssuranceWebConflictError, match="freshness"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key="remediate:freshness"
        )
    after = {
        name: _db_rows(repository, f"SELECT COUNT(*) AS count FROM {name}")[0]["count"]
        for name in before
    }
    assert after == before


def test_remediation_idempotency_conflicts_on_payload_or_key_reuse(
    tmp_path, monkeypatch
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    repository.commit_prepared_remediation(
        request, handoff, idempotency_key="remediate:conflict"
    )

    with pytest.raises(AssuranceWebConflictError):
        repository.commit_prepared_remediation(
            request.model_copy(update={"requested_by": "another-owner"}),
            handoff,
            idempotency_key="remediate:conflict",
        )
    forged_handoff = PreparedRemediationHandoff(
        result=handoff.result.model_copy(update={"reason_code": "forged"}),
        bundle=handoff.bundle,
    )
    with pytest.raises(AssuranceWebConflictError):
        repository.commit_prepared_remediation(
            request,
            forged_handoff,
            idempotency_key="remediate:conflict",
        )
    with pytest.raises(AssuranceWebConflictError, match="already committed"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key="remediate:another-key"
        )


def test_remediation_fails_closed_when_baseline_pointer_is_corrupt(
    tmp_path, monkeypatch
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    conn = sqlite3.connect(repository._db_path)
    try:
        conn.execute(
            "DELETE FROM assurance_web_idempotency WHERE idempotency_key = ?",
            (baseline.idempotency_key,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AssuranceWebError, match="pointer|idempotency"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key="remediate:bad-baseline"
        )
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0


@pytest.mark.parametrize(
    "tamper",
    ("missing", "closed", "subject_drift", "duplicate"),
)
def test_authoritative_finding_variants_fail_closed_without_writes(
    tmp_path, monkeypatch, tamper
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )
    if tamper == "missing":
        request = request.model_copy(update={"human_selected_finding_id": "missing"})
    else:
        conn = sqlite3.connect(repository._db_path)
        try:
            row = conn.execute(
                "SELECT bundle_json FROM assurance_web_runs WHERE idempotency_key = ?",
                (baseline.idempotency_key,),
            ).fetchone()
            bundle_data = json.loads(row[0])
            findings = bundle_data["findings"]
            if tamper == "closed":
                findings[0]["status"] = "closed"
                bundle_data["policy"]["input"]["findings"][0]["status"] = "closed"
            elif tamper == "subject_drift":
                findings[0]["subject_digest"] = "sha256:" + "f" * 64
                bundle_data["policy"]["input"]["findings"][0]["subject_digest"] = (
                    "sha256:" + "f" * 64
                )
            else:
                findings.append(dict(findings[0]))
                bundle_data["policy"]["input"]["findings"].append(
                    dict(bundle_data["policy"]["input"]["findings"][0])
                )
            conn.execute(
                "UPDATE assurance_web_runs SET bundle_json = ? WHERE idempotency_key = ?",
                (
                    json.dumps(
                        bundle_data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    baseline.idempotency_key,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(AssuranceWebError, match="Finding|stored|contract"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key=f"remediate:finding:{tamper}"
        )
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0


@pytest.mark.parametrize(
    "failure_point",
    ("lifecycle", "projection", "run", "idempotency"),
)
def test_remediation_failure_injection_rolls_back_every_written_layer(
    tmp_path, monkeypatch, failure_point
):
    baseline, _, request, handoff = _prepared_success(tmp_path, monkeypatch)
    repository = _fresh_repository(tmp_path)
    repository.initialize()
    repository.commit_run(
        baseline,
        idempotency_key=baseline.idempotency_key,
        request_digest=baseline.request_digest,
    )

    if failure_point == "lifecycle":
        original = repository._store._commit_prepared_remediation_in_transaction

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected lifecycle failure")

        monkeypatch.setattr(
            repository._store,
            "_commit_prepared_remediation_in_transaction",
            fail,
        )
    elif failure_point == "projection":
        original = repository._touch_web_case

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(repository, "_touch_web_case", fail)
    elif failure_point == "run":
        original = repository._run_committer._commit_run_in_transaction

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected run failure")

        monkeypatch.setattr(
            repository._run_committer, "_commit_run_in_transaction", fail
        )
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("injected idempotency failure")

        monkeypatch.setattr(repository, "_record_mutation", fail)

    with pytest.raises(RuntimeError, match="injected"):
        repository.commit_prepared_remediation(
            request, handoff, idempotency_key=f"remediate:failure:{failure_point}"
        )

    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_remediations",
    )[0]["count"] == 0
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_case_events "
        f"WHERE case_id = '{request.old_case_id}'",
    )[0]["count"] == len(baseline.events)
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_web_runs",
    )[0]["count"] == 1
    assert _db_rows(
        repository,
        "SELECT COUNT(*) AS count FROM assurance_web_idempotency"
        " WHERE idempotency_key LIKE 'remediate:failure:%'",
    )[0]["count"] == 0
