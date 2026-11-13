// A stand-in for WittyCode's Python side, for scripts/check-live.mjs: the page runs in a plain browser exactly as it would
// in Qt WebEngine. Injected before the page's own scripts (Page.addScriptToEvaluateOnNewDocument):
//   1. a QWebChannel client: Qt's own qwebchannel.js when PySide6 is around (read out of Qt's resources the way
//      desktop/ui/shell.py does), otherwise a small stand-in that speaks the same protocol;
//   2. `qt.webChannelTransport` with the server side of the QWebChannel protocol behind it (init, invokeMethod,
//      connectToSignal, signals, responses) and one object `backend` with the slot `call` and the signals `reply` and
//      `event`, answering like desktop/core/app_core.py (same payload shapes, same order of events and replies) and
//      playing a short scripted conversation.
// The check reads what the page asked for from `window.__fake.calls` and plays events with `window.__fake.emit(...)`.
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const DUMP = [
  "import sys",
  "from PySide6.QtCore import QFile, QIODevice",
  "import PySide6.QtWebChannel",
  "f = QFile(':/qtwebchannel/qwebchannel.js')",
  "assert f.open(QIODevice.OpenModeFlag.ReadOnly)",
  "open(sys.argv[1], 'wb').write(bytes(f.readAll().data()))",
].join("\n");

/** Always extract from the selected interpreter: an old global temp cache must not make local checks differ from CI. */
export function qtWebChannelClient() {
  const python = process.env.WITTYCODE_TEST_PYTHON || resolve("../.venv/Scripts/python.exe");
  const unavailable = () => {
    if (process.env.WITTYCODE_REQUIRE_QT === "1") throw new Error("CI parity requires the pinned Python and PySide6 for Qt's real QWebChannel client");
    return null;
  };
  if (!existsSync(python)) return unavailable();
  const cache = resolve("shots/qwebchannel.js");
  mkdirSync(resolve("shots"), { recursive: true });
  const r = spawnSync(python, ["-c", DUMP, cache], { encoding: "utf8", timeout: 60_000, windowsHide: true });
  if (r.status !== 0 || !existsSync(cache)) return unavailable();
  const source = readFileSync(cache, "utf8");
  return source.includes("var QWebChannel") ? source : unavailable();
}

/** The scripts to inject, in order, and which QWebChannel client they use. */
export function fakeBackendScripts(config = {}) {
  const qt = qtWebChannelClient();
  return {
    client: qt ? "Qt qwebchannel.js (PySide6)" : "built-in stand-in (no PySide6 found)",
    scripts: [qt ?? `(${standInClient.toString()})();`, `(${fakeBackend.toString()})(${JSON.stringify(config)});`],
  };
}

// ----------------------------------------------------------------------------------------------- runs in the page

/** The same wire protocol as qwebchannel.js, much less of it: enough for one object with methods and signals. */
function standInClient() {
  window.QWebChannel = function QWebChannel(transport, ready) {
    const channel = this;
    const callbacks = {};
    const connections = {};
    let next = 0;
    channel.objects = {};
    const exec = (message, callback) => {
      if (callback) {
        message.id = next++;
        callbacks[message.id] = callback;
      }
      transport.send(JSON.stringify(message));
    };
    transport.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === 10) {
        callbacks[message.id]?.(message.data);
        delete callbacks[message.id];
      } else if (message.type === 1) (connections[message.signal] ?? []).forEach((cb) => cb(...message.args));
    };
    exec({ type: 3 }, (data) => {
      for (const [name, meta] of Object.entries(data)) {
        const object = {};
        for (const [method, index] of meta.methods)
          object[method] = (...args) => {
            const callback = typeof args[args.length - 1] === "function" ? args.pop() : undefined;
            exec({ type: 6, object: name, method: method.endsWith(")") ? index : method, args }, callback ?? (() => {}));
          };
        for (const [signal, index] of meta.signals)
          object[signal] = {
            connect(callback) {
              (connections[index] ??= []).push(callback);
              if (connections[index].length === 1) exec({ type: 7, object: name, signal: index });
            },
          };
        channel.objects[name] = object;
      }
      ready(channel);
      exec({ type: 4 });
    });
  };
}

