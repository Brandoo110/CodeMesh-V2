"""V2-P4-02 Intent Auditor focused tests.

Covers package exports, model strictness and immutability, the fixed Intent
rubric/tool authority, deterministic prompt anti-forgery, bytes-only
normalization, schema_invalid fail-closed behavior, canonical sorting and
dedupe, downgrade rules, confidence handling, AST purity, and the absence of
PASS/Gate fields.
"""

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
import assurance.intent_auditor as intent_module
import assurance.reviewer_runtime as runtime_module
from assurance import (
    ChangeSubject,
    FindingOutput,
    IntentAuditor,
    ReviewerEvidenceContext,
    ReviewerInput,
    ReviewerNormalizationInput,
    ReviewerPrompt,
    ReviewQuestion,
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

SCHEMA_INVALID_DETAILS = "invalid reviewer response"

PUBLIC_API_NAMES = frozenset(
    {"ReviewerPrompt", "ReviewerNormalizationInput", "IntentAuditor"}
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
        "ReviewerInput",
        "ReviewerFailureOutcome",
        "FindingOutput",
    }
)

INTENT_TOOL_AUTHORITY = (
    "diff_read",
    "history_read",
    "spec_read",
    "tests_read",
)
INTENT_RUBRIC_CODES = (
    "SPEC_COVERAGE",
    "SCOPE_CREEP",
    "MISSING_ACCEPTANCE_NFR",
    "HIDDEN_ASSUMPTION",
    "TEST_DELETION_SKIP",
    "VALUE_READINESS_QUESTION",
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


def _truncated_contexts():
    return _default_contexts() + (
        _context(
            "ev-t",
            "command_batch",
            "truncated command batch evidence",
            truncated=True,
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
        risk_result = _risk_result(
            subject.subject_digest, manifest=manifest
        )
    profile = intent_module._INTENT_PROFILE
    values = {
        "schema_version": "v1",
        "reviewer_role": profile.role,
        "subject": subject,
        "risk_result": risk_result,
        "contexts": contexts,
        "rubric_version": profile.rubric_version,
        "rubric_hash": profile.rubric_hash,
        "evidence_allowlist": tuple(
            item.evidence_id for item in contexts
        ),
        "tool_allowlist": ("diff_read", "tests_read"),
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


def _intent_prompt(reviewer_input=None):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    return IntentAuditor.prepare(reviewer_input)


_DEFAULT_RAW = object()


def _norm_input(
    *,
    prompt=None,
    raw=_DEFAULT_RAW,
    model_ref="model/intent-1",
    completed_at=LATER_TIME,
):
    if prompt is None:
        prompt = _intent_prompt()
    if raw is _DEFAULT_RAW:
        raw = _raw(_valid_payload())
    return ReviewerNormalizationInput.model_validate(
        {
            "schema_version": "v1",
            "prompt": prompt,
            "model_ref": model_ref,
            "raw_response": raw,
            "completed_at": completed_at,
        }
    )


def _raw(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_payload(reviewer_input=None, **overrides):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    payload = {
        "schema_version": "v1",
        "subject_digest": reviewer_input.subject.subject_digest,
        "reviewer_role": reviewer_input.reviewer_role,
        "rubric_hash": intent_module._INTENT_PROFILE.rubric_hash,
        "findings": [],
        "questions": [],
    }
    payload.update(overrides)
    return payload


def _finding_draft(
    claim="spec coverage is incomplete",
    refs=("ev-a",),
    severity="medium",
    confidence=0.8,
    **overrides,
):
    values = {
        "reviewer_role": "intent",
        "claim": claim,
        "evidence_refs": refs,
        "severity": severity,
        "confidence": confidence,
    }
    values.update(overrides)
    return values


def _question_draft(
    question="is acceptance coverage complete?",
    reason="model_question",
    refs=("ev-a",),
    **overrides,
):
    values = {
        "reviewer_role": "intent",
        "question": question,
        "reason": reason,
        "evidence_refs": refs,
    }
    values.update(overrides)
    return values


def _normalize(raw, reviewer_input=None, **overrides):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    prompt = _intent_prompt(reviewer_input)
    return IntentAuditor.normalize(
        _norm_input(prompt=prompt, raw=raw, **overrides)
    )


def _assert_schema_invalid(result, completed_at=LATER_TIME):
    assert result.outcome == "schema_invalid"
    assert result.failure is not None
    assert result.failure.code == "schema_invalid"
    assert result.failure.details == SCHEMA_INVALID_DETAILS
    assert result.findings == ()
    assert result.questions == ()
    assert result.completed_at == completed_at


def test_package_public_api_imports():
    assert IntentAuditor is intent_module.IntentAuditor
    assert ReviewerPrompt is runtime_module.ReviewerPrompt
    assert ReviewerNormalizationInput is runtime_module.ReviewerNormalizationInput
    assert "IntentAuditor" in assurance.__all__
    assert "ReviewerPrompt" in assurance.__all__
    assert "ReviewerNormalizationInput" in assurance.__all__


def test_package_exports_preserve_prior_and_include_this_api_subset():
    current_public_names = set(assurance.__all__)
    assert PRIOR_PUBLIC_NAMES <= current_public_names
    assert PUBLIC_API_NAMES <= current_public_names


def test_internal_runtime_types_importable_but_not_package_exported():
    assert hasattr(runtime_module, "ReviewerProfile")
    assert hasattr(runtime_module, "RubricItem")
    assert hasattr(runtime_module, "StructuredReviewerRuntime")
    assert "ReviewerProfile" not in assurance.__all__
    assert "RubricItem" not in assurance.__all__
    assert "StructuredReviewerRuntime" not in assurance.__all__


def test_intent_rubric_authority_is_fixed_and_stable():
    profile = intent_module._INTENT_PROFILE
    assert profile.role == "intent"
    assert profile.rubric_version == "intent.v0"
    assert tuple(item.number for item in profile.rubric) == (1, 2, 3, 4, 5, 6)
    assert tuple(item.code for item in profile.rubric) == INTENT_RUBRIC_CODES
    assert all(item.name.strip() for item in profile.rubric)
    assert all(item.description.strip() for item in profile.rubric)
    assert profile.tool_authority == INTENT_TOOL_AUTHORITY
    assert profile.rubric_hash == intent_module._INTENT_RUBRIC_DIGEST
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", profile.rubric_hash)


def test_reviewer_profile_frozen_extra_forbid_and_exact_tuples():
    profile = intent_module._INTENT_PROFILE
    data = profile.model_dump(mode="python")
    with pytest.raises(ValidationError):
        runtime_module.ReviewerProfile.model_validate(
            {**data, "extra": 1}
        )
    with pytest.raises(ValidationError):
        runtime_module.ReviewerProfile.model_validate(
            {**data, "rubric": list(profile.rubric)}
        )
    with pytest.raises(ValidationError):
        runtime_module.ReviewerProfile.model_validate(
            {**data, "tool_authority": ("b", "a")}
        )
    with pytest.raises(ValidationError):
        runtime_module.ReviewerProfile.model_validate(
            {**data, "tool_authority": ("diff_read", "diff_read")}
        )
    with pytest.raises(ValidationError):
        profile.rubric[0].description = "tampered"


def test_reviewer_profile_and_rubric_item_json_round_trip():
    profile = intent_module._INTENT_PROFILE
    rebuilt = runtime_module.ReviewerProfile.model_validate_json(
        profile.model_dump_json()
    )
    assert rebuilt == profile
    item = profile.rubric[0]
    rebuilt_item = runtime_module.RubricItem.model_validate_json(
        item.model_dump_json()
    )
    assert rebuilt_item == item


def test_rubric_and_profile_deep_immutability_and_digest_stability():
    profile = intent_module._INTENT_PROFILE
    with pytest.raises(ValidationError):
        profile.rubric[0].description = "tampered"
    with pytest.raises(ValidationError):
        runtime_module.ReviewerProfile.model_validate(
            {
                **profile.model_dump(mode="python"),
                "rubric_hash": _digest("0"),
            }
        )
    with pytest.raises(TypeError):
        intent_module._INTENT_RUBRIC_TABLE["items"][0][
            "description"
        ] = "tampered"
    with pytest.raises(TypeError):
        intent_module._INTENT_RUBRIC_TABLE["tool_authority"] = ()
    original_hash = profile.rubric_hash
    first = _intent_prompt()
    second = _intent_prompt()
    assert second == first
    assert profile.rubric_hash == original_hash
    assert profile.rubric_hash == intent_module._INTENT_RUBRIC_DIGEST


def test_rubric_item_rejects_blank_codes_and_bool_numbers():
    with pytest.raises(ValidationError):
        runtime_module.RubricItem.model_validate(
            {
                "number": True,
                "code": "X",
                "name": "Name",
                "description": "Description",
            }
        )
    with pytest.raises(ValidationError):
        runtime_module.RubricItem.model_validate(
            {
                "number": 1,
                "code": "   ",
                "name": "Name",
                "description": "Description",
            }
        )
    with pytest.raises(ValidationError):
        runtime_module.RubricItem.model_validate(
            {
                "number": 1,
                "code": "X",
                "name": "Name",
                "description": "",
                "extra": 1,
            }
        )


@pytest.mark.parametrize(
    "allowlist",
    [
        (),
        ("diff_read",),
        ("history_read",),
        ("spec_read",),
        ("tests_read",),
        ("diff_read", "history_read"),
        ("history_read", "spec_read"),
        ("spec_read", "tests_read"),
        ("diff_read", "history_read", "spec_read", "tests_read"),
    ],
)
def test_tool_subsets_and_empty_are_accepted(allowlist):
    reviewer_input = _reviewer_input(tool_allowlist=allowlist)
    prompt = IntentAuditor.prepare(reviewer_input)
    assert prompt.input is reviewer_input


@pytest.mark.parametrize(
    "tool",
    [
        "shell",
        "bash",
        "write",
        "edit",
        "deploy",
        "unknown_tool",
        "diff_write",
        "history",
    ],
)
def test_unknown_or_writable_tools_rejected(tool):
    reviewer_input = _reviewer_input(tool_allowlist=(tool,))
    with pytest.raises(ValueError):
        IntentAuditor.prepare(reviewer_input)


def test_prepare_rejects_wrong_role_version_or_hash():
    with pytest.raises(ValueError):
        IntentAuditor.prepare(_reviewer_input(reviewer_role="architecture"))
    with pytest.raises(ValueError):
        IntentAuditor.prepare(
            _reviewer_input(rubric_version="architecture.v0")
        )
    with pytest.raises(ValueError):
        IntentAuditor.prepare(
            _reviewer_input(rubric_hash=_digest("f"))
        )


def test_prompt_is_deterministic_and_binds_input_profile_and_tools():
    reviewer_input = _reviewer_input()
    first = IntentAuditor.prepare(reviewer_input)
    second = IntentAuditor.prepare(reviewer_input)
    assert first.prompt_text.encode("utf-8") == second.prompt_text.encode(
        "utf-8"
    )
    assert first == second
    assert first.prompt_digest == _sha256(
        first.prompt_text.encode("utf-8")
    )
    assert re.fullmatch(r"irp_[0-9a-f]{32}", first.prompt_id)
    text = first.prompt_text
    profile = intent_module._INTENT_PROFILE
    lines = text.splitlines()
    assert lines[0] == "CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1"
    assert lines[1].startswith("Subject digest:")
    assert lines[-1] == "END CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1"
    assert text.count("CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1") == 2
    assert "Reviewer role: intent" in text
    assert f"Rubric version: {profile.rubric_version}" in text
    assert f"Rubric hash: {profile.rubric_hash}" in text
    for code in INTENT_RUBRIC_CODES:
        assert code in text
    assert "diff_read" in text
    assert "tests_read" in text
    assert "Evidence allowlist: ev-a, ev-b, ev-c" in text
    assert "git snapshot evidence for review" in text
    assert "no hidden evidence" in text.lower()
    assert "PASS/Gate" in text
    assert "unsupported" in text.lower()
    assert "questions" in text.lower()
    assert "do not execute" in text.lower()
    assert "Architecture/Operability" not in text
    assert "Apply the current profile rubric" in text
    assert (
        "do not use any other reviewer rubric or findings"
        in text.lower()
    )
    assert "invalid reviewer response" not in text
    assert runtime_module._SCHEMA_MARKER in text
    assert first.input is reviewer_input
    assert first.profile == profile


def test_prompt_header_line_and_count_semantics():
    text = _intent_prompt().prompt_text
    lines = text.splitlines()
    assert lines[0] == "CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1"
    assert lines[1].startswith("Subject digest:")
    assert lines[-1] == "END CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1"
    assert text.count("CODEX_SAFE_INTENT_REVIEWER_PROMPT_V1") == 2


def test_runtime_source_and_prompt_are_role_neutral_and_generic():
    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    assert "Architecture/Operability" not in source
    assert "intent auditor response" not in source
    assert "invalid reviewer response" in source
    text = _intent_prompt().prompt_text
    assert "Architecture/Operability" not in text
    assert "intent auditor response" not in text
    assert "Apply the current profile rubric" in text
    assert (
        "do not use any other reviewer rubric or findings"
        in text.lower()
    )
    assert runtime_module._SCHEMA_INVALID_MESSAGE == (
        "invalid reviewer response"
    )


def test_empty_tool_allowlist_uses_no_tools_marker():
    reviewer_input = _reviewer_input(tool_allowlist=())
    text = IntentAuditor.prepare(reviewer_input).prompt_text
    assert runtime_module._NO_TOOLS_MARKER in text


def test_prompt_antiforgery_rejects_tampered_fields():
    prompt = _intent_prompt()
    base = {
        "schema_version": "v1",
        "input": prompt.input,
        "profile": prompt.profile,
        "prompt_text": prompt.prompt_text,
        "prompt_digest": prompt.prompt_digest,
        "prompt_id": prompt.prompt_id,
    }
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {**base, "prompt_text": prompt.prompt_text + " tampered"}
        )
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {**base, "prompt_digest": _digest("0")}
        )
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {**base, "prompt_id": "irp_" + "0" * 32}
        )
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {
                **base,
                "profile": runtime_module.ReviewerProfile.model_validate(
                    {
                        **prompt.profile.model_dump(mode="python"),
                        "role": "architecture",
                    }
                ),
            }
        )


def test_normalize_rejects_forged_prompt_profile_or_text():
    reviewer_input = _reviewer_input()
    prompt = _intent_prompt(reviewer_input)
    other_profile = runtime_module.ReviewerProfile.model_construct(
        schema_version="v1",
        role="architecture",
        rubric_version="architecture.v0",
        rubric_hash=_digest("0"),
        rubric=(),
        tool_authority=("diff_read",),
    )
    forged = runtime_module.ReviewerPrompt.model_construct(
        schema_version="v1",
        input=reviewer_input,
        profile=other_profile,
        prompt_text="forged",
        prompt_digest=_digest("0"),
        prompt_id="irp_" + "0" * 32,
    )
    normalization = ReviewerNormalizationInput.model_construct(
        schema_version="v1",
        prompt=forged,
        model_ref="model/intent-1",
        raw_response=_raw(_valid_payload(reviewer_input)),
        completed_at=LATER_TIME,
    )
    result = IntentAuditor.normalize(normalization)
    _assert_schema_invalid(result)


def test_reviewer_prompt_json_round_trip():
    prompt = _intent_prompt()
    rebuilt = ReviewerPrompt.model_validate_json(prompt.model_dump_json())
    assert rebuilt == prompt


def test_reviewer_prompt_frozen_and_extra_forbid():
    prompt = _intent_prompt()
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {**prompt.model_dump(mode="python"), "extra": 1}
        )
    with pytest.raises(ValidationError):
        ReviewerPrompt.model_validate(
            {
                **prompt.model_dump(mode="python"),
                "input": prompt.input.model_dump(mode="python"),
            }
        )


