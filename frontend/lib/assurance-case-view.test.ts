import assert from "node:assert/strict";
import test from "node:test";

import {
  getDecisionOptions,
  getSelectedDecisionAction,
} from "./assurance-case-view.ts";
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
