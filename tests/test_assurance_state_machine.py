"""保障域验收状态机单元测试（事件 / 绑定 / 状态 / 转移矩阵 / 失效函数）。

跑法：
    PYTHONPATH=. python -m unittest -v tests.test_assurance_state_machine
"""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

import assurance
from assurance import (
    AcceptanceBinding,
    AcceptanceEvent,
    AcceptanceMachineState,
    EventConflictError,
    InvalidTransitionError,
    StaleSubjectError,
    allowed_event_kinds,
    apply_acceptance_event,
    invalidate_if_needed,
    invalidation_reasons,
)
from assurance.contracts import AcceptanceCase


_T0 = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)


def _digest(letter="a"):
    return "sha256:" + letter * 64


def _ts(offset_minutes=0):
    return _T0 + timedelta(minutes=offset_minutes)


def _event_facts(kind):
    facts = {
        "COLLECT_EVIDENCE": {"evidence_refs": ("evidence-new",)},
        "REQUEST_EVIDENCE": {"missing_evidence": ("missing-new",)},
        "RECORD_CONFLICT": {"conflicts": ("conflict-new",)},
        "CONDITIONALLY_ACCEPT": {
            "conditions": ("condition-new",),
            "policy_decision_refs": ("policy-new",),
            "human_decision_refs": ("human-new",),
        },
        "ACCEPT": {
            "policy_decision_refs": ("policy-new",),
            "human_decision_refs": ("human-new",),
        },
        "REJECT": {"policy_decision_refs": ("policy-new",)},
        "INVALIDATE": {"reason": "invalidated by reason"},
    }
    return dict(facts[kind])


def _valid_event(kind, **overrides):
    values = {
        "schema_version": "v1",
        "event_id": "event-001",
        "subject_digest": _digest(),
        "kind": kind,
        "occurred_at": _ts(),
    }
    values.update(_event_facts(kind))
    values.update(overrides)
    return AcceptanceEvent(**values)


def _valid_binding(**overrides):
    values = {
        "schema_version": "v1",
        "subject_digest": _digest(),
        "policy_version": "policy-1",
        "rubric_version": "rubric-1",
        "waiver_id": None,
        "waiver_expires_at": None,
    }
    values.update(overrides)
    return AcceptanceBinding(**values)


_ACCEPTANCE_STATE_FACTS = {
    "DRAFT": {},
    "EVIDENCE_COLLECTED": {"evidence_refs": ("evidence-001",)},
    "NEEDS_EVIDENCE": {"missing_evidence": ("missing-001",)},
    "CONFLICTED": {"conflicts": ("conflict-001",)},
    "CONDITIONAL_ACCEPTED": {
        "evidence_refs": ("evidence-001",),
        "conditions": ("condition-001",),
        "policy_decision_refs": ("policy-001",),
        "human_decision_refs": ("human-001",),
    },
    "ACCEPTED": {
        "evidence_refs": ("evidence-001",),
        "policy_decision_refs": ("policy-001",),
        "human_decision_refs": ("human-001",),
    },
    "REJECTED": {"policy_decision_refs": ("policy-001",)},
    "INVALIDATED": {"invalidation_reason": "invalidated"},
}


def _valid_case(state="DRAFT", **overrides):
    values = {
        "schema_version": "v1",
        "case_id": "case-001",
        "subject_digest": _digest(),
        "state": state,
        "evidence_refs": (),
        "finding_refs": (),
        "execution_receipt_refs": (),
        "policy_decision_refs": (),
        "human_decision_refs": (),
        "conditions": (),
        "conflicts": (),
        "missing_evidence": (),
        "invalidation_reason": None,
        "created_at": _ts(0),
        "updated_at": _ts(0),
    }
    values.update(_ACCEPTANCE_STATE_FACTS[state])
    values.update(overrides)
    return AcceptanceCase(**values)


def _initial_state(**overrides):
    values = {
        "schema_version": "v1",
        "case": _valid_case(),
        "applied_events": (),
    }
    values.update(overrides)
    return AcceptanceMachineState(**values)


_MATRIX = {
    "DRAFT": (
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "INVALIDATE",
    ),
    "EVIDENCE_COLLECTED": (
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "RECORD_CONFLICT",
        "CONDITIONALLY_ACCEPT",
        "ACCEPT",
        "REJECT",
        "INVALIDATE",
    ),
    "NEEDS_EVIDENCE": (
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "INVALIDATE",
    ),
    "CONFLICTED": (
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "RECORD_CONFLICT",
        "CONDITIONALLY_ACCEPT",
        "REJECT",
        "INVALIDATE",
    ),
    "CONDITIONAL_ACCEPTED": (
        "COLLECT_EVIDENCE",
        "REQUEST_EVIDENCE",
        "RECORD_CONFLICT",
        "CONDITIONALLY_ACCEPT",
        "ACCEPT",
        "REJECT",
        "INVALIDATE",
    ),
    "ACCEPTED": ("INVALIDATE",),
    "REJECTED": ("INVALIDATE",),
    "INVALIDATED": (),
}