def test_normalization_input_fields_are_minimal_and_binding():
    normalization = _norm_input()
    assert set(ReviewerNormalizationInput.model_fields) == {
        "schema_version",
        "prompt",
        "model_ref",
        "raw_response",
        "completed_at",
    }
    prompt = _intent_prompt()
    assert normalization.prompt == prompt
    assert normalization.completed_at == LATER_TIME
    with pytest.raises(ValidationError):
        ReviewerNormalizationInput.model_validate(
            {
                **normalization.model_dump(mode="python"),
                "prompt": prompt.model_dump(mode="python"),
            }
        )
    with pytest.raises(ValidationError):
        ReviewerNormalizationInput.model_validate(
            {
                **normalization.model_dump(mode="python"),
                "extra": 1,
            }
        )


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"0" * (1024 * 1024 + 1),
        bytearray(b"{}"),
        memoryview(b"{}"),
        "{}",
        None,
    ],
)
def test_normalization_input_rejects_bad_raw_response(bad):
    with pytest.raises(ValidationError):
        _norm_input(raw=bad)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a\x00b", 123, None, "m" * 300],
)
def test_normalization_input_rejects_bad_model_ref(bad):
    with pytest.raises(ValidationError):
        _norm_input(model_ref=bad)


@pytest.mark.parametrize(
    "bad",
    [
        EARLIER_TIME,
        datetime(2026, 8, 25, 8, 0, 0),
        1720000000,
        True,
        3.5,
        "1720000000",
    ],
)
def test_normalization_input_rejects_bad_completed_at(bad):
    with pytest.raises(ValidationError):
        _norm_input(completed_at=bad)


