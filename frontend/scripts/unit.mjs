// Unit tests of the live app's pure parts: how Python's events change the page's state (src/liveReducer.ts) and the
// relative times of the session list (src/lib/time.ts). Plain Node, no dependencies: `node scripts/unit.mjs`
// (Node 24 runs the .ts files as they are, it only strips the types).
import assert from "node:assert/strict";
import test from "node:test";
import { COMPACT_AT_DEFAULT, COMPACT_AT_MAX, COMPACT_AT_MIN, NO_CHATGPT, applyEvent, applyInit, chatgptView, clampCompactAt, emptyLiveData, selectWhenKnown, upsertTurns } from "../src/liveReducer.ts";
import { chatgptReady, chatgptState, planUsage, usageLevel } from "../src/lib/chatgpt.ts";
import { buildModelMenu } from "../src/components/modelMenu.ts";
import { formatRelative } from "../src/lib/time.ts";
import { localTarget, parsePathRef } from "../src/lib/pathref.ts";
import { carryOverage } from "../src/lib/carry.ts";
import { API_EFFORTS, contextProblem, contextWindowOf, imagesAfterKindChange, imagesByDefault, modelSpecification, withRemoteFigures } from "../src/lib/providerDefaults.ts";
import { CATEGORY_LABELS, groupSummary, toolLabel, usageTotal } from "../src/lib/format.ts";
import { changeVerb, isUnseen, parseTags, samePath, scopeLabel, sourceLine, whoConversation, whoName } from "../src/lib/memory.ts";
import { PRESETS } from "../src/lib/vocab.ts";
import { engineLabel, engineOfSource, envHintOf, envNameOf, envVarText, sourceLabel, sourceTag } from "../src/lib/models.ts";
import { effortLabel, permissionsFor, reasoningChoices, startingLevel } from "../src/lib/vocab.ts";
import { CALL_TIMEOUT_MS, QWebChannelTransport } from "../src/transport.ts";
import { COMMANDS, commandNamed, fromSkills, parseSlash, suggestions, unavailable } from "../src/lib/commands.ts";
import { quitWarning } from "../src/lib/quit.ts";

test("API windows use exact specifications, preserve overrides and require unknown limits", () => {
  const model = { id: "gpt-6.1-sol", displayName: "demo", images: true, reasoning: "max" };
  assert.equal(contextWindowOf(model), 1050000);
  assert.equal(contextProblem(model), "");
  assert.ok(modelSpecification(model.id).source.startsWith("https://developers.openai.com/"));
  assert.equal(contextWindowOf({ ...model, contextWindow: 64000 }), 64000);
  for (const id of ["gpt-6.1-sol-custom", "vendor/gpt-6.1-sol", "unknown"]) {
    assert.equal(modelSpecification(id), undefined);
    assert.ok(contextProblem({ ...model, id }).includes("请获取服务端数据"));
  }
  for (const contextWindow of [0, -1, 1.5, NaN]) assert.ok(contextProblem({ ...model, contextWindow }));
});

test("API metadata fills blank fields but never persists a catalog fallback as an override", () => {
  const model = { id: "gpt-6.1-sol", displayName: "", images: true, reasoning: "" };
  assert.equal(withRemoteFigures(model, { id: model.id, contextWindow: 1050000, contextSource: "catalog" }).contextWindow, undefined);
  assert.equal(withRemoteFigures(model, { id: model.id, contextWindow: 32000, contextSource: "server" }).contextWindow, 32000);
  assert.equal(withRemoteFigures({ ...model, contextWindow: 16000 }, { id: model.id, contextWindow: 32000, contextSource: "server" }).contextWindow, 16000);
  assert.deepEqual(API_EFFORTS, ["off", "minimal", "low", "medium", "high", "xhigh", "max"]);
});

test("quitting warns about running sessions, messages still waiting and goals in progress", () => {
  assert.equal(quitWarning([{ mark: "ready" }, { mark: "dead", queued: [] }]), "", "nothing would be lost: the window just closes");
  assert.equal(quitWarning([{ mark: "running" }]), "有 1 个会话还在工作。退出会中断它们正在做的事；下次可以从侧边栏恢复会话。");
  assert.equal(quitWarning([{ mark: "opening", queued: [{}, {}] }]), "2 条排队的消息还没发出，退出后不会再发出或继续；下次可以从侧边栏恢复会话。");
  const both = quitWarning([{ mark: "attention" }, { mark: "ready", goal: "make the tests pass" }]);
  assert.ok(both.startsWith("有 1 个会话还在工作") && both.includes("1 个目标（/goal）还没完成"), both);
});

// ------------------------------------------------------------------------------------------- payloads as Python sends them

const P = "D:\\Work\\proj";
const Q = "D:\\Work\\other";
const NOW = Date.now();

/** AppCore._saved_json */
const saved = (id, projectId, title, minutesAgo, engine = "chatgpt") => ({
  id,
  projectId,
  title,
  modified: `${minutesAgo} 分钟前`,
  modifiedAt: NOW - minutesAgo * 60_000,
  engine,
  settings: { model: "", reasoning: "", autonomy: "", mode: "auto" },
  usage: null,
  context: { used: null, window: 0, model: "", compacted: false },
  workingLabel: "",
});

/** AppCore._session_meta: the first patch of a conversation carries every key */
const meta = (id, extra = {}) => ({
  id,
  projectId: P,
  title: "新会话",
  mark: "opening",
  workingLabel: "正在连接…",
  modified: "刚刚",
  modifiedAt: NOW,
  engine: "chatgpt",
  nativeId: "",
  settings: { model: "chatgpt:gpt-6.1-sol", reasoning: "low", autonomy: "code", mode: "auto" },
  usage: null,
  context: { used: null, window: 0, model: "", compacted: false },
  handoff: null,
  ...extra,
});

const project = (id, sessionIds = []) => ({ id, name: id.split("\\").pop(), path: id, collapsed: false, sessionIds });
const userTurn = (id, text, status = "running") => ({ id, status, blocks: [{ kind: "user", id: `${id}-u`, text }], prompts: [] });
const textTurn = (id, markdown, status = "done") => ({ id, status, blocks: [{ kind: "text", id: `${id}-x`, markdown, streaming: status === "running" }], prompts: [] });

const MODELS = [
  { id: "chatgpt:gpt-6.1-sol", label: "GPT-6.1-Sol", source: "chatgpt", efforts: ["low", "high"], defaultEffort: "low", window: 0, group: "Anthropic" },
  { id: "devin:swe-2-high", label: "SWE-2", source: "devin", efforts: ["low", "medium", "high"], defaultEffort: "medium", window: 400000 },
];
const DEFAULTS = { model: "chatgpt:gpt-6.1-sol", reasoning: "low", autonomy: "off", mode: "auto" };
const SETTINGS = { maxOpen: 4, devinPath: "", hiddenModels: [], theme: "dark", fontScale: "lg", followUp: "queue", sidebarWidth: 300 };

/** AppCore._m_app_init */
const INIT = {
  version: 1,
  projects: [project(P)],
  sessions: {},
  tabs: [],
  models: [],
  defaults: { model: "", reasoning: "", autonomy: "off", mode: "auto" },
  settings: SETTINGS,
  devin: { cli: "found", auth: "checking", enabled: true },
  providers: [],
  maximized: false,
  offline: false,
};

/** Applies events in order the way live.ts does (state merged with each patch) and collects the side effects. */
function play(state, ...events) {
  const effects = [];
  for (const [name, payload] of events) {
    const r = applyEvent(state, name, payload);
    state = { ...state, ...r.patch };
    effects.push(...r.effects);
  }
  return { state, effects };
}

const started = () => ({ ...emptyLiveData(), ...applyInit(emptyLiveData(), INIT) });

/** a ready conversation c1 in P, shown */
function withConversation() {
  return play(started(), ["tabs", { tabs: ["c1"], select: "c1" }], ["session", { id: "c1", patch: meta("c1", { mark: "ready", workingLabel: "就绪" }) }]).state;
}

// ------------------------------------------------------------------------------------------------------------- init

test("before app.init the page holds nothing at all", () => {
  const s = emptyLiveData();
  assert.deepEqual([s.projects, s.sessions, s.tabs, s.models, s.providers], [[], {}, [], [], []]);
  assert.equal(s.currentId, null);
  assert.equal(s.devin.auth, "checking");
  assert.equal("factory" in s || "codex" in s, false, "no other program's account");
});

test("app.init: projects, settings, accounts and defaults come from the snapshot", () => {
  const s = started();
  assert.deepEqual(s.projects, [project(P)]);
  assert.deepEqual(s.tabs, []);
  assert.equal(s.currentId, null);
  assert.equal(s.themeChoice, "dark");
  assert.equal(s.fontScale, "lg");
  assert.equal(s.sidebarWidth, 300);
  assert.equal(s.devin.auth, "checking");
  assert.equal(s.defaults.model, "");
  assert.equal(s.maximized, false);
});

