"""V2-P4-06 Model Routing v1 focused TDD tests.

The module under test is pure and deterministic: it performs no provider
calls, no environment/config reads, no I/O, and its ``selected`` outcome is
only a routing outcome, never a PASS/Gate/approval/acceptance signal.
"""

import ast
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import assurance
import assurance.model_routing as model_routing_module
from assurance import (
    ModelCandidate,
    ModelRouteAttempt,
    ModelRouteBudget,
    ModelRouteDecision,
    ModelRouteMatch,
    ModelRouteRequest,
    ModelRouteRule,
    ModelRoutingPolicy,
    ModelRouteTarget,
    ModelRouter,
    ModelTierAlias,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
    RiskDeclarations,
)
from assurance.intake import IntakeSnapshot
from assurance.manifest import EvidenceManifest, EvidenceManifestEntry
from assurance.snapshot import GitChange, GitSnapshot


FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)
LATER_TIME = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc)

NEW_PUBLIC_NAMES = frozenset(
    {
        "ModelCandidate",
        "ModelTierAlias",
        "ModelRouteMatch",
        "ModelRouteTarget",
        "ModelRouteRule",
        "ModelRoutingPolicy",
        "ModelRouteBudget",
        "ModelRouteRequest",
        "ModelRouteAttempt",
        "ModelRouteDecision",
        "ModelRouter",
    }
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def _change(path, **overrides):
    values = {
        "schema_version": "v1",
        "path": path,
        "old_path": None,
        "status": "added",
        "current_size": 1,
        "current_digest": _digest("b"),
        "binary": False,
        "large_file": False,
        "submodule": False,
    }
    values.update(overrides)
    return GitChange.model_validate(values)


def _git_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "repository": "acme/service",
        "base_revision": "a" * 40,
        "head_revision": "b" * 40,
        "scope": "base_to_worktree",
        "worktree_dirty": False,
        "changes": (_change("a.txt"),),
        "changed_files_total": 1,
        "diff_artifact_digest": _digest("d"),
        "diff_bytes": 0,
        "diff_truncated": False,
        "files_truncated": False,
        "ignored_files_lower_bound": 0,
        "ignored_scan_truncated": False,
        "large_file_paths": (),
        "submodule_paths": (),
        "omissions": (),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    if "changes" in overrides and "changed_files_total" not in overrides:
        values["changed_files_total"] = len(values["changes"])
    return GitSnapshot.model_validate(values)


def _intake_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    documents = overrides.get("documents", ())
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "documents": documents,
        "notices": (),
        "task_digest": None,
        "task_present": False,
        "policy_count": sum(
            document.kind == "policy" for document in documents
        ),
        "adr_count": sum(document.kind == "adr" for document in documents),
        "runbook_count": sum(
            document.kind == "runbook" for document in documents
        ),
        "manifest_artifact_digest": _digest("a"),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return IntakeSnapshot.model_validate(values)


def _declarations(**overrides):
    values = {
        "changed_lines_total": 0,
        "external_side_effects": "none_declared",
        "provider_boundary": "within_declared_boundary",
    }
    values.update(overrides)
    return RiskDeclarations.model_validate(values)


def _manifest(subject_digest=None, *, evaluated_at=FIXED_TIME):
    if subject_digest is None:
        subject_digest = _digest("c")
    entry = EvidenceManifestEntry.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": "ev-a",
            "kind": "git_snapshot",
            "trust_level": "observed",
            "producer": "collector.git_snapshot",
            "subject_digest": subject_digest,
            "artifact_digest": _digest("a"),
            "source_ref": "collector.git_snapshot:0",
            "status": "success",
            "collected_at": FIXED_TIME,
            "fresh_until": LATER_TIME,
            "freshness": (
                "fresh" if evaluated_at <= LATER_TIME else "stale"
            ),
            "redaction_status": "not_applicable",
        }
    )
    values = {
        "schema_version": "v1",
        "manifest_id": "em_" + "0" * 32,
        "subject_digest": subject_digest,
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "entries": (entry.model_dump(mode="json"),),
        "evidence_count": 1,
        "completeness_status": "complete",
        "has_incomplete_evidence": False,
        "has_stale_evidence": False,
        "has_unknown_freshness": False,
        "has_unredacted_content": False,
        "has_unassessed_redaction": False,
        "canonical_digest": _digest("0"),
        "artifact_digest": _digest("0"),
    }
    body = {
        key: value
        for key, value in values.items()
        if key not in ("manifest_id", "canonical_digest", "artifact_digest")
    }
    digest = _sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    values["canonical_digest"] = digest
    values["artifact_digest"] = digest
    values["manifest_id"] = "em_" + hashlib.sha256(
        (subject_digest + digest).encode("utf-8")
    ).hexdigest()[:32]
    return EvidenceManifest.model_validate(values)


def _risk_result(subject_digest=None):
    if subject_digest is None:
        subject_digest = _digest("c")
    return RiskClassifier.classify(
        RiskClassificationInput.model_validate(
            {
                "schema_version": "v1",
                "snapshot": _git_snapshot(subject_digest),
                "intake": _intake_snapshot(subject_digest),
                "manifest": _manifest(subject_digest),
                "declarations": _declarations(),
            }
        )
    )


def _risk_result_high():
    subject_digest = _digest("c")
    return RiskClassifier.classify(
        RiskClassificationInput.model_validate(
            {
                "schema_version": "v1",
                "snapshot": _git_snapshot(subject_digest),
                "intake": _intake_snapshot(subject_digest),
                "manifest": _manifest(subject_digest),
                "declarations": _declarations(
                    provider_boundary="crosses_declared_boundary"
                ),
            }
        )
    )


def _candidate(candidate_id, provider_ref, model_ref, required_provider_boundary):
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_ref=provider_ref,
        model_ref=model_ref,
        required_provider_boundary=required_provider_boundary,
    )


