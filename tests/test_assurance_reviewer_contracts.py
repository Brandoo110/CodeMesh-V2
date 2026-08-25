"""V2-P4-01 Reviewer Base Contract focused tests."""

import ast
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import assurance
import assurance.reviewer_contracts as rc_module
import assurance.single_reviewer as single_module
from assurance import (
    ChangeSubject,
    Finding,
    FindingOutput,
    ReviewerEvidenceContext,
    ReviewerFailureOutcome,
    ReviewerInput,
    ReviewQuestion,
    RiskClassificationResult,
)
from assurance.contracts import Finding as FindingModel
from assurance.intake import IntakeSnapshot
from assurance.manifest import EvidenceManifest, EvidenceManifestEntry
from assurance.risk import (
    RiskClassificationInput,
    RiskClassifier,
    RiskDeclarations,
)
from assurance.snapshot import GitChange, GitSnapshot


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
EARLIER_TIME = datetime(2026, 8, 25, 7, 0, 0, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)

PUBLIC_API_NAMES = frozenset(
    {"ReviewerInput", "ReviewerFailureOutcome", "FindingOutput"}
)

PRIOR_PUBLIC_NAMES = frozenset(
    {
        "AcceptanceCase",
        "ChangeSubject",
        "Evidence",
        "ExecutionReceipt",
        "ExecutionStep",
        "Finding",
        "HumanDecision",
        "PolicyDecision",
        "ArtifactStore",
        "SQLiteAssuranceStore",
        "GitChange",
        "GitSnapshot",
        "IntakeSnapshot",
        "EvidenceManifestEntry",
        "EvidenceManifest",
        "RiskClassificationInput",
        "RiskClassification",
        "RiskClassificationResult",
        "RiskClassifier",
        "ReviewerEvidenceContext",
        "ReviewQuestion",
        "SingleReviewerInput",
        "SingleStrongReviewer",
    }
)


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


def _context(
    evidence_id,
    kind,
    content,
    *,
    artifact_digest=None,
    truncated=False,
    redaction_status="not_applicable",
):
    if artifact_digest is None:
        artifact_digest = _sha256(content.encode("utf-8"))
    return ReviewerEvidenceContext.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": evidence_id,
            "kind": kind,
            "artifact_digest": artifact_digest,
            "content": content,
            "content_digest": _sha256(content.encode("utf-8")),
            "truncated": truncated,
            "redaction_status": redaction_status,
        }
    )


def _default_contexts():
    return (
        _context("ev-a", "git_snapshot", "git snapshot evidence for review"),
        _context(
            "ev-b", "intake_documents", "intake documents evidence for review"
        ),
        _context(
            "ev-c", "command_batch", "command batch evidence for review"
        ),
    )


