import { create } from "zustand";
import type { ApiProvider, AttachmentView, Engine, FileView, MemoryState, ModelChoice, ProviderDraft, Session, SessionSettings, SkillItem, SwitchBlock, Turn } from "./types";
import type { FollowUp, HandoffChoice, QuestionAnswer, SavedRow } from "./protocol";
import {
  API_PROVIDERS,
  CHATGPT_ACCOUNT,
  CHATGPT_MODELS,
  CODEX_ACCOUNT,
  CODEX_MODELS,
  DEVIN_ACCOUNT,
  DEVIN_MODELS,
  FACTORY_ACCOUNT,
  FACTORY_MODELS,
  OPEN_TABS,
  PROJECTS,
  SESSIONS,
  modelsOfProvider,
} from "./mock/data";
import { MOCK_MEMORY_LATEST, MOCK_PENDING } from "./mock/memory";
import { readFiles } from "./mock/attachments";
import { cancelRun, runTurn, settlePrompt } from "./mock/simulator";
import { bridge } from "./bridge-instance";
import { engineLabel, engineOfSource, findModel } from "./lib/models";
import { handoffOf } from "./lib/handoff";
import { quitWarning } from "./lib/quit";
import { effortLabel, permissionsFor, startingLevel } from "./lib/vocab";
import { COMPACT_AT_DEFAULT, clampCompactAt, emptyLiveData, type LiveData } from "./liveReducer";
import { LIVE, liveSeed } from "./mode";

/** The skills and commands the design page shows after a slash (the live app reads the user's files). */
const DEMO_SKILLS: SkillItem[] = [
  { name: "summarize-diff", description: "总结暂存区的改动，三五条要点（演示）", hint: "", kind: "skill", source: "project" },
  { name: "code-review", description: "按清单审一遍这个分支（演示）", hint: "<分支>", kind: "command", source: "personal" },
];

/** Factory's models, Codex's, Devin's, the ChatGPT plan's, and those of the user's API providers: the one list the model menu and the settings use. */
export function buildModels(providers: ApiProvider[]): ModelChoice[] {
  return [...FACTORY_MODELS, ...CODEX_MODELS, ...CHATGPT_MODELS, ...DEVIN_MODELS, ...providers.flatMap(modelsOfProvider)];
}

/** The settings a session (or the defaults) end up with when the model changes: whatever the new model and its engine
 *  do not know is replaced by that model's own starting value. `changed` names what had to be replaced. */
export function settingsForModel(models: ModelChoice[], base: SessionSettings, modelId: string): { settings: SessionSettings; changed: ("reasoning" | "autonomy")[] } {
  const m = findModel(models, modelId);
  const settings = { ...base, model: modelId };
  const changed: ("reasoning" | "autonomy")[] = [];
  if (!m) return { settings, changed };
  if (m.efforts.length === 0 ? settings.reasoning !== "" : !m.efforts.includes(settings.reasoning)) {
    settings.reasoning = m.defaultEffort;
    changed.push("reasoning");
  }
  const engine = engineOfSource(m.source);
  if (!permissionsFor(engine).some((l) => l.value === settings.autonomy)) {
    settings.autonomy = startingLevel(engine);
    changed.push("autonomy");
  }
  return { settings, changed };
}

/** The project the first message box starts a conversation in: the one chosen there, else the one used last, else the first. */
export function landingProjectId(s: Pick<LiveData, "projects" | "lastProject"> & { landing: Landing }): string | null {
  const known = (id: string | null | undefined) => (id && s.projects.some((p) => p.id === id) ? id : null);
  return known(s.landing.projectId) ?? known(s.lastProject) ?? s.projects[0]?.id ?? null;
}

export interface DialogSpec {
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  /** the attachments of a conversation that changes engine: a list to tick, one by one. Nothing is ticked at first and
   *  Enter does not confirm: what goes along is the user's choice alone. `onPick` gets the ids ticked (maybe none). */
  carry?: { choice: HandoffChoice; onPick: (ids: string[], overLimit: boolean) => void };
  /** Enter does not confirm: a question the user must answer on purpose (Enter is also what sent the message) */
  noEnter?: boolean;
  /** a line of text to fill in (Enter or the button hands it to `onSubmit`); `onConfirm` is not used */
  input?: { value: string; placeholder?: string; onSubmit: (value: string) => void };
}

/** The first message box, shown while no conversation is open. Nothing exists in Python until a message is sent from it. */
export interface Landing {
  draft: string;
  /** the project chosen for the new conversation; null: the last one used, else the first */
  projectId: string | null;
  /** the conversation is being opened for the message just sent */
  starting: boolean;
}

/** The data (owned by Python in the live app, mock data in the design review), what only the page knows, and the
 *  actions. The actions below are the design mock's; live.ts replaces them with calls to Python. */
export interface Store extends LiveData {
  // ---- what only the page knows
  /** live app: app.init has answered (until then only the title bar is drawn); the mock is ready at once */
  ready: boolean;
  /** live app: why it could not connect to Python */
  bootError: string | null;
  settingsPage: string | null; // which page of the settings dialog is open; null: closed
  providerEdit: string | null; // the provider being edited in the settings: its id, "new", or null
  sidebarOpen: boolean;
  openGroups: Record<string, boolean>;
  openItems: Record<string, boolean>;
  dialog: DialogSpec | null;
  lightbox: AttachmentView | null;
  notice: { id: number; text: string } | null;
  reviewOpen: boolean;
  dragging: boolean;
  landing: Landing;
  /** the skills and custom commands the command menu adds to its own (`skills.list`) */
  skills: SkillItem[];