test("app.init keeps the page's own collapsed state of a project", () => {
  const before = { ...emptyLiveData(), projects: [{ ...project(P), collapsed: true }] };
  assert.equal(applyInit(before, INIT).projects[0].collapsed, true);
});

// --------------------------------------------------------------------------------------------------- tabs and select

test("a new tab is selected once its conversation is known, not before", () => {
  let { state } = play(started(), ["tabs", { tabs: ["c1"], select: "c1" }]);
  assert.deepEqual(state.tabs, ["c1"]);
  assert.equal(state.currentId, null, "no data yet: the tab is not shown empty");
  assert.equal(state.pendingSelect, "c1");
  ({ state } = play(state, ["session", { id: "c1", patch: meta("c1") }]));
  assert.equal(state.currentId, "c1");
  assert.equal(state.pendingSelect, null);
  assert.equal(state.sessions.c1.mark, "opening");
  assert.equal(state.sessions.c1.draft, "", "an open conversation gets a composer");
  assert.deepEqual(state.sessions.c1.attachments, []);
});

test("select of a known conversation shows it at once", () => {
  const s = withConversation();
  const { state } = play(s, ["tabs", { tabs: ["c1", "c2"], select: null }], ["session", { id: "c2", patch: meta("c2") }]);
  assert.equal(state.currentId, "c1", "a tab without select does not take the view");
  assert.equal(play(state, ["tabs", { tabs: ["c1", "c2"], select: "c2" }]).state.currentId, "c2");
});

test("closing the current tab shows its neighbour and forgets the conversation", () => {
  let s = withConversation();
  s = play(s, ["tabs", { tabs: ["c1", "c2", "c3"], select: null }], ["session", { id: "c2", patch: meta("c2") }], ["session", { id: "c3", patch: meta("c3") }]).state;
  s = { ...s, currentId: "c2" };
  const { state } = play(s, ["tabs", { tabs: ["c1", "c3"], select: null }]);
  assert.equal(state.currentId, "c3", "the tab that took its place");
  assert.equal(state.sessions.c2, undefined);
  assert.ok(state.sessions.c1 && state.sessions.c3);
  const last = play({ ...state, currentId: "c3" }, ["tabs", { tabs: ["c1"], select: null }]).state;
  assert.equal(last.currentId, "c1", "the last tab gives way to the one before it");
  assert.equal(play(last, ["tabs", { tabs: [], select: null }]).state.currentId, null);
});

test("a pending selection is dropped when its tab goes away (the session did not start)", () => {
  const { state } = play(withConversation(), ["tabs", { tabs: ["c1", "c2"], select: "c2" }], ["tabs", { tabs: ["c1"], select: null }]);
  assert.equal(state.pendingSelect, null);
  assert.equal(state.currentId, "c1");
});

test("selectWhenKnown: now if the page has the conversation's data, later otherwise", () => {
  const s = withConversation();
  assert.deepEqual(selectWhenKnown(s, "c1"), { currentId: "c1", pendingSelect: null });
  assert.deepEqual(selectWhenKnown(s, "c9"), { pendingSelect: "c9" });
});

// ---------------------------------------------------------------------------------------------------------- sidebar

test("sidebar: the saved rows are replaced, the open conversations stay", () => {
  let s = withConversation();
  s = { ...s, sessions: { ...s.sessions, c1: { ...s.sessions.c1, draft: "half a sentence" } } };
  let { state } = play(s, ["sidebar", { projects: [project(P, ["c1", "s-1", "s-2"])], sessions: { "s-1": saved("s-1", P, "First", 5), "s-2": saved("s-2", P, "Second", 90) } }]);
  assert.deepEqual(Object.keys(state.sessions).sort(), ["c1", "s-1", "s-2"]);
  assert.equal(state.sessions["s-1"].mark, undefined, "a saved row is not open");
  assert.equal(state.sessions["s-1"].modifiedAt, NOW - 5 * 60_000);
  assert.deepEqual(state.sessions["s-1"].turns, []);
  ({ state } = play(state, ["sidebar", { projects: [project(P, ["c1", "s-2"]), project(Q, ["dv:t1"])], sessions: { "s-2": saved("s-2", P, "Second", 90), "dv:t1": saved("dv:t1", Q, "Devin session", 3, "devin") } }]));
  assert.equal(state.sessions["s-1"], undefined, "a row Python no longer lists is gone");
  assert.equal(state.sessions["dv:t1"].engine, "devin");
  assert.equal(state.sessions.c1.draft, "half a sentence", "the open conversation is untouched");
  assert.deepEqual(state.projects.map((p) => p.id), [P, Q]);
});

test("archived rows come with app.init and the sidebar event; a sender without them leaves the list as it was", () => {
  assert.deepEqual(emptyLiveData().archived, []);
  const gone = saved("s-9", P, "Archived one", 3);
  const init = applyInit(emptyLiveData(), { ...INIT, archived: [gone] });
  assert.deepEqual(init.archived.map((r) => r.id), ["s-9"]);
  assert.deepEqual(applyInit(emptyLiveData(), INIT).archived, [], "an older Python without the key: none");
  let { state } = play({ ...started(), ...init }, ["sidebar", { projects: [project(P)], sessions: {}, archived: [] }]);
  assert.deepEqual(state.archived, [], "brought back");
  ({ state } = play({ ...started(), ...init }, ["sidebar", { projects: [project(P)], sessions: {} }]));
  assert.deepEqual(state.archived.map((r) => r.id), ["s-9"], "no key in the event: the list is kept");
});

test("the folder of the conversation started last comes with the settings (a view without it: none)", () => {
  assert.equal(emptyLiveData().lastProject, "");
  assert.equal(play(started(), ["settings", { settings: { ...SETTINGS, lastProject: P } }]).state.lastProject, P);
  assert.equal(play(started(), ["settings", { settings: SETTINGS }]).state.lastProject, "");
});

test("sidebar keeps the page's collapsed state", () => {
  const s = { ...started(), projects: [{ ...project(P), collapsed: true }] };
  const { state } = play(s, ["sidebar", { projects: [project(P), project(Q)], sessions: {} }]);
  assert.deepEqual(state.projects.map((p) => p.collapsed), [true, false]);
});

// ------------------------------------------------------------------------------------------------------- session

test("session: a patch changes only its keys", () => {
  const s = withConversation();
  const { state } = play(s, ["session", { id: "c1", patch: { id: "c1", mark: "running", workingLabel: "正在思考…" } }]);
  const c = state.sessions.c1;
  assert.equal(c.mark, "running");
  assert.equal(c.workingLabel, "正在思考…");
  assert.equal(c.projectId, P);
  assert.deepEqual(c.settings, meta("c1").settings);
});

test("session: settings, usage and context are replaced as a whole; handoff null means none", () => {
  let s = withConversation();
  s = play(s, ["session", { id: "c1", patch: { id: "c1", engine: "devin", settings: { model: "devin:swe-2-high", reasoning: "medium", autonomy: "smart", mode: "auto" }, handoff: { carried: 2, truncated: false, to: "SWE-2" } } }]).state;
  assert.equal(s.sessions.c1.engine, "devin");
  assert.equal(s.sessions.c1.settings.autonomy, "smart");
  assert.deepEqual(s.sessions.c1.handoff, { carried: 2, truncated: false, to: "SWE-2" });
  s = play(s, ["session", { id: "c1", patch: { id: "c1", handoff: null, usage: { input: 10, output: 20, cacheWrite: null, cacheRead: 5 }, context: { used: 12000, window: 0, model: "swe-2-high", compacted: false } } }]).state;
  assert.equal(s.sessions.c1.handoff, undefined);
  assert.deepEqual(s.sessions.c1.usage, { input: 10, output: 20, cacheWrite: null, cacheRead: 5 });
  assert.equal(s.sessions.c1.context.window, 0);
  assert.equal(s.sessions.c1.engine, "devin", "keys that were not in the patch stay");
});

const agent = (id, status = "working", extra = {}) => ({ id, name: "Dirac", role: "coder", model: "GPT-6.1-Sol", effort: "max", task: "Write sub.txt", status, startedAt: NOW, finishedAt: null, ...extra });

