"""V2-P3-03 Rules-Only baseline focused tests."""

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    RulesOnlyBaselineResult,
    RulesOnlyBaselineRunner,
    RulesOnlyExpectation,
    RulesOnlyFixture,
)
from assurance import rules_baseline as rules_module
from assurance.contracts import ChangeSubject
from assurance.intake import IntakeSnapshot
from assurance.manifest import EvidenceManifest, EvidenceManifestEntry
from assurance.policy import PolicyEvaluationInput, PolicyGate, PolicyGateResult
from assurance.risk import (
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
    RiskDeclarations,
)
from assurance.snapshot import GitChange, GitSnapshot


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
EARLIER_TIME = datetime(2026, 8, 25, 7, 0, 0, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def _change(path, **overrides):
    values = {
        "schema_version": "v1",
        "path": path,
        "old_path": None,
        "status": "added",
        "current_size": 1,
        "current_digest": _digest("b"),
        "binary": False,
        "large_file": False,
        "submodule": False,
    }
    values.update(overrides)
    return GitChange.model_validate(values)


def _git_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "repository": "acme/service",
        "base_revision": "a" * 40,
        "head_revision": "b" * 40,
        "scope": "base_to_worktree",
        "worktree_dirty": False,
        "changes": (_change("a.txt"),),
        "changed_files_total": 1,
        "diff_artifact_digest": _digest("d"),
        "diff_bytes": 0,
        "diff_truncated": False,
        "files_truncated": False,
        "ignored_files_lower_bound": 0,
        "ignored_scan_truncated": False,
        "large_file_paths": (),
        "submodule_paths": (),
        "omissions": (),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    if "changes" in overrides and "changed_files_total" not in overrides:
        values["changed_files_total"] = len(values["changes"])
    return GitSnapshot.model_validate(values)


def _intake_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    documents = overrides.get("documents", ())
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "documents": documents,
        "notices": (),
        "task_digest": None,
        "task_present": False,
        "policy_count": sum(
            document.kind == "policy" for document in documents
        ),
        "adr_count": sum(
            document.kind == "adr" for document in documents
        ),
        "runbook_count": sum(
            document.kind == "runbook" for document in documents
        ),
        "manifest_artifact_digest": _digest("a"),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return IntakeSnapshot.model_validate(values)


def _declarations(**overrides):
    values = {
        "changed_lines_total": 0,
        "external_side_effects": "none_declared",
        "provider_boundary": "within_declared_boundary",
    }
    values.update(overrides)
    return RiskDeclarations.model_validate(values)


def _entry(
    subject_digest,
    evidence_id,
    kind,
    producer,
    *,
    status="success",
    collected_at=FIXED_TIME,
    fresh_until=None,
    trust_level="observed",
    redaction_status="not_applicable",
    source_ref=None,
):
    if source_ref is None:
        source_ref = f"{producer}:{_digest('a')}"
    return EvidenceManifestEntry.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": evidence_id,
            "kind": kind,
            "trust_level": trust_level,
            "producer": producer,
            "subject_digest": subject_digest,
            "artifact_digest": _digest("a"),
            "source_ref": source_ref,
            "status": status,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "freshness": "unknown" if fresh_until is None else "fresh",
            "redaction_status": redaction_status,
        }
    )