def test_normalization_input_accepts_completed_at_equal_to_requested_at():
    normalization = _norm_input(completed_at=FIXED_TIME)
    assert normalization.completed_at == FIXED_TIME


def test_normalization_input_json_round_trip():
    normalization = _norm_input()
    rebuilt = ReviewerNormalizationInput.model_validate_json(
        normalization.model_dump_json()
    )
    assert rebuilt == normalization


def test_valid_finding_normalization():
    reviewer_input = _reviewer_input()
    payload = _valid_payload(
        reviewer_input,
        findings=[_finding_draft()],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.failure is None
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.subject_digest == reviewer_input.subject.subject_digest
    assert finding.reviewer_role == "intent"
    assert finding.claim == "spec coverage is incomplete"
    assert finding.evidence_refs == ("ev-a",)
    assert finding.basis == "inferred"
    assert finding.severity == "medium"
    assert finding.confidence == 0.8
    assert finding.rubric_hash == intent_module._INTENT_PROFILE.rubric_hash
    assert finding.model_ref == "model/intent-1"
    assert finding.status == "open"
    assert re.fullmatch(r"fnd_[0-9a-f]{32}", finding.finding_id)
    again = _normalize(_raw(payload), reviewer_input)
    assert again == result


def test_valid_explicit_question_normalization():
    payload = _valid_payload(
        questions=[_question_draft(reason="model_question")]
    )
    result = _normalize(_raw(payload))
    assert result.outcome == "success"
    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.reason == "model_question"
    assert question.evidence_refs == ("ev-a",)
    assert question.question == "is acceptance coverage complete?"
    assert question.status == "open"
    assert re.fullmatch(r"rq_[0-9a-f]{32}", question.question_id)


def test_empty_findings_and_questions_is_valid_success():
    result = _normalize(_raw(_valid_payload()))
    assert result.outcome == "success"
    assert result.failure is None
    assert result.findings == ()
    assert result.questions == ()


@pytest.mark.parametrize("value", [0, 1, 0.5, -0.0, 0.0, 1.0])
def test_confidence_numbers_0_to_1_accepted(value):
    payload = _valid_payload(
        findings=[_finding_draft(confidence=value)]
    )
    result = _normalize(_raw(payload))
    assert result.outcome == "success"
    assert result.findings[0].confidence == float(value)


@pytest.mark.parametrize(
    "raw_value",
    [
        "true",
        "false",
        '"0.5"',
        "1.1",
        "-0.1",
    ],
)
def test_confidence_invalid_values_rejected(raw_value):
    raw = (
        b'{"schema_version":"v1",'
        + f'"subject_digest":"{_digest("c")}",'.encode()
        + b'"reviewer_role":"intent",'
        + f'"rubric_hash":"{intent_module._INTENT_PROFILE.rubric_hash}",'.encode()
        + b'"findings":[{"reviewer_role":"intent","claim":"c",'
        + b'"evidence_refs":["ev-a"],"severity":"low",'
        + f'"confidence":{raw_value}'.encode()
        + b'}],"questions":[]}'
    )
    result = _normalize(raw)
    _assert_schema_invalid(result)


@pytest.mark.parametrize(
    "constant",
    ["NaN", "Infinity", "-Infinity"],
)
def test_confidence_nan_and_infinity_rejected(constant):
    raw = (
        b'{"schema_version":"v1",'
        + f'"subject_digest":"{_digest("c")}",'.encode()
        + b'"reviewer_role":"intent",'
        + f'"rubric_hash":"{intent_module._INTENT_PROFILE.rubric_hash}",'.encode()
        + b'"findings":[{"reviewer_role":"intent","claim":"c",'
        + b'"evidence_refs":["ev-a"],"severity":"low",'
        + f'"confidence":{constant}'.encode()
        + b'}],"questions":[]}'
    )
    result = _normalize(raw)
    _assert_schema_invalid(result)


def test_finding_draft_rejects_decimal_confidence():
    with pytest.raises(ValidationError):
        runtime_module._FindingDraft.model_validate(
            {
                "reviewer_role": "intent",
                "claim": "c",
                "evidence_refs": ("ev-a",),
                "severity": "low",
                "confidence": Decimal("0.5"),
            }
        )


def test_finding_with_no_refs_downgrades_to_question():
    payload = _valid_payload(
        findings=[_finding_draft(refs=())]
    )
    result = _normalize(_raw(payload))
    assert result.outcome == "success"
    assert result.findings == ()
    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.reason == "unsupported_finding_evidence"
    assert question.evidence_refs == ()
    assert question.question == "spec coverage is incomplete"


def test_finding_with_unknown_ref_downgrades_preserving_valid_refs():
    payload = _valid_payload(
        findings=[_finding_draft(refs=("ev-zzz",))]
    )
    result = _normalize(_raw(payload))
    assert result.findings == ()
    assert result.questions[0].reason == "unsupported_finding_evidence"
    assert result.questions[0].evidence_refs == ()

    payload = _valid_payload(
        findings=[_finding_draft(refs=("ev-zzz", "ev-b", "ev-a"))]
    )
    result = _normalize(_raw(payload))
    assert result.findings == ()
    assert result.questions[0].reason == "unsupported_finding_evidence"
    assert result.questions[0].evidence_refs == ("ev-a", "ev-b")


def test_finding_referencing_truncated_context_downgrades():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        findings=[_finding_draft(refs=("ev-t",))],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.findings == ()
    assert len(result.questions) == 1
    assert result.questions[0].reason == "truncated_context"
    assert result.questions[0].evidence_refs == ("ev-t",)


def test_finding_truncated_and_unknown_priority_is_unsupported():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        findings=[_finding_draft(refs=("ev-t", "ev-zzz"))],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.findings == ()
    assert result.questions[0].reason == "unsupported_finding_evidence"
    assert result.questions[0].evidence_refs == ("ev-t",)


def test_explicit_question_with_unknown_ref_is_schema_invalid():
    payload = _valid_payload(
        questions=[_question_draft(refs=("ev-zzz",))]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)


def test_explicit_truncated_context_question_is_accepted():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        questions=[
            _question_draft(
                reason="truncated_context",
                refs=("ev-t",),
                question="was the truncated batch captured?",
            )
        ],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.questions[0].reason == "truncated_context"
    assert result.questions[0].evidence_refs == ("ev-t",)


def test_model_question_with_empty_refs_is_valid():
    payload = _valid_payload(
        questions=[_question_draft(reason="model_question", refs=())]
    )
    result = _normalize(_raw(payload))
    assert result.outcome == "success"
    assert result.questions[0].reason == "model_question"
    assert result.questions[0].evidence_refs == ()


def test_explicit_truncated_context_question_empty_refs_is_schema_invalid():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        questions=[
            _question_draft(
                reason="truncated_context",
                refs=(),
                question="was the truncated batch captured?",
            )
        ],
    )
    result = _normalize(_raw(payload), reviewer_input)
    _assert_schema_invalid(result)


