export function getStepOrderPrefix(stepOrder: number, stepName: string): string | null {
  const escapedOrder = String(stepOrder).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const ownNumber = new RegExp(`^\\s*#?\\s*${escapedOrder}(?:[.)、]|\\s+)`);
  return ownNumber.test(stepName) ? null : `#${stepOrder}`;
}

export function formatStepTitle(stepOrder: number, stepName: string): string {
  const prefix = getStepOrderPrefix(stepOrder, stepName);
  return prefix ? `${prefix} ${stepName}` : stepName;
}