def _base_entries(
    subject_digest=None,
    *,
    fresh_until=FIXED_TIME,
    collected_at=FIXED_TIME,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    return (
        _entry(
            subject_digest,
            "ev-1",
            "git_snapshot",
            "collector.git",
            fresh_until=fresh_until,
            collected_at=collected_at,
        ),
        _entry(
            subject_digest,
            "ev-2",
            "intake_documents",
            "collector.intake",
            fresh_until=fresh_until,
            collected_at=collected_at,
        ),
        _entry(
            subject_digest,
            "ev-3",
            "command_batch",
            "collector.command",
            fresh_until=fresh_until,
            collected_at=collected_at,
        ),
    )


def _manifest_body_bytes(values):
    body = {
        key: value
        for key, value in values.items()
        if key not in ("manifest_id", "canonical_digest", "artifact_digest")
    }
    return json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest(subject_digest=None, entries=None, *, evaluated_at=FIXED_TIME):
    if subject_digest is None:
        subject_digest = _digest("c")
    if entries is None:
        entries = _base_entries(subject_digest)
    rebuilt = []
    for entry in sorted(entries, key=lambda item: item.evidence_id):
        if entry.fresh_until is None:
            freshness = "unknown"
        else:
            freshness = (
                "fresh" if evaluated_at <= entry.fresh_until else "stale"
            )
        rebuilt.append(entry.model_copy(update={"freshness": freshness}))
    entries = tuple(rebuilt)
    incomplete = any(
        entry.status in {"error", "timeout", "cancelled", "truncated"}
        for entry in entries
    )
    stale = any(entry.freshness == "stale" for entry in entries)
    unknown = any(entry.freshness == "unknown" for entry in entries)
    unredacted = any(
        entry.redaction_status == "contains_unredacted_content"
        for entry in entries
    )
    unassessed = any(
        entry.redaction_status == "not_assessed" for entry in entries
    )
    has_gaps = incomplete or stale or unknown or unredacted or unassessed
    values = {
        "schema_version": "v1",
        "manifest_id": "em_" + "0" * 32,
        "subject_digest": subject_digest,
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "evidence_count": len(entries),
        "completeness_status": "has_gaps" if has_gaps else "complete",
        "has_incomplete_evidence": incomplete,
        "has_stale_evidence": stale,
        "has_unknown_freshness": unknown,
        "has_unredacted_content": unredacted,
        "has_unassessed_redaction": unassessed,
        "canonical_digest": _digest("0"),
        "artifact_digest": _digest("0"),
    }
    digest = _sha256(_manifest_body_bytes(values))
    values["canonical_digest"] = digest
    values["artifact_digest"] = digest
    values["manifest_id"] = "em_" + hashlib.sha256(
        (subject_digest + digest).encode("utf-8")
    ).hexdigest()[:32]
    return EvidenceManifest.model_validate(values)


def _risk_input(
    subject_digest=None,
    *,
    snapshot=None,
    intake=None,
    manifest=None,
    declarations=None,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    return RiskClassificationInput.model_validate(
        {
            "schema_version": "v1",
            "snapshot": (
                snapshot
                if snapshot is not None
                else _git_snapshot(subject_digest)
            ),
            "intake": (
                intake
                if intake is not None
                else _intake_snapshot(subject_digest)
            ),
            "manifest": (
                manifest
                if manifest is not None
                else _manifest(subject_digest)
            ),
            "declarations": (
                declarations
                if declarations is not None
                else _declarations()
            ),
        }
    )


def _subject(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "schema_version": "v1",
        "change_id": "change-1",
        "subject_digest": subject_digest,
        "repository": "acme/service",
        "base_revision": "a" * 40,
        "head_revision": "b" * 40,
        "task_digest": _digest("d"),
        "policy_version": "v2-p3",
        "created_at": FIXED_TIME,
    }
    values.update(overrides)
    return ChangeSubject.model_validate(values)


def _expectation(issue_id, category, reason_refs):
    return RulesOnlyExpectation(
        issue_id=issue_id,
        category=category,
        expected_reason_refs=reason_refs,
    )


def _fixture(
    *,
    fixture_id,
    subject=None,
    risk_input=None,
    expectations,
    allowed_reason_refs=(),
    gold_outcome,
    evaluated_at=FIXED_TIME,
):
    if subject is None:
        subject = _subject()
    if risk_input is None:
        risk_input = _risk_input(subject.subject_digest)
    return RulesOnlyFixture(
        fixture_id=fixture_id,
        subject=subject,
        risk_input=risk_input,
        expectations=expectations,
        allowed_reason_refs=allowed_reason_refs,
        gold_outcome=gold_outcome,
        evaluated_at=evaluated_at,
    )


def _fixture_a():
    subject_digest = _digest("c")
    return _fixture(
        fixture_id="safe_low",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="PASS",
        evaluated_at=FIXED_TIME,
    )


def _fixture_b():
    subject_digest = _digest("c")
    entries = _base_entries(subject_digest) + (
        _entry(
            subject_digest,
            "ev-4",
            "authz_validation",
            "collector.authz_validation",
            fresh_until=FIXED_TIME,
        ),
    )
    risk_input = _risk_input(
        subject_digest,
        snapshot=_git_snapshot(
            subject_digest, changes=(_change("auth/main.py"),)
        ),
        manifest=_manifest(subject_digest, entries=entries),
    )
    return _fixture(
        fixture_id="auth_change",
        subject=_subject(subject_digest),
        risk_input=risk_input,
        expectations=(
            _expectation(
                "auth-change",
                "boundary",
                ("risk:AUTHORIZATION_CHANGE",),
            ),
        ),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="NEEDS_HUMAN",
        evaluated_at=FIXED_TIME,
    )


def _fixture_c():
    subject_digest = _digest("c")
    entries = _base_entries(subject_digest) + (
        _entry(
            subject_digest,
            "ev-5",
            "provider_boundary_attestation",
            "collector.provider_boundary_attestation",
            fresh_until=FIXED_TIME,
        ),
    )
    risk_input = _risk_input(
        subject_digest,
        manifest=_manifest(subject_digest, entries=entries),
        declarations=_declarations(
            provider_boundary="crosses_declared_boundary"
        ),
    )
    return _fixture(
        fixture_id="provider_crossing",
        subject=_subject(subject_digest),
        risk_input=risk_input,
        expectations=(
            _expectation(
                "provider-crossing",
                "boundary",
                (
                    "risk:PROVIDER_BOUNDARY_CROSSING",
                    "gate:PROVIDER_BOUNDARY_CROSSING",
                ),
            ),
        ),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="BLOCKED",
        evaluated_at=FIXED_TIME,
    )


def _fixture_d():
    subject_digest = _digest("c")
    return _fixture(
        fixture_id="scope_creep",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(_expectation("scope-creep", "intent", ()),),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="BLOCKED",
        evaluated_at=FIXED_TIME,
    )


def _fixture_e():
    subject_digest = _digest("c")
    entries = _base_entries(subject_digest, fresh_until=LATER_TIME) + (
        _entry(
            subject_digest,
            "ev-expired",
            "git_snapshot",
            "collector.git",
            collected_at=EARLIER_TIME,
            fresh_until=FIXED_TIME,
        ),
    )
    risk_input = _risk_input(
        subject_digest,
        manifest=_manifest(
            subject_digest, entries=entries, evaluated_at=LATER_TIME
        ),
    )
    return _fixture(
        fixture_id="evidence_expired",
        subject=_subject(subject_digest),
        risk_input=risk_input,
        expectations=(
            _expectation(
                "evidence-expired",
                "evidence",
                (
                    "risk:EVIDENCE_GAPS",
                    "gate:MANIFEST_HAS_GAPS",
                    "gate:EVIDENCE_EXPIRED",
                ),
            ),
        ),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="BLOCKED",
        evaluated_at=LATER_TIME,
    )


def _fixture_f():
    subject_digest = _digest("c")
    entries = _base_entries(subject_digest, fresh_until=LATER_TIME) + (
        _entry(
            subject_digest,
            "ev-6",
            "authz_validation",
            "collector.authz_validation",
            fresh_until=LATER_TIME,
        ),
        _entry(
            subject_digest,
            "ev-expired-2",
            "git_snapshot",
            "collector.git",
            collected_at=EARLIER_TIME,
            fresh_until=FIXED_TIME,
        ),
    )
    risk_input = _risk_input(
        subject_digest,
        snapshot=_git_snapshot(
            subject_digest, changes=(_change("auth/main.py"),)
        ),
        manifest=_manifest(
            subject_digest, entries=entries, evaluated_at=LATER_TIME
        ),
    )
    return _fixture(
        fixture_id="source_order",
        subject=_subject(subject_digest),
        risk_input=risk_input,
        expectations=(
            _expectation(
                "source-order-multi",
                "evidence",
                (
                    "risk:AUTHORIZATION_CHANGE",
                    "risk:EVIDENCE_GAPS",
                    "gate:MANIFEST_HAS_GAPS",
                    "gate:EVIDENCE_EXPIRED",
                    "gate:REQUIRED_REVIEWER_MISSING",
                ),
            ),
        ),
        allowed_reason_refs=("gate:REQUIRED_REVIEWER_MISSING",),
        gold_outcome="BLOCKED",
        evaluated_at=LATER_TIME,
    )


def _fixture_data(fixture):
    return {
        "schema_version": "v1",
        "fixture_id": fixture.fixture_id,
        "subject": fixture.subject,
        "risk_input": fixture.risk_input,
        "expectations": fixture.expectations,
        "allowed_reason_refs": fixture.allowed_reason_refs,
        "gold_outcome": fixture.gold_outcome,
        "evaluated_at": fixture.evaluated_at,
    }


def test_four_public_exports_importable():
    for value in (
        RulesOnlyExpectation,
        RulesOnlyFixture,
        RulesOnlyBaselineResult,
        RulesOnlyBaselineRunner,
    ):
        assert value is not None


def test_package_exports_preserve_historical_subset_and_add_four():
    historical = {
        "ChangeSubject",
        "RiskClassifier",
        "PolicyGate",
        "EvidenceManifest",
        "GitSnapshot",
        "IntakeSnapshot",
    }
    added = {
        "RulesOnlyExpectation",
        "RulesOnlyFixture",
        "RulesOnlyBaselineResult",
        "RulesOnlyBaselineRunner",
    }
    assert historical <= set(assurance.__all__)
    assert added <= set(assurance.__all__)


def test_expectation_round_trip_and_exact_types():
    expectation = RulesOnlyExpectation(
        issue_id="issue-1",
        category="evidence",
        expected_reason_refs=("risk:AUTHORIZATION_CHANGE",),
    )
    assert type(expectation.issue_id) is str
    assert type(expectation.category) is str
    assert type(expectation.expected_reason_refs) is tuple
    assert expectation.model_dump()["schema_version"] == "v1"
    restored = RulesOnlyExpectation.model_validate_json(
        expectation.model_dump_json()
    )
    assert restored == expectation


def test_expectation_v1_only_extra_forbid_and_frozen():
    valid = {
        "schema_version": "v1",
        "issue_id": "issue-1",
        "category": "evidence",
        "expected_reason_refs": (),
    }
    with pytest.raises(ValidationError):
        RulesOnlyExpectation.model_validate(
            {**valid, "schema_version": "v2"}
        )
    with pytest.raises(ValidationError):
        RulesOnlyExpectation.model_validate(
            {**valid, "unexpected_field": 1}
        )
    expectation = RulesOnlyExpectation.model_validate(valid)
    with pytest.raises(ValidationError):
        expectation.issue_id = "changed"
    with pytest.raises(TypeError):
        expectation.expected_reason_refs[0] = "risk:X"


def test_expectation_issue_id_boundary():
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            RulesOnlyExpectation(
                issue_id=bad,
                category="policy",
                expected_reason_refs=(),
            )
    with pytest.raises(ValidationError):
        RulesOnlyExpectation(
            issue_id="x" * 129,
            category="policy",
            expected_reason_refs=(),
        )


def test_expectation_category_literal():
    for category in (
        "intent",
        "architecture",
        "operability",
        "policy",
        "evidence",
        "boundary",
        "ownership",
    ):
        RulesOnlyExpectation(
            issue_id="i",
            category=category,
            expected_reason_refs=(),
        )
    with pytest.raises(ValidationError):
        RulesOnlyExpectation(
            issue_id="i",
            category="security",
            expected_reason_refs=(),
        )


def test_expectation_reason_refs_raw_tuple_and_grammar():
    RulesOnlyExpectation(
        issue_id="i",
        category="policy",
        expected_reason_refs=(
            "risk:AUTHORIZATION_CHANGE",
            "gate:REQUIRED_REVIEWER_MISSING",
        ),
    )
    with pytest.raises(ValidationError):
        RulesOnlyExpectation(
            issue_id="i",
            category="policy",
            expected_reason_refs=["risk:AUTHORIZATION_CHANGE"],
        )
    for bad in (
        "AUTHORIZATION_CHANGE",
        "risk:lower",
        "risk:1ABC",
        "risk:AB C",
        "risk:",
        "risk:AUTHORIZATION_CHANGE!",
        "gate:",
        "other:A",
        "",
        "  ",
    ):
        with pytest.raises(ValidationError):
            RulesOnlyExpectation(
                issue_id="i",
                category="policy",
                expected_reason_refs=(bad,),
            )
    with pytest.raises(ValidationError):
        RulesOnlyExpectation(
            issue_id="i",
            category="policy",
            expected_reason_refs=("risk:AA", "risk:AA"),
        )


def test_expectation_reason_refs_canonicalized():
    expectation = RulesOnlyExpectation(
        issue_id="i",
        category="policy",
        expected_reason_refs=(
            "gate:ZZZ",
            "risk:AAA",
            "gate:AAA",
        ),
    )
    assert expectation.expected_reason_refs == (
        "risk:AAA",
        "gate:AAA",
        "gate:ZZZ",
    )


def test_fixture_round_trip_and_field_order():
    fixture = _fixture_a()
    assert list(RulesOnlyFixture.model_fields) == [
        "schema_version",
        "fixture_id",
        "subject",
        "risk_input",
        "expectations",
        "allowed_reason_refs",
        "gold_outcome",
        "evaluated_at",
    ]
    restored = RulesOnlyFixture.model_validate_json(
        fixture.model_dump_json()
    )
    assert restored == fixture


def test_fixture_v1_only_extra_forbid_and_frozen():
    data = _fixture_data(_fixture_a())
    with pytest.raises(ValidationError):
        RulesOnlyFixture.model_validate(
            {**data, "schema_version": "v2"}
        )
    for forbidden in (
        "findings",
        "execution_receipts",
        "human_decisions",
        "reviewer_receipts",
        "collector_inputs",
    ):
        with pytest.raises(ValidationError):
            RulesOnlyFixture.model_validate({**data, forbidden: ()})
    fixture = _fixture_a()
    with pytest.raises(ValidationError):
        fixture.fixture_id = "changed"


def test_fixture_exact_nested_models_and_raw_tuples():
    data = _fixture_data(_fixture_a())
    with pytest.raises(ValidationError):
        RulesOnlyFixture.model_validate(
            {**data, "subject": data["subject"].model_dump()}
        )
    with pytest.raises(ValidationError):
        RulesOnlyFixture.model_validate(
            {**data, "expectations": list(data["expectations"])}
        )
    with pytest.raises(ValidationError):
        RulesOnlyFixture.model_validate(
            {
                **data,
                "allowed_reason_refs": list(data["allowed_reason_refs"]),
            }
        )


def test_fixture_expectations_empty_valid_order_and_duplicates():
    subject_digest = _digest("c")
    empty = _fixture(
        fixture_id="empty",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(),
        gold_outcome="PASS",
    )
    assert empty.expectations == ()
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="unsorted",
            subject=_subject(subject_digest),
            risk_input=_risk_input(subject_digest),
            expectations=(
                _expectation("z-issue", "policy", ()),
                _expectation("a-issue", "policy", ()),
            ),
            gold_outcome="PASS",
        )
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="duplicate",
            subject=_subject(subject_digest),
            risk_input=_risk_input(subject_digest),
            expectations=(
                _expectation("same", "policy", ()),
                _expectation("same", "evidence", ()),
            ),
            gold_outcome="PASS",
        )