def test_explicit_truncated_context_question_without_truncated_ref_is_schema_invalid():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        questions=[
            _question_draft(
                reason="truncated_context",
                refs=("ev-a",),
                question="was the truncated batch captured?",
            )
        ],
    )
    result = _normalize(_raw(payload), reviewer_input)
    _assert_schema_invalid(result)


def test_explicit_truncated_context_question_mixed_valid_refs_accepted():
    reviewer_input = _reviewer_input(contexts=_truncated_contexts())
    payload = _valid_payload(
        reviewer_input,
        questions=[
            _question_draft(
                reason="truncated_context",
                refs=("ev-a", "ev-t"),
                question="was the truncated batch captured?",
            )
        ],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.questions[0].reason == "truncated_context"
    assert result.questions[0].evidence_refs == ("ev-a", "ev-t")


def test_cross_role_subject_and_rubric_bindings_rejected():
    reviewer_input = _reviewer_input()
    cases = [
        _valid_payload(reviewer_input, reviewer_role="architecture"),
        _valid_payload(reviewer_input, subject_digest=_digest("f")),
        _valid_payload(reviewer_input, rubric_hash=_digest("f")),
    ]
    for payload in cases:
        result = _normalize(_raw(payload), reviewer_input)
        _assert_schema_invalid(result)


def test_finding_draft_extra_fields_rejected():
    payload = _valid_payload(
        findings=[
            _finding_draft(
                finding_id="fnd_" + "0" * 32,
                model_ref="model/x",
                basis="deterministic",
                status="resolved",
            )
        ]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)


def test_question_draft_extra_fields_rejected():
    payload = _valid_payload(
        questions=[
            _question_draft(
                question_id="rq_" + "0" * 32,
                status="resolved",
                rubric_hash=_digest("f"),
            )
        ]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)


def test_schema_missing_or_extra_top_level_fields_rejected():
    reviewer_input = _reviewer_input()
    base = _valid_payload(reviewer_input)
    missing_questions = dict(base)
    del missing_questions["questions"]
    missing_findings = dict(base)
    del missing_findings["findings"]
    extra = {**base, "gate": "approve"}
    for payload in (missing_questions, missing_findings, extra):
        result = _normalize(_raw(payload), reviewer_input)
        _assert_schema_invalid(result)


def test_top_level_non_object_or_malformed_json_rejected():
    for raw in (b"[]", b'"x"', b"42", b"{", b'{"a":}'):
        result = _normalize(raw)
        _assert_schema_invalid(result)


def test_duplicate_keys_bom_nul_and_invalid_utf8_rejected():
    digest = _digest("c")
    rubric_hash = intent_module._INTENT_PROFILE.rubric_hash
    valid_body = (
        f'{{"schema_version":"v1","subject_digest":"{digest}",'
        f'"reviewer_role":"intent","rubric_hash":"{rubric_hash}",'
        '"findings":[],"questions":[]}'
    ).encode()
    cases = [
        b"\xff\xfe\x00",
        b"\xef\xbb\xbf" + valid_body,
        valid_body.replace(b'"schema_version":"v1"', b'"schema_version":"v1","schema_version":"v2"'),
        (
            b'{"findings":[{"reviewer_role":"intent","claim":"a",'
            b'"claim":"b","evidence_refs":["ev-a"],"severity":"low",'
            b'"confidence":0.5}],"schema_version":"v1",'
            + f'"subject_digest":"{digest}",'.encode()
            + f'"reviewer_role":"intent","rubric_hash":"{rubric_hash}",'.encode()
            + b'"questions":[]}'
        ),
    ]
    for raw in cases:
        result = _normalize(raw)
        _assert_schema_invalid(result)


def test_nan_and_infinity_constants_rejected():
    digest = _digest("c")
    rubric_hash = intent_module._INTENT_PROFILE.rubric_hash
    for constant in ("NaN", "Infinity", "-Infinity"):
        raw = (
            f'{{"schema_version":"v1","subject_digest":"{digest}",'
            f'"reviewer_role":"intent","rubric_hash":"{rubric_hash}",'
            f'"findings":[],"questions":[],"x":{constant}}}'
        ).encode()
        result = _normalize(raw)
        _assert_schema_invalid(result)


def test_json_depth_and_node_count_limits_fail_closed():
    with pytest.raises(ValueError):
        runtime_module._parse_strict_json(
            b'{"x":' + b"[" * 70 + b"0" + b"]" * 70 + b"}"
        )
    big = b'{"x":[' + b",".join([b"0"] * 5000) + b"]}"
    with pytest.raises(ValueError):
        runtime_module._parse_strict_json(big)
    result = _normalize(big)
    _assert_schema_invalid(result)


def test_findings_and_questions_count_limit_rejected():
    findings = [
        _finding_draft(claim=f"claim-{index}")
        for index in range(257)
    ]
    payload = _valid_payload(findings=findings)
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)


