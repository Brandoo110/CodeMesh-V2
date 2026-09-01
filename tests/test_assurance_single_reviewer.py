"""V2-P3-04A Single Strong Reviewer focused tests."""

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

import assurance
import assurance.single_reviewer as single_module
from assurance import (
    ReviewerEvidenceContext,
    ReviewQuestion,
    SingleReviewerInput,
    SingleReviewerInvocation,
    SingleReviewerPrompt,
    SingleReviewerNormalizationInput,
    SingleReviewerResult,
    SingleStrongReviewer,
    SingleReviewerError,
    SingleReviewerPayloadError,
    SingleReviewerSubjectMismatchError,
    SingleReviewerArtifactError,
)
from assurance.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from assurance.contracts import ChangeSubject, ExecutionReceipt, ExecutionStep, Finding
from assurance.intake import IntakeSnapshot
from assurance.manifest import EvidenceManifest, EvidenceManifestEntry
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

PUBLIC_API_NAMES = frozenset(
    {
        "ReviewerEvidenceContext",
        "ReviewQuestion",
        "SingleReviewerInput",
        "SingleReviewerInvocation",
        "SingleReviewerPrompt",
        "SingleReviewerNormalizationInput",
        "SingleReviewerResult",
        "SingleStrongReviewer",
        "SingleReviewerError",
        "SingleReviewerPayloadError",
        "SingleReviewerSubjectMismatchError",
        "SingleReviewerArtifactError",
    }
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
        "SQLiteAssuranceStore",
        "AssuranceStoreError",
        "StoreMigrationError",
        "CaseNotFoundError",
        "StoreConflictError",
        "ProjectionIntegrityError",
        "StorePersistenceError",
        "GitChange",
        "GitSnapshot",
        "GitSnapshotResult",
        "GitSnapshotCollector",
        "GitSnapshotError",
        "GitRepositoryError",
        "GitCommandError",
        "GitWorktreeChangedError",
        "IntakeDocument",
        "IntakeNotice",
        "IntakeSnapshot",
        "IntakeResult",
        "TaskPolicyCollector",
        "IntakeCollectionError",
        "IntakePathError",
        "IntakeFormatError",
        "IntakeChangedError",
        "CommandSpec",
        "CommandObservation",
        "CommandBatchSnapshot",
        "CommandBatchResult",
        "DeterministicCommandCollector",
        "CommandCollectionError",
        "CommandSpecError",
        "CommandLaunchError",
        "CommandExecutionError",
        "GenericEvidenceImporter",
        "GenericEvidenceEnvelope",
        "GenericEvidenceImportReceipt",
        "GenericEvidenceImportResult",
        "SignatureMetadata",
        "GenericEvidenceImportError",
        "GenericEvidencePayloadError",
        "GenericEvidenceSubjectMismatch",
        "GenericEvidenceArtifactError",
        "AuthorAgentReceiptCost",
        "GenericAuthorReceiptEnvelope",
        "AuthorAgentReceipt",
        "AuthorAgentReceiptResult",
        "AuthorAgentReceiptNormalizer",
        "AuthorAgentReceiptError",
        "AuthorAgentReceiptPayloadError",
        "AuthorAgentReceiptSubjectMismatch",
        "AuthorAgentReceiptArtifactError",
        "EvidenceManifestInput",
        "EvidenceManifestEntry",
        "EvidenceManifest",
        "EvidenceManifestResult",
        "EvidenceManifestBuilder",
        "EvidenceManifestError",
        "EvidenceManifestInputError",
        "EvidenceManifestSubjectError",
        "EvidenceManifestArtifactError",
        "EvidenceManifestPersistenceError",
        "RiskDeclarations",
        "RiskClassificationInput",
        "RiskClassification",
        "RiskClassificationResult",
        "RiskClassifier",
        "PolicyEvaluationInput",
        "PolicyGateResult",
        "PolicyGate",
        "RulesOnlyExpectation",
        "RulesOnlyFixture",
        "RulesOnlyBaselineResult",
        "RulesOnlyBaselineRunner",
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


def _manifest(subject_digest=None, contexts=None, *, evaluated_at=FIXED_TIME):
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
                        "fresh"
                        if evaluated_at <= LATER_TIME
                        else "stale"
                    ),
                    "redaction_status": item.redaction_status,
                }
            )
        )
    entries = tuple(
        sorted(entries, key=lambda entry: entry.evidence_id)
    )
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


def _reviewer_input(
    subject_digest=None,
    *,
    contexts=None,
    evaluated_at=FIXED_TIME,
    subject=None,
):
    if subject_digest is None:
        subject_digest = _digest("c")
    if subject is None:
        subject = _subject(subject_digest)
    if contexts is None:
        contexts = _default_contexts()
    manifest = _manifest(subject_digest, contexts, evaluated_at=evaluated_at)
    risk_input = _risk_input(subject_digest, manifest=manifest)
    risk_result = RiskClassifier.classify(risk_input)
    return SingleReviewerInput.model_validate(
        {
            "schema_version": "v1",
            "subject": subject,
            "risk_result": risk_result,
            "contexts": contexts,
            "evaluated_at": evaluated_at,
        }
    )


def _invocation(**overrides):
    values = {
        "schema_version": "v1",
        "run_id": "run-review-1",
        "model_ref": "model/strong-1",
        "provider": "deepseek",
        "usage_status": "measured",
        "input_tokens": 120,
        "output_tokens": 80,
        "cost_usd": 0.0125,
        "started_at": FIXED_TIME,
        "completed_at": LATER_TIME,
        "latency_ms": 3_600_000,
        "timeout_seconds": 120,
        "result": "success",
        "schema_status": "valid",
        "fallback_reason": None,
        "tool_grants": (),
    }
    values.update(overrides)
    return SingleReviewerInvocation.model_validate(values)


def _prompt(reviewer_input=None):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    return SingleStrongReviewer.prepare(reviewer_input)


def _response_bytes(
    subject_digest,
    *,
    rubric_hash=None,
    findings=(),
    questions=(),
):
    if rubric_hash is None:
        rubric_hash = single_module._RUBRIC_DIGEST
    payload = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "rubric_hash": rubric_hash,
        "findings": list(findings),
        "questions": list(questions),
    }
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _valid_finding(**overrides):
    values = {
        "reviewer_role": "architecture",
        "claim": "boundary direction must be explicit",
        "evidence_refs": ["ev-a"],
        "severity": "medium",
        "confidence": 0.8,
    }
    values.update(overrides)
    return values


def _valid_question(**overrides):
    values = {
        "reviewer_role": "intent",
        "question": "what does acceptance coverage include?",
        "reason": "model_question",
        "evidence_refs": ["ev-b"],
    }
    values.update(overrides)
    return values


