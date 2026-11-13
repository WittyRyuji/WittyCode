// The view model the page renders. It mirrors desktop/core/transcript.py and prompts.py (Turn, blocks, activity
// groups, tool items, prompts), flattened to plain JSON so Python can send it as it is.

export type ThemeChoice = "system" | "light" | "dark";
export type FontScale = "sm" | "md" | "lg";

/** A reasoning level; a model offers some of them. */
export type Effort = "none" | "dynamic" | "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra";

/** The dot of a session: open and idle, working, waiting for the user, disconnected, connecting. */
export type SessionMark = "ready" | "running" | "attention" | "dead" | "opening";

export type ToolStatus = "pending" | "running" | "awaiting_permission" | "done" | "error" | "cancelled";
export type Category = "read" | "edit" | "command" | "plan" | "ask" | "memory" | "other";
export type TurnStatus = "running" | "done" | "interrupted" | "failed";
export type PromptStatus = "pending" | "sending" | "resolved";

export interface DiffLine {
  /** add / del / ctx (unchanged) / gap (a fold between hunks) */
  t: "add" | "del" | "ctx" | "gap";
  old?: number;
  new?: number;
  text: string;
}

export interface FileDiff {
  path: string;
  kind: "new" | "edit" | "delete";
  added: number;
  removed: number;
  lines: DiffLine[];
}

export interface TodoEntry {
  text: string;
  state: "pending" | "in_progress" | "completed";
}

export interface ToolItem {
  kind: "tool";
  id: string;
  name: string; // Read, Edit, Execute, ...
  category: Category;
  description: string; // one line: a path, a command, a pattern
  status: ToolStatus;
  meta?: Record<string, string>; // the input, shown as key / value rows
  command?: string;
  output?: string;
  outputError?: boolean;
  diffs?: FileDiff[];
  todos?: TodoEntry[];
  decision?: string; // what the user answered to the approval
  seconds?: number;
}

export interface ThinkingEntry {
  kind: "thinking";
  id: string;
  text: string;
  seconds?: number;
  complete: boolean;
}

export type ActivityEntry = ToolItem | ThinkingEntry;

export interface AttachmentView {
  id: string;
  kind: "image" | "text" | "pdf";
  name: string;
  size: number;
  /** data: URI of the thumbnail, only for pictures */
  url?: string;
  width?: number;
  height?: number;
  /** what Python did to it on the way in: "已压缩", "已转为 PNG", "按 GBK 读取" */
  note?: string;
}

export interface UserBlock {
  kind: "user";
  id: string;
  text: string;
  attachments?: AttachmentView[];
  /** this message was sent with the earlier conversation's text in front of it, after a change of engine */
  handoff?: { carried: number; truncated: boolean };
  /** who wrote it when that is not the user (a sub-agent's task comes from the agent that started it) */
  who?: string;
  /** said to the turn while it was running, to steer it (not the message that began the turn) */
  steer?: boolean;
  /** a steer the engine has not taken in yet (the own loop reads it before its next request) */
  pending?: boolean;
}

export interface TextBlock {
  kind: "text";
  id: string;
  markdown: string;
  streaming?: boolean;
}

export interface ActivityBlock {
  kind: "activity";
  id: string;
  entries: ActivityEntry[];
}

export interface QABlock {
  kind: "qa";
  id: string;
  pairs: [string, string][];
  cancelled?: boolean;
}

export interface ErrorBlock {
  kind: "error";
  id: string;
  message: string;
  recoverable?: boolean;
  resolved?: boolean;
}

export interface NoticeBlock {
  kind: "notice";
  id: string;
  text: string;
}

/** The line where a conversation changed engine (the own loop ⇄ Devin): what carries over and what does not. */
export interface SwitchBlock {
  kind: "switch";
  id: string;
  from: { model: string; engine: string };
  to: { model: string; engine: string };
  /** messages (the user's and the assistant's text) that go along, out of `total` in the conversation */
  carried: number;
  total: number;
  bytes: number;
  truncated: boolean;
}

export type Block = UserBlock | TextBlock | ActivityBlock | QABlock | ErrorBlock | NoticeBlock | SwitchBlock;

export interface ActionView {
  title: string;
  toolName: string;
  path?: string;
  command?: string;
  impact?: "low" | "medium" | "high";
  reason?: string;
  diffs?: FileDiff[];
  markdown?: string;
}

