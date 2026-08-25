"""P3-02 policy gate v0 focused tests."""

import ast
import hashlib
import inspect
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    ChangeSubject,
    EvidenceManifest,
    EvidenceManifestEntry,
    ExecutionReceipt,
    ExecutionStep,
    Finding,
    GitChange,
    GitSnapshot,
    HumanDecision,
    IntakeDocument,
    IntakeSnapshot,
    PolicyDecision,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
    RiskDeclarations,
)
from assurance import manifest as manifest_module
from assurance import policy as policy_module
from assurance.policy import (
    PolicyEvaluationInput,
    PolicyGate,
    PolicyGateResult,
)

FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
EARLIER_TIME = datetime(2026, 8, 25, 7, 0, 0, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
AFTER_LATER_TIME = datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc)

BASE_COLLECTORS = (
    "git_snapshot",
    "task_policy_adr",
    "deterministic_commands",
    "evidence_manifest",
)

COLLECTOR_MAPPING_EXPECTED = (
    ("git_snapshot", "git_snapshot", "collector.git"),
    ("task_policy_adr", "intake_documents", "collector.intake"),
    ("deterministic_commands", "command_batch", "collector.command"),
    ("authz_validation", "authz_validation", "collector.authz_validation"),
    (
        "migration_validation",
        "migration_validation",
        "collector.migration_validation",
    ),
    ("api_contract", "api_contract", "collector.api_contract"),
    ("dependency_audit", "dependency_audit", "collector.dependency_audit"),
    (
        "ci_iac_validation",
        "ci_iac_validation",
        "collector.ci_iac_validation",
    ),
    (
        "side_effect_validation",
        "side_effect_validation",
        "collector.side_effect_validation",
    ),
    (
        "provider_boundary_attestation",
        "provider_boundary_attestation",
        "collector.provider_boundary_attestation",
    ),
)

_COLLECTOR_SIGNALS = {
    "authz_validation": ("auth/main.py", {}),
    "migration_validation": ("db/migrations/001.sql", {}),
    "api_contract": ("api/users.py", {}),
    "dependency_audit": ("pyproject.toml", {}),
    "ci_iac_validation": (".github/workflows/ci.yml", {}),
    "side_effect_validation": (
        "a.txt",
        {"external_side_effects": "present_declared"},
    ),
    "provider_boundary_attestation": (
        "a.txt",
        {"provider_boundary": "crosses_declared_boundary"},
    ),
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def _canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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
):
    return EvidenceManifestEntry.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": evidence_id,
            "kind": kind,
            "trust_level": trust_level,
            "producer": producer,
            "subject_digest": subject_digest,
            "artifact_digest": _digest("a"),
            "source_ref": "command_batch:" + _digest("a"),
            "status": status,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "freshness": (
                "unknown" if fresh_until is None else "fresh"
            ),
            "redaction_status": redaction_status,
        }
    )


def _base_entries(subject_digest=None, *, fresh_until=FIXED_TIME):
    if subject_digest is None:
        subject_digest = _digest("c")
    return (
        _entry(
            subject_digest,
            "ev-1",
            "git_snapshot",
            "collector.git",
            fresh_until=fresh_until,
        ),
        _entry(
            subject_digest,
            "ev-2",
            "intake_documents",
            "collector.intake",
            fresh_until=fresh_until,
        ),
        _entry(
            subject_digest,
            "ev-3",
            "command_batch",
            "collector.command",
            fresh_until=fresh_until,
        ),
    )


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
        rebuilt.append(
            entry.model_copy(update={"freshness": freshness})
        )
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
    provisional = EvidenceManifest.model_construct(
        schema_version="v1",
        manifest_id="em_" + "0" * 32,
        subject_digest=subject_digest,
        evaluated_at=evaluated_at,
        entries=entries,
        evidence_count=len(entries),
        completeness_status="has_gaps" if has_gaps else "complete",
        has_incomplete_evidence=incomplete,
        has_stale_evidence=stale,
        has_unknown_freshness=unknown,
        has_unredacted_content=unredacted,
        has_unassessed_redaction=unassessed,
        canonical_digest=_digest("0"),
        artifact_digest=_digest("0"),
    )
    body = manifest_module._canonical_body(provisional)
    digest = _sha256(body)
    manifest_id = "em_" + hashlib.sha256(
        (subject_digest + digest).encode("utf-8")
    ).hexdigest()[:32]
    values = {
        "schema_version": "v1",
        "manifest_id": manifest_id,
        "subject_digest": subject_digest,
        "evaluated_at": evaluated_at,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "evidence_count": len(entries),
        "completeness_status": "has_gaps" if has_gaps else "complete",
        "has_incomplete_evidence": incomplete,
        "has_stale_evidence": stale,
        "has_unknown_freshness": unknown,
        "has_unredacted_content": unredacted,
        "has_unassessed_redaction": unassessed,
        "canonical_digest": digest,
        "artifact_digest": digest,
    }
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