test("session: messages queued while connecting or running are shown in order, and an empty list takes them away", () => {
  let s = withConversation();
  const two = [
    { id: "q1", text: "hello", attachments: [] },
    { id: "q2", text: "and this", attachments: [] },
  ];
  s = play(s, ["session", { id: "c1", patch: { id: "c1", mark: "running", queued: two } }]).state;
  assert.deepEqual(s.sessions.c1.queued, two);
  s = play(s, ["session", { id: "c1", patch: { id: "c1", queued: [two[1]] } }]).state;
  assert.deepEqual(s.sessions.c1.queued.map((m) => m.id), ["q2"], "one withdrawn or sent: the rest stay");
  s = play(s, ["session", { id: "c1", patch: { id: "c1", queued: [] } }]).state;
  assert.equal(s.sessions.c1.queued, undefined);
});

test("a steer in a running turn is a user block marked steer, dashed (pending) until the engine has taken it in", () => {
  let s = withConversation();
  const turn = (pending) => ({
    id: "t1",
    status: "running",
    prompts: [],
    blocks: [
      { kind: "user", id: "b1", text: "start" },
      { kind: "user", id: "b2", text: "also this", steer: true, ...(pending ? { pending: true } : {}) },
    ],
  });
  s = play(s, ["turns", { id: "c1", turns: [turn(true)], all: true }]).state;
  assert.deepEqual(s.sessions.c1.turns[0].blocks.map((b) => [b.steer ?? false, b.pending ?? false]), [[false, false], [true, true]]);
  s = play(s, ["turns", { id: "c1", turns: [turn(false)], all: false }]).state;
  assert.equal(s.sessions.c1.turns[0].blocks[1].pending, undefined, "taken in");
});

test("sub-agents: the list comes with the conversation, and a run opened in the panel follows its events", () => {
  let s = withConversation();
  s = play(s, ["session", { id: "c1", patch: { id: "c1", agents: [agent("a1", "waiting")] } }]).state;
  assert.equal(s.sessions.c1.agents[0].status, "waiting");
  // the page opens the panel (live.ts sets this before it asks Python to watch)
  s = { ...s, agentPanel: { sessionId: "c1", agentId: "a1" }, agentTurns: [] };
  s = play(s, ["agentTurns", { id: "c1", agentId: "a1", all: true, turns: [userTurn("t1", "task", "running")] }]).state;
  assert.deepEqual(s.agentTurns.map((t) => t.id), ["t1"]);
  s = play(s, ["agentTurns", { id: "c1", agentId: "a1", all: false, turns: [textTurn("t1", "done")] }]).state;
  assert.equal(s.agentTurns.length, 1, "a changed turn replaces the one with its id");
  assert.equal(s.agentTurns[0].status, "done");
  s = play(s, ["agentTurns", { id: "c1", agentId: "a1", all: false, turns: [textTurn("t2", "more")] }]).state;
  assert.deepEqual(s.agentTurns.map((t) => t.id), ["t1", "t2"], "a new one is added");
});

test("sub-agents: a run that is not the one on show is ignored", () => {
  let s = withConversation();
  s = { ...s, agentPanel: { sessionId: "c1", agentId: "a1" }, agentTurns: [] };
  const other = play(s, ["agentTurns", { id: "c1", agentId: "a2", all: true, turns: [userTurn("t9", "x")] }]).state;
  assert.deepEqual(other.agentTurns, []);
  const elsewhere = play(s, ["agentTurns", { id: "c2", agentId: "a1", all: true, turns: [userTurn("t9", "x")] }]).state;
  assert.deepEqual(elsewhere.agentTurns, []);
  const closed = play({ ...s, agentPanel: null }, ["agentTurns", { id: "c1", agentId: "a1", all: true, turns: [userTurn("t9", "x")] }]).state;
  assert.deepEqual(closed.agentTurns, []);
});

test("sub-agents: the panel closes when its agent is gone or its conversation closes", () => {
  let s = withConversation();
  s = play(s, ["session", { id: "c1", patch: { id: "c1", agents: [agent("a1")] } }]).state;
  const open = { ...s, agentPanel: { sessionId: "c1", agentId: "a1" }, agentTurns: [userTurn("t1", "task")] };
  const kept = play(open, ["session", { id: "c1", patch: { id: "c1", agents: [agent("a1", "done")] } }]).state;
  assert.deepEqual(kept.agentPanel, { sessionId: "c1", agentId: "a1" }, "a change of state keeps it");
  const gone = play(open, ["session", { id: "c1", patch: { id: "c1", agents: [agent("a2")] } }]).state;
  assert.equal(gone.agentPanel, null);
  assert.deepEqual(gone.agentTurns, []);
  const tabGone = play(open, ["tabs", { tabs: [], select: null }]).state;
  assert.equal(tabGone.agentPanel, null);
  const otherTab = play(open, ["tabs", { tabs: ["c1", "c2"], select: null }]).state;
  assert.notEqual(otherTab.agentPanel, null, "another tab opening does not close it");
});

test("file panel: it goes with its conversation's tab and stays when another tab opens", () => {
  const s = { ...withConversation(), filePanel: { sessionId: "c1", ref: "docs/a.md", status: "loading" } };
  assert.equal(play(s, ["tabs", { tabs: [], select: null }]).state.filePanel, null);
  assert.notEqual(play(s, ["tabs", { tabs: ["c1", "c2"], select: null }]).state.filePanel, null);
});

test("session: the attachments that go along at an engine change are counted in the hand-over", () => {
  let s = withConversation();
  const handoff = { carried: 2, truncated: false, attachments: 3, to: "SWE-2" };
  s = play(s, ["session", { id: "c1", patch: { id: "c1", handoff } }]).state;
  assert.deepEqual(s.sessions.c1.handoff, handoff);
});

test("carry: pictures and PDF files over what a message usually holds are told by how much, never refused; a text file's words do not count", () => {
  const MIB = 1024 * 1024;
  const item = (id, as, bytes) => ({ id, name: id, kind: as === "text" ? "text" : as, as, bytes });
  const choice = (attachments, files = 2, bytes = 20 * MIB, drafts = { count: 0, bytes: 0 }) => ({ attachments, limits: { files, bytes }, drafts, hard: { files: 50, bytes: 200 * MIB } });
  const three = choice([item("a", "image", 10), item("b", "pdf", 10), item("c", "image", 10), item("t", "text", 50 * MIB), item("x", "", 5)]);
  const fine = { over: "", blocked: "" };
  assert.deepEqual(carryOverage(three, []), fine);
  assert.deepEqual(carryOverage(three, ["a", "b"]), fine, "as many as a message usually holds");
  assert.deepEqual(carryOverage(three, ["a", "b", "t"]), fine, "words of a text file are not files, however long");
  assert.deepEqual(carryOverage(three, ["a", "x"]), fine, "what cannot go is not counted here (Python refuses it by name)");
  assert.deepEqual(carryOverage(three, ["a", "b", "c"]), {
    over: "比一条消息通常带的量多 1 个文件（这条消息有 3 个附件、30 B；通常最多 2 个、20 MB）",
    blocked: "",
  });
  const big = choice([item("p", "image", 12 * MIB), item("q", "pdf", 9 * MIB), item("r", "image", 8 * MIB)], 10);
  assert.deepEqual(carryOverage(big, ["p", "q"]), {
    over: "比一条消息通常带的量多 1.0 MB（这条消息有 2 个附件、21.0 MB；通常最多 10 个、20 MB）",
    blocked: "",
  });
  assert.deepEqual(carryOverage(big, ["p", "r"]), fine, "exactly 20 MiB is the usual amount");
  assert.deepEqual(
    carryOverage(choice([item("p", "image", 12 * MIB), item("q", "pdf", 12 * MIB), item("r", "image", 1)], 2), ["p", "q", "r"]).over,
    "比一条消息通常带的量多 1 个文件、多 4.0 MB（这条消息有 3 个附件、24.0 MB；通常最多 2 个、20 MB）",
    "both at once",
  );
});

test("carry: past the safety ceiling a pick is blocked, in Python's words; below it nothing is", () => {
  const MIB = 1024 * 1024;
  const item = (id, bytes) => ({ id, name: id, kind: "image", as: "image", bytes });
  const many = Array.from({ length: 51 }, (_, i) => item(`i${i}`, 10));
  const choice = { attachments: many, limits: { files: 10, bytes: 20 * MIB }, drafts: { count: 0, bytes: 0 }, hard: { files: 50, bytes: 200 * MIB } };
  const ids = many.map((m) => m.id);
  assert.equal(carryOverage(choice, ids.slice(0, 50)).blocked, "", "fifty is a lot, but it is the user's call");
  assert.match(carryOverage(choice, ids.slice(0, 50)).over, /^比一条消息通常带的量多 40 个文件/);
  assert.equal(carryOverage(choice, ids).blocked, "附件太多了：一条消息最多 50 个、200 MB，现在有 51 个、510 B：再多多半是误操作，请分几条消息发。");
  assert.equal(carryOverage(choice, ids).over, "", "blocked is not also a question");
  const heavy = { ...choice, attachments: [item("h", 201 * MIB)] };
  assert.match(carryOverage(heavy, ["h"]).blocked, /^附件太多了：一条消息最多 50 个、200 MB，现在有 1 个、201\.0 MB/);
  assert.equal(carryOverage({ ...choice, hard: undefined, attachments: [item("h", 201 * MIB)] }, ["h"]).blocked.startsWith("附件太多了"), true, "an older Python: the same ceiling");
});