export interface OptionView {
  value: string;
  label: string;
  tone: "primary" | "danger" | "normal";
}

export interface PermissionPrompt {
  kind: "permission";
  id: string;
  actions: ActionView[];
  options: OptionView[];
  status: PromptStatus;
  selected?: string;
}

export interface QuestionView {
  topic: string;
  question: string;
  options: string[];
  multiSelect: boolean;
}

export interface QuestionPrompt {
  kind: "question";
  id: string;
  questions: QuestionView[];
  status: PromptStatus;
  /** The actual asker at the time of the request; older cached cards may not have it. */
  who?: string;
}

export type Prompt = PermissionPrompt | QuestionPrompt;

export interface Turn {
  id: string;
  blocks: Block[];
  prompts: Prompt[];
  status: TurnStatus;
  seconds?: number;
  steps?: number;
  /** Latest accepted snapshot in event order; absent on older cached history, empty steps explicitly clear it. */
  plan?: { id: string; steps: TodoEntry[] };
}

/** Token use of the whole conversation: summed over every time it was disconnected, closed and reopened, and over the
 *  engines it ran on. `input` is the part that was not cached.
 *  `cacheWrite` is null where the provider has no such thing (OpenAI caches automatically and reports reads only). */
export interface Usage {
  input: number;
  output: number;
  cacheWrite: number | null;
  cacheRead: number;
}

export interface ContextState {
  used: number | null; // null: not known yet (new session, or just compacted)
  window: number;
  model: string;
  compacted?: boolean;
}

export interface SessionSettings {
  model: string;
  reasoning: string;
  autonomy: string;
  mode: string;
}

/** After a change of engine, until the next message goes out: it will carry the earlier conversation's text. */
export interface PendingHandoff {
  carried: number;
  truncated: boolean;
  /** attachments picked at the change that go out with that message (text files' words, pictures, PDF files); 0 or missing: none */
  attachments?: number;
  /** the model that receives it */
  to: string;
}

/** A sub-agent an engine started for the conversation: its own thread, shown in the side panel (no engine of this version starts one). */
export interface AgentInfo {
  /** its thread id */
  id: string;
  name: string;
  role: string;
  model: string;
  effort: string;
  /** what it was asked (the first line) */
  task: string;
  /** `waiting`: it asks the user something (an approval), in its panel */
  status: "working" | "waiting" | "done" | "stopped" | "failed";
  startedAt: number;
  finishedAt: number | null;
}

/** How fast the model wrote in the latest turn that can tell (`core/speed.py`). */
export interface Speed {
  /** output tokens per second of the model's time (the turn without its tools and questions) */
  tps: number;
  tokens: number;
  seconds: number;
  /** seconds from the message to the first output, when known */
  first: number | null;
  /** tokens per second without that wait, when known */
  genTps: number | null;
}

export interface Session {
  id: string;
  projectId: string;
  title: string;
  handoff?: PendingHandoff;
  /** the sub-agents of an open conversation, oldest first */
  agents?: AgentInfo[];
  /** undefined: saved on disk, not open. Otherwise the open session's state. */
  mark?: SessionMark;
  modified: string; // "刚刚", "1 分钟前", ...
  /** when it was last used (ms since 1970): the live app formats `modified` from this itself, the mock has none */
  modifiedAt?: number;
  /** which program runs it (the live app says so; the mock derives it from the model) */
  engine?: Engine;
  /** the engine's own session id of an open conversation */
  nativeId?: string;
  turns: Turn[];
  usage: Usage | null;
  /** Token totals attributed to configured model ids; the remainder of usage is unknown legacy usage. */
  usageModels?: Record<string, number>;
  speed?: Speed | null;
  context: ContextState;
  settings: SessionSettings;
  workingLabel: string; // "就绪", "正在思考…", ...
  draft: string;
  attachments: AttachmentView[];
  /** messages typed while the session was connecting or a turn was running: shown, and sent by Python one turn at a time,
   *  in order, each when the one before it is over */
  queued?: QueuedMessage[];
  /** the goal (`/goal`) the conversation is working toward ("" or absent: none) */
  goal?: string;
}

