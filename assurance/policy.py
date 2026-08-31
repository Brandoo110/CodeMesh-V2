"""P3-02 策略门（Policy Gate v0）：纯确定性裁决与防伪绑定。

本模块只做输入模型上的纯规则推导与摘要绑定：不读取路径/文件/网络/API/
仓库/Git，不调用 Collector/Reviewer/路由/编排，不执行子进程、shell、
eval/exec，不持有可变全局状态，也不依赖时间/随机/环境。
"""

import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import (
    ChangeSubject,
    ExecutionReceipt,
    Finding,
    HumanDecision,
    PolicyDecision,
)
from .digests import normalize_repo_path
from .risk import RiskClassificationResult


_RULES_VERSION = "gate.v0"

_REASON_ORDER = (
    "SUBJECT_DIGEST_MISMATCH",
    "FINDING_STALE",
    "PROVIDER_BOUNDARY_CROSSING",
    "PROVIDER_BOUNDARY_UNKNOWN",
    "MANIFEST_HAS_GAPS",
    "EVIDENCE_FRESHNESS_UNKNOWN",
    "EVIDENCE_EXPIRED",
    "REQUIRED_COLLECTOR_MISSING",
    "REQUIRED_COLLECTOR_NOT_SUCCESS",
    "REQUIRED_COLLECTOR_NOT_FRESH",
    "REQUIRED_REVIEWER_MISSING",
    "REQUIRED_REVIEWER_NOT_SUCCESS",
    "FINDING_EVIDENCE_REF_MISSING",
    "DETERMINISTIC_FINDING_BLOCKING",
    "REQUIRED_HUMAN_REJECTED",
    "REQUIRED_HUMAN_MISSING",
    "REQUIRED_HUMAN_WAIVER_EXPIRED",
    "REQUIRED_HUMAN_CONFLICT",
)

_OUTCOME_PRIORITY = (
    "STALE",
    "BLOCKED",
    "NEEDS_HUMAN",
    "PASS_WITH_WAIVER",
    "PASS",
)

_COLLECTOR_MAPPING = MappingProxyType(
    {
        "git_snapshot": ("git_snapshot", "collector.git"),
        "task_policy_adr": ("intake_documents", "collector.intake"),
        "deterministic_commands": ("command_batch", "collector.command"),
        # evidence_manifest 由当前 risk_result.input.manifest 本身满足，
        # 不需要在清单条目里再出现同 kind/producer 的映射对。
        "evidence_manifest": None,
        "authz_validation": (
            "authz_validation",
            "collector.authz_validation",
        ),
        "migration_validation": (
            "migration_validation",
            "collector.migration_validation",
        ),
        "api_contract": ("api_contract", "collector.api_contract"),
        "dependency_audit": (
            "dependency_audit",
            "collector.dependency_audit",
        ),
        "ci_iac_validation": (
            "ci_iac_validation",
            "collector.ci_iac_validation",
        ),
        "side_effect_validation": (
            "side_effect_validation",
            "collector.side_effect_validation",
        ),
        "provider_boundary_attestation": (
            "provider_boundary_attestation",
            "collector.provider_boundary_attestation",
        ),
    }
)

_REVIEWER_SCHEMA_STATUSES = frozenset({"valid", "repaired"})
_BLOCKING_SEVERITIES = frozenset({"high", "critical"})
_BLOCKING_STATUSES = frozenset({"open", "acknowledged"})

_RULES_TABLE = MappingProxyType(
    {
        "rules_version": _RULES_VERSION,
        "reason_order": _REASON_ORDER,
        "outcome_priority": _OUTCOME_PRIORITY,
        "collector_mapping": _COLLECTOR_MAPPING,
        "reviewer_schema_statuses": _REVIEWER_SCHEMA_STATUSES,
        "blocking_severities": _BLOCKING_SEVERITIES,
        "blocking_statuses": _BLOCKING_STATUSES,
    }
)

_NUMERIC_DATETIME_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


def _jsonable(value):
    if isinstance(value, MappingProxyType):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_jsonable(item) for item in value)
    return value


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _evidence_id(prefix: str, subject_digest: str, artifact_digest: str) -> str:
    return prefix + hashlib.sha256(
        (subject_digest + artifact_digest).encode("ascii")
    ).hexdigest()[:32]