test("carry: what the message box already holds takes its share, and the sentence is Python's (core/attachments.Overage.describe)", () => {
  const MIB = 1024 * 1024;
  const item = (id, as, bytes) => ({ id, name: id, kind: as === "text" ? "text" : as, as, bytes });
  const choice = (attachments, drafts, files = 10) => ({ attachments, limits: { files, bytes: 20 * MIB }, drafts, hard: { files: 50, bytes: 200 * MIB } });
  const three = choice([item("a", "image", 10), item("b", "image", 10), item("c", "image", 10), item("t", "text", 5)], { count: 8, bytes: 800 });
  assert.equal(carryOverage(three, ["a", "b"]).over, "", "two files and eight in the box make ten");
  assert.equal(
    carryOverage(three, ["a", "b", "c"]).over,
    "比一条消息通常带的量多 1 个文件（这条消息有 11 个附件、830 B；通常最多 10 个、20 MB）",
  );
  assert.equal(carryOverage(three, ["t"]).over, "", "a text file's words are no file: not its matter, whatever the box holds");
  const weighty = choice([item("p", "image", 12 * MIB)], { count: 1, bytes: 9 * MIB });
  assert.equal(carryOverage(weighty, ["p"]).over, "比一条消息通常带的量多 1.0 MB（这条消息有 2 个附件、21.0 MB；通常最多 10 个、20 MB）");
  assert.equal(carryOverage({ ...three, drafts: undefined }, ["a", "b", "c"]).over, "", "a page talking to an older Python: the box counts as empty");
});

test("providers: pictures are on by default for every API format but the generic one (the usual default)", () => {
  assert.equal(imagesByDefault("anthropic"), true);
  assert.equal(imagesByDefault("openai"), true);
  assert.equal(imagesByDefault("bedrock-converse"), true);
  assert.equal(imagesByDefault("generic-chat-completion-api"), false);
});

test("providers: a row at the old format's default follows the new format, a row the user set keeps its value", () => {
  assert.equal(imagesAfterKindChange(false, "generic-chat-completion-api", "openai"), true, "untouched: follows");
  assert.equal(imagesAfterKindChange(true, "anthropic", "generic-chat-completion-api"), false, "untouched: follows");
  assert.equal(imagesAfterKindChange(true, "generic-chat-completion-api", "anthropic"), true, "switched on by the user: kept");
  assert.equal(imagesAfterKindChange(false, "openai", "anthropic"), false, "switched off by the user: kept");
  assert.equal(imagesAfterKindChange(true, "openai", "anthropic"), true);
});

test("session and turns of a conversation that is not open are ignored", () => {
  const s = withConversation();
  const { state } = play(s, ["session", { id: "c7", patch: meta("c7") }], ["turns", { id: "c7", turns: [userTurn("t1", "x")], all: false }]);
  assert.equal(state.sessions.c7, undefined);
});

// ---------------------------------------------------------------------------------------------------------- turns

test("turns: changed ones replace theirs in place, new ones are appended in order", () => {
  let s = withConversation();
  s = play(s, ["turns", { id: "c1", turns: [userTurn("t1", "hello")], all: false }]).state;
  s = play(s, ["turns", { id: "c1", turns: [{ ...userTurn("t1", "hello"), blocks: [...userTurn("t1", "hello").blocks, { kind: "text", id: "b2", markdown: "Hi", streaming: true }] }], all: false }]).state;
  assert.equal(s.sessions.c1.turns.length, 1);
  assert.equal(s.sessions.c1.turns[0].blocks[1].markdown, "Hi");
  s = play(s, ["turns", { id: "c1", turns: [userTurn("t1", "hello", "done"), userTurn("t2", "next"), textTurn("t3", "third")], all: false }]).state;
  assert.deepEqual(s.sessions.c1.turns.map((t) => t.id), ["t1", "t2", "t3"]);
  assert.equal(s.sessions.c1.turns[0].status, "done");
});

test("turns: all replaces the list", () => {
  let s = withConversation();
  s = play(s, ["turns", { id: "c1", turns: [userTurn("t1", "a"), userTurn("t2", "b")], all: false }]).state;
  s = play(s, ["turns", { id: "c1", turns: [textTurn("t5", "history")], all: true }]).state;
  assert.deepEqual(s.sessions.c1.turns.map((t) => t.id), ["t5"]);
  assert.deepEqual(play(s, ["turns", { id: "c1", turns: [], all: true }]).state.sessions.c1.turns, []);
});

test("turns that arrive before the conversation's first session event are kept", () => {
  let { state } = play(started(), ["tabs", { tabs: ["c1"], select: "c1" }], ["turns", { id: "c1", turns: [textTurn("t1", "restored history")], all: false }]);
  assert.equal(state.sessions.c1.mark, "opening");
  assert.equal(state.currentId, null, "still waiting for the conversation's data");
  ({ state } = play(state, ["session", { id: "c1", patch: meta("c1", { title: "Saved one" }) }]));
  assert.equal(state.currentId, "c1");
  assert.equal(state.sessions.c1.title, "Saved one");
  assert.deepEqual(state.sessions.c1.turns.map((t) => t.id), ["t1"]);
});

test("upsertTurns leaves the list as it is for no turns", () => {
  const turns = [userTurn("t1", "a")];
  assert.equal(upsertTurns(turns, []), turns);
});

// --------------------------------------------------------------------------------------------------------- restore

test("restore: the text goes into an empty composer, the attachments are Python's list", () => {
  const picture = { id: "d3", kind: "image", name: "a.png", size: 120, url: "data:image/png;base64,AAAA", width: 2, height: 2 };
  const s = withConversation();
  let { state } = play(s, ["restore", { id: "c1", text: "try this", attachments: [picture] }]);
  assert.equal(state.sessions.c1.draft, "try this");
  assert.deepEqual(state.sessions.c1.attachments, [picture]);
  const typing = { ...s, sessions: { ...s.sessions, c1: { ...s.sessions.c1, draft: "something new", attachments: [{ id: "d4", kind: "text", name: "n.txt", size: 3 }] } } };
  ({ state } = play(typing, ["restore", { id: "c1", text: "try this", attachments: [picture, { id: "d4", kind: "text", name: "n.txt", size: 3 }] }]));
  assert.equal(state.sessions.c1.draft, "try this\n\nsomething new", "the returned words come first, what was typed since stays after them");
  assert.deepEqual(state.sessions.c1.attachments.map((a) => a.id), ["d3", "d4"]);
  ({ state } = play(typing, ["restore", { id: "c1", text: "something new", attachments: [] }]));
  assert.equal(state.sessions.c1.draft, "something new", "the same words are not added twice");
});

test("settings: followUp comes with the settings, queue until Python says otherwise", () => {
  assert.equal(emptyLiveData().followUp, "queue");
  const { state } = play(started(), ["settings", { settings: { ...SETTINGS, followUp: "steer" } }]);
  assert.equal(state.followUp, "steer");
});

test("restore for a conversation that is gone does nothing", () => {
  const s = withConversation();
  assert.deepEqual(applyEvent(s, "restore", { id: "c9", text: "x", attachments: [] }).patch, {});
});

// ------------------------------------------------------------------------------------- models, accounts, settings

test("models brings the list and the defaults; defaults only the defaults", () => {
  let { state } = play(started(), ["models", { models: MODELS, defaults: DEFAULTS }]);
  assert.deepEqual(state.models, MODELS);
  assert.deepEqual(state.defaults, DEFAULTS);
  ({ state } = play(state, ["defaults", { defaults: { ...DEFAULTS, mode: "spec" } }]));
  assert.equal(state.defaults.mode, "spec");
  assert.deepEqual(state.models, MODELS);
});

test("accounts changes only the accounts it names", () => {
  const s = started();
  const signedIn = { cli: "found", auth: "signed-in", email: "demo@example.com", enabled: true };
  let { state } = play(s, ["accounts", { devin: signedIn }]);
  assert.deepEqual(state.devin, signedIn);
  assert.deepEqual(state.chatgpt, s.chatgpt);
  const chatgpt = { ...s.chatgpt, auth: "signed-in", email: "demo@example.com" };
  ({ state } = play(state, ["accounts", { chatgpt }]));
  assert.deepEqual(state.chatgpt, chatgpt);
  assert.deepEqual(state.devin, signedIn);
});

