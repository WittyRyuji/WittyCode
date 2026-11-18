export const RAIL_MIN = 240;
export const RAIL_MAX = 600;

export function railLimit(container: number): number {
  // Wide layouts retain a usable reading/composer column; narrow layouts use the existing overlay.
  return Math.max(RAIL_MIN, Math.min(RAIL_MAX, container > 900 ? container - 600 : container - 56));
}

export function railWidth(preferred: number, container: number): number {
  return Math.round(Math.max(RAIL_MIN, Math.min(railLimit(container), preferred)));
}