def _normalization_input(
    reviewer_input=None,
    *,
    invocation=None,
    prompt=None,
    raw=None,
):
    if reviewer_input is None:
        reviewer_input = _reviewer_input()
    if prompt is None:
        prompt = SingleStrongReviewer.prepare(reviewer_input)
    if invocation is None:
        invocation = _invocation()
    if raw is None:
        raw = _response_bytes(reviewer_input.subject.subject_digest)
    return SingleReviewerNormalizationInput.model_validate(
        {
            "schema_version": "v1",
            "reviewer_input": reviewer_input,
            "prompt": prompt,
            "invocation": invocation,
            "raw_response": raw,
        }
    )


def _normalize(
    store,
    reviewer_input=None,
    *,
    invocation=None,
    raw=None,
):
    normalization_input = _normalization_input(
        reviewer_input, invocation=invocation, raw=raw
    )
    return SingleStrongReviewer.normalize(normalization_input, store)


def _finding_id_data(finding):
    return {
        key: value
        for key, value in finding.model_dump(mode="json").items()
        if key != "finding_id"
    }


def _question_id_data(question):
    return {
        key: value
        for key, value in question.model_dump(mode="json").items()
        if key != "question_id"
    }


def _receipt_id_data(receipt):
    return {
        key: value
        for key, value in receipt.model_dump(mode="json").items()
        if key != "receipt_id"
    }


def test_new_public_api_importable_from_package():
    assert PUBLIC_API_NAMES <= set(assurance.__all__)
    for name in PUBLIC_API_NAMES:
        assert hasattr(assurance, name)


def test_package_exports_preserve_prior_and_include_this_api_subset():
    current_public_names = set(assurance.__all__)
    assert PRIOR_PUBLIC_NAMES <= current_public_names
    assert PUBLIC_API_NAMES <= current_public_names


def test_module_ast_has_no_transport_execution_or_path_imports():
    source = Path(single_module.__file__).read_text(encoding="utf-8")
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


def test_module_ast_never_reads_environment_or_git():
    source = Path(single_module.__file__).read_text(encoding="utf-8")
    assert "environ" not in source
    assert "subprocess" not in source
    assert "getcwd" not in source
    assert "git" not in source.lower().replace("digest", "")


def test_rubric_version_and_hash_are_lowercase_sha256():
    assert single_module._RUBRIC_VERSION == "single_general.v0"
    assert single_module._RUBRIC_DIGEST.startswith("sha256:")
    assert len(single_module._RUBRIC_DIGEST) == 7 + 64
    assert single_module._RUBRIC_DIGEST[7:] == single_module._RUBRIC_DIGEST[
        7:
    ].lower()
    assert single_module._rubric_digest_from_table() == single_module._RUBRIC_DIGEST


def test_rubric_role_and_item_order_exact():
    roles = single_module._RUBRIC_TABLE["roles"]
    assert tuple(roles.keys()) == ("intent", "architecture", "operability")
    assert tuple(item["code"] for item in roles["intent"]["items"]) == (
        "SCOPE_ALIGNMENT",
        "ACCEPTANCE_NFR_COVERAGE",
    )
    assert tuple(item["code"] for item in roles["architecture"]["items"]) == (
        "BOUNDARY_DEPENDENCY_DIRECTION",
        "SECOND_SOURCE_DUPLICATION",
        "PUBLIC_CONTRACT_ADR",
    )
    assert tuple(item["code"] for item in roles["operability"]["items"]) == (
        "MIGRATION_ROLLBACK",
        "RETRY_IDEMPOTENCY_SIDE_EFFECTS",
        "OBSERVABILITY_KILL_SWITCH",
        "OWNERSHIP_RUNBOOK",
    )
    numbers = [
        item["number"]
        for role in roles.values()
        for item in role["items"]
    ]
    assert numbers == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_rubric_is_recursively_immutable():
    with pytest.raises(TypeError):
        single_module._RUBRIC_TABLE["roles"] = {}
    with pytest.raises(TypeError):
        single_module._RUBRIC_TABLE["roles"]["intent"] = {}
    with pytest.raises(TypeError):
        single_module._RUBRIC_TABLE["roles"]["intent"]["items"] = ()
    with pytest.raises(TypeError):
        single_module._RUBRIC_TABLE["roles"]["intent"]["items"][0] = {}


_RUBRIC_FIXED_DIGEST = (
    "sha256:7f1b7b9d1496869523946d2ccecc28c3d3db2f24f634060afc8d0a1893467515"
)
_RUBRIC_ROLES = ("intent", "architecture", "operability")


def test_rubric_item_fields_are_immutable():
    roles = single_module._RUBRIC_TABLE["roles"]
    for role in _RUBRIC_ROLES:
        items = roles[role]["items"]
        assert items
        for item in items:
            for key in ("number", "code", "name"):
                with pytest.raises(TypeError):
                    item[key] = "mutated"


def _assert_no_mutable_containers(value):
    if isinstance(value, (dict, list, set)):
        raise AssertionError(
            "mutable container " + type(value).__name__ + " in rubric item graph"
        )
    if isinstance(value, MappingProxyType):
        for child in value.values():
            _assert_no_mutable_containers(child)
    elif isinstance(value, tuple):
        for child in value:
            _assert_no_mutable_containers(child)
    elif isinstance(value, frozenset):
        for child in value:
            _assert_no_mutable_containers(child)


def test_rubric_item_graphs_have_no_mutable_containers():
    roles = single_module._RUBRIC_TABLE["roles"]
    items = [
        item
        for role in _RUBRIC_ROLES
        for item in roles[role]["items"]
    ]
    assert len(items) == 9
    for item in items:
        _assert_no_mutable_containers(item)


def test_rubric_prompt_reads_exact_table_objects(monkeypatch):
    roles = single_module._RUBRIC_TABLE["roles"]
    captured = {}
    original_sections_text = single_module._rubric_sections_text

    def spy():
        for role in _RUBRIC_ROLES:
            items = single_module._RUBRIC_TABLE["roles"][role]["items"]
            captured[role] = {"items": items, "objects": list(items)}
        return original_sections_text()

    monkeypatch.setattr(single_module, "_rubric_sections_text", spy)
    prompt = _prompt()
    assert "SCOPE_ALIGNMENT" in prompt.prompt_text
    for role in _RUBRIC_ROLES:
        table_items = roles[role]["items"]
        read_items = captured[role]["items"]
        read_objects = captured[role]["objects"]
        assert read_items is table_items
        assert len(read_objects) == len(table_items)
        for read_item, table_item in zip(read_objects, table_items):
            assert read_item is table_item


def test_rubric_digest_is_fixed_and_unchanged_after_mutation_attempts(tmp_path):
    assert single_module._RUBRIC_DIGEST == _RUBRIC_FIXED_DIGEST
    assert single_module._rubric_digest_from_table() == _RUBRIC_FIXED_DIGEST

    reviewer_input = _reviewer_input()
    before_prompt = SingleStrongReviewer.prepare(reviewer_input)
    store = ArtifactStore(tmp_path)
    before_result = _normalize(store, reviewer_input)

    roles = single_module._RUBRIC_TABLE["roles"]
    for role in _RUBRIC_ROLES:
        for item in roles[role]["items"]:
            for key in ("number", "code", "name"):
                with pytest.raises(TypeError):
                    item[key] = "mutated"

    after_prompt = SingleStrongReviewer.prepare(reviewer_input)
    after_result = _normalize(store, reviewer_input)

    assert single_module._RUBRIC_DIGEST == _RUBRIC_FIXED_DIGEST
    assert single_module._rubric_digest_from_table() == _RUBRIC_FIXED_DIGEST
    assert after_prompt == before_prompt
    assert after_result == before_result


