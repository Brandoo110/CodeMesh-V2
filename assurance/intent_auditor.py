"""V2-P4-02 Intent Auditor facade.

Pure deterministic Intent review: spec coverage, scope creep, missing
acceptance/NFR, hidden assumptions, test deletion/skip, and value/readiness
questions. It never approves or emits PASS/Gate, never executes response
content, never runs tools, and never touches provider/network/file state.
"""

from types import MappingProxyType

from .reviewer_contracts import FindingOutput, ReviewerInput
from .reviewer_runtime import (
    _canonical_json_bytes,
    _sha256_digest,
    ReviewerNormalizationInput,
    ReviewerProfile,
    ReviewerPrompt,
    RubricItem,
    StructuredReviewerRuntime,
)


_INTENT_RUBRIC_TABLE = MappingProxyType(
    {
        "role": "intent",
        "rubric_version": "intent.v0",
        "items": (
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 1,
                    "code": "SPEC_COVERAGE",
                    "name": "Spec coverage",
                    "description": (
                        "Check whether the change fully implements the "
                        "written specification and every required behavior "
                        "has a visible code path."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 2,
                    "code": "SCOPE_CREEP",
                    "name": "Scope creep",
                    "description": (
                        "Check whether the change adds behavior, files, or "
                        "contracts beyond the frozen scope without a "
                        "justified reason."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 3,
                    "code": "MISSING_ACCEPTANCE_NFR",
                    "name": "Missing acceptance and NFR",
                    "description": (
                        "Check whether acceptance criteria and "
                        "non-functional requirements are missing, implied, "
                        "or unverifiable."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 4,
                    "code": "HIDDEN_ASSUMPTION",
                    "name": "Hidden assumption",
                    "description": (
                        "Check whether the change relies on unstated "
                        "assumptions about environment, data, callers, or "
                        "deployment."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 5,
                    "code": "TEST_DELETION_SKIP",
                    "name": "Test deletion or skip",
                    "description": (
                        "Check whether tests were deleted, skipped, "
                        "weakened, or excluded without a stated reason."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 6,
                    "code": "VALUE_READINESS_QUESTION",
                    "name": "Value and readiness question",
                    "description": (
                        "Ask a readiness or value question when the change's "
                        "benefit, completeness, or production readiness is "
                        "not evidenced."
                    ),
                }
            ),
        ),
        "tool_authority": (
            "diff_read",
            "history_read",
            "spec_read",
            "tests_read",
        ),
    }
)


def _intent_rubric_digest() -> str:
    data = {
        "role": _INTENT_RUBRIC_TABLE["role"],
        "rubric_version": _INTENT_RUBRIC_TABLE["rubric_version"],
        "rubric": [
            {
                "schema_version": item["schema_version"],
                "number": item["number"],
                "code": item["code"],
                "name": item["name"],
                "description": item["description"],
            }
            for item in _INTENT_RUBRIC_TABLE["items"]
        ],
    }
    return _sha256_digest(_canonical_json_bytes(data))


_INTENT_RUBRIC_DIGEST = _intent_rubric_digest()

_INTENT_PROFILE = ReviewerProfile(
    role=_INTENT_RUBRIC_TABLE["role"],
    rubric_version=_INTENT_RUBRIC_TABLE["rubric_version"],
    rubric_hash=_INTENT_RUBRIC_DIGEST,
    rubric=tuple(
        RubricItem.model_validate(item)
        for item in _INTENT_RUBRIC_TABLE["items"]
    ),
    tool_authority=_INTENT_RUBRIC_TABLE["tool_authority"],
)


class IntentAuditor:
    """Intent-only facade over the shared structured reviewer runtime."""

    @staticmethod
    def prepare(value: ReviewerInput) -> ReviewerPrompt:
        if type(value) is not ReviewerInput:
            raise TypeError("value must be an exact ReviewerInput")
        return StructuredReviewerRuntime.prepare(value, _INTENT_PROFILE)

    @staticmethod
    def normalize(
        value: ReviewerNormalizationInput,
    ) -> FindingOutput:
        if type(value) is not ReviewerNormalizationInput:
            raise TypeError(
                "value must be an exact ReviewerNormalizationInput"
            )
        return StructuredReviewerRuntime.normalize(value, _INTENT_PROFILE)


__all__ = ("IntentAuditor",)