def _alias(alias, candidates=None):
    if candidates is None:
        candidates = (
            _candidate(
                "candidate-standard",
                "approved-provider",
                "approved-model",
                "approved-provider",
            ),
        )
    return ModelTierAlias(alias=alias, candidates=candidates)


def _match(**overrides):
    values = {}
    values.update(overrides)
    return ModelRouteMatch(**values)


def _target(
    tier_alias="standard", token_budget_cap=1000, cost_budget_cap_usd=2.0
):
    return ModelRouteTarget(
        tier_alias=tier_alias,
        token_budget_cap=token_budget_cap,
        cost_budget_cap_usd=cost_budget_cap_usd,
    )


def _rule(rule_id, match=None, target=None):
    if match is None:
        match = _match()
    if target is None:
        target = _target()
    return ModelRouteRule(rule_id=rule_id, match=match, target=target)


def _default_aliases():
    return (
        _alias(
            "alternate-strong",
            (
                _candidate(
                    "candidate-alt",
                    "remote-provider",
                    "alt-model",
                    "any",
                ),
            ),
        ),
        _alias(
            "light-or-local",
            (
                _candidate(
                    "candidate-light",
                    "local-provider",
                    "local-model",
                    "local-only",
                ),
            ),
        ),
        _alias(
            "standard",
            (
                _candidate(
                    "candidate-standard",
                    "approved-provider",
                    "approved-model",
                    "approved-provider",
                ),
            ),
        ),
        _alias(
            "strong",
            (
                _candidate(
                    "candidate-strong",
                    "remote-provider",
                    "strong-model",
                    "approved-provider",
                ),
            ),
        ),
    )


def _default_policy(enabled=True, rules=None, aliases=None):
    if aliases is None:
        aliases = _default_aliases()
    if rules is None:
        rules = (_rule("rule-catch-all"),)
    return ModelRoutingPolicy(
        enabled=enabled, rules=rules, aliases=aliases
    )


def _budget(token_budget_cap=1000, cost_budget_cap_usd=2.0):
    return ModelRouteBudget(
        token_budget_cap=token_budget_cap,
        cost_budget_cap_usd=cost_budget_cap_usd,
    )


def _request(
    risk_result=None,
    *,
    phase="extraction",
    agent_role=None,
    task_role=None,
    effective_risk=None,
    risk_upgrade_reason=None,
    priority="normal",
    provider_boundary="any",
    available_candidate_ids=None,
    allowed_provider_refs=None,
    budget=None,
):
    if risk_result is None:
        risk_result = _risk_result()
    if effective_risk is None:
        effective_risk = risk_result.classification.risk_level
    if available_candidate_ids is None:
        available_candidate_ids = (
            "candidate-alt",
            "candidate-light",
            "candidate-standard",
            "candidate-strong",
        )
    if allowed_provider_refs is None:
        allowed_provider_refs = (
            "approved-provider",
            "local-provider",
            "remote-provider",
        )
    if budget is None:
        budget = _budget()
    return ModelRouteRequest(
        schema_version="v1",
        risk_result=risk_result,
        phase=phase,
        agent_role=agent_role,
        task_role=task_role,
        effective_risk=effective_risk,
        risk_upgrade_reason=risk_upgrade_reason,
        priority=priority,
        provider_boundary=provider_boundary,
        available_candidate_ids=available_candidate_ids,
        allowed_provider_refs=allowed_provider_refs,
        budget=budget,
    )


def _decision_data(decision, **overrides):
    data = {
        "schema_version": "v1",
        "policy": decision.policy,
        "request": decision.request,
        "outcome": decision.outcome,
        "block_reason": decision.block_reason,
        "matched_rule_id": decision.matched_rule_id,
        "matched_tier_alias": decision.matched_tier_alias,
        "attempts": decision.attempts,
        "selected_candidate": decision.selected_candidate,
        "allocated_budget": decision.allocated_budget,
        "decision_id": decision.decision_id,
    }
    data.update(overrides)
    return data


# ── Scenario 1: contracts, JSON round trip, deep immutability, digest ──


def test_contracts_are_strict_immutable_and_json_round_trippable():
    candidate = _candidate(
        "candidate-x", "p1", "m1", "approved-provider"
    )
    assert (
        ModelCandidate.model_validate_json(
            candidate.model_dump_json()
        )
        == candidate
    )

    alias = _alias("standard", (candidate,))
    assert ModelTierAlias.model_validate_json(alias.model_dump_json()) == alias

    match = _match(phase="review", agent_role="intent", priority="high")
    assert ModelRouteMatch.model_validate_json(match.model_dump_json()) == match

    target = _target("strong", token_budget_cap=2500, cost_budget_cap_usd=3.5)
    assert ModelRouteTarget.model_validate_json(target.model_dump_json()) == target

    rule = _rule("rule-1", match, target)
    assert ModelRouteRule.model_validate_json(rule.model_dump_json()) == rule

    budget = _budget(600, 1.25)
    assert ModelRouteBudget.model_validate_json(budget.model_dump_json()) == budget

    policy = _default_policy()
    assert (
        ModelRoutingPolicy.model_validate_json(policy.model_dump_json())
        == policy
    )

    with pytest.raises(ValidationError):
        policy.aliases[0].alias = "changed"
    with pytest.raises(ValidationError):
        policy.rules[0].match.phase = "review"
    with pytest.raises(ValidationError):
        policy.rules = (policy.rules[0],)
    with pytest.raises(ValidationError):
        policy.aliases[0].candidates = policy.aliases[0].candidates


