"""固定的本地保障评测数据集。"""

from .dataset import (
    DATASET_ID,
    HiddenGold,
    PublicEvalCase,
    load_hidden_gold,
    load_public_cases,
    reviewer_payload,
    validate_dataset,
)
from .runner import (
    ARM_ORDER,
    ArmRunResult,
    CaseComparison,
    ComparisonRun,
    ComparisonRunner,
    EvalFinding,
    REVIEWER_ROLE_ORDER,
)
from .adapters import (
    COUNCIL_ROLES,
    InvocationFact,
    ModelArmAdapter,
    ROLE_ORDER,
    derive_finding_id,
)
from .rules_adapter import RULE_VERSION, RulesOnlyAdapter, run_rules_only
from .result_artifact import (
    MODEL_REF,
    PROVIDER,
    PUBLIC_ISSUE_TAXONOMY,
    build_result_artifact,
    replay_result_artifact,
)
from .scorers import (
    HIDDEN_TO_PUBLIC_TAXONOMY,
    RULE_TO_PUBLIC_TAXONOMY,
    build_score_report,
    normalize_predicted_issue_ids,
    verify_score_report,
)
from .promotion import (
    NOT_PROMOTED,
    PROMOTION_SCHEMA_VERSION,
    PROMOTED,
    THRESHOLDS,
    build_promotion_decision,
    derive_promotion_state,
    verify_promotion_decision,
)

__all__ = [
    "DATASET_ID",
    "HiddenGold",
    "PublicEvalCase",
    "load_hidden_gold",
    "load_public_cases",
    "reviewer_payload",
    "validate_dataset",
    "ARM_ORDER",
    "ArmRunResult",
    "CaseComparison",
    "ComparisonRun",
    "ComparisonRunner",
    "EvalFinding",
    "REVIEWER_ROLE_ORDER",
    "COUNCIL_ROLES",
    "InvocationFact",
    "ModelArmAdapter",
    "ROLE_ORDER",
    "derive_finding_id",
    "RULE_VERSION",
    "RulesOnlyAdapter",
    "run_rules_only",
    "MODEL_REF",
    "PROVIDER",
    "PUBLIC_ISSUE_TAXONOMY",
    "build_result_artifact",
    "replay_result_artifact",
    "HIDDEN_TO_PUBLIC_TAXONOMY",
    "RULE_TO_PUBLIC_TAXONOMY",
    "build_score_report",
    "normalize_predicted_issue_ids",
    "verify_score_report",
    "NOT_PROMOTED",
    "PROMOTION_SCHEMA_VERSION",
    "PROMOTED",
    "THRESHOLDS",
    "build_promotion_decision",
    "derive_promotion_state",
    "verify_promotion_decision",
]
