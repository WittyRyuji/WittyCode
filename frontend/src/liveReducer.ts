// What Python's events do to the page's state, as pure functions: (state, event) -> the changed part of the state, plus
// the side effects to run (a status bar message, the quit question). No React, no DOM, no store: `node scripts/unit.mjs`
// runs them as they are (Node strips the types), and live.ts applies them to the zustand store.
//
// Ids: a project is its folder path; a sidebar row is a saved session (own-loop conversations "cg:...", Devin sessions
// "dv:...") or an open conversation ("c1", "c2", ...). Saved rows have no `mark`; open conversations always have one.

import type {
  ApiProvider,
  ChatGPTAccount,
  DevinAccount,
  Engine,
  FilePanelState,
  FontScale,
  MemoryState,
  ModelChoice,
  Project,
  Session,
  SessionSettings,
  ThemeChoice,
  Turn,
} from "./types";
import type { Events, FollowUp, InitResult, SavedRow, SettingsView } from "./protocol";

/** The part of the store that Python owns in the live app. */
export interface LiveData {
  projects: Project[];
  sessions: Record<string, Session>;
  /** saved conversations the user archived (hidden from the project lists), the one archived last first */
  archived: SavedRow[];
  tabs: string[];
  currentId: string | null;
  /** a conversation to show as soon as the page knows it: Python announces the tab before the conversation's data */
  pendingSelect: string | null;
  models: ModelChoice[];
  defaults: SessionSettings;
  devin: DevinAccount;
  chatgpt: ChatGPTAccount;
  providers: ApiProvider[];
  maxOpen: number;
  maxSteps: number;
  devinPath: string;
  /** model ids left out of the conversation's model menu (a model in use is shown anyway) */
  hiddenModels: string[];
  themeChoice: ThemeChoice;
  fontScale: FontScale;
  /** what Enter does with a message typed while a turn runs (Ctrl+Enter does the other) */
  followUp: FollowUp;
  sidebarWidth: number;
  /** the shared memory is given to new and resumed conversations of these engines (master switch, engines) */
  memoryEnabled: boolean;
  /** each message goes with the notes that bear on it */
  memoryRecall: boolean;
  memoryCapture: boolean;
  memoryCurate: boolean;
  memoryEngines: Engine[];
  /** ChatGPT Plan: summarise the early part of a long conversation at this percent of the model's window (off: only after the server refuses) */
  compactAuto: boolean;
  compactAt: number;
  /** every model is asked to write its thinking in Chinese */
  thinkChinese: boolean;
  /** the folder of the conversation started last: where the first message box starts when nothing else was chosen */
  lastProject: string;
  /** the newest change of the shared memory, and how many by agents the user has not looked at (the badge) */
  memory: MemoryState;
  maximized: boolean;
  offline: boolean;
  /** the sub-agent open in the side panel (of a conversation, by its thread id) and its run */
  agentPanel: { sessionId: string; agentId: string } | null;
  agentTurns: Turn[];
  /** the text file open in the side panel (it takes the place of the sub-agent's) */
  filePanel: FilePanelState | null;
  /** the menu of a file path in the conversation (right click): which path, and where */
  pathMenu: { ref: string; x: number; y: number } | null;
}

export type Effect = { kind: "notice"; text: string } | { kind: "quit" };

export interface Applied {
  patch: Partial<LiveData>;
  effects: Effect[];
}

const NO_SETTINGS: SessionSettings = { model: "", reasoning: "", autonomy: "", mode: "auto" };
export const ALL_ENGINES: Engine[] = ["devin", "chatgpt"];
/** The percent of a model's window at which a ChatGPT Plan conversation is summarised: what the bridge accepts, and the default. */
export const COMPACT_AT_MIN = 50;
export const COMPACT_AT_MAX = 95;
export const COMPACT_AT_DEFAULT = 85;
export const clampCompactAt = (percent: number): number => Math.max(COMPACT_AT_MIN, Math.min(COMPACT_AT_MAX, Math.round(percent)));
export const NO_MEMORY: MemoryState = { latest: 0, unseen: 0, seen: 0, pending: 0 };