def test_policy_digest_is_derived_stable_and_anti_forgery():
    first = _default_policy()
    second = _default_policy()
    assert first.policy_digest == second.policy_digest
    assert first.policy_digest == model_routing_module._policy_digest(first)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first.policy_digest)

    explicit = ModelRoutingPolicy(
        enabled=True,
        rules=first.rules,
        aliases=first.aliases,
        policy_digest=first.policy_digest,
    )
    assert explicit == first

    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=first.rules,
            aliases=first.aliases,
            policy_digest="sha256:" + "0" * 64,
        )

    reordered = _default_policy(
        rules=(
            _rule("rule-extraction", _match(phase="extraction")),
            _rule("rule-adjudication", _match(phase="adjudication")),
        )
    )
    opposite = _default_policy(
        rules=(
            _rule("rule-adjudication", _match(phase="adjudication")),
            _rule("rule-extraction", _match(phase="extraction")),
        )
    )
    assert reordered.policy_digest != opposite.policy_digest


def test_exact_tuples_and_exact_nested_instances_required():
    candidate = _candidate("candidate-x", "p1", "m1", "local-only")
    with pytest.raises(ValidationError):
        ModelTierAlias(alias="standard", candidates=[candidate])

    alias = _alias("standard", (candidate,))
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True, rules=[_rule("r")], aliases=(alias,)
        )

    with pytest.raises(ValidationError):
        ModelRouteRule(
            rule_id="rule-1",
            match={"phase": "review"},
            target=_target(),
        )
    with pytest.raises(ValidationError):
        ModelRouteRule(
            rule_id="rule-1",
            match=_match(),
            target={"tier_alias": "standard"},
        )

    with pytest.raises(ValidationError):
        ModelRouteRequest(
            schema_version="v1",
            risk_result={"schema_version": "v1"},
            phase="extraction",
            effective_risk="low",
            priority="normal",
            provider_boundary="any",
            available_candidate_ids=(),
            allowed_provider_refs=(),
            budget=_budget(),
        )
    with pytest.raises(ValidationError):
        _request(available_candidate_ids=["candidate-light"])


def test_subclassed_risk_result_is_rejected():
    class SubRiskResult(RiskClassificationResult):
        pass

    original = _risk_result()
    sub = SubRiskResult.model_validate(
        {
            "schema_version": "v1",
            "input": original.input,
            "classification": original.classification,
        }
    )
    assert type(sub) is SubRiskResult
    with pytest.raises(ValidationError):
        _request(risk_result=sub)


# ── Scenario 2: first match wins across every dimension ──


def _matrix_policy():
    aliases = _default_aliases()
    rules = (
        _rule("r-phase", _match(phase="adjudication"), _target("strong")),
        _rule("r-agent", _match(agent_role="architecture"), _target("standard")),
        _rule("r-risk", _match(effective_risk="critical"), _target("strong")),
        _rule("r-priority", _match(priority="critical"), _target("alternate-strong")),
        _rule("r-boundary", _match(provider_boundary="local-only"), _target("light-or-local")),
        _rule("r-task", _match(task_role="docs"), _target("standard")),
        _rule("r-catch-all", _match(), _target("standard")),
    )
    return ModelRoutingPolicy(enabled=True, rules=rules, aliases=aliases)


def test_first_match_wins_across_dimensions():
    policy = _matrix_policy()

    cases = (
        (_request(phase="adjudication"), "r-phase"),
        (
            _request(phase="review", agent_role="architecture"),
            "r-agent",
        ),
        (
            _request(
                risk_result=_risk_result_high(),
                effective_risk="critical",
                risk_upgrade_reason="adversarial escalation",
            ),
            "r-risk",
        ),
        (_request(priority="critical"), "r-priority"),
        (_request(provider_boundary="local-only"), "r-boundary"),
        (_request(task_role="docs"), "r-task"),
        (
            _request(phase="synthesis", effective_risk="low"),
            "r-catch-all",
        ),
    )
    for request, expected_rule in cases:
        decision = ModelRouter.route(policy, request)
        assert decision.outcome == "selected"
        assert decision.matched_rule_id == expected_rule


def test_first_rule_wins_when_multiple_dimensions_match():
    policy = _matrix_policy()

    adjudication_high = ModelRouter.route(
        policy,
        _request(
            phase="adjudication",
            priority="critical",
        ),
    )
    assert adjudication_high.matched_rule_id == "r-phase"

    architecture_critical = ModelRouter.route(
        policy,
        _request(
            phase="review",
            agent_role="architecture",
            risk_result=_risk_result_high(),
            effective_risk="critical",
            risk_upgrade_reason="adversarial escalation",
        ),
    )
    assert architecture_critical.matched_rule_id == "r-agent"


# ── Scenario 3: duplicate and shadowed rules ──


def test_duplicate_matches_rejected():
    aliases = _default_aliases()
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(
                _rule("r1", _match(phase="review")),
                _rule("r2", _match(phase="review")),
            ),
            aliases=aliases,
        )
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r1"), _rule("r2")),
            aliases=aliases,
        )


def test_broader_earlier_rule_shadows_later_rule():
    aliases = _default_aliases()
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(
                _rule("r-broad", _match(phase="review")),
                _rule(
                    "r-narrow",
                    _match(phase="review", agent_role="intent"),
                ),
            ),
            aliases=aliases,
        )


def test_catch_all_only_valid_as_last_reachable_rule():
    aliases = _default_aliases()
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(
                _rule("r-catch", _match()),
                _rule("r-phase", _match(phase="review")),
            ),
            aliases=aliases,
        )
    valid = ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule("r-phase", _match(phase="review")),
            _rule("r-catch", _match()),
        ),
        aliases=aliases,
    )
    assert valid.rules[-1].rule_id == "r-catch"


