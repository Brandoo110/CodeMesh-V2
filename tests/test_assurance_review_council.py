"""V2-P4-05 Review Council parallel-isolation focused tests."""

import ast
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import assurance
import assurance.review_council as council_module
from assurance import (
    ChangeSubject,
    CouncilCancellation,
    CouncilEvidenceIndexEntry,
    CouncilPlan,
    CouncilRunResult,
    FindingOutput,
    ReviewerEvidenceContext,
    ReviewerFailureOutcome,
    ReviewerInput,
    ReviewQuestion,
    ReviewCouncil,
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
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)
EARLIER_TIME = datetime(2026, 8, 25, 7, 0, 0, tzinfo=timezone.utc)

ROLE_ORDER = ("intent", "architecture", "operability")

PUBLIC_API_NAMES = frozenset(
    {
        "CouncilPlan",
        "CouncilEvidenceIndexEntry",
        "CouncilRunResult",
        "CouncilCancellation",
        "ReviewCouncil",
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


def _context(evidence_id, kind, content):
    artifact_digest = _sha256(content.encode("utf-8"))
    return ReviewerEvidenceContext.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": evidence_id,
            "kind": kind,
            "artifact_digest": artifact_digest,
            "content": content,
            "content_digest": _sha256(content.encode("utf-8")),
            "truncated": False,
            "redaction_status": "not_applicable",
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
                        "fresh" if evaluated_at <= LATER_TIME else "stale"
                    ),
                    "redaction_status": item.redaction_status,
                }
            )
        )
    entries = tuple(sorted(entries, key=lambda entry: entry.evidence_id))
    values = {
        "schema_version": "v1",
        "manifest_id": "em_" + "0" * 32,
        "subject_digest": subject_digest,
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "evidence_count": len(entries),
        "completeness_status": "complete",
        "has_incomplete_evidence": False,
        "has_stale_evidence": False,
        "has_unknown_freshness": False,
        "has_unredacted_content": False,
        "has_unassessed_redaction": False,
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


def _role_contexts(role):
    all_contexts = _default_contexts()
    if role == "intent":
        return (all_contexts[0], all_contexts[1])
    if role == "architecture":
        return (all_contexts[0], all_contexts[2])
    return all_contexts


def _reviewer_input_values(
    role,
    *,
    subject=None,
    risk_result=None,
    contexts=None,
    requested_at=FIXED_TIME,
    **overrides,
):
    if subject is None:
        subject = _subject()
    if contexts is None:
        contexts = _role_contexts(role)
    if risk_result is None:
        manifest = _manifest(subject.subject_digest, _default_contexts())
        risk_result = _risk_result(subject.subject_digest, manifest=manifest)
    role_specific = {
        "intent": {
            "rubric_version": "intent.v0",
            "rubric_hash": _digest("1"),
            "tool_allowlist": ("read", "search"),
            "timeout_seconds": 30,
            "token_budget": 8_000,
            "cost_budget_usd": 1.0,
        },
        "architecture": {
            "rubric_version": "architecture.v0",
            "rubric_hash": _digest("2"),
            "tool_allowlist": ("read", "search", "trace"),
            "timeout_seconds": 30,
            "token_budget": 12_000,
            "cost_budget_usd": 2.0,
        },
        "operability": {
            "rubric_version": "operability.v0",
            "rubric_hash": _digest("3"),
            "tool_allowlist": ("read", "search", "trace", "verify"),
            "timeout_seconds": 30,
            "token_budget": 16_000,
            "cost_budget_usd": 3.0,
        },
    }[role]
    values = {
        "schema_version": "v1",
        "reviewer_role": role,
        "subject": subject,
        "risk_result": risk_result,
        "contexts": contexts,
        "evidence_allowlist": tuple(
            item.evidence_id for item in contexts
        ),
        "requested_at": requested_at,
        **role_specific,
    }
    values.update(overrides)
    return values


def _reviewer_input(role, **overrides):
    return ReviewerInput.model_validate(
        _reviewer_input_values(role, **overrides)
    )


def _default_inputs(**overrides):
    return tuple(
        _reviewer_input(role, **overrides.get(role, {}))
        for role in ROLE_ORDER
    )


def _plan(inputs=None):
    if inputs is None:
        inputs = _default_inputs()
    return CouncilPlan.model_validate(
        {"schema_version": "v1", "inputs": inputs}
    )


def _finding(input_, **overrides):
    values = {
        "schema_version": "v1",
        "finding_id": "fnd-a",
        "subject_digest": input_.subject.subject_digest,
        "reviewer_role": input_.reviewer_role,
        "claim": "boundary direction must be explicit",
        "evidence_refs": (input_.evidence_allowlist[0],),
        "basis": "inferred",
        "severity": "medium",
        "confidence": 0.8,
        "rubric_hash": input_.rubric_hash,
        "model_ref": "reviewer-strong-1",
        "status": "open",
    }
    values.update(overrides)
    return FindingModel.model_validate(values)


def _question(input_, **overrides):
    values = {
        "schema_version": "v1",
        "subject_digest": input_.subject.subject_digest,
        "reviewer_role": input_.reviewer_role,
        "question": "what does acceptance coverage include?",
        "reason": "model_question",
        "evidence_refs": (input_.evidence_allowlist[0],),
        "rubric_hash": input_.rubric_hash,
        "model_ref": "reviewer-strong-1",
        "status": "open",
    }
    values.update(overrides)
    body = {
        key: value
        for key, value in values.items()
        if key != "question_id"
    }
    question_id = "rq_" + hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
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


def _finding_output(
    input_,
    *,
    outcome="success",
    findings=(),
    questions=(),
    failure=None,
    completed_at=LATER_TIME,
    **overrides,
):
    values = {
        "schema_version": "v1",
        "input": input_,
        "outcome": outcome,
        "findings": findings,
        "questions": questions,
        "failure": failure,
        "completed_at": completed_at,
    }
    values.update(overrides)
    return FindingOutput.model_validate(values)


def _success_output(input_, findings=(), questions=()):
    return _finding_output(input_, findings=findings, questions=questions)


class _CountingClock:
    def __init__(self, stamp=LATER_TIME):
        self.stamp = stamp
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.stamp


async def _success_runner(input_):
    return _success_output(input_)


def _runners():
    return {role: _success_runner for role in ROLE_ORDER}


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


async def _pending_noncurrent_tasks():
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


def test_council_plan_three_role_canonical_and_json_round_trip():
    inputs = _default_inputs()
    plan = _plan(inputs)
    assert type(plan.inputs) is tuple
    assert tuple(item.reviewer_role for item in plan.inputs) == ROLE_ORDER
    assert plan.schema_version == "v1"
    for item in plan.inputs:
        assert type(item) is ReviewerInput
    rebuilt = CouncilPlan.model_validate_json(plan.model_dump_json())
    assert rebuilt == plan
    assert tuple(item.reviewer_role for item in rebuilt.inputs) == ROLE_ORDER


def test_council_plan_rejects_missing_duplicate_extra_or_out_of_order():
    inputs = _default_inputs()
    with pytest.raises(ValidationError):
        _plan(inputs[:2])
    duplicate = (inputs[0], inputs[0], inputs[2])
    with pytest.raises(ValidationError):
        _plan(duplicate)
    with pytest.raises(ValidationError):
        _plan(inputs + (inputs[0],))
    with pytest.raises(ValidationError):
        _plan((inputs[0], inputs[2], inputs[1]))
    with pytest.raises(ValidationError):
        CouncilPlan.model_validate(
            {"schema_version": "v1", "inputs": inputs, "extra": True}
        )
    with pytest.raises(ValidationError):
        CouncilPlan.model_validate(
            {"schema_version": "v2", "inputs": inputs}
        )
    with pytest.raises(ValidationError):
        CouncilPlan.model_validate(
            {"schema_version": "v1", "inputs": list(inputs)}
        )
    with pytest.raises(ValidationError):
        CouncilPlan.model_validate(
            {
                "schema_version": "v1",
                "inputs": tuple(
                    item.model_dump(mode="json") for item in inputs
                ),
            }
        )


def test_council_plan_rejects_different_subject_risk_or_requested_at():
    inputs = _default_inputs()
    other_subject = _subject(_digest("f"))
    different_subject = _default_inputs()
    different_subject = tuple(
        _reviewer_input(
            role,
            subject=other_subject,
            risk_result=_risk_result(other_subject.subject_digest),
        )
        if role == "intent"
        else item
        for role, item in zip(ROLE_ORDER, different_subject)
    )
    with pytest.raises(ValidationError):
        _plan(different_subject)

    extra_contexts = _default_contexts() + (
        _context("ev-d", "log_evidence", "extra evidence for a different risk"),
    )
    other_manifest = _manifest(_digest("c"), extra_contexts)
    other_risk = _risk_result(_digest("c"), manifest=other_manifest)
    different_risk = (
        _reviewer_input("intent", risk_result=other_risk),
        inputs[1],
        inputs[2],
    )
    with pytest.raises(ValidationError):
        _plan(different_risk)

    different_time = (
        _reviewer_input("intent", requested_at=LATER_TIME),
        inputs[1],
        inputs[2],
    )
    with pytest.raises(ValidationError):
        _plan(different_time)


def test_council_plan_role_specific_allowlists_and_budgets_stay_isolated():
    plan = _plan()
    intent, architecture, operability = plan.inputs
    assert intent.evidence_allowlist == ("ev-a", "ev-b")
    assert architecture.evidence_allowlist == ("ev-a", "ev-c")
    assert operability.evidence_allowlist == ("ev-a", "ev-b", "ev-c")
    assert intent.tool_allowlist == ("read", "search")
    assert architecture.tool_allowlist == ("read", "search", "trace")
    assert operability.tool_allowlist == (
        "read",
        "search",
        "trace",
        "verify",
    )
    assert intent.rubric_version != architecture.rubric_version
    assert architecture.rubric_version != operability.rubric_version
    assert len({intent.rubric_hash, architecture.rubric_hash}) == 2
    assert len(
        {
            intent.rubric_hash,
            architecture.rubric_hash,
            operability.rubric_hash,
        }
    ) == 3
    assert len({intent.token_budget, architecture.token_budget}) == 2
    assert len(
        {
            intent.token_budget,
            architecture.token_budget,
            operability.token_budget,
        }
    ) == 3


@pytest.mark.asyncio
async def test_start_barrier_proves_all_three_callbacks_started_before_submit():
    plan = _plan()
    started = []
    release = asyncio.Event()

    async def barrier_runner(input_):
        started.append(input_.reviewer_role)
        if len(started) == 3:
            release.set()
        await release.wait()
        return _success_output(input_)

    runners = {role: barrier_runner for role in ROLE_ORDER}
    clock = _CountingClock()
    result = await asyncio.wait_for(
        ReviewCouncil().run(plan, runners, clock), timeout=3
    )
    assert len(started) == 3
    assert set(started) == set(ROLE_ORDER)
    assert tuple(item.input.reviewer_role for item in result.outputs) == (
        ROLE_ORDER
    )
    assert clock.calls == 0


@pytest.mark.asyncio
async def test_varied_completion_order_returns_canonical_order():
    plan = _plan()
    completion = []

    async def runner(input_, delay):
        await asyncio.sleep(delay)
        completion.append(input_.reviewer_role)
        return _success_output(input_)

    runners = {
        "intent": lambda input_: runner(input_, 0.05),
        "architecture": lambda input_: runner(input_, 0.01),
        "operability": lambda input_: runner(input_, 0.03),
    }
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert tuple(item.input.reviewer_role for item in result.outputs) == (
        ROLE_ORDER
    )
    assert completion != list(ROLE_ORDER)


@pytest.mark.asyncio
async def test_callback_exception_becomes_generic_failure_and_others_complete():
    plan = _plan()
    secret = "secret-token-abc-123"

    async def failing_runner(input_):
        raise RuntimeError(secret)

    runners = _runners()
    runners["architecture"] = failing_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    failure = result.outputs[1]
    assert failure.outcome == "failure"
    assert failure.failure is not None
    assert failure.failure.code == "execution_failed"
    assert secret not in failure.failure.details
    assert "RuntimeError" not in failure.failure.details
    assert failure.findings == ()
    assert failure.questions == ()
    assert failure.completed_at >= failure.input.requested_at
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_timeout_becomes_timeout_while_other_roles_complete():
    inputs = _default_inputs(
        architecture={"timeout_seconds": 1},
    )
    plan = _plan(inputs)
    never = asyncio.Event()

    async def waiting_runner(input_):
        await never.wait()
        return _success_output(input_)

    runners = _runners()
    runners["architecture"] = waiting_runner
    result = await asyncio.wait_for(
        ReviewCouncil().run(plan, runners, _CountingClock()), timeout=5
    )
    timeout_output = result.outputs[1]
    assert timeout_output.outcome == "timeout"
    assert timeout_output.failure is not None
    assert timeout_output.failure.code == "timeout"
    assert timeout_output.findings == ()
    assert timeout_output.questions == ()
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_live_role_cancellation_cancels_only_that_callback():
    plan = _plan()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    control = CouncilCancellation()

    async def cancellable_runner(input_):
        try:
            started.set()
            await asyncio.Event().wait()
            return _success_output(input_)
        finally:
            cancelled.set()

    runners = _runners()
    runners["architecture"] = cancellable_runner
    task = asyncio.create_task(
        ReviewCouncil().run(plan, runners, _CountingClock(), control)
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    control.cancel("architecture")
    result = await asyncio.wait_for(task, timeout=3)
    assert cancelled.is_set()
    cancelled_output = result.outputs[1]
    assert cancelled_output.outcome == "cancelled"
    assert cancelled_output.failure is not None
    assert cancelled_output.failure.code == "cancelled"
    assert cancelled_output.findings == ()
    assert cancelled_output.questions == ()
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_callback_self_cancellation_closes_only_that_role():
    plan = _plan()

    async def self_cancelling_runner(input_):
        raise asyncio.CancelledError()

    runners = _runners()
    runners["architecture"] = self_cancelling_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    cancelled_output = result.outputs[1]
    assert cancelled_output.outcome == "cancelled"
    assert cancelled_output.failure is not None
    assert cancelled_output.failure.code == "cancelled"
    assert cancelled_output.findings == ()
    assert cancelled_output.questions == ()
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_outer_council_cancellation_propagates_and_leaves_no_owned_tasks():
    plan = _plan()
    started = asyncio.Event()
    release = asyncio.Event()

    async def waiting_runner(input_):
        started.set()
        await release.wait()
        return _success_output(input_)

    runners = {role: waiting_runner for role in ROLE_ORDER}
    task = asyncio.create_task(
        ReviewCouncil().run(plan, runners, _CountingClock())
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_cancellation_control_rejects_unknown_roles():
    control = CouncilCancellation()
    with pytest.raises(ValueError):
        control.cancel("writer")
    with pytest.raises(ValueError):
        control.cancel("")


@pytest.mark.asyncio
async def test_runner_mapping_validation_and_sync_return_fail_closed():
    plan = _plan()
    with pytest.raises((TypeError, ValueError)):
        await ReviewCouncil().run(
            plan, {"intent": _success_runner, "operability": _success_runner},
            _CountingClock(),
        )
    with pytest.raises((TypeError, ValueError)):
        await ReviewCouncil().run(
            plan, {**_runners(), "writer": _success_runner},
            _CountingClock(),
        )
    with pytest.raises((TypeError, ValueError)):
        await ReviewCouncil().run(
            plan, {**_runners(), "architecture": 42},
            _CountingClock(),
        )

    def sync_runner(input_):
        return _success_output(input_)

    runners = _runners()
    runners["architecture"] = sync_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert result.outputs[1].outcome == "schema_invalid"
    assert result.outputs[1].failure is not None
    assert result.outputs[1].failure.code == "schema_invalid"
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"


@pytest.mark.asyncio
async def test_ensure_future_rejection_closes_awaitable_and_closes_only_role(
    monkeypatch,
):
    plan = _plan()
    closed = False

    class RejectedAwaitable:
        def __await__(self):
            return iter(())

        def close(self):
            nonlocal closed
            closed = True

    rejected = RejectedAwaitable()
    real_ensure_future = asyncio.ensure_future

    def rejecting_ensure_future(awaitable, *, loop=None):
        if awaitable is rejected:
            raise TypeError("rejected malformed awaitable")
        return real_ensure_future(awaitable, loop=loop)

    monkeypatch.setattr(asyncio, "ensure_future", rejecting_ensure_future)

    def malformed_runner(input_):
        return rejected

    runners = _runners()
    runners["architecture"] = malformed_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert closed is True
    schema_invalid_output = result.outputs[1]
    assert schema_invalid_output.outcome == "schema_invalid"
    assert schema_invalid_output.failure is not None
    assert schema_invalid_output.failure.code == "schema_invalid"
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_foreign_loop_future_fails_closed_as_schema_invalid():
    inputs = _default_inputs(architecture={"timeout_seconds": 1})
    plan = _plan(inputs)
    foreign_loop = asyncio.new_event_loop()
    try:
        foreign_future = foreign_loop.create_future()
        assert foreign_future.get_loop() is not asyncio.get_running_loop()

        def foreign_future_runner(input_):
            return foreign_future

        runners = _runners()
        runners["architecture"] = foreign_future_runner
        result = await ReviewCouncil().run(plan, runners, _CountingClock())
    finally:
        foreign_loop.close()

    assert foreign_future.cancelled() is True
    schema_invalid_output = result.outputs[1]
    assert schema_invalid_output.outcome == "schema_invalid"
    assert schema_invalid_output.failure is not None
    assert schema_invalid_output.failure.code == "schema_invalid"
    assert schema_invalid_output.findings == ()
    assert schema_invalid_output.questions == ()
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_current_loop_task_runner_behavior_is_preserved():
    plan = _plan()

    def task_runner(input_):
        return asyncio.create_task(_success_runner(input_))

    runners = _runners()
    runners["architecture"] = task_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert result.outputs[1].outcome == "success"
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_wrong_type_or_wrong_input_output_fails_closed():
    plan = _plan()

    async def wrong_type_runner(input_):
        return "not a finding output"

    async def wrong_input_runner(input_):
        copied = ReviewerInput.model_validate_json(input_.model_dump_json())
        return _success_output(copied)

    runners = _runners()
    runners["architecture"] = wrong_type_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert result.outputs[1].outcome == "schema_invalid"
    assert result.outputs[1].failure is not None
    assert result.outputs[1].failure.code == "schema_invalid"
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"

    runners = _runners()
    runners["architecture"] = wrong_input_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert result.outputs[1].outcome == "schema_invalid"
    assert result.outputs[1].failure.code == "schema_invalid"
    assert result.outputs[0].outcome == "success"
    assert result.outputs[2].outcome == "success"
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_evidence_index_merges_sorts_deduplicates_and_preserves_dissent():
    plan = _plan()
    intent, architecture, operability = plan.inputs
    intent_findings = (
        _finding(
            intent,
            finding_id="fnd-intent-1",
            claim="intent dissent one",
            evidence_refs=("ev-b", "ev-a"),
        ),
        _finding(
            intent,
            finding_id="fnd-intent-2",
            claim="intent dissent two",
            evidence_refs=("ev-a",),
        ),
    )
    intent_questions = (
        _question(
            intent,
            question="intent question on intake evidence",
            evidence_refs=("ev-b",),
        ),
    )
    architecture_findings = (
        _finding(
            architecture,
            finding_id="fnd-arch-1",
            claim="architecture dissent",
            evidence_refs=("ev-c", "ev-a"),
        ),
    )
    architecture_questions = (
        _question(
            architecture,
            question="architecture question on git evidence",
            evidence_refs=("ev-a",),
        ),
    )
    operability_findings = (
        _finding(
            operability,
            finding_id="fnd-oper-1",
            claim="operability dissent",
            evidence_refs=("ev-b", "ev-c"),
        ),
    )

    async def runner_for(input_, findings, questions):
        return _success_output(input_, findings=findings, questions=questions)

    runners = {
        "intent": lambda input_: runner_for(
            input_, intent_findings, intent_questions
        ),
        "architecture": lambda input_: runner_for(
            input_, architecture_findings, architecture_questions
        ),
        "operability": lambda input_: runner_for(
            input_, operability_findings, ()
        ),
    }
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    expected = (
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=("fnd-arch-1", "fnd-intent-1", "fnd-intent-2"),
            question_ids=(
                architecture_questions[0].question_id,
            ),
            roles=("intent", "architecture"),
        ),
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-b",
            finding_ids=("fnd-intent-1", "fnd-oper-1"),
            question_ids=(intent_questions[0].question_id,),
            roles=("intent", "operability"),
        ),
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-c",
            finding_ids=("fnd-arch-1", "fnd-oper-1"),
            question_ids=(),
            roles=("architecture", "operability"),
        ),
    )
    assert result.evidence_index == expected
    assert result.outputs[0].findings == intent_findings
    assert result.outputs[0].questions == intent_questions
    assert result.outputs[1].findings == architecture_findings
    assert result.outputs[1].questions == architecture_questions
    assert result.outputs[2].findings == operability_findings
    assert result.outputs[2].questions == ()
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_council_run_result_rejects_forged_missing_reordered_index():
    plan = _plan()
    intent, architecture, operability = plan.inputs
    intent_finding = _finding(
        intent,
        finding_id="fnd-intent-a",
        claim="intent refs ev-a",
        evidence_refs=("ev-a",),
    )
    architecture_finding = _finding(
        architecture,
        finding_id="fnd-arch-b",
        claim="architecture refs ev-c",
        evidence_refs=("ev-c",),
    )
    operability_finding = _finding(
        operability,
        finding_id="fnd-oper-a",
        claim="operability refs ev-a",
        evidence_refs=("ev-a",),
    )
    outputs = (
        _success_output(intent, findings=(intent_finding,)),
        _success_output(architecture, findings=(architecture_finding,)),
        _success_output(operability, findings=(operability_finding,)),
    )
    index = (
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=("fnd-intent-a", "fnd-oper-a"),
            question_ids=(),
            roles=("intent", "operability"),
        ),
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-c",
            finding_ids=("fnd-arch-b",),
            question_ids=(),
            roles=("architecture",),
        ),
    )
    forged_entry = CouncilEvidenceIndexEntry(
        schema_version="v1",
        evidence_id="zz-forged",
        finding_ids=("fnd-forged",),
        question_ids=(),
        roles=("intent",),
    )
    with pytest.raises(ValidationError):
        CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=outputs,
            evidence_index=index + (forged_entry,),
        )
    with pytest.raises(ValidationError):
        CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=outputs,
            evidence_index=index[:-1],
        )
    if len(index) > 1:
        with pytest.raises(ValidationError):
            CouncilRunResult(
                schema_version="v1",
                plan=plan,
                outputs=outputs,
                evidence_index=tuple(reversed(index)),
            )


