"""Deterministic, offline assurance workbench demo seed."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from assurance.contracts import (
    AcceptanceCase,
    Evidence,
    ExecutionReceipt,
    ExecutionStep,
    Finding,
    HumanDecision,
    PolicyDecision,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_store import AssuranceWebNotFoundError, AssuranceWebRepository


DEMO_NAME = "change-assurance-offline-demo-v1"
OLD_CASE_ID = "assurance-demo-old-digest"
NEW_CASE_ID = "assurance-demo-new-digest"
EVIDENCE_LEVEL = "deterministic_offline_demo"
_UTC = timezone.utc
_T0 = datetime(2026, 8, 25, 0, 0, tzinfo=_UTC)
_ROLES = ("intent", "architecture", "operability")
_RISK_FINDINGS = (
    ("provider-boundary", "provider boundary breach", "architecture", "critical", 1),
    ("hardcoded-fallback", "hardcoded fallback", "architecture", "high", 1),
    ("retry-side-effect", "side-effect retry non-idempotent", "operability", "high", 1),
    ("cost-cap", "no cost cap", "operability", "high", 1),
    ("fallback-trace", "no fallback trace", "operability", "medium", 2),
    ("kill-switch", "no kill switch", "intent", "high", 1),
    ("owner-adr", "no owner/ADR", "architecture", "high", 2),
    ("scope-creep", "scope creep", "intent", "medium", 1),
)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _metadata(case_id: str, *, old: bool) -> dict[str, Any]:
    return {
        "change_id": case_id,
        "title": "Offline assurance demo old digest" if old else "Offline assurance demo repaired digest",
        "summary": (
            "Fixed offline fixture with an old subject digest and governance findings."
            if old
            else "Fixed offline fixture after a repair with a fresh subject digest."
        ),
        "owner": "release-owner",
        "owner_role": "release_owner",
        "author": "demo-author",
        "risk": "critical" if old else "high",
        "priority": 90 if old else 80,
        "value": 70 if old else 85,
        "release_status": "blocked" if old else "awaiting_release_owner",
        "policy_version": "change-assurance-policy-v1",
        "rubric_version": "change-assurance-rubric-v1",
        "intent_coverage": "scope and acceptance boundary",
        "architecture_impact": "provider and routing boundary",
        "operational_readiness": "retry, cost, telemetry and ownership",
        "knowledge_notes": "deterministic local demo; no external services",
        "ownership_notes": "release-owner approval is separate from author",
        "evidence_level": EVIDENCE_LEVEL,
        "external_services": False,
        "demo_name": DEMO_NAME,
    }


def _evidence(case_id: str, subject_digest: str, *, old: bool) -> tuple[Evidence, ...]:
    variant = "old" if old else "repaired"
    labels = (
        ("builder-green", "builder_green_test", "offline-builder"),
        ("diff", "diff", "offline-diff-collector"),
        ("author-receipt", "author_agent_receipt", "offline-author-agent"),
    )
    return tuple(
        Evidence(
            evidence_id=f"{case_id}:evidence:{slug}",
            subject_digest=subject_digest,
            kind=kind,
            producer=producer,
            artifact_digest=_digest(f"{DEMO_NAME}:{variant}:{case_id}:{slug}"),
            source_ref=f"demo://{case_id}/{variant}/{slug}",
            trace_id=f"trace:{case_id}:{slug}",
            status="success",
            trust_level="deterministic",
            collected_at=_at((1 if old else 61) + index),
        )
        for index, (slug, kind, producer) in enumerate(labels)
    )


def _findings(case_id: str, subject_digest: str, evidence: tuple[Evidence, ...], *, old: bool) -> tuple[Finding, ...]:
    status = "open" if old else "resolved"
    rubric_hash = _digest(f"{DEMO_NAME}:rubric:v1")
    return tuple(
        Finding(
            finding_id=f"{case_id}:finding:{slug}",
            subject_digest=subject_digest,
            reviewer_role=role,
            claim=claim,
            evidence_refs=(evidence[evidence_index].evidence_id,),
            basis="deterministic",
            severity=severity,
            confidence=1.0,
            rubric_hash=rubric_hash,
            model_ref="deterministic-offline-demo",
            status=status,
        )
        for slug, claim, role, severity, evidence_index in _RISK_FINDINGS
    )


def _receipt(case_id: str, subject_digest: str, *, old: bool) -> ExecutionReceipt:
    start = _at(2 if old else 62)
    end = _at(3 if old else 63)
    steps = tuple(
        ExecutionStep(
            sequence=index,
            planned_role=role,
            actual_role=role,
            model_ref="deterministic-offline-demo",
            provider="offline-local",
            tool_grants=(),
            routing_rule=f"{DEMO_NAME}:{role}",
            token_budget=0,
            timeout_seconds=1,
            result="success",
            schema_status="valid",
        )
        for index, role in enumerate(_ROLES)
    )
    return ExecutionReceipt(
        receipt_id=f"{case_id}:receipt:review",
        run_id=f"{case_id}:run:review",
        subject_digest=subject_digest,
        steps=steps,
        overall_result="success",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        started_at=start,
        completed_at=end,
    )


def _policy(
    case_id: str,
    subject_digest: str,
    evidence: tuple[Evidence, ...],
    findings: tuple[Finding, ...],
    receipt: ExecutionReceipt,
    *,
    old: bool,
) -> PolicyDecision:
    return PolicyDecision(
        decision_id=f"{case_id}:decision:policy",
        subject_digest=subject_digest,
        policy_version="change-assurance-policy-v1",
        rules_digest=_digest(f"{DEMO_NAME}:rules:{'old' if old else 'repaired'}"),
        outcome="BLOCKED" if old else "NEEDS_HUMAN",
        reason_codes=(
            ("GOVERNANCE_FINDINGS_BLOCK", "DIGEST_REPAIR_REQUIRED")
            if old
            else ("HIGH_RISK_ROUTING_CHANGE", "RELEASE_OWNER_REQUIRED")
        ),
        required_collectors=(),
        required_reviewers=_ROLES,
        required_human_role=None if old else "release_owner",
        evaluated_evidence_refs=tuple(item.evidence_id for item in evidence),
        evaluated_finding_refs=tuple(item.finding_id for item in findings),
        evaluated_receipt_refs=(receipt.receipt_id,),
        evaluated_at=_at(4 if old else 64),
    )


def _spec(case_id: str, *, old: bool) -> dict[str, Any]:
    subject_digest = _digest(f"{DEMO_NAME}:{case_id}:subject")
    evidence = _evidence(case_id, subject_digest, old=old)
    findings = _findings(case_id, subject_digest, evidence, old=old)
    receipt = _receipt(case_id, subject_digest, old=old)
    policy = _policy(case_id, subject_digest, evidence, findings, receipt, old=old)
    human = None
    if not old:
        human = HumanDecision(
            decision_id=f"{case_id}:decision:human",
            subject_digest=subject_digest,
            owner="release-owner",
            owner_role="release_owner",
            decision="approve",
            reason="release owner approved the high-risk routing change",
            decided_at=_at(65),
        )
    created_at = _at(0 if old else 60)
    final_at = _at(6 if old else 65)
    case = AcceptanceCase(
        case_id=case_id,
        subject_digest=subject_digest,
        state="DRAFT",
        created_at=created_at,
        updated_at=created_at,
    )
    binding = AcceptanceBinding(
        subject_digest=subject_digest,
        policy_version="change-assurance-policy-v1",
        rubric_version="change-assurance-rubric-v1",
    )
    return {
        "case": case,
        "binding": binding,
        "metadata": _metadata(case_id, old=old),
        "evidence": evidence,
        "findings": findings,
        "receipt": receipt,
        "policy": policy,
        "human": human,
        "old": old,
        "created_at": created_at,
        "final_at": final_at,
        "conflict": "adjudicator conflict: intent scope boundary conflicts with architecture migration prerequisite",
        "invalidation_reason": "repair produced a new subject digest",
    }


def _specs() -> tuple[dict[str, Any], dict[str, Any]]:
    return _spec(OLD_CASE_ID, old=True), _spec(NEW_CASE_ID, old=False)


def _model_json(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _payload(*models: Any, **extra: Any) -> dict[str, Any]:
    payload = {"models": [_model_json(model) for model in models]}
    payload.update(extra)
    return payload


def _create_spec(repository: AssuranceWebRepository, spec: dict[str, Any]) -> None:
    case = spec["case"]
    case_id = case.case_id
    digest = case.subject_digest
    repository.create_change(
        case,
        spec["binding"],
        spec["metadata"],
        f"{DEMO_NAME}:{case_id}:create",
        {"case": _model_json(case), "binding": _model_json(spec["binding"])},
    )
    for index, evidence in enumerate(spec["evidence"]):
        event = AcceptanceEvent(
            event_id=f"{case_id}:event:collect:{index}",
            subject_digest=digest,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            occurred_at=evidence.collected_at,
        )
        repository.collect(
            case_id,
            event,
            evidence,
            f"{DEMO_NAME}:{case_id}:collect:{index}",
            _payload(event, evidence),
        )
    receipt = spec["receipt"]
    policy = spec["policy"]
    review_event = AcceptanceEvent(
        event_id=f"{case_id}:event:review",
        subject_digest=digest,
        kind="COLLECT_EVIDENCE",
        evidence_refs=tuple(item.evidence_id for item in spec["evidence"]),
        finding_refs=tuple(item.finding_id for item in spec["findings"]),
        execution_receipt_refs=(receipt.receipt_id,),
        policy_decision_refs=(policy.decision_id,),
        occurred_at=policy.evaluated_at,
    )
    repository.review(
        case_id,
        review_event,
        spec["findings"],
        receipt,
        policy,
        f"{DEMO_NAME}:{case_id}:review",
        _payload(review_event, receipt, policy, *spec["findings"]),
    )
    if spec["old"]:
        conflict_event = AcceptanceEvent(
            event_id=f"{case_id}:event:conflict",
            subject_digest=digest,
            kind="RECORD_CONFLICT",
            conflicts=(spec["conflict"],),
            occurred_at=_at(5),
        )
        repository.collect(
            case_id,
            conflict_event,
            None,
            f"{DEMO_NAME}:{case_id}:conflict",
            _payload(conflict_event),
        )
        invalidate_event = AcceptanceEvent(
            event_id=f"{case_id}:event:invalidate",
            subject_digest=digest,
            kind="INVALIDATE",
            reason=spec["invalidation_reason"],
            occurred_at=_at(6),
        )
        repository.collect(
            case_id,
            invalidate_event,
            None,
            f"{DEMO_NAME}:{case_id}:invalidate",
            _payload(invalidate_event),
        )
    else:
        human = spec["human"]
        accept_event = AcceptanceEvent(
            event_id=f"decision:{human.decision_id}",
            subject_digest=digest,
            kind="ACCEPT",
            policy_decision_refs=(policy.decision_id,),
            human_decision_refs=(human.decision_id,),
            occurred_at=human.decided_at,
        )
        repository.decide(
            case_id,
            human,
            accept_event,
            f"{DEMO_NAME}:{case_id}:approve",
            _payload(accept_event, human),
        )


def _expected_case_refs(spec: dict[str, Any]) -> dict[str, Any]:
    case = spec["case"]
    evidence = spec["evidence"]
    findings = spec["findings"]
    receipt = spec["receipt"]
    policy = spec["policy"]
    human = spec["human"]
    return {
        "case_id": case.case_id,
        "subject_digest": case.subject_digest,
        "state": "INVALIDATED" if spec["old"] else "ACCEPTED",
        "created_at": _iso(spec["created_at"]),
        "updated_at": _iso(spec["final_at"]),
        "evidence_refs": [item.evidence_id for item in evidence],
        "finding_refs": [item.finding_id for item in findings],
        "execution_receipt_refs": [receipt.receipt_id],
        "policy_decision_refs": [policy.decision_id],
        "human_decision_refs": [] if human is None else [human.decision_id],
        "conflicts": [spec["conflict"]] if spec["old"] else [],
        "invalidation_reason": spec["invalidation_reason"] if spec["old"] else None,
    }


def _matches_demo_projection(projection: dict[str, Any], spec: dict[str, Any]) -> bool:
    expected_case = _expected_case_refs(spec)
    actual_case = projection["case"]
    case_keys = tuple(expected_case)
    if {key: actual_case.get(key) for key in case_keys} != expected_case:
        return False
    if projection["binding"] != _model_json(spec["binding"]):
        return False
    if projection["metadata"] != spec["metadata"]:
        return False
    if projection["evidence"] != [_model_json(item) for item in spec["evidence"]]:
        return False
    actual_findings = [
        {key: value for key, value in item.items() if key != "evidence_status"}
        for item in projection["findings"]
    ]
    if actual_findings != [_model_json(item) for item in spec["findings"]]:
        return False
    if projection["receipt"] != _model_json(spec["receipt"]):
        return False
    expected_decisions = [{"kind": "policy", **_model_json(spec["policy"])}]
    if spec["human"] is not None:
        expected_decisions.append({"kind": "human", **_model_json(spec["human"])})
    return (
        projection["decisions"] == expected_decisions
        and projection["digest_freshness"] is True
        and projection["gate"] == expected_case["state"]
    )


def _timeline_labels(projection: dict[str, Any], *, old: bool) -> list[str]:
    labels: list[str] = []
    for item in projection["timeline"]:
        if item["type"] == "event" and item["kind"] == "COLLECT_EVIDENCE":
            label = "review" if item["finding_refs"] else "collect"
        elif item["type"] == "event" and item["kind"] == "RECORD_CONFLICT":
            label = "conflict"
        elif item["type"] == "event" and item["kind"] == "INVALIDATE":
            label = "invalidate"
        elif item["type"] == "event" and item["kind"] == "ACCEPT":
            label = "accept"
        else:
            continue
        if label not in labels:
            labels.append(label)
    if not old and "accept" not in labels:
        labels.append("accept")
    return labels


def _summary_case(repository: AssuranceWebRepository, spec: dict[str, Any]) -> dict[str, Any]:
    projection = repository.get_change(spec["case"].case_id)
    if not _matches_demo_projection(projection, spec):
        raise ValueError(f"existing demo case {spec['case'].case_id!r} does not match")
    passport_available = False
    if not spec["old"]:
        repository.get_passport(spec["case"].case_id)
        passport_available = True
    receipt = projection["receipt"]
    return {
        "case_id": projection["case"]["case_id"],
        "subject_digest": projection["case"]["subject_digest"],
        "state": projection["case"]["state"],
        "gate": projection["gate"],
        "digest_freshness": projection["digest_freshness"],
        "evidence_ids": [item["evidence_id"] for item in projection["evidence"]],
        "evidence_count": len(projection["evidence"]),
        "finding_ids": [item["finding_id"] for item in projection["findings"]],
        "finding_claims": [item["claim"] for item in projection["findings"]],
        "finding_count": len(projection["findings"]),
        "receipt_ids": [] if receipt is None else [receipt["receipt_id"]],
        "receipt_count": 0 if receipt is None else 1,
        "receipt_roles": [] if receipt is None else [step["planned_role"] for step in receipt["steps"]],
        "policy_outcomes": [item["outcome"] for item in projection["decisions"] if item["kind"] == "policy"],
        "human_decisions": [item["decision"] for item in projection["decisions"] if item["kind"] == "human"],
        "timeline_labels": _timeline_labels(projection, old=spec["old"]),
        "timeline": projection["timeline"],
        "passport_available": passport_available,
    }


def seed_assurance_demo(db_path: Path) -> dict[str, Any]:
    """Seed exactly two deterministic demo cases into the supplied SQLite DB."""

    if not isinstance(db_path, Path):
        raise TypeError("db_path must be a pathlib.Path")
    if db_path.exists() and not db_path.is_file():
        raise ValueError("db_path must be a file path")
    repository = AssuranceWebRepository(db_path)
    repository.initialize()
    specs = _specs()
    existing: dict[str, dict[str, Any] | None] = {}
    for spec in specs:
        case_id = spec["case"].case_id
        try:
            projection = repository.get_change(case_id)
        except AssuranceWebNotFoundError:
            projection = None
        existing[case_id] = projection
        if projection is not None and not _matches_demo_projection(projection, spec):
            raise ValueError(f"existing demo case {case_id!r} does not match")
    for spec in specs:
        if existing[spec["case"].case_id] is None:
            _create_spec(repository, spec)
    cases = [_summary_case(repository, spec) for spec in specs]
    return {
        "demo_name": DEMO_NAME,
        "evidence_level": EVIDENCE_LEVEL,
        "external_services": False,
        "case_count": len(cases),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the offline assurance demo")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".codemesh" / "assurance.sqlite",
        help="SQLite path (default: ~/.codemesh/assurance.sqlite)",
    )
    summary = seed_assurance_demo(parser.parse_args(argv).db)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
