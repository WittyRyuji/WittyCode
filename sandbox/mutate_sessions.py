"""Mutation checks for archiving and renaming conversations, the last project, and the Droid sessions nobody wrote to
(``core/app_core.py`` and ``core/settings.py``).

    python sandbox/mutate_sessions.py            run every mutant
    python sandbox/mutate_sessions.py --check    only check that every pattern is found exactly once
    python sandbox/mutate_sessions.py 3 7        run just these mutants
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
APP = ROOT / "desktop" / "core" / "app_core.py"
SETTINGS = ROOT / "desktop" / "core" / "settings.py"

TESTS = {
    APP: ["tests/test_app_core.py"],
    SETTINGS: ["tests/test_settings.py"],
}

M = [
    # ---- what the lists show
    ("rows: an archived session is still listed", APP, " or saved.id in taken or saved.id in self.settings.archived_sessions:", " or saved.id in taken:"),
    ("rows: an archived session is still in the page's map of saved rows", APP, "                if saved.id not in self.settings.archived_sessions:\n                    sessions[saved.id]", "                if True:\n                    sessions[saved.id]"),
    ("rows: the archived list is not newest-archived first", APP, "found.sort(key=lambda pair: pair[0], reverse=True)", "found.sort(key=lambda pair: pair[0])"),
    ("rows: the archived list holds a session that is not archived", APP, "                if stamp is not None:\n                    found.append", "                if True:\n                    found.append"),
    ("rows: the name given here is not shown on a saved row", APP, "self.settings.session_titles.get(saved.id) or saved.title or", "saved.title or"),
    ("rows: the name given here is not shown on an open conversation", APP, "        return self.settings.session_titles.get(self._row_of(conv)) or conv.controller.title", "        return conv.controller.title"),
    ("rows: a Droid session nobody wrote to is listed", APP, "            if item.message_count == 0:", "            if False:"),
    ("rows: a session without a count is hidden", APP, "            if item.message_count == 0:", "            if not item.message_count:"),
    ("init: a reloaded page is not told what is archived", APP, '"archived": self._archived_json(),\n            "tabs"', '"archived": [],\n            "tabs"'),
    # ---- archive
    ("archive: a running conversation is archived", APP, '            if conv.controller.busy:\n                raise AppError("这个会话还在运行：先停止它，再归档。")', '            if False:\n                raise AppError("这个会话还在运行：先停止它，再归档。")'),
    ("archive: an empty conversation is archived", APP, '            if not self._has_content(conv, row):\n                raise AppError("这个会话还没有内容，直接关闭就行。")', '            if False:\n                raise AppError("这个会话还没有内容，直接关闭就行。")'),
    ("archive: the conversation stays open", APP, "            await self._close_conversation(conv)\n        self.settings.archived_sessions[row] = int(time.time() * 1000)", "        self.settings.archived_sessions[row] = int(time.time() * 1000)"),
    ("archive: the time is not in ms", APP, "self.settings.archived_sessions[row] = int(time.time() * 1000)", "self.settings.archived_sessions[row] = int(time.time())"),
    ("archive: what was said counts for nothing", APP, "        return bool(conv.controller.transcript.turns) or any(", "        return any("),
    ("archive: only what was said counts", APP, "        return bool(conv.controller.transcript.turns) or any(item.id == row for rows in self._saved.values() for item in rows)", "        return bool(conv.controller.transcript.turns)"),
    ("archive: a conversation without an id of its own has content", APP, "        if not row:\n            return False\n        return bool(conv.controller.transcript.turns)", "        return bool(conv.controller.transcript.turns)"),
    ("unarchive: one that is not archived answers ok", APP, '        if self.settings.archived_sessions.pop(row, None) is None:\n            return {"ok": False}', '        if False:\n            return {"ok": False}'),
    ("unarchive: it is not taken out", APP, "        if self.settings.archived_sessions.pop(row, None) is None:", "        if self.settings.archived_sessions.get(row, None) is None:"),
    ("archive: an id nobody knows is taken", APP, '        if not known and ident not in self.settings.archived_sessions:\n            raise AppError("找不到这个会话。")', '        if False:\n            raise AppError("找不到这个会话。")'),
    ("archive: an id that is not text is taken", APP, '        if not isinstance(ident, str) or not ident:\n            raise AppError("找不到这个会话。")', '        if False:\n            raise AppError("找不到这个会话。")'),
    # ---- rename
    ("rename: a name that is not text is taken", APP, '        if not isinstance(title, str):\n            raise AppError("名字不合法。")', '        if False:\n            raise AppError("名字不合法。")'),
    ("rename: an empty conversation is named", APP, '        if conv is not None and not self._has_content(conv, row):\n            raise AppError("这个会话还没有内容，发出第一条消息之后才能改名。")', '        if False:\n            raise AppError("这个会话还没有内容，发出第一条消息之后才能改名。")'),
    ("rename: the spaces are kept", APP, 'name = " ".join(title.split())[:MAX_SESSION_TITLE]', "name = title.strip()[:MAX_SESSION_TITLE]"),
    ("rename: the name has no length limit", APP, 'name = " ".join(title.split())[:MAX_SESSION_TITLE]', 'name = " ".join(title.split())'),
    ("rename: an empty name does not give the engine's title back", APP, "        else:  # (an empty name gives the engine's own title back)\n            self.settings.session_titles.pop(row, None)", "        else:\n            pass"),
    ("rename: an open conversation keeps showing the old name", APP, "        if conv is not None:\n            conv.dirty = True\n            self._schedule_flush()\n        self._emit_sidebar()\n        return {\"ok\": True, \"title\": name}", "        self._emit_sidebar()\n        return {\"ok\": True, \"title\": name}"),
    ("rename: the answer is not the name kept", APP, '        return {"ok": True, "title": name}', '        return {"ok": True, "title": title}'),
    # ---- the project a window starts in
    ("last project: it is not remembered", APP, "        if self.settings.last_project != folder:\n            self.settings.last_project = folder\n            self._save()", "        if False:\n            self.settings.last_project = folder\n            self._save()"),
    ("last project: the page is not told", APP, '"lastProject": self.settings.last_project,', '"lastProject": "",'),
    # ---- the settings file
    ("settings: a true is an archive time", SETTINGS, "isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0", "isinstance(stamp, int) and stamp >= 0"),
    ("settings: a negative archive time is kept", SETTINGS, "isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0", "isinstance(stamp, int) and not isinstance(stamp, bool)"),
    ("settings: there is no limit to the archived ones", SETTINGS, "and len(archived) < MAX_REMEMBERED_SESSIONS:", ":"),
    ("settings: a name made of blanks is kept", SETTINGS, "isinstance(name, str) and name.strip() and len(titles)", "isinstance(name, str) and len(titles)"),
    ("settings: a name is not tidied", SETTINGS, 'titles[key] = " ".join(name.split())[:MAX_SESSION_TITLE]', "titles[key] = name"),
    ("settings: a name has no limit", SETTINGS, 'titles[key] = " ".join(name.split())[:MAX_SESSION_TITLE]', 'titles[key] = " ".join(name.split())'),
    ("settings: the last project is not read", SETTINGS, '        last_project=text("last_project"),', '        last_project="",'),
    ("settings: the archived ones are not read", SETTINGS, "        archived_sessions=archived,", "        archived_sessions={},"),
    ("settings: the names are not read", SETTINGS, "        session_titles=titles,", "        session_titles={},"),
]

args = sys.argv[1:]
check_only = "--check" in args
only = {int(a) for a in args if a.isdigit()}  # (numbers: run just those mutants)
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
    if only and number not in only:
        continue
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"[{number}] {name}: PATTERN FOUND {text.count(old)} TIMES", flush=True)
        survivors.append(name)
        continue
    if check_only:
        continue
    path.write_text(text.replace(old, new), encoding="utf-8")
    started = time.time()
    try:
        done = subprocess.run(
            [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS[path]], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        caught = done.returncode != 0
        tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    except subprocess.TimeoutExpired:
        caught, tail = True, "timeout (hangs: caught)"
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {tail if not caught or 'error' in tail.lower() else ''}", flush=True)
    if not caught:
        survivors.append(name)

print(f"\n{len(M) - len(survivors)} of {len(M)} caught" if not check_only else f"{len(M) - len(survivors)} of {len(M)} patterns found once")
for name in survivors:
    print("  survivor:", name)
sys.exit(1 if survivors else 0)
