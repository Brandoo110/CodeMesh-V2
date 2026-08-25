"""V2-P4-04 Operability Reviewer facade.

Independent operability review profile over the accepted role-neutral
structured reviewer runtime: migration/rollback, idempotency/retry,
telemetry/alerting, timeout/rate/cost, fallback boundaries, kill-switch
recovery, and runbook/owner/SLO. This facade never approves or emits
PASS/Gate, never executes tools/providers, and contains no filesystem,
environment, network, time, random, or persistence behavior.
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


_OPERABILITY_RUBRIC_TABLE = MappingProxyType(
    {
        "role": "operability",
        "rubric_version": "operability.v0",
        "items": (
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 1,
                    "code": "MIGRATION_ROLLBACK",
                    "name": "Migration forward, rollback, replay, and data compatibility",
                    "description": (
                        "Check migration forward, rollback, replay, and "
                        "data compatibility for schema, state, or "
                        "configuration changes."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 2,
                    "code": "IDEMPOTENCY_RETRY",
                    "name": "External side effects, idempotency, retry, duplicate execution, and reconciliation",
                    "description": (
                        "Check external side effects, idempotency, retry "
                        "behavior, duplicate execution, and reconciliation "
                        "for repeated or concurrent runs."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 3,
                    "code": "TELEMETRY_ALERT",
                    "name": "Logs, metrics, traces, alerts, and fault localization",
                    "description": (
                        "Check logs, metrics, traces, alerts, and fault "
                        "localization so runtime failures are observable "
                        "and diagnosable."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 4,
                    "code": "TIMEOUT_RATE_COST",
                    "name": "Timeouts, rate limits, concurrency, cost ceilings, and resource budgets",
                    "description": (
                        "Check timeouts, rate limits, concurrency, cost "
                        "ceilings, and resource budgets for bounded and "
                        "predictable operation."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 5,
                    "code": "FALLBACK_BOUNDARY",
                    "name": "Fallback reason, provider/data boundary, degradation, and fail-closed behavior",
                    "description": (
                        "Check fallback reason, provider or data boundary, "
                        "degradation, and fail-closed behavior to preserve "
                        "evidence and correctness."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 6,
                    "code": "KILL_SWITCH_RECOVERY",
                    "name": "Kill switch, isolation, recovery, rollback, and observation window",
                    "description": (
                        "Check kill switch, isolation, recovery, rollback, "
                        "and observation window for stopping and resuming "
                        "affected flows."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 7,
                    "code": "RUNBOOK_OWNER",
                    "name": "Runbook, owner, SLO, responsibility, and escalation path",
                    "description": (
                        "Check runbook, owner, SLO, responsibility, and "
                        "escalation path for operational accountability."
                    ),
                }
            ),
        ),
        "tool_authority": (
            "config_read",
            "log_evidence_read",
            "runbook_read",
            "telemetry_evidence_read",
            "test_evidence_read",
            "validation_result_read",
        ),
    }
)


def _operability_rubric_digest() -> str:
    data = {
        "role": _OPERABILITY_RUBRIC_TABLE["role"],
        "rubric_version": _OPERABILITY_RUBRIC_TABLE["rubric_version"],
        "rubric": [
            {
                "schema_version": item["schema_version"],
                "number": item["number"],
                "code": item["code"],
                "name": item["name"],
                "description": item["description"],
            }
            for item in _OPERABILITY_RUBRIC_TABLE["items"]
        ],
    }
    return _sha256_digest(_canonical_json_bytes(data))


_OPERABILITY_RUBRIC_DIGEST = _operability_rubric_digest()

_OPERABILITY_PROFILE = ReviewerProfile(
    role=_OPERABILITY_RUBRIC_TABLE["role"],
    rubric_version=_OPERABILITY_RUBRIC_TABLE["rubric_version"],
    rubric_hash=_OPERABILITY_RUBRIC_DIGEST,
    rubric=tuple(
        RubricItem.model_validate(item)
        for item in _OPERABILITY_RUBRIC_TABLE["items"]
    ),
    tool_authority=_OPERABILITY_RUBRIC_TABLE["tool_authority"],
)


class OperabilityReviewer:
    """Operability-only facade over the shared structured reviewer runtime."""

    @staticmethod
    def prepare(value: ReviewerInput) -> ReviewerPrompt:
        if type(value) is not ReviewerInput:
            raise TypeError("value must be an exact ReviewerInput")
        return StructuredReviewerRuntime.prepare(value, _OPERABILITY_PROFILE)

    @staticmethod
    def normalize(
        value: ReviewerNormalizationInput,
    ) -> FindingOutput:
        if type(value) is not ReviewerNormalizationInput:
            raise TypeError(
                "value must be an exact ReviewerNormalizationInput"
            )
        return StructuredReviewerRuntime.normalize(
            value, _OPERABILITY_PROFILE
        )


__all__ = ("OperabilityReviewer",)