def test_rubric_hash_and_prompt_read_same_table(monkeypatch):
    original_table = single_module._RUBRIC_TABLE
    original_digest = single_module._RUBRIC_DIGEST
    base = _prompt().prompt_text
    patched_items = (
        {"number": 1, "code": "SCOPE_ALIGNMENT", "name": "Scope alignment"},
        {"number": 2, "code": "ACCEPTANCE_NFR_COVERAGE", "name": "Acceptance"},
    )
    patched_roles = dict(original_table["roles"])
    patched_roles["intent"] = {"items": patched_items}
    patched_table = {
        "rubric_version": original_table["rubric_version"],
        "roles": patched_roles,
    }
    monkeypatch.setattr(single_module, "_RUBRIC_TABLE", patched_table)
    monkeypatch.setattr(
        single_module,
        "_RUBRIC_DIGEST",
        single_module._rubric_digest_from_table(),
    )
    patched = _prompt().prompt_text
    assert base != patched
    assert single_module._rubric_digest_from_table() == (
        single_module._RUBRIC_DIGEST
    )


def test_context_field_order_v1_frozen_extra():
    assert list(ReviewerEvidenceContext.model_fields) == [
        "schema_version",
        "evidence_id",
        "kind",
        "artifact_digest",
        "content",
        "content_digest",
        "truncated",
        "redaction_status",
    ]
    model = _context("ev-a", "git_snapshot", "content")
    assert model.schema_version == "v1"
    assert model.model_config["frozen"] is True
    assert model.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        ReviewerEvidenceContext.model_validate(
            {
                **model.model_dump(),
                "extra": 1,
            }
        )


def test_context_content_digest_and_limits():
    with pytest.raises(ValidationError):
        ReviewerEvidenceContext.model_validate(
            {
                "schema_version": "v1",
                "evidence_id": "ev-a",
                "kind": "kind",
                "artifact_digest": _digest("a"),
                "content": "content",
                "content_digest": _digest("a"),
                "truncated": False,
                "redaction_status": "not_applicable",
            }
        )
    good = _context("ev-a", "kind", "x" * 65535)
    assert len(good.content.encode("utf-8")) == 65535
    with pytest.raises(ValidationError):
        _context("ev-a", "kind", "x" * 65536)


def test_context_rejects_blank_bounded_strings_bad_digests_and_bad_literals():
    for field in ("evidence_id", "kind"):
        with pytest.raises(ValidationError):
            _context(" ", "kind", "c", redaction_status="not_applicable") if (
                field == "evidence_id"
            ) else _context("ev-a", " ", "c")
    with pytest.raises(ValidationError):
        _context("ev-a", "kind", "c", redaction_status="not_assessed")
    with pytest.raises(ValidationError):
        ReviewerEvidenceContext.model_validate(
            {
                "schema_version": "v1",
                "evidence_id": "ev-a",
                "kind": "kind",
                "artifact_digest": _digest("a"),
                "content": "c",
                "content_digest": _digest("a"),
                "truncated": 1,
                "redaction_status": "not_applicable",
            }
        )
    with pytest.raises(ValidationError):
        ReviewerEvidenceContext.model_validate(
            {
                "schema_version": "v1",
                "evidence_id": "ev-a",
                "kind": "kind",
                "artifact_digest": "sha256:XYZ",
                "content": "c",
                "content_digest": _sha256(b"c"),
                "truncated": False,
                "redaction_status": "not_applicable",
            }
        )


def test_context_round_trip_stable():
    model = _context("ev-a", "git_snapshot", "content \u4e2d\u6587")
    rebuilt = ReviewerEvidenceContext.model_validate_json(
        model.model_dump_json()
    )
    assert rebuilt == model
    assert rebuilt.model_dump() == model.model_dump()


def test_question_field_order_and_id_derivation():
    assert list(ReviewQuestion.model_fields) == [
        "schema_version",
        "subject_digest",
        "question_id",
        "reviewer_role",
        "question",
        "reason",
        "evidence_refs",
        "rubric_hash",
        "model_ref",
        "status",
    ]
    data = {
        "schema_version": "v1",
        "subject_digest": _digest("c"),
        "question_id": "rq_" + "0" * 32,
        "reviewer_role": "intent",
        "question": "what does acceptance cover?",
        "reason": "model_question",
        "evidence_refs": ("ev-b",),
        "rubric_hash": single_module._RUBRIC_DIGEST,
        "model_ref": "model/strong-1",
        "status": "open",
    }
    body = {
        key: value
        for key, value in data.items()
        if key != "question_id"
    }
    expected = "rq_" + hashlib.sha256(
        single_module._canonical_bytes(body)
    ).hexdigest()[:32]
    question = ReviewQuestion.model_validate(
        {**data, "question_id": expected}
    )
    assert question.question_id == expected
    assert question.status == "open"


def test_question_rejects_forged_id_blank_text_unsorted_dup_refs():
    base = {
        "schema_version": "v1",
        "subject_digest": _digest("c"),
        "question_id": "rq_" + "0" * 32,
        "reviewer_role": "intent",
        "question": "q",
        "reason": "model_question",
        "evidence_refs": ("ev-b",),
        "rubric_hash": single_module._RUBRIC_DIGEST,
        "model_ref": "model/strong-1",
        "status": "open",
    }
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate(base)
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate({**base, "question": " "})
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate(
            {**base, "evidence_refs": ("ev-b", "ev-b")}
        )
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate(
            {**base, "evidence_refs": ("ev-c", "ev-b")}
        )
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate({**base, "status": "closed"})
    with pytest.raises(ValidationError):
        ReviewQuestion.model_validate({**base, "reason": "unsupported_x"})


def test_single_reviewer_input_field_order_and_exact_nested_types():
    assert list(SingleReviewerInput.model_fields) == [
        "schema_version",
        "subject",
        "risk_result",
        "contexts",
        "evaluated_at",
    ]
    reviewer_input = _reviewer_input()
    assert type(reviewer_input.subject) is ChangeSubject
    assert type(reviewer_input.risk_result) is RiskClassificationResult
    for item in reviewer_input.contexts:
        assert type(item) is ReviewerEvidenceContext
    assert type(reviewer_input.contexts) is tuple
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.risk_result,
                "risk_result": reviewer_input.risk_result,
                "contexts": reviewer_input.contexts,
                "evaluated_at": FIXED_TIME,
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.subject,
                "contexts": reviewer_input.contexts,
                "evaluated_at": FIXED_TIME,
            }
        )