/** A file shown in the side panel (what `files.read` answers). `line` / `lineEnd`: the lines the reference named.
 *  `kind`: a text file comes with its `text`; a PDF with the size of each page in points (the pages are drawn by Python and
 *  asked for one at a time, `files.page`); a picture with nothing more (`files.page` sends it). */
export interface FileView {
  kind: "text" | "pdf" | "image";
  pages: { w: number; h: number }[];
  path: string;
  name: string;
  dir: string;
  text: string;
  size: number;
  lines: number;
  encoding: string;
  /** only the first part of the file is in `text` */
  truncated: boolean;
  markdown: boolean;
  line: number | null;
  lineEnd: number | null;
}

/** The file panel at the right: what was asked for (`ref`, as the model wrote it) and what came of it. */
export interface FilePanelState {
  sessionId: string;
  ref: string;
  status: "loading" | "ready" | "error";
  file?: FileView;
  error?: string;
}

export interface QueuedMessage {
  id: string;
  text: string;
  attachments: AttachmentView[];
}

export interface Project {
  id: string;
  name: string;
  path: string;
  collapsed: boolean;
  sessionIds: string[];
}

/** Which program runs a session. "chatgpt" is WittyCode's own agent loop: it calls OpenAI with the user's ChatGPT plan
 *  ("Sign in with ChatGPT") and the user's API providers directly, so its sessions are WittyCode's own files. Devin
 *  (Cognition's `devin acp`, optional) runs the models of a Devin account. Each keeps its own native sessions, and neither
 *  can read the other's context. A conversation can still change engine: WittyCode starts a fresh native session on the
 *  other side and puts the earlier conversation's text (not its tool calls) in front of the next message, see
 *  `lib/handoff.ts`. */
export type Engine = "devin" | "chatgpt" | "factory";

/** Where a model comes from: "chatgpt" (the ChatGPT plan through WittyCode's own loop), "devin" (a Devin account) or
 *  "api:<providerId>" (one of the user's API providers, also run by the own loop). */
export type SourceId = string;

export interface ModelChoice {
  id: string;
  label: string;
  note?: string;
  source: SourceId;
  /** the reasoning levels this model supports, weakest first; empty: none. Engines name their own levels, so this is any
   *  string. */
  efforts: string[];
  defaultEffort: string;
  /** the context window, in tokens; 0: not known yet */
  window: number;
  /** who made the model ("Anthropic", "OpenAI", ...): the model menu groups by it */
  group?: string;
  /** the model cannot see pictures */
  noImages?: boolean;
  /** the engine lists it but it cannot be used now (not part of the plan, ...): not offered in the menu */
  disabled?: boolean;
  disabledReason?: string;
}

// ---------------------------------------------------------------------------------------------- accounts and providers

export interface LimitWindow {
  label: string; // "5 小时", "每周"
  short: string; // "5h", "1w": the status bar
  usedPercent: number;
  resetsIn: string; // "2 小时 10 分后"
}

/** The Devin account (`devin auth status` of the Devin CLI, which the desktop app's sign-in serves too). */
export interface DevinAccount {
  cli: "missing" | "found";
  cliPath?: string;
  version?: string;
  /** checking (still asking at start-up); signed-out; signing-in (the browser is open); signed-in */
  auth: "checking" | "signed-out" | "signing-in" | "signed-in";
  email?: string;
  /** "Devin Pro" */
  plan?: string;
  /** show the Devin models in the model menu */
  enabled: boolean;
  /** why the account could not be read, when it could not */
  error?: string;
}

/** One ChatGPT account the user signed in with ("Sign in with ChatGPT"). */
export interface ChatGPTSavedAccount {
  clientId: string;
  /** the email, plus a short tail of the client id when two registrations share an email */
  label: string;
  signedIn: boolean;
  /** the sign-in allows the app to use the plan */
  planEnabled: boolean;
  active: boolean;
}

/** The ChatGPT plan as an engine (WittyCode's own harness, `desktop/core/chatgpt`). The tokens stay in WittyCode's
 *  folder; the page only learns who is signed in and whether the plan may be used. */
