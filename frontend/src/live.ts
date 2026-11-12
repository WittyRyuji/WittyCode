// The live app: the store's actions become calls to Python (AppCore, see protocol.ts) and Python's events change the
// store through liveReducer.ts. main.tsx starts it before the first render when the page runs inside WittyCode; the
// design review never loads any of this.

import { confirmClose, confirmDeleteProvider, confirmQuit, confirmRemoveProject, useStore, type Store } from "./store";
import { connectTransport, liveCall } from "./liveBridge";
import { applyEvent, applyInit, chatgptView, clampCompactAt, selectWhenKnown, settingsFields } from "./liveReducer";
import { QWebChannelTransport, type Transport } from "./transport";
import { messageOf } from "./lib/errors";
import type { ApiProvider, AttachmentView, ProviderDraft } from "./types";
import type { AttachmentsResult, FileUpload, HandoffChoice, SendOver, SettingKey, SettingValue } from "./protocol";

/** Python does not even look at a file above this (core/attachments.py MAX_SOURCE_BYTES), so it is not read here either. */
export const MAX_FILE_BYTES = 64 * 1024 * 1024;
const EXTERNAL = /^(https?:|mailto:)/i;
const FAILED = Symbol("failed");
/** The ChatGPT account is read again at most this often: asking on every hover would be a request each time. */
const PLAN_USAGE_EVERY_MS = 20_000;
let planUsageAsked = 0;

const get = () => useStore.getState();
const set = useStore.setState;
const say = (text: string) => get().say(text);

/** A call whose failure the user only needs to read about: the message goes to the status bar, nothing else happens. */
async function attempt<T>(call: Promise<T>): Promise<T | typeof FAILED> {
  try {
    return await call;
  } catch (error) {
    say(messageOf(error));
    return FAILED;
  }
}

export function startLive(): void {
  let transport: Transport;
  try {
    transport = new QWebChannelTransport();
  } catch (error) {
    set({ bootError: messageOf(error) });
    return;
  }
  void installLive(transport);
}

/** Every action talks to Python from the first moment (the title bar works while app.init is on its way); the data
 *  comes with app.init's answer. Events that arrive before it are applied as well: app.init's snapshot is newer. */
export async function installLive(transport: Transport): Promise<void> {
  connectTransport(transport);
  transport.onEvent(onEvent);
  set(liveActions());
  guardLinks();
  try {
    const init = await liveCall("app.init");
    set((s) => ({ ...applyInit(s, init), ready: true, bootError: null }));
  } catch (error) {
    set({ bootError: messageOf(error) });
  }
}

function onEvent(name: string, payload: unknown): void {
  const { patch, effects } = applyEvent(get(), name, payload);
  if (Object.keys(patch).length) set(patch);
  for (const effect of effects) {
    if (effect.kind === "notice") say(effect.text);
    else get().quit();
  }
}

/** The page never navigates: a link goes to the system browser through Python (http, https and mailto only), anything
 *  else does nothing. */
function guardLinks(): void {
  const follow = (event: MouseEvent) => {
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!link) return;
    event.preventDefault();
    if (event.type === "auxclick" && event.button !== 1) return;
    const url = link.getAttribute("href") ?? "";
    if (EXTERNAL.test(url)) void attempt(liveCall("shell.openExternal", { url }));
  };
  document.addEventListener("click", follow, true);
  document.addEventListener("auxclick", follow, true);
}

function encode(file: File): Promise<FileUpload> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      resolve({ name: file.name, type: file.type, data: url.slice(url.indexOf(",") + 1) });
    };
    reader.onerror = () => reject(new Error(`读不了「${file.name}」。`));
    reader.readAsDataURL(file);
  });
}

/** What Python took goes into the composer; what it refused, it says why. */
function admitted(id: string, result: AttachmentsResult): void {
  if (result.added.length) get().patchSession(id, (s) => ({ ...s, attachments: [...s.attachments, ...result.added] }));
  if (result.rejected) say(result.rejected);
  else if (result.over) say(result.over); // allowed: the user hears by how much, and is asked when they send
}

