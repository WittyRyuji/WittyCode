import type { Project, Session } from "../types.ts";

type SidebarRow = Pick<Session, "id" | "projectId" | "pinned">;

/** Whether `source` may be dropped next to `target`: the pinned strip is a single partition across
 *  projects; ordinary rows reorder inside their own project only. */
export function sameSessionPartition(source: SidebarRow | undefined, target: SidebarRow): boolean {
  if (!source) return false;
  if (!!source.pinned || !!target.pinned) return !!source.pinned && !!target.pinned;
  return source.projectId === target.projectId;
}

/** For the demo: put an admitted, active or newly (un)pinned row at the front of its own partition. */
export function frontOfPartition(ids: string[], id: string, pinned: boolean, isPinned: (id: string) => boolean): string[] {
  const next = ids.filter((item) => item !== id);
  const firstOrdinary = pinned ? 0 : next.findIndex((item) => !isPinned(item));
  next.splice(firstOrdinary < 0 ? next.length : firstOrdinary, 0, id);
  return next;
}

/** The order the pinned strip shows. `declared` is Python's `pinnedSessionIds` where the sender knows the
 *  field; without it the `pinned` flags in project order stand in, so an older sender still gets the strip.
 *  A declared list is authoritative: its member ids stay even while their session details are still on the
 *  way (an open conversation's `session` event lands after `app.init`/`sidebar`) — rendering skips those
 *  rows. Ids that are not visible rows are left out, and each id appears once. */
export function pinnedOrder(
  projects: Pick<Project, "sessionIds">[],
  sessions: Record<string, SidebarRow | undefined>,
  declared?: string[] | null,
): string[] {
  const visible = new Set(projects.flatMap((p) => p.sessionIds));
  const ids = declared ?? projects.flatMap((p) => p.sessionIds).filter((id) => sessions[id]?.pinned === true);
  const out: string[] = [];
  for (const id of ids) {
    if (!visible.has(id) || out.includes(id)) continue;
    out.push(id);
  }
  return out;
}