/** Before the backend has said anything: no account, and (not yet) no engine to offer. */
export const NO_CHATGPT: ChatGPTAccount = { auth: "signed-out", email: "", planEnabled: false, accounts: [], enabled: false, welcomed: true, available: false };

/** The backend says `signingIn` beside `auth` (a sign-in can also renew an account that is signed in): for a signed-out
 *  account that is the page's "signing-in". */
export function chatgptView(account: ChatGPTAccount | undefined): ChatGPTAccount {
  if (!account) return { ...NO_CHATGPT };
  return account.signingIn && account.auth === "signed-out" ? { ...account, auth: "signing-in" } : account;
}

/** The live app before `app.init` has answered: nothing at all, so that no demo data can ever show. */
export function emptyLiveData(theme: ThemeChoice = "system", fontScale: FontScale = "md"): LiveData {
  return {
    projects: [],
    sessions: {},
    archived: [],
    tabs: [],
    currentId: null,
    pendingSelect: null,
    models: [],
    defaults: { ...NO_SETTINGS },
    devin: { cli: "found", auth: "checking", enabled: false },
    chatgpt: { ...NO_CHATGPT },
    providers: [],
    maxOpen: 4,
    maxSteps: 80,
    devinPath: "",
    hiddenModels: [],
    themeChoice: theme,
    fontScale,
    followUp: "queue",
    sidebarWidth: 288,
    memoryEnabled: true,
    memoryRecall: true,
    memoryCapture: true,
    memoryCurate: true,
    memoryEngines: [...ALL_ENGINES],
    compactAuto: true,
    compactAt: COMPACT_AT_DEFAULT,
    thinkChinese: true,
    lastProject: "",
    memory: { ...NO_MEMORY },
    maximized: false,
    offline: false,
    agentPanel: null,
    agentTurns: [],
    filePanel: null,
    pathMenu: null,
  };
}

const isOpen = (s: Session) => s.mark !== undefined;

/** An open conversation the page has not heard about yet; its `session` events fill it in. */
export function newConversation(id: string): Session {
  return {
    id,
    projectId: "",
    title: "",
    mark: "opening",
    modified: "",
    turns: [],
    usage: null,
    context: { used: null, window: 0, model: "", compacted: false },
    settings: { ...NO_SETTINGS },
    workingLabel: "",
    draft: "",
    attachments: [],
  };
}

export function savedSession(row: SavedRow): Session {
  return {
    id: row.id,
    projectId: row.projectId,
    title: row.title,
    modified: row.modified,
    modifiedAt: row.modifiedAt,
    engine: row.engine,
    turns: [],
    usage: null,
    context: row.context,
    settings: row.settings,
    workingLabel: row.workingLabel ?? "",
    draft: "",
    attachments: [],
  };
}

/** Python's project rows, with the page's own collapsed / expanded state kept. */
export function mergeProjects(previous: Project[], next: Project[]): Project[] {
  const collapsed = new Map(previous.map((p) => [p.id, p.collapsed]));
  return next.map((p) => ({ ...p, collapsed: collapsed.get(p.id) ?? !!p.collapsed }));
}

/** All saved rows are replaced; the open conversations stay as they are. */
export function withSavedRows(sessions: Record<string, Session>, rows: Record<string, SavedRow>): Record<string, Session> {
  const out: Record<string, Session> = {};
  for (const [id, s] of Object.entries(sessions)) if (isOpen(s)) out[id] = s;
  for (const [id, row] of Object.entries(rows)) if (!out[id]) out[id] = savedSession(row);
  return out;
}

