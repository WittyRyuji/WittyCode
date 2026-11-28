// The pinned strip's declared order is authoritative: `app.init` and `sidebar` keep member ids whose
// session details are still on the way (an open conversation's `session` event lands after them), and
// the late event puts its row in the strip — not back in its project. Plain Node: `node scripts/liveReducer.test.mjs`.
import assert from "node:assert/strict";
import test from "node:test";
import { applyEvent, applyInit, emptyLiveData } from "../src/liveReducer.ts";
import { pinnedOrder } from "../src/lib/sessionOrder.ts";

const P = "D:\\Work\\proj";
const NOW = Date.now();

const project = (id, sessionIds = []) => ({ id, name: id.split("\\").pop(), path: id, collapsed: false, sessionIds });
const saved = (id, projectId, extra = {}) => ({
  id,
  projectId,
  title: id,
  modified: "刚刚",
  modifiedAt: NOW,
  engine: "chatgpt",
  settings: { model: "", reasoning: "", autonomy: "", mode: "auto" },
  usage: null,
  context: { used: null, window: 0, model: "", compacted: false },
  workingLabel: "",
  ...extra,
});
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
  settings: { model: "", reasoning: "", autonomy: "", mode: "auto" },
  usage: null,
  context: { used: null, window: 0, model: "", compacted: false },
  handoff: null,
  ...extra,
});
const SETTINGS = { maxOpen: 4, devinPath: "", hiddenModels: [], theme: "dark", fontScale: "lg", followUp: "queue", sidebarWidth: 300 };
const init = (extra) => ({
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
  ...extra,
});

const started = (extra) => ({ ...emptyLiveData(), ...applyInit(emptyLiveData(), init(extra)) });
const play = (state, ...events) => {
  for (const [name, payload] of events) state = { ...state, ...applyEvent(state, name, payload).patch };
  return state;
};

test("pinnedOrder: a declared list keeps project members whose details have not landed", () => {
  const projects = [project(P, ["c1", "s-1", "s-2"])];
  const sessions = { "s-1": saved("s-1", P, { pinned: true }) };
  assert.deepEqual(pinnedOrder(projects, sessions, ["c1", "s-2", "s-1", "c1"]), ["c1", "s-2", "s-1"], "declared order, deduped, details optional");
  assert.deepEqual(pinnedOrder(projects, sessions, ["outside", "s-1"]), ["s-1"], "non-members still drop out");
  assert.deepEqual(pinnedOrder(projects, sessions), ["s-1"], "no declared list: the flags stand in");
  assert.deepEqual(pinnedOrder(projects, sessions, []), [], "an explicitly empty list stays empty");
});

test("app.init keeps a declared pin whose session event has not landed, and the late row reads as pinned", () => {
  let s = started({
    projects: [project(P, ["c1", "s-1"])],
    sessions: { "s-1": saved("s-1", P, { pinned: true }) },
    tabs: ["c1"],
    pinnedSessionIds: ["c1", "s-1"],
  });
  assert.deepEqual(s.pinnedSessionIds, ["c1", "s-1"], "the declared order survives missing details");
  assert.equal(s.sessions.c1, undefined, "details genuinely absent");
  s = play(s, ["session", { id: "c1", patch: meta("c1") }]);
  assert.equal(s.sessions.c1.pinned, true, "the late-arriving row reads as pinned");
  assert.deepEqual(s.pinnedSessionIds, ["c1", "s-1"], "no sidebar event needed");
});

test("the sidebar event keeps the same reservation and a patch that says pinned wins", () => {
  let s = play(
    started(),
    ["tabs", { tabs: ["c1"], select: null }],
    ["sidebar", { projects: [project(P, ["c1", "s-1"])], sessions: { "s-1": saved("s-1", P, { pinned: true }) }, pinnedSessionIds: ["c1", "s-1"] }],
  );
  assert.deepEqual(s.pinnedSessionIds, ["c1", "s-1"]);
  s = play(s, ["session", { id: "c1", patch: meta("c1") }]);
  assert.equal(s.sessions.c1.pinned, true);
  // an explicit flag in the patch beats the (by then stale) declared list
  s = play(s, ["session", { id: "c1", patch: { pinned: false } }]);
  assert.equal(s.sessions.c1.pinned, false);
});

test("a sidebar without pinnedSessionIds still derives the strip from the flags (older sender)", () => {
  const s = started({
    projects: [project(P, ["s-1", "s-2"])],
    sessions: { "s-1": saved("s-1", P, { pinned: true }), "s-2": saved("s-2", P) },
  });
  assert.deepEqual(s.pinnedSessionIds, ["s-1"]);
});
