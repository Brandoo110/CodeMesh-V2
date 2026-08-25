"""V2-P4-03 Architecture Reviewer focused tests.

Covers package exports, the fixed Architecture rubric and tool authority,
recursive immutability, deterministic prompt anti-forgery, bytes-only
normalization, role isolation, downgrade and true truncated-context semantics,
schema_invalid fail-closed behavior, absence of PASS/Gate output, AST purity,
and proof that the facade delegates to the shared runtime instead of copying
parser/normalizer logic.
"""

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
import assurance.architecture_reviewer as architecture_module
import assurance.reviewer_runtime as runtime_module
from assurance import (
    ArchitectureReviewer,
    ChangeSubject,
    FindingOutput,
    ReviewerEvidenceContext,
    ReviewerInput,
    ReviewerNormalizationInput,
    ReviewerPrompt,
    ReviewQuestion,
)
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
    {"ReviewerPrompt", "ReviewerNormalizationInput", "ArchitectureReviewer"}
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
        "IntentAuditor",
    }
)

ARCHITECTURE_TOOL_AUTHORITY = (
    "code_glob",
    "code_grep",
    "code_read",
    "git_graph_read",
    "gitnexus_context",
    "gitnexus_impact",
    "gitnexus_query",
    "prism_evidence_read",
)
ARCHITECTURE_RUBRIC_CODES = (
    "DEPENDENCY_DIRECTION",
    "BOUNDARY_INTEGRITY",
    "DUPLICATE_CAPABILITY",
    "SECOND_SOURCE_OF_TRUTH",
    "PUBLIC_CONTRACT",
    "ADR_DEVIATION",
    "BLAST_RADIUS",
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
        _context(
            "ev-a", "git_snapshot", "git snapshot evidence for architecture review"
        ),
        _context(
            "ev-b",
            "intake_documents",
            "design and contract evidence for architecture review",
        ),
        _context(
            "ev-c",
            "command_batch",
            "module boundary evidence for architecture review",
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
    profile = architecture_module._ARCHITECTURE_PROFILE
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
        "tool_allowlist": ("code_glob", "code_read"),
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


def _architecture_prompt(reviewer_input=None):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    return ArchitectureReviewer.prepare(reviewer_input)


_DEFAULT_RAW = object()


def _norm_input(
    *,
    prompt=None,
    raw=_DEFAULT_RAW,
    model_ref="model/architecture-1",
    completed_at=LATER_TIME,
):
    if prompt is None:
        prompt = _architecture_prompt()
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
        "rubric_hash": architecture_module._ARCHITECTURE_PROFILE.rubric_hash,
        "findings": [],
        "questions": [],
    }
    payload.update(overrides)
    return payload


def _finding_draft(
    claim="dependency direction violates the layer contract",
    refs=("ev-a",),
    severity="medium",
    confidence=0.8,
    **overrides,
):
    values = {
        "reviewer_role": "architecture",
        "claim": claim,
        "evidence_refs": refs,
        "severity": severity,
        "confidence": confidence,
    }
    values.update(overrides)
    return values


def _question_draft(
    question="does the module boundary remain intact?",
    reason="model_question",
    refs=("ev-a",),
    **overrides,
):
    values = {
        "reviewer_role": "architecture",
        "question": question,
        "reason": reason,
        "evidence_refs": refs,
    }
    values.update(overrides)
    return values


def _normalize(raw, reviewer_input=None, **overrides):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    prompt = _architecture_prompt(reviewer_input)
    return ArchitectureReviewer.normalize(
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
    assert ArchitectureReviewer is architecture_module.ArchitectureReviewer
    assert ReviewerPrompt is runtime_module.ReviewerPrompt
    assert (
        ReviewerNormalizationInput is runtime_module.ReviewerNormalizationInput
    )
    assert "ArchitectureReviewer" in assurance.__all__
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


def test_architecture_rubric_authority_is_fixed_and_stable():
    profile = architecture_module._ARCHITECTURE_PROFILE
    assert profile.role == "architecture"
    assert profile.rubric_version == "architecture.v0"
    assert tuple(item.number for item in profile.rubric) == (1, 2, 3, 4, 5, 6, 7)
    assert tuple(item.code for item in profile.rubric) == ARCHITECTURE_RUBRIC_CODES
    assert all(item.name.strip() for item in profile.rubric)
    assert all(item.description.strip() for item in profile.rubric)
    assert profile.tool_authority == ARCHITECTURE_TOOL_AUTHORITY
    assert profile.tool_authority == tuple(
        sorted(ARCHITECTURE_TOOL_AUTHORITY)
    )
    assert profile.rubric_hash == architecture_module._ARCHITECTURE_RUBRIC_DIGEST
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", profile.rubric_hash)


def test_architecture_rubric_items_have_stable_names_and_meanings():
    profile = architecture_module._ARCHITECTURE_PROFILE
    expected = (
        ("DEPENDENCY_DIRECTION", "Dependency direction and layering"),
        ("BOUNDARY_INTEGRITY", "Module/service boundary integrity"),
        ("DUPLICATE_CAPABILITY", "Duplicated capability or implementation"),
        (
            "SECOND_SOURCE_OF_TRUTH",
            "Competing state/config/contract truth source",
        ),
        (
            "PUBLIC_CONTRACT",
            "Public API, Event, Schema, and compatibility contracts",
        ),
        (
            "ADR_DEVIATION",
            "Deviation from accepted ADR/architecture decision",
        ),
        (
            "BLAST_RADIUS",
            "Cross-module/repository/service/owner impact surface",
        ),
    )
    keywords = (
        "dependency",
        "boundaries",
        "duplicate",
        "truth source",
        "public API",
        "ADR",
        "impact",
    )
    assert len(profile.rubric) == len(expected) == len(keywords)
    for item, (code, name), keyword in zip(
        profile.rubric, expected, keywords
    ):
        assert item.code == code
        assert item.name == name
        assert keyword.lower() in item.description.lower()


def test_architecture_rubric_digest_is_deterministic_from_single_authority():
    profile = architecture_module._ARCHITECTURE_PROFILE
    body = {
        "role": profile.role,
        "rubric_version": profile.rubric_version,
        "rubric": [
            item.model_dump(mode="json") for item in profile.rubric
        ],
    }
    expected = _sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    assert profile.rubric_hash == expected
    assert architecture_module._ARCHITECTURE_RUBRIC_DIGEST == expected
    assert architecture_module._architecture_rubric_digest() == expected


def test_reviewer_profile_frozen_extra_forbid_and_exact_tuples():
    profile = architecture_module._ARCHITECTURE_PROFILE
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
            {
                **data,
                "tool_authority": (
                    "code_glob",
                    "code_glob",
                ),
            }
        )
    with pytest.raises(ValidationError):
        profile.rubric[0].description = "tampered"


def test_reviewer_profile_and_rubric_item_json_round_trip():
    profile = architecture_module._ARCHITECTURE_PROFILE
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
    profile = architecture_module._ARCHITECTURE_PROFILE
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
        architecture_module._ARCHITECTURE_RUBRIC_TABLE["items"][0][
            "description"
        ] = "tampered"
    with pytest.raises(TypeError):
        architecture_module._ARCHITECTURE_RUBRIC_TABLE["tool_authority"] = ()
    original_hash = profile.rubric_hash
    first = _architecture_prompt()
    second = _architecture_prompt()
    assert second == first
    assert profile.rubric_hash == original_hash
    assert profile.rubric_hash == architecture_module._ARCHITECTURE_RUBRIC_DIGEST


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
        ("code_glob",),
        ("code_grep",),
        ("code_read",),
        ("git_graph_read",),
        ("gitnexus_context",),
        ("gitnexus_impact",),
        ("gitnexus_query",),
        ("prism_evidence_read",),
        ("code_glob", "code_grep"),
        ("code_grep", "code_read"),
        ("git_graph_read", "gitnexus_context"),
        ("gitnexus_impact", "gitnexus_query"),
        ("gitnexus_context", "prism_evidence_read"),
        ("code_glob", "code_grep", "code_read"),
        (
            "git_graph_read",
            "gitnexus_context",
            "gitnexus_impact",
            "gitnexus_query",
            "prism_evidence_read",
        ),
        ARCHITECTURE_TOOL_AUTHORITY,
    ],
)
def test_tool_subsets_and_empty_are_accepted(allowlist):
    reviewer_input = _reviewer_input(tool_allowlist=allowlist)
    prompt = ArchitectureReviewer.prepare(reviewer_input)
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
        "code_write",
        "git_write",
        "gitnexus_write",
        "prism_write",
        "diff_read",
        "history_read",
    ],
)
def test_unknown_or_writable_tools_rejected(tool):
    reviewer_input = _reviewer_input(tool_allowlist=(tool,))
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(reviewer_input)