def test_single_reviewer_input_requires_exact_tuple_contexts_at_raw():
    reviewer_input = _reviewer_input()
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": list(reviewer_input.contexts),
                "evaluated_at": FIXED_TIME,
            }
        )


def test_single_reviewer_input_context_count_and_order():
    reviewer_input = _reviewer_input()
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": (),
                "evaluated_at": FIXED_TIME,
            }
        )
    many = tuple(
        _context(f"ev-{index:02d}", "kind", "small")
        for index in range(17)
    )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": many,
                "evaluated_at": FIXED_TIME,
            }
        )
    unsorted = tuple(
        sorted(reviewer_input.contexts, key=lambda item: -ord(item.evidence_id[-1]))
    )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": unsorted,
                "evaluated_at": FIXED_TIME,
            }
        )


def test_single_reviewer_input_aggregate_content_limit():
    reviewer_input = _reviewer_input()
    contexts = tuple(
        _context(f"ev-{index:02d}", "kind", "x" * 16384)
        for index in range(16)
    )
    assert sum(len(item.content.encode("utf-8")) for item in contexts) == 262144
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": contexts,
                "evaluated_at": FIXED_TIME,
            }
        )


def test_single_reviewer_input_subject_manifest_and_time_bindings():
    reviewer_input = _reviewer_input()
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": _subject(_digest("z")),
                "risk_result": reviewer_input.risk_result,
                "contexts": reviewer_input.contexts,
                "evaluated_at": FIXED_TIME,
            }
        )
    missing = _context("ev-zz", "kind", "content")
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": (missing,),
                "evaluated_at": FIXED_TIME,
            }
        )
    kind_mismatch = _context("ev-a", "different_kind", "content")
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": (kind_mismatch,),
                "evaluated_at": FIXED_TIME,
            }
        )
    digest_mismatch = _context(
        "ev-a",
        "git_snapshot",
        "content",
        artifact_digest=_digest("f"),
    )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": (digest_mismatch,),
                "evaluated_at": FIXED_TIME,
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": reviewer_input.contexts,
                "evaluated_at": EARLIER_TIME,
            }
        )
    late_manifest = _manifest(
        reviewer_input.subject.subject_digest,
        evaluated_at=LATER_TIME,
    )
    risk_input = _risk_input(
        reviewer_input.subject.subject_digest, manifest=late_manifest
    )
    risk_result = RiskClassifier.classify(risk_input)
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": risk_result,
                "contexts": reviewer_input.contexts,
                "evaluated_at": FIXED_TIME,
            }
        )


def test_single_reviewer_input_rejects_numeric_and_naive_datetimes():
    reviewer_input = _reviewer_input()
    for bad in (123456, 123456.0, "123456", True):
        with pytest.raises(ValidationError):
            SingleReviewerInput.model_validate(
                {
                    "schema_version": "v1",
                    "subject": reviewer_input.subject,
                    "risk_result": reviewer_input.risk_result,
                    "contexts": reviewer_input.contexts,
                    "evaluated_at": bad,
                }
            )
    with pytest.raises(ValidationError):
        SingleReviewerInput.model_validate(
            {
                "schema_version": "v1",
                "subject": reviewer_input.subject,
                "risk_result": reviewer_input.risk_result,
                "contexts": reviewer_input.contexts,
                "evaluated_at": datetime(2026, 8, 25, 8, 0, 0),
            }
        )


def test_single_reviewer_input_json_round_trip():
    reviewer_input = _reviewer_input()
    rebuilt = SingleReviewerInput.model_validate_json(
        reviewer_input.model_dump_json()
    )
    assert rebuilt == reviewer_input


def test_invocation_field_order_defaults_and_literals():
    assert list(SingleReviewerInvocation.model_fields) == [
        "schema_version",
        "run_id",
        "model_ref",
        "provider",
        "usage_status",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "started_at",
        "completed_at",
        "latency_ms",
        "timeout_seconds",
        "result",
        "schema_status",
        "fallback_reason",
        "tool_grants",
    ]
    invocation = _invocation()
    assert invocation.result == "success"
    assert invocation.fallback_reason is None
    assert invocation.tool_grants == ()


def test_invocation_measured_requires_all_usage_values():
    for overrides in (
        {"usage_status": "measured", "input_tokens": None},
        {"usage_status": "measured", "output_tokens": None},
        {"usage_status": "measured", "cost_usd": None},
        {"usage_status": "unavailable", "input_tokens": 0},
        {"usage_status": "unavailable", "output_tokens": 0},
        {"usage_status": "unavailable", "cost_usd": 0.0},
    ):
        with pytest.raises(ValidationError):
            _invocation(**overrides)
    unavailable = _invocation(
        usage_status="unavailable",
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
    )
    assert unavailable.input_tokens is None
    assert unavailable.output_tokens is None
    assert unavailable.cost_usd is None


def test_invocation_latency_is_truncated_milliseconds():
    started = FIXED_TIME
    completed = FIXED_TIME + timedelta(seconds=1, microseconds=234567)
    assert single_module._latency_ms(started, completed) == 1234
    assert _invocation(
        started_at=started, completed_at=completed, latency_ms=1234
    )
    with pytest.raises(ValidationError):
        _invocation(
            started_at=started, completed_at=completed, latency_ms=1235
        )
    with pytest.raises(ValidationError):
        _invocation(started_at=LATER_TIME, completed_at=FIXED_TIME)


def test_invocation_rejects_bool_strings_negative_nonfinite_and_tools():
    for overrides in (
        {"input_tokens": True},
        {"input_tokens": "1"},
        {"input_tokens": -1},
        {"output_tokens": False},
        {"output_tokens": 1.0},
        {"cost_usd": True},
        {"cost_usd": "0.5"},
        {"cost_usd": -0.01},
        {"cost_usd": float("inf")},
        {"latency_ms": "1"},
        {"latency_ms": -1},
        {"timeout_seconds": 0},
        {"tool_grants": ("bash_exec",)},
        {"fallback_reason": "degraded"},
    ):
        with pytest.raises(ValidationError):
            _invocation(**overrides)


def test_invocation_rejects_numeric_string_datetimes():
    for field in ("started_at", "completed_at"):
        with pytest.raises(ValidationError):
            _invocation(**{field: "1750000000"})
        with pytest.raises(ValidationError):
            _invocation(**{field: 1750000000})


def test_invocation_json_round_trip():
    invocation = _invocation()
    rebuilt = SingleReviewerInvocation.model_validate_json(
        invocation.model_dump_json()
    )
    assert rebuilt == invocation


def test_prompt_field_order_and_prepare_type_check():
    assert list(SingleReviewerPrompt.model_fields) == [
        "schema_version",
        "input",
        "rubric_version",
        "rubric_hash",
        "prompt_text",
        "prompt_digest",
        "prompt_id",
    ]
    reviewer_input = _reviewer_input()
    prompt = SingleStrongReviewer.prepare(reviewer_input)
    assert prompt.rubric_version == "single_general.v0"
    assert prompt.input == reviewer_input
    with pytest.raises(TypeError):
        SingleStrongReviewer.prepare(reviewer_input.model_dump())


