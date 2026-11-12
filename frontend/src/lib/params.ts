import { QUEUED_SAMPLE } from "../mock/data";
import { buildModels, openKey, useStore } from "../store";
import type { ThemeChoice } from "../types";

const SCENES: Record<string, string | null> = {
  none: null,
  empty: "s-empty",
  done: "s-file-ops",
  long: "s-review",
  live: "s-live",
  approve: "s-approval",
  ask: "s-question",
  dead: "s-dead",
  index: "s-index",
  devin: "s-devin",
  chatgpt: "s-chatgpt",
  api: "s-api",
};

/** `index.html?scene=approve&theme=dark&expand=all` opens the page in one chosen state: used for the review's links
 *  and for the screenshots. Nothing here exists in the real app. */
export function applyUrlParams(): { width?: string; guide?: boolean } {
  const p = new URLSearchParams(window.location.search);
  const s = useStore.getState();
  const theme = p.get("theme");
  if (theme === "light" || theme === "dark" || theme === "system") s.setTheme(theme as ThemeChoice);
  const fs = p.get("fs");
  if (fs === "sm" || fs === "md" || fs === "lg") s.setFontScale(fs);
  // `providers=none` and the account states below show what needs no account to see.
  // `devin=signed-out|missing|signing-in`
  const devin = p.get("devin");
  if (devin === "signed-out") useStore.setState({ devin: { ...s.devin, auth: "signed-out", email: undefined, plan: undefined } });
  if (devin === "signing-in") useStore.setState({ devin: { ...s.devin, auth: "signing-in" } });
  if (devin === "missing") useStore.setState({ devin: { ...s.devin, cli: "missing", auth: "signed-out", cliPath: undefined, version: undefined } });
  // `chatgpt=signed-out|signing-in|no-plan|unwelcomed` likewise (no-plan: signed in without the permission to use the plan)
  const chatgpt = p.get("chatgpt");
  if (chatgpt === "signed-out") useStore.setState({ chatgpt: { ...s.chatgpt, auth: "signed-out", email: "", planEnabled: false, accounts: [] } });
  if (chatgpt === "signing-in") useStore.setState({ chatgpt: { ...s.chatgpt, auth: "signing-in", signingIn: true } });
  if (chatgpt === "no-plan") useStore.setState({ chatgpt: { ...s.chatgpt, planEnabled: false } });
  if (chatgpt === "unwelcomed") useStore.setState({ chatgpt: { ...s.chatgpt, welcomed: false } });
  if (p.get("providers") === "none") useStore.setState({ providers: [], models: buildModels([]) });
  const settings = p.get("settings");
  if (settings) s.openSettings(settings, p.get("provider"));
  const scene = p.get("scene");
  if (scene && scene in SCENES) s.select(SCENES[scene] ?? null);
  // `queued=1|2` puts that many waiting messages above the composer of the conversation on show.
  const queued = Number(p.get("queued"));
  const current = useStore.getState().currentId;
  if (queued > 0 && current) useStore.getState().patchSession(current, (x) => ({ ...x, queued: QUEUED_SAMPLE.slice(0, queued) }));
  // `switch=devin` shows the open conversation right after it moved to a Devin model (and `switch=back` the way back).
  const swtch = p.get("switch");
  if (swtch === "devin") s.choose("model", "devin:swe-2-high");
  if (swtch === "chatgpt") s.choose("model", "chatgpt:gpt-6.1-sol");
  if (swtch === "back") {
    s.choose("model", "devin:swe-2-high");
    useStore.getState().choose("model", "chatgpt:gpt-6-astra");
  }
  // `file=docs/handoff.md:3-8` opens that file in the side panel (the scene it is shown in is the open conversation).
  const file = p.get("file");
  if (file) useStore.getState().openFile(file);
  if (p.get("review") === "0") s.setReviewOpen(false);
  if (p.get("side") === "0") s.setSidebarOpen(false);
  const expand = p.get("expand");
  if (expand) {
    const groups: Record<string, boolean> = {};
    const items: Record<string, boolean> = {};
    for (const session of Object.values(s.sessions))
      for (const turn of session.turns)
        for (const b of turn.blocks)
          if (b.kind === "activity") {
            groups[openKey(session.id, b.id)] = true;
            if (expand === "all") for (const e of b.entries) items[openKey(session.id, e.id)] = true;
          }
    useStore.setState({ openGroups: groups, openItems: items });
  }
  const dialog = p.get("dialog");
  if (dialog) {
    s.openDialog({
      title: "关闭正在运行的会话？",
      body: "「重构 session_manager 的上下文读取」还在运行。关闭会中断当前任务，已经完成的修改不会撤销。",
      confirmLabel: "中断并关闭",
      danger: true,
      onConfirm: () => s.say("（演示）已关闭。"),
    });
  }
  if (p.get("lightbox")) {
    const att = s.sessions["s-review"]?.turns[0]?.blocks.find((b) => b.kind === "user");
    if (att?.kind === "user" && att.attachments?.[0]) s.openLightbox(att.attachments[0]);
  }
  if (p.get("drop")) s.setDragging(true);
  if (p.get("notice")) s.say("已自动关闭最久未使用的会话「修复点击闪烁」以腾出位置，可在侧边栏重新打开。");
  return { width: p.get("width") ?? undefined, guide: p.get("guide") === "1" };
}