def test_fixture_allowed_reason_refs_canonicalized_and_validated():
    subject_digest = _digest("c")
    fixture = _fixture(
        fixture_id="allowed_norm",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(_expectation("i-1", "policy", ()),),
        allowed_reason_refs=(
            "gate:REQUIRED_REVIEWER_MISSING",
            "risk:AUTHORIZATION_CHANGE",
        ),
        gold_outcome="PASS",
    )
    assert fixture.allowed_reason_refs == (
        "risk:AUTHORIZATION_CHANGE",
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="bad_ref",
            subject=_subject(subject_digest),
            risk_input=_risk_input(subject_digest),
            expectations=(_expectation("i-1", "policy", ()),),
            allowed_reason_refs=("risk:AA", "risk:AA"),
            gold_outcome="PASS",
        )
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="bad_ref2",
            subject=_subject(subject_digest),
            risk_input=_risk_input(subject_digest),
            expectations=(_expectation("i-1", "policy", ()),),
            allowed_reason_refs=("bad",),
            gold_outcome="PASS",
        )


def test_fixture_id_grammar():
    subject_digest = _digest("c")
    for bad in ("A", "1a", "a-b", "", "a b", "a" * 65):
        with pytest.raises(ValidationError):
            _fixture(
                fixture_id=bad,
                subject=_subject(subject_digest),
                risk_input=_risk_input(subject_digest),
                expectations=(_expectation("i-1", "policy", ()),),
                gold_outcome="PASS",
            )