def _risk_result(
    subject_digest=None,
    *,
    snapshot=None,
    intake=None,
    manifest=None,
    declarations=None,
):
    return RiskClassifier.classify(
        _risk_input(
            subject_digest,
            snapshot=snapshot,
            intake=intake,
            manifest=manifest,
            declarations=declarations,
        )
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


def _finding(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "schema_version": "v1",
        "finding_id": "finding-1",
        "subject_digest": subject_digest,
        "reviewer_role": "intent",
        "claim": "a finding",
        "evidence_refs": ("ev-1",),
        "basis": "inferred",
        "severity": "low",
        "confidence": 0.5,
        "rubric_hash": _digest("f"),
        "model_ref": "model-x",
        "status": "open",
    }
    values.update(overrides)
    return Finding.model_validate(values)


def _step(
    sequence,
    planned_role,
    *,
    result="success",
    actual_role=None,
    schema_status=None,
    **overrides,
):
    if result in ("success", "failure", "timeout", "cancelled"):
        if actual_role is None:
            actual_role = planned_role
    else:
        actual_role = None
    if schema_status is None:
        if result == "success":
            schema_status = "valid"
        elif result in ("skipped", "blocked", "timeout", "cancelled"):
            schema_status = "not_produced"
        else:
            schema_status = "invalid"
    values = {
        "sequence": sequence,
        "planned_role": planned_role,
        "actual_role": actual_role,
        "model_ref": "model-x" if actual_role is not None else None,
        "provider": "provider-x" if actual_role is not None else None,
        "tool_grants": (),
        "routing_rule": "route-x",
        "fallback_reason": None,
        "token_budget": None,
        "timeout_seconds": 60,
        "result": result,
        "schema_status": schema_status,
    }
    values.update(overrides)
    return ExecutionStep.model_validate(values)


def _receipt(
    subject_digest=None,
    *,
    steps=None,
    completed_at=FIXED_TIME,
    **overrides,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    if steps is None:
        steps = (_step(0, "intent"),)
    values = {
        "schema_version": "v1",
        "receipt_id": "receipt-1",
        "run_id": "run-1",
        "subject_digest": subject_digest,
        "steps": steps,
        "overall_result": "success",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "started_at": FIXED_TIME,
        "completed_at": completed_at,
    }
    values.update(overrides)
    return ExecutionReceipt.model_validate(values)


def _decision(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "schema_version": "v1",
        "decision_id": "decision-1",
        "subject_digest": subject_digest,
        "actor_type": "human",
        "owner": "owner-a",
        "owner_role": "change_owner",
        "decision": "approve",
        "reason": "approved",
        "conditions": (),
        "waiver_id": None,
        "expires_at": None,
        "decided_at": FIXED_TIME,
    }
    values.update(overrides)
    return HumanDecision.model_validate(values)


def _input_data(
    *,
    subject=None,
    risk_result=None,
    findings=(),
    execution_receipts=None,
    human_decisions=(),
    evaluated_at=FIXED_TIME,
    **overrides,
):
    if subject is None:
        subject = _subject()
    if risk_result is None:
        risk_result = _risk_result(subject.subject_digest)
    if execution_receipts is None:
        execution_receipts = (_receipt(subject.subject_digest),)
    values = {
        "schema_version": "v1",
        "subject": subject,
        "risk_result": risk_result,
        "findings": findings,
        "execution_receipts": execution_receipts,
        "human_decisions": human_decisions,
        "evaluated_at": evaluated_at,
    }
    values.update(overrides)
    return values


def _input(**kwargs):
    return PolicyEvaluationInput.model_validate(_input_data(**kwargs))


def _high_input(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    risk_result = _risk_result(
        subject_digest,
        snapshot=_git_snapshot(subject_digest, changed_files_total=21),
    )
    assert risk_result.classification.risk_level == "high"
    assert risk_result.classification.required_human_role == "change_owner"
    values = {
        "schema_version": "v1",
        "subject": _subject(subject_digest),
        "risk_result": risk_result,
        "findings": (),
        "execution_receipts": (
            _receipt(
                subject_digest,
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                ),
            ),
        ),
        "human_decisions": (_decision(subject_digest),),
        "evaluated_at": FIXED_TIME,
    }
    values.update(overrides)
    return PolicyEvaluationInput.model_validate(values)


def _late_high_input(**overrides):
    subject_digest = _digest("c")
    risk_result = _risk_result(
        subject_digest,
        snapshot=_git_snapshot(subject_digest, changed_files_total=21),
        manifest=_manifest(
            subject_digest,
            entries=_base_entries(
                subject_digest, fresh_until=LATER_TIME
            ),
        ),
    )
    values = {
        "schema_version": "v1",
        "subject": _subject(subject_digest),
        "risk_result": risk_result,
        "findings": (),
        "execution_receipts": (
            _receipt(
                subject_digest,
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                ),
            ),
        ),
        "human_decisions": (_decision(subject_digest),),
        "evaluated_at": LATER_TIME,
    }
    values.update(overrides)
    return PolicyEvaluationInput.model_validate(values)


def _collector_input(
    collector_name,
    entries,
    *,
    subject_digest=None,
    human=True,
    evaluated_at=FIXED_TIME,
    **overrides,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    change_path, declaration_overrides = _COLLECTOR_SIGNALS[collector_name]
    snapshot = _git_snapshot(
        subject_digest, changes=(_change(change_path),)
    )
    manifest = _manifest(subject_digest, entries=entries)
    risk_result = _risk_result(
        subject_digest,
        snapshot=snapshot,
        manifest=manifest,
        declarations=_declarations(**declaration_overrides),
    )
    assert collector_name in risk_result.classification.required_collectors
    values = {
        "schema_version": "v1",
        "subject": _subject(subject_digest),
        "risk_result": risk_result,
        "findings": (),
        "execution_receipts": (
            _receipt(
                subject_digest,
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                ),
            ),
        ),
        "human_decisions": (
            (_decision(subject_digest),) if human else ()
        ),
        "evaluated_at": evaluated_at,
    }
    values.update(overrides)
    return PolicyEvaluationInput.model_validate(values)


def _base_collector_case(entries, *, subject_digest=None, evaluated_at=FIXED_TIME):
    if subject_digest is None:
        subject_digest = _digest("c")
    manifest = _manifest(subject_digest, entries=entries)
    risk_result = _risk_result(subject_digest, manifest=manifest)
    return _input(
        subject=_subject(subject_digest),
        risk_result=risk_result,
        evaluated_at=evaluated_at,
    )


def _rules_table():
    return {
        "rules_version": "gate.v0",
        "reason_order": [
            "SUBJECT_DIGEST_MISMATCH",
            "FINDING_STALE",
            "PROVIDER_BOUNDARY_CROSSING",
            "PROVIDER_BOUNDARY_UNKNOWN",
            "MANIFEST_HAS_GAPS",
            "EVIDENCE_FRESHNESS_UNKNOWN",
            "EVIDENCE_EXPIRED",
            "REQUIRED_COLLECTOR_MISSING",
            "REQUIRED_COLLECTOR_NOT_SUCCESS",
            "REQUIRED_COLLECTOR_NOT_FRESH",
            "REQUIRED_REVIEWER_MISSING",
            "REQUIRED_REVIEWER_NOT_SUCCESS",
            "FINDING_EVIDENCE_REF_MISSING",
            "DETERMINISTIC_FINDING_BLOCKING",
            "REQUIRED_HUMAN_REJECTED",
            "REQUIRED_HUMAN_MISSING",
            "REQUIRED_HUMAN_WAIVER_EXPIRED",
            "REQUIRED_HUMAN_CONFLICT",
        ],
        "outcome_priority": [
            "STALE",
            "BLOCKED",
            "NEEDS_HUMAN",
            "PASS_WITH_WAIVER",
            "PASS",
        ],
        "collector_mapping": {
            "git_snapshot": ["git_snapshot", "collector.git"],
            "task_policy_adr": ["intake_documents", "collector.intake"],
            "deterministic_commands": ["command_batch", "collector.command"],
            "evidence_manifest": None,
            "authz_validation": [
                "authz_validation",
                "collector.authz_validation",
            ],
            "migration_validation": [
                "migration_validation",
                "collector.migration_validation",
            ],
            "api_contract": ["api_contract", "collector.api_contract"],
            "dependency_audit": [
                "dependency_audit",
                "collector.dependency_audit",
            ],
            "ci_iac_validation": [
                "ci_iac_validation",
                "collector.ci_iac_validation",
            ],
            "side_effect_validation": [
                "side_effect_validation",
                "collector.side_effect_validation",
            ],
            "provider_boundary_attestation": [
                "provider_boundary_attestation",
                "collector.provider_boundary_attestation",
            ],
        },
        "reviewer_schema_statuses": sorted(["valid", "repaired"]),
        "blocking_severities": sorted(["high", "critical"]),
        "blocking_statuses": sorted(["open", "acknowledged"]),
    }


def _rules_digest():
    return "sha256:" + hashlib.sha256(
        _canonical_bytes(_rules_table())
    ).hexdigest()


def _decision_id_for(data):
    body = {
        key: value for key, value in data.items() if key != "decision_id"
    }
    envelope = {
        "subject_digest": data["subject_digest"],
        "rules_digest": data["rules_digest"],
        "decision_body": body,
    }
    return "policy_" + hashlib.sha256(
        _canonical_bytes(envelope)
    ).hexdigest()[:32]


def test_public_imports_and_exports():
    assert assurance.PolicyEvaluationInput is PolicyEvaluationInput
    assert assurance.PolicyGateResult is PolicyGateResult
    assert assurance.PolicyGate is PolicyGate
    assert set(policy_module.__all__) == {
        "PolicyEvaluationInput",
        "PolicyGateResult",
        "PolicyGate",
    }
    for name in (
        "ChangeSubject",
        "Finding",
        "ExecutionReceipt",
        "HumanDecision",
        "PolicyDecision",
        "RiskClassificationResult",
        "PolicyEvaluationInput",
        "PolicyGateResult",
        "PolicyGate",
    ):
        assert name in assurance.__all__
        assert name in dir(assurance)


def test_public_field_order():
    assert list(PolicyEvaluationInput.model_fields) == [
        "schema_version",
        "subject",
        "risk_result",
        "findings",
        "execution_receipts",
        "human_decisions",
        "evaluated_at",
    ]
    assert list(PolicyGateResult.model_fields) == [
        "schema_version",
        "input",
        "decision",
    ]


def test_v1_only_extra_forbid_and_frozen():
    for model in (PolicyEvaluationInput, PolicyGateResult):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(
            {**_input_data(), "schema_version": "v2"}
        )
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(
            {**_input_data(), "extra_field": 1}
        )
    value = _input()
    with pytest.raises(ValidationError):
        value.subject = _subject()


def test_input_exact_nested_models_and_tuple_boundaries():
    base = _input_data(findings=(_finding(),))
    value = _input_data(findings=(_finding(),))

    class SubChangeSubject(ChangeSubject):
        pass

    class SubRiskClassificationResult(RiskClassificationResult):
        pass

    class SubFinding(Finding):
        pass

    class SubExecutionReceipt(ExecutionReceipt):
        pass

    class SubHumanDecision(HumanDecision):
        pass

    class TupleSubclass(tuple):
        pass

    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(
            {
                **base,
                "subject": SubChangeSubject.model_validate(
                    _subject().model_dump()
                ),
            }
        )
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(
            {
                **base,
                "risk_result": SubRiskClassificationResult.model_validate(
                    {
                        "schema_version": "v1",
                        "input": value["risk_result"].input,
                        "classification": value["risk_result"].classification,
                    }
                ),
            }
        )
    for field_name in ("findings", "execution_receipts", "human_decisions"):
        with pytest.raises(ValidationError):
            PolicyEvaluationInput.model_validate(
                {**base, field_name: []}
            )
        with pytest.raises(ValidationError):
            PolicyEvaluationInput.model_validate(
                {**base, field_name: "not-a-tuple"}
            )
    subclass_values = {
        "findings": (
            SubFinding.model_validate(_finding().model_dump()),
        ),
        "execution_receipts": (
            SubExecutionReceipt.model_validate(_receipt().model_dump()),
        ),
        "human_decisions": (
            SubHumanDecision.model_validate(_decision().model_dump()),
        ),
    }
    for field_name, items in subclass_values.items():
        with pytest.raises(ValidationError):
            PolicyEvaluationInput.model_validate(
                {**base, field_name: items}
            )
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(
            {
                **base,
                "findings": TupleSubclass((_finding(),)),
            }
        )


def test_input_duplicate_ids_rejected():
    finding = _finding()
    with pytest.raises(ValidationError):
        _input(
            findings=(
                finding,
                Finding.model_validate(
                    {**finding.model_dump(), "claim": "duplicate id"}
                ),
            )
        )
    receipt = _receipt()
    with pytest.raises(ValidationError):
        _input(
            execution_receipts=(
                receipt,
                ExecutionReceipt.model_validate(
                    {**receipt.model_dump(), "run_id": "other-run"}
                ),
            )
        )
    decision = _decision()
    with pytest.raises(ValidationError):
        _input(
            human_decisions=(
                decision,
                HumanDecision.model_validate(
                    {**decision.model_dump(), "owner": "owner-b"}
                ),
            )
        )


def test_input_datetime_boundaries():
    for bad in (0, 1.5, True, "1234567890", "1e3"):
        with pytest.raises(ValidationError):
            _input(evaluated_at=bad)
    with pytest.raises(ValidationError):
        _input(subject=_subject(created_at=LATER_TIME))
    late_manifest = _manifest(
        evaluated_at=LATER_TIME,
        entries=_base_entries(fresh_until=LATER_TIME),
    )
    with pytest.raises(ValidationError):
        _input(
            risk_result=_risk_result(manifest=late_manifest),
            evaluated_at=FIXED_TIME,
        )
    with pytest.raises(ValidationError):
        _input(execution_receipts=(_receipt(completed_at=LATER_TIME),))
    with pytest.raises(ValidationError):
        _input(human_decisions=(_decision(decided_at=LATER_TIME),))
    _input(evaluated_at=_subject().created_at)


def test_input_json_round_trip():
    value = _input(
        findings=(_finding(),),
        execution_receipts=(_receipt(),),
        human_decisions=(_decision(),),
    )
    # P3-01 约定：RiskClassificationResult 自身没有 JSON 重验证路径，
    # 因此完整 PolicyEvaluationInput 的 JSON 往返同样拒绝；raw 模式下
    # 的 dict/list 边界必须保持严格。
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate_json(value.model_dump_json())
    with pytest.raises(ValidationError):
        PolicyEvaluationInput.model_validate(value.model_dump(mode="json"))

    decision = _decision()
    restored_decision = HumanDecision.model_validate_json(
        decision.model_dump_json()
    )
    assert restored_decision == decision


def test_policy_gate_public_api_only_evaluate():
    public = {
        name for name in dir(PolicyGate) if not name.startswith("_")
    }
    assert public == {"evaluate"}


def test_policy_gate_type_checks():
    value = _input()
    result = PolicyGate.evaluate(value)
    assert type(result) is PolicyGateResult
    with pytest.raises(TypeError):
        PolicyGate.evaluate(value.model_dump())

    class SubPolicyEvaluationInput(PolicyEvaluationInput):
        pass

    subclass = SubPolicyEvaluationInput.model_validate(_input_data())
    with pytest.raises(TypeError):
        PolicyGate.evaluate(subclass)


def test_low_risk_baseline_passes():
    result = PolicyGate.evaluate(_input())
    assert result.decision.outcome == "PASS"
    assert result.decision.reason_codes == ()
    assert result.decision.waiver_ref is None


def test_outcome_priority_stale_wins_everything():
    value = _input(
        subject=_subject(_digest("e")),
        risk_result=_risk_result(_digest("c")),
        findings=(_finding(status="stale"),),
        human_decisions=(_decision(decision="reject"),),
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "STALE"
    assert result.decision.reason_codes == (
        "SUBJECT_DIGEST_MISMATCH",
        "FINDING_STALE",
    )


def test_stale_reasons_subject_digest_mismatch():
    value = _input(
        subject=_subject(_digest("e")),
        risk_result=_risk_result(_digest("c")),
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "STALE"
    assert result.decision.reason_codes == ("SUBJECT_DIGEST_MISMATCH",)
    assert result.decision.subject_digest == _digest("e")


def test_stale_reasons_subject_mismatch_entities_and_stale_only_finding():
    other = _digest("e")
    cases = (
        (
            {"findings": (_finding(subject_digest=other),)},
            ("SUBJECT_DIGEST_MISMATCH",),
        ),
        (
            {"execution_receipts": (_receipt(subject_digest=other),)},
            ("SUBJECT_DIGEST_MISMATCH",),
        ),
        (
            {"human_decisions": (_decision(subject_digest=other),)},
            ("SUBJECT_DIGEST_MISMATCH",),
        ),
        (
            {"findings": (_finding(status="stale"),)},
            ("FINDING_STALE",),
        ),
    )
    for overrides, expected in cases:
        result = PolicyGate.evaluate(_input(**overrides))
        assert result.decision.outcome == "STALE"
        assert result.decision.reason_codes == expected


def test_stale_subject_mismatch_and_stale_finding_emit_both():
    other = _digest("e")
    value = _input(
        findings=(_finding(subject_digest=other, status="stale"),)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "STALE"
    assert result.decision.reason_codes == (
        "SUBJECT_DIGEST_MISMATCH",
        "FINDING_STALE",
    )


def test_provider_boundary_reasons():
    crossing_entries = _base_entries() + (
        _entry(
            _digest("c"),
            "ev-4",
            "provider_boundary_attestation",
            "collector.provider_boundary_attestation",
            fresh_until=FIXED_TIME,
        ),
    )
    crossing = _input(
        risk_result=_risk_result(
            manifest=_manifest(entries=crossing_entries),
            declarations=_declarations(
                provider_boundary="crosses_declared_boundary"
            ),
        ),
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                )
            ),
        ),
        human_decisions=(_decision(),),
    )
    result = PolicyGate.evaluate(crossing)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("PROVIDER_BOUNDARY_CROSSING",)

    unknown_entries = _base_entries() + (
        _entry(
            _digest("c"),
            "ev-4",
            "provider_boundary_attestation",
            "collector.provider_boundary_attestation",
            fresh_until=FIXED_TIME,
        ),
    )
    unknown = _input(
        risk_result=_risk_result(
            manifest=_manifest(entries=unknown_entries),
            declarations=_declarations(
                provider_boundary="unknown"
            ),
        ),
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                )
            ),
        ),
        human_decisions=(_decision(),),
    )
    result = PolicyGate.evaluate(unknown)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("PROVIDER_BOUNDARY_UNKNOWN",)


