"""Mutation checks for the context window handling of the ChatGPT Plan conversation: ``harness/compact.py``, the loop's two
doors into it (``harness/loop.py``), what the store keeps (``harness/store.py``), the controller's wiring and the stale-loop
guard (``chatgpt/controller.py``).

    python sandbox/mutate_compaction.py            run every mutant
    python sandbox/mutate_compaction.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv").exists() else sys.executable  # (a copy of the repository has no .venv)
H = ROOT / "desktop" / "core" / "harness"
COMPACT, LOOP, STORE = H / "compact.py", H / "loop.py", H / "store.py"
CONTROLLER = ROOT / "desktop" / "core" / "chatgpt" / "controller.py"
APP = ROOT / "desktop" / "core" / "app_core.py"
SETTINGS = ROOT / "desktop" / "core" / "settings.py"

PURE = ["tests/test_harness_compact.py"]
WIRED = ["tests/test_chatgpt_compaction.py"]
TESTS = {
    COMPACT: PURE + WIRED,
    LOOP: PURE + ["tests/test_harness_loop.py"] + WIRED,
    STORE: ["tests/test_harness_store.py"] + PURE + WIRED,
    CONTROLLER: WIRED + ["tests/test_chatgpt_controller.py", "tests/test_chatgpt_recovery_crash.py"],
    APP: ["tests/test_app_standalone.py", "tests/test_app_core.py"],
    SETTINGS: ["tests/test_settings.py"],
}

M = [
    # ---- sizes
    ("sizes: latin text is far cheaper than it is", COMPACT, "int(plain / 3.5)", "int(plain / 35)"),
    ("sizes: other characters cost nothing", COMPACT, "(len(text) - plain) + 1", "0 + 1"),
    ("sizes: an image costs nothing", COMPACT, "                total += IMAGE_TOKENS\n", "                total += 0\n"),
    ("sizes: the data url of an image is counted as text", COMPACT, "            if part.get(\"type\") == \"input_image\":\n                total += IMAGE_TOKENS\n            else:", "            if False:\n                total += IMAGE_TOKENS\n            else:"),
    ("sizes: encrypted reasoning costs nothing", COMPACT, "len(str(item.get(\"encrypted_content\") or \"\")) // 5", "0"),
    # ---- the view
    ("view: a compaction past the end of the log is not held to it", COMPACT, "upto = max(0, min(int(compaction.get(\"upto\") or 0), len(items)))", "upto = int(compaction.get(\"upto\") or 0)"),
    ("view: the summary is left out", COMPACT, "    return [summary_item(str(compaction.get(\"summary\") or \"\"), kept_of(compaction)), *items[upto:]]", "    return list(items[upto:])"),
    ("view: a place behind a summary is counted from the summary", COMPACT, "    return upto + cut - synthetic", "    return upto + cut"),
    # ---- where to cut
    ("cut: a call can be cut off from its output", COMPACT, "        if index > 0 and not pending and view[index - 1].get(\"type\") != \"reasoning\":", "        if index > 0 and view[index - 1].get(\"type\") != \"reasoning\":"),
    ("cut: a reasoning item can be cut off from its call", COMPACT, "and view[index - 1].get(\"type\") != \"reasoning\":", ":"),
    ("cut: the first item can be cut off alone", COMPACT, "    points = [point for point in cut_points(view) if point > synthetic]", "    points = [point for point in cut_points(view) if point >= synthetic]"),
    ("cut: a turn is split though a message of the user would do", COMPACT, "    for pool in (users, points):", "    for pool in (points,):"),
    ("cut: the latest cut that fits is taken, not the earliest", COMPACT, "            return fitting[0]", "            return fitting[-1]"),
    ("cut: when nothing fits the biggest tail is kept", COMPACT, "    return points[-1]\n", "    return points[0]\n"),
    # ---- what the model reads
    ("render: the old summary is rendered as a message", COMPACT, "    covered = view[synthetic:cut]", "    covered = view[:cut]"),
    ("render: tool results are not cut", COMPACT, "{_clip(str(item.get('output') or ''), RESULT_LIMIT)}", "{str(item.get('output') or '')}"),
    ("chunks: everything goes in one request", COMPACT, "        if current and used + size > budget:", "        if current and used + size > budget * 1000:"),
    ("chunks: a part bigger than the budget is sent whole", COMPACT, "        if estimate_text(part) > budget:\n            part = _clip", "        if False:\n            part = _clip"),
    ("chunks: the budget has no floor", COMPACT, "    return max(CHUNK_FLOOR, min(CHUNK_CEILING, int(window * CHUNK_SHARE)))", "    return min(CHUNK_CEILING, int(window * CHUNK_SHARE))"),
    ("chunks: the budget has no ceiling", COMPACT, "    return max(CHUNK_FLOOR, min(CHUNK_CEILING, int(window * CHUNK_SHARE)))", "    return max(CHUNK_FLOOR, int(window * CHUNK_SHARE))"),
    ("digest: the newest questions are dropped, not the oldest", COMPACT, "        lines.pop(0)\n", "        lines.pop()\n"),
    ("digest: the previous summary is lost", COMPACT, "for part in (previous.strip(), head, body)", "for part in (head, body)"),
    # ---- asking
    ("ask: the summary request is not counted", COMPACT, "            if event.usage is not None and on_usage is not None:", "            if False:"),
    ("ask: a summary cut short is taken", COMPACT, "            if event.incomplete:\n                raise CompactionFailed", "            if False:\n                raise CompactionFailed"),
    ("ask: an empty summary is taken", COMPACT, "    if not text:\n        raise CompactionFailed(\"the summary came back empty\")", "    if False:\n        raise CompactionFailed(\"the summary came back empty\")"),
    ("ask: the summary request holds tools", COMPACT, "        tools=[],\n        reasoning=reasoning,\n        shape=Shape(encrypted_reasoning=False", "        tools=[{\"name\": \"x\", \"description\": \"\", \"parameters\": {}}],\n        reasoning=reasoning,\n        shape=Shape(encrypted_reasoning=False"),
    ("summarise: each piece starts from nothing", COMPACT, "        summary = await ask(stream, model, reasoning, summary, group, on_usage)", "        summary = await ask(stream, model, reasoning, previous, group, on_usage)"),
    ("compact: the old summary is forgotten", COMPACT, "    told = _joined(previous, \"\" if carried or not kept.strip() else f\"{KEPT_HEAD}\\n\\n{kept}\")", "    told = \"\""),
    ("compact: a failed request ends the conversation", COMPACT, "    except Exception as error:  # (a stop", "    except ZeroDivisionError as error:  # (a stop"),
    ("compact: a failed summary leaves no digest", COMPACT, "    if mechanical:\n        summary = digest(covered, told)", "    if False:\n        summary = digest(covered, told)"),
    ("compact: the summary goes in front of the wrong part", COMPACT, "    rest = list(view[cut:])", "    rest = list(view[cut + 1 :])"),
    # ---- the server says no
    ("overflow: the usage limit is taken for a size problem", COMPACT, "    if code.startswith(\"subscription_sharing\"):\n        return False\n", ""),
    ("overflow: the code alone does not count", COMPACT, "    if code in _OVERFLOW_CODES:\n        return True\n", ""),
    ("overflow: the words alone do not count", COMPACT, "    return bool(_OVERFLOW_TEXT.search(message))", "    return False"),
    # ---- the loop
    ("loop: the observer is never asked", LOOP, "            await self._prepare(history)\n            calls, incomplete", "            calls, incomplete"),
    ("loop: a replaced history keeps the old measure", LOOP, "        if replaced:\n            self._measured = None\n", "        if replaced:\n            pass\n"),
    ("loop: nothing is measured", LOOP, "        if context:\n            self._measured = (len(history), context)\n", ""),
    ("loop: the estimate counts the whole history again", LOOP, "self._measured[1] + estimate_items(history[self._measured[0] :])", "self._measured[1] + estimate_items(history)"),
    ("loop: a refusal after the answer began is tried again", LOOP, "            if self._events or not is_overflow(error):", "            if not is_overflow(error):"),
    ("loop: every error is tried again", LOOP, "            if self._events or not is_overflow(error):", "            if self._events:"),
    ("loop: a refusal nobody could help with is swallowed", LOOP, "            if not await self._prepare(history, overflow=True):\n                raise\n", "            await self._prepare(history, overflow=True)\n"),
    ("loop: events of an earlier request are counted", LOOP, "        context = 0\n        self._events = 0\n", "        context = 0\n"),
    # ---- the store
    ("store: a compaction past the log is believed", STORE, "    if isinstance(upto, bool) or not isinstance(upto, int) or not 0 <= upto <= count:", "    if isinstance(upto, bool) or not isinstance(upto, int):"),
    ("store: a compaction is not written", STORE, "    if session.compaction:\n        data[\"compaction\"] = session.compaction\n", ""),
    ("store: the place the summary stops is not moved", STORE, "compaction = {**compaction, \"upto\": position[upto] if upto < count else len(items)}", "compaction = {**compaction, \"upto\": upto}"),
    ("store: the turns are not moved", STORE, "        start = position[turn.start] if 0 <= turn.start < count else turn.start", "        start = turn.start"),
    ("store: the snapshot changes the live turns", STORE, "    turns, compaction = _remapped(session, items, position, copy=True)", "    turns, compaction = _remapped(session, items, position, copy=False)"),
    ("store: pairing in place copies the turns", STORE, "    session.turns, session.compaction = _remapped(session, items, position, copy=False)", "    session.turns, session.compaction = _remapped(session, items, position, copy=True)"),
    ("store: the output written for a call without one is always LOST", STORE, "\"output\": missing})", "\"output\": LOST})"),
    # ---- the controller
    ("controller: a stopped loop still owns the conversation", CONTROLLER, "    @property\n    def current(self) -> bool:\n        return self.ctl._turn is self.owner", "    @property\n    def current(self) -> bool:\n        return True"),
    ("controller: the log does not grow", CONTROLLER, "            self.ctl.saved.items.extend(items)  # (the loop works", "            pass  # (the loop works"),
    ("controller: a turn given up on leaves its calls without outputs", CONTROLLER, "            pair_in_place(self.saved, INTERRUPTED)\n", ""),
    ("controller: a turn given up on says the application crashed", CONTROLLER, "            pair_in_place(self.saved, INTERRUPTED)\n", "            pair_in_place(self.saved)\n"),
    ("controller: every request is compacted", CONTROLLER, "            if not over and (window is None or estimate < max(at, int(self._compacted_to * compact.PROGRESS))):", "            if not over and window is None:"),
    ("controller: nothing is compacted before the server refuses", CONTROLLER, "            if not over and (window is None or estimate < max(at, int(self._compacted_to * compact.PROGRESS))):", "            if True:"),
    ("controller: a compaction can follow another at once", CONTROLLER, "int(self._compacted_to * compact.PROGRESS)", "0"),
    ("controller: the user's switch is ignored", CONTROLLER, "            if not settings.chatgpt_compact:  # (the user's choice", "            if False:  # (the user's choice"),
    ("controller: the switch also holds back the answer to a refusal", CONTROLLER, "        if not overflow:\n            settings = self.ctl.manager.settings\n            if not settings.chatgpt_compact:", "        settings = self.ctl.manager.settings\n        if True:\n            if not settings.chatgpt_compact:"),
    ("controller: the user's percent is ignored", CONTROLLER, "int(window * settings.chatgpt_compact_at / 100)", "int(window * 85 / 100)"),
    ("controller: the percent is counted from the wrong end", CONTROLLER, "int(window * settings.chatgpt_compact_at / 100)", "int(window * (100 - settings.chatgpt_compact_at) / 100)"),
    ("controller: the percent is taken of a different number", CONTROLLER, "int(window * settings.chatgpt_compact_at / 100)", "int(window * settings.chatgpt_compact_at / 1000)"),
    ("controller: a refusal aims at the window it was refused at", CONTROLLER, "target = min(window, int(estimate * 0.7)) if window and overflow else (window or int(estimate * 0.7))", "target = window or int(estimate * 0.7)"),
    ("controller: the summary uses the user's own reasoning", CONTROLLER, "        return \"low\" if \"low\" in efforts else (efforts[0] if efforts else None)", "        return self.reasoning or None"),
    ("controller: the page is not told it is compacting", CONTROLLER, "        self.transcript.set_working_state(\"compacting_conversation\")\n        self._changed()", "        self._changed()"),
    ("controller: the summary request tells how full the conversation is", CONTROLLER, "        if not measures_context:  # (a summary request", "        if False:  # (a summary request"),
    ("controller: the place in the log forgets the earlier summary", CONTROLLER, "        start = int(record.get(\"upto\") or 0) if record else 0\n", "        start = 0\n"),
    ("controller: the compaction is not saved", CONTROLLER, "        self.saved.compaction = entry\n", "        self.saved.compaction = self.saved.compaction or entry\n"),
    ("controller: the view is not replaced", CONTROLLER, "        history[:] = result.view\n", ""),
    ("controller: the size stays known after a compaction", CONTROLLER, "        self._publish_context(replace(self.context, used=None, compacted=True))\n", ""),
    ("controller: the user is not told", CONTROLLER, "        self.transcript.add_notice(self._compaction_notice(result, model, window, overflow, manual))\n", ""),
    ("controller: the loop reads the log, not the view", CONTROLLER, "end = await loop.run(compact.view_of(self.saved.items, self.saved.compaction))", "end = await loop.run(self.saved.items)"),
    ("controller: a snapshot is not paired", CONTROLLER, "        return paired(saved)", "        return saved"),
    # ---- the application without Factory
    ("standalone: no model of another engine is the default", APP, "            if first is None and not any(m[\"source\"] == \"factory\" for m in self.catalog.models):", "            if False:"),
    ("standalone: hiding Factory's models falls back to another engine", APP, "            if first is None and not any(m[\"source\"] == \"factory\" for m in self.catalog.models):", "            if first is None:"),
    ("standalone: a Factory that is not there is a failure", APP, "                if not (isinstance(error, DroidNotFoundError) and not named):", "                if True:"),
    ("standalone: a path that was named does not count", APP, "named = bool(self.settings.droid_path.strip() or os.environ.get(ENV_OVERRIDE, \"\").strip())", "named = False"),
    ("standalone: the variable that names droid does not count", APP, "or os.environ.get(ENV_OVERRIDE, \"\").strip())", ")"),
    ("standalone: every failure of Factory is silent", APP, "                if not (isinstance(error, DroidNotFoundError) and not named):", "                if False:"),
    # ---- the user's threshold
    ("settings: the default percent is another", SETTINGS, "DEFAULT_COMPACT_AT = 85", "DEFAULT_COMPACT_AT = 90"),
    ("settings: the range reaches 100", SETTINGS, "COMPACT_AT_RANGE = (50, 95)", "COMPACT_AT_RANGE = (50, 100)"),
    ("settings: the range reaches 0", SETTINGS, "COMPACT_AT_RANGE = (50, 95)", "COMPACT_AT_RANGE = (0, 95)"),
    ("settings: a percent under the range is kept", SETTINGS, "or not low_at <= compact_at <= high_at:", "or not compact_at <= high_at:"),
    ("settings: a percent over the range is kept", SETTINGS, "or not low_at <= compact_at <= high_at:", "or not low_at <= compact_at:"),
    ("settings: a switch that is not a switch is taken", SETTINGS, "    if not isinstance(chatgpt_compact, bool):", "    if False:"),
    ("settings: the switch is not read", SETTINGS, "        chatgpt_compact=chatgpt_compact,\n", "        chatgpt_compact=defaults.chatgpt_compact,\n"),
    ("settings: the percent is not read", SETTINGS, "        chatgpt_compact_at=compact_at,\n", "        chatgpt_compact_at=defaults.chatgpt_compact_at,\n"),
    ("bridge: the page is not told the switch", APP, "\"compactAuto\": self.settings.chatgpt_compact,", "\"compactAuto\": True,"),
    ("bridge: the page is not told the percent", APP, "\"compactAt\": self.settings.chatgpt_compact_at,", "\"compactAt\": 85,"),
    ("bridge: the switch takes anything", APP, "elif key == \"compactAuto\" and isinstance(value, bool):", "elif key == \"compactAuto\":"),
    ("bridge: the switch is not stored", APP, "            s.chatgpt_compact = value\n", "            pass\n"),
    ("bridge: the percent is not stored", APP, "            s.chatgpt_compact_at = value\n", "            pass\n"),
    ("bridge: the percent takes any whole number", APP, "elif key == \"compactAt\" and _is_int(value) and COMPACT_AT_RANGE[0] <= value <= COMPACT_AT_RANGE[1]:", "elif key == \"compactAt\" and _is_int(value):"),
    ("bridge: the percent has no lower bound", APP, "_is_int(value) and COMPACT_AT_RANGE[0] <= value <= COMPACT_AT_RANGE[1]:", "_is_int(value) and value <= COMPACT_AT_RANGE[1]:"),
    ("bridge: the percent has no upper bound", APP, "_is_int(value) and COMPACT_AT_RANGE[0] <= value <= COMPACT_AT_RANGE[1]:", "_is_int(value) and COMPACT_AT_RANGE[0] <= value:"),
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
