"""V2-P3-05 Baseline Report focused tests."""

import ast
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
import assurance.baseline_report as report_module
import assurance.single_reviewer as single_module
import tests.test_assurance_policy as policy_tests
import tests.test_assurance_single_reviewer as single_tests
from assurance import (
    BaselineArmMetrics,
    BaselineReport,
    BaselineReportBuilder,
    BaselineReportInput,
    RulesPredictionJudgment,
    SingleFindingJudgment,
)
from assurance.artifacts import ArtifactStore
from assurance.contracts import ChangeSubject
from assurance.manifest import EvidenceManifestEntry
from assurance.policy import PolicyEvaluationInput, PolicyGate
from assurance.rules_baseline import (
    RulesOnlyBaselineResult,
    RulesOnlyBaselineRunner,
    RulesOnlyExpectation,
    RulesOnlyFixture,
)
from assurance.single_reviewer import (
    ReviewerEvidenceContext,
    SingleReviewerInput,
    SingleReviewerNormalizationInput,
    SingleStrongReviewer,
)


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)

NEW_PUBLIC_NAMES = frozenset(
    {
        "RulesPredictionJudgment",
        "SingleFindingJudgment",
        "BaselineArmMetrics",
        "BaselineReportInput",
        "BaselineReport",
        "BaselineReportBuilder",
    }
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(letter: str) -> str:
    return "sha256:" + (letter * 64)


def _context(evidence_id: str, kind: str, content: str):
    digest = _sha256(content.encode("utf-8"))
    return ReviewerEvidenceContext(
        schema_version="v1",
        evidence_id=evidence_id,
        kind=kind,
        artifact_digest=digest,
        content=content,
        content_digest=digest,
        truncated=False,
        redaction_status="not_applicable",
    )


def _default_contexts():
    return (
        _context("ev-1", "git_snapshot", "git snapshot evidence"),
        _context("ev-2", "intake_documents", "intake adr evidence"),
        _context("ev-3", "command_batch", "command batch evidence"),
    )


_COLLECTOR_PRODUCERS = {
    "git_snapshot": "collector.git",
    "intake_documents": "collector.intake",
    "command_batch": "collector.command",
}


def _stack(subject_digest=None):
    if subject_digest is None:
        subject_digest = _digest("c")
    contexts = _default_contexts()
    entries = tuple(
        EvidenceManifestEntry(
            schema_version="v1",
            evidence_id=item.evidence_id,
            kind=item.kind,
            trust_level="observed",
            producer=_COLLECTOR_PRODUCERS[item.kind],
            subject_digest=subject_digest,
            artifact_digest=item.artifact_digest,
            source_ref=_COLLECTOR_PRODUCERS[item.kind] + ":1",
            status="success",
            collected_at=FIXED_TIME,
            fresh_until=LATER_TIME,
            freshness="fresh",
            redaction_status="not_applicable",
        )
        for item in contexts
    )
    manifest = policy_tests._manifest(subject_digest, entries=entries)
    risk_input = policy_tests._risk_input(subject_digest, manifest=manifest)
    subject = policy_tests._subject(subject_digest)
    from assurance.risk import RiskClassifier

    risk_result = RiskClassifier.classify(risk_input)
    reviewer_input = SingleReviewerInput(
        schema_version="v1",
        subject=subject,
        risk_result=risk_result,
        contexts=contexts,
        evaluated_at=FIXED_TIME,
    )
    return {
        "subject_digest": subject_digest,
        "subject": subject,
        "risk_input": risk_input,
        "risk_result": risk_result,
        "reviewer_input": reviewer_input,
        "contexts": contexts,
        "manifest": manifest,
    }


def _response_bytes(subject_digest, findings=(), questions=()):
    payload = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "rubric_hash": single_module._RUBRIC_DIGEST,
        "findings": list(findings),
        "questions": list(questions),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _finding_draft(**overrides):
    values = {
        "reviewer_role": "intent",
        "claim": "semantic scope issue",
        "evidence_refs": ["ev-1"],
        "severity": "medium",
        "confidence": 0.8,
    }
    values.update(overrides)
    return values


def _default_finding_drafts():
    return (
        _finding_draft(severity="high", claim="semantic scope issue A"),
        _finding_draft(severity="info", claim="informational note"),
        _finding_draft(severity="medium", claim="semantic scope issue B"),
    )


def _single_result(stack, *, findings=None, questions=(), invocation_overrides=None):
    if findings is None:
        findings = _default_finding_drafts()
    raw = _response_bytes(stack["subject_digest"], findings, questions)
    prompt = SingleStrongReviewer.prepare(stack["reviewer_input"])
    invocation = single_tests._invocation(**(invocation_overrides or {}))
    normalization_input = SingleReviewerNormalizationInput(
        schema_version="v1",
        reviewer_input=stack["reviewer_input"],
        prompt=prompt,
        invocation=invocation,
        raw_response=raw,
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp))
        return SingleStrongReviewer.normalize(normalization_input, store)


def _rules_result(
    stack,
    *,
    expectations=None,
    gold_outcome="BLOCKED",
):
    if expectations is None:
        expectations = (
            RulesOnlyExpectation(
                schema_version="v1",
                issue_id="scope-creep",
                category="intent",
                expected_reason_refs=(),
            ),
        )
    fixture = RulesOnlyFixture(
        schema_version="v1",
        fixture_id="scope_creep",
        subject=stack["subject"],
        risk_input=stack["risk_input"],
        expectations=expectations,
        allowed_reason_refs=(),
        gold_outcome=gold_outcome,
        evaluated_at=FIXED_TIME,
    )
    return RulesOnlyBaselineRunner.run(fixture)