def test_manifest_gaps_freshness_unknown_expired_and_equal_boundary():
    subject_digest = _digest("c")
    gaps_entries = _base_entries(subject_digest) + (
        _entry(
            subject_digest,
            "ev-4",
            "unrelated",
            "unrelated",
            fresh_until=FIXED_TIME,
            redaction_status="contains_unredacted_content",
        ),
    )
    gaps = _input(
        risk_result=_risk_result(manifest=_manifest(entries=gaps_entries)),
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                )
            ),
        ),
        human_decisions=(_decision(),),
    )
    result = PolicyGate.evaluate(gaps)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("MANIFEST_HAS_GAPS",)

    unknown_entries = _base_entries(subject_digest) + (
        _entry(
            subject_digest,
            "ev-4",
            "unrelated",
            "unrelated",
        ),
    )
    unknown = _input(
        risk_result=_risk_result(manifest=_manifest(entries=unknown_entries)),
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                )
            ),
        ),
        human_decisions=(_decision(),),
    )
    result = PolicyGate.evaluate(unknown)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == (
        "MANIFEST_HAS_GAPS",
        "EVIDENCE_FRESHNESS_UNKNOWN",
    )

    expired_entries = _base_entries(
        subject_digest, fresh_until=LATER_TIME
    ) + (
        _entry(
            subject_digest,
            "ev-4",
            "unrelated",
            "unrelated",
            fresh_until=FIXED_TIME,
        ),
    )
    expired = _input(
        risk_result=_risk_result(manifest=_manifest(entries=expired_entries)),
        evaluated_at=LATER_TIME,
    )
    result = PolicyGate.evaluate(expired)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("EVIDENCE_EXPIRED",)

    equal_entries = _base_entries(
        subject_digest, fresh_until=LATER_TIME
    ) + (
        _entry(
            subject_digest,
            "ev-4",
            "unrelated",
            "unrelated",
            fresh_until=LATER_TIME,
        ),
    )
    equal = _input(
        risk_result=_risk_result(manifest=_manifest(entries=equal_entries)),
        evaluated_at=LATER_TIME,
    )
    result = PolicyGate.evaluate(equal)
    assert result.decision.outcome == "PASS"
    assert result.decision.reason_codes == ()