def test_oversized_claim_question_and_refs_rejected():
    payload = _valid_payload(
        findings=[_finding_draft(claim="x" * 5000)]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)

    payload = _valid_payload(
        questions=[_question_draft(question="x" * 5000)]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)

    payload = _valid_payload(
        questions=[
            _question_draft(refs=tuple(f"ev-{i}" for i in range(17)))
        ]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)

    payload = _valid_payload(
        questions=[_question_draft(refs=("e" * 300,))]
    )
    result = _normalize(_raw(payload))
    _assert_schema_invalid(result)


def test_canonical_sort_dedupe_and_permutation_stability():
    reviewer_input = _reviewer_input()
    f1 = _finding_draft(claim="first", refs=("ev-a",), severity="low")
    f2 = _finding_draft(claim="second", refs=("ev-b",), severity="high")
    payload_a = _valid_payload(reviewer_input, findings=[f1, f2])
    payload_b = _valid_payload(reviewer_input, findings=[f2, f1])
    result_a = _normalize(_raw(payload_a), reviewer_input)
    result_b = _normalize(_raw(payload_b), reviewer_input)
    assert result_a == result_b
    ids = [item.finding_id for item in result_a.findings]
    assert ids == sorted(ids)

    payload_dup = _valid_payload(reviewer_input, findings=[f1, f1])
    result_dup = _normalize(_raw(payload_dup), reviewer_input)
    assert len(result_dup.findings) == 1