def test_prompt_digest_and_id_derivation():
    prompt = _prompt()
    assert prompt.prompt_digest == single_module._sha256_digest(
        prompt.prompt_text.encode("utf-8")
    )
    expected_id = single_module._prompt_id_from_data(
        prompt.input.subject.subject_digest, prompt.prompt_digest
    )
    assert prompt.prompt_id == expected_id
    assert prompt.prompt_id.startswith("srp_")
    assert len(prompt.prompt_id) == 4 + 32


def test_prompt_contains_rubric_roles_schema_and_safety_rules():
    text = _prompt().prompt_text
    assert "SCOPE_ALIGNMENT" in text
    assert "ACCEPTANCE_NFR_COVERAGE" in text
    assert "BOUNDARY_DEPENDENCY_DIRECTION" in text
    assert "SECOND_SOURCE_DUPLICATION" in text
    assert "PUBLIC_CONTRACT_ADR" in text
    assert "MIGRATION_ROLLBACK" in text
    assert "RETRY_IDEMPOTENCY_SIDE_EFFECTS" in text
    assert "OBSERVABILITY_KILL_SWITCH" in text
    assert "OWNERSHIP_RUNBOOK" in text
    for role in ("INTENT", "ARCHITECTURE", "OPERABILITY"):
        assert role in text
    assert "RESPONSE_JSON_SCHEMA" in text
    assert "No tool grants" in text
    assert "no write grants" in text
    assert "Do not guess" in text
    assert "no hidden evidence" in text
    assert "must be recorded as a question" in text


def test_prompt_excludes_hidden_reference_labels():
    text = _prompt().prompt_text.lower()
    for label in ("gold", "expectation", "baseline", "fixture"):
        assert label not in text


def test_prompt_is_deterministic_and_context_sensitive():
    reviewer_input = _reviewer_input()
    first = SingleStrongReviewer.prepare(reviewer_input)
    second = SingleStrongReviewer.prepare(reviewer_input)
    assert first.prompt_text == second.prompt_text
    assert first.prompt_digest == second.prompt_digest
    assert first.prompt_id == second.prompt_id
    other = _reviewer_input(
        contexts=(
            _context("ev-a", "git_snapshot", "different git evidence"),
            _context("ev-b", "intake_documents", "intake documents evidence"),
            _context("ev-c", "command_batch", "command batch evidence"),
        )
    )
    changed = SingleStrongReviewer.prepare(other)
    assert changed.prompt_text != first.prompt_text
    assert changed.prompt_digest != first.prompt_digest
    assert changed.prompt_id != first.prompt_id