def test_collector_mapping_frozen_content():
    mapping = policy_module._COLLECTOR_MAPPING
    expected_names = [
        name for name, _, _ in COLLECTOR_MAPPING_EXPECTED
    ]
    expected_names.insert(
        expected_names.index("authz_validation"), "evidence_manifest"
    )
    assert list(mapping) == expected_names
    for name, kind, producer in COLLECTOR_MAPPING_EXPECTED:
        assert mapping[name] == (kind, producer)
    assert mapping["evidence_manifest"] is None


@pytest.mark.parametrize(
    "collector_name,kind,producer", COLLECTOR_MAPPING_EXPECTED
)
def test_collector_mapping_behavior_present(
    collector_name, kind, producer
):
    subject_digest = _digest("c")
    if collector_name in BASE_COLLECTORS:
        entries = _base_entries(subject_digest)
        value = _base_collector_case(entries)
    else:
        entries = _base_entries(subject_digest) + (
            _entry(
                subject_digest,
                "ev-4",
                kind,
                producer,
                fresh_until=FIXED_TIME,
            ),
        )
        value = _collector_input(collector_name, entries)
    result = PolicyGate.evaluate(value)
    if collector_name == "provider_boundary_attestation":
        assert result.decision.outcome == "BLOCKED"
        assert result.decision.reason_codes == (
            "PROVIDER_BOUNDARY_CROSSING",
        )
    else:
        assert result.decision.outcome == "PASS"
        assert result.decision.reason_codes == ()