def test_rule_ids_aliases_and_candidates_uniqueness():
    aliases = _default_aliases()
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(
                _rule("same-id", _match(phase="review")),
                _rule("same-id", _match(phase="extraction")),
            ),
            aliases=aliases,
        )

    standard = _alias(
        "standard",
        (_candidate("candidate-x", "p1", "m1", "local-only"),),
    )
    strong = _alias(
        "strong",
        (_candidate("candidate-x", "p2", "m2", "local-only"),),
    )
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r", _match(), _target("standard")),),
            aliases=(standard, strong),
        )

    standard = _alias(
        "standard",
        (_candidate("candidate-x", "p1", "m1", "local-only"),),
    )
    strong = _alias(
        "strong",
        (_candidate("candidate-y", "p1", "m1", "local-only"),),
    )
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r", _match(), _target("standard")),),
            aliases=(standard, strong),
        )


def test_aliases_must_be_canonical_sorted_and_unknown_alias_rejected():
    light = _alias("light-or-local")
    standard = _alias("standard")
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r", _match(), _target("light-or-local")),),
            aliases=(standard, light),
        )
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r", _match(), _target("light-or-local")),),
            aliases=(light, light),
        )
    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=True,
            rules=(_rule("r", _match(), _target("missing-tier")),),
            aliases=(light, standard),
        )


# ── Scenario 4: tier alias decouples abstract tier from model generation ──


def test_tier_alias_decouples_abstract_tier_from_concrete_models():
    light_a = _alias(
        "light-or-local",
        (_candidate("candidate-a", "local-provider", "model-gen-a", "local-only"),),
    )
    light_b = _alias(
        "light-or-local",
        (_candidate("candidate-b", "local-provider", "model-gen-b", "local-only"),),
    )
    policy_a = ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r", _match(), _target("light-or-local")),),
        aliases=(light_a,),
    )
    policy_b = ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r", _match(), _target("light-or-local")),),
        aliases=(light_b,),
    )
    request = _request(
        available_candidate_ids=("candidate-a",),
    )
    request_b = _request(
        available_candidate_ids=("candidate-b",),
    )
    assert ModelRouter.route(policy_a, request).selected_candidate.model_ref == "model-gen-a"
    assert ModelRouter.route(policy_b, request_b).selected_candidate.model_ref == "model-gen-b"
    assert ModelRouter.route(policy_a, request).matched_tier_alias == "light-or-local"


def test_module_does_not_hardcode_model_generation():
    source = inspect.getsource(model_routing_module)
    for token in ("gpt-4o", "gpt-5", "deepseek-v", "qwen", "doubao", "claude-"):
        assert token not in source


# ── Scenario 5: P3 risk exact binding, no downgrade, explicit upgrades ──


def test_risk_non_downgrade_and_upgrade_rules():
    high = _risk_result_high()
    with pytest.raises(ValidationError):
        _request(risk_result=high, effective_risk="medium")
    with pytest.raises(ValidationError):
        _request(risk_result=high, effective_risk="high", risk_upgrade_reason="noise")
    with pytest.raises(ValidationError):
        _request(
            risk_result=high,
            effective_risk="critical",
            risk_upgrade_reason=None,
        )
    with pytest.raises(ValidationError):
        _request(
            risk_result=high,
            effective_risk="critical",
            risk_upgrade_reason="   ",
        )

    same_level = _request(risk_result=high, effective_risk="high")
    assert same_level.effective_risk == "high"
    assert same_level.risk_upgrade_reason is None

    upgraded = _request(
        risk_result=high,
        effective_risk="critical",
        risk_upgrade_reason="adversarial escalation",
    )
    assert upgraded.effective_risk == "critical"
    assert upgraded.risk_upgrade_reason == "adversarial escalation"


def test_phase_review_requires_agent_role():
    with pytest.raises(ValidationError):
        _request(phase="review", agent_role=None)
    request = _request(phase="review", agent_role="intent")
    assert request.agent_role == "intent"


def test_request_ids_are_canonical_sorted_unique_and_bounded():
    with pytest.raises(ValidationError):
        _request(
            available_candidate_ids=(
                "candidate-standard",
                "candidate-light",
            )
        )
    with pytest.raises(ValidationError):
        _request(
            available_candidate_ids=(
                "candidate-light",
                "candidate-light",
            )
        )
    with pytest.raises(ValidationError):
        _request(allowed_provider_refs=("remote-provider", "local-provider"))
    with pytest.raises(ValidationError):
        _request(task_role="   ")
    with pytest.raises(ValidationError):
        _request(task_role="x" * 129)
    with pytest.raises(ValidationError):
        _candidate("", "p1", "m1", "local-only")


# ── Scenario 6: disabled and no-match block paths ──


def test_disabled_policy_returns_routing_disabled_without_attempts():
    policy = _default_policy(enabled=False)
    first = ModelRouter.route(policy, _request())
    second = ModelRouter.route(policy, _request())
    assert first.outcome == "blocked"
    assert first.block_reason == "routing_disabled"
    assert first.matched_rule_id is None
    assert first.matched_tier_alias is None
    assert first.attempts == ()
    assert first.selected_candidate is None
    assert first.allocated_budget is None
    assert first.decision_id == second.decision_id

    with pytest.raises(ValidationError):
        ModelRoutingPolicy(
            enabled=1,
            rules=(_rule("r-catch-all"),),
            aliases=_default_aliases(),
        )


def test_no_matching_rule_returns_blocked():
    policy = _default_policy(
        rules=(_rule("r-adjudication", _match(phase="adjudication")),)
    )
    decision = ModelRouter.route(policy, _request(phase="extraction"))
    assert decision.outcome == "blocked"
    assert decision.block_reason == "no_matching_rule"
    assert decision.matched_rule_id is None
    assert decision.matched_tier_alias is None
    assert decision.attempts == ()
    assert decision.selected_candidate is None
    assert decision.allocated_budget is None