/** The message has more attachments than a message usually holds: say by how much and let the user decide. Enter does not
 *  confirm (it is what sent the message a moment ago); the cancel button has the focus. */
function askToSendOver(over: SendOver, send: () => void): void {
  get().openDialog({
    title: "附件比一条消息通常带的多",
    body: `${over.text}。超出了也可以发，要发吗？`,
    confirmLabel: "仍然发送",
    noEnter: true,
    onConfirm: send,
  });
}

async function setting(key: SettingKey, value: SettingValue): Promise<void> {
  const view = await attempt(liveCall("settings.set", { key, value }));
  if (view !== FAILED) set(settingsFields(view));
}

/** Changing the engine of a conversation that has attachments: which of them go along is the user's call, one by one
 *  (Python changes nothing until it is told which). Cancel (or Esc) leaves the conversation where it is. */
function askWhichAttachments(id: string, model: string, choice: HandoffChoice): void {
  const label = get().models.find((m) => m.id === model)?.label ?? model;
  const n = choice.attachments.length;
  get().openDialog({
    title: `带哪些附件去 ${label}？`,
    body: `这个对话里有 ${n} 个附件。换过去之后 ${label} 读不到原来的附件：勾上的带过去，没勾的留在这里。文本的文字会和之前的对话一起交给它，多用一些输入 token；图片和没有文字的 PDF 随下一条消息一起发出（一条消息通常带 ${choice.limits.files} 个、合计 ${Math.floor(choice.limits.bytes / 1024 / 1024)} MB 以内；多带也可以，会告诉你超了多少，由你决定${choice.drafts.count ? `。输入框里已有 ${choice.drafts.count} 个附件，也算在内` : ""}）。`,
    confirmLabel: "换过去",
    onConfirm: () => undefined,
    carry: {
      choice,
      onPick: (attachments, overLimit) =>
        void attempt(liveCall("session.choose", overLimit ? { id, field: "model", value: model, attachments, overLimit } : { id, field: "model", value: model, attachments })),
    },
  });
}

const withoutStatus = (p: ApiProvider): ProviderDraft => {
  const draft = { ...p };
  delete draft.status;
  return draft;
};

function naturalSize(url: string): Promise<{ width?: number; height?: number }> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img.naturalWidth ? { width: img.naturalWidth, height: img.naturalHeight } : {});
    img.onerror = () => resolve({});
    img.src = url;
  });
}

/** The lightbox opens on the thumbnail at once; the whole picture replaces it when Python has sent it. */
async function showWhole(shown: AttachmentView): Promise<void> {
  const r = await attempt(liveCall("attachments.full", { id: get().currentId ?? undefined, attachmentId: shown.id }));
  if (r === FAILED || !r.url) return;
  const size = await naturalSize(r.url);
  if (get().lightbox === shown) set({ lightbox: { ...shown, url: r.url, ...size } });
}

/** Resolves when the page has heard of the conversation (Python announces a new one with its own events), or after a while. */
function known(id: string): Promise<void> {
  return new Promise((resolve) => {
    if (get().sessions[id]) return resolve();
    const stop = () => {
      off();
      window.clearTimeout(timer);
      resolve();
    };
    const off = useStore.subscribe((s) => {
      if (s.sessions[id]) stop();
    });
    const timer = window.setTimeout(stop, 5000);
  });
}

