import copy
import json
import unittest
from pathlib import Path

from assurance.evals.promotion import (
    NOT_PROMOTED,
    PROMOTION_SCHEMA_VERSION,
    PROMOTED,
    THRESHOLDS,
    build_promotion_decision,
    derive_promotion_state,
    verify_promotion_decision,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "assurance" / "evals" / "results" / "change_assurance_v0_luna.json"
SCORE_PATH = ROOT / "assurance" / "evals" / "results" / "change_assurance_v0_luna_scores.json"
PROMOTION_PATH = ROOT / "assurance" / "evals" / "results" / "change_assurance_v0_luna_promotion.json"


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.result_raw = RESULT_PATH.read_bytes()
        self.score_raw = SCORE_PATH.read_bytes()
        self.promotion_raw = PROMOTION_PATH.read_bytes()
        self.promotion = json.loads(self.promotion_raw.decode("utf-8"))

    def test_fixed_not_promoted_decision_and_canonical_artifact(self):
        all_pass = {
            name: "PASS"
            for name in (
                "benefit",
                "false_block",
                "stale_escape",
                "blocking_evidence",
                "cost",
                "role_execution",
                "conflict_retention",
            )
        }
        promoted = derive_promotion_state(all_pass)
        self.assertEqual(promoted["decision"], PROMOTED)
        self.assertEqual(promoted["default_topology"], "specialized_council+policy_gate")
        self.assertEqual(promoted["council_policy"], "default_after_promotion")
        self.assertEqual(
            promoted["council_constraints"],
            {
                "default_enabled": True,
                "allowed_when": ["default"],
                "requires": ["policy_gate"],
                "can_override_stale_or_evidence_gate": False,
                "re_evaluation_required": False,
            },
        )
        with self.assertRaises(ValueError):
            derive_promotion_state({**all_pass, "unknown": "PASS"})
        with self.assertRaises(ValueError):
            derive_promotion_state({name: "PASS" for name in all_pass if name != "cost"})
        self.assertEqual(self.promotion_raw.count(b"\n"), 1)
        self.assertEqual(self.promotion_raw.strip(), build_promotion_decision(self.score_raw, self.result_raw))
        self.assertEqual(self.promotion["schema_version"], PROMOTION_SCHEMA_VERSION)
        self.assertEqual(self.promotion["dataset_id"], "change_assurance_v0")
        self.assertEqual(self.promotion["decision"], NOT_PROMOTED)
        self.assertEqual(
            self.promotion["default_topology"],
            "single_strong_reviewer+policy_gate",
        )
        self.assertEqual(
            self.promotion["council_policy"],
            "experimental_high_risk_or_conflict_only",
        )
        self.assertEqual(
            self.promotion["council_constraints"],
            {
                "default_enabled": False,
                "allowed_when": ["high_risk", "cross_role_conflict"],
                "requires": ["policy_gate", "human_review"],
                "can_override_stale_or_evidence_gate": False,
                "re_evaluation_required": True,
            },
        )

    def test_fail_closed_checks_have_fixed_observations(self):
        with self.assertRaises(TypeError):
            THRESHOLDS["false_block_delta_max"] = 1.0
        self.assertEqual(
            THRESHOLDS,
            {
                "macro_recall_gain_min": 0.10,
                "unsupported_rate_reduction_min": 0.25,
                "human_review_time_reduction_min": 0.25,
                "false_block_delta_max": 0.03,
                "stale_escape_count_max": 0,
                "blocking_evidence_rate_min": 1.0,
                "council_cost_multiplier_max": 2.5,
            },
        )
        checks = self.promotion["checks"]
        self.assertEqual(checks["benefit"]["status"], "FAIL")
        self.assertEqual(checks["benefit"]["observed"]["macro_recall_gain"], 0.0)
        self.assertEqual(checks["benefit"]["observed"]["single_unsupported_rate"], 0.0)
        self.assertEqual(checks["benefit"]["observed"]["council_unsupported_rate"], 0.0)
        self.assertIsNone(checks["benefit"]["observed"]["unsupported_rate_reduction"])
        self.assertIsNone(checks["benefit"]["observed"]["human_review_time_reduction"])
        self.assertEqual(checks["false_block"]["status"], "FAIL")
        self.assertEqual(checks["false_block"]["observed"], 1 / 3)
        self.assertEqual(checks["stale_escape"]["status"], "PASS")
        self.assertEqual(checks["stale_escape"]["observed"], 0)
        self.assertEqual(checks["blocking_evidence"]["status"], "PASS")
        self.assertEqual(checks["blocking_evidence"]["observed"], 1.0)
        self.assertEqual(checks["cost"]["status"], "UNAVAILABLE_FAIL_CLOSED")
        self.assertIsNone(checks["cost"]["observed"]["single_cost_usd"])
        self.assertIsNone(checks["cost"]["observed"]["council_cost_usd"])

    def test_supportive_role_and_conflict_checks_pass(self):
        checks = self.promotion["checks"]
        self.assertEqual(checks["role_execution"]["status"], "PASS")
        self.assertEqual(checks["role_execution"]["observed"], {"single": 1.0, "council": 1.0})
        self.assertEqual(checks["conflict_retention"]["status"], "PASS")
        self.assertEqual(checks["conflict_retention"]["observed"], 1.0)

    def test_verify_is_deterministic_and_rejects_tampering(self):
        self.assertIsNone(verify_promotion_decision(self.promotion_raw, self.score_raw, self.result_raw))

        tampered = copy.deepcopy(self.promotion)
        tampered["decision"] = "PROMOTED"
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(),
                self.score_raw,
                self.result_raw,
            )

        score_tampered = json.loads(self.score_raw.decode("utf-8"))
        score_tampered["arms"][2]["macro_recall"] = 0.0
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                self.promotion_raw,
                json.dumps(score_tampered, sort_keys=True, separators=(",", ":")).encode(),
                self.result_raw,
            )

        tampered = copy.deepcopy(self.promotion)
        tampered["checks"]["false_block"]["reason_code"] = "ALLOW"
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(),
                self.score_raw,
                self.result_raw,
            )

        tampered = copy.deepcopy(self.promotion)
        tampered["thresholds"]["false_block_delta_max"] = 1.0
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(),
                self.score_raw,
                self.result_raw,
            )

        tampered["unknown"] = True
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(),
                self.score_raw,
                self.result_raw,
            )

        with self.assertRaises(ValueError):
            verify_promotion_decision(
                b'{"schema_version":1,"schema_version":2}',
                self.score_raw,
                self.result_raw,
            )

        tampered = copy.deepcopy(self.promotion)
        tampered["checks"]["benefit"]["observed"]["macro_recall_gain"] = float("nan")
        with self.assertRaises(ValueError):
            verify_promotion_decision(
                json.dumps(tampered, allow_nan=True).encode(),
                self.score_raw,
                self.result_raw,
            )

    def test_artifact_is_metrics_only(self):
        encoded = self.promotion_raw.decode("utf-8").lower()
        for forbidden in (
            "raw_response",
            '"claim"',
            "intent_scope_creep",
            "rubric:",
            "generated_at",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(
            set(self.promotion),
            {
                "schema_version",
                "dataset_id",
                "result_artifact_sha256",
                "score_report_digest",
                "thresholds",
                "checks",
                "decision",
                "default_topology",
                "council_policy",
                "council_constraints",
                "decision_digest",
            },
        )


if __name__ == "__main__":
    unittest.main()