def _manifest(
    subject_digest=None,
    contexts=None,
    *,
    evaluated_at=FIXED_TIME,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    if contexts is None:
        contexts = _default_contexts()
    entries = []
    for index, item in enumerate(contexts):
        entries.append(
            EvidenceManifestEntry.model_validate(
                {
                    "schema_version": "v1",
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "trust_level": "observed",
                    "producer": f"collector.{item.kind}",
                    "subject_digest": subject_digest,
                    "artifact_digest": item.artifact_digest,
                    "source_ref": f"collector.{item.kind}:{index}",
                    "status": "success",
                    "collected_at": FIXED_TIME,
                    "fresh_until": LATER_TIME,
                    "freshness": (
                        "fresh" if evaluated_at <= LATER_TIME else "stale"
                    ),
                    "redaction_status": item.redaction_status,
                }
            )
        )
    entries = tuple(sorted(entries, key=lambda entry: entry.evidence_id))
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
    body = {
        key: value
        for key, value in values.items()
        if key not in ("manifest_id", "canonical_digest", "artifact_digest")
    }
    digest = _sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    values["canonical_digest"] = digest
    values["artifact_digest"] = digest
    values["manifest_id"] = "em_" + hashlib.sha256(
        (subject_digest + digest).encode("utf-8")
    ).hexdigest()[:32]
    return EvidenceManifest.model_validate(values)


def _risk_input(subject_digest=None, *, manifest=None):
    if subject_digest is None:
        subject_digest = _digest("c")
    if manifest is None:
        manifest = _manifest(subject_digest)
    return RiskClassificationInput.model_validate(
        {
            "schema_version": "v1",
            "snapshot": _git_snapshot(subject_digest),
            "intake": _intake_snapshot(subject_digest),
            "manifest": manifest,
            "declarations": _declarations(),
        }
    )


def _risk_result(subject_digest=None, *, manifest=None):
    return RiskClassifier.classify(
        _risk_input(subject_digest, manifest=manifest)
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
        "policy_version": "v2-p4",
        "created_at": FIXED_TIME,
    }
    values.update(overrides)
    return ChangeSubject.model_validate(values)


def _reviewer_input_values(
    *,
    subject=None,
    risk_result=None,
    contexts=None,
    **overrides,
):
    if subject is None:
        subject = _subject()
    if contexts is None:
        contexts = _default_contexts()
    if risk_result is None:
        manifest = _manifest(subject.subject_digest, contexts)
        risk_result = _risk_result(subject.subject_digest, manifest=manifest)
    values = {
        "schema_version": "v1",
        "reviewer_role": "architecture",
        "subject": subject,
        "risk_result": risk_result,
        "contexts": contexts,
        "rubric_version": "reviewer_general.v0",
        "rubric_hash": _digest("e"),
        "evidence_allowlist": tuple(
            item.evidence_id for item in contexts
        ),
        "tool_allowlist": (),
        "timeout_seconds": 120,
        "token_budget": 100_000,
        "cost_budget_usd": 2.5,
        "requested_at": FIXED_TIME,
    }
    values.update(overrides)
    return values


def _reviewer_input(**overrides):
    return ReviewerInput.model_validate(
        _reviewer_input_values(**overrides)
    )


def _finding(**overrides):
    values = {
        "schema_version": "v1",
        "finding_id": "fnd-a",
        "subject_digest": _digest("c"),
        "reviewer_role": "architecture",
        "claim": "boundary direction must be explicit",
        "evidence_refs": ("ev-a",),
        "basis": "inferred",
        "severity": "medium",
        "confidence": 0.8,
        "rubric_hash": _digest("e"),
        "model_ref": "model/strong-1",
        "status": "open",
    }
    values.update(overrides)
    return FindingModel.model_validate(values)


def _question(**overrides):
    values = {
        "schema_version": "v1",
        "subject_digest": _digest("c"),
        "reviewer_role": "architecture",
        "question": "what does acceptance coverage include?",
        "reason": "model_question",
        "evidence_refs": ("ev-a",),
        "rubric_hash": _digest("e"),
        "model_ref": "model/strong-1",
        "status": "open",
    }
    values.update(overrides)
    body = {
        key: value
        for key, value in values.items()
        if key != "question_id"
    }
    question_id = "rq_" + hashlib.sha256(
        single_module._canonical_bytes(body)
    ).hexdigest()[:32]
    return ReviewQuestion.model_validate(
        {**values, "question_id": question_id}
    )


def _failure(code, details="reviewer execution failed"):
    return ReviewerFailureOutcome.model_validate(
        {
            "schema_version": "v1",
            "code": code,
            "details": details,
        }
    )


def _finding_output_values(
    *,
    reviewer_input=None,
    outcome="success",
    findings=(),
    questions=(),
    failure=None,
    completed_at=LATER_TIME,
    **overrides,
):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    values = {
        "schema_version": "v1",
        "input": reviewer_input,
        "outcome": outcome,
        "findings": findings,
        "questions": questions,
        "failure": failure,
        "completed_at": completed_at,
    }
    values.update(overrides)
    return values


def _finding_output(**overrides):
    return FindingOutput.model_validate(
        _finding_output_values(**overrides)
    )


def _assert_immutable_graph(value, seen=None):
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            _assert_immutable_graph(getattr(value, name), seen)
    elif isinstance(value, tuple):
        for item in value:
            _assert_immutable_graph(item, seen)
    elif isinstance(value, (list, dict, set)):
        raise AssertionError(
            "mutable container found: " + type(value).__name__
        )


def test_reviewer_contracts_public_api_importable():
    assert assurance.ReviewerInput is ReviewerInput
    assert assurance.ReviewerFailureOutcome is ReviewerFailureOutcome
    assert assurance.FindingOutput is FindingOutput
    assert rc_module.ReviewerInput is ReviewerInput
    assert rc_module.ReviewerFailureOutcome is ReviewerFailureOutcome
    assert rc_module.FindingOutput is FindingOutput


def test_package_exports_include_atomic_and_prior_subset():
    current_public_names = set(assurance.__all__)
    assert PRIOR_PUBLIC_NAMES <= current_public_names
    assert PUBLIC_API_NAMES <= current_public_names
    for name in PUBLIC_API_NAMES:
        assert hasattr(assurance, name)
    for name in PRIOR_PUBLIC_NAMES:
        assert hasattr(assurance, name)


def test_module_ast_has_no_forbidden_imports_or_calls():
    source = Path(rc_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {
        "os",
        "pathlib",
        "subprocess",
        "socket",
        "httpx",
        "requests",
        "openai",
        "sqlite",
        "time",
        "random",
        "urllib",
        "http",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_modules
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_modules
        elif isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            assert name not in {"eval", "exec", "open", "read"}
    assert "environ" not in source
    assert "subprocess" not in source
    assert "getcwd" not in source


def test_reviewer_input_field_order_and_schema_literal():
    assert list(ReviewerInput.model_fields) == [
        "schema_version",
        "reviewer_role",
        "subject",
        "risk_result",
        "contexts",
        "rubric_version",
        "rubric_hash",
        "evidence_allowlist",
        "tool_allowlist",
        "timeout_seconds",
        "token_budget",
        "cost_budget_usd",
        "requested_at",
    ]
    model = _reviewer_input()
    assert model.schema_version == "v1"
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(schema_version="v2")
        )


def test_failure_outcome_field_order_and_schema_literal():
    assert list(ReviewerFailureOutcome.model_fields) == [
        "schema_version",
        "code",
        "details",
    ]
    model = _failure("timeout")
    assert model.schema_version == "v1"
    with pytest.raises(ValidationError):
        ReviewerFailureOutcome.model_validate(
            {
                "schema_version": "v2",
                "code": "timeout",
                "details": "x",
            }
        )
    with pytest.raises(ValidationError):
        ReviewerFailureOutcome.model_validate(
            {
                "schema_version": "v1",
                "code": "execution_error",
                "details": "x",
            }
        )


def test_finding_output_field_order_and_schema_literal():
    assert list(FindingOutput.model_fields) == [
        "schema_version",
        "input",
        "outcome",
        "findings",
        "questions",
        "failure",
        "completed_at",
    ]
    model = _finding_output()
    assert model.schema_version == "v1"
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(
            _finding_output_values(schema_version="v2")
        )


def test_all_models_frozen_extra_forbid_assignment_and_unknown_fields():
    for model_type in (
        ReviewerInput,
        ReviewerFailureOutcome,
        FindingOutput,
    ):
        assert model_type.model_config["frozen"] is True
        assert model_type.model_config["extra"] == "forbid"
    reviewer_input = _reviewer_input()
    failure = _failure("timeout")
    output = _finding_output()
    with pytest.raises(ValidationError):
        reviewer_input.reviewer_role = "intent"
    with pytest.raises(ValidationError):
        failure.code = "timeout"
    with pytest.raises(ValidationError):
        output.outcome = "timeout"
    values = _reviewer_input_values()
    values["unexpected"] = True
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)
    with pytest.raises(ValidationError):
        ReviewerFailureOutcome.model_validate(
            {
                "schema_version": "v1",
                "code": "timeout",
                "details": "x",
                "unexpected": True,
            }
        )
    values = _finding_output_values()
    values["unexpected"] = True
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)