export interface ChatGPTAccount {
  /** signed-out; signing-in (the browser is open: also what the page shows right after the click); signed-in */
  auth: "signed-out" | "signing-in" | "signed-in";
  email: string;
  /** signed in, and the user allowed the app to use the plan (without it no request can be made: sign in again) */
  planEnabled: boolean;
  clientId?: string;
  accounts: ChatGPTSavedAccount[];
  /** show the ChatGPT Plan models in the model menu */
  enabled: boolean;
  /** the one-time "You're using your ChatGPT plan" note was seen */
  welcomed: boolean;
  /** false: this build has no ChatGPT Plan engine */
  available: boolean;
  /** the backend is waiting for the browser */
  signingIn?: boolean;
}

/** Who wrote a memory or changed it: the user, or an agent (its engine, and the conversation when WittyCode knows which
 *  one it was: `rowId` is the sidebar row it can be opened from). */
export type MemoryWho = { kind: "user" } | { kind: "app" } | { kind: "unknown" } | { kind: "agent"; engine: string; conversation?: { title: string; rowId: string; projectId: string } };

export type MemoryKind = "preference" | "convention" | "pitfall" | "fact";
export type MemoryScope = "global" | "project";

/** One note of the shared memory (desktop/core/memory). Times are milliseconds. */
export interface MemoryEntry {
  id: string;
  title: string;
  text: string;
  scope: MemoryScope;
  /** the project folder, "" for a global note */
  project: string;
  projectName: string;
  kind: MemoryKind;
  tags: string[];
  created: number;
  updated: number;
  status: "active" | "forgotten";
  rev: number;
  source: MemoryWho;
  editor: MemoryWho;
}

/** A change an agent made to the memory, newest first in `memory.changes`; `seq` is what `memory.undo` takes. */
export interface MemoryChange {
  seq: number;
  op: "create" | "update" | "forget" | "restore";
  id: string;
  title: string;
  scope: MemoryScope | "";
  projectName: string;
  by: MemoryWho;
  at: number;
}

/** `latest`: the number of the newest change; `unseen`: changes by agents after `seen`, the last one the user looked at. */
export interface MemoryState {
  latest: number;
  unseen: number;
  seen: number;
  /** notes the application wrote by itself that wait for the weekly review; 0 until a review is due (desktop/core/memory/review) */
  pending: number;
}

/** A note the application wrote by itself after a turn (it is already in the memory and in use), waiting for the weekly
 *  review: the user keeps it, edits it or drops it (`memory.reviewApply`). */
export interface MemoryReviewItem {
  /** the note's id */
  id: string;
  title: string;
  text: string;
  scope: MemoryScope;
  project: string;
  projectName: string;
  kind: MemoryKind;
  tags: string[];
  /** whose conversation it came from */
  engine: string;
  conversation: string;
  /** the sidebar row it can be opened from ("" when not known) */
  row: string;
  /** a short phrase: what makes it worth keeping */
  why: string;
  /** how sure the application is that the user wants it (0 to 1): the model's own sureness, and what the user kept and dropped before */
  score: number;
  /** what in the note made the score what it is (words the user tends to keep or drop) */
  explain: string[];
  at: number;
  /** the score is high enough to be ticked at first */
  suggestKeep: boolean;
}

/** `due`: a review is ready to be done; `last` / `next`: when the last one was and the next is ready (ms, 0: none);
 *  `labels`: how many verdicts of the user the filter has learned from; `size`: the most a review holds; `skipped`: notes
 *  left unwritten for want of certainty since the last review. */
export interface MemoryReviewState {
  due: boolean;
  pending: number;
  last: number;
  next: number;
  labels: number;
  size: number;
  skipped: number;
}

/** The notes of a review, the least sure first; `total`: how many wait in all (a review holds `size` at most). */
export interface MemoryReview extends MemoryReviewState {
  items: MemoryReviewItem[];
  total: number;
}

/** What a review did: the user's word (`kept`, `dropped`) and what the filter then did with the notes still waiting. */
export interface MemoryReviewSummary {
  kept: number;
  dropped: number;
  autoKept: number;
  autoDropped: number;
  labels: number;
  /** notes still waiting for a review */
  left: number;
  next: number;
}

/** The other applications whose memories and conversations can be brought in (desktop/core/importers). */
export type ImportApp = "claude" | "codex" | "devin" | "factory";

/** What one application has to bring: found on this computer or not, and how much (`import.scan`). */
export interface ImportSource {
  app: ImportApp;
  label: string;
  available: boolean;
  /** notes it could become (its memory files, split by heading) */
  memories: number;
  /** conversations it has */
  sessions: number;
  /** why it could not be read (a locked database, say) */
  error?: string;
}

