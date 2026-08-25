"""V2-P4-09 offline Single-vs-Multi Council comparison report.

Pure assessed-fixture evidence for the P4 Gate; not a runtime evaluator and
not P6's formal evaluation. No I/O, model/provider/tool execution, Gate
execution, clock/env/random/network access, or persistence.
``default_topology`` is always ``single_strong_reviewer``; fewer than 12
fixtures is always ``insufficient_sample``; full threshold satisfaction can
only become ``candidate_for_p6_review``, never promotion/PASS/approval.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

_CATEGORIES = ("intent", "architecture", "operability")
_RISKS = ("low", "medium", "high", "critical")
_CATEGORY = Literal["intent", "architecture", "operability"]
_RISK = Literal["low", "medium", "high", "critical"]
_ARM = Literal["single_strong_reviewer", "multi_reviewer_council"]
_STATUS = Literal[
    "insufficient_sample", "thresholds_not_met", "candidate_for_p6_review"
]
_MIN_SAMPLE = 8
_MIN_FORMAL = 12
_RECALL_GAIN = 10.0
_UNSUPPORTED_REDUCTION = 25.0
_TIME_REDUCTION = 25.0
_FALSE_BLOCK_DELTA = 3.0
_COST_RATIO = 2.5
_MAX_IDS = 128
_MAX_REF = 256
_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FIXTURE_ID_RE = re.compile(r"council_fixture_[0-9a-f]{32}\Z")
_REPORT_ID_RE = re.compile(r"council_report_[0-9a-f]{32}\Z")
_CODES = (
    "recall_gain",
    "unsupported_reduction",
    "time_reduction",
    "false_block_guard",
    "evidence_guard",
    "cost_guard",
)
_CODE_SET = frozenset(_CODES)


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _id32(prefix, payload):
    return prefix + hashlib.sha256(_json_bytes(payload)).hexdigest()[:32]


def _exact_tuple(value, info, label):
    if info.mode == "json":
        if type(value) is not list:
            raise ValueError(f"{label} must be an array in JSON mode")
        return tuple(value)
    if type(value) is not tuple:
        raise ValueError(f"{label} must be an exact tuple at raw validation")
    return value


def _text(value, label):
    if type(value) is not str or "\x00" in value or not value.strip():
        raise ValueError(f"{label} must be a nonblank exact str without NUL")
    if len(value.encode("utf-8")) > _MAX_REF:
        raise ValueError(f"{label} must not exceed {_MAX_REF} UTF-8 bytes")
    return value


def _issue_ids(value, label, allow_empty=True):
    if len(value) > _MAX_IDS:
        raise ValueError(f"{label} must contain at most {_MAX_IDS} items")
    items = [_text(item, f"{label} item") for item in value]
    if len(set(items)) != len(items):
        raise ValueError(f"{label} items must be unique")
    if not allow_empty and not items:
        raise ValueError(f"{label} must not be empty")
    return tuple(sorted(items))


def _int(value, label):
    if isinstance(value, bool) or type(value) is not int or value < 0:
        raise ValueError(f"{label} must be an exact nonnegative int")
    return value


def _num(value, label, *, lo=0.0, hi=None):
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ValueError(f"{label} must be an exact finite number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be finite")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(f"{label} must be within [{lo}, {hi}]")
    return value


class CouncilComparisonFixture(BaseModel):
    """Immutable synthetic offline Single-vs-Multi fixture."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    evidence_level: Literal["synthetic_offline_fixture"] = "synthetic_offline_fixture"
    category: _CATEGORY
    risk_level: _RISK
    subject_digest: str
    gold_issue_ids: tuple[str, ...]
    single_detected_issue_ids: tuple[str, ...]
    multi_detected_issue_ids: tuple[str, ...]
    single_unsupported_claims: StrictInt
    multi_unsupported_claims: StrictInt
    single_false_block: StrictBool
    multi_false_block: StrictBool
    multi_blocking_count: StrictInt
    multi_blocking_with_valid_evidence: StrictInt
    single_human_review_seconds: float
    multi_human_review_seconds: float
    single_cost_usd: float
    multi_cost_usd: float
    single_report_ref: str
    council_receipt_ref: str
    evaluator_ref: str
    fixture_digest: str | None = Field(default=None, validate_default=True)
    fixture_id: str | None = Field(default=None, validate_default=True)

    @field_validator("subject_digest", mode="before")
    def _subject_digest(cls, value):
        if type(value) is not str or _SHA_RE.fullmatch(value) is None:
            raise ValueError("subject_digest must be sha256:<64 lowercase hex>")
        return value

    @field_validator("gold_issue_ids", "single_detected_issue_ids", "multi_detected_issue_ids", mode="before")
    def _issue_tuple(cls, value, info):
        return _exact_tuple(value, info, "issue id tuple")

    @field_validator("gold_issue_ids", "single_detected_issue_ids", "multi_detected_issue_ids")
    def _canonical_issues(cls, value, info):
        return _issue_ids(value, info.field_name, allow_empty=info.field_name != "gold_issue_ids")

    @field_validator("single_unsupported_claims", "multi_unsupported_claims", "multi_blocking_count", "multi_blocking_with_valid_evidence", mode="before")
    def _counts(cls, value, info):
        return _int(value, info.field_name)

    @field_validator("single_human_review_seconds", "multi_human_review_seconds", "single_cost_usd", "multi_cost_usd", mode="before")
    def _numbers(cls, value, info):
        return _num(value, info.field_name)

    @field_validator("single_report_ref", "council_receipt_ref", "evaluator_ref", mode="before")
    def _refs(cls, value, info):
        return _text(value, info.field_name)

    @field_validator("fixture_digest", mode="before")
    def _fixture_digest(cls, value, info):
        if value is None:
            return _sha256(_json_bytes(info.data))
        if type(value) is not str or _SHA_RE.fullmatch(value) is None:
            raise ValueError("fixture_digest must be sha256:<64 lowercase hex>")
        return value

    @field_validator("fixture_id", mode="before")
    def _fixture_id(cls, value, info):
        if value is None:
            return _id32("council_fixture_", {"fixture_digest": info.data["fixture_digest"]})
        if type(value) is not str or _FIXTURE_ID_RE.fullmatch(value) is None:
            raise ValueError("fixture_id must be council_fixture_<32 lowercase hex>")
        return value

    @model_validator(mode="after")
    def _verify(self):
        if self.multi_blocking_with_valid_evidence > self.multi_blocking_count:
            raise ValueError("blocking_with_valid_evidence must not exceed blocking_count")
        body = self.model_dump(mode="json", exclude={"fixture_digest", "fixture_id"})
        if self.fixture_digest != _sha256(_json_bytes(body)):
            raise ValueError("fixture_digest must equal the deterministic recomputation")
        if self.fixture_id != _id32("council_fixture_", {"fixture_digest": self.fixture_digest}):
            raise ValueError("fixture_id must equal the deterministic recomputation")
        return self