def test_models_are_deep_immutable():
    _assert_immutable_graph(_reviewer_input())
    _assert_immutable_graph(_failure("timeout"))
    _assert_immutable_graph(
        _finding_output(
            findings=(_finding(),),
            questions=(_question(),),
        )
    )


def test_models_have_no_forbidden_decision_fields():
    forbidden = ("gold", "pass", "gate", "receipt", "council", "verdict")
    for model_type in (ReviewerInput, FindingOutput):
        for name in model_type.model_fields:
            assert not any(token in name.lower() for token in forbidden)
    for name in ReviewerInput.model_fields:
        assert name not in {
            "provider",
            "prompt",
            "model_ref",
            "run_id",
        }


def test_valid_reviewer_input_construction_and_bindings():
    model = _reviewer_input()
    assert model.reviewer_role == "architecture"
    assert model.subject.subject_digest == _digest("c")
    assert model.risk_result.classification.subject_digest == _digest("c")
    assert model.evidence_allowlist == (
        "ev-a",
        "ev-b",
        "ev-c",
    )
    assert model.tool_allowlist == ()
    assert model.timeout_seconds == 120
    assert model.token_budget == 100_000
    assert model.cost_budget_usd == 2.5
    assert model.requested_at == FIXED_TIME
    assert model.schema_version == "v1"