def test_prepare_rejects_wrong_role_version_or_hash():
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(
            _reviewer_input(reviewer_role="intent")
        )
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(
            _reviewer_input(reviewer_role="operability")
        )
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(
            _reviewer_input(rubric_version="intent.v0")
        )
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(
            _reviewer_input(rubric_version="operability.v0")
        )
    with pytest.raises(ValueError):
        ArchitectureReviewer.prepare(
            _reviewer_input(rubric_hash=_digest("f"))
        )


def test_prompt_is_deterministic_and_binds_input_profile_and_tools():
    reviewer_input = _reviewer_input()
    first = ArchitectureReviewer.prepare(reviewer_input)
    second = ArchitectureReviewer.prepare(reviewer_input)
    assert first.prompt_text.encode("utf-8") == second.prompt_text.encode(
        "utf-8"
    )
    assert first == second
    assert first.prompt_digest == _sha256(
        first.prompt_text.encode("utf-8")
    )
    assert re.fullmatch(r"irp_[0-9a-f]{32}", first.prompt_id)
    text = first.prompt_text
    profile = architecture_module._ARCHITECTURE_PROFILE
    lines = text.splitlines()
    assert lines[0] == "CODEX_SAFE_ARCHITECTURE_REVIEWER_PROMPT_V1"
    assert lines[1].startswith("Subject digest:")
    assert lines[-1] == "END CODEX_SAFE_ARCHITECTURE_REVIEWER_PROMPT_V1"
    assert text.count("CODEX_SAFE_ARCHITECTURE_REVIEWER_PROMPT_V1") == 2
    assert "Reviewer role: architecture" in text
    assert f"Rubric version: {profile.rubric_version}" in text
    assert f"Rubric hash: {profile.rubric_hash}" in text
    for code in ARCHITECTURE_RUBRIC_CODES:
        assert code in text
    assert "code_glob" in text
    assert "code_read" in text
    assert "Evidence allowlist: ev-a, ev-b, ev-c" in text
    assert "git snapshot evidence for architecture review" in text
    assert "no hidden evidence" in text.lower()
    assert "PASS/Gate" in text
    assert "unsupported" in text.lower()
    assert "questions" in text.lower()
    assert "do not execute" in text.lower()
    assert "Apply the current profile rubric" in text
    assert (
        "do not use any other reviewer rubric or findings"
        in text.lower()
    )
    assert "invalid reviewer response" not in text
    assert runtime_module._SCHEMA_MARKER in text
    assert first.input is reviewer_input
    assert first.profile == profile


