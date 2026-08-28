"""Pure CaseView projection and decision-action derivation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from assurance.contracts import PolicyDecision
from assurance.live_freshness import FreshnessStatus, LiveFreshness
from assurance.release_observation import ReleaseObservation
from assurance.state_machine import allowed_event_kinds


_DECISION_STATES = {
    "EVIDENCE_COLLECTED",
    "CONFLICTED",
    "CONDITIONAL_ACCEPTED",
}
_APPROVAL_CODES = {"approve", "approve_with_conditions", "waiver"}


def _action(
    code: str,
    *,
    required_human_role: str | None = None,
    high_risk: bool = False,
) -> dict[str, object]:
    approval = code in _APPROVAL_CODES
    return {
        "code": code,
        "required_human_role": required_human_role if approval else None,
        "self_approval_forbidden": approval,
        "high_risk_confirmation_required": approval and high_risk,
    }


def derive_allowed_actions(
    *,
    acceptance_state: str,
    policy_gate: Mapping[str, object],
    digest_freshness: bool,
    risk: object,
) -> list[dict[str, object]]:
    """Derive actions from acceptance, policy, digest, and risk facts."""

    actions = [_action("download_passport")]
    if not digest_freshness or acceptance_state not in _DECISION_STATES:
        return actions

    event_kinds = set(allowed_event_kinds(acceptance_state))
    if "REJECT" in event_kinds:
        actions.append(_action("reject"))

    policy_status = policy_gate["status"]
    if policy_status not in {"PASS", "NEEDS_HUMAN"}:
        return actions

    required_role = (
        policy_gate.get("required_human_role")
        if policy_status == "NEEDS_HUMAN"
        else None
    )
    high_risk = risk in {"high", "critical"}
    if "ACCEPT" in event_kinds:
        actions.append(
            _action(
                "approve",
                required_human_role=required_role,
                high_risk=high_risk,
            )
        )
    if "CONDITIONALLY_ACCEPT" in event_kinds:
        actions.append(
            _action(
                "approve_with_conditions",
                required_human_role=required_role,
                high_risk=high_risk,
            )
        )
        if policy_status == "NEEDS_HUMAN":
            actions.append(
                _action(
                    "waiver",
                    required_human_role=required_role,
                    high_risk=high_risk,
                )
            )
    return actions


def resolve_action(
    actions: Sequence[Mapping[str, Any]], code: str
) -> Mapping[str, Any] | None:
    """Return the one advertised action matching ``code``, if any."""

    return next((action for action in actions if action.get("code") == code), None)


def apply_live_freshness(
    view: Mapping[str, object], result: LiveFreshness
) -> dict[str, object]:
    """Overlay one server-owned live result on a previously built CaseView.

    The helper is additive so the existing pure CaseView interface remains
    useful for explicit database-only fixtures.  A non-FRESH result closes all
    decision actions while retaining the safe passport download action.
    """

    if not isinstance(result, LiveFreshness):
        raise TypeError("result must be a LiveFreshness")
    overlay = dict(view)
    overlay["freshness"] = result.model_dump(mode="json")
    live_digest_freshness = bool(view.get("digest_freshness")) and (
        result.status is FreshnessStatus.FRESH
    )
    overlay["digest_freshness"] = live_digest_freshness
    if not live_digest_freshness:
        overlay["allowed_actions"] = [
            _action("download_passport")
        ]
    return overlay


def _policy_gate(decisions: Sequence[object]) -> dict[str, object]:
    latest = next(
        (
            decision
            for decision in reversed(decisions)
            if isinstance(decision, PolicyDecision)
        ),
        None,
    )
    if latest is None:
        return {
            "status": "NOT_EVALUATED",
            "decision_id": None,
            "reason_codes": [],
            "required_human_role": None,
            "waiver_ref": None,
            "evaluated_at": None,
        }
    data = latest.model_dump(mode="json")
    return {
        "status": data["outcome"],
        "decision_id": data["decision_id"],
        "reason_codes": data["reason_codes"],
        "required_human_role": data["required_human_role"],
        "waiver_ref": data["waiver_ref"],
        "evaluated_at": data["evaluated_at"],
    }


def _release_state(
    observations: Sequence[ReleaseObservation],
) -> dict[str, object]:
    if not observations:
        return {
            "status": "NOT_OBSERVED",
            "observation_id": None,
            "environment": None,
            "deployment_id": None,
            "source": None,
            "trust_level": None,
            "recorded_at": None,
        }
    data = observations[-1].model_dump(mode="json")
    return {
        "status": data["outcome"],
        "observation_id": data["observation_id"],
        "environment": data["environment"],
        "deployment_id": data["deployment_id"],
        "source": data["source"],
        "trust_level": data["trust_level"],
        "recorded_at": data["recorded_at"],
    }


def build_case_view(
    *,
    case_id: str,
    subject_digest: str,
    revision: int,
    acceptance_state: str,
    decisions: Sequence[object],
    release_observations: Sequence[ReleaseObservation],
    digest_freshness: bool,
    risk: object,
) -> dict[str, object]:
    """Build the additive CaseView contract from already-loaded facts."""

    policy_gate = _policy_gate(decisions)
    return {
        "schema_version": "v1",
        "case_id": case_id,
        "subject_digest": subject_digest,
        "revision": revision,
        "digest_freshness": digest_freshness,
        "policy_gate": policy_gate,
        "acceptance_state": acceptance_state,
        "release_state": _release_state(release_observations),
        "allowed_actions": derive_allowed_actions(
            acceptance_state=acceptance_state,
            policy_gate=policy_gate,
            digest_freshness=digest_freshness,
            risk=risk,
        ),
    }


__all__ = [
    "apply_live_freshness",
    "build_case_view",
    "derive_allowed_actions",
    "resolve_action",
]