def test_prompt_rejects_forged_rubric_hash_digest_id_and_text():
    prompt = _prompt()
    with pytest.raises(ValidationError):
        SingleReviewerPrompt.model_validate(
            {
                **prompt.model_dump(),
                "rubric_hash": _digest("0"),
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerPrompt.model_validate(
            {
                **prompt.model_dump(),
                "prompt_digest": _digest("0"),
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerPrompt.model_validate(
            {
                **prompt.model_dump(),
                "prompt_id": "srp_" + "f" * 32,
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerPrompt.model_validate(
            {
                **prompt.model_dump(),
                "prompt_text": prompt.prompt_text + " forged",
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerPrompt.model_validate(
            {
                **prompt.model_dump(),
                "rubric_version": "single_other.v0",
            }
        )


def test_prompt_json_round_trip():
    prompt = _prompt()
    rebuilt = SingleReviewerPrompt.model_validate_json(
        prompt.model_dump_json()
    )
    assert rebuilt == prompt


def test_normalization_input_field_order_and_exact_nested_types():
    assert list(SingleReviewerNormalizationInput.model_fields) == [
        "schema_version",
        "reviewer_input",
        "prompt",
        "invocation",
        "raw_response",
    ]
    normalization_input = _normalization_input()
    assert type(normalization_input.reviewer_input) is SingleReviewerInput
    assert type(normalization_input.prompt) is SingleReviewerPrompt
    assert type(normalization_input.invocation) is SingleReviewerInvocation
    assert type(normalization_input.raw_response) is bytes


def test_normalization_input_requires_prompt_input_equality():
    reviewer_input = _reviewer_input()
    other = _reviewer_input(subject_digest=_digest("b"))
    prompt = SingleStrongReviewer.prepare(other)
    with pytest.raises(ValidationError):
        SingleReviewerNormalizationInput.model_validate(
            {
                "schema_version": "v1",
                "reviewer_input": reviewer_input,
                "prompt": prompt,
                "invocation": _invocation(),
                "raw_response": b"{}",
            }
        )


def test_normalization_input_rejects_wrong_bytes_type_and_size():
    normalization_input = _normalization_input()
    with pytest.raises(ValidationError):
        SingleReviewerNormalizationInput.model_validate(
            {
                **normalization_input.model_dump(),
                "raw_response": "text",
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerNormalizationInput.model_validate(
            {
                **normalization_input.model_dump(),
                "raw_response": b"",
            }
        )
    with pytest.raises(ValidationError):
        SingleReviewerNormalizationInput.model_validate(
            {
                **normalization_input.model_dump(),
                "raw_response": b" " * (1024 * 1024 + 1),
            }
        )


def test_normalize_valid_finding(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(),),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert type(finding) is Finding
    assert finding.subject_digest == reviewer_input.subject.subject_digest
    assert finding.reviewer_role == "architecture"
    assert finding.claim == "boundary direction must be explicit"
    assert finding.evidence_refs == ("ev-a",)
    assert finding.basis == "inferred"
    assert finding.severity == "medium"
    assert finding.confidence == 0.8
    assert finding.rubric_hash == single_module._RUBRIC_DIGEST
    assert finding.model_ref == "model/strong-1"
    assert finding.status == "open"
    assert len(result.questions) == 0


def test_normalize_accepts_integer_confidence_json_numbers(tmp_path):
    reviewer_input = _reviewer_input()
    for raw_confidence, expected in ((0, 0.0), (1, 1.0)):
        raw = _response_bytes(
            reviewer_input.subject.subject_digest,
            findings=(_valid_finding(confidence=raw_confidence),),
        )
        result = _normalize(
            ArtifactStore(tmp_path / str(raw_confidence)),
            reviewer_input,
            raw=raw,
        )
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert type(finding.confidence) is float
        assert finding.confidence == expected


def test_finding_draft_confidence_number_type_boundary():
    valid = _valid_finding()
    base = {**valid, "evidence_refs": tuple(valid["evidence_refs"])}
    for raw_confidence, expected in ((0, 0.0), (1, 1.0), (0.25, 0.25)):
        draft = single_module._FindingDraft.model_validate(
            {**base, "confidence": raw_confidence}
        )
        assert type(draft.confidence) is float
        assert draft.confidence == expected
    for rejected in (True, False, "0.5", Decimal("0.5")):
        with pytest.raises(ValidationError):
            single_module._FindingDraft.model_validate(
                {**base, "confidence": rejected}
            )


def test_normalize_unsupported_finding_downgrades_to_question(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(
            _valid_finding(evidence_refs=[]),
            _valid_finding(evidence_refs=["ev-zz"]),
            _valid_finding(
                claim="mixed refs claim", evidence_refs=["ev-a", "ev-zz"]
            ),
        ),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert len(result.findings) == 0
    assert len(result.questions) == 2
    empty = next(
        item for item in result.questions if item.question == "boundary direction must be explicit"
    )
    assert empty.reason == "unsupported_finding_evidence"
    assert empty.evidence_refs == ()
    mixed = next(
        item for item in result.questions if item.question == "mixed refs claim"
    )
    assert mixed.reason == "unsupported_finding_evidence"
    assert mixed.evidence_refs == ("ev-a",)


def test_normalize_model_question_keeps_only_valid_refs(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        questions=(
            _valid_question(),
            _valid_question(
                question="which context is relevant?",
                evidence_refs=["ev-a", "ev-zz", "ev-c"],
            ),
        ),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert len(result.questions) == 2
    default_question = next(
        item
        for item in result.questions
        if item.question == "what does acceptance coverage include?"
    )
    mixed_question = next(
        item
        for item in result.questions
        if item.question == "which context is relevant?"
    )
    assert default_question.evidence_refs == ("ev-b",)
    assert mixed_question.evidence_refs == ("ev-a", "ev-c")


def test_normalize_dedupes_identical_records(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(), _valid_finding()),
        questions=(_valid_question(), _valid_question()),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert len(result.findings) == 1
    assert len(result.questions) == 1


@pytest.mark.parametrize(
    ("section", "draft_type", "factory"),
    (
        ("findings", single_module._FindingDraft, _valid_finding),
        ("questions", single_module._QuestionDraft, _valid_question),
    ),
)
def test_normalize_rejects_duplicate_evidence_refs_in_finding_and_question(
    tmp_path, section, draft_type, factory
):
    reviewer_input = _reviewer_input()
    duplicate = factory(evidence_refs=["ev-a", "ev-a"])

    with pytest.raises(ValidationError):
        draft_type.model_validate_json(json.dumps(duplicate))

    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        **{section: (duplicate,)},
    )
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)


def test_normalize_same_claim_different_role_or_refs_stays_distinct(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(
            _valid_finding(claim="same claim", evidence_refs=["ev-a"]),
            _valid_finding(
                claim="same claim", evidence_refs=["ev-b"], reviewer_role="intent"
            ),
            _valid_finding(
                claim="same claim", evidence_refs=["ev-a", "ev-b"]
            ),
        ),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert len(result.findings) == 3
    assert len({item.finding_id for item in result.findings}) == 3


def test_normalize_permutation_shares_canonical_digest(tmp_path):
    reviewer_input = _reviewer_input()
    first_raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(),),
        questions=(_valid_question(),),
    )
    second_raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(claim="other"),),
        questions=(_valid_question(question="other question"),),
    )
    first = _normalize(ArtifactStore(tmp_path / "one"), reviewer_input, raw=first_raw)
    second = _normalize(
        ArtifactStore(tmp_path / "two"), reviewer_input, raw=second_raw
    )
    assert first.raw_response_artifact_digest != second.raw_response_artifact_digest
    assert first.canonical_response_digest != second.canonical_response_digest

    swapped_raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(claim="other"),),
        questions=(_valid_question(question="other question"),),
    )
    permuted_raw = json.dumps(
        {
            "schema_version": "v1",
            "subject_digest": reviewer_input.subject.subject_digest,
            "rubric_hash": single_module._RUBRIC_DIGEST,
            "questions": [_valid_question(question="other question")],
            "findings": [_valid_finding(claim="other")],
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert swapped_raw != permuted_raw
    swapped = _normalize(
        ArtifactStore(tmp_path / "three"), reviewer_input, raw=swapped_raw
    )
    permuted = _normalize(
        ArtifactStore(tmp_path / "four"), reviewer_input, raw=permuted_raw
    )
    assert swapped.raw_response_artifact_digest != permuted.raw_response_artifact_digest
    assert swapped.canonical_response_digest == permuted.canonical_response_digest


def test_normalize_rejects_too_many_response_items(tmp_path):
    reviewer_input = _reviewer_input()
    findings = tuple(
        _valid_finding(claim=f"claim {index}") for index in range(200)
    )
    questions = tuple(
        _valid_question(question=f"question {index}") for index in range(57)
    )
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=findings,
        questions=questions,
    )
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)


def test_normalize_rejects_unknown_missing_wrong_fields(tmp_path):
    reviewer_input = _reviewer_input()
    subject_digest = reviewer_input.subject.subject_digest
    base = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "rubric_hash": single_module._RUBRIC_DIGEST,
        "findings": [],
        "questions": [],
    }
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(
            ArtifactStore(tmp_path),
            reviewer_input,
            raw=json.dumps({**base, "extra": 1}).encode("utf-8"),
        )
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(
            ArtifactStore(tmp_path / "m"),
            reviewer_input,
            raw=json.dumps({key: value for key, value in base.items() if key != "findings"}).encode(
                "utf-8"
            ),
        )
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(
            ArtifactStore(tmp_path / "s"),
            reviewer_input,
            raw=json.dumps({**base, "schema_version": "v2"}).encode("utf-8"),
        )


def test_normalize_subject_and_rubric_bindings(tmp_path):
    reviewer_input = _reviewer_input()
    with pytest.raises(SingleReviewerSubjectMismatchError):
        _normalize(
            ArtifactStore(tmp_path),
            reviewer_input,
            raw=_response_bytes(_digest("a")),
        )
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(
            ArtifactStore(tmp_path / "r"),
            reviewer_input,
            raw=_response_bytes(
                reviewer_input.subject.subject_digest,
                rubric_hash=_digest("0"),
            ),
        )


def test_normalize_rejects_json_attacks(tmp_path):
    reviewer_input = _reviewer_input()
    subject_digest = reviewer_input.subject.subject_digest
    raw_valid = _response_bytes(subject_digest)
    cases = [
        b"\xef\xbb\xbf" + raw_valid,
        raw_valid.replace(b'"findings"', b'"findings\x00"'),
        b"\xff\xfe{}",
        b"[1, 2, 3]",
        b'{"schema_version":"v1","schema_version":"v1","subject_digest":"'
        + subject_digest.encode()
        + b'","rubric_hash":"'
        + single_module._RUBRIC_DIGEST.encode()
        + b'","findings":[],"questions":[]}',
        b'{"schema_version":"v1","subject_digest":"'
        + subject_digest.encode()
        + b'","rubric_hash":"'
        + single_module._RUBRIC_DIGEST.encode()
        + b'","findings":[{"confidence":NaN}],"questions":[]}',
    ]
    for index, raw in enumerate(cases):
        with pytest.raises(SingleReviewerPayloadError):
            _normalize(
                ArtifactStore(tmp_path / str(index)),
                reviewer_input,
                raw=raw,
            )


def test_normalize_rejects_depth_beyond_64(tmp_path):
    reviewer_input = _reviewer_input()
    payload = {"findings": []}
    for _ in range(70):
        payload = {"nested": payload}
    raw = json.dumps(payload).encode("utf-8")
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)


def test_normalize_rejects_node_count_beyond_4096(tmp_path):
    reviewer_input = _reviewer_input()
    raw = json.dumps({"values": [0] * 5000}).encode("utf-8")
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)


def test_normalize_rejects_invalid_draft_fields(tmp_path):
    reviewer_input = _reviewer_input()
    subject_digest = reviewer_input.subject.subject_digest
    finding = _valid_finding()
    for overrides in (
        {"reviewer_role": "planner"},
        {"claim": " "},
        {"severity": "urgent"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"confidence": "0.5"},
        {"confidence": True},
        {"evidence_refs": [" "]},
    ):
        raw = _response_bytes(
            subject_digest, findings=({**finding, **overrides},)
        )
        with pytest.raises(SingleReviewerPayloadError):
            _normalize(
                ArtifactStore(tmp_path),
                reviewer_input,
                raw=raw,
            )


def test_normalize_persists_raw_artifact_before_parse_and_keeps_on_failure(
    tmp_path,
):
    reviewer_input = _reviewer_input()
    store = ArtifactStore(tmp_path)
    raw = b'{"broken": '
    with pytest.raises(SingleReviewerPayloadError):
        _normalize(store, reviewer_input, raw=raw)
    digest = single_module._sha256_digest(raw)
    assert store.exists(digest) is True
    assert store.get_bytes(digest) == raw
    assert sum(
        1 for item in (tmp_path / "sha256").rglob("*") if item.is_file()
    ) == 1


def test_normalize_pre_persistence_failures_do_not_grow_store(tmp_path):
    reviewer_input = _reviewer_input()
    store = ArtifactStore(tmp_path)
    prompt = SingleStrongReviewer.prepare(reviewer_input)
    invocation = _invocation()

    oversized = SingleReviewerNormalizationInput.model_construct(
        schema_version="v1",
        reviewer_input=reviewer_input,
        prompt=prompt,
        invocation=invocation,
        raw_response=b" " * (1024 * 1024 + 1),
    )
    with pytest.raises(SingleReviewerPayloadError):
        SingleStrongReviewer.normalize(oversized, store)
    assert not (tmp_path / "sha256").exists() or not list(
        (tmp_path / "sha256").rglob("*")
    )
    empty = SingleReviewerNormalizationInput.model_construct(
        schema_version="v1",
        reviewer_input=reviewer_input,
        prompt=prompt,
        invocation=invocation,
        raw_response=b"",
    )
    with pytest.raises(SingleReviewerPayloadError):
        SingleStrongReviewer.normalize(empty, store)
    assert not list((tmp_path / "sha256").rglob("*"))


def test_normalize_artifact_failures_are_sanitized(tmp_path, monkeypatch):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(reviewer_input.subject.subject_digest)
    store = ArtifactStore(tmp_path)

    def boom_put(data):
        raise RuntimeError("put exploded")

    monkeypatch.setattr(store, "put_bytes", boom_put)
    with pytest.raises(SingleReviewerArtifactError) as exc:
        _normalize(store, reviewer_input, raw=raw)
    assert exc.value.__cause__ is None
    assert str(exc.value) == single_module._ARTIFACT_ERROR_MESSAGE

    store = ArtifactStore(tmp_path / "verify")

    def false_verify(digest):
        return False

    monkeypatch.setattr(store, "verify", false_verify)
    with pytest.raises(SingleReviewerArtifactError):
        _normalize(store, reviewer_input, raw=raw)
    digest = single_module._sha256_digest(raw)
    assert store.exists(digest) is True
    assert store.get_bytes(digest) == raw

    store = ArtifactStore(tmp_path / "get")

    def wrong_get(digest):
        return b"other bytes"

    monkeypatch.setattr(store, "get_bytes", wrong_get)
    with pytest.raises(SingleReviewerArtifactError):
        _normalize(store, reviewer_input, raw=raw)

    store = ArtifactStore(tmp_path / "corrupt")

    def corrupt_get(digest):
        raise ArtifactIntegrityError("corrupt")

    monkeypatch.setattr(store, "get_bytes", corrupt_get)
    with pytest.raises(SingleReviewerArtifactError):
        _normalize(store, reviewer_input, raw=raw)

    store = ArtifactStore(tmp_path / "missing")

    def missing_get(digest):
        raise ArtifactNotFoundError(digest)

    monkeypatch.setattr(store, "get_bytes", missing_get)
    with pytest.raises(SingleReviewerArtifactError):
        _normalize(store, reviewer_input, raw=raw)

    store = ArtifactStore(tmp_path / "mismatch")

    def wrong_put(data):
        return _digest("0")

    monkeypatch.setattr(store, "put_bytes", wrong_put)
    with pytest.raises(SingleReviewerArtifactError):
        _normalize(store, reviewer_input, raw=raw)


def test_normalize_normalization_input_type_checks(tmp_path):
    with pytest.raises(TypeError):
        SingleStrongReviewer.normalize("not an input", ArtifactStore(tmp_path))
    with pytest.raises(TypeError):
        SingleStrongReviewer.normalize(_normalization_input(), None)


def test_error_messages_never_contain_payload_markers(tmp_path, monkeypatch):
    reviewer_input = _reviewer_input()
    marker = "SECRET_CONTENT_MARKER_42"
    raw = (
        b'{"schema_version":"v1","subject_digest":"'
        + reviewer_input.subject.subject_digest.encode()
        + b'","rubric_hash":"'
        + single_module._RUBRIC_DIGEST.encode()
        + b'","findings":[{"reviewer_role":"intent","claim":"'
        + marker.encode()
        + b'","evidence_refs":[],"severity":"low","confidence":0.5,"extra":1}],'
        b'"questions":[]}'
    )
    with pytest.raises(SingleReviewerPayloadError) as exc:
        _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    assert marker not in str(exc.value)
    assert str(exc.value) == single_module._PAYLOAD_ERROR_MESSAGE

    marker_raw = (
        b'{"schema_version":"v1","subject_digest":"'
        + reviewer_input.subject.subject_digest.encode()
        + b'","rubric_hash":"'
        + single_module._RUBRIC_DIGEST.encode()
        + b'","findings":[{"reviewer_role":"intent","claim":"'
        + marker.encode()
        + b'","evidence_refs":[],"severity":"low","confidence":0.5}],'
        b'"questions":[]}'
    )
    store = ArtifactStore(tmp_path / "marker")
    monkeypatch.setattr(store, "get_bytes", lambda digest: b"tampered")
    with pytest.raises(SingleReviewerArtifactError) as exc:
        _normalize(store, reviewer_input, raw=marker_raw)
    assert marker not in str(exc.value)
    assert str(exc.value) == single_module._ARTIFACT_ERROR_MESSAGE


def test_receipt_has_exactly_three_shared_invocation_steps(tmp_path):
    reviewer_input = _reviewer_input()
    invocation = _invocation()
    result = _normalize(ArtifactStore(tmp_path), reviewer_input)
    receipt = result.execution_receipt
    assert type(receipt) is ExecutionReceipt
    assert [step.sequence for step in receipt.steps] == [0, 1, 2]
    assert tuple(step.planned_role for step in receipt.steps) == (
        "intent",
        "architecture",
        "operability",
    )
    assert tuple(step.actual_role for step in receipt.steps) == (
        "intent",
        "architecture",
        "operability",
    )
    for step in receipt.steps:
        assert type(step) is ExecutionStep
        assert step.model_ref == invocation.model_ref
        assert step.provider == invocation.provider
        assert step.routing_rule == "single_general.v0:shared_invocation"
        assert step.tool_grants == ()
        assert step.fallback_reason is None
        assert step.token_budget is None
        assert step.timeout_seconds == invocation.timeout_seconds
        assert step.result == "success"
        assert step.schema_status == invocation.schema_status
    assert receipt.overall_result == "success"
    assert receipt.run_id == invocation.run_id
    assert receipt.started_at == invocation.started_at
    assert receipt.completed_at == invocation.completed_at
    assert receipt.subject_digest == reviewer_input.subject.subject_digest


def test_receipt_usage_recorded_once_not_multiplied(tmp_path):
    reviewer_input = _reviewer_input()
    measured = _invocation()
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, invocation=measured)
    receipt = result.execution_receipt
    assert receipt.input_tokens == 120
    assert receipt.output_tokens == 80
    assert receipt.cost_usd == 0.0125
    unavailable = _invocation(
        usage_status="unavailable",
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
    )
    result = _normalize(
        ArtifactStore(tmp_path / "u"),
        reviewer_input,
        invocation=unavailable,
    )
    receipt = result.execution_receipt
    assert receipt.input_tokens == 0
    assert receipt.output_tokens == 0
    assert receipt.cost_usd == 0.0


def test_receipt_id_derivation(tmp_path):
    result = _normalize(ArtifactStore(tmp_path))
    receipt = result.execution_receipt
    expected = "exr_" + hashlib.sha256(
        single_module._canonical_bytes(_receipt_id_data(receipt))
    ).hexdigest()[:32]
    assert receipt.receipt_id == expected
    assert len(receipt.receipt_id) == 4 + 32


def test_result_fields_and_digest_id_derivation(tmp_path):
    result = _normalize(ArtifactStore(tmp_path))
    assert list(SingleReviewerResult.model_fields) == [
        "schema_version",
        "input",
        "raw_response_artifact_digest",
        "canonical_response_digest",
        "findings",
        "questions",
        "execution_receipt",
        "result_digest",
        "result_id",
    ]
    assert type(result.input) is SingleReviewerNormalizationInput
    assert result.raw_response_artifact_digest == single_module._sha256_digest(
        result.input.raw_response
    )
    body = single_module._result_digest_body(result)
    expected_digest = single_module._sha256_digest(
        single_module._canonical_bytes(body)
    )
    expected_id = "srr_" + hashlib.sha256(
        single_module._canonical_bytes(body)
    ).hexdigest()[:32]
    assert result.result_digest == expected_digest
    assert result.result_id == expected_id
    assert len(result.result_id) == 4 + 32


def test_result_is_deterministic_and_idempotent(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(),),
    )
    first = _normalize(ArtifactStore(tmp_path / "a"), reviewer_input, raw=raw)
    second = _normalize(ArtifactStore(tmp_path / "b"), reviewer_input, raw=raw)
    assert first.model_dump() == second.model_dump()
    rebuilt = SingleReviewerResult.model_validate(
        {
            "schema_version": "v1",
            "input": first.input,
            "raw_response_artifact_digest": first.raw_response_artifact_digest,
            "canonical_response_digest": first.canonical_response_digest,
            "findings": first.findings,
            "questions": first.questions,
            "execution_receipt": first.execution_receipt,
            "result_digest": first.result_digest,
            "result_id": first.result_id,
        }
    )
    assert rebuilt == first


