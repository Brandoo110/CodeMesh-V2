import type { AssuranceAction } from "./types";

const DECISION_ACTION_CODES = new Set([
  "approve",
  "reject",
  "approve_with_conditions",
  "waiver",
]);

export function getDecisionOptions(
  allowedActions: readonly AssuranceAction[],
): AssuranceAction[] {
  return allowedActions.filter((action) => DECISION_ACTION_CODES.has(action.code));
}

export function getSelectedDecisionAction(
  allowedActions: readonly AssuranceAction[],
  decision: string | null | undefined,
): AssuranceAction | null {
  return getDecisionOptions(allowedActions).find(
    (action) => action.code === decision,
  ) ?? null;
}
