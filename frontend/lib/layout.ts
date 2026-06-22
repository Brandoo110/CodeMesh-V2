import type { View } from "./store";

export const mainViewHostClassName = "flex-1 flex flex-col min-w-0 min-h-0";

export function viewUsesChatSidebar(view: View): boolean {
  return view === "chat";
}

export function shouldKeepViewMounted(view: View): boolean {
  return view === "workflows";
}
