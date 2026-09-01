import type { AssuranceAction, AssuranceProjection } from "./types";

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

function canonicalReadbackValue(value: unknown, path: readonly string[] = []): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalReadbackValue(item, path));
  }
  if (value && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      if (key === "checked_at" && path[path.length - 1] === "freshness") continue;
      result[key] = canonicalReadbackValue(
        (value as Record<string, unknown>)[key],
        [...path, key],
      );
    }
    return result;
  }
  return value;
}

/**
 * Compare the POST response with the next authoritative CaseView GET.
 * Freshness checking time is volatile; all other server-owned business facts
 * must match before the UI presents a decision as confirmed.
 */
export function authoritativeReadbackMatches(
  posted: AssuranceProjection,
  readback: AssuranceProjection,
): boolean {
  return JSON.stringify(canonicalReadbackValue(posted))
    === JSON.stringify(canonicalReadbackValue(readback));
}
