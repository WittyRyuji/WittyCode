// The messages between this page and Python's AppCore (desktop/core/app_core.py), as AppCore sends and accepts them.
//
// Calls: the page calls `backend.call(method, paramsJson, callId)`; Python answers with `backend.reply(callId, ok, json)`
// (ok=false: `json` is one Chinese sentence for the user). The method names are AppCore's `_m_*` handlers with "_" -> ".".
// Events: Python sends `backend.event(name, json)` whenever something changes. See transport.ts for the QWebChannel side.

import type {
  AgentInfo,
  ApiProvider,
  AttachmentView,
  ChatGPTAccount,
  ContextState,
  DevinAccount,
  Engine,
  EnvState,
  FileView,
  FontScale,
  MemoryChange,
  MemoryEntry,
  MemoryKind,
  MemoryScope,
  ImportApp,
  ImportMemory,
  ImportResult,
  ImportSession,
  ImportSource,
  MemoryReview,
  MemoryReviewSummary,
  MemoryState,
  ModelChoice,
  PendingHandoff,
  Project,
  ProviderDraft,
  QueuedMessage,
  RemoteModel,
  SkillItem,
  SessionMark,
  SessionSettings,
  Speed,
  ThemeChoice,
  Turn,
  Usage,
} from "./types";
import type { TestResult } from "./bridge";

/** AppCore.settings_view(). `devinPath` is what the user set ("" = find it). */
export interface SettingsView {
  maxOpen: number;
  devinPath: string;
  /** page model ids the user left out of the conversation's model menu */
  hiddenModels: string[];
  theme: ThemeChoice;
  fontScale: FontScale;
  /** what Enter does with a message typed while a turn runs; Ctrl+Enter does the other */
  followUp: FollowUp;
  sidebarWidth: number;
  /** the shared memory is given to new and resumed conversations (master switch), of these engines */
  memoryEnabled: boolean;
  /** each message goes with the notes that bear on it (the application's own hook before sending) */
  memoryRecall: boolean;
  /** a message that says "remember this" is written down by the application itself */
  memoryCapture: boolean;
  /** after a turn the engine is asked what is worth keeping; the answers wait as suggestions */
  memoryCurate: boolean;
  memoryEngines: Engine[];
  /** ChatGPT Plan: the early part of a long conversation is summarised before the next request would not fit the model's window */
  compactAuto: boolean;
  /** ... when the conversation is this percent of the window (50 to 95) */
  compactAt: number;
  /** every model is asked to write its thinking in Chinese */
  thinkChinese?: boolean;
  /** the folder of the conversation started last ("" before the first): the first message box starts there */
  lastProject?: string;
}
export type FollowUp = "queue" | "steer";
export type SettingKey = "theme" | "fontScale" | "followUp" | "maxOpen" | "sidebarWidth" | "devinPath" | "memoryEnabled" | "memoryRecall" | "memoryCapture" | "memoryCurate" | "memoryEngines" | "compactAuto" | "compactAt" | "thinkChinese";
export type SettingValue = string | number | boolean | string[];

/** What `memory.list` can be narrowed by (all optional). `query`: words to look for; `status` "all" lists forgotten notes too. */
export interface MemoryFilter {
  query?: string;
  scope?: MemoryScope;
  project?: string;
  kind?: MemoryKind;
  engine?: string;
  status?: "active" | "forgotten" | "all";
}
/** The note a change made (or took back), and the state of the memory after it. */
export interface MemoryChanged extends MemoryState {
  entry: MemoryEntry;
}
/** What the user changed of a note while reviewing it. */
export interface ReviewEdit {
  title?: string;
  text?: string;
  kind?: MemoryKind;
  scope?: MemoryScope;
  tags?: string[];
}
/** What the user edits of a note; `scope` "global" makes a project note global, "project" with `project` gives it to one. */
export interface MemoryEdit {
  id: string;
  title?: string;
  text?: string;
  kind?: MemoryKind;
  tags?: string[];
  scope?: MemoryScope;
  project?: string;
}

/** AppCore._saved_json(): a saved session listed in the sidebar, not open. Own-loop conversations have ids "cg:<id>", Devin
 *  sessions "dv:<session id>". */