@pytest.mark.asyncio
async def test_council_run_result_rejects_duplicate_cross_role_finding_ids():
    plan = _plan()
    intent, architecture, _ = plan.inputs
    shared_finding = _finding(
        intent,
        finding_id="fnd-shared",
        claim="duplicate finding",
        evidence_refs=("ev-a",),
    )
    architecture_finding = _finding(
        architecture,
        finding_id="fnd-shared",
        claim="duplicate finding",
        evidence_refs=("ev-a",),
    )
    outputs = (
        _success_output(intent, findings=(shared_finding,)),
        _success_output(architecture, findings=(architecture_finding,)),
        _success_output(plan.inputs[2]),
    )
    with pytest.raises(ValidationError):
        CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=outputs,
            evidence_index=(),
        )


@pytest.mark.asyncio
async def test_run_closes_duplicate_role_as_schema_invalid_without_cancelling():
    plan = _plan()
    intent, architecture, _ = plan.inputs
    shared_finding = _finding(
        intent,
        finding_id="fnd-shared",
        claim="duplicate finding",
        evidence_refs=("ev-a",),
    )
    architecture_finding = _finding(
        architecture,
        finding_id="fnd-shared",
        claim="duplicate finding",
        evidence_refs=("ev-a",),
    )

    async def intent_runner(input_):
        return _success_output(input_, findings=(shared_finding,))

    async def architecture_runner(input_):
        return _success_output(input_, findings=(architecture_finding,))

    runners = _runners()
    runners["intent"] = intent_runner
    runners["architecture"] = architecture_runner
    result = await ReviewCouncil().run(plan, runners, _CountingClock())
    assert result.outputs[0].outcome == "success"
    assert result.outputs[1].outcome == "schema_invalid"
    assert result.outputs[2].outcome == "success"
    assert result.evidence_index == (
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=("fnd-shared",),
            question_ids=(),
            roles=("intent",),
        ),
    )