def test_reviewer_input_json_round_trip():
    model = _reviewer_input()
    rebuilt = ReviewerInput.model_validate_json(model.model_dump_json())
    assert rebuilt == model


def test_failure_outcome_json_round_trip():
    model = _failure("budget_exceeded", details="token budget exceeded")
    rebuilt = ReviewerFailureOutcome.model_validate_json(
        model.model_dump_json()
    )
    assert rebuilt == model


def test_valid_success_finding_output():
    q1 = _question(question="question alpha")
    q2 = _question(question="question zulu")
    if q1.question_id > q2.question_id:
        q1, q2 = q2, q1
    output = _finding_output(
        findings=(_finding(), _finding(finding_id="fnd-b", claim="second")),
        questions=(q1, q2),
    )
    assert output.outcome == "success"
    assert output.failure is None
    assert len(output.findings) == 2
    assert len(output.questions) == 2
    assert output.completed_at == LATER_TIME


def test_finding_output_json_round_trip_success_and_failure():
    success = _finding_output(
        findings=(_finding(),),
        questions=(_question(),),
    )
    rebuilt_success = FindingOutput.model_validate_json(
        success.model_dump_json()
    )
    assert rebuilt_success == success

    failure = _finding_output(
        outcome="timeout",
        failure=_failure("timeout"),
    )
    rebuilt_failure = FindingOutput.model_validate_json(
        failure.model_dump_json()
    )
    assert rebuilt_failure == failure
    assert rebuilt_failure.failure.code == "timeout"


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("failure", "execution_failed"),
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("budget_exceeded", "budget_exceeded"),
        ("schema_invalid", "schema_invalid"),
    ],
)
def test_each_non_success_outcome_is_valid(outcome, code):
    output = _finding_output(
        outcome=outcome,
        failure=_failure(code),
    )
    assert output.outcome == outcome
    assert output.failure.code == code
    assert output.findings == ()
    assert output.questions == ()


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("failure", "execution_failed"),
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("budget_exceeded", "budget_exceeded"),
        ("schema_invalid", "schema_invalid"),
    ],
)
def test_outcome_failure_code_mismatch_rejected(outcome, code):
    wrong_code = "timeout" if code != "timeout" else "execution_failed"
    with pytest.raises(ValidationError):
        _finding_output(
            outcome=outcome,
            failure=_failure(wrong_code),
        )


def test_success_must_not_carry_failure():
    with pytest.raises(ValidationError):
        _finding_output(
            outcome="success",
            failure=_failure("execution_failed"),
        )


def test_non_success_requires_failure():
    for outcome in (
        "failure",
        "timeout",
        "cancelled",
        "budget_exceeded",
        "schema_invalid",
    ):
        with pytest.raises(ValidationError):
            _finding_output(outcome=outcome, failure=None)


def test_non_success_must_not_carry_findings_or_questions():
    with pytest.raises(ValidationError):
        _finding_output(
            outcome="failure",
            failure=_failure("execution_failed"),
            findings=(_finding(),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            outcome="failure",
            failure=_failure("execution_failed"),
            questions=(_question(),),
        )


def test_python_collections_require_exact_tuples_and_json_arrays_work():
    values = _reviewer_input_values()
    values["contexts"] = list(values["contexts"])
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)
    values = _reviewer_input_values()
    values["evidence_allowlist"] = list(values["evidence_allowlist"])
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)
    values = _reviewer_input_values()
    values["tool_allowlist"] = list(values["tool_allowlist"])
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)

    values = _finding_output_values(
        findings=(_finding(),),
        questions=(_question(),),
    )
    values["findings"] = list(values["findings"])
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)
    values = _finding_output_values(
        findings=(_finding(),),
        questions=(_question(),),
    )
    values["questions"] = list(values["questions"])
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)

    reviewer_input = _reviewer_input()
    json_values = json.loads(reviewer_input.model_dump_json())
    assert type(json_values["contexts"]) is list
    assert type(json_values["evidence_allowlist"]) is list
    assert type(json_values["tool_allowlist"]) is list
    rebuilt = ReviewerInput.model_validate_json(
        json.dumps(json_values)
    )
    assert rebuilt == reviewer_input