def _strict_coverage_enabled(value: "PolicyEvaluationInput") -> bool:
    """Detect a real collector projection while retaining v1 fixture shape.

    Older callers supplied hand-authored manifest entries with opaque IDs.  A
    real collector result carries its derived ``ev_*`` identity; only those
    entries opt into the stronger byte/ref binding checks below.
    """

    return any(
        entry.evidence_id.startswith(
            ("ev_git_", "ev_intake_", "ev_command_", "ev_api_contract_", "ev_official_")
        )
        for entry in value.risk_result.input.manifest.entries
    )


def project_collector_coverage(
    value: "PolicyEvaluationInput",
) -> tuple[dict[str, object], ...]:
    """Project only typed collector facts into a deterministic coverage view.

    This helper is deliberately pure.  It never reads CAS; all bytes are
    represented by the already-bound artifact digests and the original
    collector Evidence fields.  The manifest projection excludes its own
    synthetic entry so the closure cannot recurse.
    """

    if type(value) is not PolicyEvaluationInput:
        raise TypeError("value must be an exact PolicyEvaluationInput")
    subject = value.subject.subject_digest
    intake = value.risk_result.input.intake
    manifest = value.risk_result.input.manifest

    def first_entry(kind: str, producer: str):
        return next(
            (
                entry
                for entry in manifest.entries
                if entry.kind == kind and entry.producer == producer
            ),
            None,
        )

    def evidence_projection(entry):
        if entry is None:
            return None
        return {
            "evidence_id": entry.evidence_id,
            "kind": entry.kind,
            "producer": entry.producer,
            "subject_digest": entry.subject_digest,
            "artifact_digest": entry.artifact_digest,
            "source_ref": entry.source_ref,
            "status": entry.status,
            "collected_at": entry.collected_at,
        }

    intake_entry = first_entry("intake_documents", "collector.intake")
    command_entries = tuple(
        entry
        for entry in manifest.entries
        if entry.kind == "command_batch" and entry.producer == "collector.command"
    )
    command_entry = command_entries[0] if command_entries else None
    manifest_id = "ev_manifest_" + hashlib.sha256(
        (manifest.manifest_id + manifest.artifact_digest).encode("ascii")
    ).hexdigest()[:32]
    return (
        {
            "collector": "task_policy_adr",
            "evidence": evidence_projection(intake_entry),
            "documents": tuple(
                {
                    "kind": document.kind,
                    "path": document.path,
                    "artifact_digest": document.artifact_digest,
                    "byte_size": document.byte_size,
                }
                for document in intake.documents
                if document.kind in {"task_spec", "policy", "adr"}
            ),
        },
        {
            "collector": "deterministic_commands",
            "evidence": evidence_projection(command_entry),
            "observations": (),
        },
        {
            "collector": "evidence_manifest",
            "evidence": {
                "kind": "evidence_manifest",
                "producer": "builder.evidence_manifest",
                "subject_digest": subject,
                "artifact_digest": manifest.artifact_digest,
                "status": "success"
                if manifest.completeness_status == "complete"
                else "truncated",
                "source_ref": f"evidence_manifest:{manifest.manifest_id}",
                "evidence_id": manifest_id,
                "collected_at": manifest.evaluated_at,
            },
            "entries": tuple(
                entry.model_dump(mode="json")
                for entry in manifest.entries
                if entry.kind != "evidence_manifest"
                and entry.evidence_id != manifest.manifest_id
            ),
        },
    )