test("the Devin account: checking before Python has said anything, then what init and later events bring", () => {
  assert.equal(emptyLiveData().devin.auth, "checking");
  assert.equal(emptyLiveData().devin.enabled, false, "no Devin section in the model menu before Python has said anything");
  assert.deepEqual(started().devin, { cli: "found", auth: "checking", enabled: true });
  const s = started();
  const signedIn = { cli: "found", auth: "signed-in", email: "a@b.c", plan: "Devin Pro", enabled: true };
  let { state } = play(s, ["accounts", { devin: signedIn }]);
  assert.deepEqual(state.devin, signedIn);
  assert.deepEqual(state.chatgpt, s.chatgpt, "the other accounts are left as they were");
  ({ state } = play(state, ["accounts", { chatgpt: { ...s.chatgpt, auth: "signed-out" } }]));
  assert.deepEqual(state.devin, signedIn);
  // an older Python that does not know Devin: nothing to show
  const { devin, ...older } = INIT;
  assert.deepEqual(applyInit(emptyLiveData(), older).devin, { cli: "missing", auth: "signed-out", enabled: false });
});

const PLAN = { auth: "signed-in", email: "a@b.c", planEnabled: true, clientId: "oaiapp_1", accounts: [], enabled: true, welcomed: false, available: true };

test("the ChatGPT account: nothing before Python has said anything, then what init and later events bring", () => {
  assert.deepEqual(emptyLiveData().chatgpt, NO_CHATGPT);
  assert.equal(emptyLiveData().chatgpt.available, false, "no ChatGPT Plan section in the model menu before Python has said anything");
  const s = started();
  assert.deepEqual(s.chatgpt, NO_CHATGPT, "an older Python that does not know the engine: nothing to show");
  assert.deepEqual(applyInit(emptyLiveData(), { ...INIT, chatgpt: PLAN }).chatgpt, PLAN);
  let { state } = play(s, ["accounts", { chatgpt: PLAN }]);
  assert.deepEqual(state.chatgpt, PLAN);
  assert.deepEqual(state.devin, s.devin, "the other accounts are left as they were");
  ({ state } = play(state, ["accounts", { devin: { cli: "missing", auth: "signed-out", enabled: true } }]));
  assert.deepEqual(state.chatgpt, PLAN);
  // the backend says signingIn beside auth: for an account that is signed out that is the page's "signing-in"
  assert.equal(chatgptView({ ...PLAN, auth: "signed-out", signingIn: true }).auth, "signing-in");
  assert.equal(chatgptView({ ...PLAN, signingIn: true }).auth, "signed-in", "a signed-in account that renews its sign-in stays signed in");
  assert.deepEqual(chatgptView(undefined), NO_CHATGPT);
});

test("the ChatGPT account: what the card says and when the plan may be used", () => {
  assert.equal(chatgptReady(PLAN), true);
  assert.equal(chatgptReady({ ...PLAN, planEnabled: false }), false, "signed in without the permission to use the plan");
  assert.equal(chatgptReady({ ...PLAN, auth: "signed-out" }), false);
  assert.equal(chatgptReady({ ...PLAN, available: false }), false);
  assert.deepEqual(chatgptState(PLAN), { text: "已登录 · a@b.c · 使用你的 ChatGPT 套餐", tone: "ok" });
  assert.equal(chatgptState({ ...PLAN, planEnabled: false }).tone, "warn");
  assert.equal(chatgptState({ ...PLAN, auth: "signing-in" }).tone, "busy");
  assert.deepEqual(chatgptState({ ...PLAN, auth: "signed-out", email: "" }), { text: "未登录", tone: "off" });
});

test("a ChatGPT Plan model, session and permission scale are told apart from the others'", () => {
  assert.equal(engineOfSource("chatgpt"), "chatgpt");
  assert.equal(sourceLabel("chatgpt", []), "ChatGPT Plan");
  assert.equal(sourceTag("chatgpt"), "订阅");
  assert.deepEqual(permissionsFor("chatgpt").map((l) => l.value), ["ask", "code", "full"], "the order of WittyCode's own levels, least to most");
  assert.ok(permissionsFor("chatgpt").every((l) => l.label && l.hint));
});

test("the model menu: a ChatGPT Plan section with its models, or the way to sign in", () => {
  const models = [
    { id: "api:p1:qwen", label: "Qwen", source: "api:p1", efforts: [], defaultEffort: "", window: 0 },
    { id: "chatgpt:gpt-6.1-sol", label: "GPT-6.1-Sol", source: "chatgpt", efforts: ["low"], defaultEffort: "low", window: 0 },
  ];
  const input = { models, providers: [], devin: { cli: "found", auth: "signed-out", enabled: false }, current: "api:p1:qwen", onPick() {}, onManage() {} };
  const ids = (menu) => menu.map((i) => i.id);
  const ready = buildModelMenu({ ...input, chatgpt: PLAN });
  assert.ok(ids(ready).includes("h-chatgpt") && ids(ready).includes("chatgpt:gpt-6.1-sol"));
  assert.equal(ready.find((i) => i.id === "h-chatgpt").label, "CHATGPT PLAN");
  assert.equal(ready.find((i) => i.id === "h-chatgpt").description, "a@b.c");
  const out = buildModelMenu({ ...input, chatgpt: { ...PLAN, auth: "signed-out", planEnabled: false, email: "" } });
  assert.ok(!ids(out).includes("chatgpt:gpt-6.1-sol"), "no models before signing in");
  const login = out.find((i) => i.id === "chatgpt-login");
  assert.equal(login.label, "Continue with ChatGPT");
  const asked = [];
  login.onSelect();
  assert.deepEqual(asked, [], "without a handler the item opens the settings page");
  const direct = buildModelMenu({ ...input, chatgpt: { ...PLAN, auth: "signed-out", planEnabled: false }, onChatGPTLogin: (force) => asked.push(force) });
  direct.find((i) => i.id === "chatgpt-login").onSelect();
  assert.deepEqual(asked, [false], "signing in anew does not force the consent screen");
  const again = buildModelMenu({ ...input, chatgpt: { ...PLAN, planEnabled: false }, onChatGPTLogin: (force) => asked.push(force) });
  assert.equal(again.find((i) => i.id === "chatgpt-login").label, "允许使用 ChatGPT 套餐…");
  again.find((i) => i.id === "chatgpt-login").onSelect();
  assert.deepEqual(asked, [false, true], "signed in without the permission to use the plan: ask for it again");
  assert.ok(!ids(buildModelMenu({ ...input, chatgpt: { ...PLAN, enabled: false } })).includes("h-chatgpt"), "switched off in the settings");
  assert.ok(!ids(buildModelMenu({ ...input, chatgpt: { ...PLAN, available: false } })).includes("h-chatgpt"), "a build without the engine");
  const waiting = buildModelMenu({ ...input, chatgpt: { ...PLAN, auth: "signing-in", planEnabled: false } });
  assert.ok(!ids(waiting).includes("chatgpt-login"), "no second sign-in while the browser is open");
});

test("a call that waits for the user has no timeout; any other call gives up after two minutes", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const calls = [];
  const backend = { call: (method) => calls.push(method), reply: { connect() {} }, event: { connect() {} } };
  const Channel = function (transport, ready) {
    queueMicrotask(() => ready({ objects: { backend } }));
  };
  const transport = new QWebChannelTransport({}, Channel);
  const outcome = (promise) => {
    const box = { state: "waiting" };
    promise.then(
      () => (box.state = "answered"),
      (error) => (box.state = error.message),
    );
    return box;
  };
  const kinds = ["chatgpt.login", "devin.login", "project.add", "attachments.pick", "providers.list", "devin.account"];
  const boxes = kinds.map((method) => outcome(transport.call(method)));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, kinds, "all of them were sent");
  t.mock.timers.tick(CALL_TIMEOUT_MS + 1000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(
    boxes.map((b) => b.state),
    ["waiting", "waiting", "waiting", "waiting", "WittyCode 后台 120 秒没有回应（providers.list）。", "WittyCode 后台 120 秒没有回应（devin.account）。"],
    "the sign-ins and the file dialogs wait for as long as the user takes",
  );
});

test("Devin's program path is a setting like the others", () => {
  assert.equal(started().devinPath, "");
  const { state } = play(started(), ["settings", { settings: { ...SETTINGS, devinPath: "D:\\devin\\devin.exe" } }]);
  assert.equal(state.devinPath, "D:\\devin\\devin.exe");
  assert.equal(applyInit(emptyLiveData(), { ...INIT, settings: { ...SETTINGS, devinPath: undefined } }).devinPath, "", "a Python without the key");
});

