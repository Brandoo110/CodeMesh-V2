"""V2-P4-09 Council report focused tests.

The module under test is a pure, deterministic first-pass offline
Single-vs-Multi fixture report for the P4 Gate: no I/O, model/provider/tool
execution, Gate execution, clock/env/random/network/subprocess access, or
persistence. ``default_topology`` is always ``single_strong_reviewer`` and
the report never emits promoted/default-Multi/PASS/approval/Gate success.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    CouncilArmSummary,
    CouncilComparisonFixture,
    CouncilPromotionAssessment,
    CouncilReport,
    CouncilReportBuilder,
)
from assurance import council_report as council_report_module


CATEGORIES = ("intent", "architecture", "operability")
RISKS = ("low", "medium", "high", "critical")
FIXTURE_ID_RE = re.compile(r"council_fixture_[0-9a-f]{32}\Z")
REPORT_ID_RE = re.compile(r"council_report_[0-9a-f]{32}\Z")
SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _fixture_values(
    i,
    *,
    category=None,
    risk=None,
    gold=("a", "b"),
    single_detected=("a", "fp"),
    multi_detected=("a", "b"),
    single_unsupported=1,
    multi_unsupported=0,
    single_false_block=False,
    multi_false_block=False,
    single_seconds=120.0,
    multi_seconds=80.0,
    single_cost=1.0,
    multi_cost=2.0,
    blocking_count=1,
    blocking_valid=1,
):
    return {
        "schema_version": "v1",
        "evidence_level": "synthetic_offline_fixture",
        "category": CATEGORIES[i % 3] if category is None else category,
        "risk_level": RISKS[i % 4] if risk is None else risk,
        "subject_digest": "sha256:" + f"{i + 1:064x}",
        "gold_issue_ids": tuple(f"{item}-{i}" for item in gold),
        "single_detected_issue_ids": tuple(
            f"{item}-{i}" for item in single_detected
        ),
        "multi_detected_issue_ids": tuple(
            f"{item}-{i}" for item in multi_detected
        ),
        "single_unsupported_claims": single_unsupported,
        "multi_unsupported_claims": multi_unsupported,
        "single_false_block": single_false_block,
        "multi_false_block": multi_false_block,
        "multi_blocking_count": blocking_count,
        "multi_blocking_with_valid_evidence": blocking_valid,
        "single_human_review_seconds": single_seconds,
        "multi_human_review_seconds": multi_seconds,
        "single_cost_usd": single_cost,
        "multi_cost_usd": multi_cost,
        "single_report_ref": f"report-{i}",
        "council_receipt_ref": f"receipt-{i}",
        "evaluator_ref": f"eval-{i}",
    }


def _fixture(i, **overrides):
    return CouncilComparisonFixture(**_fixture_values(i, **overrides))


def _eight_fixtures():
    return tuple(
        _fixture(
            i,
            multi_detected=("a", "b") if i % 2 == 0 else ("a", "fp"),
            single_unsupported=1 if i < 4 else 0,
            multi_unsupported=1 if i < 2 else 0,
            single_false_block=i == 0,
            multi_false_block=i == 1,
        )
        for i in range(8)
    )


def _twelve_threshold_met():
    return tuple(
        _fixture(
            i,
            multi_detected=("a", "b"),
            single_unsupported=1,
            multi_unsupported=0,
            multi_seconds=60.0,
        )
        for i in range(12)
    )


def _twelve_thresholds_not_met():
    return tuple(
        _fixture(
            i,
            multi_detected=("a", "fp"),
            single_unsupported=1,
            multi_unsupported=1,
            multi_seconds=120.0,
        )
        for i in range(12)
    )


def test_eight_fixture_insufficient_sample_report():
    fixtures = _eight_fixtures()
    assert len(fixtures) == 8
    report = CouncilReportBuilder.build(fixtures)

    assert report.sample_size == 8
    assert report.minimum_formal_sample_size == 12
    assert report.category_counts == (
        ("intent", 3),
        ("architecture", 3),
        ("operability", 2),
    )
    single, multi = report.single_metrics, report.multi_metrics
    assert single.total_gold_issues == multi.total_gold_issues == 16
    assert single.total_detected_gold_issues == 8
    assert multi.total_detected_gold_issues == 12
    assert single.macro_recall_percent == pytest.approx(50.0)
    assert multi.macro_recall_percent == pytest.approx(75.0)
    assert single.per_fixture_recall_percent == pytest.approx((50.0,) * 8)
    assert set(multi.per_fixture_recall_percent) == {50.0, 100.0}
    assert single.total_unsupported_claims == 4
    assert multi.total_unsupported_claims == 2
    assert single.false_block_count == multi.false_block_count == 1
    assert single.false_block_rate_percent == pytest.approx(12.5)
    assert multi.false_block_rate_percent == pytest.approx(12.5)
    assert multi.blocking_count == 8
    assert multi.blocking_with_valid_evidence == 8
    assert multi.blocking_evidence_coverage_percent == pytest.approx(100.0)
    assert single.blocking_count is None
    assert single.blocking_evidence_coverage_percent is None
    assert single.total_human_review_seconds == pytest.approx(960.0)
    assert multi.total_human_review_seconds == pytest.approx(640.0)
    assert single.total_cost_usd == pytest.approx(8.0)
    assert multi.total_cost_usd == pytest.approx(16.0)

    assessment = report.promotion_assessment
    assert assessment.macro_recall_gain_percent == pytest.approx(25.0)
    assert assessment.unsupported_claims_reduction_percent == pytest.approx(
        50.0
    )
    assert assessment.human_review_time_reduction_percent == pytest.approx(
        33.33333333333333
    )
    assert assessment.false_block_rate_delta_percent == pytest.approx(0.0)
    assert assessment.cost_ratio == pytest.approx(2.0)
    assert assessment.blocking_evidence_coverage_percent == pytest.approx(
        100.0
    )
    assert assessment.recall_gain_met
    assert assessment.unsupported_reduction_met
    assert assessment.time_reduction_met
    assert assessment.quality_gain_met
    assert assessment.false_block_guard_met
    assert assessment.evidence_guard_met
    assert assessment.cost_guard_met
    assert assessment.all_thresholds_met
    assert assessment.unmet_threshold_codes == ()

    assert report.status == "insufficient_sample"
    assert report.default_topology == "single_strong_reviewer"
    assert len(report.fixture_ids) == len(report.fixture_digests) == 8
    assert len(set(report.fixture_ids)) == 8
    assert all(FIXTURE_ID_RE.fullmatch(item) for item in report.fixture_ids)
    assert all(SHA_RE.fullmatch(item) for item in report.fixture_digests)
    assert REPORT_ID_RE.fullmatch(report.report_id)
    assert SHA_RE.fullmatch(report.report_digest)
    assert "promoted" not in report.model_dump_json().lower()


def test_twelve_fixture_threshold_met_candidate_for_p6_review():
    report = CouncilReportBuilder.build(_twelve_threshold_met())
    assert report.sample_size == 12
    assert report.category_counts == (
        ("intent", 4),
        ("architecture", 4),
        ("operability", 4),
    )
    assert report.single_metrics.macro_recall_percent == pytest.approx(50.0)
    assert report.multi_metrics.macro_recall_percent == pytest.approx(100.0)
    assert report.promotion_assessment.all_thresholds_met
    assert report.promotion_assessment.unmet_threshold_codes == ()
    assert report.status == "candidate_for_p6_review"
    assert report.default_topology == "single_strong_reviewer"


def test_twelve_fixture_thresholds_not_met():
    report = CouncilReportBuilder.build(_twelve_thresholds_not_met())
    assert report.sample_size == 12
    assessment = report.promotion_assessment
    assert not assessment.recall_gain_met
    assert not assessment.unsupported_reduction_met
    assert not assessment.time_reduction_met
    assert not assessment.quality_gain_met
    assert assessment.false_block_guard_met
    assert assessment.evidence_guard_met
    assert assessment.cost_guard_met
    assert not assessment.all_thresholds_met
    assert assessment.unmet_threshold_codes == (
        "recall_gain",
        "time_reduction",
        "unsupported_reduction",
    )
    assert assessment.recall_gain_shortfall_percent == pytest.approx(10.0)
    assert assessment.unsupported_reduction_shortfall_percent == pytest.approx(
        25.0
    )
    assert assessment.time_reduction_shortfall_percent == pytest.approx(25.0)
    assert report.status == "thresholds_not_met"
    assert report.default_topology == "single_strong_reviewer"


def test_fixture_derived_digest_and_id_deterministic_and_tamper_proof():
    fixture = _fixture(0)
    assert SHA_RE.fullmatch(fixture.fixture_digest)
    assert FIXTURE_ID_RE.fullmatch(fixture.fixture_id)

    payload = json.loads(fixture.model_dump_json())
    body = {
        key: value
        for key, value in payload.items()
        if key not in ("fixture_digest", "fixture_id")
    }
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert fixture.fixture_digest == expected_digest

    tampered_digest = dict(payload, fixture_digest="sha256:" + "0" * 64)
    with pytest.raises(ValidationError):
        CouncilComparisonFixture.model_validate_json(
            json.dumps(tampered_digest)
        )
    tampered_id = dict(payload, fixture_id="council_fixture_" + "0" * 32)
    with pytest.raises(ValidationError):
        CouncilComparisonFixture.model_validate_json(
            json.dumps(tampered_id)
        )


@pytest.mark.parametrize(
    ("patch",),
    [
        ({"single_unsupported_claims": True},),
        ({"multi_unsupported_claims": -1},),
        ({"multi_blocking_count": 1, "multi_blocking_with_valid_evidence": 2},),
        ({"gold_issue_ids": ()},),
        ({"subject_digest": "not-a-digest"},),
        ({"single_human_review_seconds": float("nan")},),
        ({"multi_cost_usd": float("-inf")},),
        ({"single_false_block": 1},),
        ({"single_report_ref": ""},),
        ({"category": "unknown"},),
        ({"risk_level": "unknown"},),
        ({"evidence_level": "runtime"},),
        ({"fixture_id": "council_fixture_zzzz"},),
        ({"unexpected_field": 1},),
    ],
)
def test_fixture_invalid_fields_rejected(patch):
    values = _fixture_values(0)
    values.update(patch)
    with pytest.raises(ValidationError):
        CouncilComparisonFixture.model_validate(values)


def test_canonical_issue_tuples_sorted_and_unique():
    values = _fixture_values(0)
    fixture = CouncilComparisonFixture.model_validate(
        dict(values, gold_issue_ids=("b-0", "a-0"))
    )
    assert fixture.gold_issue_ids == ("a-0", "b-0")
    with pytest.raises(ValidationError):
        CouncilComparisonFixture.model_validate(
            dict(values, gold_issue_ids=("a-0", "a-0"))
        )
    with pytest.raises(ValidationError):
        CouncilComparisonFixture.model_validate(
            dict(values, single_detected_issue_ids=["a-0", "fp-0"])
        )


def test_builder_tuple_and_duplicate_rules():
    eight = _eight_fixtures()
    with pytest.raises(TypeError):
        CouncilReportBuilder.build(list(eight))
    with pytest.raises(ValueError):
        CouncilReportBuilder.build(eight[:7])
    with pytest.raises(ValueError):
        CouncilReportBuilder.build(eight + (eight[0],))
    with pytest.raises(TypeError):
        CouncilReportBuilder.build((object(),) * 8)


def test_json_round_trip_fixture_and_report():
    fixture = _fixture(2)
    assert CouncilComparisonFixture.model_validate_json(
        fixture.model_dump_json()
    ) == fixture
    report = CouncilReportBuilder.build(_eight_fixtures())
    assert CouncilReport.model_validate_json(report.model_dump_json()) == report


def test_report_tampered_derived_fields_rejected():
    report = CouncilReportBuilder.build(_eight_fixtures())
    payload = json.loads(report.model_dump_json())
    tampered_cases = (
        {"report_id": "council_report_" + "0" * 32},
        {"report_digest": "sha256:" + "0" * 64},
        {"status": "candidate_for_p6_review"},
        {"sample_size": 12},
        {"category_counts": [["intent", 8], ["architecture", 0], ["operability", 0]]},
        {"fixture_ids": ["council_fixture_" + "0" * 32] + payload["fixture_ids"][1:]},
    )
    for patch in tampered_cases:
        with pytest.raises(ValidationError):
            CouncilReport.model_validate_json(json.dumps({**payload, **patch}))

    tampered_metrics = dict(payload["single_metrics"], macro_recall_percent=100.0)
    with pytest.raises(ValidationError):
        CouncilReport.model_validate_json(
            json.dumps({**payload, "single_metrics": tampered_metrics})
        )
    tampered_assessment = dict(
        payload["promotion_assessment"], quality_gain_met=False
    )
    with pytest.raises(ValidationError):
        CouncilReport.model_validate_json(
            json.dumps({**payload, "promotion_assessment": tampered_assessment})
        )
    tampered_fixture = dict(payload["fixtures"][0], fixture_digest="sha256:" + "0" * 64)
    with pytest.raises(ValidationError):
        CouncilReport.model_validate_json(
            json.dumps(
                {
                    **payload,
                    "fixtures": [tampered_fixture] + payload["fixtures"][1:],
                }
            )
        )
    with pytest.raises(ValidationError):
        CouncilReport.model_validate(
            report.model_copy(update={"status": "candidate_for_p6_review"})
        )


def test_frozen_and_extra_forbid():
    fixture = _fixture(0)
    with pytest.raises(ValidationError):
        fixture.category = "operability"
    report = CouncilReportBuilder.build(_eight_fixtures())
    with pytest.raises(ValidationError):
        report.default_topology = "multi_reviewer_council"


def test_package_exports():
    public_names = (
        "CouncilComparisonFixture",
        "CouncilArmSummary",
        "CouncilPromotionAssessment",
        "CouncilReport",
        "CouncilReportBuilder",
    )
    for name in public_names:
        assert getattr(assurance, name) is globals()[name]
    assert set(public_names) <= set(assurance.__all__)


def test_module_source_audit_no_io_or_runtime_access():
    source = Path(council_report_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "os.environ",
        "import os",
        "from os",
        "random.",
        "import random",
        "socket",
        "requests",
        "httpx",
        "datetime",
        "model_copy",
        "model_construct",
    ):
        assert forbidden not in source
    assert "single_strong_reviewer" in source
    assert "candidate_for_p6_review" in source


def test_public_model_basics():
    assert CouncilComparisonFixture.model_config["frozen"] is True
    assert CouncilComparisonFixture.model_config["extra"] == "forbid"
    assert CouncilArmSummary.model_config["frozen"] is True
    assert CouncilPromotionAssessment.model_config["frozen"] is True
    assert CouncilReport.model_config["frozen"] is True
    assert council_report_module.__all__ == (
        "CouncilComparisonFixture",
        "CouncilArmSummary",
        "CouncilPromotionAssessment",
        "CouncilReport",
        "CouncilReportBuilder",
    )
