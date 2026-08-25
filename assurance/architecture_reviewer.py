"""V2-P4-03 Architecture Reviewer facade.

Independent architecture-review profile over the accepted role-neutral
structured reviewer runtime: dependency direction, boundary integrity,
duplicate capability, competing truth sources, public contracts, ADR
deviation, and blast radius. This facade never approves or emits PASS/Gate,
never executes tools/providers, and contains no filesystem, environment,
network, time, random, or persistence behavior.
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


_ARCHITECTURE_RUBRIC_TABLE = MappingProxyType(
    {
        "role": "architecture",
        "rubric_version": "architecture.v0",
        "items": (
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 1,
                    "code": "DEPENDENCY_DIRECTION",
                    "name": "Dependency direction and layering",
                    "description": (
                        "Check dependency direction and layering for cycles, "
                        "inverted ownership, or leaks across declared layers."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 2,
                    "code": "BOUNDARY_INTEGRITY",
                    "name": "Module/service boundary integrity",
                    "description": (
                        "Check whether module or service boundaries are "
                        "bypassed, crossed through hidden channels, or "
                        "weakened by the change."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 3,
                    "code": "DUPLICATE_CAPABILITY",
                    "name": "Duplicated capability or implementation",
                    "description": (
                        "Check for duplicated capability or implementation "
                        "that should be reused or consolidated."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 4,
                    "code": "SECOND_SOURCE_OF_TRUTH",
                    "name": "Competing state/config/contract truth source",
                    "description": (
                        "Check for a competing state, config, or contract "
                        "truth source that can diverge from the canonical one."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 5,
                    "code": "PUBLIC_CONTRACT",
                    "name": "Public API, Event, Schema, and compatibility contracts",
                    "description": (
                        "Check public API, Event, Schema, and compatibility "
                        "contracts for breaking or undocumented changes."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 6,
                    "code": "ADR_DEVIATION",
                    "name": "Deviation from accepted ADR/architecture decision",
                    "description": (
                        "Check whether the change deviates from an accepted "
                        "ADR or architecture decision without a new accepted "
                        "decision."
                    ),
                }
            ),
            MappingProxyType(
                {
                    "schema_version": "v1",
                    "number": 7,
                    "code": "BLAST_RADIUS",
                    "name": "Cross-module/repository/service/owner impact surface",
                    "description": (
                        "Check the cross-module, cross-repository, "
                        "cross-service, or cross-owner impact surface of the "
                        "change."
                    ),
                }
            ),
        ),
        "tool_authority": (
            "code_glob",
            "code_grep",
            "code_read",
            "git_graph_read",
            "gitnexus_context",
            "gitnexus_impact",
            "gitnexus_query",
            "prism_evidence_read",
        ),
    }
)


def _architecture_rubric_digest() -> str:
    data = {
        "role": _ARCHITECTURE_RUBRIC_TABLE["role"],
        "rubric_version": _ARCHITECTURE_RUBRIC_TABLE["rubric_version"],
        "rubric": [
            {
                "schema_version": item["schema_version"],
                "number": item["number"],
                "code": item["code"],
                "name": item["name"],
                "description": item["description"],
            }
            for item in _ARCHITECTURE_RUBRIC_TABLE["items"]
        ],
    }
    return _sha256_digest(_canonical_json_bytes(data))


_ARCHITECTURE_RUBRIC_DIGEST = _architecture_rubric_digest()

_ARCHITECTURE_PROFILE = ReviewerProfile(
    role=_ARCHITECTURE_RUBRIC_TABLE["role"],
    rubric_version=_ARCHITECTURE_RUBRIC_TABLE["rubric_version"],
    rubric_hash=_ARCHITECTURE_RUBRIC_DIGEST,
    rubric=tuple(
        RubricItem.model_validate(item)
        for item in _ARCHITECTURE_RUBRIC_TABLE["items"]
    ),
    tool_authority=_ARCHITECTURE_RUBRIC_TABLE["tool_authority"],
)


class ArchitectureReviewer:
    """Architecture-only facade over the shared structured reviewer runtime."""

    @staticmethod
    def prepare(value: ReviewerInput) -> ReviewerPrompt:
        if type(value) is not ReviewerInput:
            raise TypeError("value must be an exact ReviewerInput")
        return StructuredReviewerRuntime.prepare(value, _ARCHITECTURE_PROFILE)

    @staticmethod
    def normalize(
        value: ReviewerNormalizationInput,
    ) -> FindingOutput:
        if type(value) is not ReviewerNormalizationInput:
            raise TypeError(
                "value must be an exact ReviewerNormalizationInput"
            )
        return StructuredReviewerRuntime.normalize(
            value, _ARCHITECTURE_PROFILE
        )


__all__ = ("ArchitectureReviewer",)