@pytest.mark.parametrize(
    "collector_name,kind,producer", COLLECTOR_MAPPING_EXPECTED
)
def test_collector_missing(collector_name, kind, producer):
    subject_digest = _digest("c")
    if collector_name in BASE_COLLECTORS:
        entries = tuple(
            entry
            for entry in _base_entries(subject_digest)
            if not (entry.kind == kind and entry.producer == producer)
        )
        value = _base_collector_case(entries)
    else:
        entries = _base_entries(subject_digest)
        value = _collector_input(collector_name, entries)
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert "REQUIRED_COLLECTOR_MISSING" in result.decision.reason_codes


@pytest.mark.parametrize(
    "collector_name,kind,producer", COLLECTOR_MAPPING_EXPECTED
)
def test_collector_not_success(collector_name, kind, producer):
    subject_digest = _digest("c")
    if collector_name in BASE_COLLECTORS:
        entries = tuple(
            _entry(
                subject_digest,
                "ev-1" if collector_name == "git_snapshot" else (
                    "ev-2" if collector_name == "task_policy_adr" else "ev-3"
                ),
                    kind,
                    producer,
                    status="failure",
                    fresh_until=FIXED_TIME,
                )
            if entry.evidence_id == (
                "ev-1" if collector_name == "git_snapshot" else (
                    "ev-2" if collector_name == "task_policy_adr" else "ev-3"
                )
            )
            else entry
            for entry in _base_entries(subject_digest)
        )
        value = _base_collector_case(entries)
    else:
        entries = _base_entries(subject_digest) + (
            _entry(
                subject_digest,
                "ev-4",
                    kind,
                    producer,
                    status="failure",
                    fresh_until=FIXED_TIME,
                ),
            )
        value = _collector_input(collector_name, entries)
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert "REQUIRED_COLLECTOR_NOT_SUCCESS" in result.decision.reason_codes


@pytest.mark.parametrize(
    "collector_name,kind,producer", COLLECTOR_MAPPING_EXPECTED
)
def test_collector_not_fresh_unknown(collector_name, kind, producer):
    subject_digest = _digest("c")
    if collector_name in BASE_COLLECTORS:
        entries = tuple(
            entry.model_copy(
                update={"fresh_until": None}
            )
            if entry.evidence_id == (
                "ev-1" if collector_name == "git_snapshot" else (
                    "ev-2" if collector_name == "task_policy_adr" else "ev-3"
                )
            )
            else entry
            for entry in _base_entries(subject_digest)
        )
        manifest = _manifest(subject_digest, entries=entries)
        risk_result = _risk_result(subject_digest, manifest=manifest)
        value = _input(
            subject=_subject(subject_digest),
            risk_result=risk_result,
            execution_receipts=(
                _receipt(
                    subject_digest,
                    steps=(
                        _step(0, "intent"),
                        _step(1, "architecture"),
                        _step(2, "operability"),
                    ),
                ),
            ),
            human_decisions=(_decision(subject_digest),),
        )
    else:
        entries = _base_entries(subject_digest) + (
            _entry(
                subject_digest,
                "ev-4",
                kind,
                producer,
            ),
        )
        value = _collector_input(collector_name, entries)
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert "REQUIRED_COLLECTOR_NOT_FRESH" in result.decision.reason_codes