export interface SavedRow {
  id: string;
  projectId: string;
  title: string;
  modified: string;
  modifiedAt: number;
  engine: Engine;
  /** `model` is "" here: a saved row does not know its model */
  settings: SessionSettings;
  usage: null;
  context: ContextState;
  workingLabel: string;
}

/** AppCore._session_meta(): an open conversation ("c1", "c2", ...). The first `session` event carries all of it, later
 *  ones the keys that changed. `context.window` 0: not known yet. */
export interface ConversationMeta {
  id: string;
  projectId: string;
  title: string;
  mark: SessionMark;
  workingLabel: string;
  modified: string;
  modifiedAt: number;
  engine: Engine;
  nativeId: string;
  settings: SessionSettings;
  usage: Usage | null;
  speed: Speed | null;
  context: ContextState;
  handoff: PendingHandoff | null;
  queued: QueuedMessage[];
  agents: AgentInfo[];
  /** the goal (`/goal`) the conversation is working toward; "" when none */
  goal?: string;
}

/** What `app.init` answers, from the application's own cache (`core/cache.py`): the saved sessions, the model list and the
 *  Devin account as they were last seen, so the window is full before anything is connected. The real ones follow as
 *  `sidebar`, `models` and `accounts` events a moment later and replace them. With no cache yet (the very first start)
 *  `sessions` is empty, `models` may be, and Devin is auth "checking". */
export interface InitResult {
  version: number;
  /** the shared memory's state; null where the application has none */
  memory: MemoryState | null;
  projects: Project[];
  sessions: Record<string, SavedRow>;
  /** the saved conversations the user archived, the one archived last first */
  archived?: SavedRow[];
  /** the open conversations, in tab order */
  tabs: string[];
  models: ModelChoice[];
  defaults: SessionSettings;
  settings: SettingsView;
  devin: DevinAccount;
  /** read from the credential files, no network: ready at once */
  chatgpt: ChatGPTAccount;
  providers: ApiProvider[];
  maximized: boolean;
  offline: boolean;
}

/** One attachment of the conversation, as `session.choose` offers it for the engine change. `as` is how it would
 *  travel: "text" (its words go with the earlier text), "image" or "pdf" (the file goes out with the next message);
 *  "" when it cannot go, and then `reason` says why. `bytes`: the words (text) or the file (image, pdf). */
export interface CarryItem {
  id: string;
  name: string;
  kind: "text" | "image" | "pdf";
  as: "text" | "image" | "pdf" | "";
  bytes: number;
  reason?: string;
}

/** What `session.choose` asks before it changes the engine of a conversation that holds attachments: every one of them,
 *  and what one message can hold (`files`: images and PDFs without words, `bytes`: those files together). */
export interface HandoffChoice {
  attachments: CarryItem[];
  limits: { files: number; bytes: number };
  /** what the message box holds already: it goes with the next message too and takes its share of `limits` */
  drafts: { count: number; bytes: number };
  /** the safety ceiling: past it a pick is refused whatever the user says */
  hard: { files: number; bytes: number };
}

/** `session.send` found more attachments than a message usually holds (`limits` of HandoffChoice) and sent nothing: the
 *  user is asked, and the page sends again with `confirmOver`. `text` is Python's sentence on by how much. */
export interface SendOver {
  count: number;
  total: number;
  files: number;
  bytes: number;
  text: string;
}

export interface FileUpload {
  name: string;
  /** the browser's MIME type, "" when it does not know */
  type: string;
  /** the bytes, base64 */
  data: string;
}

/** Python admits attachments (limits, shrinking pictures, decoding text); `rejected` is a sentence naming the ones it did
 *  not take, "" when it took them all. */
export interface AttachmentsResult {
  added: AttachmentView[];
  rejected: string;
  /** "": the box holds no more than a message usually does. Else by how much it is over (allowed; asked about at send). */
  over?: string;
}

export type ChooseField = "model" | "reasoning" | "autonomy" | "mode";
export type Edge = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
/** one per question: a string, or the chosen options of a multi-select question */
export type QuestionAnswer = string | string[];

type None = Record<string, never>;

