import copy
import json
import re
import unittest
from pathlib import Path

from assurance.evals.scorers import (
    ARM_ORDER,
    HIDDEN_TO_PUBLIC_TAXONOMY,
    RULE_TO_PUBLIC_TAXONOMY,
    build_score_report,
    normalize_predicted_issue_ids,
    verify_score_report,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "assurance" / "evals" / "results" / "change_assurance_v0_luna.json"
SCORE_PATH = ROOT / "assurance" / "evals" / "results" / "change_assurance_v0_luna_scores.json"
CASE_TOTAL = 12
EXPECTED = {
    "rules_only": {
        "outcomes": {"BLOCKED": 5, "NEEDS_HUMAN": 6, "STALE": 1},
        "findings": 6,
        "questions": 6,
        "precision": 1.0,
        "matched_cases": 6,
        "false_block": (1, 3),
        "missed_block": (4, 8),
        "unsupported": 0,
        "evidence_refs": (6, 6),
        "evidence_location": (5, 4),
        "stale_escape": 0,
    },
    "single_strong_reviewer": {
        "outcomes": {"BLOCKED": 7, "NEEDS_HUMAN": 4, "STALE": 1},
        "findings": 12,
        "questions": 5,
        "precision": 1.0,
        "matched_cases": 12,
        "false_block": (0, 3),
        "missed_block": (1, 8),
        "unsupported": 0,
        "evidence_refs": (39, 39),
        "evidence_location": (12, 12),
        "stale_escape": 0,
    },
    "specialized_council": {
        "outcomes": {"BLOCKED": 8, "NEEDS_HUMAN": 3, "STALE": 1},
        "findings": 12,
        "questions": 10,
        "precision": 1.0,
        "matched_cases": 12,
        "false_block": (1, 3),
        "missed_block": (1, 8),
        "unsupported": 0,
        "evidence_refs": (35, 35),
        "evidence_location": (12, 12),
        "stale_escape": 0,
    },
}


class ScorerTests(unittest.TestCase):
    def setUp(self):
        self.result_raw = RESULT_PATH.read_bytes().strip()
        self.score_raw = SCORE_PATH.read_bytes()
        self.score = json.loads(self.score_raw.decode("utf-8"))
        self.by_arm = {item["arm"]: item for item in self.score["arms"]}

    def test_mapping_is_complete_and_score_is_canonical(self):
        self.assertEqual(len(HIDDEN_TO_PUBLIC_TAXONOMY), 12)
        with self.assertRaises(TypeError):
            HIDDEN_TO_PUBLIC_TAXONOMY["tamper"] = "intent.scope"
        self.assertEqual(
            RULE_TO_PUBLIC_TAXONOMY,
            {
                "rule:data_scope": "intent.scope",
                "rule:migration_reversibility": "operability.rollback",
                "rule:operator_control": "operability.telemetry_control",
                "rule:bounded_attempts": "cost.bounded_fallback",
                "rule:ownership_metadata": "ownership.owner_runbook",
                "rule:independent_digest_comparison": "freshness.digest_binding",
            },
        )
        self.assertEqual(len(RULE_TO_PUBLIC_TAXONOMY), 6)
        with self.assertRaises(TypeError):
            RULE_TO_PUBLIC_TAXONOMY["rule:tamper"] = "intent.scope"
        self.assertTrue(
            set(RULE_TO_PUBLIC_TAXONOMY.values())
            <= set(HIDDEN_TO_PUBLIC_TAXONOMY.values())
        )
        self.assertEqual(
            normalize_predicted_issue_ids(("rule:unknown",)), ("rule:unknown",)
        )
        self.assertEqual(tuple(item["arm"] for item in self.score["arms"]), ARM_ORDER)
        canonical = json.dumps(
            self.score,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertTrue(self.score_raw.endswith(b"\n"))
        self.assertFalse(self.score_raw[:-1].endswith(b"\n"))
        self.assertEqual(self.score_raw.strip(), canonical)
        self.assertRegex(self.score["score_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_three_arm_metrics_match_fixed_actual_expectations(self):
        for arm_name in ARM_ORDER:
            item = self.by_arm[arm_name]
            expected = EXPECTED[arm_name]
            self.assertEqual(item["outcome_counts"], expected["outcomes"])
            self.assertEqual(item["finding_count"], expected["findings"])
            self.assertEqual(item["question_count"], expected["questions"])
            self.assertEqual(item["precision"], expected["precision"])
            self.assertEqual(item["matched_case_count"], expected["matched_cases"])
            self.assertEqual(item["macro_recall"], expected["matched_cases"] / CASE_TOTAL)
            self.assertEqual(
                (item["false_block"]["numerator"], item["false_block"]["eligible_denominator"]),
                expected["false_block"],
            )
            self.assertEqual(
                (item["missed_block"]["numerator"], item["missed_block"]["denominator"]),
                expected["missed_block"],
            )
            self.assertEqual(item["unsupported_finding"]["count"], expected["unsupported"])
            self.assertEqual(
                (item["evidence_ref_exists"]["total"], item["evidence_ref_exists"]["valid"]),
                expected["evidence_refs"],
            )
            self.assertEqual(
                (
                    item["evidence_location_correctness"]["blocking_finding_total"],
                    item["evidence_location_correctness"]["correct"],
                ),
                expected["evidence_location"],
            )
            self.assertEqual(item["stale_escape"]["count"], expected["stale_escape"])
            self.assertEqual(item["required_role_execution"], {"matched": 12, "total": 12, "rate": 1.0})

    def test_evidence_conflict_role_and_metric_availability_contract(self):
        self.assertEqual(self.by_arm["rules_only"]["council_conflict_retention"], {"status": "not_applicable"})
        self.assertEqual(self.by_arm["single_strong_reviewer"]["council_conflict_retention"], {"status": "not_applicable"})
        self.assertEqual(
            self.by_arm["specialized_council"]["council_conflict_retention"],
            {"status": "measured", "conflict_cases": 12, "retained": 12, "rate": 1.0},
        )
        rules = self.by_arm["rules_only"]
        self.assertEqual(rules["input_tokens"], {"available": True, "total": 0})
        self.assertEqual(rules["output_tokens"], {"available": True, "total": 0})
        self.assertEqual(rules["cost_usd"], {"available": True, "total": 0})
        self.assertEqual(rules["latency_seconds"], {"available": False, "total": None})
        for arm_name in ("single_strong_reviewer", "specialized_council"):
            for field in ("input_tokens", "output_tokens", "cost_usd", "latency_seconds"):
                self.assertEqual(self.by_arm[arm_name][field], {"available": False, "total": None})

    def test_verify_recomputes_and_rejects_score_or_result_tampering(self):
        self.assertIsNone(verify_score_report(self.score_raw.strip(), self.result_raw))

        score_tamper = copy.deepcopy(self.score)
        score_tamper["arms"][0]["precision"] = float("nan")
        with self.assertRaises(ValueError):
            verify_score_report(
                json.dumps(score_tamper, allow_nan=True).encode("utf-8"),
                self.result_raw,
            )

        score_tamper = copy.deepcopy(self.score)
        score_tamper["unknown"] = True
        with self.assertRaises(ValueError):
            verify_score_report(
                json.dumps(score_tamper, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                self.result_raw,
            )

        result_tamper = self.result_raw.replace(b"change-assurance-result-v0", b"tampered-result-v0")
        with self.assertRaises(ValueError):
            verify_score_report(self.score_raw.strip(), result_tamper)

    def test_score_contains_metrics_only_no_raw_claim_or_promotion(self):
        encoded = self.score_raw.decode("utf-8").lower()
        for forbidden in ("raw_response", '"claim"', "intent_scope_creep", "rubric:", "promotion", "human_time"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(self.score), {"schema_version", "dataset_id", "result_artifact_sha256", "arms", "score_digest"})


if __name__ == "__main__":
    unittest.main()