def test_fixture_evaluated_at_fail_closed():
    subject_digest = _digest("c")
    for bad in (123, 1.5, True, "1724673600", "1.5e3"):
        with pytest.raises(ValidationError):
            _fixture(
                fixture_id="bad_time",
                subject=_subject(subject_digest),
                risk_input=_risk_input(subject_digest),
                expectations=(_expectation("i-1", "policy", ()),),
                gold_outcome="PASS",
                evaluated_at=bad,
            )


def test_fixture_subject_digest_mismatch():
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="mismatch",
            subject=_subject(_digest("d")),
            risk_input=_risk_input(_digest("c")),
            expectations=(_expectation("i-1", "policy", ()),),
            gold_outcome="PASS",
        )


def test_fixture_time_bounds():
    subject_digest = _digest("c")
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="before_subject",
            subject=_subject(subject_digest),
            risk_input=_risk_input(subject_digest),
            expectations=(_expectation("i-1", "policy", ()),),
            gold_outcome="PASS",
            evaluated_at=EARLIER_TIME,
        )
    with pytest.raises(ValidationError):
        _fixture(
            fixture_id="before_manifest",
            subject=_subject(subject_digest),
            risk_input=_risk_input(
                subject_digest,
                manifest=_manifest(
                    subject_digest, evaluated_at=LATER_TIME
                ),
            ),
            expectations=(_expectation("i-1", "policy", ()),),
            gold_outcome="PASS",
            evaluated_at=FIXED_TIME,
        )