def test_python_nested_models_require_exact_types():
    values = _reviewer_input_values()
    values["subject"] = values["subject"].model_dump()
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)
    values = _reviewer_input_values()
    values["risk_result"] = values["risk_result"].model_dump()
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)
    values = _reviewer_input_values()
    values["contexts"] = tuple(
        item.model_dump() for item in values["contexts"]
    )
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(values)

    values = _finding_output_values()
    values["input"] = values["input"].model_dump()
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)
    values = _finding_output_values(findings=(_finding(),))
    values["findings"] = tuple(
        item.model_dump() for item in values["findings"]
    )
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)
    values = _finding_output_values(questions=(_question(),))
    values["questions"] = tuple(
        item.model_dump() for item in values["questions"]
    )
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)
    values = _finding_output_values(failure=_failure("timeout"))
    values["failure"] = values["failure"].model_dump()
    with pytest.raises(ValidationError):
        FindingOutput.model_validate(values)


def test_subclass_instances_fail_closed_in_python_mode():
    class SubSubject(ChangeSubject):
        pass

    subject = SubSubject.model_validate(_subject().model_dump())
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(subject=subject)
        )

    valid_risk = _risk_result()

    class SubRiskResult(RiskClassificationResult):
        pass

    risk = SubRiskResult(
        schema_version="v1",
        input=valid_risk.input,
        classification=valid_risk.classification,
    )
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(risk_result=risk)
        )

    class SubFinding(Finding):
        pass

    finding = SubFinding.model_validate(_finding().model_dump())
    with pytest.raises(ValidationError):
        _finding_output(findings=(finding,))

    class SubQuestion(ReviewQuestion):
        pass

    question = SubQuestion.model_validate(_question().model_dump())
    with pytest.raises(ValidationError):
        _finding_output(questions=(question,))

    class SubFailure(ReviewerFailureOutcome):
        pass

    failure = SubFailure(
        schema_version="v1",
        code="timeout",
        details="x",
    )
    with pytest.raises(ValidationError):
        _finding_output(outcome="timeout", failure=failure)


def test_contexts_count_bounds():
    many_contexts = tuple(
        _context(f"ev-{i:02d}", "git_snapshot", f"content {i}")
        for i in range(16)
    )
    model = _reviewer_input(contexts=many_contexts)
    assert len(model.contexts) == 16
    with pytest.raises(ValidationError):
        _reviewer_input(
            contexts=many_contexts
            + (_context("ev-16", "git_snapshot", "content 16"),)
        )

    subject = _subject()
    ctxs = _default_contexts()
    manifest = _manifest(subject.subject_digest, ctxs)
    risk_result = _risk_result(subject.subject_digest, manifest=manifest)
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=(),
            )
        )


def test_contexts_must_be_sorted_and_unique():
    subject = _subject()
    ctxs = _default_contexts()
    manifest = _manifest(subject.subject_digest, ctxs)
    risk_result = _risk_result(subject.subject_digest, manifest=manifest)
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=(ctxs[1], ctxs[0], ctxs[2]),
            )
        )
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=(ctxs[0], ctxs[0], ctxs[1]),
            )
        )


def test_every_context_must_match_manifest_entry():
    subject = _subject()
    ctxs = _default_contexts()
    manifest = _manifest(subject.subject_digest, ctxs)
    risk_result = _risk_result(subject.subject_digest, manifest=manifest)

    unknown = _context("ev-zz", "git_snapshot", "unknown evidence")
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=ctxs + (unknown,),
            )
        )

    kind_mismatch = _context(
        "ev-a",
        "intake_documents",
        "git snapshot evidence for review",
    )
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=(kind_mismatch, ctxs[1], ctxs[2]),
            )
        )

    artifact_mismatch = _context(
        "ev-a",
        "git_snapshot",
        "git snapshot evidence for review",
        artifact_digest=_digest("f"),
    )
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=(artifact_mismatch, ctxs[1], ctxs[2]),
            )
        )