def test_deep_immutability_and_json_round_trip_for_new_contracts():
    plan = _plan()
    index_entry = CouncilEvidenceIndexEntry(
        schema_version="v1",
        evidence_id="ev-a",
        finding_ids=("fnd-1",),
        question_ids=("rq_" + "0" * 32,),
        roles=("intent",),
    )
    run_result = CouncilRunResult(
        schema_version="v1",
        plan=plan,
        outputs=tuple(
            _success_output(input_) for input_ in plan.inputs
        ),
        evidence_index=(),
    )
    for value in (plan, index_entry, run_result):
        _assert_immutable_graph(value)
        assert type(value).model_config["frozen"] is True
        assert type(value).model_config["extra"] == "forbid"
        rebuilt = type(value).model_validate_json(value.model_dump_json())
        assert rebuilt == value
    with pytest.raises(ValidationError):
        plan.inputs = plan.inputs
    with pytest.raises(ValidationError):
        index_entry.finding_ids = index_entry.finding_ids
    with pytest.raises(ValidationError):
        run_result.outputs = run_result.outputs


def test_index_entry_rejects_bad_sortedness_or_empty_references():
    with pytest.raises(ValidationError):
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=("fnd-2", "fnd-1"),
            question_ids=(),
            roles=("intent",),
        )
    with pytest.raises(ValidationError):
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=(),
            question_ids=(),
            roles=("intent",),
        )
    with pytest.raises(ValidationError):
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=(),
            question_ids=(),
            roles=(),
        )
    with pytest.raises(ValidationError):
        CouncilEvidenceIndexEntry(
            schema_version="v1",
            evidence_id="ev-a",
            finding_ids=(),
            question_ids=("rq_" + "0" * 32,),
            roles=("operability", "architecture"),
        )