def test_fixture_deterministic_normalization_bytes():
    subject_digest = _digest("c")
    first = _fixture(
        fixture_id="deterministic",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(
            _expectation(
                "i-1",
                "policy",
                ("gate:ZZZ", "risk:AAA"),
            ),
        ),
        allowed_reason_refs=(
            "gate:REQUIRED_REVIEWER_MISSING",
            "risk:AUTHORIZATION_CHANGE",
        ),
        gold_outcome="PASS",
    )
    second = _fixture(
        fixture_id="deterministic",
        subject=_subject(subject_digest),
        risk_input=_risk_input(subject_digest),
        expectations=(
            _expectation(
                "i-1",
                "policy",
                ("risk:AAA", "gate:ZZZ"),
            ),
        ),
        allowed_reason_refs=(
            "risk:AUTHORIZATION_CHANGE",
            "gate:REQUIRED_REVIEWER_MISSING",
        ),
        gold_outcome="PASS",
    )
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_runner_rejects_dict_and_subclass():
    fixture = _fixture_a()
    with pytest.raises(TypeError):
        RulesOnlyBaselineRunner.run(fixture.model_dump())

    class SubRulesOnlyFixture(RulesOnlyFixture):
        pass

    subclass = SubRulesOnlyFixture(**_fixture_data(fixture))
    with pytest.raises(TypeError):
        RulesOnlyBaselineRunner.run(subclass)