export interface Calls {
  "app.init": { params: None; result: InitResult };
  /** no path: Python asks with the folder dialog; `added` false when the user cancelled */
  "project.add": { params: { path?: string }; result: { added: boolean; id?: string } };
  "project.remove": { params: { id: string }; result: null };
  /** no id: every project */
  "project.refresh": { params: { id?: string }; result: null };
  "project.reveal": { params: { id: string }; result: null };
  /** `fromId`: the conversation whose project to use when there is no `projectId` */
  "session.new": { params: { projectId?: string; modelId?: string; fromId?: string }; result: { id: string | null } };
  "session.openSaved": { params: { id: string; projectId: string; title?: string }; result: { id: string | null } };
  "session.reorder": { params: { projectId: string; id: string; targetId: string; after: boolean }; result: { ok: boolean } };
  "session.close": { params: { id: string }; result: null };
  /** hide the conversation from the lists (an open one is closed first; one that is running is refused). Nothing of the
   *  engine's own files is touched; `session.unarchive` brings it back. `id`: an open conversation's, or a saved row's. */
  "session.archive": { params: { id: string }; result: { ok: boolean } };
  "session.unarchive": { params: { id: string }; result: { ok: boolean } };
  /** the name shown here for the conversation (the engine's title is left alone); an empty `title` gives that title back */
  "session.rename": { params: { id: string; title: string }; result: { ok: boolean; title: string } };
  "session.stop": { params: { id: string }; result: null };
  /** `/compact`: `text` says what was done (or why nothing was) */
  "session.compact": { params: { id: string }; result: { ok: boolean; text: string } };
  /** `/goal`: starts the goal's first round; `text` says so */
  "session.goal": { params: { id: string; text: string }; result: { ok: boolean; text: string } };
  "session.reconnect": { params: { id: string }; result: null };
  /** `ok` false with `over`: more attachments than a message usually holds, nothing was sent; ask, then send again with `confirmOver` */
  /** `how`, for a message sent while a turn runs: `queue` holds it until the turn is over, `steer` gives it to the running
   *  turn now. `queued`: the message is kept and goes out by itself, in its turn (`session.unqueue` takes it back); `steered`:
   *  the running turn has it. A steer the engine cannot take is queued instead (Python says so in a notice). */
  "session.send": { params: { id: string; text: string; attachmentIds?: string[]; confirmOver?: boolean; how?: FollowUp }; result: { ok: boolean; over?: SendOver; queued?: boolean; steered?: boolean } };
  /** takes queued messages back into the composer: the one `queueId` names, or all of them */
  "session.unqueue": { params: { id: string; queueId?: string }; result: { ok: boolean } };
  /** changes the words of a waiting message (it keeps its place and attachments); ok false: it had gone out or been taken back */
  "session.editQueued": { params: { id: string; queueId: string; text: string }; result: { ok: boolean } };
  /** sends a waiting message now: to the running turn (`steered`), or as the next message once no turn runs; ok false: it was
   *  not possible (Python says why in a notice) or the message was already gone */
  "session.sendQueued": { params: { id: string; queueId: string }; result: { ok: boolean; steered?: boolean } };
  /** ok false: the prompt was no longer open (a late click), not an error */
  /** `agentId`: the approval is a sub-agent's (asked in its side panel) */
  "session.answerPermission": { params: { id: string; promptId: string; value: string; agentId?: string }; result: { ok: boolean } };
  "session.answerQuestion": { params: { id: string; promptId: string; answers: QuestionAnswer[] | null; agentId?: string }; result: { ok: boolean } };
  /** a text file the conversation named, for the side panel; `path` as the model wrote it (relative to the project, an optional
   *  `:line` or `:line-line` at the end). An error is one sentence for the user (no such file, not text, a folder). */
  "files.read": { params: { id: string; path: string }; result: FileView };
  /** what the panel draws of a PDF (page `page` from 0, drawn `width` pixels wide) or a picture (the whole picture), as a
   *  `data:` URI. An error is one sentence for the user. */
  "files.page": { params: { id: string; path: string; page: number; width: number }; result: { url: string } };
  /** open it outside the window: `default` program (a program or script is only shown in its folder: `shown`, `message`),
   *  the system's `choose` dialog, or `reveal` in its folder */
  "files.open": { params: { id: string; path: string; how: "default" | "choose" | "reveal" }; result: { ok: boolean; shown?: boolean; message?: string } };
  /** where the path is, as an absolute path (to copy it) */
  "files.resolve": { params: { id: string; path: string }; result: { path: string; isDir: boolean; line: number | null } };
  /** open a sub-agent's run: its whole run comes as an `agentTurns` event, then its changes, until `agents.unwatch` */
  "agents.watch": { params: { id: string; agentId: string }; result: { ok: boolean } };
  "agents.unwatch": { params: { id: string }; result: { ok: boolean } };
  /** no id: the defaults for new sessions. A model of the other engine moves the conversation there (`switched`).
   *  When the conversation holds any attachment, Python first answers `{ ok: false, choose }` and changes nothing: the
   *  page asks the user which to take along and calls again with `attachments`, the ids picked (empty: none), and
   *  `overLimit` when the user has confirmed a pick that makes the first message hold more than a message usually does. */
  "session.choose": { params: { id?: string; field: ChooseField; value: string; attachments?: string[]; overLimit?: boolean }; result: { ok: boolean; switched?: boolean; choose?: HandoffChoice } };
  "attachments.add": { params: { id: string; files: FileUpload[] }; result: AttachmentsResult };
  /** Python's file dialog */
  "attachments.pick": { params: { id: string }; result: AttachmentsResult };
  "attachments.remove": { params: { id: string; attachmentId: string }; result: null };
  /** the whole picture as a data: URI, for the big view; null when Python no longer has it */
  "attachments.full": { params: { id?: string; attachmentId: string }; result: { url: string | null } };
  "settings.set": { params: { key: SettingKey; value: SettingValue }; result: SettingsView };
  /** the shared memory (desktop/core/memory): notes that the own loop and Devin read and write, and the user too.
   *  Nothing is ever deleted: forgetting marks a note, every change is in a journal and `memory.undo` takes one back. */
  "memory.list": { params: MemoryFilter; result: { entries: MemoryEntry[]; counts: Record<string, number> } & MemoryState };
  /** the newest changes made by agents (newest first); `since`: only those after this change number */
  "memory.changes": { params: { since?: number }; result: { changes: MemoryChange[] } & MemoryState };
  "memory.create": { params: { title: string; text: string; scope: MemoryScope; project?: string; kind?: MemoryKind; tags?: string[] }; result: MemoryChanged };
  "memory.update": { params: MemoryEdit; result: MemoryChanged };
  "memory.forget": { params: { id: string }; result: MemoryChanged };
  "memory.restore": { params: { id: string }; result: MemoryChanged };
  "memory.undo": { params: { seq: number }; result: MemoryChanged };
  /** the user has looked at the changes up to `seq` (none: all of them) */
  "memory.markSeen": { params: { seq?: number }; result: MemoryState };
  /** what other applications have to bring (Claude Code, Codex, Devin, Factory): found or not, and how much */
  "import.scan": { params: None; result: { sources: ImportSource[] } };
  /** the notes their memory files would make (split by heading), to be ticked in a preview; nothing is written */
  "import.memories": { params: { apps: ImportApp[] }; result: { items: ImportMemory[] } };
  /** their conversations, newest first */
  "import.sessions": { params: { apps: ImportApp[] }; result: { items: ImportSession[] } };
  /** make notes and conversations of the ticked ones (by key: the words come from the files again, not from the page) */
  "import.commit": { params: { memories: string[]; sessions: string[] }; result: ImportResult & MemoryState };
  /** take an import back: the notes it made are forgotten (they can be restored) */
  "import.undo": { params: { ids: string[] }; result: MemoryState };
  /** the notes the application wrote by itself since the last review (they are in the memory already), the least sure first */
  "memory.review": { params: None; result: MemoryReview };
  /** the user's word on a review: `keep` and `drop` are note ids (dropping forgets a note, which can be restored); `edits`
   *  change the words of kept ones. What is learned from it filters what the application writes next. */
  "memory.reviewApply": { params: { keep: string[]; drop: string[]; edits?: Record<string, ReviewEdit> }; result: MemoryReviewSummary & MemoryState };
  /** show (`visible`) or hide models in the conversation's model menu; the answer is the settings with the new list.
   *  The default model follows as a `defaults` event when hiding it changes what new sessions start with. */
  "models.setVisible": { params: { ids: string[]; visible: boolean }; result: SettingsView };
  "devin.account": { params: None; result: DevinAccount };
  /** runs `devin auth login` (it opens the browser); answers once the user has signed in or cancelled */
  "devin.login": { params: None; result: DevinAccount };
  "devin.cancelLogin": { params: None; result: DevinAccount };
  "devin.logout": { params: None; result: DevinAccount };
  "devin.setEnabled": { params: { on: boolean }; result: DevinAccount };
  "chatgpt.account": { params: None; result: ChatGPTAccount };
  /** "Continue with ChatGPT": opens the browser (OpenAI's sign-in, PKCE, a loopback callback) and answers once the user has
   *  finished there, cancelled or the five minutes ran out. `add`: another account instead of signing the current one in
   *  again; `forceConsent`: ask once more for the permission to use the plan (a sign-in that was given without it). */
  "chatgpt.login": { params: { add?: boolean; forceConsent?: boolean }; result: ChatGPTAccount };
  "chatgpt.cancelLogin": { params: None; result: ChatGPTAccount };
  /** the tokens are revoked at OpenAI (best effort) and cleared here; no `clientId`: the active account */
  "chatgpt.logout": { params: { clientId?: string }; result: ChatGPTAccount };
  /** make a saved account the one the conversations are paid by */
  "chatgpt.use": { params: { clientId: string }; result: ChatGPTAccount };
  "chatgpt.setEnabled": { params: { on: boolean }; result: ChatGPTAccount };
  /** the one-time "You're using your ChatGPT plan" note was seen */
  "chatgpt.welcomed": { params: None; result: ChatGPTAccount };
  "providers.save": { params: { draft: ProviderDraft }; result: ApiProvider };
  "providers.remove": { params: { id: string }; result: null };
  "providers.test": { params: { draft: ProviderDraft }; result: TestResult };
  "providers.listModels": { params: { draft: ProviderDraft }; result: RemoteModel[] };
  /** the skills and custom commands for the project of a conversation (`id`) or a project; read from the files every time */
  "skills.list": { params: { id?: string; projectId?: string }; result: { items: SkillItem[] } };
  "providers.envStatus": { params: { names: string[] }; result: Record<string, EnvState> };
  "window.minimize": { params: None; result: null };
  /** the new state */
  "window.toggleMaximize": { params: None; result: boolean };
  "window.close": { params: None; result: null };
  "window.startMove": { params: None; result: null };
  "window.startResize": { params: { edge: Edge }; result: null };
  "window.isMaximized": { params: None; result: boolean };
  /** http, https and mailto only; anything else is ignored */
  "shell.openExternal": { params: { url: string }; result: null };
}
export type Method = keyof Calls;