def test_subject_digest_must_equal_risk_input_and_classification():
    subject = _subject(_digest("d"))
    risk_result = _risk_result(_digest("c"))
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
            )
        )


def test_requested_at_ordering_bindings():
    subject = _subject(created_at=LATER_TIME)
    with pytest.raises(ValidationError):
        _reviewer_input(subject=subject, requested_at=FIXED_TIME)

    subject = _subject()
    ctxs = _default_contexts()
    manifest = _manifest(
        subject.subject_digest,
        ctxs,
        evaluated_at=LATER_TIME,
    )
    risk_result = _risk_result(subject.subject_digest, manifest=manifest)
    with pytest.raises(ValidationError):
        ReviewerInput.model_validate(
            _reviewer_input_values(
                subject=subject,
                risk_result=risk_result,
                contexts=ctxs,
                requested_at=FIXED_TIME,
            )
        )


def test_evidence_allowlist_must_exactly_match_context_ids():
    for bad in (
        ("ev-a", "ev-b"),
        ("ev-a", "ev-b", "ev-c", "ev-d"),
        ("ev-c", "ev-a", "ev-b"),
        ("ev-a", "ev-a", "ev-b"),
    ):
        with pytest.raises(ValidationError):
            _reviewer_input(evidence_allowlist=bad)


def test_allowlist_item_text_rules():
    for bad in (
        ("ev-a\x00b",),
        (" ",),
        ("x" * 257,),
        ("ev-a", 1),
    ):
        with pytest.raises(ValidationError):
            _reviewer_input(evidence_allowlist=bad)
        with pytest.raises(ValidationError):
            _reviewer_input(tool_allowlist=bad)


def test_tool_allowlist_empty_sorted_unique_and_bounds():
    assert _reviewer_input(tool_allowlist=()).tool_allowlist == ()
    assert _reviewer_input(
        tool_allowlist=("a-tool", "b-tool")
    ).tool_allowlist == ("a-tool", "b-tool")
    with pytest.raises(ValidationError):
        _reviewer_input(tool_allowlist=("b-tool", "a-tool"))
    with pytest.raises(ValidationError):
        _reviewer_input(tool_allowlist=("a-tool", "a-tool"))
    too_many = tuple(f"tool-{i:02d}" for i in range(65))
    with pytest.raises(ValidationError):
        _reviewer_input(tool_allowlist=too_many)


def test_rubric_version_and_hash_strictness():
    assert _reviewer_input(rubric_version="x" * 128).rubric_version == (
        "x" * 128
    )
    for bad in (" ", "x" * 129, "a\x00b", 123):
        with pytest.raises(ValidationError):
            _reviewer_input(rubric_version=bad)
    for bad in (
        "sha256:" + "A" * 64,
        "sha256:" + "g" * 64,
        "sha256:" + "ab" * 31,
        "md5:" + "a" * 64,
        "",
    ):
        with pytest.raises(ValidationError):
            _reviewer_input(rubric_hash=bad)


def test_reviewer_role_literal():
    for role in ("intent", "architecture", "operability"):
        assert _reviewer_input(reviewer_role=role).reviewer_role == role
    with pytest.raises(ValidationError):
        _reviewer_input(reviewer_role="security")


def test_timeout_and_token_budget_int_strictness():
    for bad in (True, 1.5, Decimal("120"), "120"):
        with pytest.raises(ValidationError):
            _reviewer_input(timeout_seconds=bad)
        with pytest.raises(ValidationError):
            _reviewer_input(token_budget=bad)
    for bad in (0, -1, 3601):
        with pytest.raises(ValidationError):
            _reviewer_input(timeout_seconds=bad)
    assert _reviewer_input(timeout_seconds=1).timeout_seconds == 1
    assert _reviewer_input(timeout_seconds=3600).timeout_seconds == 3600
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            _reviewer_input(token_budget=bad)
    assert _reviewer_input(token_budget=None).token_budget is None


def test_cost_budget_float_strictness():
    for bad in (True, 1, Decimal("2.5"), "2.5"):
        with pytest.raises(ValidationError):
            _reviewer_input(cost_budget_usd=bad)
    for bad in (-0.01, float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValidationError):
            _reviewer_input(cost_budget_usd=bad)
    assert _reviewer_input(cost_budget_usd=None).cost_budget_usd is None
    assert _reviewer_input(cost_budget_usd=0.0).cost_budget_usd == 0.0


