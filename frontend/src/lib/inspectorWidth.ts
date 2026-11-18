export const INSPECTOR_MIN = 340;
const INSPECTOR_MAX = 4000;
const CONVERSATION_MIN = 360;

export function inspectorLimit(windowWidth: number, sidebarWidth: number): number {
  return Math.max(INSPECTOR_MIN, Math.min(INSPECTOR_MAX, Math.floor(windowWidth - sidebarWidth - CONVERSATION_MIN)));
}

export function inspectorWidth(saved: number, windowWidth: number, sidebarWidth: number): number {
  return Math.min(inspectorLimit(windowWidth, sidebarWidth), Math.max(INSPECTOR_MIN, Math.round(saved || windowWidth * 0.42)));
}