def _recall(fixture, arm):
    gold = set(fixture.gold_issue_ids)
    detected = set(fixture.single_detected_issue_ids if arm == "single_strong_reviewer" else fixture.multi_detected_issue_ids)
    return 100.0 * len(gold & detected) / len(gold)


class CouncilArmSummary(BaseModel):
    """Derived aggregate for one arm of the report."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    arm: _ARM
    sample_size: StrictInt
    total_gold_issues: StrictInt
    total_detected_gold_issues: StrictInt
    per_fixture_recall_percent: tuple[float, ...]
    macro_recall_percent: float
    total_unsupported_claims: StrictInt
    false_block_count: StrictInt
    false_block_rate_percent: float
    total_human_review_seconds: float
    total_cost_usd: float
    blocking_count: StrictInt | None = None
    blocking_with_valid_evidence: StrictInt | None = None
    blocking_evidence_coverage_percent: float | None = None

    @field_validator("sample_size", "total_gold_issues", "total_detected_gold_issues", "total_unsupported_claims", "false_block_count", mode="before")
    def _counts(cls, value, info):
        return _int(value, info.field_name)

    @field_validator("per_fixture_recall_percent", mode="before")
    def _recall_tuple(cls, value, info):
        return _exact_tuple(value, info, "per_fixture_recall_percent")

    @field_validator("per_fixture_recall_percent")
    def _recalls(cls, value):
        return tuple(_num(item, "recall item", hi=100.0) for item in value)

    @field_validator("macro_recall_percent", "false_block_rate_percent", mode="before")
    def _rates(cls, value, info):
        return _num(value, info.field_name, hi=100.0)

    @field_validator("total_human_review_seconds", "total_cost_usd", mode="before")
    def _totals(cls, value, info):
        return _num(value, info.field_name)

    @field_validator("blocking_count", "blocking_with_valid_evidence")
    def _blocking_counts(cls, value, info):
        return None if value is None else _int(value, info.field_name)

    @field_validator("blocking_evidence_coverage_percent")
    def _coverage(cls, value):
        return None if value is None else _num(value, "coverage", hi=100.0)

    @model_validator(mode="after")
    def _verify(self):
        if len(self.per_fixture_recall_percent) != self.sample_size:
            raise ValueError("per_fixture_recall_percent must have one item per sample")
        if self.macro_recall_percent != sum(self.per_fixture_recall_percent) / self.sample_size:
            raise ValueError("macro_recall_percent must equal the mean per-fixture recall")
        if self.false_block_rate_percent != 100.0 * self.false_block_count / self.sample_size:
            raise ValueError("false_block_rate_percent must equal the derived rate")
        if self.arm == "single_strong_reviewer":
            if self.blocking_count is not None or self.blocking_with_valid_evidence is not None or self.blocking_evidence_coverage_percent is not None:
                raise ValueError("single arm must not carry Multi blocking metrics")
            return self
        if self.blocking_count is None or self.blocking_with_valid_evidence is None or self.blocking_evidence_coverage_percent is None:
            raise ValueError("Multi arm requires all blocking Evidence metrics")
        if self.blocking_with_valid_evidence > self.blocking_count:
            raise ValueError("blocking_with_valid_evidence must not exceed blocking_count")
        expected = 100.0 if self.blocking_count == 0 else 100.0 * self.blocking_with_valid_evidence / self.blocking_count
        if self.blocking_evidence_coverage_percent != expected:
            raise ValueError("blocking_evidence_coverage_percent must equal the derived coverage")
        return self


def _assessment_data(*, sample_size, macro_recall_gain_percent, unsupported_claims_reduction_percent, human_review_time_reduction_percent, false_block_rate_delta_percent, cost_ratio, blocking_evidence_coverage_percent):
    recall_gain_met = macro_recall_gain_percent >= _RECALL_GAIN
    unsupported_reduction_met = unsupported_claims_reduction_percent >= _UNSUPPORTED_REDUCTION
    time_reduction_met = human_review_time_reduction_percent >= _TIME_REDUCTION
    false_block_guard_met = false_block_rate_delta_percent <= _FALSE_BLOCK_DELTA
    evidence_guard_met = blocking_evidence_coverage_percent >= 100.0
    cost_guard_met = cost_ratio <= _COST_RATIO
    quality_gain_met = recall_gain_met or unsupported_reduction_met or time_reduction_met
    unmet = [
        code
        for code, met in (
            ("recall_gain", recall_gain_met),
            ("unsupported_reduction", unsupported_reduction_met),
            ("time_reduction", time_reduction_met),
            ("false_block_guard", false_block_guard_met),
            ("evidence_guard", evidence_guard_met),
            ("cost_guard", cost_guard_met),
        )
        if not met
    ]
    return {
        "schema_version": "v1",
        "sample_size": sample_size,
        "minimum_formal_sample_size": _MIN_FORMAL,
        "macro_recall_gain_percent": macro_recall_gain_percent,
        "unsupported_claims_reduction_percent": unsupported_claims_reduction_percent,
        "human_review_time_reduction_percent": human_review_time_reduction_percent,
        "false_block_rate_delta_percent": false_block_rate_delta_percent,
        "cost_ratio": cost_ratio,
        "blocking_evidence_coverage_percent": blocking_evidence_coverage_percent,
        "recall_gain_met": recall_gain_met,
        "unsupported_reduction_met": unsupported_reduction_met,
        "time_reduction_met": time_reduction_met,
        "quality_gain_met": quality_gain_met,
        "false_block_guard_met": false_block_guard_met,
        "evidence_guard_met": evidence_guard_met,
        "cost_guard_met": cost_guard_met,
        "all_thresholds_met": quality_gain_met and false_block_guard_met and evidence_guard_met and cost_guard_met,
        "unmet_threshold_codes": tuple(sorted(unmet)),
        "recall_gain_shortfall_percent": max(0.0, _RECALL_GAIN - macro_recall_gain_percent),
        "unsupported_reduction_shortfall_percent": max(0.0, _UNSUPPORTED_REDUCTION - unsupported_claims_reduction_percent),
        "time_reduction_shortfall_percent": max(0.0, _TIME_REDUCTION - human_review_time_reduction_percent),
    }


class CouncilPromotionAssessment(BaseModel):
    """Deterministic promotion-experiment guard assessment."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    sample_size: StrictInt
    minimum_formal_sample_size: Literal[12] = 12
    macro_recall_gain_percent: float
    unsupported_claims_reduction_percent: float
    human_review_time_reduction_percent: float
    false_block_rate_delta_percent: float
    cost_ratio: float
    blocking_evidence_coverage_percent: float
    recall_gain_met: StrictBool
    unsupported_reduction_met: StrictBool
    time_reduction_met: StrictBool
    quality_gain_met: StrictBool
    false_block_guard_met: StrictBool
    evidence_guard_met: StrictBool
    cost_guard_met: StrictBool
    all_thresholds_met: StrictBool
    unmet_threshold_codes: tuple[str, ...]
    recall_gain_shortfall_percent: float
    unsupported_reduction_shortfall_percent: float
    time_reduction_shortfall_percent: float

    @field_validator("sample_size", mode="before")
    def _sample(cls, value):
        return _int(value, "sample_size")

    @field_validator("macro_recall_gain_percent", "unsupported_claims_reduction_percent", "human_review_time_reduction_percent", "false_block_rate_delta_percent", "cost_ratio", mode="before")
    def _deltas(cls, value, info):
        return _num(value, info.field_name, lo=None)

    @field_validator("blocking_evidence_coverage_percent", mode="before")
    def _coverage(cls, value):
        return _num(value, "blocking_evidence_coverage_percent", hi=100.0)

    @field_validator("recall_gain_shortfall_percent", "unsupported_reduction_shortfall_percent", "time_reduction_shortfall_percent", mode="before")
    def _shortfalls(cls, value, info):
        return _num(value, info.field_name)

    @field_validator("unmet_threshold_codes", mode="before")
    def _codes_tuple(cls, value, info):
        return _exact_tuple(value, info, "unmet_threshold_codes")

    @field_validator("unmet_threshold_codes")
    def _codes(cls, value):
        if tuple(value) != tuple(sorted(value)) or len(set(value)) != len(value):
            raise ValueError("unmet_threshold_codes must be canonical-sorted unique")
        for code in value:
            if code not in _CODE_SET:
                raise ValueError(f"unmet_threshold_codes item {code!r} is not allowed")
        return value

    @model_validator(mode="after")
    def _verify(self):
        expected = _assessment_data(
            sample_size=self.sample_size,
            macro_recall_gain_percent=self.macro_recall_gain_percent,
            unsupported_claims_reduction_percent=self.unsupported_claims_reduction_percent,
            human_review_time_reduction_percent=self.human_review_time_reduction_percent,
            false_block_rate_delta_percent=self.false_block_rate_delta_percent,
            cost_ratio=self.cost_ratio,
            blocking_evidence_coverage_percent=self.blocking_evidence_coverage_percent,
        )
        for name, expected_value in expected.items():
            if getattr(self, name) != expected_value:
                raise ValueError(f"{name} must equal the deterministic recomputation")
        return self