def _expected_collector_bindings(
    value: "PolicyEvaluationInput",
) -> dict[str, dict[str, object]]:
    """Derive real collector Evidence identity from the typed risk inputs."""

    subject = value.subject.subject_digest
    snapshot = value.risk_result.input.snapshot
    intake = value.risk_result.input.intake
    manifest = value.risk_result.input.manifest
    expected = {
        "git_snapshot": {
            "evidence_id": _evidence_id(
                "ev_git_", subject, snapshot.diff_artifact_digest
            ),
            "kind": "git_snapshot",
            "producer": "collector.git",
            "subject_digest": subject,
            "artifact_digest": snapshot.diff_artifact_digest,
            "source_ref": (
                f"git_snapshot:{snapshot.repository}:{snapshot.base_revision}:"
                f"{snapshot.head_revision}:base_to_worktree"
            ),
            "status": "success" if snapshot.complete else "truncated",
            "trust_level": "deterministic",
            "collected_at": snapshot.collected_at,
        },
        "task_policy_adr": {
            "evidence_id": _evidence_id(
                "ev_intake_", subject, intake.manifest_artifact_digest
            ),
            "kind": "intake_documents",
            "producer": "collector.intake",
            "subject_digest": subject,
            "artifact_digest": intake.manifest_artifact_digest,
            "source_ref": f"intake_documents:{subject}",
            "status": "success" if intake.complete else "truncated",
            "trust_level": "deterministic",
            "collected_at": intake.collected_at,
        },
        "deterministic_commands": {
            "kind": "command_batch",
            "producer": "collector.command",
            "subject_digest": subject,
            "source_ref": f"command_batch:{subject}",
            "trust_level": "deterministic",
        },
        "evidence_manifest": {
            "evidence_id": "ev_manifest_" + hashlib.sha256(
                (manifest.manifest_id + manifest.artifact_digest).encode("ascii")
            ).hexdigest()[:32],
        },
        "api_contract": {
            "kind": "api_contract",
            "producer": "collector.api_contract",
            "subject_digest": subject,
            "source_ref": "api_contract:contracts/openapi.json",
            "trust_level": "deterministic",
        },
    }
    command_entries = tuple(
        entry
        for entry in manifest.entries
        if entry.kind == "command_batch"
        and entry.producer == "collector.command"
    )
    if command_entries:
        expected["deterministic_commands"]["status"] = command_entries[0].status
        expected["deterministic_commands"]["artifact_digest"] = command_entries[0].artifact_digest
        expected["deterministic_commands"]["evidence_id"] = _evidence_id(
            "ev_command_", subject, command_entries[0].artifact_digest
        )
        expected["deterministic_commands"]["collected_at"] = command_entries[0].collected_at
    for entry in manifest.entries:
        if entry.kind != "api_contract" or entry.producer != "collector.api_contract":
            continue
        if entry.source_ref != "api_contract:contracts/openapi.json":
            continue
        expected["api_contract"]["evidence_id"] = "ev_api_contract_" + hashlib.sha256(
            "|".join(
                (
                    subject,
                    snapshot.head_revision,
                    "contracts/openapi.json",
                    entry.artifact_digest,
                    entry.status,
                )
            ).encode("ascii")
        ).hexdigest()[:32]
    return expected


def _entry_matches_projection(entry, expected: dict[str, object]) -> bool:
    values = entry.model_dump(mode="python")
    for key, expected_value in expected.items():
        if key == "source_ref_prefix":
            if not entry.source_ref.startswith(str(expected_value)):
                return False
            suffix = entry.source_ref[len(str(expected_value)):]
            if suffix == "missing":
                return False
            try:
                if normalize_repo_path(suffix) != suffix:
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if values.get(key, object()) != expected_value:
            return False
    return True


_RULES_DIGEST = _sha256_digest(
    _canonical_json_bytes(_jsonable(_RULES_TABLE))
)

__all__ = (
    "PolicyEvaluationInput",
    "PolicyGateResult",
    "PolicyGate",
)