/** A note that would be made from another application's memory file; nothing is written until the user ticks it. */
export interface ImportMemory {
  key: string;
  app: ImportApp;
  title: string;
  text: string;
  scope: MemoryScope;
  project: string;
  projectName: string;
  kind: MemoryKind;
  tags: string[];
  /** where it comes from: "<file> › <heading>" */
  origin: string;
  /** a note with these words is in the memory already */
  exists: boolean;
}

/** A conversation of another application that can become a conversation of the ChatGPT plan engine. */
export interface ImportSession {
  key: string;
  app: ImportApp;
  title: string;
  project: string;
  projectName: string;
  started: number;
  updated: number;
  /** what the user said and what was answered (not tool output) */
  messages: number;
  /** it was brought in before: importing it again replaces that copy */
  imported: boolean;
}

/** What `import.commit` did: ids of the notes and conversations made, and what was left out and why. */
export interface ImportResult {
  notes: { key: string; id: string; title: string }[];
  sessions: { key: string; id: string; title: string; row: string }[];
  skipped: { key: string; title: string; reason: string }[];
}

/** The API formats an API provider's model entry (`customModels[].provider`) can name. */
export type ApiKind = "anthropic" | "openai" | "generic-chat-completion-api" | "bedrock-converse";

/** How the key reaches the request: read from an environment variable (`${NAME}` in `apiKey`), printed by a command
 *  (`apiKeyHelper`), or not needed (local servers, gateways that authenticate otherwise).
 *
 *  "inline" is only what an existing settings.json may already contain (older setups wrote keys there).
 *  The editor shows it with a warning and offers the way out; it never lets you choose it, and the page never receives
 *  the key, only a masked `keyHint`. WittyCode itself never writes a key into settings.json. */
export type KeyMode = "inline" | "env" | "command" | "none";

/** Whether an environment variable is there for the requests WittyCode makes: set; "pending" is set in the user's
 *  environment (registry) but WittyCode started before that, so a restart is needed; or not set at all. */
export type EnvState = "set" | "pending" | "unset";

export interface ApiModel {
  id: string; // sent to the API
  displayName: string;
  contextWindow?: number; // maxContextLimit
  maxOutput?: number; // maxOutputTokens
  /** Default reasoning level; "" = no reasoning control. Configurable levels include XHigh and Max. */
  reasoning: Effort | "";
  images: boolean; // not noImageSupport
}

/** A model named by `providers.listModels`. Windows may come from the server or the bundled exact-id specification. */
export interface RemoteModel {
  id: string;
  contextWindow?: number;
  contextSource?: "server" | "catalog";
  maxOutput?: number;
}

/** A skill or a custom command the message box offers after a slash (`skills.list`; the files are in `.wittycode`, `.agents` and the
 *  other agents' skill folders, and in plugins). Naming one in a message makes its instructions go with the message, on every engine. */
export interface SkillItem {
  name: string;
  description: string;
  /** what a command wants after its name (`<branch>`), or "" */
  hint: string;
  kind: "skill" | "command";
  /** where it is kept: `project` | `personal` | `plugin` */
  source: string;
}

/** One API provider as the page shows it. WittyCode's own grouping: the models file's `customModels` is a flat list, the models
 *  of one base URL and format are shown together under a name that WittyCode keeps. `keyHint` is the variable
 *  (`${OPENROUTER_API_KEY}`), the command, or for a legacy inline key a masked tail: no key ever reaches the page. */
export interface ApiProvider {
  id: string;
  name: string;
  kind: ApiKind;
  baseUrl: string;
  keyMode: KeyMode;
  keyHint: string;
  keyTtlMs?: number; // apiKeyHelperTtlMs
  bearer?: boolean; // authMode: "bearer" (anthropic only)
  bedrock?: { method: "aws-credentials" | "credentials-command" | "environment-api-key"; region: string; profile: string };
  models: ApiModel[];
  headers: { k: string; v: string }[]; // extraHeaders
  extraArgs: string; // extraArgs, as JSON text
  status?: { ok: boolean; text: string };
}

/** What the editor sends on save/test. A legacy "inline" provider that is saved without switching to an environment
 *  variable keeps its key as it is. */
export type ProviderDraft = ApiProvider;