def test_numeric_and_naive_datetimes_rejected():
    for bad in (True, 123, 1.5, "123", " 1.5e3 ", "1e3"):
        with pytest.raises(ValidationError):
            _reviewer_input(requested_at=bad)
        with pytest.raises(ValidationError):
            _finding_output(completed_at=bad)
    with pytest.raises(ValidationError):
        _reviewer_input(
            requested_at=datetime(2026, 8, 25, 8, 0)
        )
    with pytest.raises(ValidationError):
        _finding_output(
            completed_at=datetime(2026, 8, 25, 9, 0)
        )


def test_failure_details_rules():
    assert _failure("timeout", details="x" * 4096).details == "x" * 4096
    for bad in (" ", "a\x00b", "x" * 4097, 123):
        with pytest.raises(ValidationError):
            _failure("timeout", details=bad)


def test_finding_and_question_bindings_to_input():
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(_finding(subject_digest=_digest("f")),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(_finding(reviewer_role="operability"),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(_finding(rubric_hash=_digest("f")),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(_finding(evidence_refs=("ev-zz",)),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            questions=(_question(subject_digest=_digest("f")),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            questions=(_question(reviewer_role="intent"),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            questions=(_question(rubric_hash=_digest("f")),),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            questions=(_question(evidence_refs=("ev-zz",)),),
        )


def test_existing_finding_and_question_reject_duplicate_refs():
    with pytest.raises(ValidationError):
        _finding(evidence_refs=("ev-a", "ev-a"))
    with pytest.raises(ValidationError):
        _question(evidence_refs=("ev-a", "ev-a"))
    with pytest.raises(ValidationError):
        _question(evidence_refs=("ev-b", "ev-a"))


def test_finding_and_question_ids_must_be_canonical_and_unique():
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(
                _finding(finding_id="fnd-b"),
                _finding(finding_id="fnd-a"),
            ),
        )
    with pytest.raises(ValidationError):
        _finding_output(
            findings=(
                _finding(),
                _finding(finding_id="fnd-a", claim="duplicate id"),
            ),
        )
    q1 = _question(question="question alpha")
    q2 = _question(question="question zulu")
    if q1.question_id > q2.question_id:
        q1, q2 = q2, q1
    with pytest.raises(ValidationError):
        _finding_output(questions=(q2, q1))
    with pytest.raises(ValidationError):
        _finding_output(questions=(q1, q1))


def test_completed_at_must_not_precede_requested_at():
    with pytest.raises(ValidationError):
        _finding_output(completed_at=EARLIER_TIME)


def test_outcome_failure_codes_mapping_is_immutable_and_mutation_preserves_validation():
    with pytest.raises(TypeError):
        rc_module._OUTCOME_FAILURE_CODES["failure"] = "execution_failed"
    valid = _finding_output(
        outcome="failure",
        failure=_failure("execution_failed"),
    )
    assert valid.outcome == "failure"
    assert valid.failure.code == "execution_failed"
    with pytest.raises(ValidationError):
        _finding_output(
            outcome="failure",
            failure=_failure("timeout"),
        )


def test_missing_nested_input_keys_raise_validation_error_not_key_error():
    model = _finding_output(
        outcome="timeout",
        failure=_failure("timeout"),
    )
    deletion_paths = (
        ("input.subject", ("input", "subject")),
        ("input.risk_result", ("input", "risk_result")),
        (
            "input.risk_result.input.snapshot",
            ("input", "risk_result", "input", "snapshot"),
        ),
        (
            "input.risk_result.classification",
            ("input", "risk_result", "classification"),
        ),
    )
    for label, path in deletion_paths:
        data = json.loads(model.model_dump_json())
        target = data
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        with pytest.raises(ValidationError):
            FindingOutput.model_validate_json(json.dumps(data))


def test_reviewer_contracts_does_not_import_single_reviewer_private_risk_result_from_json():
    source = Path(rc_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    private_imports = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level == 0:
            targets_single_reviewer = module == "assurance.single_reviewer"
        else:
            targets_single_reviewer = (
                node.level == 1 and module == "single_reviewer"
            )
        if not targets_single_reviewer:
            continue
        for alias in node.names:
            if alias.name == "_risk_result_from_json":
                private_imports.append((node.lineno, alias.name))
    assert private_imports == []