def test_collector_multiple_history_entries():
    subject_digest = _digest("c")
    later_entries = _base_entries(
        subject_digest, fresh_until=LATER_TIME
    )
    fresh_then_failure = _base_entries(
        subject_digest, fresh_until=LATER_TIME
    ) + (
        _entry(
            subject_digest,
            "ev-4",
            "git_snapshot",
            "collector.git",
            status="failure",
            fresh_until=LATER_TIME,
        ),
    )
    value = _base_collector_case(
        fresh_then_failure, evaluated_at=LATER_TIME
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS"
    assert result.decision.reason_codes == ()

    fresh_then_expired = later_entries + (
        _entry(
            subject_digest,
            "ev-4",
            "git_snapshot",
            "collector.git",
            fresh_until=FIXED_TIME,
        ),
    )
    value = _base_collector_case(fresh_then_expired, evaluated_at=LATER_TIME)
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("EVIDENCE_EXPIRED",)
    assert "REQUIRED_COLLECTOR_NOT_FRESH" not in result.decision.reason_codes

    fresh_then_unknown = later_entries + (
        _entry(
            subject_digest,
            "ev-4",
            "git_snapshot",
            "collector.git",
        ),
    )
    manifest = _manifest(subject_digest, entries=fresh_then_unknown)
    risk_result = _risk_result(subject_digest, manifest=manifest)
    value = _input(
        subject=_subject(subject_digest),
        risk_result=risk_result,
        execution_receipts=(
            _receipt(
                subject_digest,
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                ),
            ),
        ),
        human_decisions=(_decision(subject_digest),),
        evaluated_at=LATER_TIME,
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == (
        "MANIFEST_HAS_GAPS",
        "EVIDENCE_FRESHNESS_UNKNOWN",
    )
    assert "REQUIRED_COLLECTOR_NOT_FRESH" not in result.decision.reason_codes

    all_expired = _base_entries(
        subject_digest, fresh_until=FIXED_TIME
    ) + (
        _entry(
            subject_digest,
            "ev-4",
            "git_snapshot",
            "collector.git",
            fresh_until=FIXED_TIME,
        ),
    )
    value = _base_collector_case(all_expired, evaluated_at=LATER_TIME)
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == (
        "EVIDENCE_EXPIRED",
        "REQUIRED_COLLECTOR_NOT_FRESH",
    )


def test_reviewer_missing_and_not_success():
    missing = _input(
        execution_receipts=(
            _receipt(steps=(_step(0, "architecture"),)),
        )
    )
    result = PolicyGate.evaluate(missing)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("REQUIRED_REVIEWER_MISSING",)

    for outcome_result, overall_result in (
        ("failure", "failure"),
        ("timeout", "failure"),
    ):
        not_success = _input(
            execution_receipts=(
                _receipt(
                    steps=(_step(0, "intent", result=outcome_result),),
                    overall_result=overall_result,
                ),
            )
        )
        result = PolicyGate.evaluate(not_success)
        assert result.decision.outcome == "BLOCKED"
        assert result.decision.reason_codes == (
            "REQUIRED_REVIEWER_NOT_SUCCESS",
        )


def test_reviewer_skipped_and_blocked_do_not_satisfy():
    skipped = _input(
        execution_receipts=(
            _receipt(
                steps=(_step(0, "intent", result="skipped"),),
                overall_result="partial",
            ),
        )
    )
    result = PolicyGate.evaluate(skipped)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("REQUIRED_REVIEWER_MISSING",)

    blocked = _input(
        execution_receipts=(
            _receipt(
                steps=(_step(0, "intent", result="blocked"),),
                overall_result="blocked",
            ),
        )
    )
    result = PolicyGate.evaluate(blocked)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("REQUIRED_REVIEWER_MISSING",)


def test_reviewer_valid_and_repaired_schema():
    repaired = _input(
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent", schema_status="repaired"),
                ),
            ),
        )
    )
    result = PolicyGate.evaluate(repaired)
    assert result.decision.outcome == "PASS"


