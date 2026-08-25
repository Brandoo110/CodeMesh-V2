import inspect
import json
import math
import unittest
from dataclasses import FrozenInstanceError

from assurance.evals.dataset import load_public_cases, reviewer_payload
from assurance.evals import runner as runner_module
from assurance.evals.runner import (
    ARM_ORDER,
    ArmRunResult,
    CaseComparison,
    ComparisonRunner,
    ComparisonRun,
    EvalFinding,
)


CASE_IDS = tuple(f"ca_v0_{index:03d}" for index in range(1, 13))
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


def _success(payload, arm):
    roles = {
        "rules_only": ("rules",),
        "single_strong_reviewer": ("general",),
        "specialized_council": ("intent", "architecture", "operability"),
    }[arm]
    return ArmRunResult(
        case_id=payload["case_id"],
        arm=arm,
        status="success",
        predicted_outcome="PASS",
        executed_roles=roles,
        model_refs=tuple(f"model-{role}" for role in roles),
    )


class AssuranceEvalRunnerTests(unittest.TestCase):
    def test_three_arms_keep_order_and_canonical_payload_bytes(self):
        payload_bytes = {}

        def make_executor(arm):
            def execute(payload):
                payload_bytes.setdefault(payload["case_id"], []).append(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                )
                return _success(payload, arm)

            return execute

        runner = ComparisonRunner({arm: make_executor(arm) for arm in ARM_ORDER})
        result = runner.run()

        self.assertIsInstance(result, ComparisonRun)
        self.assertEqual(result.dataset_id, "change_assurance_v0")
        self.assertEqual(tuple(case.case_id for case in result.cases), CASE_IDS)
        self.assertEqual(len(result.cases), 12)
        for case in result.cases:
            self.assertEqual(tuple(item.arm for item in case.arms), ARM_ORDER)
            self.assertEqual(len(payload_bytes[case.case_id]), 3)
            self.assertEqual(len(set(payload_bytes[case.case_id])), 1)
            self.assertTrue(case.public_payload_digest.startswith("sha256:"))

    def test_json_round_trip_prevents_executor_mutation_leak(self):
        cases = load_public_cases()
        baseline = {
            case.case_id: reviewer_payload(case)["source_files"]
            for case in cases
        }
        observed = {}

        def mutating(payload):
            payload["source_files"][0][1] = "tampered"
            return _success(payload, "rules_only")

        def observing(payload):
            observed[payload["case_id"]] = payload["source_files"]
            return _success(payload, "single_strong_reviewer")

        runner = ComparisonRunner(
            {
                "rules_only": mutating,
                "single_strong_reviewer": observing,
                "specialized_council": lambda payload: _success(
                    payload, "specialized_council"
                ),
            }
        )
        runner.run(cases)

        self.assertEqual(observed, baseline)
        self.assertEqual(
            {
                case.case_id: reviewer_payload(case)["source_files"]
                for case in cases
            },
            baseline,
        )

    def test_executor_exception_isolated_and_text_not_recorded(self):
        def broken(_payload):
            raise RuntimeError("secret exception text")

        runner = ComparisonRunner(
            {
                "rules_only": broken,
                "single_strong_reviewer": lambda payload: _success(
                    payload, "single_strong_reviewer"
                ),
                "specialized_council": lambda payload: _success(
                    payload, "specialized_council"
                ),
            }
        )
        result = runner.run()

        self.assertTrue(
            all(case.arms[0].status == "failure" for case in result.cases)
        )
        self.assertTrue(
            all(case.arms[0].error_code == "executor_error" for case in result.cases)
        )
        self.assertTrue(
            all(
                arm.status == "success"
                for case in result.cases
                for arm in case.arms[1:]
            )
        )
        self.assertNotIn("secret exception text", repr(result))

    def test_bad_arm_results_and_evidence_fail_closed_per_arm(self):
        def wrong_case(payload):
            return ArmRunResult(
                case_id="ca_v0_999",
                arm="rules_only",
                status="success",
                executed_roles=("rules",),
                model_refs=("model-rules",),
            )

        def bad_evidence(payload):
            finding = EvalFinding(
                finding_id="f1",
                claim="claim",
                severity="high",
                blocking=True,
                evidence_refs=("not-public",),
                predicted_issue_ids=("issue_x",),
            )
            return ArmRunResult(
                case_id=payload["case_id"],
                arm="single_strong_reviewer",
                status="success",
                findings=(finding,),
                executed_roles=("general",),
                model_refs=("model-general",),
            )

        result = ComparisonRunner(
            {
                "rules_only": wrong_case,
                "single_strong_reviewer": bad_evidence,
                "specialized_council": lambda payload: object(),
            }
        ).run()

        for case in result.cases:
            self.assertEqual(
                tuple(arm.status for arm in case.arms),
                ("schema_invalid", "schema_invalid", "schema_invalid"),
            )
            self.assertEqual(
                tuple(arm.error_code for arm in case.arms),
                ("invalid_arm_result",) * 3,
            )

    def test_adversarially_mutated_result_is_revalidated_from_scratch(self):
        def evil_status(payload):
            result = _success(payload, "rules_only")
            object.__setattr__(result, "status", "evil")
            return result

        def evil_metrics(payload):
            result = _success(payload, "single_strong_reviewer")
            object.__setattr__(result, "cost_usd", -1.0)
            return result

        def evil_nested_finding(payload):
            finding = EvalFinding(
                finding_id="f1",
                claim="claim",
                severity="low",
                blocking=False,
                evidence_refs=(payload["evidence_refs"][0],),
                predicted_issue_ids=("issue_x",),
                reviewer_role="architecture",
            )
            object.__setattr__(finding, "severity", "evil")
            result = ArmRunResult(
                case_id=payload["case_id"],
                arm="specialized_council",
                status="success",
                findings=(finding,),
                executed_roles=("intent", "architecture", "operability"),
                model_refs=("model-intent", "model-architecture", "model-operability"),
            )
            object.__setattr__(result, "arm", "evil-arm")
            return result

        result = ComparisonRunner(
            {
                "rules_only": evil_status,
                "single_strong_reviewer": evil_metrics,
                "specialized_council": evil_nested_finding,
            }
        ).run()
        for case in result.cases:
            self.assertEqual(
                tuple(arm.status for arm in case.arms),
                ("schema_invalid",) * 3,
            )
            self.assertEqual(
                tuple(arm.error_code for arm in case.arms),
                ("invalid_arm_result",) * 3,
            )

    def test_object_mutated_public_case_is_rejected_before_execution(self):
        cases = load_public_cases()
        original_refs = cases[0].evidence_refs
        object.__setattr__(cases[0], "evidence_refs", ("forged-ref",))
        executors = {
            arm: (lambda payload, current_arm=arm: _success(payload, current_arm))
            for arm in ARM_ORDER
        }
        try:
            with self.assertRaises(ValueError):
                ComparisonRunner(executors).run(cases)
        finally:
            object.__setattr__(cases[0], "evidence_refs", original_refs)

    def test_models_are_frozen_and_validate_metrics_contract(self):
        finding = EvalFinding(
            finding_id="f1",
            claim="claim",
            severity="medium",
            blocking=False,
            evidence_refs=("ev1",),
            predicted_issue_ids=("issue1",),
            reviewer_role="rules",
        )
        with self.assertRaises(FrozenInstanceError):
            finding.claim = "changed"
        with self.assertRaises(ValueError):
            EvalFinding("f1", "claim", "urgent", False, (), ())
        with self.assertRaises(ValueError):
            EvalFinding("f1", "claim", "low", False, ("ev1", "ev1"), ())
        with self.assertRaises(ValueError):
            ArmRunResult("ca_v0_001", "rules_only", "success", error_code="bad")
        with self.assertRaises(ValueError):
            ArmRunResult("ca_v0_001", "rules_only", "failure")
        with self.assertRaises(ValueError):
            ArmRunResult(
                "ca_v0_001",
                "rules_only",
                "success",
                input_tokens=-1,
            )
        with self.assertRaises(ValueError):
            ArmRunResult(
                "ca_v0_001",
                "rules_only",
                "success",
                cost_usd=math.inf,
            )
        valid = ArmRunResult(
            "ca_v0_001",
            "rules_only",
            "success",
            findings=(finding,),
            input_tokens=1,
            output_tokens=2,
            cost_usd=0.1,
            latency_seconds=0.2,
            executed_roles=("rules",),
            model_refs=("model-rules",),
        )
        self.assertEqual(valid.findings, (finding,))

    def test_model_refs_may_repeat_when_aligned_with_executed_roles(self):
        role_sets = {
            "rules_only": ("rules",),
            "single_strong_reviewer": ("general",),
            "specialized_council": ("intent", "architecture", "operability"),
        }
        shared_model = "gpt-5.6-luna"

        for arm, roles in role_sets.items():
            result = ArmRunResult(
                "ca_v0_001",
                arm,
                "success",
                predicted_outcome="PASS",
                executed_roles=roles,
                model_refs=(shared_model,) * len(roles),
            )
            self.assertEqual(result.model_refs, (shared_model,) * len(roles))

    def test_comparison_models_enforce_digest_and_arm_order(self):
        digest = "sha256:" + ("a" * 64)
        arms = tuple(
            ArmRunResult(
                "ca_v0_001",
                arm,
                "success",
                executed_roles={
                    "rules_only": ("rules",),
                    "single_strong_reviewer": ("general",),
                    "specialized_council": ("intent", "architecture", "operability"),
                }[arm],
                model_refs={
                    "rules_only": ("model-rules",),
                    "single_strong_reviewer": ("model-general",),
                    "specialized_council": (
                        "model-intent",
                        "model-architecture",
                        "model-operability",
                    ),
                }[arm],
            )
            for arm in ARM_ORDER
        )
        comparison = CaseComparison("ca_v0_001", digest, arms)
        self.assertEqual(comparison.arms, arms)
        with self.assertRaises(ValueError):
            CaseComparison("ca_v0_001", "bad-digest", arms)
        with self.assertRaises(ValueError):
            CaseComparison("ca_v0_001", digest, tuple(reversed(arms)))

    def test_public_only_boundary_has_no_hidden_loader_or_gold_fields(self):
        source = inspect.getsource(runner_module)
        self.assertNotIn("load_hidden_gold", source)

        def inspect_payload(payload):
            encoded = json.dumps(payload, sort_keys=True)
            for key in payload:
                self.assertNotIn("gold", key.lower())
                self.assertNotIn("rubric", key.lower())
                self.assertNotIn("expected", key.lower())
            for label in HIDDEN_ISSUE_LABELS:
                self.assertNotIn(label, encoded)
            self.assertNotIn("HIGH", encoded)
            self.assertNotIn("BLOCKED", encoded)
            return _success(payload, "rules_only")

        result = ComparisonRunner(
            {
                "rules_only": inspect_payload,
                "single_strong_reviewer": lambda payload: _success(
                    payload, "single_strong_reviewer"
                ),
                "specialized_council": lambda payload: _success(
                    payload, "specialized_council"
                ),
            }
        ).run()
        self.assertEqual(len(result.cases), 12)

    def test_runner_is_deterministic_and_requires_exact_executor_keys(self):
        executors = {
            arm: (lambda payload, current_arm=arm: _success(payload, current_arm))
            for arm in ARM_ORDER
        }
        runner = ComparisonRunner(executors)
        self.assertEqual(runner.run(), runner.run())
        with self.assertRaises(ValueError):
            ComparisonRunner({ARM_ORDER[0]: executors[ARM_ORDER[0]]})
        with self.assertRaises(ValueError):
            ComparisonRunner({**executors, "extra": executors[ARM_ORDER[0]]})


if __name__ == "__main__":
    unittest.main()
