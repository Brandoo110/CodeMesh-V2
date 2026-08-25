"""V2-P4-05 Council parallel isolation.

This module provides the first deterministic, offline council orchestration
primitive. Three reviewers run as independent asyncio tasks behind an
all-three-ready start barrier. Role-level timeout, cancellation, exception,
and schema-invalid outcomes are closed out with generic terminal outputs;
they are never treated as acceptance. The module performs no filesystem,
network, external process, persistence, or wall-clock access, and it does
not make acceptance decisions.
"""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .reviewer_contracts import (
    FindingOutput,
    ReviewerFailureOutcome,
    ReviewerInput,
)

_ROLE_ORDER = ("intent", "architecture", "operability")
_ROLE_KEYS = frozenset(_ROLE_ORDER)
_ROLE_INDEX = {role: index for index, role in enumerate(_ROLE_ORDER)}
_ROLE = Literal["intent", "architecture", "operability"]

_FAILURE_CODES = {
    "failure": "execution_failed",
    "timeout": "timeout",
    "cancelled": "cancelled",
    "schema_invalid": "schema_invalid",
}

_FAILURE_DETAILS = {
    "execution_failed": "role review raised an unexpected error; closed out",
    "timeout": "role review exceeded its allowed duration; closed out",
    "cancelled": "role review was cancelled; closed out",
    "schema_invalid": "role review returned a schema-invalid result; closed out",
}


class CouncilError(Exception):
    """Base error for council orchestration failures."""


class CouncilOrchestrationError(CouncilError):
    """Fatal council orchestration contract error."""