  // ---- actions
  minimizeWindow: () => void;
  toggleMaximize: () => Promise<void>;
  /** closing the window: asks first when a session is still working */
  quit: () => void;
  select: (id: string | null) => void;
  openSaved: (id: string) => void;
  /** show the first message box (in `projectId` when given); the conversation itself is made when the first message goes */
  newSession: (projectId?: string) => void;
  setLandingDraft: (draft: string) => void;
  setLandingProject: (projectId: string | null) => void;
  /** open a conversation in `projectId`; its id, or null when it could not be opened (the user was told why) */
  createSession: (projectId: string) => Promise<string | null>;
  /** send the first message box: the conversation is opened for it, then the message goes */
  startSession: () => Promise<void>;
  /** the first message box needs a conversation to put files in: open it now, with what is typed so far */
  adoptLanding: () => Promise<string | null>;
  /** hide a conversation (open ones are closed first) / bring it back / name it here */
  archiveSession: (id: string) => void;
  unarchiveSession: (id: string) => void;
  /** ask for a new name (a line of text), then `setSessionTitle` */
  renameSession: (id: string) => void;
  /** the name shown here for the conversation; "" gives the engine's own title back */
  setSessionTitle: (id: string, title: string) => void;
  closeTab: (id: string, force?: boolean) => void;
  reconnect: (id: string) => void;
  /** take a queued message back into the composer (with no `queueId`: all of them) */
  unqueue: (id: string, queueId?: string) => void;
  /** change the words of a waiting message (it keeps its place) / give a waiting message to the running turn now */
  editQueued: (id: string, queueId: string, text: string) => void;
  sendQueued: (id: string, queueId: string) => void;
  /** open a sub-agent's run in the side panel / close the panel */
  openAgent: (sessionId: string, agentId: string) => void;
  closeAgent: () => void;
  /** open a file the conversation named (`ref` as the model wrote it): in the side panel, or outside the window with the
   *  default program / the system's "open with" / its folder */
  openFile: (ref: string, how?: "panel" | "default" | "choose" | "reveal") => void;
  /** read the Codex account again (its limits are what the ChatGPT Plan badge shows); at most once in a while */
  refreshPlanUsage: () => void;
  /** a page of the PDF (or the picture) in the file panel, as a `data:` URI; null when it could not be drawn */
  filePage: (ref: string, page: number, width: number) => Promise<string | null>;
  closeFile: () => void;
  copyPath: (ref: string) => void;
  /** the menu of a path (right click) */
  showPathMenu: (ref: string, x: number, y: number) => void;
  hidePathMenu: () => void;
  toggleProject: (id: string) => void;
  addProject: () => void;
  removeProject: (id: string) => void;
  /** reload the saved sessions of one project, or of all of them */
  refreshProjects: (id?: string) => void;
  /** show the project folder in Explorer */
  revealProject: (id: string) => void;
  setDraft: (id: string, draft: string) => void;
  /** dropped or pasted files become attachments of the session's next message */
  attachFiles: (id: string, files: File[]) => Promise<void>;
  /** the paperclip in the live app: Windows' own file dialog */
  pickAttachments: (id: string) => Promise<void>;
  removeAttachment: (id: string, attachmentId: string) => void;
  /** `how`, while a turn runs: `queue` holds the message until the turn is over, `steer` gives it to the running turn now */
  send: (id: string, how?: FollowUp) => void;
  stop: (id: string) => void;
  /** `/compact`: summarise the conversation now (the own loop, Droid and Codex do; Devin manages its own context) */
  compact: (id: string) => Promise<void>;
  /** `/goal`: a goal the model goes on with, round after round, until it is done or needs the user (any engine) */
  startGoal: (id: string, text: string) => Promise<void>;
  /** read the skills and custom commands again, for the project of a conversation (or the first message box's) */
  refreshSkills: (id?: string) => void;
  choose: (field: keyof SessionSettings, value: string) => void;
  answerPermission: (sessionId: string, promptId: string, value: string) => void;
  /** answers: one per question (the chosen options of a multi-select one), or null when the user skips the questions */
  answerQuestion: (sessionId: string, promptId: string, answers: QuestionAnswer[] | null) => void;