export function settingsFields(v: SettingsView): Partial<LiveData> {
  return {
    maxOpen: v.maxOpen,
    maxSteps: v.maxSteps ?? 80,
    devinPath: v.devinPath ?? "",
    hiddenModels: v.hiddenModels ?? [],
    themeChoice: v.theme,
    fontScale: v.fontScale,
    followUp: v.followUp ?? "queue",
    sidebarWidth: v.sidebarWidth,
    memoryEnabled: v.memoryEnabled ?? true,
    memoryRecall: v.memoryRecall ?? true,
    memoryCapture: v.memoryCapture ?? true,
    memoryCurate: v.memoryCurate ?? true,
    memoryEngines: v.memoryEngines ?? [...ALL_ENGINES],
    compactAuto: v.compactAuto ?? true,
    compactAt: v.compactAt ?? COMPACT_AT_DEFAULT,
    thinkChinese: v.thinkChinese ?? true,
    lastProject: v.lastProject ?? "",
  };
}

/** Show this conversation now if the page has its data, otherwise as soon as its first `session` event arrives. */
export function selectWhenKnown(s: Pick<LiveData, "sessions">, id: string): Partial<LiveData> {
  return s.sessions[id]?.projectId ? { currentId: id, pendingSelect: null } : { pendingSelect: id };
}

/** Changed turns replace the ones with the same id; new ones are appended in the order given. */
export function upsertTurns(turns: Turn[], incoming: Turn[]): Turn[] {
  if (incoming.length === 0) return turns;
  const at = new Map(turns.map((t, i) => [t.id, i]));
  const out = [...turns];
  for (const turn of incoming) {
    const i = at.get(turn.id);
    if (i === undefined) {
      at.set(turn.id, out.length);
      out.push(turn);
    } else out[i] = turn;
  }
  return out;
}

/** Conversations whose tab is gone are dropped; the current one, if it went, gives way to its neighbour. */
function withTabs(s: LiveData, tabs: string[], sessions: Record<string, Session>, select: string | null): Partial<LiveData> {
  const kept = { ...sessions };
  for (const [id, x] of Object.entries(kept)) if (isOpen(x) && !tabs.includes(id)) delete kept[id];
  let currentId = s.currentId;
  let pendingSelect = s.pendingSelect;
  if (select) {
    const chosen = selectWhenKnown({ sessions: kept }, select);
    if (chosen.currentId !== undefined) currentId = chosen.currentId;
    pendingSelect = chosen.pendingSelect ?? null;
  }
  if (currentId !== null && !tabs.includes(currentId)) {
    const at = s.tabs.indexOf(currentId);
    currentId = tabs.length ? tabs[at < 0 ? 0 : Math.min(at, tabs.length - 1)]! : null;
  }
  if (pendingSelect !== null && !tabs.includes(pendingSelect)) pendingSelect = null;
  return { tabs, sessions: kept, currentId, pendingSelect };
}

/** `app.init`: the snapshot replaces whatever the page had. */
export function applyInit(s: LiveData, init: InitResult): Partial<LiveData> {
  return {
    ...withTabs(s, [...init.tabs], withSavedRows(s.sessions, init.sessions ?? {}), null),
    projects: mergeProjects(s.projects, init.projects),
    archived: init.archived ?? [],
    models: init.models,
    defaults: init.defaults,
    devin: init.devin ?? { cli: "missing", auth: "signed-out", enabled: false },
    chatgpt: chatgptView(init.chatgpt),
    providers: init.providers,
    ...settingsFields(init.settings),
    memory: init.memory ?? { ...NO_MEMORY },
    maximized: !!init.maximized,
    offline: !!init.offline,
  };
}

function onSession(s: LiveData, e: Events["session"]): Partial<LiveData> {
  if (!s.tabs.includes(e.id)) return {}; // a conversation that is not (or no longer) open
  const before = s.sessions[e.id] ?? newConversation(e.id);
  const { handoff, queued, ...rest } = e.patch;
  const next: Session = { ...before, ...rest, id: e.id };
  if ("handoff" in e.patch) next.handoff = handoff ?? undefined;
  if ("queued" in e.patch) next.queued = queued && queued.length > 0 ? queued : undefined;
  const patch: Partial<LiveData> = { sessions: { ...s.sessions, [e.id]: next } };
  if (s.pendingSelect === e.id) {
    patch.currentId = e.id;
    patch.pendingSelect = null;
  }
  // The sub-agent on show is gone (its thread was closed): the panel closes with it.
  if (s.agentPanel?.sessionId === e.id && e.patch.agents && !e.patch.agents.some((a) => a.id === s.agentPanel?.agentId)) {
    patch.agentPanel = null;
    patch.agentTurns = [];
  }
  return patch;
}