_ALL_KINDS = (
    "COLLECT_EVIDENCE",
    "REQUEST_EVIDENCE",
    "RECORD_CONFLICT",
    "CONDITIONALLY_ACCEPT",
    "ACCEPT",
    "REJECT",
    "INVALIDATE",
)


class TestPackageExports(unittest.TestCase):
    def test_all_public_names_exported(self):
        names = (
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
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, assurance.__all__)
                self.assertTrue(hasattr(assurance, name))


class TestAcceptanceEventContract(unittest.TestCase):
    def test_valid_construction_round_trip_and_stable_serialization(self):
        event = _valid_event("CONDITIONALLY_ACCEPT")
        dumped = event.model_dump(mode="json")
        restored = AcceptanceEvent.model_validate(dumped)
        self.assertEqual(restored, event)
        self.assertEqual(restored.model_dump_json(), event.model_dump_json())
        self.assertIsNotNone(event.occurred_at.tzinfo)

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        event = _valid_event("COLLECT_EVIDENCE")
        self.assertEqual(event.schema_version, "v1")
        values = event.model_dump()
        values.pop("schema_version")
        self.assertEqual(AcceptanceEvent(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_event("COLLECT_EVIDENCE", schema_version="v2")

    def test_exact_field_order(self):
        self.assertEqual(
            list(AcceptanceEvent.model_fields.keys()),
            [
                "schema_version",
                "event_id",
                "subject_digest",
                "kind",
                "evidence_refs",
                "finding_refs",
                "execution_receipt_refs",
                "policy_decision_refs",
                "human_decision_refs",
                "conditions",
                "conflicts",
                "missing_evidence",
                "reason",
                "occurred_at",
            ],
        )

    def test_unknown_field_rejected(self):
        values = _valid_event("COLLECT_EVIDENCE").model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            AcceptanceEvent.model_validate(values)

    def test_assignment_mutation_rejected(self):
        event = _valid_event("COLLECT_EVIDENCE")
        with self.assertRaises(ValidationError):
            event.event_id = "changed"
        with self.assertRaises(ValidationError):
            event.evidence_refs = ("changed",)

    def test_event_id_blank_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_event("COLLECT_EVIDENCE", event_id=bad)

    def test_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "z" * 64,
            "md5:" + "a" * 32,
            "",
        )
        for bad in bad_digests:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_event("COLLECT_EVIDENCE", subject_digest=bad)

    def test_reason_blank_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_event("INVALIDATE", reason=bad)

    def test_tuple_fields_preserve_order_and_reject_blank_and_duplicates(self):
        tuple_cases = (
            ("COLLECT_EVIDENCE", "evidence_refs", ("e-1", "e-2")),
            ("COLLECT_EVIDENCE", "finding_refs", ("f-1", "f-2")),
            (
                "COLLECT_EVIDENCE",
                "execution_receipt_refs",
                ("r-1", "r-2"),
            ),
            ("COLLECT_EVIDENCE", "policy_decision_refs", ("p-1", "p-2")),
            ("COLLECT_EVIDENCE", "human_decision_refs", ("h-1", "h-2")),
            ("CONDITIONALLY_ACCEPT", "conditions", ("c-1", "c-2")),
            ("RECORD_CONFLICT", "conflicts", ("cf-1", "cf-2")),
            ("REQUEST_EVIDENCE", "missing_evidence", ("m-1", "m-2")),
        )
        for kind, field, values in tuple_cases:
            with self.subTest(field=field):
                event = _valid_event(kind, **{field: values})
                self.assertEqual(getattr(event, field), values)
                with self.assertRaises(ValidationError):
                    _valid_event(kind, **{field: ("x", " ")})
                with self.assertRaises(ValidationError):
                    _valid_event(kind, **{field: ("x", "x")})

    def test_tuple_fields_are_input_mutation_safe(self):
        evidence_list = ["e-1", "e-2"]
        event = _valid_event("COLLECT_EVIDENCE", evidence_refs=evidence_list)
        evidence_list.append("e-3")
        self.assertEqual(event.evidence_refs, ("e-1", "e-2"))

    def test_naive_occurred_at_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_event(
                "COLLECT_EVIDENCE",
                occurred_at=datetime(2026, 8, 25, 2, 30),
            )

    def test_every_kind_required_facts_enforced(self):
        required_removals = (
            ("COLLECT_EVIDENCE", {"evidence_refs": ()}),
            ("REQUEST_EVIDENCE", {"missing_evidence": ()}),
            ("RECORD_CONFLICT", {"conflicts": ()}),
            ("CONDITIONALLY_ACCEPT", {"conditions": ()}),
            (
                "CONDITIONALLY_ACCEPT",
                {
                "policy_decision_refs": (),
                "human_decision_refs": (),
            },
            ),
            ("ACCEPT", {"policy_decision_refs": ()}),
            ("ACCEPT", {"human_decision_refs": ()}),
            (
                "REJECT",
                {"policy_decision_refs": (), "human_decision_refs": ()},
            ),
            ("INVALIDATE", {"reason": None}),
        )
        for kind, removals in required_removals:
            with self.subTest(kind=kind, removals=removals):
                with self.assertRaises(ValidationError):
                    _valid_event(kind, **removals)

    def test_every_kind_prohibited_facts_rejected(self):
        prohibited = (
            ("CONDITIONALLY_ACCEPT", "conditions"),
            ("RECORD_CONFLICT", "conflicts"),
            ("REQUEST_EVIDENCE", "missing_evidence"),
            ("INVALIDATE", "reason"),
        )
        for kind, field in prohibited:
            for other_kind in _ALL_KINDS:
                if other_kind == kind:
                    continue
                with self.subTest(prohibited_field=field, other_kind=other_kind):
                    if field == "reason":
                        override = {"reason": "must not be set"}
                    else:
                        override = {field: ("must-not-be-set",)}
                    with self.assertRaises(ValidationError):
                        _valid_event(other_kind, **override)

    def test_legal_success_for_every_kind(self):
        for kind in _ALL_KINDS:
            with self.subTest(kind=kind):
                event = _valid_event(kind)
                self.assertEqual(event.kind, kind)


class TestAcceptanceBindingContract(unittest.TestCase):
    def test_valid_construction_round_trip_and_stable_serialization(self):
        binding = _valid_binding(
            waiver_id="waiver-001",
            waiver_expires_at=_ts(30),
        )
        dumped = binding.model_dump(mode="json")
        restored = AcceptanceBinding.model_validate(dumped)
        self.assertEqual(restored, binding)
        self.assertEqual(restored.model_dump_json(), binding.model_dump_json())
        self.assertIsNotNone(restored.waiver_expires_at.tzinfo)

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        binding = _valid_binding()
        self.assertEqual(binding.schema_version, "v1")
        values = binding.model_dump()
        values.pop("schema_version")
        self.assertEqual(AcceptanceBinding(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_binding(schema_version="v2")

    def test_exact_field_order(self):
        self.assertEqual(
            list(AcceptanceBinding.model_fields.keys()),
            [
                "schema_version",
                "subject_digest",
                "policy_version",
                "rubric_version",
                "waiver_id",
                "waiver_expires_at",
            ],
        )

    def test_unknown_field_rejected(self):
        values = _valid_binding().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            AcceptanceBinding.model_validate(values)

    def test_assignment_mutation_rejected(self):
        binding = _valid_binding()
        with self.assertRaises(ValidationError):
            binding.policy_version = "policy-2"
        with self.assertRaises(ValidationError):
            binding.waiver_id = "waiver-001"

    def test_digest_rejected(self):
        for bad in ("sha256:" + "A" * 64, "sha256:" + "a" * 63, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_binding(subject_digest=bad)

    def test_blank_policy_or_rubric_rejected(self):
        for field in ("policy_version", "rubric_version"):
            for bad in ("", "   "):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_binding(**{field: bad})

    def test_blank_waiver_id_rejected(self):
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_binding(
                        waiver_id=bad,
                        waiver_expires_at=_ts(30),
                    )

    def test_waiver_pair_mismatch_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_binding(waiver_id="waiver-001", waiver_expires_at=None)
        with self.assertRaises(ValidationError):
            _valid_binding(waiver_id=None, waiver_expires_at=_ts(30))

    def test_waiver_pair_both_set_or_both_absent(self):
        self.assertIsNone(_valid_binding().waiver_id)
        binding = _valid_binding(waiver_id="w", waiver_expires_at=_ts(30))
        self.assertEqual(binding.waiver_id, "w")
        self.assertEqual(binding.waiver_expires_at, _ts(30))

    def test_naive_waiver_expiry_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_binding(
                waiver_id="w",
                waiver_expires_at=datetime(2026, 8, 25, 2, 30),
            )


class TestAcceptanceMachineStateContract(unittest.TestCase):
    def test_valid_construction_round_trip_and_stable_serialization(self):
        event = _valid_event("COLLECT_EVIDENCE", event_id="event-001")
        state = AcceptanceMachineState(
            case=_valid_case(),
            applied_events=(event,),
        )
        dumped = state.model_dump(mode="json")
        restored = AcceptanceMachineState.model_validate(dumped)
        self.assertEqual(restored, state)
        self.assertEqual(restored.model_dump_json(), state.model_dump_json())

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        state = _initial_state()
        self.assertEqual(state.schema_version, "v1")
        values = state.model_dump()
        values.pop("schema_version")
        self.assertEqual(AcceptanceMachineState(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _initial_state(schema_version="v2")

    def test_exact_field_order(self):
        self.assertEqual(
            list(AcceptanceMachineState.model_fields.keys()),
            ["schema_version", "case", "applied_events"],
        )

    def test_unknown_field_rejected(self):
        values = _initial_state().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            AcceptanceMachineState.model_validate(values)

    def test_assignment_mutation_rejected(self):
        state = _initial_state()
        with self.assertRaises(ValidationError):
            state.case = _valid_case(state="ACCEPTED")
        with self.assertRaises(ValidationError):
            state.applied_events = ()

    def test_duplicate_event_id_rejected(self):
        event = _valid_event("COLLECT_EVIDENCE", event_id="duplicate")
        with self.assertRaises(ValidationError):
            _initial_state(applied_events=(event, event))

    def test_historical_subject_mismatch_rejected(self):
        event = _valid_event(
            "COLLECT_EVIDENCE",
            subject_digest=_digest("b"),
        )
        with self.assertRaises(ValidationError):
            _initial_state(applied_events=(event,))

    def test_input_containers_are_copied(self):
        case_values = _valid_case().model_dump()
        event_values = [
            _valid_event(
                "COLLECT_EVIDENCE",
                event_id="event-list",
                evidence_refs=("evidence-list",),
            ).model_dump()
        ]
        state = AcceptanceMachineState(case=case_values, applied_events=event_values)
        case_values["state"] = "ACCEPTED"
        event_values.append(
            _valid_event(
                "REQUEST_EVIDENCE",
                event_id="event-extra",
            ).model_dump()
        )
        self.assertEqual(state.case.state, "DRAFT")
        self.assertEqual(len(state.applied_events), 1)
        self.assertEqual(state.applied_events[0].event_id, "event-list")

    def test_deep_immutability(self):
        event = _valid_event("INVALIDATE", event_id="event-deep")
        state = _initial_state(
            case=_valid_case(state="INVALIDATED", invalidation_reason="x"),
            applied_events=(event,),
        )
        self.assertIsInstance(state.applied_events, tuple)
        with self.assertRaises(ValidationError):
            state.applied_events[0].reason = "changed"


class TestTransitionMatrix(unittest.TestCase):
    def test_allowed_event_kinds_exact_rows_in_declaration_order(self):
        for state_name, expected in _MATRIX.items():
            with self.subTest(state_name=state_name):
                result = allowed_event_kinds(state_name)
                self.assertIsInstance(result, tuple)
                self.assertEqual(result, expected)

    def test_allowed_event_kinds_rejects_invalid_inputs(self):
        for bad in (None, 1, ("DRAFT",)):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    allowed_event_kinds(bad)
        for bad in ("draft", "DRAFT ", "UNKNOWN", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    allowed_event_kinds(bad)

    def test_matrix_allowed_pairs_succeed(self):
        for source_state, kinds in _MATRIX.items():
            for kind in kinds:
                with self.subTest(source=source_state, kind=kind):
                    state = _initial_state(case=_valid_case(source_state))
                    event = _valid_event(
                        kind,
                        event_id=f"evt-{source_state}-{kind}",
                        occurred_at=_ts(0),
                    )
                    result = apply_acceptance_event(state, event)
                    self.assertIsNot(result, state)
                    self.assertEqual(result.case.created_at, state.case.created_at)
                    self.assertEqual(result.case.updated_at, event.occurred_at)
                    self.assertEqual(result.applied_events[-1], event)

    def test_matrix_forbidden_pairs_raise_invalid_transition(self):
        for source_state in _MATRIX:
            for kind in _ALL_KINDS:
                if kind in _MATRIX[source_state]:
                    continue
                with self.subTest(source=source_state, kind=kind):
                    state = _initial_state(case=_valid_case(source_state))
                    event = _valid_event(
                        kind,
                        event_id=f"forbidden-{source_state}-{kind}",
                        occurred_at=_ts(0),
                    )
                    with self.assertRaises(InvalidTransitionError):
                        apply_acceptance_event(state, event)


class TestApplyAcceptanceEvent(unittest.TestCase):
    def test_duplicate_exact_event_returns_same_state_before_time_check(self):
        event = _valid_event(
            "COLLECT_EVIDENCE",
            event_id="evt-dup-time",
            occurred_at=_ts(0),
            evidence_refs=("evidence-001",),
        )
        state = _initial_state(
            case=_valid_case(
                state="EVIDENCE_COLLECTED",
                evidence_refs=("evidence-001",),
                updated_at=_ts(60),
            ),
            applied_events=(event,),
        )
        self.assertIs(apply_acceptance_event(state, event), state)

    def test_duplicate_exact_event_returns_same_state_before_matrix_check(self):
        event = _valid_event(
            "INVALIDATE",
            event_id="evt-dup-matrix",
            reason="same reason",
            occurred_at=_ts(0),
        )
        state = _initial_state(
            case=_valid_case(
                state="INVALIDATED",
                invalidation_reason="same reason",
            ),
            applied_events=(event,),
        )
        self.assertIs(apply_acceptance_event(state, event), state)

    def test_same_id_changed_payload_raises_conflict_before_stale_time_matrix(self):
        existing = _valid_event(
            "COLLECT_EVIDENCE",
            event_id="evt-conflict",
            evidence_refs=("e-1",),
            occurred_at=_ts(0),
        )
        state = _initial_state(applied_events=(existing,))
        changed_subject = _valid_event(
            "COLLECT_EVIDENCE",
            event_id="evt-conflict",
            subject_digest=_digest("b"),
            evidence_refs=("e-2",),
            occurred_at=_ts(-5),
        )
        with self.assertRaises(EventConflictError):
            apply_acceptance_event(state, changed_subject)

    def test_stale_subject_rejected(self):
        state = _initial_state()
        event = _valid_event(
            "COLLECT_EVIDENCE",
            subject_digest=_digest("b"),
        )
        with self.assertRaises(StaleSubjectError):
            apply_acceptance_event(state, event)

    def test_backward_time_rejected(self):
        state = _initial_state()
        event = _valid_event("COLLECT_EVIDENCE", occurred_at=_ts(-1))
        with self.assertRaises(InvalidTransitionError):
            apply_acceptance_event(state, event)

    def test_equal_time_is_allowed(self):
        state = _initial_state()
        event = _valid_event("COLLECT_EVIDENCE", occurred_at=_ts(0))
        result = apply_acceptance_event(state, event)
        self.assertEqual(result.case.updated_at, event.occurred_at)

    def test_invalid_argument_types_rejected(self):
        event = _valid_event("COLLECT_EVIDENCE")
        state = _initial_state()
        for bad_state in (None, {}, event.model_dump()):
            with self.subTest(bad_state=bad_state):
                with self.assertRaises(TypeError):
                    apply_acceptance_event(bad_state, event)
        for bad_event in (None, {}, "COLLECT_EVIDENCE"):
            with self.subTest(bad_event=bad_event):
                with self.assertRaises(TypeError):
                    apply_acceptance_event(state, bad_event)

    def _assert_refs_merged(self, case, expected):
        self.assertEqual(case.evidence_refs, expected["evidence_refs"])
        self.assertEqual(case.finding_refs, expected["finding_refs"])
        self.assertEqual(
            case.execution_receipt_refs, expected["execution_receipt_refs"]
        )
        self.assertEqual(
            case.policy_decision_refs, expected["policy_decision_refs"]
        )
        self.assertEqual(case.human_decision_refs, expected["human_decision_refs"])

    def test_collect_evidence_target_facts(self):
        source = _valid_case(
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "COLLECT_EVIDENCE",
            event_id="evt-collect",
            occurred_at=_ts(30),
            evidence_refs=("e-new", "e-old"),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "EVIDENCE_COLLECTED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conditions, ())
        self.assertEqual(case.conflicts, ())
        self.assertEqual(case.missing_evidence, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_request_evidence_target_facts(self):
        source = _valid_case(
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "REQUEST_EVIDENCE",
            event_id="evt-request",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
            missing_evidence=("m-new", "m-old"),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "NEEDS_EVIDENCE")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.missing_evidence, ("m-new", "m-old"))
        self.assertEqual(case.conditions, ())
        self.assertEqual(case.conflicts, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_record_conflict_target_facts(self):
        source = _valid_case(
            state="EVIDENCE_COLLECTED",
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "RECORD_CONFLICT",
            event_id="evt-conflict",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
            conflicts=("cf-new", "cf-old"),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "CONFLICTED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conflicts, ("cf-new", "cf-old"))
        self.assertEqual(case.conditions, ())
        self.assertEqual(case.missing_evidence, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_conditionally_accept_target_facts(self):
        source = _valid_case(
            state="EVIDENCE_COLLECTED",
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "CONDITIONALLY_ACCEPT",
            event_id="evt-conditional",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new", "p-old"),
            human_decision_refs=("h-new", "h-old"),
            conditions=("c-new", "c-old"),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "CONDITIONAL_ACCEPTED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conditions, ("c-new", "c-old"))
        self.assertEqual(case.conflicts, ())
        self.assertEqual(case.missing_evidence, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_accept_target_facts(self):
        source = _valid_case(
            state="EVIDENCE_COLLECTED",
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "ACCEPT",
            event_id="evt-accept",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "ACCEPTED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conditions, ())
        self.assertEqual(case.conflicts, ())
        self.assertEqual(case.missing_evidence, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_reject_target_facts(self):
        source = _valid_case(
            state="EVIDENCE_COLLECTED",
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "REJECT",
            event_id="evt-reject",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "REJECTED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conditions, ())
        self.assertEqual(case.conflicts, ())
        self.assertEqual(case.missing_evidence, ())
        self.assertIsNone(case.invalidation_reason)
        self.assertEqual(result.applied_events, (event,))

    def test_invalidate_target_facts(self):
        source = _valid_case(
            evidence_refs=("e-old",),
            finding_refs=("f-old",),
            execution_receipt_refs=("r-old",),
            policy_decision_refs=("p-old",),
            human_decision_refs=("h-old",),
            conditions=("c-old",),
            conflicts=("cf-old",),
            missing_evidence=("m-old",),
        )
        event = _valid_event(
            "INVALIDATE",
            event_id="evt-invalidate",
            occurred_at=_ts(30),
            evidence_refs=("e-new",),
            finding_refs=("f-new",),
            execution_receipt_refs=("r-new",),
            policy_decision_refs=("p-new",),
            human_decision_refs=("h-new",),
            reason="invalidated by policy drift",
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        case = result.case
        self.assertEqual(case.state, "INVALIDATED")
        self.assertEqual(case.created_at, source.created_at)
        self.assertEqual(case.updated_at, event.occurred_at)
        self._assert_refs_merged(
            case,
            {
                "evidence_refs": ("e-old", "e-new"),
                "finding_refs": ("f-old", "f-new"),
                "execution_receipt_refs": ("r-old", "r-new"),
                "policy_decision_refs": ("p-old", "p-new"),
                "human_decision_refs": ("h-old", "h-new"),
            },
        )
        self.assertEqual(case.conditions, ("c-old",))
        self.assertEqual(case.conflicts, ("cf-old",))
        self.assertEqual(case.missing_evidence, ("m-old",))
        self.assertEqual(case.invalidation_reason, "invalidated by policy drift")
        self.assertEqual(result.applied_events, (event,))

    def test_accept_requires_merged_evidence(self):
        source = _valid_case(
            state="CONDITIONAL_ACCEPTED",
            evidence_refs=(),
            conditions=("condition-001",),
            policy_decision_refs=("policy-001",),
            human_decision_refs=("human-001",),
        )
        event = _valid_event(
            "ACCEPT",
            event_id="evt-accept-no-evidence",
            occurred_at=_ts(0),
        )
        with self.assertRaises(ValidationError):
            apply_acceptance_event(_initial_state(case=source), event)

    def test_accept_event_can_contribute_evidence(self):
        source = _valid_case(
            state="CONDITIONAL_ACCEPTED",
            evidence_refs=(),
            conditions=("condition-001",),
            policy_decision_refs=("policy-001",),
            human_decision_refs=("human-001",),
        )
        event = _valid_event(
            "ACCEPT",
            event_id="evt-accept-with-evidence",
            occurred_at=_ts(0),
            evidence_refs=("evidence-from-event",),
        )
        result = apply_acceptance_event(_initial_state(case=source), event)
        self.assertEqual(result.case.state, "ACCEPTED")
        self.assertEqual(result.case.evidence_refs, ("evidence-from-event",))

    def test_conflicted_cannot_directly_accept(self):
        state = _initial_state(case=_valid_case(state="CONFLICTED"))
        event = _valid_event("ACCEPT", event_id="evt-accept-conflicted")
        with self.assertRaises(InvalidTransitionError):
            apply_acceptance_event(state, event)

    def test_accepted_only_invalidate(self):
        for kind in _ALL_KINDS:
            if kind == "INVALIDATE":
                continue
            with self.subTest(kind=kind):
                state = _initial_state(case=_valid_case(state="ACCEPTED"))
                event = _valid_event(kind, event_id=f"accepted-{kind}")
                with self.assertRaises(InvalidTransitionError):
                    apply_acceptance_event(state, event)
        state = _initial_state(case=_valid_case(state="ACCEPTED"))
        event = _valid_event("INVALIDATE", event_id="accepted-invalidate")
        result = apply_acceptance_event(state, event)
        self.assertEqual(result.case.state, "INVALIDATED")

    def test_rejected_only_invalidate(self):
        for kind in _ALL_KINDS:
            if kind == "INVALIDATE":
                continue
            with self.subTest(kind=kind):
                state = _initial_state(case=_valid_case(state="REJECTED"))
                event = _valid_event(kind, event_id=f"rejected-{kind}")
                with self.assertRaises(InvalidTransitionError):
                    apply_acceptance_event(state, event)
        state = _initial_state(case=_valid_case(state="REJECTED"))
        event = _valid_event("INVALIDATE", event_id="rejected-invalidate")
        result = apply_acceptance_event(state, event)
        self.assertEqual(result.case.state, "INVALIDATED")

    def test_invalidated_has_no_exit(self):
        for kind in _ALL_KINDS:
            with self.subTest(kind=kind):
                state = _initial_state(
                    case=_valid_case(state="INVALIDATED")
                )
                event = _valid_event(kind, event_id=f"invalidated-{kind}")
                with self.assertRaises(InvalidTransitionError):
                    apply_acceptance_event(state, event)


class TestInvalidationReasons(unittest.TestCase):
    def test_no_reasons_when_bound_matches_current_without_waiver(self):
        bound = _valid_binding()
        current = _valid_binding()
        self.assertEqual(invalidation_reasons(bound, current, _ts(30)), ())

    def test_each_reason_alone(self):
        cases = (
            (
                _valid_binding(subject_digest=_digest("a")),
                _valid_binding(subject_digest=_digest("b")),
                ("SUBJECT_DIGEST_CHANGED",),
            ),
            (
                _valid_binding(policy_version="policy-1"),
                _valid_binding(policy_version="policy-2"),
                ("POLICY_VERSION_CHANGED",),
            ),
            (
                _valid_binding(rubric_version="rubric-1"),
                _valid_binding(rubric_version="rubric-2"),
                ("RUBRIC_VERSION_CHANGED",),
            ),
            (
                _valid_binding(
                    waiver_id="w",
                    waiver_expires_at=_ts(0),
                ),
                _valid_binding(),
                ("WAIVER_EXPIRED",),
            ),
        )
        for bound, current, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    invalidation_reasons(bound, current, _ts(1)),
                    expected,
                )

    def test_combined_reasons_fixed_order(self):
        bound = _valid_binding(
            subject_digest=_digest("a"),
            policy_version="policy-1",
            rubric_version="rubric-1",
            waiver_id="w",
            waiver_expires_at=_ts(0),
        )
        current = _valid_binding(
            subject_digest=_digest("b"),
            policy_version="policy-2",
            rubric_version="rubric-2",
            waiver_id="w2",
            waiver_expires_at=_ts(100),
        )
        self.assertEqual(
            invalidation_reasons(bound, current, _ts(1)),
            (
                "SUBJECT_DIGEST_CHANGED",
                "POLICY_VERSION_CHANGED",
                "RUBRIC_VERSION_CHANGED",
                "WAIVER_EXPIRED",
            ),
        )

    def test_waiver_expired_at_exact_expiry(self):
        bound = _valid_binding(
            waiver_id="w",
            waiver_expires_at=_ts(30),
        )
        self.assertEqual(
            invalidation_reasons(bound, _valid_binding(), _ts(30)),
            ("WAIVER_EXPIRED",),
        )

    def test_current_waiver_extension_does_not_hide_bound_expiry(self):
        bound = _valid_binding(
            waiver_id="w",
            waiver_expires_at=_ts(0),
        )
        current = _valid_binding(
            waiver_id="w2",
            waiver_expires_at=_ts(100),
        )
        self.assertEqual(
            invalidation_reasons(bound, current, _ts(1)),
            ("WAIVER_EXPIRED",),
        )

    def test_current_waiver_removal_does_not_hide_bound_expiry(self):
        bound = _valid_binding(
            waiver_id="w",
            waiver_expires_at=_ts(0),
        )
        self.assertEqual(
            invalidation_reasons(bound, _valid_binding(), _ts(1)),
            ("WAIVER_EXPIRED",),
        )

    def test_naive_now_rejected(self):
        bound = _valid_binding()
        current = _valid_binding()
        with self.assertRaises(ValueError):
            invalidation_reasons(bound, current, datetime(2026, 8, 25, 2, 30))

    def test_non_datetime_now_rejected(self):
        bound = _valid_binding()
        current = _valid_binding()
        with self.assertRaises(TypeError):
            invalidation_reasons(bound, current, "2026-08-25T02:30:00+00:00")

    def test_wrong_binding_types_rejected(self):
        binding = _valid_binding()
        with self.assertRaises(TypeError):
            invalidation_reasons(None, binding, _ts(1))
        with self.assertRaises(TypeError):
            invalidation_reasons(binding, {}, _ts(1))


class TestInvalidateIfNeeded(unittest.TestCase):
    def test_no_reasons_returns_same_state(self):
        state = _initial_state()
        bound = _valid_binding()
        current = _valid_binding()
        self.assertIs(
            invalidate_if_needed(state, bound, current, _ts(1), "unused"),
            state,
        )

    def test_reason_transition_generates_invalidate_event(self):
        state = _initial_state()
        bound = _valid_binding(
            subject_digest=_digest("a"),
            policy_version="policy-1",
            rubric_version="rubric-1",
            waiver_id="w",
            waiver_expires_at=_ts(0),
        )
        current = _valid_binding(
            subject_digest=_digest("b"),
            policy_version="policy-2",
            rubric_version="rubric-2",
        )
        now = _ts(1)
        result = invalidate_if_needed(
            state, bound, current, now, "invalidate-1"
        )
        self.assertIsNot(result, state)
        self.assertEqual(result.case.state, "INVALIDATED")
        self.assertEqual(
            result.case.invalidation_reason,
            "SUBJECT_DIGEST_CHANGED,POLICY_VERSION_CHANGED,"
            "RUBRIC_VERSION_CHANGED,WAIVER_EXPIRED",
        )
        self.assertEqual(len(result.applied_events), 1)
        event = result.applied_events[0]
        self.assertEqual(event.event_id, "invalidate-1")
        self.assertEqual(event.subject_digest, bound.subject_digest)
        self.assertEqual(event.kind, "INVALIDATE")
        self.assertEqual(
            event.reason,
            "SUBJECT_DIGEST_CHANGED,POLICY_VERSION_CHANGED,"
            "RUBRIC_VERSION_CHANGED,WAIVER_EXPIRED",
        )
        self.assertEqual(event.occurred_at, now)
        self.assertEqual(result.case.updated_at, now)
        self.assertEqual(result.case.created_at, state.case.created_at)

    def test_repeated_exact_idempotency_returns_same_state(self):
        state = _initial_state()
        bound = _valid_binding()
        current = _valid_binding(subject_digest=_digest("b"))
        now = _ts(1)
        first = invalidate_if_needed(
            state, bound, current, now, "invalidate-1"
        )
        second = invalidate_if_needed(
            first, bound, current, now, "invalidate-1"
        )
        self.assertIs(second, first)

    def test_same_id_different_payload_raises_conflict(self):
        state = _initial_state()
        bound = _valid_binding()
        current = _valid_binding(subject_digest=_digest("b"))
        first = invalidate_if_needed(
            state, bound, current, _ts(1), "invalidate-1"
        )
        with self.assertRaises(EventConflictError):
            invalidate_if_needed(
                first, bound, current, _ts(2), "invalidate-1"
            )

    def test_already_invalidated_new_event_id_returns_same_state(self):
        state = _initial_state()
        bound = _valid_binding()
        current = _valid_binding(subject_digest=_digest("b"))
        first = invalidate_if_needed(
            state, bound, current, _ts(1), "invalidate-1"
        )
        again = invalidate_if_needed(
            first, bound, current, _ts(2), "invalidate-2"
        )
        self.assertIs(again, first)
        self.assertEqual(len(again.applied_events), 1)

    def test_bound_mismatch_raises_stale_subject(self):
        state = _initial_state()
        bound = _valid_binding(subject_digest=_digest("b"))
        current = _valid_binding(subject_digest=_digest("b"))
        with self.assertRaises(StaleSubjectError):
            invalidate_if_needed(
                state, bound, current, _ts(1), "invalidate-1"
            )

    def test_invalid_argument_types_rejected(self):
        state = _initial_state()
        bound = _valid_binding()
        current = _valid_binding()
        with self.assertRaises(TypeError):
            invalidate_if_needed(None, bound, current, _ts(1), "e")
        with self.assertRaises(TypeError):
            invalidate_if_needed(state, {}, current, _ts(1), "e")
        with self.assertRaises(TypeError):
            invalidate_if_needed(state, bound, None, _ts(1), "e")
        with self.assertRaises(TypeError):
            invalidate_if_needed(state, bound, current, "not-a-time", "e")
        with self.assertRaises(ValueError):
            invalidate_if_needed(
                state, bound, current, datetime(2026, 8, 25, 2, 30), "e"
            )
        with self.assertRaises(TypeError):
            invalidate_if_needed(state, bound, current, _ts(1), 123)
        for bad in ("", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    invalidate_if_needed(state, bound, current, _ts(1), bad)


if __name__ == "__main__":
    unittest.main()