# ── Scenarios 7-9: fallback attempts and reasons ──


def _fallback_alias(candidates):
    return _alias("standard", candidates)


def _fallback_policy():
    candidates = (
        _candidate("candidate-local", "local-provider", "local-model", "local-only"),
        _candidate("candidate-standard", "approved-provider", "approved-model", "approved-provider"),
        _candidate("candidate-any", "remote-provider", "remote-model", "any"),
    )
    return ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r-catch-all", _match(), _target("standard")),),
        aliases=(_fallback_alias(candidates),),
    )


def test_primary_selected_with_zero_fallback_attempts():
    policy = _fallback_policy()
    decision = ModelRouter.route(
        policy,
        _request(
            available_candidate_ids=(
                "candidate-any",
                "candidate-local",
                "candidate-standard",
            ),
        ),
    )
    assert decision.outcome == "selected"
    assert decision.selected_candidate.candidate_id == "candidate-local"
    assert decision.attempts == (
        ModelRouteAttempt(
            candidate_id="candidate-local", reason="selected"
        ),
    )
    assert len(decision.attempts) == 1


def test_primary_unavailable_second_selected_reason_preserved():
    policy = _fallback_policy()
    decision = ModelRouter.route(
        policy,
        _request(
            available_candidate_ids=(
                "candidate-any",
                "candidate-standard",
            ),
        ),
    )
    assert decision.outcome == "selected"
    assert decision.selected_candidate.candidate_id == "candidate-standard"
    assert decision.attempts == (
        ModelRouteAttempt(
            candidate_id="candidate-local", reason="candidate_unavailable"
        ),
        ModelRouteAttempt(
            candidate_id="candidate-standard", reason="selected"
        ),
    )


def test_provider_not_allowed_and_boundary_denied_recorded_before_compliant():
    candidates = (
        _candidate("candidate-local", "local-provider", "local-model", "local-only"),
        _candidate("candidate-arch", "approved-provider", "approved-model", "approved-provider"),
        _candidate("candidate-gpu", "remote-provider", "remote-model", "any"),
        _candidate("candidate-final", "fallback-provider", "fallback-model", "local-only"),
    )
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r-catch-all", _match(), _target("standard")),),
        aliases=(_alias("standard", candidates),),
    )
    decision = ModelRouter.route(
        policy,
        _request(
            provider_boundary="local-only",
            available_candidate_ids=(
                "candidate-arch",
                "candidate-final",
                "candidate-gpu",
                "candidate-local",
            ),
            allowed_provider_refs=(
                "approved-provider",
                "fallback-provider",
            ),
        ),
    )
    assert decision.outcome == "selected"
    assert decision.selected_candidate.candidate_id == "candidate-final"
    assert tuple(attempt.reason for attempt in decision.attempts) == (
        "provider_not_allowed",
        "provider_boundary_denied",
        "provider_not_allowed",
        "selected",
    )


# ── Scenario 10: local-only boundary and blocked never selected ──


def test_local_only_request_cannot_select_approved_or_any():
    candidates = (
        _candidate("candidate-arch", "approved-provider", "approved-model", "approved-provider"),
        _candidate("candidate-gpu", "remote-provider", "remote-model", "any"),
    )
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r-catch-all", _match(), _target("standard")),),
        aliases=(_alias("standard", candidates),),
    )
    request = _request(
        provider_boundary="local-only",
        available_candidate_ids=("candidate-arch", "candidate-gpu"),
        allowed_provider_refs=("approved-provider", "remote-provider"),
        budget=_budget(500, 1.0),
    )
    decision = ModelRouter.route(policy, request)
    assert decision.outcome == "blocked"
    assert decision.block_reason == "no_eligible_candidate"
    assert decision.selected_candidate is None
    assert tuple(attempt.reason for attempt in decision.attempts) == (
        "provider_boundary_denied",
        "provider_boundary_denied",
    )
    assert decision.matched_rule_id == "r-catch-all"
    assert decision.matched_tier_alias == "standard"
    assert decision.allocated_budget == _budget(500, 1.0)
    dumped = decision.model_dump(mode="json")
    assert "PASS" not in json.dumps(dumped)
    assert "Gate" not in json.dumps(dumped)


def test_no_cross_tier_automatic_downgrade():
    aliases = (
        _alias(
            "light-or-local",
            (
                _candidate(
                    "candidate-light",
                    "local-provider",
                    "local-model",
                    "local-only",
                ),
            ),
        ),
        _alias(
            "strong",
            (
                _candidate(
                    "candidate-strong",
                    "remote-provider",
                    "strong-model",
                    "approved-provider",
                ),
            ),
        ),
    )
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "r-high-strong",
                _match(effective_risk="high"),
                _target("strong", token_budget_cap=4000, cost_budget_cap_usd=4.0),
            ),
            _rule(
                "r-catch-light",
                _match(),
                _target("light-or-local", token_budget_cap=2000, cost_budget_cap_usd=0.5),
            ),
        ),
        aliases=aliases,
    )
    decision = ModelRouter.route(
        policy,
        _request(
            risk_result=_risk_result_high(),
            effective_risk="high",
            available_candidate_ids=("candidate-light",),
        ),
    )
    assert decision.outcome == "blocked"
    assert decision.block_reason == "no_eligible_candidate"
    assert decision.matched_tier_alias == "strong"
    assert decision.attempts == (
        ModelRouteAttempt(
            candidate_id="candidate-strong", reason="candidate_unavailable"
        ),
    )
    assert decision.selected_candidate is None


# ── Scenario 11: unknown runtime available IDs rejected ──


def test_unknown_available_candidate_ids_rejected_not_ignored():
    policy = _default_policy()
    with pytest.raises(ValueError):
        ModelRouter.route(
            policy,
            _request(
                available_candidate_ids=(
                    "candidate-ghost",
                    "candidate-light",
                )
            ),
        )


