import json
import unittest
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from assurance.evals.dataset import (
    DATASET_ID,
    HiddenGold,
    PublicEvalCase,
    load_hidden_gold,
    load_public_cases,
    reviewer_payload,
    validate_dataset,
)


EXPECTED_CATEGORIES = (
    "intent",
    "intent",
    "architecture",
    "architecture",
    "architecture",
    "operability",
    "operability",
    "operability",
    "cost",
    "ownership",
    "boundary",
    "freshness",
)

EXPECTED_CASE_IDS = tuple(f"ca_v0_{index:03d}" for index in range(1, 13))

HIDDEN_ISSUE_LABELS = (
    "intent_scope_creep",
    "intent_missing_acceptance_nfr",
    "architecture_dependency_reversal",
    "architecture_duplicate_rule_second_source",
    "architecture_public_contract_without_adr",
    "operability_migration_without_rollback",
    "operability_retry_duplicate_side_effect",
    "operability_missing_telemetry_kill_switch",
    "cost_unbounded_retries_fallback",
    "ownership_missing_owner_runbook",
    "boundary_provider_data_residency",
    "freshness_old_approval_survives_new_digest",
)


class AssuranceEvalDatasetTests(unittest.TestCase):
    def test_fixed_dataset_shape_and_order(self):
        cases = load_public_cases()
        self.assertEqual(DATASET_ID, "change_assurance_v0")
        self.assertIsInstance(cases, tuple)
        self.assertEqual(len(cases), 12)
        self.assertEqual(tuple(case.case_id for case in cases), EXPECTED_CASE_IDS)
        self.assertEqual(tuple(case.category for case in cases), EXPECTED_CATEGORIES)
        self.assertEqual(len({case.case_id for case in cases}), 12)
        self.assertTrue(all(case.case_id for case in cases))

    def test_public_material_compiles_and_executes(self):
        cases = load_public_cases()
        for case in cases:
            namespace = {"__builtins__": {}}
            for path, content in case.source_files:
                exec(compile(content, path, "exec"), namespace)
            for path, content in case.public_test_files:
                test_namespace = dict(namespace)
                exec(compile(content, path, "exec"), test_namespace)
                test_namespace["run_public_test"](test_namespace)

    def test_each_case_has_unique_material_and_observable_semantics(self):
        cases = load_public_cases()
        self.assertEqual(len({case.source_files[0][1] for case in cases}), 12)
        self.assertEqual(len({case.change_material for case in cases}), 12)
        by_id = {case.case_id: case for case in cases}

        scope = by_id["ca_v0_001"]
        scope_source = scope.source_files[0][1]
        self.assertIn("customer_email", scope_source)
        self.assertIn("status", scope_source)
        self.assertNotIn("customer_email", scope.public_test_files[0][1])

        nfr = by_id["ca_v0_002"]
        self.assertIn("latency", nfr.task_spec.lower())
        self.assertIn("failure", nfr.task_spec.lower())

        dependency = by_id["ca_v0_003"]
        self.assertIn("delivery_adapter", dependency.source_files[0][1])

        duplicate = by_id["ca_v0_004"]
        self.assertIn("RETRY_LIMIT", duplicate.source_files[0][1])

        contract = by_id["ca_v0_005"]
        self.assertIn("version", contract.source_files[0][1])
        self.assertIn("ev_ca_v0_005_architecture_decision_lookup", contract.evidence_refs)

        migration = by_id["ca_v0_006"]
        self.assertIn("migrate", migration.source_files[0][1])
        self.assertNotIn("rollback", migration.source_files[0][1].lower())

    def test_hidden_outcomes_capture_case_semantics(self):
        gold = load_hidden_gold()
        needs_human = {
            "ca_v0_002",
            "ca_v0_005",
            "ca_v0_010",
        }
        for case_id, item in gold.items():
            if case_id in needs_human:
                self.assertEqual(item.expected_outcome, "NEEDS_HUMAN")
            elif case_id == "ca_v0_012":
                self.assertEqual(item.expected_outcome, "STALE")
            else:
                self.assertEqual(item.expected_outcome, "BLOCKED")
            self.assertIn(item.expected_risk, {"HIGH", "CRITICAL"})

    def test_gold_is_separate_and_refs_are_complete(self):
        cases = load_public_cases()
        gold = load_hidden_gold()
        self.assertIsInstance(gold, MappingProxyType)
        self.assertEqual(set(gold), {case.case_id for case in cases})
        for case in cases:
            item = gold[case.case_id]
            self.assertIsInstance(item, HiddenGold)
            self.assertTrue(item.rubric_ids)
            self.assertTrue(item.expected_outcome)
            self.assertTrue(item.expected_risk)
            self.assertTrue(set(item.evidence_refs) <= set(case.evidence_refs))

    def test_reviewer_payload_is_public_only_and_jsonable(self):
        for case in load_public_cases():
            payload = reviewer_payload(case)
            encoded = json.dumps(payload, sort_keys=True)
            self.assertEqual(
                set(payload),
                {
                    "case_id",
                    "category",
                    "title",
                    "task_spec",
                    "source_files",
                    "public_test_files",
                    "change_material",
                    "evidence_refs",
                },
            )
            for forbidden in ("gold", "expected", "rubric", "answer"):
                self.assertNotIn(forbidden, encoded.lower())
            self.assertNotIn("HIGH", encoded)
            self.assertNotIn("BLOCK", encoded)
            self.assertNotIn("adr_missing", encoded)
            self.assertNotIn("runbook_missing", encoded)
            for issue_label in HIDDEN_ISSUE_LABELS:
                self.assertNotIn(issue_label, encoded)

    def test_loader_is_deterministic_and_models_are_frozen(self):
        self.assertEqual(load_public_cases(), load_public_cases())
        self.assertEqual(load_hidden_gold(), load_hidden_gold())
        case = load_public_cases()[0]
        with self.assertRaises(FrozenInstanceError):
            case.title = "mutated"
        with self.assertRaises(TypeError):
            case.source_files[0] = ("x.py", "pass")
        with self.assertRaises(TypeError):
            load_hidden_gold()[case.case_id] = HiddenGold(
                case_id=case.case_id,
                rubric_ids=("R0",),
                expected_outcome="BLOCK",
                expected_risk="HIGH",
                evidence_refs=case.evidence_refs[:1],
            )

    def test_validator_fails_closed_for_duplicate_id_and_dangling_ref(self):
        cases = load_public_cases()
        gold = load_hidden_gold()
        with self.assertRaises(ValueError):
            validate_dataset((cases[0], replace(cases[1], case_id=cases[0].case_id), *cases[2:]), gold)
        bad_gold = dict(gold)
        bad_gold[cases[0].case_id] = replace(
            gold[cases[0].case_id], evidence_refs=("missing-ref",)
        )
        with self.assertRaises(ValueError):
            validate_dataset(cases, bad_gold)

    def test_validator_fails_closed_for_unrunnable_public_material(self):
        cases = load_public_cases()
        gold = load_hidden_gold()
        broken_source = replace(
            cases[0], source_files=(("broken.py", "def broken(:\n    pass"),)
        )
        with self.assertRaises(ValueError):
            validate_dataset((broken_source, *cases[1:]), gold)
        broken_test = replace(
            cases[0], public_test_files=(("broken_test.py", "def run_public_test(ns):\n    raise AssertionError"),)
        )
        with self.assertRaises(ValueError):
            validate_dataset((broken_test, *cases[1:]), gold)


if __name__ == "__main__":
    unittest.main()