class PolicyEvaluationInput(BaseModel):
    """策略门输入：精确嵌套合同与严格时间边界。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject: ChangeSubject
    risk_result: RiskClassificationResult
    findings: tuple[Finding, ...] = ()
    execution_receipts: tuple[ExecutionReceipt, ...] = ()
    human_decisions: tuple[HumanDecision, ...] = ()
    evaluated_at: AwareDatetime

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _reject_numeric_datetime(cls, value: object) -> object:
        if isinstance(value, bool) or isinstance(value, (int, float)):
            raise ValueError("datetime must not be a numeric value")
        if isinstance(value, str):
            stripped = value.strip()
            if (
                stripped
                and _NUMERIC_DATETIME_RE.fullmatch(stripped) is not None
            ):
                raise ValueError("datetime must not be a numeric string")
        return value

    @field_validator(
        "findings",
        "execution_receipts",
        "human_decisions",
        mode="before",
    )
    @classmethod
    def _exact_tuple_at_raw_validation(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("must be an exact tuple at raw validation")

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "PolicyEvaluationInput must validate from a mapping"
            )
        if info.mode == "json":
            return data
        expected = {
            "subject": ChangeSubject,
            "risk_result": RiskClassificationResult,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact "
                    f"{model_type.__name__} instance"
                )
        tuple_fields = (
            ("findings", Finding),
            ("execution_receipts", ExecutionReceipt),
            ("human_decisions", HumanDecision),
        )
        for field_name, model_type in tuple_fields:
            items = data.get(field_name, ())
            if type(items) is not tuple:
                raise ValueError(
                    f"{field_name} must be an exact tuple at raw validation"
                )
            for item in items:
                if type(item) is not model_type:
                    raise ValueError(
                        f"{field_name} items must be exact "
                        f"{model_type.__name__} instances"
                    )
        return data

    @model_validator(mode="after")
    def _require_exact_parsed_types(self) -> "PolicyEvaluationInput":
        if type(self.subject) is not ChangeSubject:
            raise ValueError("subject must be an exact ChangeSubject instance")
        if type(self.risk_result) is not RiskClassificationResult:
            raise ValueError(
                "risk_result must be an exact "
                "RiskClassificationResult instance"
            )
        for item in self.findings:
            if type(item) is not Finding:
                raise ValueError(
                    "findings items must be exact Finding instances"
                )
        for item in self.execution_receipts:
            if type(item) is not ExecutionReceipt:
                raise ValueError(
                    "execution_receipts items must be exact "
                    "ExecutionReceipt instances"
                )
        for item in self.human_decisions:
            if type(item) is not HumanDecision:
                raise ValueError(
                    "human_decisions items must be exact "
                    "HumanDecision instances"
                )
        return self

    @model_validator(mode="after")
    def _require_unique_ids(self) -> "PolicyEvaluationInput":
        finding_ids: set[str] = set()
        for item in self.findings:
            if item.finding_id in finding_ids:
                raise ValueError("finding_id must be unique")
            finding_ids.add(item.finding_id)
        receipt_ids: set[str] = set()
        for item in self.execution_receipts:
            if item.receipt_id in receipt_ids:
                raise ValueError("receipt_id must be unique")
            receipt_ids.add(item.receipt_id)
        decision_ids: set[str] = set()
        for item in self.human_decisions:
            if item.decision_id in decision_ids:
                raise ValueError("decision_id must be unique")
            decision_ids.add(item.decision_id)
        return self

    @model_validator(mode="after")
    def _require_time_bounds(self) -> "PolicyEvaluationInput":
        if self.evaluated_at < self.subject.created_at:
            raise ValueError("evaluated_at must be >= subject.created_at")
        manifest_evaluated_at = self.risk_result.input.manifest.evaluated_at
        if self.evaluated_at < manifest_evaluated_at:
            raise ValueError(
                "evaluated_at must be >= risk manifest evaluated_at"
            )
        for item in self.execution_receipts:
            if self.evaluated_at < item.completed_at:
                raise ValueError(
                    "evaluated_at must be >= every receipt completed_at"
                )
        for item in self.human_decisions:
            if self.evaluated_at < item.decided_at:
                raise ValueError(
                    "evaluated_at must be >= every human decided_at"
                )
        return self


def _validate_decision_grammar(decision: PolicyDecision) -> None:
    """校验 Gate 决策的严格语法（复用 PolicyDecision 合同之上的门规则）。"""
    order = _REASON_ORDER
    positions = {name: index for index, name in enumerate(order)}
    seen = set()
    previous = -1
    for reason in decision.reason_codes:
        if reason not in positions:
            raise ValueError("reason_codes must belong to the gate reason set")
        if reason in seen:
            raise ValueError("reason_codes must be unique")
        current = positions[reason]
        if current <= previous:
            raise ValueError("reason_codes must follow the global order")
        seen.add(reason)
        previous = current
    if decision.outcome in ("STALE", "BLOCKED", "NEEDS_HUMAN"):
        if not decision.reason_codes:
            raise ValueError("reason_codes is required for this outcome")
    elif decision.reason_codes:
        raise ValueError("PASS/PASS_WITH_WAIVER must not have reason_codes")
    if decision.outcome == "PASS_WITH_WAIVER":
        if decision.waiver_ref is None:
            raise ValueError("waiver_ref is required for PASS_WITH_WAIVER")
    elif decision.waiver_ref is not None:
        raise ValueError("waiver_ref is only allowed for PASS_WITH_WAIVER")
    if decision.outcome == "NEEDS_HUMAN":
        if decision.required_human_role is None:
            raise ValueError(
                "required_human_role is required for NEEDS_HUMAN"
            )


def _decision_id_from_data(data: dict) -> str:
    body = {
        key: value for key, value in data.items() if key != "decision_id"
    }
    envelope = {
        "subject_digest": data["subject_digest"],
        "rules_digest": data["rules_digest"],
        "decision_body": body,
    }
    return "policy_" + hashlib.sha256(
        _canonical_json_bytes(envelope)
    ).hexdigest()[:32]


def _stale_reasons(value: PolicyEvaluationInput) -> tuple[str, ...]:
    reasons = []
    subject_digest = value.subject.subject_digest
    if subject_digest != value.risk_result.classification.subject_digest:
        reasons.append("SUBJECT_DIGEST_MISMATCH")
    if any(
        item.subject_digest != subject_digest for item in value.findings
    ) or any(
        item.subject_digest != subject_digest
        for item in value.execution_receipts
    ) or any(
        item.subject_digest != subject_digest
        for item in value.human_decisions
    ):
        reasons.append("SUBJECT_DIGEST_MISMATCH")
    if any(item.status == "stale" for item in value.findings):
        reasons.append("FINDING_STALE")
    return tuple(dict.fromkeys(reasons))


def _blocked_reasons(value: PolicyEvaluationInput) -> tuple[str, ...]:
    reasons = []
    manifest = value.risk_result.input.manifest
    evaluated_at = value.evaluated_at
    boundary = value.risk_result.input.declarations.provider_boundary
    if boundary == "crosses_declared_boundary":
        reasons.append("PROVIDER_BOUNDARY_CROSSING")
    elif boundary == "unknown":
        reasons.append("PROVIDER_BOUNDARY_UNKNOWN")
    if manifest.completeness_status == "has_gaps":
        reasons.append("MANIFEST_HAS_GAPS")
    if any(entry.fresh_until is None for entry in manifest.entries):
        reasons.append("EVIDENCE_FRESHNESS_UNKNOWN")
    if any(
        entry.fresh_until is not None and evaluated_at > entry.fresh_until
        for entry in manifest.entries
    ):
        reasons.append("EVIDENCE_EXPIRED")

    collector_mapping = _RULES_TABLE["collector_mapping"]
    strict_coverage = _strict_coverage_enabled(value)
    expected_coverage = _expected_collector_bindings(value) if strict_coverage else {}
    if strict_coverage and any(
        entry.kind == "evidence_manifest"
        or entry.evidence_id == expected_coverage.get("evidence_manifest", {}).get("evidence_id")
        for entry in manifest.entries
    ):
        # The manifest is the closed view of the other Evidence; including it
        # as one of its own entries would make coverage recursive.
        reasons.append("REQUIRED_COLLECTOR_NOT_SUCCESS")
    for collector_name in value.risk_result.classification.required_collectors:
        mapping = collector_mapping[collector_name]
        if mapping is None:
            continue
        kind, producer = mapping
        matches = [
            entry
            for entry in manifest.entries
            if entry.kind == kind and entry.producer == producer
        ]
        if not matches:
            reasons.append("REQUIRED_COLLECTOR_MISSING")
            continue
        successes = [
            entry for entry in matches if entry.status == "success"
        ]
        if not successes:
            reasons.append("REQUIRED_COLLECTOR_NOT_SUCCESS")
            continue
        if strict_coverage:
            expected = expected_coverage.get(collector_name)
            if expected is not None and not _entry_matches_projection(
                successes[0], expected
            ):
                reasons.append("REQUIRED_COLLECTOR_NOT_SUCCESS")
                continue
        qualifying = [
            entry
            for entry in successes
            if entry.fresh_until is not None
            and evaluated_at <= entry.fresh_until
        ]
        if not qualifying:
            reasons.append("REQUIRED_COLLECTOR_NOT_FRESH")

    reviewer_schema = _RULES_TABLE["reviewer_schema_statuses"]
    steps = [
        step
        for receipt in value.execution_receipts
        for step in receipt.steps
    ]
    api_contract_preflight_blocked = any(
        step.result == "blocked"
        and step.fallback_reason == "api_contract_missing"
        for step in steps
    )
    for role in value.risk_result.classification.required_reviewers:
        if api_contract_preflight_blocked:
            continue
        role_steps = [step for step in steps if step.actual_role == role]
        if not role_steps:
            reasons.append("REQUIRED_REVIEWER_MISSING")
            continue
        if not any(
            step.result == "success" and step.schema_status in reviewer_schema
            for step in role_steps
        ):
            reasons.append("REQUIRED_REVIEWER_NOT_SUCCESS")

    manifest_ids = {entry.evidence_id for entry in manifest.entries}
    if any(
        ref not in manifest_ids
        for finding in value.findings
        for ref in finding.evidence_refs
    ):
        reasons.append("FINDING_EVIDENCE_REF_MISSING")

    blocking_severities = _RULES_TABLE["blocking_severities"]
    blocking_statuses = _RULES_TABLE["blocking_statuses"]
    if any(
        finding.basis == "deterministic"
        and finding.severity in blocking_severities
        and finding.status in blocking_statuses
        for finding in value.findings
    ):
        reasons.append("DETERMINISTIC_FINDING_BLOCKING")

    required_human_role = (
        value.risk_result.classification.required_human_role
    )
    if required_human_role is not None:
        subject_digest = value.subject.subject_digest
        decisions = [
            item
            for item in value.human_decisions
            if item.subject_digest == subject_digest
            and item.owner_role == required_human_role
        ]
        if decisions:
            latest = max(item.decided_at for item in decisions)
            latest_decisions = tuple(
                item for item in decisions if item.decided_at == latest
            )
            distinct_facts = {
                (item.decision, item.waiver_id, item.expires_at)
                for item in latest_decisions
            }
            if len(distinct_facts) == 1 and all(
                item.decision == "reject" for item in latest_decisions
            ):
                reasons.append("REQUIRED_HUMAN_REJECTED")
    unique_reasons = tuple(dict.fromkeys(reasons))
    reason_positions = {
        reason: index for index, reason in enumerate(_REASON_ORDER)
    }
    return tuple(sorted(unique_reasons, key=reason_positions.__getitem__))


def _current_role_decisions(
    value: PolicyEvaluationInput,
) -> tuple[HumanDecision, ...]:
    required_human_role = (
        value.risk_result.classification.required_human_role
    )
    if required_human_role is None:
        return ()
    subject_digest = value.subject.subject_digest
    return tuple(
        item
        for item in value.human_decisions
        if item.subject_digest == subject_digest
        and item.owner_role == required_human_role
    )


def _needs_human_reasons(value: PolicyEvaluationInput) -> tuple[str, ...]:
    required_human_role = (
        value.risk_result.classification.required_human_role
    )
    if required_human_role is None:
        return ()
    decisions = _current_role_decisions(value)
    if not decisions:
        return ("REQUIRED_HUMAN_MISSING",)
    latest = max(item.decided_at for item in decisions)
    latest_decisions = tuple(
        item for item in decisions if item.decided_at == latest
    )
    distinct_facts = {
        (item.decision, item.waiver_id, item.expires_at)
        for item in latest_decisions
    }
    conflict = len(distinct_facts) > 1
    expiry = any(
        item.decision == "approve_with_waiver"
        and value.evaluated_at >= item.expires_at
        for item in latest_decisions
    )
    reasons = []
    if expiry:
        reasons.append("REQUIRED_HUMAN_WAIVER_EXPIRED")
    if conflict:
        reasons.append("REQUIRED_HUMAN_CONFLICT")
    return tuple(reasons)


def _pass_or_waiver(
    value: PolicyEvaluationInput,
) -> tuple[str, str | None]:
    decisions = _current_role_decisions(value)
    if not decisions:
        return "PASS", None
    latest = max(item.decided_at for item in decisions)
    latest_decisions = tuple(
        item for item in decisions if item.decided_at == latest
    )
    distinct_facts = {
        (item.decision, item.waiver_id, item.expires_at)
        for item in latest_decisions
    }
    if len(distinct_facts) > 1:
        return "PASS", None
    effective = latest_decisions[0]
    if effective.decision == "approve_with_waiver":
        return "PASS_WITH_WAIVER", effective.waiver_id
    return "PASS", None


def _derive_outcome(
    value: PolicyEvaluationInput,
) -> tuple[str, tuple[str, ...], str | None]:
    stale = _stale_reasons(value)
    if stale:
        return "STALE", stale, None
    blocked = _blocked_reasons(value)
    if blocked:
        return "BLOCKED", blocked, None
    needed = _needs_human_reasons(value)
    if needed:
        return "NEEDS_HUMAN", needed, None
    outcome, waiver_ref = _pass_or_waiver(value)
    return outcome, (), waiver_ref


def _derive_decision(value: PolicyEvaluationInput) -> PolicyDecision:
    if type(value) is not PolicyEvaluationInput:
        raise TypeError("value must be an exact PolicyEvaluationInput")
    outcome, reason_codes, waiver_ref = _derive_outcome(value)
    decision_data = {
        "schema_version": "v1",
        "decision_id": "",
        "subject_digest": value.subject.subject_digest,
        "policy_version": value.subject.policy_version,
        "rules_digest": _RULES_DIGEST,
        "outcome": outcome,
        "reason_codes": reason_codes,
        "required_collectors": (
            value.risk_result.classification.required_collectors
        ),
        "required_reviewers": (
            value.risk_result.classification.required_reviewers
        ),
        "required_human_role": (
            value.risk_result.classification.required_human_role
        ),
        "evaluated_evidence_refs": tuple(
            sorted(
                {
                    entry.evidence_id
                    for entry in value.risk_result.input.manifest.entries
                }
            )
        ),
        "evaluated_finding_refs": tuple(
            sorted({item.finding_id for item in value.findings})
        ),
        "evaluated_receipt_refs": tuple(
            sorted({item.receipt_id for item in value.execution_receipts})
        ),
        "waiver_ref": waiver_ref,
        "evaluated_at": value.evaluated_at,
    }
    provisional = PolicyDecision.model_construct(**decision_data)
    decision_data["decision_id"] = _decision_id_from_data(
        provisional.model_dump(mode="json")
    )
    decision = PolicyDecision(**decision_data)
    _validate_decision_grammar(decision)
    return decision


class PolicyGateResult(BaseModel):
    """策略门完整结果：输入与同一纯派生决策必须一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: PolicyEvaluationInput
    decision: PolicyDecision

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(
        cls, data: object, info: ValidationInfo
    ) -> object:
        if not isinstance(data, dict):
            raise ValueError("PolicyGateResult must validate from a mapping")
        if info.mode == "json":
            return data
        if type(data.get("input")) is not PolicyEvaluationInput:
            raise ValueError(
                "input must be an exact PolicyEvaluationInput instance"
            )
        if type(data.get("decision")) is not PolicyDecision:
            raise ValueError(
                "decision must be an exact PolicyDecision instance"
            )
        return data

    @model_validator(mode="after")
    def _require_derived_decision(self) -> "PolicyGateResult":
        _validate_decision_grammar(self.decision)
        derived = _derive_decision(self.input)
        if self.decision != derived:
            raise ValueError("decision must equal the derived decision")
        return self


class PolicyGate:
    """纯确定性策略门：无状态、无配置、无 I/O。"""

    @staticmethod
    def evaluate(value: PolicyEvaluationInput) -> PolicyGateResult:
        if type(value) is not PolicyEvaluationInput:
            raise TypeError("value must be an exact PolicyEvaluationInput")
        return PolicyGateResult(
            schema_version="v1",
            input=value,
            decision=_derive_decision(value),
        )