function fakeBackend(config) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const later = (ms, fn) => setTimeout(fn, ms);
  const clone = (v) => JSON.parse(JSON.stringify(v));

  // ------------------------------------------------------------------ the QWebChannel server side
  const REPLY = 3;
  const EVENT = 4;
  const connected = new Set();
  const wire = [];
  const transport = {
    onmessage: null,
    send(raw) {
      const message = JSON.parse(raw);
      wire.push(["page", message.type]);
      setTimeout(() => receive(message), 0);
    },
  };
  const deliver = (message) =>
    setTimeout(() => {
      wire.push(["python", message.type]);
      transport.onmessage?.({ data: JSON.stringify(message) });
    }, 0);
  function receive(message) {
    if (message.type === 3) {
      // what Qt's QMetaObjectPublisher sends for Backend(QObject) of desktop/ui/shell.py
      deliver({
        type: 10,
        id: message.id,
        data: {
          backend: {
            methods: [
              ["deleteLater", 4],
              ["deleteLater()", 4],
              ["call", 5],
              ["call(QString,QString,QString)", 5],
            ],
            properties: [[0, "objectName", [1, 2], "backend"]],
            signals: [
              ["destroyed", 0],
              ["destroyed(QObject*)", 0],
              ["reply", REPLY],
              ["reply(QString,bool,QString)", REPLY],
              ["event", EVENT],
              ["event(QString,QString)", EVENT],
            ],
            enums: {},
          },
        },
      });
    } else if (message.type === 7 && message.object === "backend") connected.add(message.signal);
    else if (message.type === 6 && message.object === "backend" && (message.method === "call" || message.method === 5)) {
      deliver({ type: 10, id: message.id, data: null });
      call(...message.args);
    }
  }
  const signal = (index, args) => connected.has(index) && deliver({ type: 1, object: "backend", signal: index, args });

  // ------------------------------------------------------------------ AppCore, scripted
  const PROJECT = "D:\\Work\\demo-project";
  const OTHER = "D:\\Work\\second-project";
  /** the models of the user's API provider "Gateway", run by WittyCode's own loop (the window is not known yet) */
  const GW_MODELS = [
    { id: "api:gw:claude-haiku-5-5", label: "Claude Haiku 5.5", source: "api:gw", efforts: ["off", "low", "medium", "high"], defaultEffort: "low", window: 0 },
    { id: "api:gw:claude-opus-5-5", label: "Claude Opus 5.5", source: "api:gw", efforts: ["low", "medium", "high", "max"], defaultEffort: "high", window: 0 },
    { id: "api:gw:gpt-5.4", label: "GPT-5.4", source: "api:gw", efforts: ["none", "low", "medium", "high"], defaultEffort: "medium", window: 0 },
    { id: "api:gw:gemini-3-pro", label: "Gemini 3 Pro", source: "api:gw", efforts: ["dynamic", "low", "high"], defaultEffort: "dynamic", window: 0 },
    { id: "api:gw:glm-legacy", label: "GLM Legacy", source: "api:gw", efforts: [], defaultEffort: "", window: 0, disabled: true, disabledReason: "not offered by the gateway" },
  ];
  const GATEWAY = {
    id: "gw",
    name: "Gateway",
    kind: "openai",
    baseUrl: "https://gateway.example.com/v1",
    keyMode: "env",
    keyHint: "${GATEWAY_API_KEY}",
    models: GW_MODELS.map((m) => ({ id: m.id.slice(7), displayName: m.label, reasoning: m.defaultEffort, images: true })),
    headers: [],
    extraArgs: "",
  };
  /** a Devin account's models, sorted under their makers like Python's list */
  const DEVIN_MODELS = [
    { id: "devin:swe-2-high", label: "SWE-2", source: "devin", efforts: ["medium", "high", "max"], defaultEffort: "high", window: 262000, group: "Cognition" },
    { id: "devin:claude-opus-5-5-medium", label: "Claude Opus 5.5", source: "devin", efforts: ["low", "medium", "high", "xhigh", "max"], defaultEffort: "medium", window: 400000, group: "Anthropic" },
    { id: "devin:gpt-6-sol-medium", label: "GPT-6 Sol", source: "devin", efforts: ["low", "medium", "high", "xhigh"], defaultEffort: "medium", window: 400000, group: "OpenAI" },
  ];
  const DEVIN_IN = {
    cli: "found",
    auth: "signed-in",
    email: "dev@example.com",
    plan: "Devin Pro",
    version: "3000.11.238",
    cliPath: "C:\\Users\\dev\\AppData\\Local\\Programs\\devin\\devin.exe",
    enabled: true,
  };
  const CHATGPT_MODELS = [{ id: "chatgpt:gpt-6.1-sol", label: "GPT-6.1-Sol", source: "chatgpt", efforts: ["low", "medium", "high"], defaultEffort: "medium", window: 400000 }];
  /** the account as Python reports it (core/chatgpt/engine.py cached_account, plus what AppCore adds) */
  const CHATGPT_OUT = { auth: "signed-out", email: "", planEnabled: false, accounts: [], clientId: "", available: true, enabled: true, welcomed: false };
  const CHATGPT_IN = {
    auth: "signed-in",
    email: "dev@example.com",
    planEnabled: true,
    clientId: "oaiapp_live0001",
    accounts: [{ clientId: "oaiapp_live0001", label: "dev@example.com", signedIn: true, planEnabled: true, active: true }],
    available: true,
    enabled: true,
    welcomed: false,
  };
  const engineOf = (model) => (model.startsWith("devin:") ? "devin" : "chatgpt");
  const START = { devin: "code", chatgpt: "code" };
  const ago = (minutes) => Date.now() - minutes * 60_000;
  const savedRow = (id, projectId, title, modifiedAt, engine = "chatgpt") => ({
    id,
    projectId,
    title,
    modified: "（Python 的旧写法）",
    modifiedAt,
    engine,
    settings: { model: "", reasoning: "", autonomy: "", mode: "auto" },
    usage: null,
    context: { used: null, window: 0, model: "", compacted: false },
    workingLabel: "",
  });

  let releaseInit;
  const initReady = new Promise((resolve) => { releaseInit = resolve; if (!config.initHold) resolve(); });
  const S = {
    factory: { cli: "missing", cliPath: "", path: "", enabled: false },
    connections: [
      { id: "local-skill", kind: "skill", name: "summarize-diff", source: "C:\\Users\\me\\.agents\\skills\\summarize-diff\\SKILL.md", project: "", config: { path: "C:\\Users\\me\\.agents\\skills\\summarize-diff\\SKILL.md" }, enabled: true, imported: false },
      { id: "local-mcp", kind: "mcp", name: "local-tools", source: "C:\\Users\\me\\.codex\\config.toml", project: "", config: { command: "python", args: ["-m", "demo_mcp"] }, enabled: false, imported: false },
    ],
    calls: [],
    events: [],
    failNext: {},
    replyDelay: {},
    sendFiles: 10,
    projects: [PROJECT, OTHER],
    saved: [],
    archived: [],
    titles: {},
    savedLoaded: false,
    convs: {},
    order: [],
    cid: 0,
    draft: 0,
    id: 0,
    maximized: false,
    models: [...GW_MODELS],
    providers: [clone(GATEWAY)],
    defaults: { model: "", reasoning: "", autonomy: "code", mode: "auto" },
    settings: { maxOpen: 4, maxSteps: 80, devinPath: "", hiddenModels: [], theme: "dark", fontScale: "md", sidebarWidth: 300, memoryEnabled: true, memoryRecall: true, memoryCapture: true, memoryCurate: true, memoryEngines: ["devin", "chatgpt", "factory"], compactAuto: true, compactAt: 85, lastProject: "" },
    devin: { cli: "found", auth: "checking", enabled: true },
    chatgpt: clone(CHATGPT_OUT),
  };
  const nid = (prefix) => `${prefix}${++S.id}`;
  const reply = (callId, ok, payload) => signal(REPLY, [callId, ok, JSON.stringify(payload === undefined ? null : payload)]);
  const emit = (name, payload) => {
    S.events.push([name, clone(payload)]);
    signal(EVENT, [name, JSON.stringify(payload)]);
  };

  function call(method, paramsJson, callId) {
    let params;
    try {
      params = paramsJson ? JSON.parse(paramsJson) : {};
    } catch {
      return reply(callId, false, "参数不是合法的 JSON。");
    }
    S.calls.push({ method, params });
    const failure = S.failNext[method];
    if (failure) {
      delete S.failNext[method];
      return reply(callId, false, failure);
    }
    const handler = H[method];
    if (!handler) return reply(callId, false, `没有这个操作：${method}`);
    Promise.resolve()
      .then(() => handler(params))
      .then(async (result) => {
        // a slow call (an engine's process takes a second or two to start): what it does is done, the answer comes late
        const wait = S.replyDelay[method];
        if (wait) {
          delete S.replyDelay[method];
          await sleep(wait);
        }
        return result;
      })
      .then(
        (result) => reply(callId, true, result),
        (error) => reply(callId, false, error instanceof Error ? error.message : String(error)),
      );
  }

  // ------------------------------------------------------------------ sidebar and conversations
  function rowsFor(folder) {
    const open = S.order.map((cid) => S.convs[cid]).filter((c) => c.project === folder);
    const taken = new Set(open.map((c) => c.savedId).filter(Boolean));
    const rows = [...S.saved.filter((r) => r.projectId === folder && !taken.has(r.id) && !S.archived.includes(r.id)).map((r) => [r.modifiedAt, r.id]), ...open.map((c) => [c.usedAt, c.cid])];
    return rows.sort((a, b) => b[0] - a[0]).map((row) => row[1]);
  }
  const projectRows = () => S.projects.map((p) => ({ id: p, name: p.split("\\").pop(), path: p, collapsed: false, sessionIds: rowsFor(p) }));
  const shownRow = (r) => (S.titles[r.id] ? { ...r, title: S.titles[r.id] } : r);
  const archivedRows = () => S.archived.map((id) => S.saved.find((r) => r.id === id)).filter(Boolean).map(shownRow);
  const emitSidebar = () => emit("sidebar", { projects: projectRows(), sessions: Object.fromEntries(S.saved.filter((r) => !S.archived.includes(r.id)).map((r) => [r.id, shownRow(r)])), archived: clone(archivedRows()) });
  const emitTabs = (select) => emit("tabs", { tabs: [...S.order], select });
  const emitTurns = (c, turns, all = false) => emit("turns", { id: c.cid, turns: clone(turns), all });
  const modelOf = (id) => [...S.models, ...DEVIN_MODELS, ...CHATGPT_MODELS].find((m) => m.id === id);

  const metaOf = (c) => ({
    id: c.cid,
    projectId: c.project,
    title: c.title,
    mark: c.mark,
    workingLabel: c.workingLabel,
    modified: "刚刚",
    modifiedAt: c.usedAt,
    engine: c.engine,
    nativeId: c.nativeId,
    settings: { model: c.model, reasoning: c.reasoning, autonomy: c.autonomy, mode: c.mode },
    usage: c.usage,
    speed: c.speed ?? null,
    context: c.context,
    handoff: c.handoff,
  });
  /** AppCore._flush: only the keys that changed since the last time */
  function flush(c) {
    if (!S.convs[c.cid]) return;
    const patch = {};
    for (const [key, value] of Object.entries(metaOf(c)))
      if (JSON.stringify(c.sent[key]) !== JSON.stringify(value)) {
        patch[key] = clone(value);
        c.sent[key] = clone(value);
      }
    if (Object.keys(patch).length) emit("session", { id: c.cid, patch: { id: c.cid, ...patch } });
  }
  function open(project, model, extra = {}) {
    const info = modelOf(model);
    const engine = engineOf(model);
    const c = {
      cid: `c${++S.cid}`,
      project,
      model,
      engine,
      reasoning: info?.defaultEffort ?? "",
      autonomy: engine === "chatgpt" && S.defaults.autonomy ? S.defaults.autonomy : START[engine],
      mode: "auto",
      mark: "opening",
      title: "新会话",
      workingLabel: "正在连接…",
      usedAt: Date.now(),
      nativeId: "",
      usage: null,
      speed: null,
      context: { used: null, window: 0, model: "", compacted: false },
      handoff: null,
      turns: [],
      sent: {},
      drafts: [],
      sends: 0,
      waiting: null,
      ...extra,
    };
    S.convs[c.cid] = c;
    S.order.push(c.cid);
    emitTabs(c.cid);
    emitSidebar();
    later(40, () => flush(c));
    c.ready = new Promise((resolve) => {
      later(250, () => {
        c.mark = "ready";
        c.workingLabel = "就绪";
        c.nativeId ||= `native-${c.cid}`;
        flush(c);
        resolve();
      });
    });
    return c;
  }
  function close(c) {
    delete S.convs[c.cid];
    S.order = S.order.filter((cid) => cid !== c.cid);
    emitTabs(null);
    emitSidebar();
  }
  const conversation = (id) => {
    const c = S.convs[id];
    if (!c) throw new Error("这个会话已经关闭。");
    return c;
  };

  // ------------------------------------------------------------------ the scripted turns
  const PERMISSION_OPTIONS = [
    { value: "proceed_once", label: "允许（仅此一次）", tone: "primary" },
    { value: "proceed_always", label: "允许，并始终允许低风险操作", tone: "normal" },
    { value: "cancel", label: "拒绝", tone: "danger" },
  ];
  const FINAL = "改好了。说明见 [文档](https://example.com/docs)，本地文件 [notepad](file:///C:/Windows/notepad.exe) 不会被打开。";

  /** one streamed text block, flushed piece by piece like AppCore's 40 ms flushes */
  async function stream(c, turn, text, pieces = 6) {
    const block = { kind: "text", id: nid("b"), markdown: "", streaming: true };
    turn.blocks.push(block);
    c.workingLabel = "正在回答…";
    for (let i = 1; i <= pieces; i++) {
      block.markdown = text.slice(0, Math.ceil((text.length * i) / pieces));
      emitTurns(c, [turn]);
      flush(c);
      await sleep(140);
    }
    block.streaming = false;
  }
  function waitForAnswer(c, promptId) {
    return new Promise((resolve) => {
      c.waiting = { promptId, resolve };
    });
  }
  function finish(c, turn, status = "done") {
    turn.status = status;
    turn.seconds = 2.4;
    turn.steps = turn.blocks.filter((b) => b.kind === "activity").reduce((n, b) => n + b.entries.length, 0);
    turn.prompts = [];
    c.mark = "ready";
    c.workingLabel = "就绪";
    c.usage = { input: 1200 * c.sends, output: 340 * c.sends, cacheWrite: c.engine === "chatgpt" ? null : 800, cacheRead: 5000 * c.sends };
    c.speed = { tps: 42.3 + c.sends, tokens: 340 * c.sends, seconds: 8, first: 1.2, genTps: 50.1 };
    c.context = { used: 12345, window: c.engine === "devin" ? 262000 : 0, model: c.model, compacted: false };
    emitTurns(c, [turn]);
    flush(c);
  }
  async function withPermission(c, turn) {
    await stream(c, turn, "你好！我先看一下项目的结构，然后创建 hello.txt。这一段文字是一点一点流出来的。");
    const tool = { kind: "tool", id: nid("i"), name: "Create", category: "edit", description: "hello.txt", status: "awaiting_permission", diffs: [{ path: "hello.txt", kind: "new", added: 1, removed: 0, lines: [{ t: "add", text: "hi", new: 1 }] }] };
    turn.blocks.push({ kind: "activity", id: nid("b"), entries: [tool] });
    const prompt = { kind: "permission", id: nid("p"), status: "pending", actions: [{ title: "创建文件 hello.txt", toolName: "Create", path: "hello.txt", impact: "medium" }], options: PERMISSION_OPTIONS };
    turn.prompts = [prompt];
    c.mark = "attention";
    c.workingLabel = "等待你的确认";
    emitTurns(c, [turn]);
    flush(c);
    const value = await waitForAnswer(c, prompt.id);
    turn.prompts = [];
    if (value === null) return finish(c, turn, "interrupted");
    tool.status = value === "cancel" ? "cancelled" : "done";
    tool.decision = value;
    tool.seconds = 0.4;
    c.mark = "running";
    c.workingLabel = "正在运行…";
    emitTurns(c, [turn]);
    flush(c);
    await sleep(150);
    await stream(c, turn, FINAL, 3);
    finish(c, turn);
  }
  async function withQuestion(c, turn) {
    const tool = { kind: "tool", id: nid("i"), name: "AskUser", category: "ask", description: "用哪种方式？ 等 2 个问题", status: "awaiting_permission" };
    turn.blocks.push({ kind: "activity", id: nid("b"), entries: [tool] });
    const questions = [
      { topic: "方案", question: "用哪种方式？", options: ["方式 A", "方式 B"], multiSelect: false },
      { topic: "范围", question: "要包括哪些部分？", options: ["界面", "后台", "测试"], multiSelect: true },
    ];
    const prompt = { kind: "question", id: nid("p"), status: "pending", questions };
    turn.prompts = [prompt];
    c.mark = "attention";
    c.workingLabel = "等待你的确认";
    emitTurns(c, [turn]);
    flush(c);
    const answers = await waitForAnswer(c, prompt.id);
    turn.prompts = [];
    tool.status = "done";
    const text = (a) => (Array.isArray(a) ? a.join(", ") : a);
    turn.blocks.push({ kind: "qa", id: nid("b"), pairs: answers ? questions.map((q, i) => [q.question, text(answers[i])]) : [], cancelled: !answers });
    c.mark = "running";
    await stream(c, turn, "好的，就按你选的做。", 2);
    finish(c, turn);
  }
  async function plain(c, turn) {
    await stream(c, turn, "收到。", 2);
    finish(c, turn);
  }
  const SCRIPTS = [withPermission, withQuestion, plain];

  const sentView = (a) => clone(a);

  /** core/attachments.Overage.describe: by how much a message is over what one usually holds (10 files, 20 MB; `soft` here) */
  const MIB = 1024 * 1024;
  const sizeText = (n) => (n < 1024 ? `${n} B` : n < MIB ? `${(n / 1024).toFixed(1)} KB` : `${(n / MIB).toFixed(1)} MB`);
  /** what a message "usually holds" as the checks set it: `carryFiles` for the engine change, `S.sendFiles` for sending (settable) */
  const carryLimit = () => config.carryFiles ?? 10;
  const sendLimit = () => S.sendFiles;
  function describeOver(count, total, limit) {
    const parts = [];
    if (count > limit) parts.push(`多 ${count - limit} 个文件`);
    if (total > 20 * MIB) parts.push(`多 ${sizeText(total - 20 * MIB)}`);
    return parts.length ? `比一条消息通常带的量${parts.join("、")}（这条消息有 ${count} 个附件、${sizeText(total)}；通常最多 ${limit} 个、20 MB）` : "";
  }

  // ------------------------------------------------------------------ the shared memory (core/memory/bridge.py, in small)
  const M = { entries: [], journal: [], seen: 0, review: [], due: true, labels: 0 };
  const memoryState = () => ({ latest: M.journal.at(-1)?.seq ?? 0, seen: M.seen, unseen: M.journal.filter((c) => c.seq > M.seen && c.by.kind === "agent").length, pending: M.due ? M.review.length : 0 });
  const memoryEntry = (id) => {
    const entry = M.entries.find((e) => e.id === id);
    if (!entry) throw new Error("没有这条记忆。");
    return entry;
  };
  function memoryChange(op, entry, by, before = null) {
    M.journal.push({ seq: M.journal.length + 1, op, id: entry.id, by, at: Date.now(), before });
    emit("memory", memoryState());
    return { entry: clone(entry), ...memoryState() };
  }
  function memoryNew(p, by) {
    if (!String(p.title ?? "").trim()) throw new Error("记忆需要一个标题。");
    if (!String(p.text ?? "").trim()) throw new Error("记忆需要内容。");
    if (p.scope === "project" && !p.project) throw new Error("项目笔记要指明是哪个项目。");
    const at = Date.now();
    const entry = {
      id: `n${M.entries.length + 1}`,
      title: p.title.trim(),
      text: p.text.trim(),
      scope: p.scope,
      project: p.scope === "project" ? p.project : "",
      projectName: p.scope === "project" ? p.project.split(/[\\/]/).pop() : "",
      kind: p.kind ?? "fact",
      tags: p.tags ?? [],
      created: at,
      updated: at,
      status: "active",
      rev: 1,
      source: by,
      editor: by,
    };
    M.entries.unshift(entry);
    return memoryChange("create", entry, by);
  }
  const agentOf = (engine, title) => ({ kind: "agent", engine, ...(title ? { conversation: { title, rowId: "s-saved-1", projectId: PROJECT } } : {}) });
  function memorySet(id, op, patch) {
    const entry = memoryEntry(id);
    const before = clone(entry);
    Object.assign(entry, patch, { updated: Date.now(), rev: entry.rev + 1, editor: { kind: "user" } });
    return memoryChange(op, entry, { kind: "user" }, before);
  }

  // ------------------------------------------------------------------ the calls (AppCore._m_*)
  let fullPicture = null;
  const H = {
    "memory.list": (p) => {
      const status = p.status ?? "active";
      const words = String(p.query ?? "").toLowerCase().split(/\s+/).filter(Boolean);
      const entries = M.entries
        .filter((e) => status === "all" || e.status === status)
        .filter((e) => (!p.scope || e.scope === p.scope) && (!p.kind || e.kind === p.kind))
        .filter((e) => !p.engine || (e.source.kind === "agent" ? e.source.engine : e.source.kind === "app" ? "app" : "user") === p.engine)
        .filter((e) => words.every((w) => `${e.title} ${e.text} ${e.tags.join(" ")}`.toLowerCase().includes(w)));
      const counts = {};
      for (const e of M.entries) counts[e.status] = (counts[e.status] ?? 0) + 1;
      return { entries: clone(entries), counts, ...memoryState() };
    },
    "memory.changes": () => ({
      changes: M.journal
        .filter((c) => c.by.kind === "agent")
        .reverse()
        .map((c) => {
          const e = memoryEntry(c.id);
          return { seq: c.seq, op: c.op, id: c.id, title: e.title, scope: e.scope, projectName: e.projectName, by: c.by, at: c.at };
        }),
      ...memoryState(),
    }),
    "memory.create": (p) => memoryNew(p, { kind: "user" }),
    "memory.update": (p) => {
      const { id, ...patch } = p;
      return memorySet(id, "update", patch);
    },
    "memory.forget": (p) => memorySet(p.id, "forget", { status: "forgotten" }),
    "memory.restore": (p) => memorySet(p.id, "restore", { status: "active" }),
    "memory.undo": (p) => {
      const change = M.journal.find((c) => c.seq === p.seq);
      if (!change) throw new Error("没有这次改动。");
      return memorySet(change.id, change.op === "create" ? "forget" : "restore", { status: change.op === "forget" || change.op === "update" ? "active" : "forgotten" });
    },
    "memory.review": () => ({
      items: clone([...M.review].sort((a, b) => a.score - b.score).slice(0, 50)),
      total: M.review.length,
      due: M.due,
      pending: M.review.length,
      last: 0,
      next: Date.now() + 7 * 86400000,
      labels: M.labels,
      size: 50,
      skipped: 2,
    }),
    "memory.reviewApply": (p) => {
      const keep = p.keep ?? [];
      const drop = p.drop ?? [];
      for (const id of keep) {
        const edit = (p.edits ?? {})[id];
        if (edit) Object.assign(memoryEntry(id), edit);
      }
      for (const id of drop) memorySet(id, "forget", { status: "forgotten" });
      const decided = new Set([...keep, ...drop]);
      M.review = M.review.filter((i) => !decided.has(i.id));
      M.labels += decided.size;
      emit("memory", memoryState());
      return { kept: keep.length, dropped: drop.length, autoKept: 0, autoDropped: 0, labels: M.labels, left: M.review.length, next: Date.now() + 7 * 86400000, ...memoryState() };
    },    "memory.markSeen": (p) => {
      M.seen = p.seq === undefined ? M.journal.length : Math.max(0, Math.min(p.seq, M.journal.length));
      emit("memory", memoryState());
      return memoryState();
    },
    "app.init": async () => {
      await initReady;
      if (config.initDelay) await sleep(config.initDelay);
      later(60, () => {
        S.defaults = { model: "api:gw:claude-haiku-5-5", reasoning: "low", autonomy: "code", mode: "auto" };
        emit("models", { models: clone(S.models), defaults: S.defaults });
      });
      later(120, () => {
        S.saved = [savedRow("s-saved-1", PROJECT, "整理发布说明", ago(5)), savedRow("dv:th-9", PROJECT, "Devin 会话：迁移脚本", ago(40), "devin"), savedRow("s-saved-2", OTHER, "旧的会话", ago(60 * 24 * 3))];
        S.savedLoaded = true;
        emitSidebar();
      });
      return {
        version: 1,
        memory: memoryState(),
        projects: projectRows(),
        sessions: {},
        archived: clone(archivedRows()),
        tabs: [...S.order],
        models: [],
        defaults: S.defaults,
        settings: { ...S.settings },
        devin: S.devin,
        chatgpt: S.chatgpt,
        providers: clone(S.providers),
        maximized: S.maximized,
        offline: false,
      };
    },
    "project.add": () => ({ added: false }),
    "project.remove": (p) => {
      for (const cid of [...S.order]) if (S.convs[cid].project === p.id) close(S.convs[cid]);
      S.projects = S.projects.filter((x) => x !== p.id);
      emitSidebar();
      return null;
    },
    "project.refresh": () => {
      emitSidebar();
      return null;
    },
    "project.reveal": () => null,
    "session.new": (p) => {
      const project = p.projectId || (p.fromId && S.convs[p.fromId]?.project) || S.projects[0];
      if (!project) throw new Error("请先添加一个项目文件夹。");
      return { id: open(project, p.modelId || S.defaults.model).cid };
    },
    "session.openSaved": (p) => {
      const already = S.order.find((cid) => S.convs[cid].savedId === p.id);
      if (already) return { id: already };
      const row = S.saved.find((r) => r.id === p.id);
      const c = open(p.projectId, row?.engine === "devin" ? "devin:swe-2-high" : "api:gw:claude-haiku-5-5", { title: p.title || row?.title || "（无标题）", savedId: p.id, nativeId: p.id });
      // Python sends the restored history before the conversation's first session event
      c.turns = [{ id: nid("t"), status: "done", seconds: 4.2, blocks: [{ kind: "user", id: nid("b"), text: "上次的问题" }, { kind: "text", id: nid("b"), markdown: "上次的回答，见 `docs/notes.md:5`，另有 `docs/report.pdf`、`docs/long.pdf`、`docs/broken.pdf` 和 `shots/ui.png`。", streaming: false }], prompts: [] }];
      later(20, () => emitTurns(c, c.turns));
      return { id: c.cid };
    },
    "session.close": (p) => {
      close(conversation(p.id));
      return null;
    },
    "session.stop": (p) => {
      const c = conversation(p.id);
      if (c.waiting) {
        const w = c.waiting;
        c.waiting = null;
        w.resolve(null);
      }
      return null;
    },
    "session.reconnect": () => null,
    "session.compact": (p) => {
      conversation(p.id);
      return { ok: true, text: "已压缩：对话约 120K，压缩到约 18K。" };
    },
    "session.goal": (p) => {
      conversation(p.id);
      return { ok: true, text: `目标已设置：${p.text}` };
    },
    // the skills and custom commands the command menu adds (Python reads them from the user's files)
    "skills.list": (p) => {
      if (p.id) conversation(p.id);
      return {
        items: [
          { name: "weekly-report", description: "把本周的改动整理成周报", hint: "", kind: "skill", source: "personal" },
          { name: "deploy", description: "发布到测试环境", hint: "<环境>", kind: "command", source: "project" },
        ],
      };
    },
    "session.archive": (p) => {
      const c = S.convs[p.id];
      const id = c ? c.savedId || c.nativeId : p.id;
      if (c && c.mark === "running") throw new Error("这个会话还在运行：先停止它，再归档。");
      if (!id || !S.saved.some((r) => r.id === id)) throw new Error(c ? "这个会话还没有内容，直接关闭就行。" : "找不到这个会话。");
      if (c) close(c);
      if (!S.archived.includes(id)) S.archived.unshift(id);
      emitSidebar();
      return { ok: true };
    },
    "session.unarchive": (p) => {
      if (!S.archived.includes(p.id)) return { ok: false };
      S.archived = S.archived.filter((id) => id !== p.id);
      emitSidebar();
      return { ok: true };
    },
    "session.rename": (p) => {
      const c = S.convs[p.id];
      const id = c ? c.savedId || c.nativeId : p.id;
      const name = String(p.title ?? "").trim();
      if (name) S.titles[id] = name;
      else delete S.titles[id];
      if (c) {
        c.title = name || c.title;
        flush(c);
      }
      emitSidebar();
      return { ok: true, title: name };
    },
    "session.send": async (p) => {
      const c = conversation(p.id);
      // (the real application keeps a message sent while the conversation is still connecting and sends it when ready)
      if (c.mark === "opening") await c.ready;
      if (c.mark !== "ready") throw new Error("现在不能发送：会话没有就绪。");
      const wanted = new Set(p.attachmentIds ?? c.drafts.map((a) => a.id));
      const attachments = c.drafts.filter((a) => wanted.has(a.id));
      // AppCore._m_session_send: more than a message usually holds is the user's call: asked once, nothing sent
      const total = attachments.reduce((n, a) => n + (a.size || 0), 0);
      const over = describeOver(attachments.length, total, sendLimit());
      if (over && p.confirmOver !== true) {
        return { ok: false, over: { count: attachments.length, total, files: Math.max(0, attachments.length - sendLimit()), bytes: Math.max(0, total - 20 * MIB), text: over } };
      }
      c.drafts = c.drafts.filter((a) => !wanted.has(a.id));
      const text = String(p.text ?? "").trim() || "请查看附件。";
      if (c.title === "新会话") c.title = text.split("\n")[0].slice(0, 60);
      const user = { kind: "user", id: nid("b"), text };
      if (attachments.length) user.attachments = attachments.map(sentView);
      if (c.handoff) user.handoff = { carried: c.handoff.carried, truncated: c.handoff.truncated };
      const turn = { id: nid("t"), status: "running", blocks: [user], prompts: [] };
      c.turns.push(turn);
      c.mark = "running";
      c.workingLabel = "正在思考…";
      c.usedAt = Date.now();
      c.handoff = null;
      // ("quick: ..." is answered without asking for anything: a test that only needs the message to go out and the turn to end)
      const script = text.startsWith("quick:") ? (c.sends++, plain) : SCRIPTS[Math.min(c.sends++, SCRIPTS.length - 1)];
      later(40, () => {
        emitTurns(c, [turn]);
        flush(c);
        void script(c, turn);
      });
      return { ok: true };
    },
    "session.answerPermission": (p) => {
      const c = conversation(p.id);
      if (!c.waiting || c.waiting.promptId !== p.promptId) return { ok: false };
      const w = c.waiting;
      c.waiting = null;
      w.resolve(p.value);
      return { ok: true };
    },
    "session.answerQuestion": (p) => {
      const c = conversation(p.id);
      if (!c.waiting || c.waiting.promptId !== p.promptId) return { ok: false };
      const w = c.waiting;
      c.waiting = null;
      w.resolve(p.answers);
      return { ok: true };
    },
    "session.choose": (p) => {
      if (!["model", "reasoning", "autonomy", "mode"].includes(p.field)) throw new Error("没有这个设置。");
      const setDefault = (field, value) => {
        S.defaults = { ...S.defaults, [field]: value };
        if (field === "model") {
          const m = modelOf(value);
          if (m && m.efforts.length && !m.efforts.includes(S.defaults.reasoning)) S.defaults.reasoning = m.defaultEffort;
        }
        emit("defaults", { defaults: S.defaults });
      };
      if (!p.id) {
        setDefault(p.field, p.value);
        return { ok: true };
      }
      const c = conversation(p.id);
      if (p.field !== "model") {
        c[p.field] = p.value;
        later(40, () => flush(c));
        setDefault(p.field, p.value);
        return { ok: true };
      }
      const engine = engineOf(p.value);
      if (engine === c.engine) {
        c.model = p.value;
        later(40, () => flush(c));
        setDefault("model", p.value);
        return { ok: true };
      }
      if (c.mark === "running" || c.mark === "attention") throw new Error("本轮还在运行：结束后才能换引擎。");
      // AppCore._switch_engine: with any attachment in the conversation Python asks first and changes nothing; every
      // attachment is listed, how it would go or why it cannot (a PDF named "scan…" has no words, like a scanned one)
      const options = c.turns
        .flatMap((t) => t.blocks)
        .filter((b) => b.kind === "user")
        .flatMap((b) => b.attachments ?? [])
        .map((a) => {
          const words = a.kind === "text" || (a.kind === "pdf" && !/scan/i.test(a.name));
          const item = { id: a.id, name: a.name, kind: a.kind, as: words ? "text" : a.kind, bytes: a.size };
          if (!words && a.kind === "pdf") Object.assign(item, { as: "", reason: "模型只能读 PDF 里的文字，这个 PDF 里没有提取到文字。" });
          return item;
        });
      const inBox = { count: c.drafts.length, bytes: c.drafts.reduce((n, a) => n + (a.size || 0), 0) };
      if (options.length && p.attachments === undefined) {
        return { ok: false, choose: { attachments: options, limits: { files: carryLimit(), bytes: 20 * MIB }, hard: { files: 50, bytes: 200 * MIB }, drafts: inBox } };
      }
      if (p.overLimit !== undefined && typeof p.overLimit !== "boolean") throw new Error("这个设置的值不合法。");
      if (p.attachments !== undefined && (!Array.isArray(p.attachments) || !p.attachments.every((i) => typeof i === "string"))) throw new Error("这个设置的值不合法。");
      const chosen = [];
      for (const id of new Set(p.attachments ?? [])) {
        const option = options.find((o) => o.id === id);
        if (!option) throw new Error("找不到选中的附件，对话可能已经变了：请重新选择。");
        if (!option.as) throw new Error(`「${option.name}」带不过去：${option.reason}`);
        chosen.push(option);
      }
      const files = chosen.filter((o) => o.as !== "text");
      const held = files.length ? describeOver(files.length + inBox.count, files.reduce((n, o) => n + o.bytes, 0) + inBox.bytes, carryLimit()) : "";
      if (held && p.overLimit !== true) throw new Error(`${held}：要这样带过去，请在清单里确认。`);
      // a new native session continues the same transcript
      const from = modelOf(c.model);
      const to = modelOf(p.value);
      const carried = c.turns.flatMap((t) => t.blocks).filter((b) => b.kind === "user" || (b.kind === "text" && b.markdown)).length;
      const names = { devin: "Devin", chatgpt: "ChatGPT Plan" };
      const shownEngine = (m) => (m.source.startsWith("api:") ? "API" : names[engineOf(m.id)]);
      Object.assign(c, { engine, model: p.value, reasoning: to.defaultEffort, autonomy: START[engine], usage: null, speed: null, context: { used: null, window: to.window, model: "", compacted: false }, handoff: carried || chosen.length ? { carried, truncated: false, attachments: chosen.length, to: to.label } : null, sent: {} });
      setDefault("model", p.value);
      emitSidebar();
      if (carried) {
        const turn = { id: nid("t"), status: "done", blocks: [{ kind: "switch", id: nid("b"), from: { model: from.label, engine: shownEngine(from) }, to: { model: to.label, engine: shownEngine(to) }, carried, total: carried, bytes: 180, truncated: false }], prompts: [] };
        c.turns.push(turn);
        emitTurns(c, [turn]);
      }
      flush(c);
      const along = [
        [chosen.filter((o) => o.as === "text").length, "个文本附件的内容"],
        [chosen.filter((o) => o.as === "image").length, "张图片"],
        [chosen.filter((o) => o.as === "pdf").length, "个 PDF 文件"],
      ]
        .filter(([n]) => n)
        .map(([n, what]) => `${n} ${what}`);
      const filesNote = along.length ? `${along.join("、")}一起带过去。` : "";
      emit("notice", { text: `已换到 ${to.label}（${shownEngine(to)}）。${carried ? `之前 ${carried} 条对话的文字会随下一条消息交给它。` : ""}${filesNote}` });
      return { ok: true, switched: true };
    },
    "attachments.add": (p) => {
      const c = conversation(p.id);
      const added = [];
      const refused = [];
      for (const file of p.files ?? []) {
        if (!file.data || /\.bin$/i.test(file.name)) {
          refused.push(`「${file.name}」`);
          continue;
        }
        const id = `d${++S.draft}`;
        const size = Math.floor((file.data.length * 3) / 4);
        const view = /^image\//.test(file.type)
          ? { id, kind: "image", name: file.name, size, note: "已压缩", url: `data:${file.type};base64,${file.data}`, width: 64, height: 40 }
          : file.type === "application/pdf"
            ? { id, kind: "pdf", name: file.name, size }
            : { id, kind: "text", name: file.name, size, note: "按 GBK 读取" };
        c.drafts.push(view);
        added.push(view);
      }
      const heldNow = describeOver(c.drafts.length, c.drafts.reduce((n, a) => n + (a.size || 0), 0), sendLimit());
      return {
        added,
        rejected: refused.length ? `${refused.join("")}不是能附加的文件：只支持图片、PDF 和文本。` : "",
        over: heldNow ? `现在${heldNow}，发送时会让你确认。` : "",
      };
    },
    "attachments.pick": (p) => {
      const view = { id: `d${++S.draft}`, kind: "pdf", name: "picked.pdf", size: 2048 };
      conversation(p.id).drafts.push(view);
      return { added: [view], rejected: "" };
    },
    "attachments.remove": (p) => {
      const c = conversation(p.id);
      c.drafts = c.drafts.filter((a) => a.id !== p.attachmentId);
      return null;
    },
    "attachments.full": async () => {
      await sleep(250);
      if (!fullPicture) {
        const canvas = document.createElement("canvas");
        canvas.width = 300;
        canvas.height = 200;
        const g = canvas.getContext("2d");
        g.fillStyle = "#2f6bff";
        g.fillRect(0, 0, 300, 200);
        fullPicture = canvas.toDataURL("image/png");
      }
      return { url: fullPicture };
    },
    "settings.set": (p) => {
      const s = S.settings;
      const v = p.value;
      if (p.key === "theme" && ["system", "light", "dark"].includes(v)) s.theme = v;
      else if (p.key === "fontScale" && ["sm", "md", "lg"].includes(v)) s.fontScale = v;
      else if (p.key === "maxOpen" && Number.isInteger(v) && v >= 1 && v <= 16) s.maxOpen = v;
      else if (p.key === "maxSteps" && Number.isSafeInteger(v) && v >= 1) s.maxSteps = v;
      else if (p.key === "sidebarWidth" && Number.isInteger(v) && v >= 220 && v <= 440) s.sidebarWidth = v;
      else if (p.key === "devinPath" && typeof v === "string") s.devinPath = v.trim();
      else if (p.key === "memoryEnabled" && typeof v === "boolean") s.memoryEnabled = v;
      else if (p.key === "memoryRecall" && typeof v === "boolean") s.memoryRecall = v;
      else if (p.key === "memoryCapture" && typeof v === "boolean") s.memoryCapture = v;
      else if (p.key === "memoryCurate" && typeof v === "boolean") s.memoryCurate = v;
      else if (p.key === "memoryEngines" && Array.isArray(v) && v.every((e) => ["devin", "chatgpt", "factory"].includes(e))) s.memoryEngines = [...new Set(v)];
      else if (p.key === "compactAuto" && typeof v === "boolean") s.compactAuto = v;
      else if (p.key === "compactAt" && Number.isInteger(v) && v >= 50 && v <= 95) s.compactAt = v;
      else throw new Error("这个设置的值不合法。");
      emit("settings", { settings: { ...s } });
      return { ...s };
    },
    "models.setVisible": (p) => {
      const ids = p.ids;
      if (typeof p.visible !== "boolean" || !Array.isArray(ids) || !ids.every((i) => typeof i === "string" && i)) throw new Error("这个设置的值不合法。");
      const s = S.settings;
      s.hiddenModels = p.visible ? s.hiddenModels.filter((i) => !ids.includes(i)) : [...s.hiddenModels, ...new Set(ids.filter((i) => !s.hiddenModels.includes(i)))];
      emit("settings", { settings: { ...s } });
      return { ...s };
    },
    "connections.list": () => ({ items: clone(S.connections), errors: [], hookEvents: ["UserPromptSubmit", "PreToolUse", "PostToolUse"] }),
    "connections.preview": (p) => {
      const items = S.connections.filter((e) => e.source === p.path);
      if (!items.length) throw new Error("没有找到可导入的本地配置。");
      return { preview: "local-preview", items: clone(items), commands: items.filter((e) => e.kind === "mcp").map((e) => ({ name: e.name, kind: e.kind, command: e.config.command, args: e.config.args || [] })) };
    },
    "connections.import": (p) => {
      if (p.preview !== "local-preview" || !p.confirmed) throw new Error("请确认导入。");
      const item = S.connections.find((e) => e.id === "local-mcp");
      item.enabled = item.imported = true;
      return { ok: true, count: 1 };
    },
    "connections.update": (p) => {
      const item = S.connections.find((e) => e.id === p.id);
      if (!item) throw new Error("找不到这个连接。");
      if (item.kind !== "skill" && (p.config || (p.enabled && !item.imported)) && !p.confirmed) throw new Error("请确认命令。");
      Object.assign(item, { enabled: p.enabled, imported: true }, p.config ? { config: p.config } : {});
      return { ok: true };
    },
    "connections.test": () => ({ ok: true, text: "连接成功 · 1 个工具", tools: ["echo"] }),
    "factory.account": () => clone(S.factory),
    "factory.configure": (p) => (S.factory = { ...S.factory, path: p.path, enabled: p.enabled }),
    "devin.account": () => S.devin,
    "devin.login": async () => {
      await sleep(200);
      S.devin = clone(DEVIN_IN);
      emit("accounts", { devin: S.devin });
      later(30, () => {
        S.models = [...S.models.filter((m) => m.source !== "devin"), ...DEVIN_MODELS];
        emit("models", { models: clone(S.models), defaults: S.defaults });
      });
      return S.devin;
    },
    "devin.cancelLogin": () => S.devin,
    "devin.logout": () => (S.devin = { cli: "found", auth: "signed-out", enabled: true }),
    "devin.setEnabled": (p) => (S.devin = { ...S.devin, enabled: !!p.on }),
    "chatgpt.account": () => S.chatgpt,
    "chatgpt.login": async () => {
      emit("accounts", { chatgpt: { ...S.chatgpt, signingIn: true } });
      await sleep(200);
      S.chatgpt = clone(CHATGPT_IN);
      emit("accounts", { chatgpt: S.chatgpt });
      later(30, () => {
        S.models = [...S.models.filter((m) => m.source !== "chatgpt"), ...CHATGPT_MODELS];
        emit("models", { models: clone(S.models), defaults: S.defaults });
      });
      return S.chatgpt;
    },
    "chatgpt.cancelLogin": () => S.chatgpt,
    "chatgpt.logout": () => {
      S.chatgpt = clone(CHATGPT_OUT);
      S.models = S.models.filter((m) => m.source !== "chatgpt");
      later(30, () => emit("models", { models: clone(S.models), defaults: S.defaults }));
      return S.chatgpt;
    },
    "chatgpt.use": () => S.chatgpt,
    "chatgpt.setEnabled": (p) => (S.chatgpt = { ...S.chatgpt, enabled: !!p.on }),
    "chatgpt.welcomed": () => (S.chatgpt = { ...S.chatgpt, welcomed: true }),
    "providers.envStatus": (p) => Object.fromEntries((p.names ?? []).map((n) => [n, "unset"])),
    "providers.save": (p) => ({ ...p.draft }),
    "providers.remove": () => null,
    "providers.test": () => ({ ok: true, text: "连接成功 · 12 ms", ms: 12, models: null }),
    "providers.listModels": () => [{ id: "model-a", contextWindow: 200000, maxOutput: 32000 }, { id: "model-b" }],
    "window.minimize": () => null,
    "window.toggleMaximize": () => {
      S.maximized = !S.maximized;
      emit("window", { maximized: S.maximized });
      return S.maximized;
    },
    "window.close": () => null,
    "window.startMove": () => null,
    "window.startResize": () => null,
    "window.isMaximized": () => S.maximized,
    "shell.openExternal": () => null,
    "files.resolve": (p) => ({ path: `D:\\Work\\demo-project\\${String(p.path).replace(/:\d+(-\d+)?$/, "").replace(/\//g, "\\")}`, isDir: false, line: null }),
    "files.read": (p) => {
      if (/missing/.test(p.path)) throw new Error("找不到这个文件：" + p.path);
      const preview = /\.pdf$/i.test(p.path) ? "pdf" : /\.(png|jpe?g|svg)$/i.test(p.path) ? "image" : null;
      if (preview) {
        const name = p.path.split("/").pop();
        const pages = preview === "pdf" ? Array.from({ length: /long/.test(p.path) ? 40 : 5 }, (_, i) => ({ w: 595, h: i === 2 ? 595 : 842 })) : [];
        return { kind: preview, pages, path: "D:\\Work\\demo-project\\" + name, name, dir: "D:\\Work\\demo-project", text: "", size: 183400, lines: 0, encoding: "", truncated: false, markdown: false, line: null, lineEnd: null };
      }
      const range = /:(\d+)(?:-(\d+))?$/.exec(p.path);
      const text = Array.from({ length: 12 }, (_, i) => (i === 0 ? "# 笔记" : `第 ${i + 1} 行`)).join("\n");
      return { kind: "text", pages: [], path: "D:\\Work\\demo-project\\docs\\notes.md", name: "notes.md", dir: "D:\\Work\\demo-project\\docs", text, size: text.length, lines: 12, encoding: "UTF-8", truncated: false, markdown: true, line: range ? Number(range[1]) : null, lineEnd: range?.[2] ? Number(range[2]) : null };
    },
    "files.page": (p) => {
      if (/broken/.test(p.path) && p.page === 1) throw new Error("这一页画不出来。");
      const w = /\.pdf$/i.test(p.path) ? 595 : 320;
      const h = /\.pdf$/i.test(p.path) ? (p.page === 2 ? 595 : 842) : 200;
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="#fff"/><text x="30" y="60" font-size="28" fill="#222" font-family="sans-serif">page ${p.page + 1} @${p.width}</text></svg>`;
      return { url: "data:image/svg+xml;base64," + btoa(svg) };
    },
    "files.open": () => ({ ok: true }),
  };

  window.__fake = {
    releaseInit,
    calls: S.calls,
    events: S.events,
    wire,
    callsOf: (method) => S.calls.filter((c) => c.method === method).map((c) => c.params),
    emit,
    failNext: (method, message) => {
      S.failNext[method] = message;
    },
    delayReply: (method, ms) => {
      S.replyDelay[method] = ms;
    },
    /** how many attachments a message "usually holds" for sending (the engine change has its own: config.carryFiles) */
    setSendFiles: (n) => {
      S.sendFiles = n;
    },
    /** an agent writes a note to the memory, from a conversation (saved row s-saved-1) or from none */
    /** the application wrote a note by itself after a turn (it is in the memory, waiting for the weekly review) */
    autoNote: (title, text, score = 0.7, engine = "chatgpt", conversation = "整理发布说明") => {
      const made = memoryNew({ title, text, scope: "project", project: PROJECT, kind: "convention" }, { kind: "app" });
      M.review.push({ id: made.entry.id, title, text, scope: "project", project: PROJECT, projectName: PROJECT.split(/[\\/]/).pop(), kind: "convention", tags: [], engine, conversation, row: "s-saved-1", why: "用户明确说了", score, explain: ["提交", "中文"], at: Date.now(), suggestKeep: score >= 0.5 });
      emit("memory", memoryState());
    },
    /** a review is due (or not yet) */
    reviewDue: (due) => {
      M.due = due;
      emit("memory", memoryState());
    },    agentWrote: (engine, title, text, conversation = "整理发布说明") => memoryNew({ title, text, scope: "global", kind: "pitfall" }, agentOf(engine, conversation)),
    /** change an open conversation the way Python would, and send what changed */
    patch: (cid, changes) => {
      Object.assign(S.convs[cid], changes);
      flush(S.convs[cid]);
    },
    addSaved: (id, projectId, title, modifiedAt) => {
      S.saved.push(savedRow(id, projectId, title, modifiedAt));
      emitSidebar();
    },
    PROJECT,
    OTHER,
  };
  window.qt = { webChannelTransport: transport };
}
