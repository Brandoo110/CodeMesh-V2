export const RUN_PANEL_DEFAULT_WIDTH = 320;
export const RUN_PANEL_MIN_WIDTH = 280;
export const RUN_PANEL_MAX_WIDTH = 640;

export function clampRunPanelWidth(width: number): number {
  if (!Number.isFinite(width)) return RUN_PANEL_DEFAULT_WIDTH;
  return Math.min(
    RUN_PANEL_MAX_WIDTH,
    Math.max(RUN_PANEL_MIN_WIDTH, Math.round(width)),
  );
}