def test_prompt_role_isolation_no_intent_or_operability_rubric():
    text = _architecture_prompt().prompt_text
    assert "intent" not in text.lower()
    assert "operability" not in text.lower()
    assert "intent.v0" not in text
    assert "operability.v0" not in text
    for code in (
        "SPEC_COVERAGE",
        "SCOPE_CREEP",
        "MISSING_ACCEPTANCE_NFR",
        "HIDDEN_ASSUMPTION",
        "TEST_DELETION_SKIP",
        "VALUE_READINESS_QUESTION",
    ):
        assert code not in text
    for code in ARCHITECTURE_RUBRIC_CODES:
        assert code in text
    source = Path(architecture_module.__file__).read_text(encoding="utf-8")
    assert "IntentAuditor" not in source
    assert "_INTENT_PROFILE" not in source
    assert "_OPERABILITY" not in source


def test_empty_tool_allowlist_uses_no_tools_marker():
    reviewer_input = _reviewer_input(tool_allowlist=())
    text = ArchitectureReviewer.prepare(reviewer_input).prompt_text
    assert runtime_module._NO_TOOLS_MARKER in text


def test_prompt_antiforgery_rejects_tampered_fields():
    prompt = _architecture_prompt()
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
                        "role": "intent",
                    }
                ),
            }
        )