def test_same_claim_with_different_refs_is_not_merged():
    payload = _valid_payload(
        findings=[
            _finding_draft(claim="same claim", refs=("ev-a",)),
            _finding_draft(claim="same claim", refs=("ev-b",)),
        ]
    )
    result = _normalize(_raw(payload))
    assert len(result.findings) == 2
    assert len({item.finding_id for item in result.findings}) == 2


def test_question_dedupe_and_sort():
    q1 = _question_draft(question="same question")
    q2 = _question_draft(
        question="same question",
        refs=("ev-b",),
    )
    payload = _valid_payload(questions=[q2, q1, q1])
    result = _normalize(_raw(payload))
    assert len(result.questions) == 2
    ids = [item.question_id for item in result.questions]
    assert ids == sorted(ids)


def test_failure_details_are_fixed_and_secret_free():
    raw = b'{"TOPSECRET_MARKER":"abc"'
    result = _normalize(raw)
    _assert_schema_invalid(result)
    assert "TOPSECRET" not in result.failure.details
    assert result.failure.details == SCHEMA_INVALID_DETAILS


def test_schema_invalid_preserves_input_and_completed_at():
    reviewer_input = _reviewer_input()
    result = _normalize(b"{", reviewer_input, completed_at=LATER_TIME)
    _assert_schema_invalid(result, completed_at=LATER_TIME)
    assert result.input == reviewer_input