function onAgentTurns(s: LiveData, e: Events["agentTurns"]): Partial<LiveData> {
  const panel = s.agentPanel;
  if (!panel || panel.sessionId !== e.id || panel.agentId !== e.agentId) return {};
  return { agentTurns: e.all ? [...e.turns] : upsertTurns(s.agentTurns, e.turns) };
}

function onTurns(s: LiveData, e: Events["turns"]): Partial<LiveData> {
  if (!s.tabs.includes(e.id)) return {};
  const before = s.sessions[e.id] ?? newConversation(e.id);
  const turns = e.all ? [...e.turns] : upsertTurns(before.turns, e.turns);
  return { sessions: { ...s.sessions, [e.id]: { ...before, turns } } };
}

/** A message that did not go out comes back (a failed send, a withdrawn queue entry, a steer nobody took in): its text
 *  goes in front of what has been typed since, its attachments as Python lists them (they come first, the ones added
 *  since after them). */
function onRestore(s: LiveData, e: Events["restore"]): Partial<LiveData> {
  const before = s.sessions[e.id];
  if (!before || !isOpen(before)) return {};
  const draft = !before.draft.trim() ? e.text : !e.text.trim() || before.draft.includes(e.text) ? before.draft : `${e.text}\n\n${before.draft}`;
  return { sessions: { ...s.sessions, [e.id]: { ...before, draft, attachments: [...e.attachments] } } };
}

export function applyEvent(s: LiveData, name: string, payload: unknown): Applied {
  const done = (patch: Partial<LiveData>, effects: Effect[] = []): Applied => ({ patch, effects });
  switch (name) {
    case "notice":
      return done({}, [{ kind: "notice", text: String((payload as Events["notice"]).text ?? "") }]);
    case "tabs": {
      const e = payload as Events["tabs"];
      const gone = s.agentPanel !== null && !e.tabs.includes(s.agentPanel.sessionId);
      const fileGone = s.filePanel !== null && !e.tabs.includes(s.filePanel.sessionId);
      return done({
        ...withTabs(s, [...e.tabs], s.sessions, e.select),
        ...(gone ? { agentPanel: null, agentTurns: [] } : {}),
        ...(fileGone ? { filePanel: null } : {}),
      });
    }
    case "sidebar": {
      const e = payload as Events["sidebar"];
      return done({ projects: mergeProjects(s.projects, e.projects), sessions: withSavedRows(s.sessions, e.sessions), archived: e.archived ?? s.archived });
    }
    case "models": {
      const e = payload as Events["models"];
      return done({ models: e.models, defaults: e.defaults });
    }
    case "defaults":
      return done({ defaults: (payload as Events["defaults"]).defaults });
    case "session":
      return done(onSession(s, payload as Events["session"]));
    case "turns":
      return done(onTurns(s, payload as Events["turns"]));
    case "agentTurns":
      return done(onAgentTurns(s, payload as Events["agentTurns"]));
    case "restore":
      return done(onRestore(s, payload as Events["restore"]));
    case "settings":
      return done(settingsFields((payload as Events["settings"]).settings));
    case "accounts": {
      const e = payload as Events["accounts"];
      return done({
        ...(e.devin ? { devin: e.devin } : {}),
        ...(e.chatgpt ? { chatgpt: chatgptView(e.chatgpt) } : {}),
      });
    }
    case "memory": {
      const e = payload as Events["memory"];
      return done({ memory: { latest: e.latest, unseen: e.unseen, seen: e.seen, pending: e.pending ?? 0 } });
    }
    case "window":
      return done({ maximized: !!(payload as Events["window"]).maximized });
    case "closeRequested":
      return done({}, [{ kind: "quit" }]);
    default:
      return done({});
  }
}