def test_result_rejects_synchronized_forgeries(tmp_path):
    reviewer_input = _reviewer_input()
    raw = _response_bytes(
        reviewer_input.subject.subject_digest,
        findings=(_valid_finding(),),
    )
    result = _normalize(ArtifactStore(tmp_path), reviewer_input, raw=raw)
    base = {
        "schema_version": "v1",
        "input": result.input,
        "raw_response_artifact_digest": result.raw_response_artifact_digest,
        "canonical_response_digest": result.canonical_response_digest,
        "findings": result.findings,
        "questions": result.questions,
        "execution_receipt": result.execution_receipt,
        "result_digest": result.result_digest,
        "result_id": result.result_id,
    }
    forged_finding = result.findings[0].model_copy(
        update={"claim": "forged claim"}
    )
    forged_question = single_module._question_from_fields(
        subject_digest=reviewer_input.subject.subject_digest,
        reviewer_role="intent",
        question="forged question",
        reason="model_question",
        refs=(),
        model_ref="model/strong-1",
    )
    forged_receipt = result.execution_receipt.model_copy(
        update={"run_id": "forged-run"}
    )
    for overrides in (
        {"raw_response_artifact_digest": _digest("0")},
        {"canonical_response_digest": _digest("0")},
        {"findings": (forged_finding,)},
        {"questions": (forged_question,)},
        {"execution_receipt": forged_receipt},
        {"result_digest": _digest("0")},
        {"result_id": "srr_" + "f" * 32},
    ):
        with pytest.raises(ValidationError):
            SingleReviewerResult.model_validate({**base, **overrides})


def test_result_rejects_wrong_nested_types(tmp_path):
    result = _normalize(ArtifactStore(tmp_path))
    base = {
        "schema_version": "v1",
        "input": result.input,
        "raw_response_artifact_digest": result.raw_response_artifact_digest,
        "canonical_response_digest": result.canonical_response_digest,
        "findings": result.findings,
        "questions": result.questions,
        "execution_receipt": result.execution_receipt,
        "result_digest": result.result_digest,
        "result_id": result.result_id,
    }
    with pytest.raises(ValidationError):
        SingleReviewerResult.model_validate(
            {**base, "findings": ("not-a-finding",)}
        )
    with pytest.raises(ValidationError):
        SingleReviewerResult.model_validate(
            {**base, "questions": [dict(base["questions"])]}
        )
    with pytest.raises(ValidationError):
        SingleReviewerResult.model_validate(
            {**base, "execution_receipt": result.execution_receipt.model_dump()}
        )


def test_error_hierarchy():
    assert issubclass(SingleReviewerPayloadError, SingleReviewerError)
    assert issubclass(SingleReviewerSubjectMismatchError, SingleReviewerError)
    assert issubclass(SingleReviewerArtifactError, SingleReviewerError)
    assert issubclass(SingleReviewerError, Exception)