test("a Devin model, session and permission scale are told apart from the own loop's", () => {
  assert.deepEqual(["devin", "api:x", "chatgpt"].map(engineOfSource), ["devin", "chatgpt", "chatgpt"], "an API provider's models run in the own loop");
  assert.deepEqual(["devin", "chatgpt", "api:x", "api:p1"].map(engineLabel), ["Devin", "ChatGPT Plan", "API", "API"], "to the user they are API, never the plan");
  assert.equal(sourceLabel("devin", []), "Devin");
  assert.equal(sourceLabel("chatgpt", []), "ChatGPT Plan");
  assert.equal(sourceTag("devin"), "订阅");
  assert.equal(sourceTag("api:x"), "API");
  assert.deepEqual(permissionsFor("devin").map((l) => l.value), ["ask", "plan", "code", "smart", "full"], "the order of Devin's modes, least to most");
  assert.deepEqual(["devin", "chatgpt"].map(startingLevel), ["code", "code"], "Devin and ChatGPT Plan start at Code, not at the head of the list");
  assert.ok(permissionsFor("devin").every((l) => l.label && l.hint));
});

test("planUsage: the plan's use is not known (OpenAI gives no figures to this sign-in), and the reason is said", () => {
  const chatgpt = { ...NO_CHATGPT, auth: "signed-in", email: "demo@example.com", planEnabled: true, available: true };
  const usage = planUsage(chatgpt);
  assert.equal(usage.known, false);
  assert.match(usage.why, /用量页面/);
  assert.match(planUsage({ ...chatgpt, auth: "signed-out" }).why, /还没有用 ChatGPT 套餐登录/);
  assert.deepEqual([0, 79, 80, 94, 95, 100].map(usageLevel), ["", "", "warn", "warn", "crit", "crit"]);
});
test("reasoning levels are written with a capital, also the ones the menu has no word for", () => {
  assert.equal(effortLabel("ultra"), "Ultra", "an engine may offer ultra");
  assert.deepEqual(["low", "medium", "high", "xhigh", "max"].map(effortLabel), ["Low", "Medium", "High", "XHigh", "Max"]);
  assert.equal(effortLabel("turbo"), "Turbo", "a level nobody knew yet still reads like the others");
  assert.equal(effortLabel(""), "");
  const choices = reasoningChoices({ id: "devin:x", label: "X", source: "devin", efforts: ["low", "xhigh", "ultra", "turbo"], defaultEffort: "low", window: 1 });
  assert.deepEqual(choices.map((c) => [c.value, c.label]), [["low", "Low"], ["xhigh", "XHigh"], ["ultra", "Ultra"], ["turbo", "Turbo"]]);
});
test("settings: every field of the settings view", () => {
  const { state } = play(started(), ["settings", { settings: { ...SETTINGS, theme: "light", fontScale: "sm", maxOpen: 6, sidebarWidth: 240, devinPath: "C:\\x\\devin.exe" } }]);
  assert.deepEqual(
    [state.themeChoice, state.fontScale, state.maxOpen, state.sidebarWidth, state.devinPath],
    ["light", "sm", 6, 240, "C:\\x\\devin.exe"],
  );
});

test("hidden models: nothing before Python says, then what its settings list (at init and in later events)", () => {
  assert.deepEqual(emptyLiveData().hiddenModels, []);
  assert.deepEqual(started().hiddenModels, []);
  const init = applyInit(emptyLiveData(), { ...INIT, settings: { ...SETTINGS, hiddenModels: ["gpt-5.4", "devin:swe-2-high"] } });
  assert.deepEqual(init.hiddenModels, ["gpt-5.4", "devin:swe-2-high"]);
  const { state } = play(started(), ["settings", { settings: { ...SETTINGS, hiddenModels: ["kimi-k3"] } }]);
  assert.deepEqual(state.hiddenModels, ["kimi-k3"]);
  assert.deepEqual(play(state, ["settings", { settings: { ...SETTINGS, hiddenModels: [] } }]).state.hiddenModels, [], "showing them again");
  const { hiddenModels, ...older } = SETTINGS;
  assert.deepEqual(play(state, ["settings", { settings: older }]).state.hiddenModels, [], "a settings view without the field hides nothing");
});

test("ChatGPT Plan compaction: the switch and the percent come with the settings, a view without them is the default", () => {
  const empty = emptyLiveData();
  assert.deepEqual([empty.compactAuto, empty.compactAt, COMPACT_AT_DEFAULT], [true, 85, 85]);
  const { state } = play(started(), ["settings", { settings: { ...SETTINGS, compactAuto: false, compactAt: 60 } }]);
  assert.deepEqual([state.compactAuto, state.compactAt], [false, 60]);
  const defaults = play(state, ["settings", { settings: SETTINGS }]).state;
  assert.deepEqual([defaults.compactAuto, defaults.compactAt], [true, 85], "an older sender without the keys: on, at 85");
  const init = applyInit(emptyLiveData(), { ...INIT, settings: { ...SETTINGS, compactAuto: false, compactAt: 95 } });
  assert.deepEqual([init.compactAuto, init.compactAt], [false, 95], "app.init carries them too");
  assert.deepEqual([40, 50, 52.4, 52.5, 87, 95, 100, 1e9].map(clampCompactAt), [50, 50, 52, 53, 87, 95, 95, 95], "whole points, inside the range the bridge accepts");
  assert.deepEqual([COMPACT_AT_MIN, COMPACT_AT_MAX], [50, 95]);
});

test("shared memory: switches come with the settings, the badge with app.init and the memory event", () => {
  const empty = emptyLiveData();
  assert.deepEqual([empty.memoryEnabled, empty.memoryRecall, empty.memoryCapture, empty.memoryCurate, empty.memoryEngines, empty.memory], [true, true, true, true, ["devin", "chatgpt"], { latest: 0, unseen: 0, seen: 0, pending: 0 }]);
  const off = { ...SETTINGS, memoryEnabled: false, memoryRecall: false, memoryCapture: false, memoryCurate: false, memoryEngines: ["devin"] };
  const { state } = play(started(), ["settings", { settings: off }]);
  assert.deepEqual([state.memoryEnabled, state.memoryRecall, state.memoryCapture, state.memoryCurate, state.memoryEngines], [false, false, false, false, ["devin"]]);
  const defaults = play(state, ["settings", { settings: SETTINGS }]).state;
  assert.deepEqual([defaults.memoryEnabled, defaults.memoryRecall, defaults.memoryCapture, defaults.memoryCurate, defaults.memoryEngines], [true, true, true, true, ["devin", "chatgpt"]], "a settings view without the keys is the default: on, for every engine");
  const init = applyInit(emptyLiveData(), { ...INIT, memory: { latest: 7, unseen: 2, seen: 5, pending: 3 } });
  assert.deepEqual(init.memory, { latest: 7, unseen: 2, seen: 5, pending: 3 });
  assert.deepEqual(applyInit(emptyLiveData(), { ...INIT, memory: null }).memory, { latest: 0, unseen: 0, seen: 0, pending: 0 }, "an application without memory");
  const heard = play(started(), ["memory", { latest: 8, unseen: 3, seen: 5, pending: 2 }]);
  assert.deepEqual(heard.state.memory, { latest: 8, unseen: 3, seen: 5, pending: 2 });
  assert.deepEqual(play(started(), ["memory", { latest: 8, unseen: 3, seen: 5 }]).state.memory.pending, 0, "an older sender without the number: none waiting");
  assert.deepEqual(heard.effects, [], "a change of the memory is not announced, the badge shows it");
});

test("shared memory: who wrote a note, and how a change and a source line read", () => {
  const devin = { kind: "agent", engine: "devin", conversation: { title: "修复缓存", rowId: "dv:1", projectId: "D:\\a" } };
  assert.deepEqual([{ kind: "user" }, devin, { kind: "agent", engine: "chatgpt" }, { kind: "unknown" }].map(whoName), ["我", "Devin", "ChatGPT Plan", "未知"]);
  assert.deepEqual(whoConversation(devin), { title: "修复缓存", rowId: "dv:1" });
  assert.equal(whoConversation({ kind: "agent", engine: "devin", conversation: { title: "x", rowId: "", projectId: "" } }), null, "a conversation that cannot be opened is not a link");
  assert.equal(whoConversation({ kind: "user" }), null);
  assert.deepEqual(["create", "update", "forget", "restore"].map(changeVerb), ["记住了", "修改了", "忘记了", "恢复了"]);
  assert.equal(isUnseen({ seq: 6 }, 5), true);
  assert.equal(isUnseen({ seq: 5 }, 5), false);
  const note = { source: devin, editor: devin, rev: 1 };
  assert.equal(sourceLine(note), "Devin 写入");
  assert.equal(sourceLine({ ...note, rev: 3 }), "Devin 写入", "changed again by the same engine");
  assert.equal(sourceLine({ ...note, editor: { kind: "user" }, rev: 2 }), "Devin 写入，我改过");
  assert.equal(sourceLine({ ...note, editor: { kind: "agent", engine: "chatgpt" }, rev: 2 }), "Devin 写入，ChatGPT Plan 改过");
  assert.deepEqual([{ scope: "global", projectName: "" }, { scope: "project", projectName: "wittycode" }].map(scopeLabel), ["全局", "wittycode"]);
});