# ── Scenario 12: budget allocation and strict types ──


def test_budget_allocation_is_fieldwise_minimum():
    policy = _default_policy(
        rules=(
            _rule(
                "r-catch-all",
                _match(),
                _target("standard", token_budget_cap=1000, cost_budget_cap_usd=2.0),
            ),
        )
    )
    tight = ModelRouter.route(
        policy, _request(budget=_budget(500, 1.0))
    )
    assert tight.allocated_budget == _budget(500, 1.0)

    loose = ModelRouter.route(
        policy, _request(budget=_budget(2000, 3.0))
    )
    assert loose.allocated_budget == _budget(1000, 2.0)


def test_budget_rejects_coercion_nan_inf_and_negative():
    with pytest.raises(ValidationError):
        _budget(token_budget_cap=True)
    with pytest.raises(ValidationError):
        _budget(token_budget_cap=0)
    with pytest.raises(ValidationError):
        _budget(token_budget_cap=-1)
    with pytest.raises(ValidationError):
        _budget(cost_budget_cap_usd=True)
    with pytest.raises(ValidationError):
        _budget(cost_budget_cap_usd=1)
    with pytest.raises(ValidationError):
        _budget(cost_budget_cap_usd=-0.01)
    with pytest.raises(ValidationError):
        _budget(cost_budget_cap_usd=float("nan"))
    with pytest.raises(ValidationError):
        _budget(cost_budget_cap_usd=float("inf"))
    with pytest.raises(ValidationError):
        _target(
            "standard",
            token_budget_cap=0,
            cost_budget_cap_usd=1.0,
        )
    with pytest.raises(ValidationError):
        _target(
            "standard",
            token_budget_cap=100,
            cost_budget_cap_usd=float("inf"),
        )


# ── Scenario 13: forged/reordered decisions rejected ──


def test_forged_decisions_rejected():
    policy = _default_policy()
    request = _request()
    valid = ModelRouter.route(policy, request)
    assert valid.decision_id is not None
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", valid.decision_id)

    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, attempts=())
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, matched_rule_id="rule-forged")
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, selected_candidate=None)
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, allocated_budget=_budget(9999, 99.0))
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, decision_id="sha256:" + "0" * 64)
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(valid, outcome="blocked", block_reason="routing_disabled")
        )


def test_reordered_attempts_and_silent_fallback_rejected():
    candidates = (
        _candidate("candidate-arch", "approved-provider", "approved-model", "approved-provider"),
        _candidate("candidate-gpu", "remote-provider", "remote-model", "any"),
    )
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(_rule("r-catch-all", _match(), _target("standard")),),
        aliases=(_alias("standard", candidates),),
    )
    blocked = ModelRouter.route(
        policy,
        _request(
            provider_boundary="local-only",
            available_candidate_ids=("candidate-arch", "candidate-gpu"),
            allowed_provider_refs=("approved-provider", "remote-provider"),
        ),
    )
    assert blocked.outcome == "blocked"

    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(blocked, attempts=tuple(reversed(blocked.attempts)))
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(
                blocked,
                outcome="selected",
                block_reason=None,
                selected_candidate=candidates[0],
            )
        )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(
            _decision_data(
                blocked,
                attempts=(
                    ModelRouteAttempt(
                        candidate_id="candidate-arch", reason="selected"
                    ),
                ),
                selected_candidate=candidates[0],
            )
        )


# ── Scenario 13b: exact-class copies must revalidate at the trust boundary ──


def _standard_candidate(policy):
    return next(
        candidate
        for alias in policy.aliases
        if alias.alias == "standard"
        for candidate in alias.candidates
    )


def _forged_candidate_boundary_policy(policy):
    original = _standard_candidate(policy)
    forged_candidate = original.model_copy(
        update={"required_provider_boundary": "local-only"}
    )
    forged_aliases = tuple(
        alias.model_copy(
            update={
                "candidates": tuple(
                    forged_candidate
                    if candidate is original
                    else candidate
                    for candidate in alias.candidates
                )
            }
        )
        if alias.alias == "standard"
        else alias
        for alias in policy.aliases
    )
    return forged_candidate, policy.model_copy(update={"aliases": forged_aliases})


def test_model_copy_disabled_policy_with_stale_digest_rejected_by_router():
    policy = _default_policy()
    forged = policy.model_copy(update={"enabled": False})
    assert type(forged) is ModelRoutingPolicy
    assert forged.enabled is False
    assert forged.policy_digest == policy.policy_digest
    with pytest.raises(ValueError):
        ModelRouter.route(forged, _request())


def test_model_construct_policy_with_stale_digest_rejected_by_router():
    policy = _default_policy()
    constructed = ModelRoutingPolicy.model_construct(
        schema_version="v1",
        policy_version="routing-v1",
        enabled=False,
        rules=policy.rules,
        aliases=policy.aliases,
        policy_digest=policy.policy_digest,
    )
    assert type(constructed) is ModelRoutingPolicy
    with pytest.raises(ValueError):
        ModelRouter.route(constructed, _request())


def test_model_copy_disabled_policy_with_stale_digest_rejected_by_decision():
    policy = _default_policy()
    request = _request()
    valid = ModelRouter.route(policy, request)
    forged = policy.model_copy(update={"enabled": False})
    data = _decision_data(valid)
    data["decision_id"] = None
    data.update(
        {
            "policy": forged,
            "outcome": "blocked",
            "block_reason": "routing_disabled",
            "matched_rule_id": None,
            "matched_tier_alias": None,
            "attempts": (),
            "selected_candidate": None,
            "allocated_budget": None,
        }
    )
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(data)