  toggleGroup: (key: string) => void;
  toggleItem: (key: string) => void;
  setTheme: (t: LiveData["themeChoice"]) => void;
  setFontScale: (f: LiveData["fontScale"]) => void;
  setFollowUp: (f: FollowUp) => void;
  openSettings: (page: string | null, providerEdit?: string | null) => void;
  setProviderEdit: (id: string | null) => void;
  saveProvider: (draft: ProviderDraft) => Promise<boolean>;
  removeProvider: (id: string) => void;
  testProvider: (id: string) => Promise<void>;
  codexLogin: () => Promise<void>;
  codexCancel: () => Promise<void>;
  codexLogout: () => Promise<void>;
  codexEnable: (on: boolean) => Promise<void>;
  devinLogin: () => Promise<void>;
  devinCancel: () => Promise<void>;
  devinLogout: () => Promise<void>;
  devinEnable: (on: boolean) => Promise<void>;
  /** "Continue with ChatGPT": the browser opens; resolves when the user has finished there. `add`: another account;
   *  `forceConsent`: ask again for the permission to use the plan */
  chatgptLogin: (options?: { add?: boolean; forceConsent?: boolean }) => Promise<void>;
  chatgptCancel: () => Promise<void>;
  chatgptLogout: (clientId?: string) => Promise<void>;
  chatgptUse: (clientId: string) => Promise<void>;
  chatgptEnable: (on: boolean) => Promise<void>;
  /** the user dismissed "You're using your ChatGPT plan" */
  chatgptWelcome: () => Promise<void>;
  /** look for droid.exe again (and its version) */
  recheckDroid: () => Promise<void>;
  /** ask Codex again whether it is there and signed in */
  recheckCodex: () => Promise<void>;
  /** ask Devin again whether it is there and signed in */
  recheckDevin: () => Promise<void>;
  setDefault: (field: keyof SessionSettings, value: string) => void;
  /** show or hide models in the conversation's model menu (the settings page's lists) */
  setModelsVisible: (ids: string[], visible: boolean) => void;
  setMaxOpen: (n: number) => void;
  /** the shared memory: the master switch, the engines that get it, and the state the memory page last heard of */
  setMemoryEnabled: (on: boolean) => void;
  setMemoryRecall: (on: boolean) => void;
  setMemoryCapture: (on: boolean) => void;
  setMemoryCurate: (on: boolean) => void;
  setMemoryEngines: (engines: Engine[]) => void;
  /** ChatGPT Plan: summarise a long conversation before it outgrows the model's window (switch), at this percent of it (50 to 95) */
  setCompactAuto: (on: boolean) => void;
  setCompactAt: (percent: number) => void;
  /** ask every model to write its thinking in Chinese */
  setThinkChinese: (on: boolean) => void;
  setMemoryState: (state: MemoryState) => void;
  setDroidPath: (p: string) => void;
  setCodexPath: (p: string) => void;
  setDevinPath: (p: string) => void;
  /** while the splitter is dragged; `commit` once it is let go (the live app then saves it) */
  setSidebarWidth: (px: number, commit?: boolean) => void;
  setSidebarOpen: (open: boolean) => void;
  setReviewOpen: (open: boolean) => void;
  setDragging: (v: boolean) => void;
  openDialog: (d: DialogSpec | null) => void;
  openLightbox: (a: AttachmentView | null) => void;
  say: (text: string) => void;
  patchSession: (id: string, fn: (s: Session) => Session) => void;
  patchTurn: (sessionId: string, turnId: string, fn: (t: Turn) => Turn) => void;
}

/** The key of an activity group's or a tool row's open state. Python numbers blocks per conversation ("b3" exists in
 *  every one of them), so the key carries the conversation. */
export const openKey = (sessionId: string, id: string) => `${sessionId}/${id}`;

let noticeTimer: number | undefined;
let sessionCounter = 0;
let projectCounter = 0;
let switchCounter = 0;

const index = (list: Session[]) => Object.fromEntries(list.map((s) => [s.id, s]));

const HISTORY = (title: string): Turn[] => [
  {
    id: `h-${title}`,
    status: "done",
    seconds: 18,
    steps: 3,
    prompts: [],
    blocks: [
      { kind: "user", id: `hu-${title}`, text: title },
      { kind: "text", id: `hx-${title}`, markdown: "这是一个已保存的历史会话（演示数据）：打开时 Droid 会恢复它，历史消息按保存的样子显示。" },
    ],
  },
];

function mockData(): LiveData {
  return {
    projects: PROJECTS,
    sessions: index(SESSIONS),
    archived: [],
    tabs: OPEN_TABS,
    currentId: "s-file-ops",
    pendingSelect: null,
    models: buildModels(API_PROVIDERS),
    defaults: { model: "claude-haiku-5-5", reasoning: "low", autonomy: "off", mode: "auto" },
    factory: FACTORY_ACCOUNT,
    codex: CODEX_ACCOUNT,
    devin: DEVIN_ACCOUNT,
    chatgpt: CHATGPT_ACCOUNT,
    providers: API_PROVIDERS,
    maxOpen: 4,
    droidPath: "",
    droidFound: "C:\\Users\\me\\AppData\\Local\\Factory\\bin\\droid.exe",
    codexPath: "",
    devinPath: "",
    hiddenModels: [],
    themeChoice: "dark",
    fontScale: "md",
    followUp: "queue",
    sidebarWidth: 288,
    memoryEnabled: true,
    memoryRecall: true,
    memoryCapture: true,
    memoryCurate: true,
    memoryEngines: ["droid", "codex", "devin", "chatgpt"],
    compactAuto: true,
    compactAt: COMPACT_AT_DEFAULT,
    thinkChinese: true,
    lastProject: "",
    memory: { latest: MOCK_MEMORY_LATEST, unseen: 2, seen: MOCK_MEMORY_LATEST - 2, pending: MOCK_PENDING },
    maximized: false,
    offline: false,
    agentPanel: null,
    agentTurns: [],
    filePanel: null,
    pathMenu: null,
  };
}

function startData(): LiveData {
  if (!LIVE) return mockData();
  const seed = liveSeed();
  return emptyLiveData(seed.theme, seed.fontScale);
}