test("shared memory: tags are split on commas and spaces, projects are compared as folders", () => {
  assert.deepEqual(parseTags("shell, windows，shell  测试、"), ["shell", "windows", "测试"]);
  assert.deepEqual(parseTags("  "), []);
  assert.equal(samePath("C:\\Users\\me\\code\\wittycode", "c:/users/me/code/wittycode/"), true);
  assert.equal(samePath("D:\\a", "D:\\b"), false);
});

test("shared memory: its tools read the same whichever way the engine names them", () => {
  const names = ["wittycode-memory___memory_write", "wittycode-memory/memory_write", "mcp__wittycode-memory__memory_write"];
  assert.deepEqual(names.map(toolLabel), ["记住", "记住", "记住"]);
  assert.deepEqual(["memory_search", "memory_read", "memory_update", "memory_forget"].map((t) => toolLabel(`wittycode-memory/${t}`)), ["回忆", "读取记忆", "修改记忆", "忘记"]);
  assert.equal(toolLabel("other-server/memory_write"), "other-server/memory_write", "only this server's tools");
  assert.equal(CATEGORY_LABELS.memory, "记忆");
  const block = { kind: "activity", id: "b1", entries: [{ kind: "tool", id: "t1", name: names[1], category: "memory", status: "done", description: "记住：x" }] };
  assert.match(groupSummary(block), /1 步 · 记忆 1/);
});

test("usageTotal: everything sent (new, cache written, cache read) and everything that came back", () => {
  assert.equal(usageTotal({ input: 10, output: 20, cacheWrite: 5, cacheRead: 100 }), 135);
  assert.equal(usageTotal({ input: 10, output: 20, cacheWrite: null, cacheRead: 100 }), 130, "Devin reports no cache writes");
});

test("provider presets: every one has a usable address, the bearer key only where the format allows it, models that fit", () => {
  const ids = PRESETS.map((p) => p.id);
  assert.equal(new Set(ids).size, ids.length, "ids are unique");
  for (const p of PRESETS) {
    if (p.kind !== "bedrock-converse" && p.id !== "custom") assert.match(p.baseUrl, /^https?:\/\/\S+$/, p.id);
    if (p.bearer) assert.equal(p.kind, "anthropic", `${p.id}: a bearer key goes only in the Anthropic format`);
    if (p.models) {
      assert.ok(p.models.length > 0 && p.models.every((m) => m.id.trim()), p.id);
      assert.equal(new Set(p.models.map((m) => m.id)).size, p.models.length, `${p.id}: duplicate model`);
    }
  }
  const mimo = PRESETS.filter((p) => p.id.startsWith("mimo-"));
  assert.deepEqual(mimo.map((p) => new URL(p.baseUrl).host), ["token-plan-cn.xiaomimimo.com", "token-plan-sgp.xiaomimimo.com", "token-plan-ams.xiaomimimo.com"]);
  assert.ok(mimo.every((p) => p.kind === "anthropic" && p.bearer && p.baseUrl.endsWith("/anthropic") && p.envName === "MIMO_API_KEY"));
  const deepseek = PRESETS.find((p) => p.id === "deepseek");
  assert.equal(deepseek?.baseUrl, "https://api.deepseek.com/anthropic");
  assert.deepEqual(deepseek?.models?.map((m) => m.id), ["deepseek-v4-pro", "deepseek-flash"]);
});

test("the environment-variable field takes the bare name; what is stored is still ${NAME}", () => {
  const stored = (name) => "$" + "{" + name + "}";
  assert.equal(envHintOf("MIMO_API_KEY"), stored("MIMO_API_KEY"));
  assert.equal(envHintOf("  MIMO_API_KEY  "), stored("MIMO_API_KEY"), "stray spaces around the name do not matter");
  assert.equal(envHintOf(stored("MIMO_API_KEY")), stored("MIMO_API_KEY"), "a pasted ${NAME} is not wrapped twice");
  assert.equal(envHintOf(""), "", "nothing typed stores nothing");
  assert.equal(envHintOf("   "), "");
  assert.equal(envVarText(stored("MIMO_API_KEY")), "MIMO_API_KEY");
  assert.equal(envVarText(""), "");
  assert.equal(envVarText("sk-12 34"), "sk-12 34", "a malformed value is shown as it is, so the user sees what is wrong");
  // Every keystroke round-trips: the field never rewrites what the user is typing.
  for (const typed of ["M", "MIMO_", "MIMO_API_KEY", "1BAD", "a b"]) assert.equal(envVarText(envHintOf(typed)), typed, typed);
  // The name check works on the stored form: a well-formed name passes, a key pasted by mistake does not.
  assert.equal(envNameOf(envHintOf("MIMO_API_KEY")), "MIMO_API_KEY");
  assert.equal(envNameOf(envHintOf("1BAD")), null);
  assert.equal(envNameOf(envHintOf("sk-12 34")), null);
  assert.equal(envNameOf(envHintOf("")), null);
});

test("window, notice, closeRequested and unknown events", () => {
  const s = started();
  assert.equal(play(s, ["window", { maximized: true }]).state.maximized, true);
  assert.deepEqual(applyEvent(s, "notice", { text: "已自动关闭最久未使用的会话" }), { patch: {}, effects: [{ kind: "notice", text: "已自动关闭最久未使用的会话" }] });
  assert.deepEqual(applyEvent(s, "closeRequested", {}), { patch: {}, effects: [{ kind: "quit" }] });
  assert.deepEqual(applyEvent(s, "somethingNew", { x: 1 }), { patch: {}, effects: [] });
});

// ----------------------------------------------------------------------------------------------- relative times

test("formatRelative words times the way desktop/core/timefmt.py does", () => {
  const at = (y, mo, d, h, mi, s = 0) => new Date(y, mo - 1, d, h, mi, s).getTime();
  const now = at(2026, 10, 29, 15, 30);
  assert.equal(formatRelative(now - 59_000, now), "刚刚");
  assert.equal(formatRelative(now + 3_600_000, now), "刚刚", "a time in the future counts as now");
  assert.equal(formatRelative(now - 60_000, now), "1 分钟前");
  assert.equal(formatRelative(now - 59 * 60_000 - 59_000, now), "59 分钟前");
  assert.equal(formatRelative(at(2026, 10, 29, 12, 29), now), "3 小时前");
  assert.equal(formatRelative(at(2026, 10, 29, 0, 5), now), "15 小时前");
  assert.equal(formatRelative(at(2026, 10, 28, 14, 20), now), "昨天 14:20");
  assert.equal(formatRelative(at(2026, 10, 28, 23, 50), at(2026, 10, 29, 0, 20)), "30 分钟前", "under an hour wins over the day");
  assert.equal(formatRelative(at(2026, 10, 28, 22, 10), at(2026, 10, 29, 0, 20)), "昨天 22:10");
  assert.equal(formatRelative(at(2026, 10, 27, 9, 5), now), "10-27 09:05");
  assert.equal(formatRelative(at(2026, 1, 2, 3, 4), now), "01-02 03:04");
  assert.equal(formatRelative(at(2025, 12, 31, 23, 0), now), "2025-12-31");
  assert.equal(formatRelative(at(2025, 12, 31, 23, 0), at(2026, 1, 1, 8, 0)), "昨天 23:00", "yesterday across the new year");
});

// ----------------------------------------------------------------------------------------------- paths in the text

