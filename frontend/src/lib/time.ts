// Relative times of the session list, as desktop/core/timefmt.py::format_relative words them. The live app gets
// `modifiedAt` (ms) from Python and formats it here, so the words stay current while the page is open.

const pad = (n: number) => String(n).padStart(2, "0");
const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** `刚刚` / `5 分钟前` / `3 小时前` / `昨天 14:20` / `10-28 09:05` / `2025-12-31`. Day boundaries are the machine's local
 *  time; a time in the future counts as now. */
export function formatRelative(then: number, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - then) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  const a = new Date(then);
  const b = new Date(now);
  // Calendar days apart; rounding absorbs the 23- or 25-hour days of a daylight-saving change.
  const days = Math.round((startOfDay(b) - startOfDay(a)) / 86_400_000);
  if (days === 0) return `${Math.floor(seconds / 3600)} 小时前`;
  const time = `${pad(a.getHours())}:${pad(a.getMinutes())}`;
  if (days === 1) return `昨天 ${time}`;
  if (a.getFullYear() === b.getFullYear()) return `${pad(a.getMonth() + 1)}-${pad(a.getDate())} ${time}`;
  return `${a.getFullYear()}-${pad(a.getMonth() + 1)}-${pad(a.getDate())}`;
}