def test_finding_evidence_ref_missing():
    value = _input(
        findings=(_finding(evidence_refs=("missing-ev",)),)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == (
        "FINDING_EVIDENCE_REF_MISSING",
    )


@pytest.mark.parametrize(
    ("basis", "severity", "status", "blocking"),
    [
        ("deterministic", "high", "open", True),
        ("deterministic", "high", "acknowledged", True),
        ("deterministic", "critical", "open", True),
        ("deterministic", "critical", "acknowledged", True),
        ("deterministic", "low", "open", False),
        ("deterministic", "medium", "open", False),
        ("deterministic", "high", "resolved", False),
        ("deterministic", "high", "dismissed", False),
        ("inferred", "high", "open", False),
    ],
)
def test_deterministic_finding_blocking(
    basis, severity, status, blocking
):
    value = _input(
        findings=(
            _finding(basis=basis, severity=severity, status=status),
        )
    )
    result = PolicyGate.evaluate(value)
    if blocking:
        assert result.decision.outcome == "BLOCKED"
        assert result.decision.reason_codes == (
            "DETERMINISTIC_FINDING_BLOCKING",
        )
    else:
        assert result.decision.outcome == "PASS"
        assert result.decision.reason_codes == ()


def test_high_risk_human_approve_passes():
    result = PolicyGate.evaluate(_high_input())
    assert result.decision.outcome == "PASS"
    assert result.decision.reason_codes == ()
    assert result.decision.waiver_ref is None


def test_high_risk_human_reject_blocks():
    value = _high_input(
        human_decisions=(_decision(decision="reject"),)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_REJECTED",)


def test_high_risk_no_human_missing():
    value = _high_input(human_decisions=())
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_MISSING",)
    assert result.decision.required_human_role == "change_owner"


def test_high_risk_role_mismatch_is_missing():
    value = _high_input(
        human_decisions=(_decision(owner_role="other_role"),)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_MISSING",)


def test_low_medium_unrelated_human_decision_ignored():
    value = _input(human_decisions=(_decision(),))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS"


def test_high_risk_waiver_valid():
    decision = _decision(
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    value = _late_high_input(human_decisions=(decision,))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS_WITH_WAIVER"
    assert result.decision.reason_codes == ()
    assert result.decision.waiver_ref == "waiver-1"


def test_high_risk_waiver_expired_at_equality():
    decision = _decision(
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=LATER_TIME,
        decided_at=FIXED_TIME,
    )
    value = _late_high_input(human_decisions=(decision,))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == (
        "REQUIRED_HUMAN_WAIVER_EXPIRED",
    )
    assert result.decision.waiver_ref is None


def test_human_latest_choice_wins():
    older_reject = _decision(
        decision="reject",
        decided_at=FIXED_TIME,
    )
    newer_approve = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve",
        decided_at=LATER_TIME,
    )
    value = _late_high_input(
        human_decisions=(older_reject, newer_approve)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS"


def test_human_same_time_conflict():
    approve = _decision()
    waiver = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    value = _high_input(human_decisions=(approve, waiver))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_CONFLICT",)


def test_human_conflict_with_reject_and_waiver():
    reject = _decision(decision="reject")
    waiver = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    value = _high_input(human_decisions=(reject, waiver))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_CONFLICT",)


def test_human_conflict_with_reject_and_approve():
    reject = _decision(decision="reject")
    approve = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve",
    )
    value = _high_input(human_decisions=(reject, approve))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_CONFLICT",)


def test_human_conflict_and_expiry_preserve_order():
    approve = _decision(decided_at=EARLIER_TIME)
    expired_waiver = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=FIXED_TIME,
        decided_at=EARLIER_TIME,
    )
    value = _late_high_input(
        human_decisions=(approve, expired_waiver)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "NEEDS_HUMAN"
    assert result.decision.reason_codes == (
        "REQUIRED_HUMAN_WAIVER_EXPIRED",
        "REQUIRED_HUMAN_CONFLICT",
    )


def test_human_identical_decisions_are_not_conflict():
    first = _decision()
    second = _decision(
        decision_id="decision-2",
        owner="owner-b",
    )
    value = _high_input(human_decisions=(first, second))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS"

    first_waiver = _decision(
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    second_waiver = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    value = _late_high_input(
        human_decisions=(first_waiver, second_waiver)
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "PASS_WITH_WAIVER"
    assert result.decision.waiver_ref == "waiver-1"

    first_reject = _decision(decision="reject")
    second_reject = _decision(
        decision_id="decision-2",
        owner="owner-b",
        decision="reject",
    )
    value = _high_input(human_decisions=(first_reject, second_reject))
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("REQUIRED_HUMAN_REJECTED",)


def test_waiver_cannot_bypass_blocked_facts():
    waiver = _decision(
        decision="approve_with_waiver",
        waiver_id="waiver-1",
        expires_at=AFTER_LATER_TIME,
        decided_at=FIXED_TIME,
    )
    crossing_entries = _base_entries() + (
        _entry(
            _digest("c"),
            "ev-4",
            "provider_boundary_attestation",
            "collector.provider_boundary_attestation",
            fresh_until=FIXED_TIME,
        ),
    )
    value = _input(
        risk_result=_risk_result(
            manifest=_manifest(entries=crossing_entries),
            declarations=_declarations(
                provider_boundary="crosses_declared_boundary"
            ),
        ),
        execution_receipts=(
            _receipt(
                steps=(
                    _step(0, "intent"),
                    _step(1, "architecture"),
                    _step(2, "operability"),
                )
            ),
        ),
        human_decisions=(waiver,),
    )
    result = PolicyGate.evaluate(value)
    assert result.decision.outcome == "BLOCKED"
    assert result.decision.reason_codes == ("PROVIDER_BOUNDARY_CROSSING",)


def test_decision_bindings_and_refs():
    value = _input(
        findings=(
            _finding(finding_id="finding-b"),
            _finding(
                finding_id="finding-a",
                evidence_refs=("ev-2",),
            ),
        ),
        execution_receipts=(
            _receipt(receipt_id="receipt-b"),
            _receipt(receipt_id="receipt-a", run_id="run-a"),
        ),
        human_decisions=(_decision(),),
    )
    result = PolicyGate.evaluate(value)
    decision = result.decision
    assert decision.subject_digest == value.subject.subject_digest
    assert decision.policy_version == value.subject.policy_version
    assert decision.rules_digest == policy_module._RULES_DIGEST
    assert (
        decision.required_collectors
        == value.risk_result.classification.required_collectors
    )
    assert (
        decision.required_reviewers
        == value.risk_result.classification.required_reviewers
    )
    assert (
        decision.required_human_role
        == value.risk_result.classification.required_human_role
    )
    assert decision.evaluated_evidence_refs == (
        "ev-1",
        "ev-2",
        "ev-3",
    )
    assert decision.evaluated_finding_refs == (
        "finding-a",
        "finding-b",
    )
    assert decision.evaluated_receipt_refs == (
        "receipt-a",
        "receipt-b",
    )
    assert decision.waiver_ref is None
    assert decision.evaluated_at == value.evaluated_at


def test_permutation_order_never_changes_decision():
    subject_digest = _digest("c")
    risk_result = _risk_result(
        subject_digest,
        snapshot=_git_snapshot(subject_digest, changed_files_total=21),
    )
    findings = (
        _finding(finding_id="finding-1"),
        _finding(finding_id="finding-2"),
    )
    receipts = (
        _receipt(
            receipt_id="receipt-1",
            steps=(
                _step(0, "intent"),
                _step(1, "architecture"),
                _step(2, "operability"),
            ),
        ),
        _receipt(
            receipt_id="receipt-2",
            run_id="run-2",
            steps=(_step(0, "intent"),),
        ),
    )
    decisions = (
        _decision(),
        _decision(decision_id="decision-2", owner="owner-b"),
    )
    ordered = _input(
        subject=_subject(subject_digest),
        risk_result=risk_result,
        findings=findings,
        execution_receipts=receipts,
        human_decisions=decisions,
    )
    reversed_value = _input(
        subject=_subject(subject_digest),
        risk_result=risk_result,
        findings=tuple(reversed(findings)),
        execution_receipts=tuple(reversed(receipts)),
        human_decisions=tuple(reversed(decisions)),
    )
    assert (
        PolicyGate.evaluate(ordered).decision
        == PolicyGate.evaluate(reversed_value).decision
    )


def test_rules_table_immutable_and_digest_independently_recomputed():
    assert policy_module._RULES_DIGEST == _rules_digest()
    assert policy_module._RULES_DIGEST.startswith("sha256:")
    assert len(policy_module._RULES_DIGEST) == 7 + 64
    assert (
        policy_module._RULES_TABLE["collector_mapping"]
        is policy_module._COLLECTOR_MAPPING
    )
    assert (
        policy_module._RULES_TABLE["reason_order"]
        is policy_module._REASON_ORDER
    )
    with pytest.raises(TypeError):
        policy_module._RULES_TABLE["rules_version"] = "gate.v1"
    assert policy_module._RULES_TABLE["rules_version"] == "gate.v0"
    with pytest.raises(TypeError):
        policy_module._COLLECTOR_MAPPING["git_snapshot"] = ("x", "y")
    with pytest.raises(TypeError):
        policy_module._COLLECTOR_MAPPING["git_snapshot"][0] = "x"
    with pytest.raises(AttributeError):
        policy_module._RULES_TABLE["reviewer_schema_statuses"].add("invalid")


def test_collector_mapping_is_single_immutable_rule_authority():
    value = _input()
    digest_before = policy_module._RULES_DIGEST
    before = PolicyGate.evaluate(value)
    mutated = False
    try:
        with pytest.raises(TypeError):
            policy_module._COLLECTOR_MAPPING["git_snapshot"] = (
                "other",
                "other",
            )
            mutated = True
    finally:
        if mutated:
            policy_module._COLLECTOR_MAPPING["git_snapshot"] = (
                "git_snapshot",
                "collector.git",
            )
    after = PolicyGate.evaluate(value)
    assert after == before
    assert after.decision.rules_digest == digest_before
    assert policy_module._RULES_DIGEST == digest_before


def test_decision_id_independently_recomputed():
    result = PolicyGate.evaluate(_input())
    data = result.decision.model_dump(mode="json")
    assert result.decision.decision_id == _decision_id_for(data)
    assert result.decision.rules_digest == _rules_digest()


@pytest.mark.parametrize(
    ("field", "mutator"),
    [
        (
            "decision_id",
            lambda data: data.update(decision_id="policy_" + "0" * 32),
        ),
        (
            "subject_digest",
            lambda data: data.update(subject_digest=_digest("e")),
        ),
        (
            "policy_version",
            lambda data: data.update(policy_version="other-version"),
        ),
        (
            "rules_digest",
            lambda data: data.update(rules_digest=_digest("e")),
        ),
        (
            "outcome",
            lambda data: data.update(
                outcome="NEEDS_HUMAN",
                reason_codes=("REQUIRED_HUMAN_MISSING",),
                required_human_role="change_owner",
            ),
        ),
        (
            "reason_codes",
            lambda data: data.update(
                reason_codes=("AUTHORIZATION_CHANGE",)
            ),
        ),
        (
            "required_collectors",
            lambda data: data.update(required_collectors=("git_snapshot",)),
        ),
        (
            "required_reviewers",
            lambda data: data.update(required_reviewers=("architecture",)),
        ),
        (
            "required_human_role",
            lambda data: data.update(required_human_role="change_owner"),
        ),
        (
            "evaluated_evidence_refs",
            lambda data: data.update(evaluated_evidence_refs=("ev-9",)),
        ),
        (
            "evaluated_finding_refs",
            lambda data: data.update(evaluated_finding_refs=("finding-x",)),
        ),
        (
            "evaluated_receipt_refs",
            lambda data: data.update(evaluated_receipt_refs=("receipt-x",)),
        ),
        (
            "waiver_ref",
            lambda data: data.update(
                outcome="PASS_WITH_WAIVER",
                waiver_ref="waiver-x",
            ),
        ),
        (
            "evaluated_at",
            lambda data: data.update(evaluated_at=LATER_TIME),
        ),
    ],
)
def test_result_forgery_rejection_every_decision_field(field, mutator):
    value = _input()
    decision = PolicyGate.evaluate(value).decision
    data = decision.model_dump()
    mutator(data)
    forged = PolicyDecision.model_validate(data)
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate(
            {"input": value, "decision": forged}
        )


def test_result_rejects_synchronized_id_forgery():
    value = _input()
    decision = PolicyGate.evaluate(value).decision
    data = decision.model_dump(mode="json")
    data["subject_digest"] = _digest("e")
    data["outcome"] = "BLOCKED"
    data["reason_codes"] = ("REQUIRED_HUMAN_MISSING",)
    data["required_human_role"] = "change_owner"
    data["evaluated_evidence_refs"] = ("ev-9",)
    data["decision_id"] = _decision_id_for(data)
    forged = PolicyDecision.model_validate(data)
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate(
            {"input": value, "decision": forged}
        )


def test_result_rejects_stale_id_forgery():
    value = _input()
    decision = PolicyGate.evaluate(value).decision
    forged = PolicyDecision.model_construct(
        schema_version="v1",
        decision_id=decision.decision_id,
        subject_digest=_digest("e"),
        policy_version=decision.policy_version,
        rules_digest=decision.rules_digest,
        outcome="PASS",
        reason_codes=(),
        required_collectors=decision.required_collectors,
        required_reviewers=decision.required_reviewers,
        required_human_role=decision.required_human_role,
        evaluated_evidence_refs=decision.evaluated_evidence_refs,
        evaluated_finding_refs=decision.evaluated_finding_refs,
        evaluated_receipt_refs=decision.evaluated_receipt_refs,
        waiver_ref=None,
        evaluated_at=decision.evaluated_at,
    )
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate(
            {"input": value, "decision": forged}
        )


def test_result_no_dict_json_revalidation_path():
    value = _input()
    result = PolicyGate.evaluate(value)
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate(result.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        PolicyGateResult.model_validate_json(result.model_dump_json())


def test_source_ast_audit_pure_policy_module():
    source = inspect.getsource(policy_module)
    tree = ast.parse(source)

    banned_modules = {
        "os",
        "pathlib",
        "sqlite3",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "shutil",
        "io",
        "pickle",
        "shelve",
        "dbm",
        "tempfile",
        "requests",
        "httpx",
        "git",
        "asyncio",
        "threading",
        "multiprocessing",
        "sys",
        "datetime",
        "time",
        "random",
        "environment",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_modules)

    banned_calls = {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "print",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "locals",
        "globals",
        "vars",
        "execfile",
    }
    banned_attributes = {
        "read",
        "write",
        "readline",
        "readlines",
        "writelines",
        "flush",
        "seek",
        "close",
        "connect",
        "send",
        "recv",
        "request",
        "urlopen",
        "execute",
        "commit",
        "rollback",
        "Popen",
        "run",
    }
    banned_names = {
        "Collector",
        "path",
        "file",
        "url",
        "http",
        "network",
        "sqlite",
        "storage",
        "shell",
        "subprocess",
        "eval",
        "exec",
        "open",
        "git",
        "io",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned_calls
        ):
            raise AssertionError(f"banned call {node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr in banned_attributes:
            raise AssertionError(f"banned attribute {node.attr}")
        if isinstance(node, ast.Name) and node.id in banned_names:
            raise AssertionError(f"banned name {node.id}")

    class_names = [
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    ]
    assert class_names == [
        "PolicyEvaluationInput",
        "PolicyGateResult",
        "PolicyGate",
    ]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = [
                base.id for base in node.bases if isinstance(base, ast.Name)
            ]
            if node.name == "PolicyGate":
                assert base_names == []
            else:
                assert base_names == ["BaseModel"]

    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.List, ast.Dict, ast.Set)
        ):
            raise AssertionError("mutable module-level assignment")