function liveActions(): Partial<Store> {
  const opening = new Set<string>(); // saved rows on their way open: a double click opens them once
  const sending = new Set<string>(); // conversations whose message is on its way
  // How many times the conversation on show has changed. A slow open (an engine's process takes a second or two) answers late;
  // when the user has gone elsewhere meanwhile, the answer must not take them back to the new tab.
  let moved = 0;
  useStore.subscribe((now, before) => {
    if (now.currentId !== before.currentId) moved++;
  });
  /** Show the conversation a call opened, unless the user has changed tabs since they asked for it. */
  const showOpened = (id: string, movedBefore: number) => {
    if (moved === movedBefore || get().currentId === id) set(selectWhenKnown(get(), id));
  };
  return {
    minimizeWindow: () => void attempt(liveCall("window.minimize")),
    toggleMaximize: async () => {
      const maximized = await attempt(liveCall("window.toggleMaximize"));
      if (maximized !== FAILED) set({ maximized });
    },
    quit: () => confirmQuit(() => void attempt(liveCall("window.close"))),

    openSaved: async (id) => {
      const st = get();
      if (st.tabs.includes(id)) return set({ currentId: id, pendingSelect: null });
      const row = st.sessions[id];
      if (!row || opening.has(id)) return;
      opening.add(id);
      const movedBefore = moved;
      try {
        const r = await attempt(liveCall("session.openSaved", { id, projectId: row.projectId, title: row.title }));
        if (r !== FAILED && r.id) showOpened(r.id, movedBefore);
      } finally {
        opening.delete(id);
      }
    },
    createSession: async (projectId) => {
      const movedBefore = moved;
      const r = await attempt(liveCall("session.new", { projectId }));
      if (r === FAILED || !r.id) return null;
      showOpened(r.id, movedBefore);
      set({ lastProject: projectId });
      await known(r.id);
      return r.id;
    },
    archiveSession: async (id) => {
      const s = get().sessions[id];
      if (s && (s.mark === "running" || s.mark === "attention")) return say("这个会话还在运行：先停止它，再归档。");
      const r = await attempt(liveCall("session.archive", { id }));
      if (r !== FAILED && r.ok) say("已归档，可以在侧栏底部的「已归档」里找回。");
    },
    unarchiveSession: (id) => void attempt(liveCall("session.unarchive", { id })),
    setSessionTitle: (id, title) => void attempt(liveCall("session.rename", { id, title })),
    closeTab: (id, force = false) => {
      const close = () => void attempt(liveCall("session.close", { id }));
      if (force) close();
      else confirmClose(id, close);
    },
    reconnect: (id) => void attempt(liveCall("session.reconnect", { id })),
    unqueue: (id, queueId) => void attempt(liveCall("session.unqueue", queueId ? { id, queueId } : { id })),
    editQueued: async (id, queueId, text) => {
      const r = await attempt(liveCall("session.editQueued", { id, queueId, text }));
      if (r !== FAILED && !r.ok) say("这条消息已经发出，或者已经被取走了，没能修改。");
    },
    sendQueued: async (id, queueId) => {
      const r = await attempt(liveCall("session.sendQueued", { id, queueId }));
      if (r !== FAILED && !r.ok && !get().sessions[id]?.queued?.some((m) => m.id === queueId)) say("这条消息已经发出，或者已经被取走了。");
    },
    openFile: async (ref, how = "panel") => {
      const id = get().currentId;
      if (!id || !get().sessions[id]?.mark) return;
      if (how !== "panel") {
        const r = await attempt(liveCall("files.open", { id, path: ref, how }));
        if (r !== FAILED && r.message) say(r.message);
        return;
      }
      // The panel opens at once with what is being asked for; the file replaces the wait (or the sentence says why not).
      const watched = get().agentPanel;
      set({ agentPanel: null, agentTurns: [], filePanel: { sessionId: id, ref, status: "loading" } });
      if (watched) void attempt(liveCall("agents.unwatch", { id: watched.sessionId }));
      try {
        const file = await liveCall("files.read", { id, path: ref });
        if (get().filePanel?.ref === ref) set({ filePanel: { sessionId: id, ref, status: "ready", file } });
      } catch (error) {
        if (get().filePanel?.ref === ref) set({ filePanel: { sessionId: id, ref, status: "error", error: messageOf(error) } });
      }
    },
    refreshPlanUsage: async () => {
      const now = Date.now();
      if (now - planUsageAsked < PLAN_USAGE_EVERY_MS) return;
      planUsageAsked = now;
      try {
        set({ chatgpt: await liveCall("chatgpt.account") });
      } catch {
        /* the figures stay as they were; the badge says why it has none */
      }
    },
    filePage: async (ref, page, width) => {
      const id = get().currentId;
      if (!id) return null;
      try {
        return (await liveCall("files.page", { id, path: ref, page, width })).url;
      } catch {
        return null;
      }
    },
    closeFile: () => set({ filePanel: null }),
    copyPath: async (ref) => {
      const id = get().currentId;
      if (!id) return;
      const r = await attempt(liveCall("files.resolve", { id, path: ref }));
      if (r === FAILED) return;
      try {
        await navigator.clipboard.writeText(r.path);
        say(`已复制路径：${r.path}`);
      } catch {
        say(`没能复制到剪贴板：${r.path}`);
      }
    },
    showPathMenu: (ref, x, y) => set({ pathMenu: { ref, x, y } }),
    hidePathMenu: () => set({ pathMenu: null }),
    openAgent: async (sessionId, agentId) => {
      set({ agentPanel: { sessionId, agentId }, agentTurns: [], filePanel: null });
      const r = await attempt(liveCall("agents.watch", { id: sessionId, agentId }));
      if (r === FAILED) set({ agentPanel: null, agentTurns: [] });
    },
    closeAgent: () => {
      const panel = get().agentPanel;
      set({ agentPanel: null, agentTurns: [] });
      if (panel) void attempt(liveCall("agents.unwatch", { id: panel.sessionId }));
    },
    addProject: () => void attempt(liveCall("project.add", {})),
    removeProject: (id) => confirmRemoveProject(id, () => void attempt(liveCall("project.remove", { id }))),
    refreshProjects: async (id) => {
      if ((await attempt(liveCall("project.refresh", id ? { id } : {}))) !== FAILED) say("已刷新会话列表。");
    },
    revealProject: (id) => void attempt(liveCall("project.reveal", { id })),

    attachFiles: async (id, files) => {
      const big = files.filter((f) => f.size > MAX_FILE_BYTES);
      if (big.length) say(`${big.map((f) => `「${f.name}」`).join("")}超过 64 MiB，没有添加。`);
      const rest = files.filter((f) => f.size <= MAX_FILE_BYTES);
      if (rest.length === 0) return;
      let uploads: FileUpload[];
      try {
        uploads = await Promise.all(rest.map(encode));
      } catch (error) {
        return say(messageOf(error));
      }
      const r = await attempt(liveCall("attachments.add", { id, files: uploads }));
      if (r !== FAILED) admitted(id, r);
    },
    pickAttachments: async (id) => {
      const r = await attempt(liveCall("attachments.pick", { id }));
      if (r !== FAILED) admitted(id, r);
    },
    removeAttachment: async (id, attachmentId) => {
      if ((await attempt(liveCall("attachments.remove", { id, attachmentId }))) === FAILED) return;
      get().patchSession(id, (s) => ({ ...s, attachments: s.attachments.filter((a) => a.id !== attachmentId) }));
    },
    send: async (id, how) => {
      const s = get().sessions[id];
      if (!s || sending.has(id)) return;
      // A session that is still connecting takes the message too (Python keeps it and sends it when ready), and so does
      // one whose turn is running: queued behind the turn, or given to it, as `how` says.
      const running = s.mark === "running" || s.mark === "attention";
      if (s.mark !== "ready" && s.mark !== "opening" && !running) return say("连接已断开：重新连接后才能发送。");
      const text = s.draft;
      const attachmentIds = s.attachments.map((a) => a.id);
      if (!text.trim() && attachmentIds.length === 0) return;
      // The turn waits for the answer to a question card: a message typed now would only queue behind it, and the user
      // would believe they had answered. The words stay in the box.
      if (running && s.turns.at(-1)?.prompts?.some((p) => p.kind === "question" && p.status === "pending")) {
        return say("本轮在等你回答上面的问题：请在问题卡片里作答（卡片里也能写自己的答案）。输入框里的字还在。");
      }
      // More attachments than a message usually holds is the user's call: Python sends nothing and says by how much, the
      // page asks, and sends the same message again with the user's word (`confirmOver`).
      const submit = async (confirmOver: boolean): Promise<void> => {
        if (sending.has(id)) return;
        sending.add(id);
        try {
          const r = await attempt(
            liveCall("session.send", { id, text, attachmentIds, ...(confirmOver ? { confirmOver } : {}), ...(running ? { how: how ?? get().followUp } : {}) }),
          );
          if (r === FAILED) return;
          if (!r.ok && r.over) return askToSendOver(r.over, () => void submit(true));
          // What was typed while the message was on its way stays.
          get().patchSession(id, (x) => ({ ...x, draft: x.draft === text ? "" : x.draft, attachments: x.attachments.filter((a) => !attachmentIds.includes(a.id)) }));
        } finally {
          sending.delete(id);
        }
      };
      await submit(false);
    },
    stop: (id) => void attempt(liveCall("session.stop", { id })),
    compact: async (id) => {
      const r = await attempt(liveCall("session.compact", { id }));
      if (r !== FAILED && r.text) say(r.text);
    },
    startGoal: async (id, text) => {
      const r = await attempt(liveCall("session.goal", { id, text }));
      if (r !== FAILED && r.text) say(r.text);
    },
    refreshSkills: async (id) => {
      try {
        set({ skills: (await liveCall("skills.list", id ? { id } : {})).items });
      } catch {
        /* the menu keeps what it had */
      }
    },
    choose: async (field, value) => {
      const id = get().currentId;
      const r = await attempt(liveCall("session.choose", id ? { id, field, value } : { field, value }));
      if (r !== FAILED && id && r.choose) askWhichAttachments(id, value, r.choose);
    },
    setDefault: (field, value) => void attempt(liveCall("session.choose", { field, value })),
    setModelsVisible: async (ids, visible) => {
      const view = await attempt(liveCall("models.setVisible", { ids, visible }));
      if (view !== FAILED) set(settingsFields(view));
    },
    // A sub-agent's panel shows its run under the id "<conversation>::<agent>": the answer goes to that agent.
    answerPermission: (id, promptId, value) => {
      const [cid, agentId] = id.split("::");
      void attempt(liveCall("session.answerPermission", agentId ? { id: cid!, agentId, promptId, value } : { id, promptId, value }));
    },
    answerQuestion: (id, promptId, answers) => {
      const [cid, agentId] = id.split("::");
      void attempt(liveCall("session.answerQuestion", agentId ? { id: cid!, agentId, promptId, answers } : { id, promptId, answers }));
    },

    setTheme: (theme) => void setting("theme", theme),
    setFontScale: (scale) => void setting("fontScale", scale),
    setFollowUp: (how) => void setting("followUp", how),
    setMaxOpen: (n) => void setting("maxOpen", Math.max(1, Math.min(16, n))),
    setMemoryEnabled: (on) => void setting("memoryEnabled", on),
    setMemoryRecall: (on) => void setting("memoryRecall", on),
    setMemoryCapture: (on) => void setting("memoryCapture", on),
    setMemoryCurate: (on) => void setting("memoryCurate", on),
    setMemoryEngines: (engines) => void setting("memoryEngines", engines),
    setCompactAuto: (on) => void setting("compactAuto", on),
    setThinkChinese: (on) => void setting("thinkChinese", on),
    setCompactAt: (percent) => void setting("compactAt", clampCompactAt(percent)),
    setDevinPath: (path) => void setting("devinPath", path),
    setSidebarWidth: (px, commit = false) => {
      set({ sidebarWidth: px });
      if (commit) void setting("sidebarWidth", Math.round(px));
    },

    saveProvider: async (draft) => {
      try {
        const saved = await liveCall("providers.save", { draft: withoutStatus(draft) });
        set((s) => ({ providers: s.providers.some((p) => p.id === saved.id) ? s.providers.map((p) => (p.id === saved.id ? saved : p)) : [...s.providers, saved] }));
        say(`已保存「${saved.name}」：${saved.models.length} 个模型现在可以在模型菜单里选。`);
        return true;
      } catch (error) {
        say(`没有保存成功：${messageOf(error)}`);
        return false;
      }
    },
    removeProvider: (id) =>
      confirmDeleteProvider(id, async (p) => {
        if ((await attempt(liveCall("providers.remove", { id }))) === FAILED) return;
        set((s) => ({ providers: s.providers.filter((x) => x.id !== id), providerEdit: null }));
        say(`已删除「${p.name}」。`);
      }),
    testProvider: async (id) => {
      const p = get().providers.find((x) => x.id === id);
      if (!p) return;
      const show = (status: ApiProvider["status"]) => set((s) => ({ providers: s.providers.map((x) => (x.id === id ? { ...x, status } : x)) }));
      show({ ok: true, text: "正在测试…" });
      const r = await attempt(liveCall("providers.test", { draft: withoutStatus(p) }));
      show(r === FAILED ? p.status : { ok: r.ok, text: r.text });
    },
    devinLogin: async () => {
      const before = get().devin;
      set({ devin: { ...before, auth: "signing-in" } });
      const devin = await attempt(liveCall("devin.login"));
      if (devin === FAILED) return set({ devin: before });
      set({ devin });
      if (devin.auth === "signed-in") say(`已登录 Devin${devin.email ? `：${devin.email}` : ""}${devin.plan ? `（${devin.plan}）` : ""}。`);
    },
    devinCancel: async () => {
      const devin = await attempt(liveCall("devin.cancelLogin"));
      if (devin !== FAILED) set({ devin });
    },
    devinLogout: async () => {
      const devin = await attempt(liveCall("devin.logout"));
      if (devin === FAILED) return;
      set({ devin });
      say("已退出 Devin（Devin 桌面版和命令行共用这个登录，它们也会退出）。打开着的 Devin 会话在下次发送时会提示重新登录。");
    },
    devinEnable: async (on) => {
      const devin = await attempt(liveCall("devin.setEnabled", { on }));
      if (devin !== FAILED) set({ devin });
    },
    chatgptLogin: async (options) => {
      const before = get().chatgpt;
      set({ chatgpt: chatgptView({ ...before, signingIn: true }) });
      const chatgpt = await attempt(liveCall("chatgpt.login", options ?? {}));
      if (chatgpt === FAILED) return set({ chatgpt: before });
      set({ chatgpt: chatgptView(chatgpt) });
      if (chatgpt.auth === "signed-in") say(chatgpt.planEnabled ? `已登录 ChatGPT：${chatgpt.email}。` : "已登录 ChatGPT，但没有允许使用套餐：在模型菜单里选「允许使用 ChatGPT 套餐」。");
    },
    chatgptCancel: async () => {
      const chatgpt = await attempt(liveCall("chatgpt.cancelLogin"));
      if (chatgpt !== FAILED) set({ chatgpt: chatgptView(chatgpt) });
    },
    chatgptLogout: async (clientId) => {
      const chatgpt = await attempt(liveCall("chatgpt.logout", clientId ? { clientId } : {}));
      if (chatgpt === FAILED) return;
      set({ chatgpt: chatgptView(chatgpt) });
      say("已退出 ChatGPT。打开着的 ChatGPT Plan 会话在下次发送时会提示重新登录。");
    },
    chatgptUse: async (clientId) => {
      const chatgpt = await attempt(liveCall("chatgpt.use", { clientId }));
      if (chatgpt !== FAILED) set({ chatgpt: chatgptView(chatgpt) });
    },
    chatgptEnable: async (on) => {
      const chatgpt = await attempt(liveCall("chatgpt.setEnabled", { on }));
      if (chatgpt !== FAILED) set({ chatgpt: chatgptView(chatgpt) });
    },
    chatgptWelcome: async () => {
      set({ chatgpt: { ...get().chatgpt, welcomed: true } });
      const chatgpt = await attempt(liveCall("chatgpt.welcomed"));
      if (chatgpt !== FAILED) set({ chatgpt: chatgptView(chatgpt) });
    },
    recheckDevin: async () => {
      const devin = await attempt(liveCall("devin.account"));
      if (devin === FAILED) return;
      set({ devin });
      say(devin.cli === "missing" ? "已重新检测：仍然没有找到 devin。" : "已重新检测：找到了 devin。");
    },
    openLightbox: (a) => {
      set({ lightbox: a });
      if (a?.kind === "image") void showWhole(a);
    },
  };
}
