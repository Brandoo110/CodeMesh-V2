import inspect
import json
import unittest

from assurance.evals.dataset import load_public_cases, reviewer_payload
from assurance.evals import adapters as adapters_module
from assurance.evals.adapters import InvocationFact, ModelArmAdapter


ROLE_ORDER = ("general", "intent", "architecture", "operability")
COUNCIL_ROLES = ("intent", "architecture", "operability")


def _response(*, claim="ok", outcome="PASS", evidence_ref=None, role_count=1):
    refs = [] if evidence_ref is None else [evidence_ref]
    findings = [
        {
            "claim": claim,
            "severity": "medium",
            "blocking": False,
            "evidence_refs": refs,
            "predicted_issue_ids": [f"issue_{role_count}"],
        }
    ]
    return json.dumps(
        {
            "findings": findings,
            "questions": ["confirm owner"],
            "predicted_outcome": outcome,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _invokers(raw_by_role):
    return {
        role: (lambda payload, role=role: raw_by_role[role])
        for role in ROLE_ORDER
    }


def _adapter(raw_by_role, **kwargs):
    return ModelArmAdapter(
        _invokers(raw_by_role),
        model_refs={role: f"model-{role}" for role in ROLE_ORDER},
        providers={role: "test-provider" for role in ROLE_ORDER},
        **kwargs,
    )


class AssuranceEvalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.case = load_public_cases()[0]
        self.payload = reviewer_payload(self.case)
        self.evidence_ref = self.payload["evidence_refs"][0]

    def test_single_valid_response_injects_general_role_and_derives_id(self):
        raw = _response(evidence_ref=self.evidence_ref)
        calls = []
        raw_by_role = {role: raw for role in ROLE_ORDER}
        raw_by_role["general"] = raw
        adapter = ModelArmAdapter(
            {
                **_invokers(raw_by_role),
                "general": lambda payload: calls.append(payload) or raw,
            },
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        )

        result = adapter.run_single(self.payload)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.executed_roles, ("general",))
        self.assertEqual(result.model_refs, ("model-general",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.findings[0].reviewer_role, "general")
        self.assertTrue(result.findings[0].finding_id.startswith("finding_"))
        self.assertRegex(result.receipt_ref, r"^sha256:[0-9a-f]{64}$")

    def test_same_model_can_serve_single_and_council_roles(self):
        raw = _response(evidence_ref=self.evidence_ref)
        shared_model = "gpt-5.6-luna"
        adapter = ModelArmAdapter(
            _invokers({role: raw for role in ROLE_ORDER}),
            model_refs={role: shared_model for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        )

        single = adapter.run_single(self.payload)
        council = adapter.run_council(self.payload)

        self.assertEqual(single.status, "success")
        self.assertEqual(single.model_refs, (shared_model,))
        self.assertEqual(council.status, "success")
        self.assertEqual(council.model_refs, (shared_model,) * 3)

    def test_council_aggregates_three_roles_with_conservative_priority(self):
        raw_by_role = {
            "general": _response(evidence_ref=self.evidence_ref),
            "intent": _response(
                claim="intent", outcome="BLOCKED", evidence_ref=self.evidence_ref
            ),
            "architecture": _response(
                claim="architecture", outcome="STALE", evidence_ref=self.evidence_ref
            ),
            "operability": _response(
                claim="operability", outcome="PASS", evidence_ref=self.evidence_ref
            ),
        }
        result = _adapter(raw_by_role).run_council(self.payload)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.executed_roles, COUNCIL_ROLES)
        self.assertEqual(result.model_refs, tuple(f"model-{role}" for role in COUNCIL_ROLES))
        self.assertEqual(
            tuple(finding.reviewer_role for finding in result.findings),
            COUNCIL_ROLES,
        )
        self.assertEqual(result.predicted_outcome, "STALE")
        self.assertEqual(len({finding.finding_id for finding in result.findings}), 3)

    def test_each_council_role_gets_independent_json_round_trip_payload(self):
        baseline = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        observed = {}

        def make(role):
            def invoke(payload):
                observed[role] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if role == "intent":
                    payload["source_files"][0][1] = "mutated"
                return _response(evidence_ref=self.evidence_ref)

            return invoke

        adapter = ModelArmAdapter(
            {role: make(role) for role in ROLE_ORDER},
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        )
        adapter.run_council(self.payload)

        self.assertEqual(
            tuple(observed[role] for role in COUNCIL_ROLES),
            (baseline, baseline, baseline),
        )
        self.assertEqual(self.payload, reviewer_payload(self.case))

    def test_strict_json_rejects_unknown_duplicate_nonfinite_bom_and_nul(self):
        bad = (
            b'{"findings":[],"unknown":1}',
            b'{"findings":[],"findings":[]}',
            b'{"findings":[],"predicted_outcome":NaN}',
            b'\xef\xbb\xbf{"findings":[]}',
            b'{"findings":[],"questions":["bad\x00"]}',
        )
        for raw in bad:
            result = _adapter({role: raw for role in ROLE_ORDER}).run_single(self.payload)
            self.assertEqual(result.status, "schema_invalid")
            self.assertEqual(result.error_code, "invalid_model_response")

    def test_questions_over_limit_are_schema_invalid(self):
        raw = json.dumps(
            {
                "findings": [],
                "questions": [f"question-{index}" for index in range(65)],
                "predicted_outcome": "PASS",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        result = _adapter({role: raw for role in ROLE_ORDER}).run_single(self.payload)

        self.assertEqual(result.status, "schema_invalid")
        self.assertEqual(result.error_code, "invalid_model_response")

    def test_schema_rejects_malformed_finding_evidence_and_oversize(self):
        malformed = (
            {"findings": [{"claim": "x"}]},
            {
                "findings": [
                    {
                        "claim": "x",
                        "severity": "medium",
                        "blocking": False,
                        "evidence_refs": ["not-public"],
                        "predicted_issue_ids": [],
                    }
                ]
            },
            {
                "findings": [
                    {
                        "claim": "x",
                        "severity": "medium",
                        "blocking": False,
                        "evidence_refs": [],
                        "predicted_issue_ids": [],
                        "finding_id": "model-supplied",
                    }
                ]
            },
        )
        for value in malformed:
            raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
            result = _adapter({role: raw for role in ROLE_ORDER}).run_single(self.payload)
            self.assertEqual(result.status, "schema_invalid")
            self.assertEqual(result.error_code, "invalid_model_response")
        oversized = b'{"findings":[]}' + (b" " * (256 * 1024))
        result = _adapter({role: oversized for role in ROLE_ORDER}).run_single(self.payload)
        self.assertEqual(result.status, "schema_invalid")

    def test_invocation_exception_is_redacted_and_partial_roles_are_reported(self):
        def fail(_payload):
            raise RuntimeError("secret traceback text")

        calls = []

        def intent(payload):
            calls.append("intent")
            return _response(evidence_ref=self.evidence_ref)

        def architecture(payload):
            calls.append("architecture")
            raise RuntimeError("secret traceback text")

        def operability(payload):
            calls.append("operability")
            return _response(evidence_ref=self.evidence_ref)

        adapter = ModelArmAdapter(
            {"general": fail, "intent": intent, "architecture": architecture, "operability": operability},
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        )
        result = adapter.run_council(self.payload)

        self.assertEqual(result.status, "failure")
        self.assertEqual(result.error_code, "model_invocation_error")
        self.assertEqual(result.executed_roles, ("intent",))
        self.assertEqual(result.model_refs, ("model-intent",))
        self.assertEqual(calls, ["intent", "architecture"])
        self.assertNotIn("secret traceback text", repr(result))

    def test_invocation_fact_metrics_and_receipt_are_deterministic(self):
        raw = _response(evidence_ref=self.evidence_ref)

        def invoke(_payload):
            return InvocationFact(
                role="general",
                model_ref="model-general",
                provider="test-provider",
                raw_response=raw,
                input_tokens=3,
                output_tokens=4,
                cost_usd=0.25,
                latency_seconds=0.5,
            )

        invokers = {role: (invoke if role == "general" else lambda payload: raw) for role in ROLE_ORDER}
        first = ModelArmAdapter(
            invokers,
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        ).run_single(self.payload)
        second = ModelArmAdapter(
            invokers,
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        ).run_single(self.payload)
        self.assertEqual(first.input_tokens, 3)
        self.assertEqual(first.output_tokens, 4)
        self.assertEqual(first.cost_usd, 0.25)
        self.assertEqual(first.latency_seconds, 0.5)
        self.assertEqual(first.receipt_ref, second.receipt_ref)

    def test_mutated_invocation_fact_is_rejected_fail_closed(self):
        raw = _response(evidence_ref=self.evidence_ref)

        def mutate_fact(_payload):
            fact = InvocationFact(
                role="general",
                model_ref="model-general",
                provider="test-provider",
                raw_response=raw,
            )
            object.__setattr__(fact, "role", "intent")
            return fact

        invokers = _invokers({role: raw for role in ROLE_ORDER})
        invokers["general"] = mutate_fact
        result = ModelArmAdapter(
            invokers,
            model_refs={role: f"model-{role}" for role in ROLE_ORDER},
            providers={role: "test-provider" for role in ROLE_ORDER},
        ).run_single(self.payload)

        self.assertEqual(result.status, "schema_invalid")
        self.assertEqual(result.error_code, "invalid_model_response")

    def test_invocation_fact_rejects_non_bytes_and_nonfinite_measurement(self):
        with self.assertRaises(ValueError):
            InvocationFact("general", "model", "provider", "not-bytes")
        with self.assertRaises(ValueError):
            InvocationFact(
                "general", "model", "provider", b"{}", cost_usd=float("inf")
            )

    def test_adapter_source_has_no_hidden_loader(self):
        source = inspect.getsource(adapters_module)
        self.assertNotIn("load_hidden_gold", source)


if __name__ == "__main__":
    unittest.main()
