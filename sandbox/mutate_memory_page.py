"""Mutation checks for the page side of the shared memory: the reducer and helpers (unit), the memory page and its badges
(check.mjs on the design page, check-live.mjs against the scripted Python)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
SRC = FE / "src"


def run(command, cwd, timeout=900):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=True, encoding="utf-8", errors="replace")


def unit():
    result = run("npm run unit", FE)
    return result.returncode != 0, ""


def page(script):
    def go():
        built = run("npm run build:app", FE)
        if built.returncode != 0:
            return False, "DID NOT BUILD"
        result = run(f"node scripts/{script}", FE)
        lines = [line for line in result.stdout.splitlines() if "FAIL" in line]
        return result.returncode != 0, lines[0] if lines else ""

    return go


R = SRC / "liveReducer.ts"
L = SRC / "lib" / "memory.ts"
F = SRC / "lib" / "format.ts"
P = SRC / "components" / "MemoryPage.tsx"
S = SRC / "components" / "Settings.tsx"
B = SRC / "components" / "Sidebar.tsx"

M = [
    ("reducer: the memory event is ignored", R, "return done({ memory: { latest: e.latest, unseen: e.unseen, seen: e.seen } });", "return done({});", unit),
    ("reducer: app.init does not carry the memory state", R, "    memory: init.memory ?? { ...NO_MEMORY },", "    memory: { ...NO_MEMORY },", unit),
    ("reducer: the master switch is always on", R, "    memoryEnabled: v.memoryEnabled ?? true,", "    memoryEnabled: true,", unit),
    ("reducer: every engine always has the memory", R, "    memoryEngines: v.memoryEngines ?? [...ALL_ENGINES],", "    memoryEngines: [...ALL_ENGINES],", unit),
    ("reducer: the recall switch is always on", R, "    memoryRecall: v.memoryRecall ?? true,", "    memoryRecall: true,", unit),
    ("page: the recall switch changes the master switch", P, "<Switch on={recall} onChange={setRecall}", "<Switch on={recall} onChange={(on) => { setRecall(on); setEnabled(on); }}", page("check.mjs")),
    ("page: the recall switch is shown with the memory off", P, "      {enabled && (\n        <Field label=\"每条消息带上相关笔记\"", "      {(\n        <Field label=\"每条消息带上相关笔记\"", page("check.mjs")),
    ("memory: a change at the seen mark is news", L, "export const isUnseen = (change: Pick<MemoryChange, \"seq\">, seen: number) => change.seq > seen;", "export const isUnseen = (change: Pick<MemoryChange, \"seq\">, seen: number) => change.seq >= seen;", unit),
    ("memory: a changed note is not told from a first draft", L, "  if (entry.rev <= 1) return wrote;\n", "", unit),
    ("memory: the same editor still counts as someone else", L, "return sameEditor ? wrote :", "return false ? wrote :", unit),
    ("memory: a conversation without a row is a link", L, "who.conversation && who.conversation.rowId ?", "who.conversation ?", unit),
    ("memory: tags are split on commas only", L, "text.split(/[,，、\\s]+/)", "text.split(\",\")", unit),
    ("memory: folders are compared case-sensitively", L, ".replace(/\\//g, \"\\\\\").toLowerCase() === b", ".replace(/\\//g, \"\\\\\") === b", unit),
    ("memory: a Latin name sticks to the Chinese", L, "return /[A-Za-z]$/.test(name) ? `${name} ` : name;", "return name;", unit),
    ("memory: a forgotten note is announced as remembered", L, "forget: \"忘记了\"", "forget: \"记住了\"", unit),
    ("format: another server's memory_write is ours", F, "name.includes(\"wittycode-memory\") ? MEMORY_LABELS", "true ? MEMORY_LABELS", unit),
    ("format: the memory has no category in a summary", F, "const CATEGORY_ORDER: Category[] = [\"read\", \"edit\", \"command\", \"plan\", \"ask\", \"memory\", \"other\"];", "const CATEGORY_ORDER: Category[] = [\"read\", \"edit\", \"command\", \"plan\", \"ask\", \"other\"];", unit),
    ("page: the answer of a call does not reach the badge", P, "    useStore.getState().setMemoryState(result);\n", "", page("check.mjs")),
    ("page: an engine switch changes none", P, "setEngines(ENGINES.filter((e) => (e === engine ? on : engines.includes(e))))", "setEngines(on ? engines : engines)", page("check.mjs")),
    ("page: the sidebar button always has a badge", B, "          {unseen > 0 && (\n            <span className=\"foot-badge\"", "          {(\n            <span className=\"foot-badge\"", page("check.mjs")),
    ("page: the settings tab always has a badge", S, "{p.id === \"memory\" && unseen > 0 && (", "{p.id === \"memory\" && (", page("check.mjs")),
    ("page: Esc in the editor closes the settings too", P, "          e.stopPropagation();\n          onCancel();", "          onCancel();", page("check.mjs")),
    ("page: the editor does not take the focus", P, " invalid={!!titleError} autoFocus />", " invalid={!!titleError} />", page("check.mjs")),
    ("page: a note that left the list stays open", P, "setOpenId((id) => (id !== null && r.entries.some((e) => e.id === id) ? id : null));", "setOpenId((id) => id);", page("check.mjs")),
    ("page: the list does not follow the memory", P, "  }, [query, scope, kind, engine, withForgotten, latest]);", "  }, [query, scope, kind, engine, withForgotten, latest * 0]);", page("check-live.mjs")),
    ("page: the changes do not follow the memory", P, "  }, [latest]);\n  const shown", "  }, [latest * 0]);\n  const shown", page("check-live.mjs")),
    ("page: the link to a conversation does not open it", P, "    st.openSettings(null);\n    return st.openSaved(rowId);", "    return;", page("check-live.mjs")),
    ("page: a note of a project is created without it", P, "...(v.scope === \"project\" ? { project: v.project } : {}) }),\n      () => `已记下", "}),\n      () => `已记下", page("check-live.mjs")),
]

only = sys.argv[1:]
survivors = []
for number, (name, path, old, new, test) in enumerate(M, 1):
    if only and not any(word in name for word in only):
        continue
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        caught, detail = test()
    finally:
        path.write_text(text, encoding="utf-8")
    verdict = "caught " if caught and detail != "DID NOT BUILD" else ("DID NOT BUILD" if detail == "DID NOT BUILD" else "SURVIVED")
    print(f"[{number}] {verdict} {name} ({time.time() - started:.0f}s) {detail}", flush=True)
    if verdict != "caught ":
        survivors.append(name)
built = run("npm run build:app", FE)
print("clean page rebuilt:", built.returncode == 0)
print("survivors:", survivors or "none")
