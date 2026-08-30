import assert from "node:assert/strict";
import test from "node:test";

import {
  authoritativeReadbackMatches,
  getDecisionOptions,
  getSelectedDecisionAction,
} from "./assurance-case-view.ts";
import type { AssuranceProjection } from "./types.ts";
import type { AssuranceAction } from "./types.ts";

function action(
  code: AssuranceAction["code"],
  overrides: Partial<AssuranceAction> = {},
): AssuranceAction {
  return {
    code,
    required_human_role: null,
    self_approval_forbidden: false,
    high_risk_confirmation_required: false,
    ...overrides,
  };
}

test("decision options come only from allowed decision actions", () => {
  const allowed = [
    action("download_passport"),
    action("future_action"),
    action("reject"),
    action("approve"),
  ];

  assert.deepEqual(
    getDecisionOptions(allowed).map((item) => item.code),
    ["reject", "approve"],
  );
  assert.equal(getSelectedDecisionAction(allowed, "download_passport"), null);
  assert.equal(getSelectedDecisionAction(allowed, "future_action"), null);
  assert.equal(getSelectedDecisionAction(allowed, "approve")?.code, "approve");
});

test("PASS_WITH_WAIVER, blocked, and stale use only server-advertised actions", () => {
  for (const { allowed, expected } of [
    {
      policyStatus: "PASS_WITH_WAIVER",
      allowed: [action("download_passport"), action("reject")],
      expected: ["reject"],
    },
    {
      policyStatus: "BLOCKED",
      allowed: [action("download_passport")],
      expected: [],
    },
    {
      policyStatus: "STALE",
      allowed: [action("download_passport")],
      expected: [],
    },
  ]) {
    assert.deepEqual(
      getDecisionOptions(allowed).map((item) => item.code),
      expected,
    );
  }
});

test("helper does not rebuild the policy or state action matrix", () => {
  const reject = action("reject");
  assert.deepEqual(
    getDecisionOptions([action("download_passport"), reject]),
    [reject],
  );
});

test("selected action preserves role and high-risk flags from CaseView", () => {
  const allowed = [
    action("reject"),
    action("approve", {
      required_human_role: "security_owner",
      self_approval_forbidden: true,
      high_risk_confirmation_required: true,
    }),
    action("approve_with_conditions", {
      required_human_role: "security_owner",
      self_approval_forbidden: true,
      high_risk_confirmation_required: true,
    }),
    action("waiver", {
      required_human_role: "security_owner",
      self_approval_forbidden: true,
      high_risk_confirmation_required: true,
    }),
  ];

  assert.deepEqual(getDecisionOptions(allowed), allowed);
  assert.deepEqual(getSelectedDecisionAction(allowed, "approve"), allowed[1]);
  assert.deepEqual(getSelectedDecisionAction(allowed, "reject"), allowed[0]);
  assert.deepEqual(getSelectedDecisionAction(allowed, "reject"), {
    code: "reject",
    required_human_role: null,
    self_approval_forbidden: false,
    high_risk_confirmation_required: false,
  });
  assert.equal(getSelectedDecisionAction(allowed, "waiver")?.required_human_role, "security_owner");
  assert.equal(getSelectedDecisionAction(allowed, "waiver")?.high_risk_confirmation_required, true);
});

function projection(overrides: Partial<AssuranceProjection> = {}): AssuranceProjection {
  return {
    case: {
      case_id: "case-1",
      subject_digest: "sha256:" + "a".repeat(64),
      state: "EVIDENCE_COLLECTED",
      evidence_refs: [],
      finding_refs: [],
      execution_receipt_refs: [],
      policy_decision_refs: [],
      human_decision_refs: [],
      conditions: [],
      conflicts: [],
      missing_evidence: [],
      invalidation_reason: null,
      created_at: "2026-08-30T00:00:00Z",
      updated_at: "2026-08-30T00:00:00Z",
    },
    binding: {
      policy_version: "policy-v1",
      rubric_version: "rubric-v1",
      waiver_id: null,
      waiver_expires_at: null,
    },
    metadata: null,
    evidence: [],
    findings: [],
    receipt: null,
    decisions: [],
    timeline: [],
    revision: 2,
    schema_version: "v1",
    case_id: "case-1",
    subject_digest: "sha256:" + "a".repeat(64),
    policy_gate: {
      status: "PASS",
      decision_id: "policy-1",
      reason_codes: [],
      required_human_role: null,
      waiver_ref: null,
      evaluated_at: "2026-08-30T00:00:00Z",
    },
    acceptance_state: "EVIDENCE_COLLECTED",
    release_state: {
      status: "NOT_OBSERVED",
      observation_id: null,
      environment: null,
      deployment_id: null,
      source: null,
      trust_level: null,
      recorded_at: null,
    },
    allowed_actions: [],
    gate: "PASS",
    digest_freshness: true,
    freshness: {
      status: "FRESH",
      reason_code: "FRESHNESS_MATCH",
      checked_at: "2026-08-30T00:00:01Z",
      expected_subject_digest: "sha256:" + "a".repeat(64),
      observed_subject_digest: "sha256:" + "a".repeat(64),
    },
    attention_reason: null,
    ...overrides,
  };
}

test("authoritative readback ignores only freshness check time", () => {
  const posted = projection();
  const readback = projection({
    freshness: { ...posted.freshness!, checked_at: "2026-08-30T00:00:02Z" },
  });

  assert.equal(authoritativeReadbackMatches(posted, readback), true);
  assert.equal(
    authoritativeReadbackMatches(posted, projection({ revision: 3 })),
    false,
  );
  assert.equal(
    authoritativeReadbackMatches(posted, projection({ acceptance_state: "ACCEPTED" })),
    false,
  );
});