class CouncilPlan(BaseModel):
    """Immutable plan carrying exactly three reviewer inputs in canonical order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    inputs: tuple[ReviewerInput, ReviewerInput, ReviewerInput]

    @field_validator("inputs", mode="before")
    @classmethod
    def _exact_input_tuple(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json":
            if type(value) is not list:
                raise ValueError("inputs must be an array in JSON mode")
            return tuple(
                item
                if type(item) is ReviewerInput
                else ReviewerInput.model_validate_json(json.dumps(item))
                for item in value
            )
        if type(value) is not tuple:
            raise ValueError("inputs must be an exact tuple at raw validation")
        for item in value:
            if type(item) is not ReviewerInput:
                raise ValueError(
                    "inputs must contain exact ReviewerInput instances"
                )
        return value

    @model_validator(mode="after")
    def _validate_canonical_roles_and_bindings(self) -> "CouncilPlan":
        inputs = self.inputs
        roles = tuple(item.reviewer_role for item in inputs)
        if roles != _ROLE_ORDER:
            raise ValueError(
                "inputs must contain exactly intent, architecture, "
                "and operability in canonical order"
            )
        first_subject = inputs[0].subject
        first_risk = inputs[0].risk_result
        first_requested_at = inputs[0].requested_at
        for item in inputs[1:]:
            if item.subject != first_subject:
                raise ValueError(
                    "all inputs must bind to the same ChangeSubject"
                )
            if item.risk_result != first_risk:
                raise ValueError(
                    "all inputs must bind to the same "
                    "RiskClassificationResult"
                )
            if item.requested_at != first_requested_at:
                raise ValueError(
                    "all inputs must share the same requested_at"
                )
        return self


class CouncilEvidenceIndexEntry(BaseModel):
    """One Evidence ID with deterministic sorted references and roles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    evidence_id: str
    finding_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    roles: tuple[_ROLE, ...] = ()

    @field_validator("evidence_id", mode="before")
    @classmethod
    def _validate_evidence_id(cls, value: object) -> str:
        if type(value) is not str or not value.strip() or "\x00" in value:
            raise ValueError("evidence_id must be a nonblank exact string")
        return value

    @field_validator("finding_ids", "question_ids", "roles", mode="before")
    @classmethod
    def _exact_item_tuples(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        raise ValueError(
            f"{info.field_name} must be an exact tuple at raw validation"
        )

    @model_validator(mode="after")
    def _validate_canonical_refs(self) -> "CouncilEvidenceIndexEntry":
        finding_ids = self.finding_ids
        if finding_ids != tuple(sorted(finding_ids)) or len(
            set(finding_ids)
        ) != len(finding_ids):
            raise ValueError("finding_ids must be sorted and unique")
        question_ids = self.question_ids
        if question_ids != tuple(sorted(question_ids)) or len(
            set(question_ids)
        ) != len(question_ids):
            raise ValueError("question_ids must be sorted and unique")
        roles = self.roles
        if roles != tuple(
            sorted(roles, key=_ROLE_INDEX.__getitem__)
        ) or len(set(roles)) != len(roles):
            raise ValueError("roles must be canonical-sorted and unique")
        if not finding_ids and not question_ids:
            raise ValueError(
                "an index entry must reference at least one finding "
                "or question"
            )
        if not roles:
            raise ValueError(
                "an index entry must reference at least one reviewer role"
            )
        return self


class CouncilRunResult(BaseModel):
    """Immutable council result with canonical outputs and Evidence-ID index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    plan: CouncilPlan
    outputs: tuple[FindingOutput, FindingOutput, FindingOutput]
    evidence_index: tuple[CouncilEvidenceIndexEntry, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _rebuild_json_or_require_exact(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("CouncilRunResult must validate from a mapping")
        if info.mode == "json":
            data = dict(data)
            if type(data.get("plan")) is dict:
                data["plan"] = CouncilPlan.model_validate_json(
                    json.dumps(data["plan"])
                )
            outputs_raw = data.get("outputs")
            if type(outputs_raw) is list:
                data["outputs"] = tuple(
                    item
                    if type(item) is FindingOutput
                    else FindingOutput.model_validate_json(json.dumps(item))
                    for item in outputs_raw
                )
            index_raw = data.get("evidence_index")
            if type(index_raw) is list:
                data["evidence_index"] = tuple(
                    item
                    if type(item) is CouncilEvidenceIndexEntry
                    else CouncilEvidenceIndexEntry.model_validate_json(
                        json.dumps(item)
                    )
                    for item in index_raw
                )
            return data
        if type(data.get("plan")) is not CouncilPlan:
            raise ValueError("plan must be an exact CouncilPlan instance")
        if type(data.get("outputs")) is not tuple:
            raise ValueError(
                "outputs must be an exact tuple at raw validation"
            )
        for item in data.get("outputs", ()):
            if type(item) is not FindingOutput:
                raise ValueError(
                    "output items must be exact FindingOutput instances"
                )
        entries = data.get("evidence_index", ())
        if type(entries) is not tuple:
            raise ValueError(
                "evidence_index must be an exact tuple at raw validation"
            )
        for item in entries:
            if type(item) is not CouncilEvidenceIndexEntry:
                raise ValueError(
                    "evidence_index items must be exact "
                    "CouncilEvidenceIndexEntry instances"
                )
        return data

    @model_validator(mode="after")
    def _validate_council_bindings(self) -> "CouncilRunResult":
        plan = self.plan
        if len(self.outputs) != 3:
            raise ValueError(
                "outputs must contain exactly three terminal outputs"
            )
        for role_input, output in zip(plan.inputs, self.outputs):
            if output.input != role_input:
                raise ValueError(
                    "every output must bind to its exact plan input"
                )
            if output.input.reviewer_role != role_input.reviewer_role:
                raise ValueError(
                    "output role must equal its plan input role"
                )
        finding_ids = [
            item.finding_id
            for output in self.outputs
            for item in output.findings
        ]
        question_ids = [
            item.question_id
            for output in self.outputs
            for item in output.questions
        ]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError(
                "finding_ids must be unique across the council"
            )
        if len(question_ids) != len(set(question_ids)):
            raise ValueError(
                "question_ids must be unique across the council"
            )
        entries = self.evidence_index
        evidence_ids = [entry.evidence_id for entry in entries]
        if evidence_ids != sorted(evidence_ids) or len(
            set(evidence_ids)
        ) != len(evidence_ids):
            raise ValueError(
                "evidence_index must be strictly sorted and unique "
                "by evidence_id"
            )
        expected = _build_evidence_index(self.outputs)
        if entries != expected:
            raise ValueError(
                "evidence_index must equal the deterministic "
                "recomputation from outputs"
            )
        return self


class CouncilCancellation:
    """Transient per-role cancellation control for an active council run."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events = {role: asyncio.Event() for role in _ROLE_ORDER}

    def cancel(self, role: str) -> None:
        if role not in _ROLE_KEYS:
            raise ValueError("unknown council role")
        self._events[role].set()

    def _event(self, role: str) -> asyncio.Event:
        return self._events[role]


Runner = Callable[[ReviewerInput], Awaitable[FindingOutput]]
Clock = Callable[[], datetime]


def _snapshot_runners(runners: Mapping[str, Runner]) -> dict[str, Runner]:
    if type(runners) is not dict:
        raise TypeError("runners must be an exact dict")
    snapshot = dict(runners)
    if set(snapshot) != _ROLE_KEYS:
        raise ValueError(
            "runners must contain exactly the three council roles"
        )
    for role, runner in snapshot.items():
        if not callable(runner):
            raise TypeError(f"runner for {role} must be callable")
    return snapshot


def _synthetic_output(
    role_input: ReviewerInput,
    outcome: Literal[
        "failure", "timeout", "cancelled", "schema_invalid"
    ],
    clock: Clock,
) -> FindingOutput:
    stamped = clock()
    if (
        not isinstance(stamped, datetime)
        or stamped.tzinfo is None
        or stamped.tzinfo.utcoffset(stamped) is None
    ):
        raise CouncilOrchestrationError(
            "clock must return an aware datetime"
        )
    if stamped < role_input.requested_at:
        raise CouncilOrchestrationError(
            "clock output must be at or after requested_at"
        )
    failure = ReviewerFailureOutcome(
        schema_version="v1",
        code=_FAILURE_CODES[outcome],
        details=_FAILURE_DETAILS[_FAILURE_CODES[outcome]],
    )
    return FindingOutput(
        schema_version="v1",
        input=role_input,
        outcome=outcome,
        findings=(),
        questions=(),
        failure=failure,
        completed_at=stamped,
    )


async def _suppress(awaitable: Awaitable[object]) -> None:
    try:
        await awaitable
    except (asyncio.CancelledError, Exception):
        return None


def _current_task_cancel_requested() -> bool:
    current = asyncio.current_task()
    if current is None:
        return False
    cancelling = getattr(current, "cancelling", None)
    if cancelling is not None:
        return cancelling() > 0
    return bool(getattr(current, "_must_cancel", False))


def _close_or_cancel_awaitable(awaitable: object) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            return None
        return
    cancel = getattr(awaitable, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            return None


async def _consume_callback(
    task: asyncio.Task[object],
    role_input: ReviewerInput,
    clock: Clock,
) -> FindingOutput:
    try:
        result = await task
    except asyncio.CancelledError:
        if _current_task_cancel_requested():
            raise
        return _synthetic_output(role_input, "cancelled", clock)
    except Exception:
        return _synthetic_output(role_input, "failure", clock)
    if type(result) is not FindingOutput:
        return _synthetic_output(role_input, "schema_invalid", clock)
    if result.input is not role_input:
        return _synthetic_output(role_input, "schema_invalid", clock)
    return result


async def _run_role(
    role_input: ReviewerInput,
    runner: Runner,
    clock: Clock,
    cancellation: CouncilCancellation,
    barrier: asyncio.Barrier,
) -> FindingOutput:
    role = role_input.reviewer_role
    await barrier.wait()
    cancel_event = cancellation._event(role)
    try:
        coro = runner(role_input)
    except Exception:
        return _synthetic_output(role_input, "failure", clock)
    if not inspect.isawaitable(coro):
        return _synthetic_output(role_input, "schema_invalid", clock)
    try:
        running_loop = asyncio.get_running_loop()
        if asyncio.isfuture(coro) and coro.get_loop() is not running_loop:
            raise RuntimeError(
                "runner returned a Future bound to a foreign event loop"
            )
        callback_task = asyncio.ensure_future(coro)
    except Exception:
        _close_or_cancel_awaitable(coro)
        return _synthetic_output(role_input, "schema_invalid", clock)
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            (callback_task, cancel_task),
            timeout=role_input.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if callback_task in done:
            cancel_task.cancel()
            await _suppress(cancel_task)
            return await _consume_callback(callback_task, role_input, clock)
        if cancel_task in done:
            callback_task.cancel()
            await _suppress(callback_task)
            return _synthetic_output(role_input, "cancelled", clock)
        callback_task.cancel()
        await _suppress(callback_task)
        cancel_task.cancel()
        await _suppress(cancel_task)
        return _synthetic_output(role_input, "timeout", clock)
    finally:
        if not callback_task.done():
            callback_task.cancel()
            await _suppress(callback_task)
        if not cancel_task.done():
            cancel_task.cancel()
            await _suppress(cancel_task)


def _close_duplicate_ids(
    outputs: tuple[FindingOutput, ...],
    plan: CouncilPlan,
    clock: Clock,
) -> tuple[FindingOutput, ...]:
    seen_finding_ids: set[str] = set()
    seen_question_ids: set[str] = set()
    closed: list[FindingOutput] = []
    for output, role_input in zip(outputs, plan.inputs):
        finding_ids = {item.finding_id for item in output.findings}
        question_ids = {item.question_id for item in output.questions}
        if finding_ids & seen_finding_ids or question_ids & seen_question_ids:
            closed.append(
                _synthetic_output(role_input, "schema_invalid", clock)
            )
            continue
        seen_finding_ids.update(finding_ids)
        seen_question_ids.update(question_ids)
        closed.append(output)
    return tuple(closed)


def _build_evidence_index(
    outputs: tuple[FindingOutput, ...],
) -> tuple[CouncilEvidenceIndexEntry, ...]:
    buckets: dict[str, dict[str, set[str]]] = {}
    for output in outputs:
        role = output.input.reviewer_role
        for finding in output.findings:
            for evidence_id in finding.evidence_refs:
                bucket = buckets.setdefault(
                    evidence_id,
                    {"finding_ids": set(), "question_ids": set(), "roles": set()},
                )
                bucket["finding_ids"].add(finding.finding_id)
                bucket["roles"].add(role)
        for question in output.questions:
            for evidence_id in question.evidence_refs:
                bucket = buckets.setdefault(
                    evidence_id,
                    {"finding_ids": set(), "question_ids": set(), "roles": set()},
                )
                bucket["question_ids"].add(question.question_id)
                bucket["roles"].add(role)
    entries = []
    for evidence_id in sorted(buckets):
        bucket = buckets[evidence_id]
        entries.append(
            CouncilEvidenceIndexEntry(
                schema_version="v1",
                evidence_id=evidence_id,
                finding_ids=tuple(sorted(bucket["finding_ids"])),
                question_ids=tuple(sorted(bucket["question_ids"])),
                roles=tuple(
                    sorted(bucket["roles"], key=_ROLE_INDEX.__getitem__)
                ),
            )
        )
    return tuple(entries)


class ReviewCouncil:
    """Stateless council runner with per-role task isolation."""

    async def run(
        self,
        plan: CouncilPlan,
        runners: Mapping[str, Runner],
        clock: Clock,
        cancellation: CouncilCancellation | None = None,
    ) -> CouncilRunResult:
        if type(plan) is not CouncilPlan:
            raise TypeError("plan must be an exact CouncilPlan")
        runner_map = _snapshot_runners(runners)
        if cancellation is None:
            cancellation = CouncilCancellation()
        if type(cancellation) is not CouncilCancellation:
            raise TypeError(
                "cancellation must be an exact CouncilCancellation or None"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        barrier = asyncio.Barrier(3)
        tasks = {
            role: asyncio.create_task(
                _run_role(
                    plan.inputs[index],
                    runner_map[role],
                    clock,
                    cancellation,
                    barrier,
                )
            )
            for index, role in enumerate(_ROLE_ORDER)
        }
        try:
            raw_outputs = await asyncio.gather(*tasks.values())
        except BaseException:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise
        outputs = _close_duplicate_ids(raw_outputs, plan, clock)
        return CouncilRunResult(
            schema_version="v1",
            plan=plan,
            outputs=outputs,
            evidence_index=_build_evidence_index(outputs),
        )