class CouncilReport(BaseModel):
    """Immutable deterministic P4-09 Council comparison report."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    report_id: str
    report_digest: str
    default_topology: Literal["single_strong_reviewer"] = "single_strong_reviewer"
    status: _STATUS
    sample_size: StrictInt
    minimum_formal_sample_size: Literal[12] = 12
    category_counts: tuple[tuple[_CATEGORY, StrictInt], ...]
    single_metrics: CouncilArmSummary
    multi_metrics: CouncilArmSummary
    promotion_assessment: CouncilPromotionAssessment
    fixture_ids: tuple[str, ...]
    fixture_digests: tuple[str, ...]
    fixtures: tuple[CouncilComparisonFixture, ...]

    @field_validator("report_id", mode="before")
    def _report_id(cls, value):
        if type(value) is not str or _REPORT_ID_RE.fullmatch(value) is None:
            raise ValueError("report_id must be council_report_<32 lowercase hex>")
        return value

    @field_validator("report_digest", mode="before")
    def _report_digest(cls, value):
        if type(value) is not str or _SHA_RE.fullmatch(value) is None:
            raise ValueError("report_digest must be sha256:<64 lowercase hex>")
        return value

    @field_validator("sample_size", mode="before")
    def _sample(cls, value):
        return _int(value, "sample_size")

    @field_validator("category_counts", mode="before")
    def _category_tuple(cls, value, info):
        return tuple(tuple(pair) for pair in _exact_tuple(value, info, "category_counts"))

    @field_validator("category_counts")
    def _category_counts(cls, value):
        if len(value) != len(_CATEGORIES):
            raise ValueError("category_counts must have one entry per category")
        result = []
        for category, count in value:
            if category not in _CATEGORIES:
                raise ValueError(f"unknown category {category!r}")
            result.append((category, _int(count, "category count")))
        if tuple(category for category, _ in result) != _CATEGORIES:
            raise ValueError("category_counts must be in canonical category order")
        return tuple(result)

    @field_validator("fixture_ids", "fixture_digests", mode="before")
    def _fixture_tuple(cls, value, info):
        return _exact_tuple(value, info, "fixture tuple")

    @field_validator("fixture_ids")
    def _fixture_ids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("fixture_ids must be unique")
        for item in value:
            if type(item) is not str or _FIXTURE_ID_RE.fullmatch(item) is None:
                raise ValueError("fixture_ids must be council_fixture_<32 lowercase hex>")
        return value

    @field_validator("fixture_digests")
    def _fixture_digests(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("fixture_digests must be unique")
        for item in value:
            if type(item) is not str or _SHA_RE.fullmatch(item) is None:
                raise ValueError("fixture_digests must be sha256:<64 lowercase hex>")
        return value

    @field_validator("fixtures", mode="before")
    def _fixtures_tuple(cls, value, info):
        return _exact_tuple(value, info, "fixtures")

    @model_validator(mode="before")
    def _nested_models(cls, data, info):
        if not isinstance(data, dict):
            raise ValueError("CouncilReport must validate from a mapping")
        data = dict(data)
        nested = {
            "single_metrics": CouncilArmSummary,
            "multi_metrics": CouncilArmSummary,
            "promotion_assessment": CouncilPromotionAssessment,
        }
        if info.mode == "json":
            for name, model_type in nested.items():
                if type(data.get(name)) is dict:
                    data[name] = model_type.model_validate_json(json.dumps(data[name]))
            return data
        for name, model_type in nested.items():
            if type(data.get(name)) is not model_type:
                raise ValueError(f"{name} must be an exact {model_type.__name__} instance")
        if type(data.get("fixtures")) is not tuple:
            raise ValueError("fixtures must be an exact tuple at raw validation")
        for item in data.get("fixtures", ()):
            if type(item) is not CouncilComparisonFixture:
                raise ValueError("fixtures items must be exact CouncilComparisonFixture instances")
        return data

    @model_validator(mode="after")
    def _verify(self):
        expected = _derive_report_data(self.fixtures)
        if self.model_dump(mode="json") != _report_json_data(expected):
            raise ValueError("report must equal the pure derivation from its fixtures")
        return self


def _report_json_data(data):
    return {
        "schema_version": data["schema_version"],
        "report_id": data["report_id"],
        "report_digest": data["report_digest"],
        "default_topology": data["default_topology"],
        "status": data["status"],
        "sample_size": data["sample_size"],
        "minimum_formal_sample_size": data["minimum_formal_sample_size"],
        "category_counts": [list(pair) for pair in data["category_counts"]],
        "single_metrics": data["single_metrics"].model_dump(mode="json"),
        "multi_metrics": data["multi_metrics"].model_dump(mode="json"),
        "promotion_assessment": data["promotion_assessment"].model_dump(mode="json"),
        "fixture_ids": list(data["fixture_ids"]),
        "fixture_digests": list(data["fixture_digests"]),
        "fixtures": [item.model_dump(mode="json") for item in data["fixtures"]],
    }


def _arm_summary(arm, ordered, recalls, *, gold, detected, unsupported, false_blocks, seconds, cost):
    sample_size = len(ordered)
    blocking_count = blocking_valid = coverage = None
    if arm == "multi_reviewer_council":
        blocking_count = sum(item.multi_blocking_count for item in ordered)
        blocking_valid = sum(item.multi_blocking_with_valid_evidence for item in ordered)
        coverage = 100.0 if blocking_count == 0 else 100.0 * blocking_valid / blocking_count
    return CouncilArmSummary(
        schema_version="v1", arm=arm, sample_size=sample_size,
        total_gold_issues=gold, total_detected_gold_issues=detected,
        per_fixture_recall_percent=recalls, macro_recall_percent=sum(recalls) / sample_size,
        total_unsupported_claims=unsupported, false_block_count=false_blocks,
        false_block_rate_percent=100.0 * false_blocks / sample_size,
        total_human_review_seconds=seconds, total_cost_usd=cost,
        blocking_count=blocking_count, blocking_with_valid_evidence=blocking_valid,
        blocking_evidence_coverage_percent=coverage,
    )


def _derive_report_data(fixtures):
    if type(fixtures) is not tuple:
        raise TypeError("fixtures must be an exact tuple")
    if len(fixtures) < _MIN_SAMPLE:
        raise ValueError(f"at least {_MIN_SAMPLE} fixtures are required")
    seen_ids = set()
    for item in fixtures:
        if type(item) is not CouncilComparisonFixture:
            raise TypeError("fixtures must contain exact CouncilComparisonFixture instances")
        if item.fixture_id in seen_ids:
            raise ValueError("fixtures must be unique")
        seen_ids.add(item.fixture_id)
    ordered = tuple(sorted(fixtures, key=lambda item: item.fixture_id))
    sample_size = len(ordered)
    category_counts = tuple(
        (category, sum(1 for item in ordered if item.category == category))
        for category in _CATEGORIES
    )
    total_gold = sum(len(item.gold_issue_ids) for item in ordered)
    single_hits = sum(len(set(item.gold_issue_ids) & set(item.single_detected_issue_ids)) for item in ordered)
    multi_hits = sum(len(set(item.gold_issue_ids) & set(item.multi_detected_issue_ids)) for item in ordered)
    single = _arm_summary(
        "single_strong_reviewer", ordered,
        tuple(_recall(item, "single_strong_reviewer") for item in ordered),
        gold=total_gold, detected=single_hits,
        unsupported=sum(item.single_unsupported_claims for item in ordered),
        false_blocks=sum(1 for item in ordered if item.single_false_block),
        seconds=sum(item.single_human_review_seconds for item in ordered),
        cost=sum(item.single_cost_usd for item in ordered),
    )
    multi = _arm_summary(
        "multi_reviewer_council", ordered,
        tuple(_recall(item, "multi_reviewer_council") for item in ordered),
        gold=total_gold, detected=multi_hits,
        unsupported=sum(item.multi_unsupported_claims for item in ordered),
        false_blocks=sum(1 for item in ordered if item.multi_false_block),
        seconds=sum(item.multi_human_review_seconds for item in ordered),
        cost=sum(item.multi_cost_usd for item in ordered),
    )
    unsupported_reduction = (
        100.0 * (single.total_unsupported_claims - multi.total_unsupported_claims) / single.total_unsupported_claims
        if single.total_unsupported_claims > 0 else 0.0
    )
    time_reduction = (
        100.0 * (single.total_human_review_seconds - multi.total_human_review_seconds) / single.total_human_review_seconds
        if single.total_human_review_seconds > 0 else 0.0
    )
    cost_ratio = (
        multi.total_cost_usd / single.total_cost_usd
        if single.total_cost_usd > 0 else (1.0 if multi.total_cost_usd == 0 else 2.6)
    )
    assessment = CouncilPromotionAssessment.model_validate(_assessment_data(
        sample_size=sample_size,
        macro_recall_gain_percent=multi.macro_recall_percent - single.macro_recall_percent,
        unsupported_claims_reduction_percent=unsupported_reduction,
        human_review_time_reduction_percent=time_reduction,
        false_block_rate_delta_percent=multi.false_block_rate_percent - single.false_block_rate_percent,
        cost_ratio=cost_ratio,
        blocking_evidence_coverage_percent=multi.blocking_evidence_coverage_percent,
    ))
    if sample_size < _MIN_FORMAL:
        status = "insufficient_sample"
    elif assessment.all_thresholds_met:
        status = "candidate_for_p6_review"
    else:
        status = "thresholds_not_met"
    data = {
        "schema_version": "v1", "report_id": "", "report_digest": "",
        "default_topology": "single_strong_reviewer", "status": status,
        "sample_size": sample_size, "minimum_formal_sample_size": _MIN_FORMAL,
        "category_counts": category_counts, "single_metrics": single,
        "multi_metrics": multi, "promotion_assessment": assessment,
        "fixture_ids": tuple(item.fixture_id for item in ordered),
        "fixture_digests": tuple(item.fixture_digest for item in ordered),
        "fixtures": ordered,
    }
    report_digest = _sha256(_json_bytes(_report_json_data(data)))
    data["report_digest"] = report_digest
    data["report_id"] = _id32("council_report_", {"digest": report_digest, "sample_size": sample_size})
    return data


class CouncilReportBuilder:
    """Pure deterministic builder for the P4-09 comparison report."""

    @staticmethod
    def build(fixtures):
        if type(fixtures) is not tuple:
            raise TypeError("fixtures must be an exact tuple")
        return CouncilReport.model_validate(_derive_report_data(fixtures))


__all__ = (
    "CouncilComparisonFixture",
    "CouncilArmSummary",
    "CouncilPromotionAssessment",
    "CouncilReport",
    "CouncilReportBuilder",
)
