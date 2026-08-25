import copy
import inspect
import math
import unittest

from assurance.evals.dataset import load_public_cases, reviewer_payload
from assurance.evals import rules_adapter as rules_adapter_module
from assurance.evals.rules_adapter import RulesOnlyAdapter


CASE_IDS = tuple(f"ca_v0_{index:03d}" for index in range(1, 13))


def _payload(index=0):
    return reviewer_payload(load_public_cases()[index])


def _neutral_payload(task_spec="Return the status value."):
    return {
        "case_id": "custom_case",
        "category": "intent",
        "title": "Neutral public change",
        "task_spec": task_spec,
        "source_files": [["fixtures/custom.py", "def apply(value):\n    return value\n"]],
        "public_test_files": [
            [
                "fixtures/test_custom.py",
                "def run_public_test(ns):\n    assert ns['apply']('ok') == 'ok'\n",
            ]
        ],
        "change_material": [["fixtures/custom.py", "return value"]],
        "evidence_refs": [
            "ev_custom_change",
            "ev_custom_test",
            "ev_custom_task",
        ],
    }


class RulesOnlyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RulesOnlyAdapter()

    def test_all_public_cases_run_with_fixed_rules_facts(self):
        results = [self.adapter.run(_payload(index)) for index in range(12)]

        self.assertEqual(tuple(result.case_id for result in results), CASE_IDS)
        self.assertTrue(all(result.status == "success" for result in results))
        self.assertTrue(any(result.findings or result.questions for result in results))
        self.assertTrue(
            len({finding.predicted_issue_ids[0] for result in results for finding in result.findings})
            < len(results)
        )
        for result in results:
            self.assertEqual(result.arm, "rules_only")
            self.assertEqual(result.executed_roles, ("rules",))
            self.assertEqual(result.model_refs, ("rules:public-v0",))
            self.assertEqual(result.input_tokens, 0)
            self.assertEqual(result.output_tokens, 0)
            self.assertEqual(result.cost_usd, 0)
            self.assertIsNone(result.latency_seconds)

    def test_representative_observable_rules_are_generic(self):
        results = {
            result.case_id: result
            for index, result in enumerate(
                (self.adapter.run(_payload(index)) for index in range(12))
            )
        }

        def codes(case_id):
            return {issue_id for finding in results[case_id].findings for issue_id in finding.predicted_issue_ids}

        self.assertIn("rule:data_scope", codes("ca_v0_001"))
        self.assertIn("rule:migration_reversibility", codes("ca_v0_006"))
        self.assertIn("rule:operator_control", codes("ca_v0_008"))
        self.assertIn("rule:bounded_attempts", codes("ca_v0_009"))
        self.assertEqual(results["ca_v0_012"].predicted_outcome, "STALE")
        self.assertIn("rule:independent_digest_comparison", codes("ca_v0_012"))
        self.assertTrue(
            all(
                finding.reviewer_role == "rules"
                and set(finding.evidence_refs) <= set(_payload(int(result.case_id[-3:]) - 1)["evidence_refs"])
                for result in results.values()
                for finding in result.findings
            )
        )

    def test_rules_source_has_no_hidden_loader_labels_or_case_lookup(self):
        source = inspect.getsource(rules_adapter_module)
        for forbidden in (
            "load_hidden_gold",
            "intent_scope_creep",
            "freshness_old_approval_survives_new_digest",
            "issue:ca_v0_",
            "rubric:ca_v0_",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("ca_v0_001", source)

    def test_invalid_public_payloads_fail_closed_without_exception_text(self):
        valid = _payload()
        invalid = []

        unknown = copy.deepcopy(valid)
        unknown["unexpected"] = "no"
        invalid.append(unknown)

        duplicate_refs = copy.deepcopy(valid)
        duplicate_refs["evidence_refs"].append(duplicate_refs["evidence_refs"][0])
        invalid.append(duplicate_refs)

        tuple_files = copy.deepcopy(valid)
        tuple_files["source_files"] = tuple(tuple(item) for item in tuple_files["source_files"])
        invalid.append(tuple_files)

        nul_text = copy.deepcopy(valid)
        nul_text["task_spec"] = "bad\x00task"
        invalid.append(nul_text)

        nonfinite = copy.deepcopy(valid)
        nonfinite["change_material"][0][1] = math.inf
        invalid.append(nonfinite)

        for payload in invalid:
            result = self.adapter.run(payload)
            self.assertEqual(result.status, "schema_invalid")
            self.assertEqual(result.error_code, "invalid_public_payload")
            self.assertNotIn("bad", repr(result))

    def test_deterministic_receipt_and_payload_mutation_isolation(self):
        payload = _payload(8)
        original = copy.deepcopy(payload)

        first = self.adapter.run(payload)
        second = self.adapter.run(copy.deepcopy(payload))

        self.assertEqual(payload, original)
        self.assertEqual(first, second)
        self.assertRegex(first.receipt_ref, r"^sha256:[0-9a-f]{64}$")

    def test_questions_only_needs_human_and_no_signal_passes(self):
        question_payload = _payload(10)
        question_result = self.adapter.run(question_payload)
        self.assertEqual(question_result.predicted_outcome, "NEEDS_HUMAN")
        self.assertFalse(question_result.findings)
        self.assertTrue(question_result.questions)

        pass_result = self.adapter.run(_neutral_payload())
        self.assertEqual(pass_result.predicted_outcome, "PASS")
        self.assertFalse(pass_result.findings)
        self.assertFalse(pass_result.questions)


if __name__ == "__main__":
    unittest.main()