def test_no_pass_or_gate_fields_in_models_or_prompt_schema():
    for model in (ReviewerPrompt, ReviewerNormalizationInput, FindingOutput):
        fields = set(model.model_fields)
        assert "pass" not in fields
        assert "gate" not in fields
    text = _intent_prompt().prompt_text
    assert '"pass"' not in text.lower()
    assert '"gate"' not in text.lower()


@pytest.mark.parametrize(
    "module",
    [runtime_module, intent_module],
)
def test_module_ast_has_no_transport_execution_or_path_imports(module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {
        "os",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "http",
        "pathlib",
        "git",
        "random",
        "time",
        "datetime",
        "platform",
        "shutil",
        "tempfile",
        "importlib",
        "ctypes",
        "signal",
        "multiprocessing",
        "threading",
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
            forbidden_calls = {"eval", "exec", "compile", "open"}
            if isinstance(node.func, ast.Attribute):
                forbidden_calls = {"eval", "exec", "open"}
            assert name not in forbidden_calls


def test_module_ast_never_reads_environment_or_runtime_state():
    for module in (runtime_module, intent_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in {"environ", "ArtifactStore"}
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "ArtifactStore"}
        assert "os.environ" not in source
        assert "subprocess" not in source
        assert "getcwd" not in source
        assert "datetime.now" not in source
        assert "time.time" not in source


def test_intent_module_has_no_dead_header_or_unused_runtime_imports():
    source = Path(intent_module.__file__).read_text(encoding="utf-8")
    assert "_PROMPT_HEADER" not in source
    assert "_NO_TOOLS_MARKER" not in source
    assert "_SCHEMA_MARKER" not in source
    tree = ast.parse(source)
    used_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    runtime_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "reviewer_runtime",
            ".reviewer_runtime",
        }:
            runtime_imports.update(
                alias.asname or alias.name for alias in node.names
            )
    assert runtime_imports
    for name in sorted(runtime_imports):
        assert name in used_names


def test_strict_json_parser_returns_mapping_for_valid_object():
    payload = _valid_payload()
    parsed = runtime_module._parse_strict_json(_raw(payload))
    assert type(parsed) is dict
    assert parsed["schema_version"] == "v1"
