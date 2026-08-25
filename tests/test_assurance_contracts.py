"""保障域领域合同单元测试（ChangeSubject / Evidence / Finding / ExecutionStep / ExecutionReceipt）。

跑法：
    PYTHONPATH=. python -m unittest -v tests.test_assurance_contracts
"""

import tomllib
import unittest
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from assurance.contracts import (
    AcceptanceCase,
    ChangeSubject,
    Evidence,
    ExecutionReceipt,
    ExecutionStep,
    Finding,
    HumanDecision,
    PolicyDecision,
)


def _valid_subject(**overrides):
    values = {
        "schema_version": "v1",
        "change_id": "change-001",
        "subject_digest": "sha256:" + "a" * 64,
        "repository": "acme/service",
        "base_revision": "base-abc123",
        "head_revision": "head-def456",
        "task_digest": "sha256:" + "b" * 64,
        "policy_version": "policy-1",
        "created_at": "2026-08-25T02:30:00+08:00",
    }
    values.update(overrides)
    return ChangeSubject(**values)


class TestChangeSubjectContract(unittest.TestCase):
    def test_valid_construction_and_round_trip(self):
        subject = _valid_subject()
        dumped = subject.model_dump(mode="json")
        restored = ChangeSubject.model_validate(dumped)
        self.assertEqual(restored, subject)
        self.assertEqual(restored.created_at, subject.created_at)
        self.assertIsNotNone(subject.created_at.tzinfo)

    def test_repeated_json_serialization_is_stable(self):
        subject = _valid_subject()
        self.assertEqual(subject.model_dump_json(), subject.model_dump_json())
        self.assertEqual(subject.model_dump(mode="json"), subject.model_dump(mode="json"))
        self.assertEqual(
            list(subject.model_dump().keys()),
            [
                "schema_version",
                "change_id",
                "subject_digest",
                "repository",
                "base_revision",
                "head_revision",
                "task_digest",
                "policy_version",
                "created_at",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        subject = _valid_subject()
        self.assertEqual(subject.schema_version, "v1")
        values = subject.model_dump()
        values.pop("schema_version")
        self.assertEqual(ChangeSubject(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_subject(schema_version="v2")

    def test_invalid_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for field in ("subject_digest", "task_digest"):
            for bad in bad_digests:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_subject(**{field: bad})

    def test_whitespace_only_identity_field_rejected(self):
        for field in (
            "change_id",
            "repository",
            "base_revision",
            "head_revision",
            "policy_version",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_subject(**{field: "   "})

    def test_naive_created_at_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_subject(created_at=datetime(2026, 8, 25, 2, 30))

    def test_unknown_field_rejected(self):
        values = _valid_subject().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            ChangeSubject.model_validate(values)

    def test_assignment_mutation_rejected(self):
        subject = _valid_subject()
        with self.assertRaises(ValidationError):
            subject.change_id = "mutated"


def _valid_evidence(**overrides):
    values = {
        "schema_version": "v1",
        "evidence_id": "evidence-001",
        "subject_digest": "sha256:" + "c" * 64,
        "kind": "test-run",
        "producer": "codemesh-assurance",
        "artifact_digest": "sha256:" + "d" * 64,
        "source_ref": "tests/test_assurance_contracts.py",
        "trace_id": "trace-abc123",
        "status": "success",
        "trust_level": "observed",
        "collected_at": "2026-08-25T02:30:00+08:00",
    }
    values.update(overrides)
    return Evidence(**values)


def _valid_finding(**overrides):
    values = {
        "schema_version": "v1",
        "finding_id": "finding-001",
        "subject_digest": "sha256:" + "a" * 64,
        "reviewer_role": "intent",
        "claim": "room control path is not end-to-end deterministic",
        "evidence_refs": ["evidence-001", "evidence-002"],
        "basis": "inferred",
        "severity": "medium",
        "confidence": 0.8,
        "rubric_hash": "sha256:" + "b" * 64,
        "model_ref": "gpt-5.6-sol",
        "status": "open",
    }
    values.update(overrides)
    return Finding(**values)


def _valid_step(**overrides):
    values = {
        "sequence": 0,
        "planned_role": "intent",
        "actual_role": "architecture",
        "model_ref": "gpt-5.6-sol",
        "provider": "openai",
        "tool_grants": [],
        "routing_rule": "allow-all",
        "fallback_reason": None,
        "token_budget": 8000,
        "timeout_seconds": 120,
        "result": "success",
        "schema_status": "valid",
    }
    values.update(overrides)
    return ExecutionStep(**values)


def _valid_receipt(**overrides):
    values = {
        "schema_version": "v1",
        "receipt_id": "receipt-001",
        "run_id": "run-001",
        "subject_digest": "sha256:" + "e" * 64,
        "steps": [_valid_step()],
        "overall_result": "success",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.42,
        "started_at": "2026-08-25T02:30:00+08:00",
        "completed_at": "2026-08-25T02:31:00+08:00",
    }
    values.update(overrides)
    return ExecutionReceipt(**values)


def _valid_policy_decision(**overrides):
    values = {
        "schema_version": "v1",
        "decision_id": "decision-001",
        "subject_digest": "sha256:" + "a" * 64,
        "policy_version": "policy-1",
        "rules_digest": "sha256:" + "b" * 64,
        "outcome": "PASS",
        "reason_codes": [],
        "required_collectors": [],
        "required_reviewers": [],
        "required_human_role": None,
        "evaluated_evidence_refs": [],
        "evaluated_finding_refs": [],
        "evaluated_receipt_refs": [],
        "waiver_ref": None,
        "evaluated_at": "2026-08-25T02:30:00+08:00",
    }
    values.update(overrides)
    return PolicyDecision(**values)


def _valid_human_decision(**overrides):
    values = {
        "schema_version": "v1",
        "decision_id": "human-decision-001",
        "subject_digest": "sha256:" + "a" * 64,
        "actor_type": "human",
        "owner": "owner-001",
        "owner_role": "security-officer",
        "decision": "approve",
        "reason": "approved after review",
        "conditions": [],
        "waiver_id": None,
        "expires_at": None,
        "decided_at": "2026-08-25T02:30:00+08:00",
    }
    values.update(overrides)
    return HumanDecision(**values)


def _valid_acceptance_case(**overrides):
    values = {
        "schema_version": "v1",
        "case_id": "case-001",
        "subject_digest": "sha256:" + "a" * 64,
        "state": "DRAFT",
        "evidence_refs": (),
        "finding_refs": (),
        "execution_receipt_refs": (),
        "policy_decision_refs": (),
        "human_decision_refs": (),
        "conditions": (),
        "conflicts": (),
        "missing_evidence": (),
        "invalidation_reason": None,
        "created_at": "2026-08-25T02:30:00+08:00",
        "updated_at": "2026-08-25T02:30:00+08:00",
    }
    values.update(overrides)
    return AcceptanceCase(**values)


_ACCEPTANCE_STATE_FACTS = {
    "DRAFT": {},
    "EVIDENCE_COLLECTED": {"evidence_refs": ["evidence-001"]},
    "NEEDS_EVIDENCE": {"missing_evidence": ["missing-001"]},
    "CONFLICTED": {"conflicts": ["conflict-001"]},
    "CONDITIONAL_ACCEPTED": {
        "conditions": ["condition-001"],
        "policy_decision_refs": ["policy-001"],
        "human_decision_refs": ["human-001"],
    },
    "ACCEPTED": {
        "evidence_refs": ["evidence-001"],
        "policy_decision_refs": ["policy-001"],
        "human_decision_refs": ["human-001"],
    },
    "REJECTED": {"policy_decision_refs": ["policy-001"]},
    "INVALIDATED": {"invalidation_reason": "duplicate case"},
}


class TestEvidenceContract(unittest.TestCase):
    def test_valid_construction_and_lossless_json_round_trip(self):
        evidence = _valid_evidence()
        dumped = evidence.model_dump(mode="json")
        restored = Evidence.model_validate(dumped)
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsNotNone(evidence.collected_at.tzinfo)

    def test_repeated_json_serialization_is_stable(self):
        evidence = _valid_evidence()
        self.assertEqual(evidence.model_dump_json(), evidence.model_dump_json())
        self.assertEqual(
            evidence.model_dump(mode="json"), evidence.model_dump(mode="json")
        )
        self.assertEqual(
            list(evidence.model_dump().keys()),
            [
                "schema_version",
                "evidence_id",
                "subject_digest",
                "kind",
                "producer",
                "artifact_digest",
                "source_ref",
                "trace_id",
                "status",
                "trust_level",
                "collected_at",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        evidence = _valid_evidence()
        self.assertEqual(evidence.schema_version, "v1")
        values = evidence.model_dump()
        values.pop("schema_version")
        self.assertEqual(Evidence(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_evidence(schema_version="v2")

    def test_invalid_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for field in ("subject_digest", "artifact_digest"):
            for bad in bad_digests:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_evidence(**{field: bad})

    def test_empty_or_whitespace_source_ref_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_evidence(source_ref=bad)

    def test_present_but_blank_trace_id_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_evidence(trace_id=bad)
        values = _valid_evidence().model_dump()
        values.pop("trace_id")
        self.assertIsNone(Evidence(**values).trace_id)

    def test_invalid_status_rejected(self):
        for bad in ("passed", "pending", "SUCCESS", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_evidence(status=bad)

    def test_invalid_trust_level_rejected(self):
        for bad in ("none", "OBSERVED", "auto", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_evidence(trust_level=bad)

    def test_naive_collected_at_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_evidence(collected_at=datetime(2026, 8, 25, 2, 30))

    def test_unknown_field_rejected(self):
        values = _valid_evidence().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            Evidence.model_validate(values)

    def test_assignment_mutation_rejected(self):
        evidence = _valid_evidence()
        with self.assertRaises(ValidationError):
            evidence.status = "failure"


class TestFindingContract(unittest.TestCase):
    def test_valid_construction_and_lossless_json_round_trip(self):
        finding = _valid_finding()
        dumped = finding.model_dump(mode="json")
        restored = Finding.model_validate(dumped)
        self.assertEqual(restored, finding)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsInstance(finding.evidence_refs, tuple)
        self.assertIsInstance(finding.model_dump()["evidence_refs"], tuple)
        self.assertIsInstance(dumped["evidence_refs"], list)

    def test_repeated_json_serialization_is_stable_and_field_order(self):
        finding = _valid_finding()
        self.assertEqual(finding.model_dump_json(), finding.model_dump_json())
        self.assertEqual(
            finding.model_dump(mode="json"), finding.model_dump(mode="json")
        )
        self.assertEqual(
            list(finding.model_dump().keys()),
            [
                "schema_version",
                "finding_id",
                "subject_digest",
                "reviewer_role",
                "claim",
                "evidence_refs",
                "basis",
                "severity",
                "confidence",
                "rubric_hash",
                "model_ref",
                "status",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        finding = _valid_finding()
        self.assertEqual(finding.schema_version, "v1")
        values = finding.model_dump()
        values.pop("schema_version")
        self.assertEqual(Finding(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_finding(schema_version="v2")

    def test_invalid_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for field in ("subject_digest", "rubric_hash"):
            for bad in bad_digests:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_finding(**{field: bad})

    def test_empty_or_whitespace_text_fields_rejected(self):
        for field in ("finding_id", "claim", "model_ref"):
            for bad in ("", "   ", "\t"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_finding(**{field: bad})

    def test_empty_evidence_refs_rejected(self):
        for bad in ([], ()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_finding(evidence_refs=bad)

    def test_duplicate_evidence_refs_rejected(self):
        for bad in (
            ["evidence-001", "evidence-001"],
            ("evidence-001", "evidence-001"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_finding(evidence_refs=bad)

    def test_blank_evidence_ref_item_rejected(self):
        for bad in (
            ["evidence-001", ""],
            ["evidence-001", "   "],
            ["evidence-001", "\t"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_finding(evidence_refs=bad)

    def test_evidence_ref_input_order_preserved(self):
        finding = _valid_finding(
            evidence_refs=["evidence-002", "evidence-001", "evidence-003"]
        )
        self.assertEqual(
            finding.evidence_refs,
            ("evidence-002", "evidence-001", "evidence-003"),
        )

    def test_invalid_literals_rejected(self):
        cases = {
            "reviewer_role": ("designer", ""),
            "basis": ("unknown", ""),
            "severity": ("none", ""),
            "status": ("closed", ""),
        }
        for field, bads in cases.items():
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_finding(**{field: bad})

    def test_confidence_bounds(self):
        for value in (0.0, 1.0):
            with self.subTest(value=value):
                self.assertEqual(_valid_finding(confidence=value).confidence, value)
        for value in (-0.01, 1.01):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    _valid_finding(confidence=value)

    def test_confidence_accepts_real_json_number_inputs(self):
        for value, expected in (
            (0, 0.0),
            (1, 1.0),
            (0.0, 0.0),
            (1.0, 1.0),
            (0.5, 0.5),
        ):
            with self.subTest(value=value):
                self.assertEqual(_valid_finding(confidence=value).confidence, expected)

    def test_confidence_rejects_bool_numeric_strings_and_non_finite(self):
        for bad in (
            True,
            False,
            "0.8",
            "1",
            float("nan"),
            float("inf"),
            float("-inf"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_finding(confidence=bad)

    def test_unknown_field_rejected(self):
        values = _valid_finding().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            Finding.model_validate(values)

    def test_assignment_mutation_rejected(self):
        finding = _valid_finding()
        with self.assertRaises(ValidationError):
            finding.status = "resolved"

    def test_evidence_refs_are_deeply_immutable_and_input_copy_safe(self):
        source = ["evidence-001", "evidence-002"]
        finding = _valid_finding(evidence_refs=source)
        self.assertIsInstance(finding.evidence_refs, tuple)
        with self.assertRaises(TypeError):
            finding.evidence_refs[0] = "evidence-009"
        with self.assertRaises(AttributeError):
            finding.evidence_refs.append("evidence-009")
        source.append("evidence-003")
        source[0] = "mutated"
        self.assertEqual(
            finding.evidence_refs, ("evidence-001", "evidence-002")
        )


class TestExecutionStepContract(unittest.TestCase):
    def test_valid_construction_and_lossless_json_round_trip(self):
        step = _valid_step()
        dumped = step.model_dump(mode="json")
        restored = ExecutionStep.model_validate(dumped)
        self.assertEqual(restored, step)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsInstance(step.tool_grants, tuple)
        self.assertIsInstance(step.model_dump()["tool_grants"], tuple)
        self.assertIsInstance(dumped["tool_grants"], list)

    def test_repeated_json_serialization_is_stable_and_field_order(self):
        step = _valid_step()
        self.assertEqual(step.model_dump_json(), step.model_dump_json())
        self.assertEqual(
            step.model_dump(mode="json"), step.model_dump(mode="json")
        )
        self.assertEqual(
            list(step.model_dump().keys()),
            [
                "sequence",
                "planned_role",
                "actual_role",
                "model_ref",
                "provider",
                "tool_grants",
                "routing_rule",
                "fallback_reason",
                "token_budget",
                "timeout_seconds",
                "result",
                "schema_status",
            ],
        )

    def test_whitespace_only_routing_rule_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(routing_rule=bad)

    def test_present_optional_strings_reject_whitespace_but_none_allowed(self):
        base = {
            "actual_role": None,
            "model_ref": None,
            "provider": None,
            "fallback_reason": None,
            "result": "skipped",
            "schema_status": "not_produced",
        }
        for field in ("model_ref", "provider", "fallback_reason"):
            with self.subTest(field=field, none=True):
                values = dict(base)
                values[field] = None
                self.assertIsNone(getattr(_valid_step(**values), field))
            for bad in ("", "   ", "\t"):
                with self.subTest(field=field, bad=bad):
                    values = dict(base)
                    values[field] = bad
                    with self.assertRaises(ValidationError):
                        _valid_step(**values)

    def test_invalid_enum_rejected(self):
        cases = {
            "planned_role": ("designer", "", "INTENT"),
            "actual_role": ("designer", "", "INTENT"),
            "result": ("passed", "SUCCESS", ""),
            "schema_status": ("unknown", "VALID", ""),
        }
        for field, bads in cases.items():
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_step(**{field: bad})

    def test_numeric_boundaries(self):
        self.assertEqual(_valid_step(sequence=0).sequence, 0)
        for bad in (-1, -100):
            with self.subTest(field="sequence", bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(sequence=bad)
        for value in (None, 0, 100):
            with self.subTest(token_budget=value):
                self.assertEqual(_valid_step(token_budget=value).token_budget, value)
        for bad in (-1, -100):
            with self.subTest(field="token_budget", bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(token_budget=bad)
        self.assertEqual(_valid_step(timeout_seconds=1).timeout_seconds, 1)
        for bad in (0, -5):
            with self.subTest(timeout_seconds=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(timeout_seconds=bad)

    def test_numeric_fields_reject_bool_and_numeric_strings(self):
        for field in ("sequence", "token_budget", "timeout_seconds"):
            for bad in (True, False, "1", "0", "0.5", 0.0, 1.0, 0.5):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_step(**{field: bad})

    def test_executed_outcomes_require_actual_role_model_and_provider(self):
        for result in ("success", "failure", "timeout", "cancelled"):
            for field in ("actual_role", "model_ref", "provider"):
                with self.subTest(result=result, field=field):
                    values = {"result": result, field: None}
                    if result == "success":
                        values["schema_status"] = "valid"
                    elif result in ("timeout", "cancelled"):
                        values["schema_status"] = "not_produced"
                    with self.assertRaises(ValidationError):
                        _valid_step(**values)

    def test_skipped_blocked_actual_role_must_be_none(self):
        for result in ("skipped", "blocked"):
            for bad in ("intent", "architecture", "operability"):
                with self.subTest(result=result, actual_role=bad):
                    with self.assertRaises(ValidationError):
                        _valid_step(
                            actual_role=bad,
                            result=result,
                            schema_status="not_produced",
                        )
            step = _valid_step(
                actual_role=None,
                model_ref="gpt-5.6-sol",
                provider="openai",
                result=result,
                schema_status="not_produced",
            )
            self.assertIsNone(step.actual_role)
            self.assertEqual(step.model_ref, "gpt-5.6-sol")
            self.assertEqual(step.provider, "openai")

    def test_schema_status_combinations(self):
        for status in ("valid", "repaired"):
            with self.subTest(result="success", schema_status=status):
                self.assertEqual(_valid_step(schema_status=status).schema_status, status)
        for bad in ("invalid", "not_produced"):
            with self.subTest(result="success", schema_status=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(schema_status=bad)
        for result in ("skipped", "blocked", "timeout", "cancelled"):
            values = {"result": result}
            if result in ("skipped", "blocked"):
                values["actual_role"] = None
            for status in ("valid", "repaired", "invalid"):
                with self.subTest(result=result, schema_status=status):
                    with self.assertRaises(ValidationError):
                        _valid_step(**{**values, "schema_status": status})
            with self.subTest(result=result, schema_status="not_produced"):
                self.assertEqual(
                    _valid_step(**{**values, "schema_status": "not_produced"}).schema_status,
                    "not_produced",
                )
        for status in ("valid", "repaired", "invalid", "not_produced"):
            with self.subTest(result="failure", schema_status=status):
                self.assertEqual(
                    _valid_step(result="failure", schema_status=status).schema_status,
                    status,
                )

    def test_tool_grants_duplicate_and_blank_rejected(self):
        for bad in (
            ["read", "read"],
            ("read", "read"),
            ["read", ""],
            ["read", "   "],
            ["read", "\t"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_step(tool_grants=bad)

    def test_tool_grants_order_tuple_json_and_input_copy_safety(self):
        source = ["read", "write", "execute"]
        step = _valid_step(tool_grants=source)
        self.assertEqual(step.tool_grants, ("read", "write", "execute"))
        self.assertEqual(
            step.model_dump()["tool_grants"], ("read", "write", "execute")
        )
        self.assertEqual(
            step.model_dump(mode="json")["tool_grants"],
            ["read", "write", "execute"],
        )
        with self.assertRaises(TypeError):
            step.tool_grants[0] = "mutated"
        with self.assertRaises(AttributeError):
            step.tool_grants.append("extra")
        source.append("extra")
        source[0] = "mutated"
        self.assertEqual(step.tool_grants, ("read", "write", "execute"))

    def test_tool_grants_default_empty(self):
        step = _valid_step(tool_grants=())
        self.assertEqual(step.tool_grants, ())

    def test_no_extra_role_or_fallback_inference(self):
        step = _valid_step(
            planned_role="intent",
            actual_role="operability",
            fallback_reason="quota-exhausted",
        )
        self.assertEqual(step.planned_role, "intent")
        self.assertEqual(step.actual_role, "operability")
        self.assertEqual(step.fallback_reason, "quota-exhausted")

    def test_unknown_field_rejected(self):
        values = _valid_step().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            ExecutionStep.model_validate(values)

    def test_assignment_mutation_rejected(self):
        step = _valid_step()
        with self.assertRaises(ValidationError):
            step.result = "failure"


class TestExecutionReceiptContract(unittest.TestCase):
    def test_valid_construction_and_lossless_json_round_trip(self):
        second = _valid_step(
            sequence=1,
            planned_role="operability",
            actual_role=None,
            model_ref=None,
            provider=None,
            result="skipped",
            schema_status="not_produced",
        )
        receipt = _valid_receipt(
            steps=[_valid_step(), second], overall_result="partial"
        )
        dumped = receipt.model_dump(mode="json")
        restored = ExecutionReceipt.model_validate(dumped)
        self.assertEqual(restored, receipt)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsInstance(receipt.steps, tuple)
        self.assertIsInstance(receipt.model_dump()["steps"], tuple)
        self.assertIsInstance(dumped["steps"], list)
        self.assertIsInstance(restored.steps[0], ExecutionStep)
        self.assertIsNotNone(receipt.started_at.tzinfo)
        self.assertIsNotNone(receipt.completed_at.tzinfo)

    def test_repeated_json_serialization_is_stable_and_field_order(self):
        receipt = _valid_receipt()
        self.assertEqual(receipt.model_dump_json(), receipt.model_dump_json())
        self.assertEqual(
            receipt.model_dump(mode="json"), receipt.model_dump(mode="json")
        )
        self.assertEqual(
            list(receipt.model_dump().keys()),
            [
                "schema_version",
                "receipt_id",
                "run_id",
                "subject_digest",
                "steps",
                "overall_result",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "started_at",
                "completed_at",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        receipt = _valid_receipt()
        self.assertEqual(receipt.schema_version, "v1")
        values = receipt.model_dump()
        values.pop("schema_version")
        self.assertEqual(ExecutionReceipt(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_receipt(schema_version="v2")

    def test_invalid_overall_result_rejected(self):
        for bad in ("finished", "SUCCESS", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_receipt(overall_result=bad)

    def test_receipt_and_run_id_whitespace_rejected(self):
        for field in ("receipt_id", "run_id"):
            for bad in ("", "   ", "\t"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_receipt(**{field: bad})

    def test_subject_digest_validated(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "e" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "e" * 64,
            "sha256:" + "e" * 65,
        )
        for bad in bad_digests:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_receipt(subject_digest=bad)

    def test_steps_required_and_sequence_rules(self):
        values = _valid_receipt().model_dump()
        values.pop("steps")
        with self.assertRaises(ValidationError):
            ExecutionReceipt(**values)
        for bad in ([], ()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_receipt(steps=bad)
        for bad_steps in (
            [
                _valid_step(),
                _valid_step(sequence=2, result="failure", schema_status="invalid"),
            ],
            [
                _valid_step(sequence=1, result="failure", schema_status="invalid"),
                _valid_step(),
            ],
            [_valid_step(sequence=1, result="failure", schema_status="invalid")],
            [_valid_step(), _valid_step(sequence=0)],
        ):
            with self.subTest(bad_steps=bad_steps):
                with self.assertRaises(ValidationError):
                    _valid_receipt(steps=bad_steps, overall_result="partial")

    def test_steps_tuple_json_behavior_and_input_copy_safety(self):
        source = [
            _valid_step().model_dump(mode="json"),
            _valid_step(
                sequence=1, result="failure", schema_status="invalid"
            ).model_dump(mode="json"),
        ]
        receipt = _valid_receipt(steps=source, overall_result="failure")
        self.assertIsInstance(receipt.steps, tuple)
        self.assertEqual(receipt.steps[1].sequence, 1)
        self.assertEqual(
            receipt.model_dump()["steps"][0], receipt.steps[0].model_dump()
        )
        self.assertIsInstance(receipt.model_dump(mode="json")["steps"], list)
        source[0]["sequence"] = 99
        source[0]["result"] = "blocked"
        source[1]["tool_grants"].append("mutated")
        self.assertEqual(receipt.steps[0].sequence, 0)
        self.assertEqual(receipt.steps[0].result, "success")
        self.assertEqual(receipt.steps[1].tool_grants, ())

    def test_nested_step_frozen_mutation_rejected(self):
        receipt = _valid_receipt()
        with self.assertRaises(ValidationError):
            receipt.steps[0].result = "failure"

    def test_naive_and_reversed_times_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_receipt(started_at=datetime(2026, 8, 25, 2, 30))
        with self.assertRaises(ValidationError):
            _valid_receipt(completed_at=datetime(2026, 8, 25, 2, 31))
        with self.assertRaises(ValidationError):
            _valid_receipt(
                started_at="2026-08-25T02:31:00+08:00",
                completed_at="2026-08-25T02:30:00+08:00",
            )
        equal = _valid_receipt(
            started_at="2026-08-25T02:30:00+08:00",
            completed_at="2026-08-25T02:30:00+08:00",
        )
        self.assertEqual(equal.started_at, equal.completed_at)

    def test_token_and_cost_boundaries(self):
        values = _valid_receipt().model_dump()
        for key in ("input_tokens", "output_tokens", "cost_usd"):
            values.pop(key)
        minimal = ExecutionReceipt(**values)
        self.assertEqual(
            (minimal.input_tokens, minimal.output_tokens, minimal.cost_usd),
            (0, 0, 0),
        )
        for field in ("input_tokens", "output_tokens"):
            for bad in (-1, -100):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_receipt(**{field: bad})
        for bad in (-0.01, -1.0):
            with self.subTest(cost_usd=bad):
                with self.assertRaises(ValidationError):
                    _valid_receipt(cost_usd=bad)

    def test_token_fields_reject_bool_and_numeric_strings(self):
        for field in ("input_tokens", "output_tokens"):
            for bad in (True, False, "1", "0", "0.5", 0.0, 1.0, 0.5):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_receipt(**{field: bad})

    def test_cost_usd_accepts_real_json_number_inputs(self):
        for value, expected in (
            (0, 0.0),
            (1, 1.0),
            (0.0, 0.0),
            (1.0, 1.0),
            (0.42, 0.42),
        ):
            with self.subTest(value=value):
                self.assertEqual(_valid_receipt(cost_usd=value).cost_usd, expected)

    def test_cost_usd_rejects_bool_numeric_strings_and_non_finite(self):
        for bad in (
            True,
            False,
            "0.42",
            "1",
            float("nan"),
            float("inf"),
            float("-inf"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_receipt(cost_usd=bad)

    def test_overall_success_requires_all_steps_success(self):
        good = _valid_receipt(steps=[_valid_step(), _valid_step(sequence=1)])
        self.assertEqual(good.overall_result, "success")
        for result in ("failure", "blocked", "cancelled", "skipped", "timeout"):
            if result in ("skipped", "blocked"):
                step = _valid_step(
                    sequence=1,
                    actual_role=None,
                    result=result,
                    schema_status="not_produced",
                )
            else:
                step = _valid_step(
                    sequence=1, result=result, schema_status="not_produced"
                )
            with self.subTest(result=result):
                with self.assertRaises(ValidationError):
                    _valid_receipt(steps=[_valid_step(), step], overall_result="success")

    def test_blocked_step_forces_blocked_overall(self):
        blocked_step = _valid_step(
            sequence=1,
            actual_role=None,
            result="blocked",
            schema_status="not_produced",
        )
        receipt = _valid_receipt(
            steps=[_valid_step(), blocked_step], overall_result="blocked"
        )
        self.assertEqual(receipt.overall_result, "blocked")
        for overall in ("success", "partial", "failure", "cancelled"):
            with self.subTest(overall=overall):
                with self.assertRaises(ValidationError):
                    _valid_receipt(
                        steps=[_valid_step(), blocked_step], overall_result=overall
                    )

    def test_blocked_overall_does_not_require_blocked_step(self):
        receipt = _valid_receipt(
            steps=[
                _valid_step(),
                _valid_step(
                    sequence=1,
                    actual_role=None,
                    result="skipped",
                    schema_status="not_produced",
                ),
            ],
            overall_result="blocked",
        )
        self.assertEqual(receipt.overall_result, "blocked")

    def test_cancelled_overall_requires_cancelled_step(self):
        cancelled_step = _valid_step(
            sequence=1, result="cancelled", schema_status="not_produced"
        )
        receipt = _valid_receipt(
            steps=[_valid_step(), cancelled_step], overall_result="cancelled"
        )
        self.assertEqual(receipt.overall_result, "cancelled")
        for other in ("success", "skipped"):
            if other == "skipped":
                step = _valid_step(
                    sequence=1,
                    actual_role=None,
                    result=other,
                    schema_status="not_produced",
                )
            else:
                step = _valid_step(sequence=1)
            with self.subTest(other=other):
                with self.assertRaises(ValidationError):
                    _valid_receipt(
                        steps=[_valid_step(), step], overall_result="cancelled"
                    )

    def test_cancelled_step_does_not_force_cancelled_overall(self):
        cancelled_step = _valid_step(
            sequence=1, result="cancelled", schema_status="not_produced"
        )
        receipt = _valid_receipt(
            steps=[_valid_step(), cancelled_step], overall_result="partial"
        )
        self.assertEqual(receipt.overall_result, "partial")

    def test_partial_and_failure_allow_mixed_steps(self):
        skipped = _valid_step(
            sequence=1,
            actual_role=None,
            result="skipped",
            schema_status="not_produced",
        )
        failed = _valid_step(
            sequence=1, result="failure", schema_status="invalid"
        )
        partial = _valid_receipt(
            steps=[_valid_step(), skipped], overall_result="partial"
        )
        failure = _valid_receipt(
            steps=[_valid_step(), failed], overall_result="failure"
        )
        self.assertEqual(partial.overall_result, "partial")
        self.assertEqual(failure.overall_result, "failure")

    def test_unknown_field_rejected(self):
        values = _valid_receipt().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            ExecutionReceipt.model_validate(values)

    def test_assignment_mutation_rejected(self):
        receipt = _valid_receipt()
        with self.assertRaises(ValidationError):
            receipt.overall_result = "failure"
        with self.assertRaises(ValidationError):
            receipt.steps = ()


class TestPolicyDecisionContract(unittest.TestCase):
    def test_valid_construction_and_lossless_round_trip(self):
        from assurance import PolicyDecision as PackagePolicyDecision

        decision = _valid_policy_decision(
            reason_codes=["code-1"],
            required_collectors=["collector-1"],
            required_reviewers=["intent", "operability"],
            required_human_role="security-officer",
            evaluated_evidence_refs=["evidence-1"],
            evaluated_finding_refs=["finding-1"],
            evaluated_receipt_refs=["receipt-1"],
        )
        dumped = decision.model_dump(mode="json")
        restored = PolicyDecision.model_validate(dumped)
        self.assertEqual(restored, decision)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsNotNone(decision.evaluated_at.tzinfo)
        self.assertIs(PackagePolicyDecision, PolicyDecision)
        self.assertIsInstance(decision.reason_codes, tuple)

    def test_repeated_json_serialization_is_stable_and_field_order(self):
        decision = _valid_policy_decision(
            reason_codes=["code-1"],
            required_collectors=["collector-1"],
            required_reviewers=["intent", "operability"],
            evaluated_evidence_refs=["evidence-1"],
            evaluated_finding_refs=["finding-1"],
            evaluated_receipt_refs=["receipt-1"],
        )
        self.assertEqual(decision.model_dump_json(), decision.model_dump_json())
        self.assertEqual(
            decision.model_dump(mode="json"),
            decision.model_dump(mode="json"),
        )
        self.assertEqual(
            list(decision.model_dump().keys()),
            [
                "schema_version",
                "decision_id",
                "subject_digest",
                "policy_version",
                "rules_digest",
                "outcome",
                "reason_codes",
                "required_collectors",
                "required_reviewers",
                "required_human_role",
                "evaluated_evidence_refs",
                "evaluated_finding_refs",
                "evaluated_receipt_refs",
                "waiver_ref",
                "evaluated_at",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        decision = _valid_policy_decision()
        self.assertEqual(decision.schema_version, "v1")
        values = decision.model_dump()
        values.pop("schema_version")
        self.assertEqual(PolicyDecision(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_policy_decision(schema_version="v2")

    def test_invalid_digests_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for field in ("subject_digest", "rules_digest"):
            for bad in bad_digests:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_policy_decision(**{field: bad})

    def test_blank_identity_strings_rejected(self):
        for field in ("decision_id", "policy_version"):
            for bad in ("", "   ", "\t"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_policy_decision(**{field: bad})

    def test_present_optional_strings_reject_blank_but_none_allowed(self):
        decision = _valid_policy_decision()
        self.assertIsNone(decision.required_human_role)
        self.assertIsNone(decision.waiver_ref)
        for bad in ("", "   ", "\t"):
            with self.subTest(field="required_human_role", bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(required_human_role=bad)
            with self.subTest(field="waiver_ref", bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(
                        outcome="PASS_WITH_WAIVER",
                        waiver_ref=bad,
                    )

    def test_invalid_outcome_rejected(self):
        for bad in ("pass", "PASSED", "APPROVED", "PENDING", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(outcome=bad)

    def test_invalid_reviewer_rejected(self):
        for bad in (["designer"], ["INTENT"], [""], ["intent", "designer"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(required_reviewers=bad)

    def test_tuple_families_blank_item_rejected(self):
        families = {
            "reason_codes": ["code-1", ""],
            "required_collectors": ["collector-1", "   "],
            "required_reviewers": ["intent", "\t"],
            "evaluated_evidence_refs": ["evidence-1", ""],
            "evaluated_finding_refs": ["finding-1", "   "],
            "evaluated_receipt_refs": ["receipt-1", "\t"],
        }
        for field, value in families.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(**{field: value})

    def test_tuple_families_duplicate_rejected(self):
        families = {
            "reason_codes": ["code-1", "code-1"],
            "required_collectors": ["collector-1", "collector-1"],
            "required_reviewers": ["intent", "intent"],
            "evaluated_evidence_refs": ["evidence-1", "evidence-1"],
            "evaluated_finding_refs": ["finding-1", "finding-1"],
            "evaluated_receipt_refs": ["receipt-1", "receipt-1"],
        }
        for field, value in families.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(**{field: value})

    def test_tuple_families_order_tuple_json_and_input_copy_safety(self):
        families = {
            "reason_codes": (
                ["code-2", "code-1"],
                ("code-2", "code-1"),
            ),
            "required_collectors": (
                ["collector-2", "collector-1"],
                ("collector-2", "collector-1"),
            ),
            "required_reviewers": (
                ["operability", "architecture", "intent"],
                ("operability", "architecture", "intent"),
            ),
            "evaluated_evidence_refs": (
                ["evidence-2", "evidence-1"],
                ("evidence-2", "evidence-1"),
            ),
            "evaluated_finding_refs": (
                ["finding-2", "finding-1"],
                ("finding-2", "finding-1"),
            ),
            "evaluated_receipt_refs": (
                ["receipt-2", "receipt-1"],
                ("receipt-2", "receipt-1"),
            ),
        }
        for field, (source, expected) in families.items():
            with self.subTest(field=field):
                decision = _valid_policy_decision(**{field: source})
                stored = getattr(decision, field)
                self.assertEqual(stored, expected)
                self.assertIsInstance(stored, tuple)
                self.assertEqual(decision.model_dump()[field], expected)
                json_value = decision.model_dump(mode="json")[field]
                self.assertEqual(json_value, list(expected))
                self.assertIsInstance(json_value, list)
                source.append("extra")
                source[0] = "mutated"
                self.assertEqual(stored, expected)
                with self.assertRaises(TypeError):
                    stored[0] = "mutated"
                with self.assertRaises(AttributeError):
                    stored.append("extra")

    def test_tuple_families_default_empty(self):
        decision = _valid_policy_decision()
        for field in (
            "reason_codes",
            "required_collectors",
            "required_reviewers",
            "evaluated_evidence_refs",
            "evaluated_finding_refs",
            "evaluated_receipt_refs",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(decision, field), ())

    def test_naive_evaluated_at_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_policy_decision(evaluated_at=datetime(2026, 8, 25, 2, 30))
        aware = _valid_policy_decision(evaluated_at="2026-08-25T02:30:00+00:00")
        self.assertIsNotNone(aware.evaluated_at.tzinfo)

    def test_stale_blocked_needs_human_require_reason_code(self):
        for outcome in ("STALE", "BLOCKED", "NEEDS_HUMAN"):
            with self.subTest(outcome=outcome, reason_codes=()):
                values = {"outcome": outcome}
                if outcome == "NEEDS_HUMAN":
                    values["required_human_role"] = "security-officer"
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(**values)
            with self.subTest(outcome=outcome, reason_codes=("policy-rule-1",)):
                values = {"outcome": outcome, "reason_codes": ["policy-rule-1"]}
                if outcome == "NEEDS_HUMAN":
                    values["required_human_role"] = "security-officer"
                self.assertEqual(
                    _valid_policy_decision(**values).outcome,
                    outcome,
                )

    def test_pass_with_waiver_requires_waiver_ref(self):
        for waiver in (None, ""):
            with self.subTest(waiver=waiver):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(
                        outcome="PASS_WITH_WAIVER",
                        waiver_ref=waiver,
                    )
        decision = _valid_policy_decision(
            outcome="PASS_WITH_WAIVER",
            waiver_ref="waiver-001",
        )
        self.assertEqual(decision.waiver_ref, "waiver-001")

    def test_other_outcomes_forbid_waiver_ref(self):
        for outcome in ("STALE", "BLOCKED", "NEEDS_HUMAN", "PASS"):
            values = {"outcome": outcome, "waiver_ref": "waiver-001"}
            if outcome == "STALE":
                values["reason_codes"] = ["stale"]
            if outcome == "BLOCKED":
                values["reason_codes"] = ["blocked"]
            if outcome == "NEEDS_HUMAN":
                values["reason_codes"] = ["human"]
                values["required_human_role"] = "security-officer"
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValidationError):
                    _valid_policy_decision(**values)
            values["waiver_ref"] = None
            self.assertEqual(_valid_policy_decision(**values).waiver_ref, None)

    def test_needs_human_requires_required_human_role(self):
        with self.assertRaises(ValidationError):
            _valid_policy_decision(
                outcome="NEEDS_HUMAN",
                reason_codes=["human"],
            )
        decision = _valid_policy_decision(
            outcome="NEEDS_HUMAN",
            reason_codes=["human"],
            required_human_role="security-officer",
        )
        self.assertEqual(decision.required_human_role, "security-officer")

    def test_unknown_field_rejected(self):
        values = _valid_policy_decision().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            PolicyDecision.model_validate(values)

    def test_assignment_mutation_rejected(self):
        decision = _valid_policy_decision()
        with self.assertRaises(ValidationError):
            decision.outcome = "BLOCKED"
        with self.assertRaises(ValidationError):
            decision.reason_codes = ("blocked",)


class TestHumanDecisionContract(unittest.TestCase):
    def test_valid_construction_and_lossless_round_trip_and_package_export(self):
        from assurance import HumanDecision as PackageHumanDecision

        decision = _valid_human_decision(
            conditions=["cond-1", "cond-2"],
        )
        self.assertIs(PackageHumanDecision, HumanDecision)
        dumped = decision.model_dump(mode="json")
        restored = HumanDecision.model_validate(dumped)
        self.assertEqual(restored, decision)
        self.assertEqual(restored.model_dump(mode="json"), dumped)
        self.assertIsNotNone(decision.decided_at.tzinfo)
        self.assertIsNone(decision.waiver_id)
        self.assertIsNone(decision.expires_at)
        self.assertIsInstance(decision.conditions, tuple)
        self.assertIsInstance(decision.model_dump()["conditions"], tuple)
        self.assertIsInstance(dumped["conditions"], list)

    def test_repeated_json_serialization_is_stable_and_exact_field_order(self):
        decision = _valid_human_decision(
            decision="approve_with_waiver",
            waiver_id="waiver-001",
            expires_at="2026-08-25T03:30:00+08:00",
            conditions=["cond-2", "cond-1"],
        )
        self.assertEqual(decision.model_dump_json(), decision.model_dump_json())
        self.assertEqual(
            decision.model_dump(mode="json"),
            decision.model_dump(mode="json"),
        )
        self.assertEqual(
            list(decision.model_dump().keys()),
            [
                "schema_version",
                "decision_id",
                "subject_digest",
                "actor_type",
                "owner",
                "owner_role",
                "decision",
                "reason",
                "conditions",
                "waiver_id",
                "expires_at",
                "decided_at",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        decision = _valid_human_decision()
        self.assertEqual(decision.schema_version, "v1")
        values = decision.model_dump()
        values.pop("schema_version")
        self.assertEqual(HumanDecision(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_human_decision(schema_version="v2")

    def test_subject_digest_validated(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for bad in bad_digests:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(subject_digest=bad)

    def test_actor_type_defaults_to_human_and_rejects_machine_agent(self):
        values = _valid_human_decision().model_dump()
        values.pop("actor_type")
        decision = HumanDecision(**values)
        self.assertEqual(decision.actor_type, "human")
        self.assertEqual(_valid_human_decision(actor_type="human").actor_type, "human")
        for bad in ("machine", "agent", "assistant", "HUMAN", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(actor_type=bad)

    def test_decision_enum_rejection(self):
        for bad in ("APPROVE", "deny", "reject_with_waiver", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(decision=bad)

    def test_blank_identity_text_fields_rejected(self):
        for field in ("decision_id", "owner", "owner_role", "reason"):
            for bad in ("", "   ", "\t"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_human_decision(**{field: bad})

    def test_present_but_blank_waiver_id_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(
                        decision="approve_with_waiver",
                        waiver_id=bad,
                        expires_at="2026-08-25T03:30:00+08:00",
                    )

    def test_conditions_default_blank_and_duplicate_rejected(self):
        decision = _valid_human_decision()
        self.assertEqual(decision.conditions, ())
        for bad in (
            ["cond-1", ""],
            ["cond-1", "   "],
            ["cond-1", "\t"],
            ["cond-1", "cond-1"],
            ("cond-1", "cond-1"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(conditions=bad)

    def test_conditions_order_tuple_json_and_input_copy_safety(self):
        source = ["cond-2", "cond-1", "cond-3"]
        decision = _valid_human_decision(conditions=source)
        expected = ("cond-2", "cond-1", "cond-3")
        self.assertEqual(decision.conditions, expected)
        self.assertIsInstance(decision.conditions, tuple)
        self.assertEqual(decision.model_dump()["conditions"], expected)
        json_value = decision.model_dump(mode="json")["conditions"]
        self.assertEqual(json_value, list(expected))
        self.assertIsInstance(json_value, list)
        source.append("cond-4")
        source[0] = "mutated"
        self.assertEqual(decision.conditions, expected)
        with self.assertRaises(TypeError):
            decision.conditions[0] = "mutated"
        with self.assertRaises(AttributeError):
            decision.conditions.append("cond-4")

    def test_naive_decided_at_and_expires_at_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_human_decision(decided_at=datetime(2026, 8, 25, 2, 30))
        with self.assertRaises(ValidationError):
            _valid_human_decision(
                decision="approve_with_waiver",
                waiver_id="waiver-001",
                expires_at=datetime(2026, 8, 25, 3, 30),
            )
        aware = _valid_human_decision(
            decision="approve_with_waiver",
            waiver_id="waiver-001",
            expires_at="2026-08-25T03:30:00+00:00",
        )
        self.assertIsNotNone(aware.decided_at.tzinfo)
        self.assertIsNotNone(aware.expires_at.tzinfo)

    def test_waiver_cross_field_invalid_and_valid_combinations(self):
        base = {
            "decision": "approve_with_waiver",
            "waiver_id": "waiver-001",
            "expires_at": "2026-08-25T03:30:00+08:00",
        }
        decision = _valid_human_decision(**base)
        self.assertEqual(decision.decision, "approve_with_waiver")
        self.assertEqual(decision.waiver_id, "waiver-001")
        self.assertIsNotNone(decision.expires_at)

        for missing in ("waiver_id", "expires_at"):
            values = dict(base)
            values[missing] = None
            with self.subTest(missing=missing):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(**values)

        for decision_value in ("approve", "reject"):
            for forbidden in ("waiver_id", "expires_at"):
                values = {
                    "decision": decision_value,
                    forbidden: (
                        "waiver-001"
                        if forbidden == "waiver_id"
                        else "2026-08-25T03:30:00+08:00"
                    ),
                }
                with self.subTest(decision=decision_value, forbidden=forbidden):
                    with self.assertRaises(ValidationError):
                        _valid_human_decision(**values)

        for expires_at in (
            "2026-08-25T02:30:00+08:00",
            "2026-08-25T01:30:00+08:00",
        ):
            with self.subTest(expires_at=expires_at):
                with self.assertRaises(ValidationError):
                    _valid_human_decision(**{**base, "expires_at": expires_at})

        self.assertEqual(
            _valid_human_decision(decision="approve").decision,
            "approve",
        )
        self.assertEqual(
            _valid_human_decision(decision="reject").decision,
            "reject",
        )

    def test_unknown_field_rejected(self):
        values = _valid_human_decision().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            HumanDecision.model_validate(values)

    def test_assignment_mutation_rejected(self):
        decision = _valid_human_decision()
        with self.assertRaises(ValidationError):
            decision.reason = "mutated"
        with self.assertRaises(ValidationError):
            decision.conditions = ("cond-9",)
        with self.assertRaises(ValidationError):
            decision.waiver_id = "waiver-009"


class TestAcceptanceCaseContract(unittest.TestCase):
    def test_package_export_valid_construction_stable_json_and_round_trip(self):
        from assurance import AcceptanceCase as PackageAcceptanceCase

        self.assertIs(PackageAcceptanceCase, AcceptanceCase)
        case = _valid_acceptance_case()
        self.assertEqual(case.schema_version, "v1")
        self.assertEqual(case.model_dump_json(), case.model_dump_json())
        self.assertEqual(
            case.model_dump(mode="json"), case.model_dump(mode="json")
        )
        dumped = case.model_dump(mode="json")
        restored = AcceptanceCase.model_validate(dumped)
        self.assertEqual(restored, case)
        self.assertEqual(restored.model_dump(mode="json"), dumped)

    def test_exact_field_order_and_schema_version_default_rejection(self):
        case = _valid_acceptance_case()
        self.assertEqual(
            list(case.model_dump().keys()),
            [
                "schema_version",
                "case_id",
                "subject_digest",
                "state",
                "evidence_refs",
                "finding_refs",
                "execution_receipt_refs",
                "policy_decision_refs",
                "human_decision_refs",
                "conditions",
                "conflicts",
                "missing_evidence",
                "invalidation_reason",
                "created_at",
                "updated_at",
            ],
        )
        values = case.model_dump()
        values.pop("schema_version")
        self.assertEqual(AcceptanceCase(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(schema_version="v2")

    def test_all_state_literals_construct_with_required_facts(self):
        for state, facts in _ACCEPTANCE_STATE_FACTS.items():
            with self.subTest(state=state):
                case = _valid_acceptance_case(state=state, **facts)
                self.assertEqual(case.state, state)

    def test_unknown_and_case_mismatched_state_values_fail_closed(self):
        for bad in ("draft", "Draft", "DRAFTED", "ACCEPTED ", "PENDING", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(state=bad)

    def test_invalid_subject_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for bad in bad_digests:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(subject_digest=bad)

    def test_blank_case_id_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(case_id=bad)

    def test_present_but_blank_invalidation_reason_rejected(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(
                        state="INVALIDATED",
                        invalidation_reason=bad,
                    )

    def test_naive_datetimes_rejected_and_aware_accepted(self):
        for field in ("created_at", "updated_at"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(
                        **{field: datetime(2026, 8, 25, 2, 30)}
                    )
        case = _valid_acceptance_case(
            created_at="2026-08-25T02:30:00+00:00",
            updated_at="2026-08-25T02:31:00+00:00",
        )
        self.assertIsNotNone(case.created_at.tzinfo)
        self.assertIsNotNone(case.updated_at.tzinfo)

    def test_updated_before_created_rejected_and_equality_accepted(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(
                created_at="2026-08-25T02:31:00+08:00",
                updated_at="2026-08-25T02:30:00+08:00",
            )
        equal = _valid_acceptance_case(
            created_at="2026-08-25T02:30:00+08:00",
            updated_at="2026-08-25T02:30:00+08:00",
        )
        self.assertEqual(equal.updated_at, equal.created_at)

    def test_tuple_families_default_empty(self):
        case = _valid_acceptance_case()
        for field in (
            "evidence_refs",
            "finding_refs",
            "execution_receipt_refs",
            "policy_decision_refs",
            "human_decision_refs",
            "conditions",
            "conflicts",
            "missing_evidence",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(case, field), ())

    def test_tuple_families_blank_item_rejected(self):
        families = {
            "evidence_refs": ["evidence-001", ""],
            "finding_refs": ["finding-001", "   "],
            "execution_receipt_refs": ["receipt-001", "\t"],
            "policy_decision_refs": ["policy-001", ""],
            "human_decision_refs": ["human-001", "   "],
            "conditions": ["condition-001", "\t"],
            "conflicts": ["conflict-001", ""],
            "missing_evidence": ["missing-001", "   "],
        }
        for field, value in families.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(**{field: value})

    def test_tuple_families_duplicate_rejected(self):
        families = {
            "evidence_refs": ["evidence-001", "evidence-001"],
            "finding_refs": ["finding-001", "finding-001"],
            "execution_receipt_refs": ["receipt-001", "receipt-001"],
            "policy_decision_refs": ["policy-001", "policy-001"],
            "human_decision_refs": ["human-001", "human-001"],
            "conditions": ["condition-001", "condition-001"],
            "conflicts": ["conflict-001", "conflict-001"],
            "missing_evidence": ["missing-001", "missing-001"],
        }
        for field, value in families.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(**{field: value})

    def test_tuple_families_order_tuple_json_and_input_copy_safety(self):
        families = {
            "evidence_refs": ["evidence-002", "evidence-001"],
            "finding_refs": ["finding-002", "finding-001"],
            "execution_receipt_refs": ["receipt-002", "receipt-001"],
            "policy_decision_refs": ["policy-002", "policy-001"],
            "human_decision_refs": ["human-002", "human-001"],
            "conditions": ["condition-002", "condition-001"],
            "conflicts": ["conflict-002", "conflict-001"],
            "missing_evidence": ["missing-002", "missing-001"],
        }
        for field, source in families.items():
            with self.subTest(field=field):
                case = _valid_acceptance_case(**{field: source})
                stored = getattr(case, field)
                expected = tuple(source)
                self.assertEqual(stored, expected)
                self.assertIsInstance(stored, tuple)
                self.assertEqual(case.model_dump()[field], expected)
                json_value = case.model_dump(mode="json")[field]
                self.assertEqual(json_value, list(expected))
                self.assertIsInstance(json_value, list)
                source.append("extra")
                source[0] = "mutated"
                self.assertEqual(stored, expected)
                with self.assertRaises(TypeError):
                    stored[0] = "mutated"
                with self.assertRaises(AttributeError):
                    stored.append("extra")

    def test_evidence_collected_requires_evidence_ref(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(state="EVIDENCE_COLLECTED")
        case = _valid_acceptance_case(
            state="EVIDENCE_COLLECTED",
            evidence_refs=["evidence-001"],
        )
        self.assertEqual(case.state, "EVIDENCE_COLLECTED")

    def test_needs_evidence_requires_missing_evidence(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(state="NEEDS_EVIDENCE")
        case = _valid_acceptance_case(
            state="NEEDS_EVIDENCE",
            missing_evidence=["missing-001"],
        )
        self.assertEqual(case.state, "NEEDS_EVIDENCE")

    def test_conflicted_requires_conflict(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(state="CONFLICTED")
        case = _valid_acceptance_case(
            state="CONFLICTED",
            conflicts=["conflict-001"],
        )
        self.assertEqual(case.state, "CONFLICTED")

    def test_conditional_accepted_requires_conditions_policy_and_human(self):
        valid_facts = {
            "conditions": ["condition-001"],
            "policy_decision_refs": ["policy-001"],
            "human_decision_refs": ["human-001"],
        }
        for missing in valid_facts:
            values = dict(valid_facts)
            values.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(
                        state="CONDITIONAL_ACCEPTED", **values
                    )
        case = _valid_acceptance_case(
            state="CONDITIONAL_ACCEPTED", **valid_facts
        )
        self.assertEqual(case.state, "CONDITIONAL_ACCEPTED")

    def test_accepted_requires_evidence_policy_and_human(self):
        valid_facts = {
            "evidence_refs": ["evidence-001"],
            "policy_decision_refs": ["policy-001"],
            "human_decision_refs": ["human-001"],
        }
        for missing in valid_facts:
            values = dict(valid_facts)
            values.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(state="ACCEPTED", **values)
        case = _valid_acceptance_case(state="ACCEPTED", **valid_facts)
        self.assertEqual(case.state, "ACCEPTED")

    def test_rejected_requires_policy_or_human_decision(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(state="REJECTED")
        policy_only = _valid_acceptance_case(
            state="REJECTED",
            policy_decision_refs=["policy-001"],
        )
        self.assertEqual(policy_only.policy_decision_refs, ("policy-001",))
        self.assertEqual(policy_only.human_decision_refs, ())
        human_only = _valid_acceptance_case(
            state="REJECTED",
            human_decision_refs=["human-001"],
        )
        self.assertEqual(human_only.human_decision_refs, ("human-001",))
        self.assertEqual(human_only.policy_decision_refs, ())
        both = _valid_acceptance_case(
            state="REJECTED",
            policy_decision_refs=["policy-001"],
            human_decision_refs=["human-001"],
        )
        self.assertEqual(both.state, "REJECTED")

    def test_invalidated_requires_and_accepts_invalidation_reason(self):
        with self.assertRaises(ValidationError):
            _valid_acceptance_case(state="INVALIDATED")
        case = _valid_acceptance_case(
            state="INVALIDATED",
            invalidation_reason="duplicate case",
        )
        self.assertEqual(case.invalidation_reason, "duplicate case")

    def test_non_invalidated_states_reject_invalidation_reason(self):
        for state in (
            "DRAFT",
            "EVIDENCE_COLLECTED",
            "NEEDS_EVIDENCE",
            "CONFLICTED",
            "CONDITIONAL_ACCEPTED",
            "ACCEPTED",
            "REJECTED",
        ):
            facts = dict(_ACCEPTANCE_STATE_FACTS[state])
            facts["invalidation_reason"] = "must not be set"
            with self.subTest(state=state):
                with self.assertRaises(ValidationError):
                    _valid_acceptance_case(state=state, **facts)

    def test_unknown_field_rejected(self):
        values = _valid_acceptance_case().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            AcceptanceCase.model_validate(values)

    def test_assignment_mutation_rejected(self):
        case = _valid_acceptance_case()
        with self.assertRaises(ValidationError):
            case.state = "ACCEPTED"
        with self.assertRaises(ValidationError):
            case.evidence_refs = ("evidence-001",)


class TestPackageDiscoveryContract(unittest.TestCase):
    def test_setuptools_packages_find_include_preserves_prior_entries_and_appends_assurance(self):
        repo_root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads(
            (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        include = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertEqual(
            include,
            [
                "memory*",
                "execution*",
                "feedback*",
                "orchestration*",
                "rag*",
                "web*",
                "assurance*",
            ],
        )
        self.assertEqual(include[-1], "assurance*")
        self.assertEqual(
            include[:-1],
            ["memory*", "execution*", "feedback*", "orchestration*", "rag*", "web*"],
        )


if __name__ == "__main__":
    unittest.main()