def _policy_result(
    stack,
    single_result,
    *,
    subject=None,
    risk_result=None,
    findings=None,
    execution_receipts=None,
):
    policy_input = PolicyEvaluationInput(
        schema_version="v1",
        subject=subject if subject is not None else stack["subject"],
        risk_result=(
            risk_result if risk_result is not None else stack["risk_result"]
        ),
        findings=(
            findings if findings is not None else single_result.findings
        ),
        execution_receipts=(
            execution_receipts
            if execution_receipts is not None
            else (single_result.execution_receipt,)
        ),
        human_decisions=(),
        evaluated_at=LATER_TIME,
    )
    return PolicyGate.evaluate(policy_input)


def _rules_judgment(
    prediction_ref,
    *,
    matched=(),
    support="supported",
    readability="assessed",
    score=5,
    assessor="human:lead",
):
    return RulesPredictionJudgment(
        schema_version="v1",
        prediction_ref=prediction_ref,
        matched_issue_ids=matched,
        support_status=support,
        readability_status=readability,
        readability_score=score,
        assessor_ref=assessor,
    )


def _single_judgment(
    finding_id,
    *,
    matched=(),
    support="supported",
    readability="assessed",
    score=5,
    assessor="human:lead",
):
    return SingleFindingJudgment(
        schema_version="v1",
        finding_id=finding_id,
        matched_issue_ids=matched,
        support_status=support,
        readability_status=readability,
        readability_score=score,
        assessor_ref=assessor,
    )


def _default_rules_judgments(rules_result, *, support="unsupported"):
    return tuple(
        _rules_judgment(ref, support=support) for ref in rules_result.observed_reason_refs
    )


def _default_single_judgments(single_result, *, matched=("scope-creep",)):
    return tuple(
        _single_judgment(finding.finding_id, matched=matched)
        for finding in single_result.findings
    )


def _input(
    stack,
    rules_result,
    single_result,
    single_policy_result,
    *,
    rules_judgments=None,
    single_judgments=None,
):
    if rules_judgments is None:
        rules_judgments = _default_rules_judgments(rules_result)
    if single_judgments is None:
        single_judgments = _default_single_judgments(single_result)
    return BaselineReportInput(
        schema_version="v1",
        rules_result=rules_result,
        single_result=single_result,
        single_policy_result=single_policy_result,
        rules_judgments=rules_judgments,
        single_judgments=single_judgments,
    )


def _build(stack, rules_result, single_result, single_policy_result, **kwargs):
    return BaselineReportBuilder.build(
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            **kwargs,
        )
    )