export interface Events {
  /** a status bar message */
  notice: { text: string };
  /** the open conversations in tab order; `select`: show this one (a conversation the page just opened) */
  tabs: { tabs: string[]; select: string | null };
  /** every project with its rows (saved sessions and open conversations, newest first) and all saved rows */
  sidebar: { projects: Project[]; sessions: Record<string, SavedRow>; archived?: SavedRow[] };
  models: { models: ModelChoice[]; defaults: SessionSettings };
  defaults: { defaults: SessionSettings };
  /** merge `patch` into the conversation `id` */
  session: { id: string; patch: Partial<ConversationMeta> };
  /** the run of the sub-agent that is open in the side panel: all of it (`all`), or the turns that changed */
  agentTurns: { id: string; agentId: string; turns: Turn[]; all: boolean };
  /** `all`: these are all of its turns; otherwise the ones that changed (new ones come last) */
  turns: { id: string; turns: Turn[]; all: boolean };
  /** a message with attachments whose turn failed goes back into the composer */
  restore: { id: string; text: string; attachments: AttachmentView[] };
  settings: { settings: SettingsView };
  /** the shared memory was written to (by an agent, or elsewhere) or looked at */
  memory: MemoryState;
  accounts: { devin?: DevinAccount; chatgpt?: ChatGPTAccount };
  /** the window was maximized or restored (also from outside: double click, Win+Up) */
  window: { maximized: boolean };
  /** the user closed the window from outside (Alt+F4, the taskbar): ask if needed, then call window.close */
  closeRequested: Record<string, never>;
}
export type EventName = keyof Events;
