// Ordering helpers for the sidebar's own drag & drop: the project list, and the same splice the
// session rows use inside their partition.

import type { Project } from "../types";

/** `id` moved next to `targetId` — before it, or after it when `after`. A no-op when either is missing. */
export function moveBefore<T>(list: readonly T[], id: T, targetId: T, after: boolean): T[] {
  if (id === targetId || !list.includes(id)) return [...list];
  const next = list.filter((item) => item !== id);
  const at = next.indexOf(targetId);
  if (at < 0) return [...list];
  next.splice(at + Number(after), 0, id);
  return next;
}

/** The projects in `order`; any id the order forgot keeps its place at the end. */
export function orderProjects(projects: Project[], order: string[]): Project[] {
  const byId = new Map(projects.map((p) => [p.id, p]));
  const named = new Set(order);
  return [...order.flatMap((id) => (byId.has(id) ? [byId.get(id)!] : [])), ...projects.filter((p) => !named.has(p.id))];
}