def test_normalize_rejects_forged_prompt_profile_or_text():
    reviewer_input = _reviewer_input()
    prompt = _architecture_prompt(reviewer_input)
    other_profile = runtime_module.ReviewerProfile.model_construct(
        schema_version="v1",
        role="intent",
        rubric_version="intent.v0",
        rubric_hash=_digest("0"),
        rubric=(),
        tool_authority=("code_glob",),
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
        model_ref="model/architecture-1",
        raw_response=_raw(_valid_payload(reviewer_input)),
        completed_at=LATER_TIME,
    )
    result = ArchitectureReviewer.normalize(normalization)
    _assert_schema_invalid(result)


def test_reviewer_prompt_json_round_trip():
    prompt = _architecture_prompt()
    rebuilt = ReviewerPrompt.model_validate_json(prompt.model_dump_json())
    assert rebuilt == prompt


def test_reviewer_prompt_frozen_and_extra_forbid():
    prompt = _architecture_prompt()
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
    prompt = _architecture_prompt()
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
    assert finding.reviewer_role == "architecture"
    assert finding.claim == "dependency direction violates the layer contract"
    assert finding.evidence_refs == ("ev-a",)
    assert finding.basis == "inferred"
    assert finding.severity == "medium"
    assert finding.confidence == 0.8
    assert (
        finding.rubric_hash
        == architecture_module._ARCHITECTURE_PROFILE.rubric_hash
    )
    assert finding.model_ref == "model/architecture-1"
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
    assert question.question == "does the module boundary remain intact?"
    assert question.status == "open"
    assert re.fullmatch(r"rq_[0-9a-f]{32}", question.question_id)


def test_empty_findings_and_questions_is_valid_success():
    result = _normalize(_raw(_valid_payload()))
    assert result.outcome == "success"
    assert result.failure is None
    assert result.findings == ()
    assert result.questions == ()


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


def test_finding_with_unknown_ref_downgrades_preserving_valid_refs():
    reviewer_input = _reviewer_input()
    payload = _valid_payload(
        reviewer_input,
        findings=[_finding_draft(refs=("ev-a", "ev-unknown"))],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.findings == ()
    assert len(result.questions) == 1
    assert result.questions[0].reason == "unsupported_finding_evidence"
    assert result.questions[0].evidence_refs == ("ev-a",)


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
        findings=[_finding_draft(refs=("ev-t", "ev-unknown"))],
    )
    result = _normalize(_raw(payload), reviewer_input)
    assert result.outcome == "success"
    assert result.findings == ()
    assert result.questions[0].reason == "unsupported_finding_evidence"
    assert result.questions[0].evidence_refs == ("ev-t",)


def test_explicit_question_with_unknown_ref_is_schema_invalid():
    reviewer_input = _reviewer_input()
    payload = _valid_payload(
        reviewer_input,
        questions=[_question_draft(refs=("ev-unknown",))],
    )
    result = _normalize(_raw(payload), reviewer_input)
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
        _valid_payload(reviewer_input, reviewer_role="intent"),
        _valid_payload(reviewer_input, reviewer_role="operability"),
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
    extra_gate = {**base, "gate": "approve"}
    extra_pass = {**base, "pass": True}
    for payload in (
        missing_questions,
        missing_findings,
        extra_gate,
        extra_pass,
    ):
        result = _normalize(_raw(payload), reviewer_input)
        _assert_schema_invalid(result)


def test_top_level_non_object_or_malformed_json_rejected():
    for raw in (b"[]", b'"x"', b"42", b"{", b'{"a":}'):
        result = _normalize(raw)
        _assert_schema_invalid(result)