@pytest.mark.asyncio
async def test_invalid_clock_is_fatal_orchestration_contract_error():
    plan = _plan()

    async def failing_runner(input_):
        raise RuntimeError("clock probe")

    runners = _runners()
    runners["architecture"] = failing_runner

    def naive_clock():
        return datetime(2026, 8, 25, 9, 0, 0)

    with pytest.raises(council_module.CouncilOrchestrationError):
        await ReviewCouncil().run(plan, runners, naive_clock)
    assert await _pending_noncurrent_tasks() == []


@pytest.mark.asyncio
async def test_non_callable_clock_rejected_before_runners_start():
    plan = _plan()
    started = []

    async def starting_runner(input_):
        started.append(input_.reviewer_role)
        return _success_output(input_)

    runners = {role: starting_runner for role in ROLE_ORDER}
    with pytest.raises(TypeError):
        await ReviewCouncil().run(plan, runners, clock=42)
    assert started == []


@pytest.mark.asyncio
async def test_clock_is_only_used_for_synthetic_failure_outputs():
    plan = _plan()
    clock = _CountingClock()
    result = await ReviewCouncil().run(plan, _runners(), clock)
    assert clock.calls == 0
    assert all(output.outcome == "success" for output in result.outputs)

    inputs = _default_inputs(architecture={"timeout_seconds": 1})
    never = asyncio.Event()

    async def waiting_runner(input_):
        await never.wait()
        return _success_output(input_)

    runners = _runners()
    runners["architecture"] = waiting_runner
    clock = _CountingClock()
    result = await asyncio.wait_for(
        ReviewCouncil().run(_plan(inputs), runners, clock), timeout=5
    )
    assert clock.calls == 1
    assert result.outputs[1].outcome == "timeout"