def test_runner_is_stateless_and_deterministic():
    fixture = _fixture_a()
    first = RulesOnlyBaselineRunner.run(fixture)
    second = RulesOnlyBaselineRunner.run(_fixture_a())
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert type(first) is RulesOnlyBaselineResult


def test_fixture_a_safe_low_false_block_and_no_unexpected():
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    assert result.fixture.expectations == ()
    assert result.risk_result.classification.risk_level == "low"
    assert result.policy_result.decision.outcome == "BLOCKED"
    assert result.observed_reason_refs == (
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    assert result.detected_issue_ids == ()
    assert result.missed_issue_ids == ()
    assert result.unexpected_reason_refs == ()
    assert result.outcome_match is False
    assert result.false_block is True
    assert result.false_pass is False


def test_fixture_b_auth_change_detected_and_false_block():
    result = RulesOnlyBaselineRunner.run(_fixture_b())
    assert "AUTHORIZATION_CHANGE" in (
        result.risk_result.classification.reason_codes
    )
    assert result.risk_result.classification.risk_level == "high"
    assert result.policy_result.decision.outcome == "BLOCKED"
    assert result.observed_reason_refs == (
        "risk:AUTHORIZATION_CHANGE",
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    assert result.detected_issue_ids == ("auth-change",)
    assert result.missed_issue_ids == ()
    assert result.unexpected_reason_refs == ()
    assert result.outcome_match is False
    assert result.false_block is True
    assert result.false_pass is False


def test_fixture_c_provider_crossing_detected_and_outcome_match():
    result = RulesOnlyBaselineRunner.run(_fixture_c())
    assert result.observed_reason_refs == (
        "risk:PROVIDER_BOUNDARY_CROSSING",
        "gate:PROVIDER_BOUNDARY_CROSSING",
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    assert result.detected_issue_ids == ("provider-crossing",)
    assert result.missed_issue_ids == ()
    assert result.unexpected_reason_refs == ()
    assert result.outcome_match is True
    assert result.false_block is False
    assert result.false_pass is False


def test_fixture_d_scope_creep_missed_despite_outcome_match():
    result = RulesOnlyBaselineRunner.run(_fixture_d())
    assert len(result.fixture.expectations) == 1
    assert result.fixture.expectations[0].expected_reason_refs == ()
    assert result.policy_result.decision.outcome == "BLOCKED"
    assert result.outcome_match is True
    assert result.detected_issue_ids == ()
    assert result.missed_issue_ids == ("scope-creep",)
    assert result.unexpected_reason_refs == ()
    assert result.false_block is False
    assert result.false_pass is False


def test_fixture_e_evidence_gap_expired_detected_and_blocked():
    result = RulesOnlyBaselineRunner.run(_fixture_e())
    assert "EVIDENCE_GAPS" in result.risk_result.classification.reason_codes
    assert result.policy_result.decision.outcome == "BLOCKED"
    assert result.observed_reason_refs == (
        "risk:EVIDENCE_GAPS",
        "gate:MANIFEST_HAS_GAPS",
        "gate:EVIDENCE_EXPIRED",
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    assert result.detected_issue_ids == ("evidence-expired",)
    assert result.missed_issue_ids == ()
    assert result.unexpected_reason_refs == ()
    assert result.outcome_match is True
    assert result.false_block is False
    assert result.false_pass is False


def test_fixture_f_multi_reason_preserves_source_order():
    result = RulesOnlyBaselineRunner.run(_fixture_f())
    expected_observed = (
        "risk:AUTHORIZATION_CHANGE",
        "risk:EVIDENCE_GAPS",
        "gate:MANIFEST_HAS_GAPS",
        "gate:EVIDENCE_EXPIRED",
        "gate:REQUIRED_REVIEWER_MISSING",
    )
    assert expected_observed != tuple(sorted(expected_observed))
    assert result.observed_reason_refs == expected_observed
    assert result.observed_reason_refs == tuple(
        f"risk:{code}"
        for code in result.risk_result.classification.reason_codes
    ) + tuple(
        f"gate:{code}"
        for code in result.policy_result.decision.reason_codes
    )
    assert result.detected_issue_ids == ("source-order-multi",)
    assert result.missed_issue_ids == ()
    assert result.unexpected_reason_refs == ()


def test_result_field_order_and_exact_types():
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    assert list(RulesOnlyBaselineResult.model_fields) == [
        "schema_version",
        "fixture",
        "risk_result",
        "policy_result",
        "observed_reason_refs",
        "detected_issue_ids",
        "missed_issue_ids",
        "unexpected_reason_refs",
        "outcome_match",
        "false_block",
        "false_pass",
        "spec_digest",
        "result_digest",
        "result_id",
    ]
    assert type(result.fixture) is RulesOnlyFixture
    assert type(result.risk_result) is RiskClassificationResult
    assert type(result.policy_result) is PolicyGateResult
    for field in (
        "observed_reason_refs",
        "detected_issue_ids",
        "missed_issue_ids",
        "unexpected_reason_refs",
    ):
        assert type(getattr(result, field)) is tuple
    for field in ("outcome_match", "false_block", "false_pass"):
        assert type(getattr(result, field)) is bool


def test_result_v1_only_extra_forbid_and_frozen():
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    data = result.model_dump(mode="json")
    with pytest.raises(ValidationError):
        RulesOnlyBaselineResult.model_validate_json(
            json.dumps({**data, "schema_version": "v2"})
        )
    with pytest.raises(ValidationError):
        RulesOnlyBaselineResult.model_validate_json(
            json.dumps({**data, "extra_field": 1})
        )
    with pytest.raises(ValidationError):
        result.fixture = _fixture_b()


def test_result_round_trip():
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    restored = RulesOnlyBaselineResult.model_validate_json(
        result.model_dump_json()
    )
    assert restored == result
    assert restored.model_dump_json() == result.model_dump_json()


def test_result_rejects_single_field_forgery():
    valid = RulesOnlyBaselineRunner.run(_fixture_a())
    other = RulesOnlyBaselineRunner.run(_fixture_b())
    base = valid.model_dump(mode="json")
    other_data = other.model_dump(mode="json")
    mutations = {
        "fixture": other_data["fixture"],
        "risk_result": other_data["risk_result"],
        "policy_result": other_data["policy_result"],
        "observed_reason_refs": other_data["observed_reason_refs"],
        "detected_issue_ids": other_data["detected_issue_ids"],
        "missed_issue_ids": ("forged-missed",),
        "unexpected_reason_refs": ("gate:UNEXPECTED",),
        "outcome_match": not valid.outcome_match,
        "false_block": not valid.false_block,
        "false_pass": not valid.false_pass,
        "spec_digest": _digest("9"),
        "result_digest": _digest("8"),
        "result_id": "rulesb_" + "0" * 32,
    }
    for field, value in mutations.items():
        mutated = dict(base)
        mutated[field] = value
        with pytest.raises(ValidationError):
            RulesOnlyBaselineResult.model_validate_json(
                json.dumps(mutated)
            )


def test_result_synchronized_forgery_rejected():
    valid = RulesOnlyBaselineRunner.run(_fixture_a())
    other = RulesOnlyBaselineRunner.run(_fixture_b())
    mutated = dict(valid.model_dump(mode="json"))
    other_data = other.model_dump(mode="json")
    for field in (
        "risk_result",
        "policy_result",
        "observed_reason_refs",
        "detected_issue_ids",
        "missed_issue_ids",
        "unexpected_reason_refs",
        "outcome_match",
        "false_block",
        "false_pass",
        "spec_digest",
        "result_digest",
        "result_id",
    ):
        mutated[field] = other_data[field]
    with pytest.raises(ValidationError):
        RulesOnlyBaselineResult.model_validate_json(json.dumps(mutated))

    mutated = dict(other_data)
    valid_data = valid.model_dump(mode="json")
    for field in (
        "risk_result",
        "policy_result",
        "observed_reason_refs",
        "detected_issue_ids",
        "missed_issue_ids",
        "unexpected_reason_refs",
        "outcome_match",
        "false_block",
        "false_pass",
        "spec_digest",
        "result_digest",
        "result_id",
    ):
        mutated[field] = valid_data[field]
    with pytest.raises(ValidationError):
        RulesOnlyBaselineResult.model_validate_json(json.dumps(mutated))


def test_result_digest_and_id_formats():
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result.spec_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result.result_digest)
    assert re.fullmatch(r"rulesb_[0-9a-f]{32}", result.result_id)
    assert result.spec_digest == rules_module._RULES_DIGEST


def test_rules_table_specifies_risk_then_gate_source_order():
    assert rules_module._RULES_TABLE["ref_canonical_order"] == (
        "risk_then_gate_source_order"
    )
    assert "lexicographic" not in json.dumps(
        rules_module._jsonable(rules_module._RULES_TABLE)
    )


def test_spec_table_nested_mutation_fails_and_digest_unchanged():
    before_digest = rules_module._RULES_DIGEST
    result = RulesOnlyBaselineRunner.run(_fixture_a())
    with pytest.raises(TypeError):
        rules_module._RULES_TABLE["rules_version"] = "changed"
    with pytest.raises(TypeError):
        rules_module._RULES_TABLE["derivations"][
            "result_id_prefix"
        ] = "x"
    with pytest.raises(TypeError):
        rules_module._RULES_TABLE["outcome_sets"]["blocked"] = frozenset()
    assert rules_module._RULES_DIGEST == before_digest
    assert RulesOnlyBaselineRunner.run(_fixture_a()) == result


def test_rules_baseline_no_io_model_or_environment_usage():
    source_path = Path(assurance.__file__).resolve().parent / (
        "rules_baseline.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    allowed_modules = {
        "hashlib",
        "json",
        "re",
        "types",
        "typing",
        "pydantic",
        "contracts",
        "risk",
        "policy",
    }
    forbidden_identifiers = {
        "os",
        "sys",
        "pathlib",
        "Path",
        "open",
        "sqlite3",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "random",
        "datetime",
        "time",
        "environ",
        "getenv",
        "subprocess",
        "Popen",
        "check_output",
        "model_construct",
        "Collector",
        "Collectors",
        "Reviewer",
        "Reviewers",
        "LLM",
        "OpenAI",
        "Anthropic",
        "litellm",
        "eval",
        "exec",
        "GitSnapshot",
        "IntakeSnapshot",
        "EvidenceManifest",
        "EvidenceManifestEntry",
        "EvidenceManifestInput",
        "ArtifactStore",
        "GitSnapshotCollector",
        "TaskPolicyCollector",
        "DeterministicCommandCollector",
        "AuthorAgentReceipt",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root in allowed_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").lstrip(".")
            assert module in allowed_modules, module
            for alias in node.names:
                assert alias.name not in forbidden_identifiers, alias.name
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_identifiers, node.id
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_identifiers, node.attr
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_identifiers