test("parsePathRef: a file the text names, with the lines it points at", () => {
  assert.deepEqual(parsePathRef("docs/x.md"), { path: "docs/x.md", line: null, end: null });
  assert.deepEqual(parsePathRef("src/a.py:42"), { path: "src/a.py", line: 42, end: null });
  assert.deepEqual(parsePathRef("src/a.py:42-51"), { path: "src/a.py", line: 42, end: 51 });
  assert.deepEqual(parsePathRef("src/a.py:42:7"), { path: "src/a.py", line: 42, end: null });
  assert.deepEqual(parsePathRef("README.md"), { path: "README.md", line: null, end: null });
  assert.deepEqual(parsePathRef("D:\\work\\b.txt:7-9"), { path: "D:\\work\\b.txt", line: 7, end: 9 });
  assert.deepEqual(parsePathRef("C:/Users/me/a b/c.log"), { path: "C:/Users/me/a b/c.log", line: null, end: null });
  assert.deepEqual(parsePathRef("~/notes/todo.txt"), { path: "~/notes/todo.txt", line: null, end: null });
  assert.equal(parsePathRef("outputs/报告 最终版.pdf"), null, "a name with a space is a file only when the path is absolute");
  assert.ok(parsePathRef(".gitignore") && parsePathRef("src/.env"), "a file that is only an extension");
  assert.ok(parsePathRef("D:\\我的 文档\\报告.docx"), "an absolute path may hold spaces and any language");
});

test("parsePathRef: code that only looks like a path stays code", () => {
  for (const text of ["self.state", "os.path", "read/write", "v1.2", "a.b", "foo.bar()", "https://example.com/a.md", "npm run build", "git diff --stat", "-rf.md", "x = a/b.py", "a;b.py", "$HOME/x.md", "node_modules", "1.5", "src/", "C:", ".py", ".md"]) {
    assert.equal(parsePathRef(text), null, text);
  }
});

test("parsePathRef: a folder in front lets any extension count, a bare name needs a known one", () => {
  assert.equal(parsePathRef("lib/thing.unknownext")?.path, "lib/thing.unknownext");
  assert.ok(parsePathRef("lib/thing.h"));
  assert.equal(parsePathRef("thing.unknownext"), null);
  assert.ok(parsePathRef("Cargo.toml"));
});

test("localTarget: a link to a local file, whatever its writing; an address is not one", () => {
  assert.equal(localTarget("docs/x.md"), "docs/x.md");
  assert.equal(localTarget("docs/%E6%8A%A5%E5%91%8A.md"), "docs/报告.md");
  assert.equal(localTarget("file:///D:/work/b.txt"), "D:/work/b.txt");
  assert.equal(localTarget("file:///home/me/a.py"), "/home/me/a.py");
  assert.equal(localTarget("/D:/work/a.md"), "D:/work/a.md");
  assert.equal(localTarget("https://example.com/a.md"), null);
  assert.equal(localTarget("mailto:a@b.c"), null);
  assert.equal(localTarget("#section"), null);
  assert.equal(localTarget(undefined), null);
  assert.equal(localTarget("something-without-an-extension"), null);
});

// ------------------------------------------------------------------------------------------------- slash commands

test("parseSlash: one word after a slash is a command, anything else is a message", () => {
  assert.deepEqual(parseSlash("/compact"), { name: "compact", args: "", typing: true });
  assert.deepEqual(parseSlash("/"), { name: "", args: "", typing: true });
  assert.deepEqual(parseSlash("/goal fix the tests"), { name: "goal", args: "fix the tests", typing: false });
  assert.deepEqual(parseSlash("/goal   spaced  "), { name: "goal", args: "spaced", typing: false });
  assert.deepEqual(parseSlash("/goal\nsecond line"), { name: "goal", args: "second line", typing: false });
  assert.deepEqual(parseSlash("/plan "), { name: "plan", args: "", typing: false }, "a space ends the name");
  for (const text of ["", "hello", " /plan", "look at /usr/bin", "/usr/bin/env", "/路径", "//", "/a_b", "/a.b", "/1/2"]) {
    assert.equal(parseSlash(text), null, JSON.stringify(text));
  }
});

test("suggestions: the commands whose name starts with what is typed, only while the name is being typed", () => {
  assert.deepEqual(suggestions("/").map((c) => c.name), COMMANDS.map((c) => c.name));
  assert.deepEqual(suggestions("/co").map((c) => c.name), ["compact"]);
  assert.deepEqual(suggestions("/P").map((c) => c.name), ["plan"], "case does not matter");
  assert.deepEqual(suggestions("/goal").map((c) => c.name), ["goal"]);
  assert.deepEqual(suggestions("/goal "), [], "after the space the menu goes away");
  assert.deepEqual(suggestions("/zzz"), []);
  assert.deepEqual(suggestions("plan"), []);
  assert.deepEqual(suggestions(""), []);
});

test("commandNamed finds a command by its name, in any case", () => {
  assert.equal(commandNamed("compact")?.name, "compact");
  assert.equal(commandNamed("PLAN")?.name, "plan");
  assert.equal(commandNamed("nope"), undefined);
  assert.equal(commandNamed(""), undefined);
  assert.ok(COMMANDS.every((c) => c.hint.length > 0 && /^[a-z]+$/.test(c.name)));
});

test("unavailable: why a command cannot run, and null when it can", () => {
  const own = { session: true, engine: "chatgpt", running: false, plan: false };
  for (const name of ["compact", "plan"]) assert.equal(unavailable(name, own), null, name);
  assert.equal(unavailable("goal", own, "make it pass"), null);

  assert.match(unavailable("compact", { ...own, session: false }), /打开一个会话/);
  assert.match(unavailable("compact", { ...own, running: true }), /运行/);
  assert.equal(unavailable("compact", { ...own, engine: "chatgpt" }), null, "the own loop compacts");
  assert.match(unavailable("compact", { ...own, engine: "devin" }), /Devin 没有提供手动压缩/);

  assert.match(unavailable("goal", { ...own, session: false }, "x"), /打开一个会话/);
  assert.match(unavailable("goal", { ...own, running: true }, "x"), /运行/);
  for (const engine of ["devin", "chatgpt"]) assert.equal(unavailable("goal", { ...own, engine }, "x"), null, `a goal goes on any engine: ${engine}`);
  assert.match(unavailable("goal", { ...own, plan: true }, "x"), /Plan 模式/);
  assert.match(unavailable("goal", own, ""), /用法/);
  assert.match(unavailable("goal", own, "   "), /用法/);
  for (const word of ["off", "STOP", " clear ", "cancel", "关闭", "停止", "取消", "清除"]) {
    assert.equal(unavailable("goal", { ...own, running: true, plan: true }, word), null, `"${word}" cancels a goal whatever the state`);
  }
  assert.match(unavailable("goal", { ...own, session: false }, "off"), /打开一个会话/);

  for (const engine of ["devin", "chatgpt"]) assert.equal(unavailable("plan", { ...own, engine }), null, engine);
  assert.equal(unavailable("plan", { session: false, engine: "chatgpt", running: true, plan: false }), null, "Plan flips the next message, so it needs no session");
});

test("the page's own commands are /compact, /plan and /goal: the new-conversation button does not need a /new", () => {
  assert.deepEqual(COMMANDS.map((c) => c.name), ["compact", "plan", "goal"]);
  assert.equal(commandNamed("new"), undefined);
  assert.deepEqual(suggestions("/n").map((c) => c.name), []);
  const mine = fromSkills([{ name: "new-feature", description: "d", hint: "", kind: "skill", source: "project" }]);
  assert.deepEqual(suggestions("/n", mine).map((c) => c.name), ["new-feature"]);
});

test("the user's skills and commands join the menu after the page's own, and a name of the page's is not taken", () => {
  const extra = fromSkills([
    { name: "summarize-diff", description: "总结改动", hint: "", kind: "skill", source: "project" },
    { name: "code-review", description: "审分支", hint: "<分支>", kind: "command", source: "personal" },
    { name: "plan", description: "someone's own plan skill", hint: "", kind: "skill", source: "personal" },
    { name: "weekly-report", description: "周报", hint: "", kind: "skill", source: "personal" },
  ]);
  assert.deepEqual(extra[1], { name: "code-review", args: "<分支>", hint: "审分支", kind: "command", source: "personal" });
  assert.equal(extra[0].args, undefined, "no hint, no argument shown");
  assert.deepEqual(suggestions("/", extra).map((c) => c.name), ["compact", "plan", "goal", "summarize-diff", "code-review", "weekly-report"]);
  assert.equal(suggestions("/plan", extra).filter((c) => c.kind).length, 0, "the page's /plan, not the skill of that name");
  assert.deepEqual(suggestions("/wee", extra).map((c) => c.name), ["weekly-report"]);
  assert.deepEqual(suggestions("/Co", extra).map((c) => c.name), ["compact", "code-review"], "case does not matter");
  assert.deepEqual(suggestions("/wee ", extra), [], "after the space the menu goes away");
  assert.equal(commandNamed("weekly-report"), undefined, "the page runs none of them");
  assert.deepEqual(parseSlash("/weekly-report 第三周"), { name: "weekly-report", args: "第三周", typing: false });
  assert.deepEqual(parseSlash("/his"), { name: "his", args: "", typing: true });
  assert.equal(parseSlash("/a_b"), null);
});