def _canonical_body(data: dict) -> bytes:
    body = {
        key: value
        for key, value in data.items()
        if key not in ("report_id", "report_digest")
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _metrics(arm="single_strong_reviewer", **overrides):
    values = {
        "schema_version": "v1",
        "arm": arm,
        "prediction_count": 0,
        "true_positive_predictions": 0,
        "false_positive_predictions": 0,
        "precision_status": "unavailable",
        "precision": None,
        "gold_issue_count": 0,
        "detected_gold_issue_count": 0,
        "recall_status": "unavailable",
        "recall": None,
        "unsupported_count": 0,
        "questions_count": 0,
        "actual_outcome": "PASS",
        "gold_outcome": "PASS",
        "outcome_match": True,
        "false_block": False,
        "false_pass": False,
        "usage_status": "not_applicable" if arm == "rules_only" else "measured",
        "input_tokens": None if arm == "rules_only" else 120,
        "output_tokens": None if arm == "rules_only" else 80,
        "cost_status": "not_applicable" if arm == "rules_only" else "measured",
        "cost_usd": 0.0 if arm == "rules_only" else 0.0125,
        "latency_status": "unavailable" if arm == "rules_only" else "measured",
        "latency_ms": None if arm == "rules_only" else 3_600_000,
        "readability_status": "assessed",
        "readability_score": 4.0,
    }
    values.update(overrides)
    return BaselineArmMetrics.model_validate(values)


@pytest.fixture(scope="module")
def scenario():
    stack = _stack()
    rules_result = _rules_result(stack)
    single_result = _single_result(stack)
    single_policy_result = _policy_result(stack, single_result)
    return stack, rules_result, single_result, single_policy_result


@pytest.fixture(scope="module")
def report(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    return _build(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        rules_judgments=_default_rules_judgments(rules_result),
        single_judgments=_default_single_judgments(single_result),
    )


def test_public_api_importable_from_package():
    assert NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is getattr(report_module, name)


def test_package_exports_preserve_prior_and_allow_future_additions():
    exported = set(assurance.__all__)
    prior_sentinels = {
        "AcceptanceCase",
        "ChangeSubject",
        "ArtifactStore",
        "RiskClassifier",
        "PolicyGate",
        "RulesOnlyBaselineRunner",
        "SingleStrongReviewer",
        "EvidenceManifest",
        "AuthorAgentReceipt",
    }
    assert prior_sentinels <= exported
    assert NEW_PUBLIC_NAMES <= exported
    assert len(assurance.__all__) == len(set(assurance.__all__))


def test_module_ast_has_no_io_model_execution_environment_or_network():
    source = Path(report_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_modules = {
        "base64",
        "hashlib",
        "json",
        "re",
        "typing",
        "pydantic",
        "contracts",
        "intake",
        "manifest",
        "policy",
        "risk",
        "rules_baseline",
        "snapshot",
        "single_reviewer",
    }
    forbidden_identifiers = {
        "os",
        "sys",
        "pathlib",
        "Path",
        "open",
        "sqlite3",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "random",
        "datetime",
        "time",
        "environ",
        "getenv",
        "subprocess",
        "Popen",
        "check_output",
        "model_construct",
        "RiskClassifier",
        "PolicyGate",
        "RulesOnlyBaselineRunner",
        "SingleStrongReviewer",
        "ArtifactStore",
        "AuthorAgentReceipt",
        "eval",
        "exec",
        "__import__",
    }
    forbidden_calls = {
        "open",
        "eval",
        "exec",
        "__import__",
        "model_construct",
        "RiskClassifier",
        "PolicyGate",
        "RulesOnlyBaselineRunner",
        "SingleStrongReviewer",
        "classify",
        "evaluate",
        "run",
        "normalize",
        "prepare",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root in allowed_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").lstrip(".")
            assert module in allowed_modules, module
            for alias in node.names:
                assert alias.name not in forbidden_identifiers, alias.name
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_identifiers, node.id
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_identifiers, node.attr
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
    assert "subprocess" not in source
    assert "environ" not in source
    assert "datetime" not in source


def test_judgment_field_order_v1_frozen_extra_forbid():
    assert list(RulesPredictionJudgment.model_fields) == [
        "schema_version",
        "prediction_ref",
        "matched_issue_ids",
        "support_status",
        "readability_status",
        "readability_score",
        "assessor_ref",
    ]
    assert list(SingleFindingJudgment.model_fields) == [
        "schema_version",
        "finding_id",
        "matched_issue_ids",
        "support_status",
        "readability_status",
        "readability_score",
        "assessor_ref",
    ]
    for model_type in (RulesPredictionJudgment, SingleFindingJudgment):
        assert model_type.model_config["frozen"] is True
        assert model_type.model_config["extra"] == "forbid"
        assert RulesPredictionJudgment(schema_version="v1", prediction_ref="risk:A", support_status="supported", readability_status="unavailable").schema_version == "v1"


def test_rules_judgment_ref_grammar_and_bounds():
    valid = _rules_judgment("risk:AUTHORIZATION_CHANGE")
    assert valid.prediction_ref == "risk:AUTHORIZATION_CHANGE"
    assert _rules_judgment("gate:REQUIRED_REVIEWER_MISSING")
    for bad in (
        "risk:lower",
        "gate:",
        "risk:1BAD",
        "risk:A B",
        "reason:A",
        "risk:A\x00B",
        "risk:" + "A" * 300,
    ):
        with pytest.raises(ValidationError):
            RulesPredictionJudgment(
                schema_version="v1",
                prediction_ref=bad,
                support_status="supported",
                readability_status="unavailable",
            )
    with pytest.raises(ValidationError):
        RulesPredictionJudgment(
            schema_version="v1",
            prediction_ref="risk:A",
            support_status="supported",
            readability_status="unavailable",
            extra_field=True,
        )


def test_single_judgment_finding_id_bounds():
    assert _single_judgment("fnd_abc").finding_id == "fnd_abc"
    for bad in ("", "   ", "fnd\x00x", "f" * 300):
        with pytest.raises(ValidationError):
            SingleFindingJudgment(
                schema_version="v1",
                finding_id=bad,
                support_status="supported",
                readability_status="unavailable",
            )


def test_judgment_matched_issue_ids_canonical_limits():
    judgment = _rules_judgment(
        "risk:A", matched=("z-issue", "a-issue")
    )
    assert judgment.matched_issue_ids == ("a-issue", "z-issue")
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", matched=("dup", "dup"))
    with pytest.raises(ValidationError):
        RulesPredictionJudgment(
            schema_version="v1",
            prediction_ref="risk:A",
            matched_issue_ids=["a-issue"],
            support_status="supported",
            readability_status="unavailable",
        )
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", matched=("",))
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", matched=("bad\x00id",))
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", matched=("x" * 300,))
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", matched=tuple(f"i-{i}" for i in range(257)))
    loaded = RulesPredictionJudgment.model_validate_json(
        RulesPredictionJudgment(
            schema_version="v1",
            prediction_ref="risk:A",
            matched_issue_ids=("b", "a"),
            support_status="supported",
            readability_status="unavailable",
        ).model_dump_json()
    )
    assert loaded.matched_issue_ids == ("a", "b")


def test_judgment_readability_assessed_requires_score_and_assessor():
    for model_type, key in (
        (RulesPredictionJudgment, "prediction_ref"),
        (SingleFindingJudgment, "finding_id"),
    ):
        base = {
            "schema_version": "v1",
            key: "risk:A" if model_type is RulesPredictionJudgment else "fnd_1",
            "matched_issue_ids": (),
            "support_status": "supported",
            "readability_status": "assessed",
        }
        with pytest.raises(ValidationError):
            model_type.model_validate({**base, "readability_score": None, "assessor_ref": None})
        with pytest.raises(ValidationError):
            model_type.model_validate({**base, "readability_score": 0, "assessor_ref": "human"})
        with pytest.raises(ValidationError):
            model_type.model_validate({**base, "readability_score": 6, "assessor_ref": "human"})
        with pytest.raises(ValidationError):
            model_type.model_validate({**base, "readability_score": 4, "assessor_ref": None})
        with pytest.raises(ValidationError):
            model_type.model_validate({**base, "readability_score": 4, "assessor_ref": "   "})
        valid = model_type.model_validate(
            {**base, "readability_score": 4, "assessor_ref": "human:lead"}
        )
        assert valid.readability_score == 4
        assert valid.assessor_ref == "human:lead"


def test_judgment_readability_unavailable_requires_none():
    for model_type, key in (
        (RulesPredictionJudgment, "prediction_ref"),
        (SingleFindingJudgment, "finding_id"),
    ):
        base = {
            "schema_version": "v1",
            key: "risk:A" if model_type is RulesPredictionJudgment else "fnd_1",
            "matched_issue_ids": (),
            "support_status": "supported",
            "readability_status": "unavailable",
        }
        valid = model_type.model_validate({**base, "readability_score": None, "assessor_ref": None})
        assert valid.readability_score is None
        assert valid.assessor_ref is None
        for score, assessor in ((4, None), (None, "human"), (4, "human")):
            with pytest.raises(ValidationError):
                model_type.model_validate(
                    {**base, "readability_score": score, "assessor_ref": assessor}
                )


@pytest.mark.parametrize("score", [True, 4.0, "4"])
def test_judgment_rejects_bool_float_numeric_string_score(score):
    with pytest.raises(ValidationError):
        RulesPredictionJudgment(
            schema_version="v1",
            prediction_ref="risk:A",
            support_status="supported",
            readability_status="assessed",
            readability_score=score,
            assessor_ref="human",
        )


def test_judgment_support_independent_of_matching():
    assert _rules_judgment("risk:A", matched=("scope-creep",), support="unsupported")
    assert _rules_judgment("risk:A", matched=(), support="supported")
    with pytest.raises(ValidationError):
        _rules_judgment("risk:A", support="maybe")


def test_judgment_json_round_trip():
    for model_type, key in (
        (RulesPredictionJudgment, "prediction_ref"),
        (SingleFindingJudgment, "finding_id"),
    ):
        model = model_type.model_validate(
            {
                "schema_version": "v1",
                key: "risk:A" if model_type is RulesPredictionJudgment else "fnd_1",
                "matched_issue_ids": ("a", "b"),
                "support_status": "unsupported",
                "readability_status": "assessed",
                "readability_score": 4,
                "assessor_ref": "human:lead",
            }
        )
        payload = model.model_dump_json()
        restored = model_type.model_validate_json(payload)
        assert restored == model
        assert restored.model_dump_json() == payload


def test_metrics_field_order_v1_frozen_extra_forbid():
    assert list(BaselineArmMetrics.model_fields) == [
        "schema_version",
        "arm",
        "prediction_count",
        "true_positive_predictions",
        "false_positive_predictions",
        "precision_status",
        "precision",
        "gold_issue_count",
        "detected_gold_issue_count",
        "recall_status",
        "recall",
        "unsupported_count",
        "questions_count",
        "actual_outcome",
        "gold_outcome",
        "outcome_match",
        "false_block",
        "false_pass",
        "usage_status",
        "input_tokens",
        "output_tokens",
        "cost_status",
        "cost_usd",
        "latency_status",
        "latency_ms",
        "readability_status",
        "readability_score",
    ]
    assert BaselineArmMetrics.model_config["frozen"] is True
    assert BaselineArmMetrics.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        _metrics(extra_field=1)


def test_metrics_exact_int_counts_and_nonnegative():
    metrics = _metrics(
        prediction_count=1,
        true_positive_predictions=1,
        false_positive_predictions=0,
        precision_status="available",
        precision=1.0,
        gold_issue_count=1,
        detected_gold_issue_count=1,
        recall_status="available",
        recall=1.0,
    )
    assert metrics.prediction_count == 1
    for bad in (True, 1.0, "1", -1):
        with pytest.raises(ValidationError):
            _metrics(prediction_count=bad)
        with pytest.raises(ValidationError):
            _metrics(gold_issue_count=bad)
        with pytest.raises(ValidationError):
            _metrics(latency_ms=bad)


def test_metrics_precision_recall_status_value_pairing():
    with pytest.raises(ValidationError):
        _metrics(precision_status="available", precision=None)
    with pytest.raises(ValidationError):
        _metrics(precision_status="unavailable", precision=0.0)
    with pytest.raises(ValidationError):
        _metrics(recall_status="available", recall=None)
    with pytest.raises(ValidationError):
        _metrics(recall_status="unavailable", recall=1.0)
    with pytest.raises(ValidationError):
        _metrics(precision_status="available", precision=1.5)
    with pytest.raises(ValidationError):
        _metrics(precision_status="available", precision=float("nan"))
    with pytest.raises(ValidationError):
        _metrics(recall_status="available", recall="0.5")
    valid = _metrics(
        precision_status="available",
        precision=1,
        recall_status="available",
        recall=0.5,
    )
    assert valid.precision == 1.0
    assert valid.recall == 0.5


def test_metrics_usage_cost_pairings():
    _metrics(usage_status="measured", cost_status="measured", cost_usd=0.1)
    _metrics(
        usage_status="unavailable",
        input_tokens=None,
        output_tokens=None,
        cost_status="unavailable",
        cost_usd=None,
    )
    _metrics(
        arm="rules_only",
        usage_status="not_applicable",
        input_tokens=None,
        output_tokens=None,
        cost_status="not_applicable",
        cost_usd=0.0,
        latency_status="unavailable",
        latency_ms=None,
    )
    with pytest.raises(ValidationError):
        _metrics(usage_status="measured", input_tokens=None)
    with pytest.raises(ValidationError):
        _metrics(
            usage_status="unavailable",
            input_tokens=None,
            output_tokens=None,
            cost_status="unavailable",
            cost_usd=0.0,
        )
    with pytest.raises(ValidationError):
        _metrics(usage_status="unavailable", cost_status="measured", cost_usd=0.0)
    with pytest.raises(ValidationError):
        _metrics(cost_usd=-0.01)
    with pytest.raises(ValidationError):
        _metrics(cost_usd=float("inf"))


def test_metrics_arm_specific_invariants():
    with pytest.raises(ValidationError):
        _metrics(arm="rules_only", usage_status="measured")
    with pytest.raises(ValidationError):
        _metrics(arm="rules_only", cost_usd=0.5)
    with pytest.raises(ValidationError):
        _metrics(arm="rules_only", latency_ms=1)
    with pytest.raises(ValidationError):
        _metrics(arm="single_strong_reviewer", latency_status="unavailable", latency_ms=None)
    with pytest.raises(ValidationError):
        _metrics(arm="single_strong_reviewer", usage_status="not_applicable")
    with pytest.raises(ValidationError):
        _metrics(arm="other")


def test_metrics_readability_pairing():
    with pytest.raises(ValidationError):
        _metrics(readability_status="assessed", readability_score=None)
    with pytest.raises(ValidationError):
        _metrics(readability_status="unavailable", readability_score=4.0)
    with pytest.raises(ValidationError):
        _metrics(readability_status="assessed", readability_score=0.5)
    with pytest.raises(ValidationError):
        _metrics(readability_status="assessed", readability_score=5.5)
    with pytest.raises(ValidationError):
        _metrics(readability_status="assessed", readability_score="4")
    valid = _metrics(readability_status="assessed", readability_score=4.5)
    assert valid.readability_score == 4.5


def test_metrics_json_round_trip():
    metrics = _metrics()
    payload = metrics.model_dump_json()
    restored = BaselineArmMetrics.model_validate_json(payload)
    assert restored == metrics
    assert restored.model_dump_json() == payload


def test_input_field_order_and_exact_nested_types(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    assert list(BaselineReportInput.model_fields) == [
        "schema_version",
        "rules_result",
        "single_result",
        "single_policy_result",
        "rules_judgments",
        "single_judgments",
    ]
    assert BaselineReportInput.model_config["frozen"] is True
    assert BaselineReportInput.model_config["extra"] == "forbid"
    data = {
        "schema_version": "v1",
        "rules_result": rules_result,
        "single_result": single_result,
        "single_policy_result": single_policy_result,
        "rules_judgments": _default_rules_judgments(rules_result),
        "single_judgments": _default_single_judgments(single_result),
    }
    for field in ("rules_result", "single_result", "single_policy_result"):
        bad = dict(data)
        bad[field] = data[field].model_dump(mode="json")
        with pytest.raises(ValidationError):
            BaselineReportInput.model_validate(bad)
    bad = dict(data)
    bad["rules_judgments"] = list(bad["rules_judgments"])
    with pytest.raises(ValidationError):
        BaselineReportInput.model_validate(bad)
    bad = dict(data)
    bad["single_judgments"] = list(bad["single_judgments"])
    with pytest.raises(ValidationError):
        BaselineReportInput.model_validate(bad)


def test_input_judgment_tuple_boundaries_and_json_mode(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    value = _input(stack, rules_result, single_result, single_policy_result)
    payload = value.model_dump_json()
    restored = BaselineReportInput.model_validate_json(payload)
    assert restored == value
    assert restored.model_dump_json() == payload
    json_data = json.loads(payload)
    assert type(json_data["rules_judgments"]) is list
    assert type(json_data["single_judgments"]) is list
    assert BaselineReportInput.model_validate_json(json.dumps(json_data))


def test_input_canonicalizes_judgment_order_and_rejects_duplicates(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = _default_rules_judgments(rules_result)
    single_judgments = _default_single_judgments(single_result)
    permuted = BaselineReportInput.model_validate(
        {
            "schema_version": "v1",
            "rules_result": rules_result,
            "single_result": single_result,
            "single_policy_result": single_policy_result,
            "rules_judgments": tuple(reversed(rules_judgments)),
            "single_judgments": tuple(reversed(single_judgments)),
        }
    )
    canonical = _input(stack, rules_result, single_result, single_policy_result)
    assert permuted.rules_judgments == canonical.rules_judgments
    assert permuted.single_judgments == canonical.single_judgments
    duplicate = _input(stack, rules_result, single_result, single_policy_result)
    with pytest.raises(ValidationError):
        BaselineReportInput.model_validate(
            {
                "schema_version": "v1",
                "rules_result": rules_result,
                "single_result": single_result,
                "single_policy_result": single_policy_result,
                "rules_judgments": (
                    rules_judgments[0],
                    rules_judgments[0],
                ),
                "single_judgments": single_judgments,
            }
        )


def test_input_judgment_count_limit_512(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    too_many = tuple(
        _rules_judgment(f"risk:C{index}") for index in range(513)
    )
    with pytest.raises(ValidationError):
        BaselineReportInput.model_validate(
            {
                "schema_version": "v1",
                "rules_result": rules_result,
                "single_result": single_result,
                "single_policy_result": single_policy_result,
                "rules_judgments": too_many,
                "single_judgments": _default_single_judgments(single_result),
            }
        )


def test_input_binding_cross_subject_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    other_stack = _stack(_digest("e"))
    other_single = _single_result(other_stack)
    other_policy = _policy_result(other_stack, other_single)
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            other_single,
            other_policy,
        )


def test_input_policy_subject_risk_mismatch_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    other_subject = policy_tests._subject(
        stack["subject_digest"], change_id="change-2"
    )
    mismatched = _policy_result(
        stack, single_result, subject=other_subject
    )
    with pytest.raises(ValidationError):
        _input(stack, rules_result, single_result, mismatched)
    other_risk = policy_tests._risk_result(
        stack["subject_digest"],
        manifest=policy_tests._manifest(stack["subject_digest"]),
    )
    mismatched = _policy_result(
        stack, single_result, risk_result=other_risk
    )
    with pytest.raises(ValidationError):
        _input(stack, rules_result, single_result, mismatched)


def test_input_rules_and_single_reviewer_risk_mismatch_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    other_stack = _stack(stack["subject_digest"])
    other_risk = policy_tests._risk_result(
        stack["subject_digest"],
        manifest=other_stack["manifest"],
        declarations=policy_tests._declarations(changed_lines_total=10),
    )
    assert other_risk != rules_result.risk_result
    other_stack["risk_result"] = other_risk
    other_stack["reviewer_input"] = SingleReviewerInput(
        schema_version="v1",
        subject=other_stack["subject"],
        risk_result=other_risk,
        contexts=other_stack["contexts"],
        evaluated_at=FIXED_TIME,
    )
    other_single = _single_result(other_stack)
    other_policy = _policy_result(other_stack, other_single)
    assert other_single.input.reviewer_input.risk_result == other_risk
    assert other_policy.input.risk_result == other_risk
    with pytest.raises(ValidationError):
        _input(stack, rules_result, other_single, other_policy)


def test_input_policy_findings_mismatch_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    mismatched = _policy_result(stack, single_result, findings=())
    with pytest.raises(ValidationError):
        _input(stack, rules_result, single_result, mismatched)


def test_input_missing_receipt_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    missing = _policy_result(
        stack, single_result, execution_receipts=()
    )
    with pytest.raises(ValidationError):
        _input(stack, rules_result, single_result, missing)


def test_input_judgment_missing_extra_unknown_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = _default_rules_judgments(rules_result)
    single_judgments = _default_single_judgments(single_result)
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            rules_judgments=(),
        )
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            rules_judgments=rules_judgments
            + (_rules_judgment("risk:EXTRA_CODE"),),
        )
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            single_judgments=(
                single_judgments[0],
                _single_judgment("fnd_ghost"),
            ),
        )
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            single_judgments=(),
        )


def test_input_unknown_matched_issue_id_rejected(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = _default_rules_judgments(rules_result)
    with pytest.raises(ValidationError):
        _input(
            stack,
            rules_result,
            single_result,
            single_policy_result,
            rules_judgments=tuple(
                judgment.model_copy(
                    update={"matched_issue_ids": ("ghost-issue",)}
                )
                for judgment in rules_judgments
            ),
        )


def test_builder_rejects_non_exact_input():
    with pytest.raises(TypeError):
        BaselineReportBuilder.build({})


def test_builder_scope_creep_metrics(report):
    rules_metrics = report.rules_metrics
    single_metrics = report.single_metrics
    assert rules_metrics.arm == "rules_only"
    assert rules_metrics.prediction_count == 1
    assert rules_metrics.true_positive_predictions == 0
    assert rules_metrics.false_positive_predictions == 1
    assert rules_metrics.precision_status == "available"
    assert rules_metrics.precision == 0.0
    assert rules_metrics.gold_issue_count == 1
    assert rules_metrics.detected_gold_issue_count == 0
    assert rules_metrics.recall_status == "available"
    assert rules_metrics.recall == 0.0
    assert rules_metrics.unsupported_count == 1
    assert rules_metrics.questions_count == 0
    assert rules_metrics.actual_outcome == "BLOCKED"
    assert rules_metrics.gold_outcome == "BLOCKED"
    assert rules_metrics.outcome_match is True
    assert rules_metrics.false_block is False
    assert rules_metrics.false_pass is False

    assert single_metrics.arm == "single_strong_reviewer"
    assert single_metrics.prediction_count == 2
    assert single_metrics.true_positive_predictions == 2
    assert single_metrics.false_positive_predictions == 0
    assert single_metrics.precision_status == "available"
    assert single_metrics.precision == 1.0
    assert single_metrics.gold_issue_count == 1
    assert single_metrics.detected_gold_issue_count == 1
    assert single_metrics.recall_status == "available"
    assert single_metrics.recall == 1.0
    assert single_metrics.unsupported_count == 0
    assert single_metrics.questions_count == 0
    assert single_metrics.actual_outcome == "PASS"
    assert single_metrics.gold_outcome == "BLOCKED"
    assert single_metrics.outcome_match is False
    assert single_metrics.false_block is False
    assert single_metrics.false_pass is True


def test_report_field_order_and_literals(report):
    assert list(BaselineReport.model_fields) == [
        "schema_version",
        "report_id",
        "input",
        "subject_digest",
        "sample_size",
        "minimum_promotion_sample_size",
        "promotion_eligible",
        "rules_metrics",
        "single_metrics",
        "limitation_codes",
        "conclusion_codes",
        "report_digest",
    ]
    assert BaselineReport.model_config["frozen"] is True
    assert BaselineReport.model_config["extra"] == "forbid"
    assert report.sample_size == 1
    assert report.minimum_promotion_sample_size == 12
    assert report.promotion_eligible is False
    assert report.conclusion_codes == ("no_promotion_claim",)
    assert report.report_id.startswith("baseline_")
    assert len(report.report_id) == len("baseline_") + 32
    assert report.report_id[9:].lower() == report.report_id[9:]
    assert report.subject_digest.startswith("sha256:")
    assert len(report.subject_digest) == 7 + 64
    assert report.report_digest.startswith("sha256:")
    assert len(report.report_digest) == 7 + 64


def test_report_deterministic_bytes_digest_id_and_permutation(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = _default_rules_judgments(rules_result)
    single_judgments = _default_single_judgments(single_result)
    first = _build(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        rules_judgments=rules_judgments,
        single_judgments=single_judgments,
    )
    second = _build(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        rules_judgments=tuple(reversed(rules_judgments)),
        single_judgments=tuple(reversed(single_judgments)),
    )
    assert first.model_dump_json() == second.model_dump_json()
    assert first.report_digest == second.report_digest
    assert first.report_id == second.report_id
    data = json.loads(first.model_dump_json())
    body = {
        key: value
        for key, value in data.items()
        if key not in ("report_id", "report_digest")
    }
    expected_digest = _sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    assert first.report_digest == expected_digest
    expected_id = "baseline_" + hashlib.sha256(
        (first.subject_digest + first.report_digest).encode("utf-8")
    ).hexdigest()[:32]
    assert first.report_id == expected_id


def test_report_json_round_trip_bytes(report):
    payload = report.model_dump_json()
    assert type(payload) is str
    restored = BaselineReport.model_validate_json(payload)
    assert restored == report
    assert restored.model_dump_json() == payload
    restored = BaselineReport.model_validate_json(payload.encode("utf-8"))
    assert restored == report


def test_rules_blocked_gold_blocked_not_false_block(report):
    assert report.rules_metrics.actual_outcome == "BLOCKED"
    assert report.rules_metrics.gold_outcome == "BLOCKED"
    assert report.rules_metrics.outcome_match is True
    assert report.rules_metrics.false_block is False
    assert report.rules_metrics.false_pass is False


def test_single_pass_gold_blocked_false_pass(report):
    assert report.single_metrics.actual_outcome == "PASS"
    assert report.single_metrics.gold_outcome == "BLOCKED"
    assert report.single_metrics.outcome_match is False
    assert report.single_metrics.false_pass is True


def test_usage_unavailable_never_zero_and_rules_not_applicable(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    unavailable = _single_result(
        stack,
        invocation_overrides={
            "usage_status": "unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        },
    )
    policy = _policy_result(stack, unavailable)
    value = _input(stack, rules_result, unavailable, policy)
    result = BaselineReportBuilder.build(value)
    assert result.rules_metrics.usage_status == "not_applicable"
    assert result.rules_metrics.input_tokens is None
    assert result.rules_metrics.output_tokens is None
    assert result.rules_metrics.cost_status == "not_applicable"
    assert result.rules_metrics.cost_usd == 0.0
    assert result.single_metrics.usage_status == "unavailable"
    assert result.single_metrics.input_tokens is None
    assert result.single_metrics.output_tokens is None
    assert result.single_metrics.cost_status == "unavailable"
    assert result.single_metrics.cost_usd is None
    assert result.single_metrics.cost_usd != 0
    assert "single_usage_unavailable" in result.limitation_codes


def test_latency_measured_exact_invocation(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    assert single_result.input.invocation.latency_ms == 3_600_000
    value = _input(stack, rules_result, single_result, single_policy_result)
    result = BaselineReportBuilder.build(value)
    assert result.single_metrics.latency_status == "measured"
    assert result.single_metrics.latency_ms == 3_600_000
    assert result.rules_metrics.latency_status == "unavailable"
    assert result.rules_metrics.latency_ms is None


def test_readability_assessed_averages_correctly(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    single_judgments = tuple(
        _single_judgment(
            finding.finding_id,
            score=score,
            assessor=f"human:{index}",
        )
        for index, (finding, score) in enumerate(
            zip(single_result.findings, (5, 4, 3))
        )
    )
    value = _input(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        single_judgments=single_judgments,
    )
    result = BaselineReportBuilder.build(value)
    assert result.rules_metrics.readability_status == "assessed"
    assert result.rules_metrics.readability_score == 5.0
    assert result.single_metrics.readability_status == "assessed"
    assert result.single_metrics.readability_score == 4.0
    assert "readability_unavailable" not in result.limitation_codes


def test_readability_unavailable_no_fabrication(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = tuple(
        _rules_judgment(ref, readability="unavailable", score=None, assessor=None)
        for ref in rules_result.observed_reason_refs
    )
    single_judgments = tuple(
        _single_judgment(
            finding.finding_id,
            readability="unavailable",
            score=None,
            assessor=None,
        )
        for finding in single_result.findings
    )
    value = _input(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        rules_judgments=rules_judgments,
        single_judgments=single_judgments,
    )
    result = BaselineReportBuilder.build(value)
    assert result.rules_metrics.readability_status == "unavailable"
    assert result.rules_metrics.readability_score is None
    assert result.single_metrics.readability_status == "unavailable"
    assert result.single_metrics.readability_score is None
    assert "readability_unavailable" in result.limitation_codes


def test_zero_judgments_readability_unavailable(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    empty_single = _single_result(stack, findings=())
    policy = _policy_result(stack, empty_single)
    value = _input(
        stack,
        rules_result,
        empty_single,
        policy,
        single_judgments=(),
    )
    result = BaselineReportBuilder.build(value)
    assert result.single_metrics.readability_status == "unavailable"
    assert result.single_metrics.readability_score is None
    assert result.single_metrics.prediction_count == 0
    assert result.single_metrics.precision_status == "unavailable"
    assert result.single_metrics.precision is None


def test_zero_gold_and_zero_prediction_denominators_unavailable(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    zero_gold = _rules_result(stack, expectations=())
    info_only = _single_result(
        stack, findings=(_finding_draft(severity="info", claim="note"),)
    )
    policy = _policy_result(stack, info_only)
    value = _input(
        stack,
        zero_gold,
        info_only,
        policy,
        single_judgments=_default_single_judgments(info_only, matched=()),
    )
    result = BaselineReportBuilder.build(value)
    assert result.rules_metrics.gold_issue_count == 0
    assert result.rules_metrics.recall_status == "unavailable"
    assert result.rules_metrics.recall is None
    assert result.rules_metrics.precision_status == "available"
    assert result.rules_metrics.precision == 0.0
    assert result.single_metrics.gold_issue_count == 0
    assert result.single_metrics.recall_status == "unavailable"
    assert result.single_metrics.recall is None
    assert result.single_metrics.prediction_count == 0
    assert result.single_metrics.precision_status == "unavailable"
    assert result.single_metrics.precision is None


def test_unsupported_counts(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    rules_judgments = _default_rules_judgments(rules_result, support="unsupported")
    single_judgments = tuple(
        _single_judgment(finding.finding_id, support="unsupported")
        for index, finding in enumerate(single_result.findings)
        if index == 0
    ) + tuple(
        _single_judgment(finding.finding_id, support="supported")
        for index, finding in enumerate(single_result.findings)
        if index != 0
    )
    value = _input(
        stack,
        rules_result,
        single_result,
        single_policy_result,
        rules_judgments=rules_judgments,
        single_judgments=single_judgments,
    )
    result = BaselineReportBuilder.build(value)
    assert result.rules_metrics.unsupported_count == 1
    assert result.single_metrics.unsupported_count == 1


def test_limitation_codes_mandatory_and_conditional(report):
    assert report.limitation_codes[0] == "single_case_only"
    assert "rules_latency_unavailable" in report.limitation_codes
    assert "policy_advisory_disagreement" in report.limitation_codes
    assert "readability_unavailable" not in report.limitation_codes
    assert "single_usage_unavailable" not in report.limitation_codes


def test_policy_advisory_disagreement_absent_without_high_open_finding(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    safe_single = _single_result(
        stack,
        findings=(
            _finding_draft(severity="medium", claim="scope A"),
            _finding_draft(severity="info", claim="note"),
        ),
    )
    policy = _policy_result(stack, safe_single)
    value = _input(stack, rules_result, safe_single, policy)
    result = BaselineReportBuilder.build(value)
    assert "policy_advisory_disagreement" not in result.limitation_codes


def test_conclusion_and_promotion_boundary_fixed(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    value = _input(stack, rules_result, single_result, single_policy_result)
    result = BaselineReportBuilder.build(value)
    assert result.conclusion_codes == ("no_promotion_claim",)
    assert result.sample_size == 1
    assert result.minimum_promotion_sample_size == 12
    assert result.promotion_eligible is False


def _forged_metrics(base, updates):
    return base.model_copy(update=updates)


RULES_METRIC_FORGES = {
    "prediction_count": {"prediction_count": 2},
    "true_positive_predictions": {"true_positive_predictions": 1},
    "false_positive_predictions": {"false_positive_predictions": 2},
    "precision_status": {"precision_status": "unavailable", "precision": None},
    "precision": {"precision": 0.5},
    "gold_issue_count": {"gold_issue_count": 2},
    "detected_gold_issue_count": {"detected_gold_issue_count": 1},
    "recall_status": {"recall_status": "unavailable", "recall": None},
    "recall": {"recall": 1.0},
    "unsupported_count": {"unsupported_count": 0},
    "questions_count": {"questions_count": 1},
    "actual_outcome": {"actual_outcome": "PASS"},
    "gold_outcome": {"gold_outcome": "PASS"},
    "outcome_match": {"outcome_match": False},
    "false_block": {"false_block": True},
    "false_pass": {"false_pass": True},
    "readability_score": {"readability_score": 4.5},
    "usage_status_measured": {
        "usage_status": "measured",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_status": "measured",
        "cost_usd": 0.1,
    },
    "cost_usd_nonzero": {"cost_usd": 0.5},
    "latency_measured": {"latency_status": "measured", "latency_ms": 5},
}


SINGLE_METRIC_FORGES = {
    "prediction_count": {"prediction_count": 3},
    "true_positive_predictions": {"true_positive_predictions": 1},
    "false_positive_predictions": {"false_positive_predictions": 1},
    "precision_status": {"precision_status": "unavailable", "precision": None},
    "precision": {"precision": 0.5},
    "gold_issue_count": {"gold_issue_count": 2},
    "detected_gold_issue_count": {"detected_gold_issue_count": 2},
    "recall_status": {"recall_status": "unavailable", "recall": None},
    "recall": {"recall": 0.5},
    "unsupported_count": {"unsupported_count": 1},
    "questions_count": {"questions_count": 1},
    "actual_outcome": {"actual_outcome": "BLOCKED"},
    "gold_outcome": {"gold_outcome": "PASS"},
    "outcome_match": {"outcome_match": True},
    "false_block": {"false_block": True},
    "false_pass": {"false_pass": False},
    "usage_status_unavailable": {
        "usage_status": "unavailable",
        "input_tokens": None,
        "output_tokens": None,
        "cost_status": "unavailable",
        "cost_usd": None,
    },
    "input_tokens": {"input_tokens": 121},
    "output_tokens": {"output_tokens": 81},
    "cost_usd": {"cost_usd": 0.5},
    "latency_ms": {"latency_ms": 1},
    "readability_status": {"readability_status": "unavailable", "readability_score": None},
    "readability_score": {"readability_score": 4.5},
}


@pytest.mark.parametrize(
    "name,mutator",
    list(RULES_METRIC_FORGES.items()),
    ids=list(RULES_METRIC_FORGES),
)
def test_report_rejects_rules_metric_forgery(report, name, mutator):
    forged = _forged_metrics(report.rules_metrics, mutator)
    candidate = report.model_copy(update={"rules_metrics": forged})
    with pytest.raises(ValidationError):
        BaselineReport.model_validate_json(candidate.model_dump_json())


@pytest.mark.parametrize(
    "name,mutator",
    list(SINGLE_METRIC_FORGES.items()),
    ids=list(SINGLE_METRIC_FORGES),
)
def test_report_rejects_single_metric_forgery(report, name, mutator):
    forged = _forged_metrics(report.single_metrics, mutator)
    candidate = report.model_copy(update={"single_metrics": forged})
    with pytest.raises(ValidationError):
        BaselineReport.model_validate_json(candidate.model_dump_json())


def test_report_rejects_code_subject_digest_id_forgery(report):
    for forged in (
        report.model_copy(update={"limitation_codes": ("single_case_only",)}),
        report.model_copy(
            update={"limitation_codes": ("single_case_only", "readability_unavailable")}
        ),
        report.model_copy(
            update={"conclusion_codes": ("no_promotion_claim", "extra")}
        ),
        report.model_copy(update={"subject_digest": _digest("0")}),
        report.model_copy(update={"report_digest": _digest("0")}),
        report.model_copy(update={"report_id": "baseline_" + "0" * 32}),
    ):
        with pytest.raises(ValidationError):
            BaselineReport.model_validate_json(forged.model_dump_json())


def test_report_rejects_literal_boundary_forgery(report):
    for updates in (
        {"sample_size": 2},
        {"minimum_promotion_sample_size": 11},
        {"promotion_eligible": True},
    ):
        with pytest.raises(ValidationError):
            BaselineReport.model_validate_json(
                report.model_copy(update=updates).model_dump_json()
            )


def test_report_synchronized_forgery_rejected(report):
    data = json.loads(report.model_dump_json())
    data["rules_metrics"]["precision"] = 0.5
    data["rules_metrics"]["true_positive_predictions"] = 1
    data["single_metrics"]["recall"] = 0.0
    data["single_metrics"]["detected_gold_issue_count"] = 0
    digest = _sha256(_canonical_body(data))
    data["report_digest"] = digest
    data["report_id"] = "baseline_" + hashlib.sha256(
        (data["subject_digest"] + digest).encode("utf-8")
    ).hexdigest()[:32]
    with pytest.raises(ValidationError):
        BaselineReport.model_validate_json(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )


def test_report_validator_rederives_all_fields(report):
    rebuilt = BaselineReport.model_validate_json(report.model_dump_json())
    assert rebuilt == report
    assert rebuilt.model_dump_json() == report.model_dump_json()


def test_builder_is_stateless_and_deterministic(scenario):
    stack, rules_result, single_result, single_policy_result = scenario
    value = _input(stack, rules_result, single_result, single_policy_result)
    first = BaselineReportBuilder.build(value)
    second = BaselineReportBuilder.build(value)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.report_digest == second.report_digest
    assert first.report_id == second.report_id