def test_model_copy_narrowed_candidate_boundary_with_stale_digest_never_selects():
    policy = _default_policy()
    _, forged_policy = _forged_candidate_boundary_policy(policy)
    assert forged_policy.policy_digest == policy.policy_digest
    local_only = _request(
        provider_boundary="local-only",
        available_candidate_ids=("candidate-standard",),
    )
    with pytest.raises(ValueError):
        ModelRouter.route(forged_policy, local_only)


def test_model_copy_narrowed_candidate_boundary_forged_decision_rejected():
    policy = _default_policy()
    forged_candidate, forged_policy = _forged_candidate_boundary_policy(policy)
    local_only = _request(
        provider_boundary="local-only",
        available_candidate_ids=("candidate-standard",),
    )
    data = {
        "schema_version": "v1",
        "policy": forged_policy,
        "request": local_only,
        "outcome": "selected",
        "block_reason": None,
        "matched_rule_id": "rule-catch-all",
        "matched_tier_alias": "standard",
        "attempts": (
            ModelRouteAttempt(
                candidate_id="candidate-standard", reason="selected"
            ),
        ),
        "selected_candidate": forged_candidate,
        "allocated_budget": _budget(1000, 2.0),
        "decision_id": None,
    }
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(data)


def test_model_copy_downgraded_high_risk_request_rejected_by_router():
    high = _risk_result_high()
    request = _request(risk_result=high, effective_risk="high")
    forged = request.model_copy(
        update={"effective_risk": "low", "risk_upgrade_reason": None}
    )
    assert type(forged) is ModelRouteRequest
    with pytest.raises(ValueError):
        ModelRouter.route(_default_policy(), forged)


def test_model_copy_downgraded_high_risk_request_forged_decision_rejected():
    policy = _default_policy()
    high = _risk_result_high()
    request = _request(risk_result=high, effective_risk="high")
    forged = request.model_copy(
        update={"effective_risk": "low", "risk_upgrade_reason": None}
    )
    data = {
        "schema_version": "v1",
        "policy": policy,
        "request": forged,
        "outcome": "selected",
        "block_reason": None,
        "matched_rule_id": "rule-catch-all",
        "matched_tier_alias": "standard",
        "attempts": (
            ModelRouteAttempt(
                candidate_id="candidate-standard", reason="selected"
            ),
        ),
        "selected_candidate": _standard_candidate(policy),
        "allocated_budget": _budget(1000, 2.0),
        "decision_id": None,
    }
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(data)


def _forged_low_risk_request():
    high = _risk_result_high()
    request = _request(risk_result=high, effective_risk="high")
    forged_classification = high.classification.model_copy(
        update={
            "risk_level": "low",
            "required_human_role": None,
            "classification_id": "risk_" + "0" * 32,
        }
    )
    forged_result = high.model_copy(
        update={"classification": forged_classification}
    )
    return request.model_copy(
        update={
            "risk_result": forged_result,
            "effective_risk": "low",
            "risk_upgrade_reason": None,
        }
    )


def test_model_copy_high_risk_request_with_forged_low_result_rejected_by_router():
    forged = _forged_low_risk_request()
    # The Request-level validator alone cannot see this bypass: both
    # effective_risk and the copied risk result claim low. Only P3 result
    # revalidation can reject it.
    assert forged.risk_result.classification.risk_level == "low"
    assert forged.effective_risk == "low"
    with pytest.raises(ValueError):
        ModelRouter.route(_default_policy(), forged)


def test_model_copy_high_risk_request_with_forged_low_result_decision_rejected():
    policy = _default_policy()
    forged = _forged_low_risk_request()
    data = {
        "schema_version": "v1",
        "policy": policy,
        "request": forged,
        "outcome": "selected",
        "block_reason": None,
        "matched_rule_id": "rule-catch-all",
        "matched_tier_alias": "standard",
        "attempts": (
            ModelRouteAttempt(
                candidate_id="candidate-standard", reason="selected"
            ),
        ),
        "selected_candidate": _standard_candidate(policy),
        "allocated_budget": _budget(1000, 2.0),
        "decision_id": None,
    }
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(data)


def test_model_copy_invalid_budget_rejected_at_router_boundary():
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "r-extraction",
                _match(phase="extraction"),
                _target("standard"),
            ),
        ),
        aliases=(_alias("standard"),),
    )
    request = _request(
        phase="adjudication",
        available_candidate_ids=("candidate-standard",),
    )
    # The request is blocked before allocation, so only the trust boundary
    # revalidation can reject a forged exact-class budget.
    assert ModelRouter.route(policy, request).outcome == "blocked"
    bad_budgets = (
        request.budget.model_copy(update={"token_budget_cap": -1}),
        request.budget.model_copy(update={"token_budget_cap": True}),
        request.budget.model_copy(
            update={"cost_budget_cap_usd": float("nan")}
        ),
        request.budget.model_copy(
            update={"cost_budget_cap_usd": float("inf")}
        ),
        request.budget.model_copy(update={"cost_budget_cap_usd": -0.01}),
        request.budget.model_copy(update={"cost_budget_cap_usd": 1}),
        request.budget.model_copy(update={"cost_budget_cap_usd": True}),
    )
    for bad_budget in bad_budgets:
        forged = request.model_copy(update={"budget": bad_budget})
        with pytest.raises(ValueError):
            ModelRouter.route(policy, forged)