export const useStore = create<Store>((set, get) => ({
  ...startData(),
  ready: !LIVE,
  bootError: null,
  settingsPage: null,
  providerEdit: null,
  sidebarOpen: true,
  openGroups: {},
  openItems: {},
  dialog: null,
  lightbox: null,
  notice: null,
  reviewOpen: !LIVE,
  dragging: false,
  landing: { draft: "", projectId: null, starting: false },
  skills: [],

  minimizeWindow: () => {
    void bridge.window.minimize();
    get().say("（演示）这里会把窗口最小化。");
  },
  toggleMaximize: async () => set({ maximized: await bridge.window.toggleMaximize() }),
  quit: () =>
    confirmQuit(() => {
      void bridge.window.close();
      get().say("（演示）这里会关闭窗口。");
    }),

  select: (id) => set({ currentId: id, pendingSelect: null }),

  openSaved: (id) => {
    const s = get().sessions[id];
    if (!s) return;
    if (get().tabs.includes(id)) return set({ currentId: id });
    get().patchSession(id, (x) => ({ ...x, mark: "opening", workingLabel: "正在连接…", turns: x.turns.length ? x.turns : HISTORY(x.title) }));
    set((st) => ({ tabs: [...st.tabs, id], currentId: id }));
    window.setTimeout(() => get().patchSession(id, (x) => ({ ...x, mark: "ready", workingLabel: "就绪" })), 700);
  },

  newSession: (projectId) => {
    // (from a conversation: its project is where the next one starts, as it always was)
    set((s) => ({ currentId: null, pendingSelect: null, landing: { ...s.landing, projectId: projectId ?? s.sessions[s.currentId ?? ""]?.projectId ?? s.landing.projectId } }));
    window.dispatchEvent(new Event("wc:focus-composer"));
  },

  setLandingDraft: (draft) => set((s) => ({ landing: { ...s.landing, draft } })),
  setLandingProject: (projectId) => set((s) => ({ landing: { ...s.landing, projectId } })),

  createSession: async (pid) => {
    const st = get();
    const id = `s-new-${++sessionCounter}`;
    const settings = { ...st.defaults };
    const m = findModel(st.models, settings.model);
    const session: Session = {
      id,
      projectId: pid,
      title: "新会话",
      mark: "opening",
      modified: "刚刚",
      turns: [],
      usage: null,
      context: { used: null, model: settings.model, window: m?.window ?? 250_000 },
      settings,
      workingLabel: "正在连接…",
      draft: "",
      attachments: [],
    };
    set((s) => ({
      sessions: { ...s.sessions, [id]: session },
      projects: s.projects.map((p) => (p.id === pid ? { ...p, collapsed: false, sessionIds: [id, ...p.sessionIds] } : p)),
      tabs: [...s.tabs, id],
      currentId: id,
      lastProject: pid,
    }));
    window.setTimeout(() => get().patchSession(id, (x) => ({ ...x, mark: "ready", workingLabel: "就绪" })), 600);
    // (the demo connects in 0.6 s; the real application keeps a message sent before that, this one waits for it)
    await new Promise((resolve) => window.setTimeout(resolve, 650));
    return id;
  },

  adoptLanding: async () => {
    const st = get();
    if (st.landing.starting) return null;
    const projectId = landingProjectId(st);
    if (!projectId) {
      st.say("请先添加一个项目文件夹。");
      st.addProject();
      return null;
    }
    set((s) => ({ landing: { ...s.landing, starting: true } }));
    let id: string | null = null;
    try {
      id = await get().createSession(projectId);
    } finally {
      set((s) => ({ landing: { ...s.landing, starting: false } }));
    }
    if (!id) return null;
    if (!get().sessions[id]) {
      get().say("会话没有准备好，输入框里的内容还在，请再发一次。");
      return null;
    }
    const draft = get().landing.draft;
    get().patchSession(id, (x) => ({ ...x, draft }));
    set({ landing: { draft: "", projectId: null, starting: false } });
    return id;
  },

  startSession: async () => {
    const st = get();
    if (!st.landing.draft.trim() || st.landing.starting) return;
    const id = await get().adoptLanding();
    if (id) get().send(id);
  },

  archiveSession: (id) => {
    const s = get().sessions[id];
    if (!s) return;
    if (s.mark === "running" || s.mark === "attention") return get().say("这个会话还在运行：先停止它，再归档。");
    if (s.mark) get().closeTab(id, true);
    const row: SavedRow = {
      id: s.id,
      projectId: s.projectId,
      title: s.title,
      modified: s.modified,
      modifiedAt: s.modifiedAt ?? Date.now(),
      engine: s.engine ?? "droid",
      settings: { ...s.settings, model: "" },
      usage: null,
      context: { used: null, window: 0, model: "", compacted: false },
      workingLabel: "",
    };
    set((st) => ({
      archived: [row, ...st.archived.filter((r) => r.id !== id)],
      projects: st.projects.map((p) => ({ ...p, sessionIds: p.sessionIds.filter((x) => x !== id) })),
    }));
    get().say("已归档，可以在侧栏底部的「已归档」里找回。");
  },
  unarchiveSession: (id) => {
    const row = get().archived.find((r) => r.id === id);
    if (!row) return;
    set((st) => ({
      archived: st.archived.filter((r) => r.id !== id),
      projects: st.projects.map((p) => (p.id === row.projectId ? { ...p, sessionIds: [id, ...p.sessionIds] } : p)),
    }));
  },
  renameSession: (id) => {
    const s = get().sessions[id] ?? get().archived.find((r) => r.id === id);
    if (!s) return;
    get().openDialog({
      title: "重命名会话",
      body: "只改 WittyCode 里显示的名字，不改 Droid、Codex、Devin 自己保存的标题。留空则恢复原来的标题。",
      confirmLabel: "保存",
      onConfirm: () => undefined,
      input: { value: s.title, placeholder: "会话名称", onSubmit: (value) => get().setSessionTitle(id, value) },
    });
  },
  setSessionTitle: (id, title) => {
    const name = title.trim();
    if (!name) return get().say("（演示）留空会恢复引擎自己的标题。");
    get().patchSession(id, (x) => ({ ...x, title: name }));
  },

  closeTab: (id, force = false) => {
    const close = () => {
      cancelRun(id);
      set((st) => {
        const at = st.tabs.indexOf(id);
        const tabs = st.tabs.filter((t) => t !== id);
        const currentId = st.currentId === id ? (tabs[Math.min(at, tabs.length - 1)] ?? null) : st.currentId;
        const sessions = { ...st.sessions, [id]: { ...st.sessions[id]!, mark: undefined, draft: "" } };
        return { tabs, currentId, sessions };
      });
    };
    if (!get().sessions[id]) return;
    if (force) close();
    else confirmClose(id, close);
  },

  reconnect: (id) => {
    get().patchSession(id, (s) => ({
      ...s,
      mark: "opening",
      workingLabel: "正在连接…",
      turns: s.turns.map((t) => ({
        ...t,
        blocks: t.blocks.map((b) => (b.kind === "error" ? { ...b, resolved: true } : b)),
      })),
    }));
    window.setTimeout(() => get().patchSession(id, (s) => ({ ...s, mark: "ready", workingLabel: "就绪" })), 900);
  },

  unqueue: (id, queueId) => {
    // the design prototype: a queue is only ever shown, but the buttons work on it
    get().patchSession(id, (s) => ({ ...s, queued: queueId ? s.queued?.filter((m) => m.id !== queueId) : undefined }));
  },
  editQueued: (id, queueId, text) => {
    get().patchSession(id, (s) => ({ ...s, queued: s.queued?.map((m) => (m.id === queueId ? { ...m, text: text.trim() } : m)) }));
  },
  sendQueued: (id, queueId) => {
    get().patchSession(id, (s) => ({ ...s, queued: s.queued?.filter((m) => m.id !== queueId) }));
    get().say("（演示）已交给正在运行的这一轮。");
  },
  openAgent: () => undefined, // ... and have no sub-agents
  closeAgent: () => set({ agentPanel: null, agentTurns: [] }),
  openFile: (ref, how = "panel") => {
    const sessionId = get().currentId;
    if (!sessionId) return;
    if (how !== "panel") return get().say(`（演示）${{ default: "用默认程序打开", choose: "选择打开方式", reveal: "在文件夹中显示" }[how]}：${ref}`);
    const name = ref.replace(/:\d+(-\d+)?$/, "").split(/[\\/]/).pop() ?? ref;
    const preview = /\.pdf$/i.test(name) ? "pdf" : /\.(png|jpe?g|gif|webp|svg)$/i.test(name) ? "image" : null;
    if (preview) {
      const pages = preview === "pdf" ? Array.from({ length: 4 }, () => ({ w: 595, h: 842 })) : [];
      const file: FileView = { kind: preview, pages, path: `C:\\Users\\me\\proj\\${name}`, name, dir: "C:\\Users\\me\\proj", text: "", size: 183_400, lines: 0, encoding: "", truncated: false, markdown: false, line: null, lineEnd: null };
      set({ agentPanel: null, agentTurns: [], filePanel: { sessionId, ref, status: "ready", file } });
      return;
    }
    const markdown = /\.md$/i.test(name);
    const text = markdown
      ? "# 示例文档\n\n这是设计页里的示例内容，真实窗口里是文件本身。\n\n- 第一项\n- 第二项，带一个 `docs/handoff.md` 路径\n\n| 列 | 值 |\n| --- | --- |\n| a | 1 |\n"
      : Array.from({ length: 40 }, (_, i) => `line ${i + 1}: ${"x".repeat((i * 7) % 30)}`).join("\n");
    const range = /:(\d+)(?:-(\d+))?$/.exec(ref);
    const line = Number(range?.[1]) || null;
    const lineEnd = Number(range?.[2]) || null;
    set({
      agentPanel: null,
      agentTurns: [],
      filePanel: {
        sessionId,
        ref,
        status: "ready",
        file: { kind: "text", pages: [], path: `C:\\Users\\me\\proj\\${name}`, name, dir: "C:\\Users\\me\\proj", text, size: text.length, lines: text.split("\n").length, encoding: "UTF-8", truncated: false, markdown, line, lineEnd },
      },
    });
  },
  refreshPlanUsage: () => undefined, // (the demo's figures are fixed)
  filePage: async (_ref, page) => {
    const label = `第 ${page + 1} 页（演示）`;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="595" height="842"><rect width="595" height="842" fill="#fff"/><text x="40" y="80" font-size="28" fill="#333" font-family="sans-serif">${label}</text></svg>`;
    return `data:image/svg+xml;base64,${btoa(svg)}`;
  },
  closeFile: () => set({ filePanel: null }),
  copyPath: (ref) => get().say(`（演示）已复制路径：${ref}`),
  showPathMenu: (ref, x, y) => set({ pathMenu: { ref, x, y } }),
  hidePathMenu: () => set({ pathMenu: null }),

  toggleProject: (id) => set((s) => ({ projects: s.projects.map((p) => (p.id === id ? { ...p, collapsed: !p.collapsed } : p)) })),

  addProject: () => {
    const id = `p-new-${++projectCounter}`;
    set((s) => ({
      projects: [...s.projects, { id, name: `my-project-${projectCounter}`, path: `D:\\Projects\\my-project-${projectCounter}`, collapsed: false, sessionIds: [] }],
    }));
    get().say("已添加项目文件夹（演示）。");
  },

  removeProject: (id) =>
    confirmRemoveProject(id, (open) => {
      open.forEach((sid) => get().closeTab(sid, true));
      set((s) => ({ projects: s.projects.filter((x) => x.id !== id) }));
    }),

  refreshProjects: () => get().say("已刷新会话列表。"),
  revealProject: () => get().say("（演示）这里会在资源管理器中打开文件夹。"),

  setDraft: (id, draft) => get().patchSession(id, (s) => ({ ...s, draft })),
  attachFiles: async (id, files) => {
    const items = await readFiles(files, get().say);
    if (items.length) get().patchSession(id, (s) => ({ ...s, attachments: [...s.attachments, ...items] }));
  },
  pickAttachments: async () => get().say("（演示）这里会打开文件选择对话框。"),
  removeAttachment: (id, aid) => get().patchSession(id, (s) => ({ ...s, attachments: s.attachments.filter((a) => a.id !== aid) })),

  send: (id) => {
    const s = get().sessions[id];
    if (!s) return;
    if (s.mark !== "ready") return get().say("（演示）运行中可以排队或引导，设计稿里没有做；真实应用里 Enter 排队、Ctrl+Enter 立即引导。");
    if (!s.draft.trim() && s.attachments.length === 0) return;
    runTurn(id);
  },

  stop: (id) => cancelRun(id, true),
  compact: async () => get().say("（演示）真实应用里这里会把更早的对话压缩成摘要。"),
  startGoal: async (_id, text) => get().say(`（演示）真实应用里模型会一轮轮朝「${text}」做下去。`),
  refreshSkills: () => set({ skills: DEMO_SKILLS }),

  choose: (field, value) => {
    const id = get().currentId;
    if (!id) return get().setDefault(field, value);
    const st = get();
    const session = st.sessions[id]!;
    if (field !== "model") {
      get().patchSession(id, (s) => ({ ...s, settings: { ...s.settings, [field]: value } }));
      return get().setDefault(field, value);
    }
    const model = findModel(st.models, value);
    if (!model) return;
    const was = findModel(st.models, session.settings.model);
    if (engineOfSource(model.source) !== engineOfSource(was?.source ?? "factory")) return switchEngine(id, model, was);
    const { settings, changed } = settingsForModel(st.models, session.settings, value);
    get().patchSession(id, (s) => ({ ...s, settings, context: { ...s.context, model: value, window: model.window } }));
    if (changed.includes("reasoning")) {
      get().say(
        model.efforts.length === 0
          ? `${model.label} 没有推理档位，推理设置已停用。`
          : `${model.label} 不支持「${effortLabel(session.settings.reasoning)}」推理，已改为「${effortLabel(settings.reasoning)}」。`,
      );
    }
    get().setDefault("model", value);
  },

  answerPermission: (sessionId, promptId, value) => settlePrompt(sessionId, promptId, value),
  answerQuestion: (sessionId, promptId, answers) => {
    const joined = (a: QuestionAnswer | undefined) => (Array.isArray(a) ? a.join("、") : (a ?? ""));
    const turn = get().sessions[sessionId]?.turns.find((t) => t.prompts.some((p) => p.id === promptId));
    const prompt = turn?.prompts.find((p) => p.id === promptId);
    if (turn && prompt?.kind === "question") {
      const pairs = prompt.questions.map((q, i): [string, string] => [q.question, joined(answers?.[i])]);
      get().patchTurn(sessionId, turn.id, (t) => ({
        ...t,
        blocks: [...t.blocks, { kind: "qa", id: `qa-${promptId}`, pairs: answers ? pairs : [], cancelled: !answers }],
      }));
    }
    settlePrompt(sessionId, promptId, answers ? answers.map(joined).join("；") : "cancel");
  },

  toggleGroup: (key) => set((s) => ({ openGroups: { ...s.openGroups, [key]: !s.openGroups[key] } })),
  toggleItem: (key) => set((s) => ({ openItems: { ...s.openItems, [key]: !s.openItems[key] } })),
  setTheme: (themeChoice) => set({ themeChoice }),
  setFontScale: (fontScale) => set({ fontScale }),
  setFollowUp: (followUp) => set({ followUp }),
  openSettings: (settingsPage, providerEdit = null) => set({ settingsPage, providerEdit }),
  setProviderEdit: (providerEdit) => set({ providerEdit }),

  saveProvider: async (draft) => {
    try {
      const saved = await bridge.providers.save(draft);
      set((s) => {
        const exists = s.providers.some((p) => p.id === saved.id);
        const providers = exists ? s.providers.map((p) => (p.id === saved.id ? saved : p)) : [...s.providers, saved];
        return { providers, models: buildModels(providers) };
      });
      get().say(`已保存「${saved.name}」：${saved.models.length} 个模型现在可以在模型菜单里选。`);
      return true;
    } catch (e) {
      get().say(`没有保存成功：${e instanceof Error ? e.message : String(e)}`);
      return false;
    }
  },

  removeProvider: (id) =>
    confirmDeleteProvider(id, async (p) => {
      await bridge.providers.remove(id);
      set((s) => {
        const providers = s.providers.filter((x) => x.id !== id);
        const defaults = s.defaults.model.startsWith(`api:${id}:`) ? { model: "claude-haiku-5-5", reasoning: "low", autonomy: "off", mode: s.defaults.mode } : s.defaults;
        return { providers, models: buildModels(providers), defaults, providerEdit: null };
      });
      get().say(`已删除「${p.name}」。`);
    }),

  testProvider: async (id) => {
    const p = get().providers.find((x) => x.id === id);
    if (!p) return;
    set((s) => ({ providers: s.providers.map((x) => (x.id === id ? { ...x, status: { ok: true, text: "正在测试…" } } : x)) }));
    const r = await bridge.providers.test(p);
    set((s) => ({ providers: s.providers.map((x) => (x.id === id ? { ...x, status: { ok: r.ok, text: r.text } } : x)) }));
  },

  codexLogin: async () => {
    set((s) => ({ codex: { ...s.codex, auth: "signing-in" } }));
    const codex = await bridge.codex.login();
    set({ codex });
    if (codex.auth === "chatgpt") get().say(`已登录 Codex：${codex.email}（${codex.plan}）。`);
  },
  codexCancel: async () => set({ codex: await bridge.codex.cancelLogin() }),
  codexLogout: async () => {
    set({ codex: await bridge.codex.logout() });
    get().say("已退出 Codex。打开着的 Codex 会话在下次发送时会提示重新登录。");
  },
  codexEnable: async (on) => set({ codex: await bridge.codex.setEnabled(on) }),
  devinLogin: async () => {
    set((s) => ({ devin: { ...s.devin, auth: "signing-in" } }));
    const devin = await bridge.devin.login();
    set({ devin });
    if (devin.auth === "signed-in") get().say(`已登录 Devin${devin.email ? `：${devin.email}` : ""}${devin.plan ? `（${devin.plan}）` : ""}。`);
  },
  devinCancel: async () => set({ devin: await bridge.devin.cancelLogin() }),
  devinLogout: async () => {
    set({ devin: await bridge.devin.logout() });
    get().say("已退出 Devin（Devin 桌面版和命令行共用这个登录，它们也会退出）。打开着的 Devin 会话在下次发送时会提示重新登录。");
  },
  devinEnable: async (on) => set({ devin: await bridge.devin.setEnabled(on) }),
  chatgptLogin: async (options) => {
    set((s) => ({ chatgpt: { ...s.chatgpt, auth: "signing-in", signingIn: true } }));
    const chatgpt = await bridge.chatgpt.login(options);
    set({ chatgpt });
    if (chatgpt.auth === "signed-in") get().say(chatgpt.planEnabled ? `已登录 ChatGPT：${chatgpt.email}。` : "已登录 ChatGPT，但没有允许使用套餐。");
  },
  chatgptCancel: async () => set({ chatgpt: await bridge.chatgpt.cancelLogin() }),
  chatgptLogout: async (clientId) => {
    set({ chatgpt: await bridge.chatgpt.logout(clientId) });
    get().say("已退出 ChatGPT。打开着的 ChatGPT Plan 会话在下次发送时会提示重新登录。");
  },
  chatgptUse: async (clientId) => set({ chatgpt: await bridge.chatgpt.use(clientId) }),
  chatgptEnable: async (on) => set({ chatgpt: await bridge.chatgpt.setEnabled(on) }),
  chatgptWelcome: async () => set({ chatgpt: await bridge.chatgpt.welcomed() }),
  recheckDroid: async () => get().say(`已重新检测：找到 droid ${get().factory.version}。`),
  recheckCodex: async () => get().say("已重新检测：仍然没有找到 codex。"),
  recheckDevin: async () => get().say("已重新检测：仍然没有找到 devin。"),

  setDefault: (field, value) =>
    set((s) => {
      if (field !== "model") return { defaults: { ...s.defaults, [field]: value } };
      return { defaults: settingsForModel(s.models, s.defaults, value).settings };
    }),
  setModelsVisible: (ids, visible) =>
    set((s) => ({ hiddenModels: visible ? s.hiddenModels.filter((id) => !ids.includes(id)) : [...s.hiddenModels, ...ids.filter((id, i) => !s.hiddenModels.includes(id) && ids.indexOf(id) === i)] })),
  setMaxOpen: (maxOpen) => set({ maxOpen: Math.max(1, Math.min(16, maxOpen)) }),
  setMemoryEnabled: (memoryEnabled) => set({ memoryEnabled }),
  setMemoryRecall: (memoryRecall) => set({ memoryRecall }),
  setMemoryCapture: (memoryCapture) => set({ memoryCapture }),
  setMemoryCurate: (memoryCurate) => set({ memoryCurate }),
  setMemoryEngines: (memoryEngines) => set({ memoryEngines }),
  setCompactAuto: (compactAuto) => set({ compactAuto }),
  setThinkChinese: (thinkChinese) => set({ thinkChinese }),
  setCompactAt: (percent) => set({ compactAt: clampCompactAt(percent) }),
  setMemoryState: (memory) => set({ memory: { latest: memory.latest, unseen: memory.unseen, seen: memory.seen, pending: memory.pending ?? 0 } }),
  setDroidPath: (droidPath) => set({ droidPath }),
  setCodexPath: (codexPath) => set({ codexPath }),
  setDevinPath: (devinPath) => set({ devinPath }),
  setSidebarWidth: (sidebarWidth) => set({ sidebarWidth }),
  setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
  setReviewOpen: (reviewOpen) => set({ reviewOpen }),
  setDragging: (dragging) => set({ dragging }),
  openDialog: (dialog) => set({ dialog }),
  openLightbox: (lightbox) => set({ lightbox }),

  say: (text) => {
    const id = Date.now();
    set({ notice: { id, text } });
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => set((s) => (s.notice?.id === id ? { notice: null } : {})), 6000);
  },

  patchSession: (id, fn) => set((st) => (st.sessions[id] ? { sessions: { ...st.sessions, [id]: fn(st.sessions[id]!) } } : {})),
  patchTurn: (sessionId, turnId, fn) =>
    get().patchSession(sessionId, (s) => ({ ...s, turns: s.turns.map((t) => (t.id === turnId ? fn(t) : t)) })),
}));

// ------------------------------------------------------------------------------------- questions both modes ask first

/** Closing the window asks first while a session is still working, a message waits to be sent or a goal is in progress. */
export function confirmQuit(close: () => void) {
  const st = useStore.getState();
  const body = quitWarning(Object.values(st.sessions));
  if (!body) return close();
  st.openDialog({ title: "退出 WittyCode？", body, confirmLabel: "退出", danger: true, onConfirm: close });
}

/** Closing a tab whose turn is running asks first. */
export function confirmClose(id: string, close: () => void) {
  const st = useStore.getState();
  const s = st.sessions[id];
  if (!s) return;
  if (s.mark !== "running") return close();
  st.openDialog({
    title: "关闭正在运行的会话？",
    body: `「${s.title}」还在运行。关闭会中断当前任务，已经完成的修改不会撤销。`,
    confirmLabel: "中断并关闭",
    danger: true,
    onConfirm: close,
  });
}

/** Removing a project from the list always asks; `remove` gets its open conversations. */
export function confirmRemoveProject(id: string, remove: (open: string[]) => void) {
  const st = useStore.getState();
  const p = st.projects.find((x) => x.id === id);
  if (!p) return;
  const open = p.sessionIds.filter((sid) => st.tabs.includes(sid));
  st.openDialog({
    title: `从列表中移除「${p.name}」？`,
    body: open.length ? `它有 ${open.length} 个打开的会话，会一并关闭。磁盘上的文件夹和已保存的会话不会被删除。` : "磁盘上的文件夹和已保存的会话不会被删除。",
    confirmLabel: "移除",
    danger: true,
    onConfirm: () => remove(open),
  });
}

/** Deleting an API provider always asks, and says what goes with it. */
export function confirmDeleteProvider(id: string, remove: (p: ApiProvider) => void | Promise<void>) {
  const st = useStore.getState();
  const p = st.providers.find((x) => x.id === id);
  if (!p) return;
  const inUse = Object.values(st.sessions).filter((s) => s.mark && s.settings.model.startsWith(`api:${id}:`)).length;
  st.openDialog({
    title: `删除「${p.name}」？`,
    body: `${p.models.length} 个模型会从模型菜单里消失，并从 ~/.factory/settings.json 的 customModels 删除。${inUse ? `有 ${inUse} 个打开的会话正在用它，它们会继续用到结束。` : ""}密钥如果是环境变量，变量本身不会动。`,
    confirmLabel: "删除",
    danger: true,
    onConfirm: () => void remove(p),
  });
}

/** The conversation moves to the other engine's model. Neither engine can read the other's context, so the other side
 *  starts a fresh native session and the next message carries the earlier conversation's text (`lib/handoff.ts`). In
 *  the real app Python does this; here the page only shows what would happen. */
function switchEngine(id: string, model: ModelChoice, was: ModelChoice | undefined) {
  const st = useStore.getState();
  const session = st.sessions[id]!;
  if (session.mark === "running" || session.mark === "attention") return st.say("本轮还在运行：结束后才能换引擎。");
  const engine = engineOfSource(model.source);
  const { settings, changed } = settingsForModel(st.models, session.settings, model.id);
  const h = handoffOf(session.turns);
  const block: SwitchBlock = {
    kind: "switch",
    id: `sw-${++switchCounter}`,
    from: { model: was?.label ?? session.settings.model, engine: engineLabel(was?.source ?? "factory") },
    to: { model: model.label, engine: engineLabel(model.source) },
    carried: h.carried,
    total: h.total,
    bytes: h.bytes,
    truncated: h.truncated,
  };
  st.patchSession(id, (s) => ({
    ...s,
    settings,
    // Tokens add up over the whole conversation, whatever engine used them; the context window is the new model's own.
    context: { used: null, model: model.id, window: model.window },
    handoff: h.carried > 0 ? { carried: h.carried, truncated: h.truncated, to: model.label } : undefined,
    turns: h.total > 0 ? [...s.turns, { id: `turn-sw-${switchCounter}`, blocks: [block], prompts: [], status: "done" }] : s.turns,
  }));
  const level = permissionsFor(engine).find((l) => l.value === settings.autonomy)?.label;
  st.say(
    `已换到 ${model.label}（${engineLabel(model.source)}）。${h.carried > 0 ? `之前 ${h.carried} 条对话的文字会随下一条消息交给它。` : ""}${changed.includes("autonomy") ? `权限回到「${level}」。` : ""}`,
  );
  st.setDefault("model", model.id);
}

export const currentSession = (s: Store): Session | null => (s.currentId ? (s.sessions[s.currentId] ?? null) : null);
