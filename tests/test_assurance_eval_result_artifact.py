import copy
import inspect
import json
import unittest

from assurance.evals.dataset import load_public_cases, reviewer_payload
from assurance.evals import result_artifact as artifact_module
from assurance.evals.result_artifact import (
    MODEL_REF,
    PROVIDER,
    PUBLIC_ISSUE_TAXONOMY,
    ROLE_ORDER,
    build_result_artifact,
    replay_result_artifact,
)


CASE_IDS = tuple(f"ca_v0_{index:03d}" for index in range(1, 13))
ROLE_ISSUES = {
    "general": "intent.scope",
    "intent": "intent.acceptance_nfr",
    "architecture": "architecture.dependency_direction",
    "operability": "operability.rollback",
}


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _role_bundle(role, *, issue=None):
    issue = ROLE_ISSUES[role] if issue is None else issue
    responses = []
    for case in load_public_cases():
        payload = reviewer_payload(case)
        responses.append(
            {
                "case_id": case.case_id,
                "findings": [
                    {
                        "claim": f"{role} public claim",
                        "severity": "low",
                        "blocking": False,
                        "evidence_refs": [payload["evidence_refs"][0]],
                        "predicted_issue_ids": [issue],
                    }
                ],
                "questions": [],
                "predicted_outcome": "PASS",
            }
        )
    return _canonical({"role": role, "responses": responses})


def _valid_bundles():
    return {role: _role_bundle(role) for role in ROLE_ORDER}


class ResultArtifactTests(unittest.TestCase):
    def test_build_has_fixed_taxonomy_12x3_and_role_facts(self):
        raw = build_result_artifact(_valid_bundles())
        artifact = json.loads(raw.decode("utf-8"))

        self.assertEqual(
            set(artifact),
            {
                "schema_version",
                "dataset_id",
                "run_label",
                "model_ref",
                "provider",
                "public_issue_taxonomy",
                "role_bundles",
                "comparison_run",
            },
        )
        self.assertEqual(artifact["dataset_id"], "change_assurance_v0")
        self.assertEqual(artifact["run_label"], "codex-desktop-luna-max")
        self.assertEqual(artifact["model_ref"], MODEL_REF)
        self.assertEqual(artifact["provider"], PROVIDER)
        self.assertEqual(tuple(artifact["public_issue_taxonomy"]), PUBLIC_ISSUE_TAXONOMY)
        self.assertEqual(set(artifact["role_bundles"]), set(ROLE_ORDER))
        self.assertEqual(
            tuple(item["case_id"] for item in artifact["comparison_run"]["cases"]),
            CASE_IDS,
        )
        self.assertEqual(
            sum(len(item["arms"]) for item in artifact["comparison_run"]["cases"]),
            36,
        )
        for case in artifact["comparison_run"]["cases"]:
            self.assertEqual(
                tuple(arm["arm"] for arm in case["arms"]),
                ("rules_only", "single_strong_reviewer", "specialized_council"),
            )
            for arm in case["arms"][1:]:
                self.assertEqual(arm["model_refs"], [MODEL_REF] * len(arm["executed_roles"]))
                self.assertIsNone(arm["input_tokens"])
                self.assertIsNone(arm["output_tokens"])
                self.assertIsNone(arm["cost_usd"])
                self.assertIsNone(arm["latency_seconds"])
        self.assertNotIn("hidden", raw.decode("utf-8").lower())
        self.assertNotIn("scoring", raw.decode("utf-8").lower())
        self.assertNotIn("promotion", raw.decode("utf-8").lower())
        self.assertNotIn("generated_at", raw.decode("utf-8").lower())
        for role in ROLE_ORDER:
            self.assertEqual(
                tuple(item["case_id"] for item in artifact["role_bundles"][role]["responses"]),
                CASE_IDS,
            )
            self.assertTrue(
                all(isinstance(item["raw_response"], str) for item in artifact["role_bundles"][role]["responses"])
            )

    def test_shared_model_refs_are_valid_and_replay_is_deterministic(self):
        first = build_result_artifact(_valid_bundles())
        second = build_result_artifact(_valid_bundles())

        self.assertEqual(first, second)
        replayed = replay_result_artifact(first)
        self.assertEqual(replayed.dataset_id, "change_assurance_v0")
        self.assertEqual(tuple(case.case_id for case in replayed.cases), CASE_IDS)
        self.assertEqual(sum(len(case.arms) for case in replayed.cases), 36)

    def test_role_taxonomy_isolated_and_only_declared_ids_are_accepted(self):
        invalid = _valid_bundles()
        invalid["intent"] = _role_bundle(
            "intent", issue="architecture.dependency_direction"
        )
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        invalid = _valid_bundles()
        invalid["operability"] = _role_bundle(
            "operability", issue="boundary.provider_residency"
        )
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        self.assertEqual(
            set(PUBLIC_ISSUE_TAXONOMY),
            {
                "intent.scope",
                "intent.acceptance_nfr",
                "architecture.dependency_direction",
                "architecture.single_source_policy",
                "architecture.contract_decision",
                "operability.rollback",
                "operability.idempotency",
                "operability.telemetry_control",
                "cost.bounded_fallback",
                "ownership.owner_runbook",
                "boundary.provider_residency",
                "freshness.digest_binding",
            },
        )

    def test_outer_bundle_is_strict_about_encoding_shape_order_and_keys(self):
        valid = _valid_bundles()

        duplicate_key = b'{"role":"general","role":"general","responses":[]}'
        invalid = dict(valid)
        invalid["general"] = duplicate_key

        unknown = json.loads(valid["general"].decode("utf-8"))
        unknown["extra"] = True
        invalid["general"] = _canonical(unknown)
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        invalid = dict(valid)
        invalid["general"] = duplicate_key
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        wrong_role = json.loads(valid["general"].decode("utf-8"))
        wrong_role["role"] = "intent"
        invalid = dict(valid)
        invalid["general"] = _canonical(wrong_role)
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        wrong_order = json.loads(valid["general"].decode("utf-8"))
        wrong_order["responses"] = list(reversed(wrong_order["responses"]))
        invalid = dict(valid)
        invalid["general"] = _canonical(wrong_order)
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        invalid = dict(valid)
        invalid["general"] = b"\xef\xbb\xbf" + valid["general"]
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

        invalid = dict(valid)
        invalid["general"] = valid["general"].replace(b"\"responses\"", b"\"responses\":NaN,\"ignored\"")
        with self.assertRaises(ValueError):
            build_result_artifact(invalid)

    def test_replay_rejects_stored_comparison_or_raw_response_tampering(self):
        artifact = json.loads(build_result_artifact(_valid_bundles()).decode("utf-8"))

        comparison_tamper = copy.deepcopy(artifact)
        comparison_tamper["comparison_run"]["cases"][0]["public_payload_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            replay_result_artifact(_canonical(comparison_tamper))

        raw_tamper = copy.deepcopy(artifact)
        raw_tamper["role_bundles"]["general"]["responses"][0]["raw_response"] = _canonical(
            {"findings": [], "questions": [], "predicted_outcome": "PASS"}
        ).decode("utf-8")
        with self.assertRaises(ValueError):
            replay_result_artifact(_canonical(raw_tamper))

    def test_result_artifact_source_has_no_hidden_loader(self):
        source = inspect.getsource(artifact_module)
        self.assertNotIn("load_hidden_gold", source)
        self.assertNotIn("issue:", source)
        self.assertNotIn("rubric:", source)


if __name__ == "__main__":
    unittest.main()