def test_model_copy_invalid_budget_forged_decision_rejected():
    policy = ModelRoutingPolicy(
        enabled=True,
        rules=(
            _rule(
                "r-extraction",
                _match(phase="extraction"),
                _target("standard"),
            ),
        ),
        aliases=(_alias("standard"),),
    )
    request = _request(
        phase="adjudication",
        available_candidate_ids=("candidate-standard",),
    )
    forged = request.model_copy(
        update={
            "budget": request.budget.model_copy(
                update={"token_budget_cap": -1}
            )
        }
    )
    data = {
        "schema_version": "v1",
        "policy": policy,
        "request": forged,
        "outcome": "blocked",
        "block_reason": "no_matching_rule",
        "matched_rule_id": None,
        "matched_tier_alias": None,
        "attempts": (),
        "selected_candidate": None,
        "allocated_budget": None,
        "decision_id": None,
    }
    with pytest.raises(ValidationError):
        ModelRouteDecision.model_validate(data)


def test_semantically_identical_copy_accepted_and_identity_preserved():
    policy = _default_policy()
    request = _request()
    same = policy.model_copy(update={"enabled": policy.enabled})
    assert same == policy
    first = ModelRouter.route(policy, request)
    second = ModelRouter.route(same, request)
    assert first.outcome == second.outcome == "selected"
    assert first.decision_id == second.decision_id
    assert first.policy is policy
    assert first.request is request
    assert second.policy is same
    assert second.request is request


# ── Scenario 14: product example rules ──


def _product_example_policy():
    aliases = (
        _alias(
            "alternate-strong",
            (
                _candidate(
                    "candidate-alt",
                    "remote-provider",
                    "alt-model",
                    "any",
                ),
            ),
        ),
        _alias(
            "light-or-local",
            (
                _candidate(
                    "candidate-light",
                    "local-provider",
                    "local-model",
                    "local-only",
                ),
            ),
        ),
        _alias(
            "standard",
            (
                _candidate(
                    "candidate-standard",
                    "approved-provider",
                    "approved-model",
                    "approved-provider",
                ),
            ),
        ),
        _alias(
            "strong",
            (
                _candidate(
                    "candidate-strong",
                    "remote-provider",
                    "strong-model",
                    "approved-provider",
                ),
            ),
        ),
    )
    rules = (
        _rule(
            "rule-adjudication-strong",
            _match(phase="adjudication"),
            _target("strong", token_budget_cap=4000, cost_budget_cap_usd=4.0),
        ),
        _rule(
            "rule-high-architecture-strong",
            _match(agent_role="architecture", effective_risk="high"),
            _target("strong", token_budget_cap=4000, cost_budget_cap_usd=4.0),
        ),
        _rule(
            "rule-extraction-light",
            _match(phase="extraction"),
            _target("light-or-local", token_budget_cap=2000, cost_budget_cap_usd=0.5),
        ),
        _rule(
            "rule-low-risk-standard",
            _match(effective_risk="low"),
            _target("standard", token_budget_cap=3000, cost_budget_cap_usd=1.5),
        ),
    )
    return ModelRoutingPolicy(enabled=True, rules=rules, aliases=aliases)


def test_product_example_routes_exactly():
    policy = _product_example_policy()

    adjudication = ModelRouter.route(
        policy, _request(phase="adjudication")
    )
    assert adjudication.matched_rule_id == "rule-adjudication-strong"
    assert adjudication.matched_tier_alias == "strong"

    high_architecture = ModelRouter.route(
        policy,
        _request(
            phase="review",
            agent_role="architecture",
            risk_result=_risk_result_high(),
            effective_risk="high",
        ),
    )
    assert high_architecture.matched_rule_id == "rule-high-architecture-strong"
    assert high_architecture.matched_tier_alias == "strong"

    extraction = ModelRouter.route(
        policy, _request(phase="extraction")
    )
    assert extraction.matched_rule_id == "rule-extraction-light"
    assert extraction.matched_tier_alias == "light-or-local"

    low_risk = ModelRouter.route(
        policy, _request(phase="synthesis", effective_risk="low")
    )
    assert low_risk.matched_rule_id == "rule-low-risk-standard"
    assert low_risk.matched_tier_alias == "standard"


# ── Scenario 15: separation and safety ──


def test_module_has_no_provider_env_io_topology_or_gate_fields():
    source = inspect.getsource(model_routing_module)
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.lstrip(".").split(".")[0])
    assert imports == {
        "hashlib",
        "json",
        "math",
        "re",
        "typing",
        "pydantic",
        "risk",
    }
    for token in (
        "os.",
        "environ[",
        "getenv",
        "PASS",
        "Gate",
        "Receipt",
        "approval",
    ):
        assert token not in source
    for token in ("council", "topology", "execution", "receipt", "approval"):
        assert token not in set(ModelRouteDecision.model_fields)


def test_router_is_stateless_and_route_requires_exact_types():
    assert all(
        key.startswith("__") or key == "route"
        for key in vars(ModelRouter)
    )
    policy = _default_policy()
    request = _request()
    with pytest.raises(TypeError):
        ModelRouter.route({"enabled": True}, request)
    with pytest.raises(TypeError):
        ModelRouter.route(policy, {"phase": "extraction"})

    decision = ModelRouter.route(policy, request)
    assert decision.policy is policy
    assert decision.request is request
    assert decision.outcome in ("selected", "blocked")
    assert "PASS" not in decision.outcome.upper().replace("SELECTED", "")


def test_selected_is_only_a_routing_outcome():
    policy = _default_policy()
    decision = ModelRouter.route(policy, _request())
    assert decision.outcome == "selected"
    assert decision.block_reason is None
    assert decision.selected_candidate is not None
    dumped = json.dumps(decision.model_dump(mode="json"))
    assert "PASS" not in dumped
    assert "Gate" not in dumped
    assert "Receipt" not in dumped
    assert "approval" not in dumped


# ── Scenario 16: package public exports ──


def test_public_exports_present_in_assurance_package():
    missing = NEW_PUBLIC_NAMES - set(assurance.__all__)
    assert not missing
    for name in sorted(NEW_PUBLIC_NAMES):
        assert getattr(assurance, name) is not None
