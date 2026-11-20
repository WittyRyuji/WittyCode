import type { Session } from "../types";

type SidebarRow = Pick<Session, "id" | "projectId" | "pinned">;

export function sameSessionPartition(source: SidebarRow | undefined, target: SidebarRow): boolean {
  return !!source && source.projectId === target.projectId && !!source.pinned === !!target.pinned;
}

/** For the demo: put an admitted, active or newly (un)pinned row at the front of its own partition. */
export function frontOfPartition(ids: string[], id: string, pinned: boolean, isPinned: (id: string) => boolean): string[] {
  const next = ids.filter((item) => item !== id);
  const firstOrdinary = pinned ? 0 : next.findIndex((item) => !isPinned(item));
  next.splice(firstOrdinary < 0 ? next.length : firstOrdinary, 0, id);
  return next;
}