def test_package_exports_new_public_symbols():
    assert PUBLIC_API_NAMES <= set(assurance.__all__)
    for name in PUBLIC_API_NAMES:
        assert hasattr(assurance, name)
    assert assurance.CouncilPlan is council_module.CouncilPlan
    assert assurance.CouncilEvidenceIndexEntry is (
        council_module.CouncilEvidenceIndexEntry
    )
    assert assurance.CouncilRunResult is council_module.CouncilRunResult
    assert assurance.CouncilCancellation is council_module.CouncilCancellation
    assert assurance.ReviewCouncil is council_module.ReviewCouncil


def test_module_has_no_forbidden_fields_or_io():
    source = Path(council_module.__file__).read_text(encoding="utf-8")
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

    forbidden_fields = {
        "gate",
        "pass",
        "routing",
        "provider",
        "receipt",
        "adjudicat",
    }
    for contract in (
        CouncilPlan,
        CouncilEvidenceIndexEntry,
        CouncilRunResult,
    ):
        for field_name in contract.model_fields:
            lowered = field_name.lower()
            assert not any(token in lowered for token in forbidden_fields)
    lowered_source = source.lower()
    assert "gate" not in lowered_source
    assert "pass" not in lowered_source
    assert "routing" not in lowered_source
    assert "provider" not in lowered_source
    assert "receipt" not in lowered_source
    assert "adjudicat" not in lowered_source
