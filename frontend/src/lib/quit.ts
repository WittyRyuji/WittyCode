/** What closing the window would cut short, said in the quit dialog. */
export interface QuitSession {
  mark?: string;
  /** messages waiting to be sent (they live in memory only) */
  queued?: unknown[];
  /** the goal (`/goal`) the conversation is working toward ("" or absent: none) */
  goal?: string;
}

/** The sentence of the quit dialog; "" when nothing would be lost (the window just closes). */
export function quitWarning(sessions: QuitSession[]): string {
  const working = sessions.filter((s) => s.mark === "running" || s.mark === "attention").length;
  const waiting = sessions.reduce((n, s) => n + (s.queued?.length ?? 0), 0);
  const goals = sessions.filter((s) => !!s.goal).length;
  if (working === 0 && waiting === 0 && goals === 0) return "";
  const lost = [waiting ? `${waiting} 条排队的消息还没发出` : "", goals ? `${goals} 个目标（/goal）还没完成` : ""].filter(Boolean);
  const parts = [working ? `有 ${working} 个会话还在工作。退出会中断它们正在做的事` : "", lost.length ? `${lost.join("，")}，退出后不会再发出或继续` : ""].filter(Boolean);
  return `${parts.join("；")}；下次可以从侧边栏恢复会话。`;
}