def test_duplicate_keys_bom_nul_and_invalid_utf8_rejected():
    digest = _digest("c")
    rubric_hash = architecture_module._ARCHITECTURE_PROFILE.rubric_hash
    valid_body = (
        f'{{"schema_version":"v1","subject_digest":"{digest}",'
        f'"reviewer_role":"architecture","rubric_hash":"{rubric_hash}",'
        '"findings":[],"questions":[]}'
    ).encode()
    cases = [
        b"\xff\xfe\x00",
        b"\xef\xbb\xbf" + valid_body,
        valid_body.replace(
            b'"schema_version":"v1"',
            b'"schema_version":"v1","schema_version":"v2"',
        ),
        (
            b'{"findings":[{"reviewer_role":"architecture","claim":"a",'
            b'"claim":"b","evidence_refs":["ev-a"],"severity":"low",'
            b'"confidence":0.5}],"schema_version":"v1",'
            + f'"subject_digest":"{digest}",'.encode()
            + f'"reviewer_role":"architecture","rubric_hash":"{rubric_hash}",'.encode()
            + b'"questions":[]}'
        ),
    ]
    for raw in cases:
        result = _normalize(raw)
        _assert_schema_invalid(result)


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
    text = _architecture_prompt().prompt_text
    assert '"pass"' not in text.lower()
    assert '"gate"' not in text.lower()


def test_no_pass_or_gate_in_normalization_output():
    payload = _valid_payload(
        findings=[_finding_draft()],
        questions=[_question_draft()],
    )
    result = _normalize(_raw(payload))
    dumped = result.model_dump(mode="json")
    assert not hasattr(result, "pass")
    assert not hasattr(result, "gate")
    for key in dumped:
        assert "pass" not in key.lower()
        assert "gate" not in key.lower()


@pytest.mark.parametrize(
    "module",
    [runtime_module, architecture_module],
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
        "gitnexus",
        "prism",
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
    for module in (runtime_module, architecture_module):
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


def test_architecture_module_has_no_dead_header_or_unused_runtime_imports():
    source = Path(architecture_module.__file__).read_text(encoding="utf-8")
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


def test_architecture_module_has_no_gitnexus_prism_or_provider_tool_imports():
    source = Path(architecture_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {
                    "git",
                    "gitnexus",
                    "prism",
                }
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {
                "git",
                "gitnexus",
                "prism",
            }
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {
                "execute",
                "run",
                "impact",
                "query",
                "context",
            }
    assert "import gitnexus" not in source
    assert "from gitnexus" not in source
    assert "import prism" not in source
    assert "from prism" not in source


def test_facade_does_not_copy_parser_or_normalization_logic():
    source = Path(architecture_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "def _parse_strict_json",
        "def _normalize_response",
        "def _normalize_drafts",
        "def _build_prompt_text",
        "def _response_schema",
        "def _response_schema_text",
        "class _FindingDraft",
        "class _QuestionDraft",
        "class _ResponseDraft",
        "def _finding_from_draft",
        "def _question_from_fields",
        "def _dedupe_models",
    ):
        assert marker not in source
    assert "json.loads" not in source
    assert "json.dumps" not in source
    assert "hashlib" not in source
    assert "_canonical_json_bytes" in source
    assert "_sha256_digest" in source
    assert "StructuredReviewerRuntime.prepare" in source
    assert "StructuredReviewerRuntime.normalize" in source


def test_facade_delegates_prepare_and_normalize_to_shared_runtime(
    monkeypatch,
):
    prepare_sentinel = object()
    normalize_sentinel = object()
    prompt = _architecture_prompt()
    normalization = _norm_input(prompt=prompt)
    monkeypatch.setattr(
        runtime_module.StructuredReviewerRuntime,
        "prepare",
        staticmethod(lambda value, profile: prepare_sentinel),
    )
    monkeypatch.setattr(
        runtime_module.StructuredReviewerRuntime,
        "normalize",
        staticmethod(lambda value, profile: normalize_sentinel),
    )
    assert (
        ArchitectureReviewer.prepare(_reviewer_input())
        is prepare_sentinel
    )
    assert (
        ArchitectureReviewer.normalize(normalization)
        is normalize_sentinel
    )


def test_facade_public_methods_require_exact_input_types():
    with pytest.raises(TypeError):
        ArchitectureReviewer.prepare({})
    with pytest.raises(TypeError):
        ArchitectureReviewer.normalize({})
