import hashlib
import json
import unittest
from pathlib import Path

from assurance.evals.result_artifact import (
    MODEL_REF,
    PROVIDER,
    PUBLIC_ISSUE_TAXONOMY,
    ROLE_ORDER,
    replay_result_artifact,
)


ARTIFACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "assurance"
    / "evals"
    / "results"
    / "change_assurance_v0_luna.json"
)
CASE_IDS = tuple(f"ca_v0_{index:03d}" for index in range(1, 13))
ARM_ORDER = ("rules_only", "single_strong_reviewer", "specialized_council")
EXPECTED_OUTCOMES = {
    "rules_only": {"BLOCKED": 5, "NEEDS_HUMAN": 6, "STALE": 1},
    "single_strong_reviewer": {"BLOCKED": 7, "NEEDS_HUMAN": 4, "STALE": 1},
    "specialized_council": {"BLOCKED": 8, "NEEDS_HUMAN": 3, "STALE": 1},
}
EXPECTED_FINDINGS = {
    "rules_only": 6,
    "single_strong_reviewer": 12,
    "specialized_council": 12,
}
EXPECTED_QUESTIONS = {
    "rules_only": 6,
    "single_strong_reviewer": 5,
    "specialized_council": 10,
}

class ActualResultArtifactTests(unittest.TestCase):
    def test_artifact_is_canonical_and_replays_fixed_12x3(self):
        raw = ARTIFACT_PATH.read_bytes()
        canonical = json.dumps(
            json.loads(raw),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw[:-1].endswith(b"\n"))
        self.assertEqual(raw.strip(), canonical)
        artifact = json.loads(raw.decode("utf-8"))
        replayed = replay_result_artifact(raw.strip())

        self.assertEqual(tuple(case.case_id for case in replayed.cases), CASE_IDS)
        self.assertEqual(sum(len(case.arms) for case in replayed.cases), 36)
        self.assertEqual(
            tuple(arm["arm"] for arm in artifact["comparison_run"]["cases"][0]["arms"]),
            ARM_ORDER,
        )
        self.assertEqual(set(artifact["role_bundles"]), set(ROLE_ORDER))
        self.assertEqual(
            sum(
                len(artifact["role_bundles"][role]["responses"])
                for role in ROLE_ORDER
            ),
            48,
        )
        self.assertEqual(artifact["model_ref"], MODEL_REF)
        self.assertEqual(artifact["provider"], PROVIDER)
        self.assertEqual(tuple(artifact["public_issue_taxonomy"]), PUBLIC_ISSUE_TAXONOMY)

    def test_role_alignment_metrics_and_fixed_outcome_distribution(self):
        run = replay_result_artifact(ARTIFACT_PATH.read_bytes().strip())
        by_arm = {arm: [] for arm in ARM_ORDER}
        for case in run.cases:
            for arm in case.arms:
                by_arm[arm.arm].append(arm)

        for arm_name in ARM_ORDER:
            arms = by_arm[arm_name]
            outcomes = {}
            findings = 0
            questions = 0
            for arm in arms:
                outcomes[arm.predicted_outcome] = outcomes.get(arm.predicted_outcome, 0) + 1
                findings += len(arm.findings)
                questions += len(arm.questions)
                self.assertEqual(arm.status, "success")
                if arm_name == "rules_only":
                    self.assertEqual(arm.executed_roles, ("rules",))
                    self.assertEqual(arm.model_refs, ("rules:public-v0",))
                    self.assertEqual((arm.input_tokens, arm.output_tokens, arm.cost_usd), (0, 0, 0))
                    self.assertIsNone(arm.latency_seconds)
                elif arm_name == "single_strong_reviewer":
                    self.assertEqual(arm.executed_roles, ("general",))
                    self.assertEqual(arm.model_refs, (MODEL_REF,))
                else:
                    self.assertEqual(
                        arm.executed_roles,
                        ("intent", "architecture", "operability"),
                    )
                    self.assertEqual(arm.model_refs, (MODEL_REF,) * 3)
                if arm_name != "rules_only":
                    self.assertIsNone(arm.input_tokens)
                    self.assertIsNone(arm.output_tokens)
                    self.assertIsNone(arm.cost_usd)
                    self.assertIsNone(arm.latency_seconds)
            self.assertEqual(outcomes, EXPECTED_OUTCOMES[arm_name])
            self.assertEqual(findings, EXPECTED_FINDINGS[arm_name])
            self.assertEqual(questions, EXPECTED_QUESTIONS[arm_name])

    def test_artifact_has_no_hidden_or_promotion_material(self):
        raw = ARTIFACT_PATH.read_bytes().decode("utf-8")
        lowered = raw.lower()
        for forbidden in (
            "intent_scope_creep",
            "freshness_old_approval_survives_new_digest",
            "issue:",
            "rubric:",
            "scoring",
            "promotion",
            "generated_at",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_artifact_digest_is_stable(self):
        canonical = ARTIFACT_PATH.read_bytes().strip()
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "46805cb6e65b69114552f7d81d78f9dc93c3eaba2eb2e376ee101e993ca5f3c5",
        )


if __name__ == "__main__":
    unittest.main()
