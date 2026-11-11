"""Mutation checks for G3 G4 G5 of the harness review of 2026-11-08 (``docs/harness-review-2026-11-08.md``): the room a
compaction must make, the user's words it must keep, and the place in the log it must not get wrong. Each mutant undoes
one part of the fix; the regression tests (``tests/test_harness_g3_g5_compaction.py`` first) must catch it.

    python sandbox/mutate_harness_g345.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_harness_g345.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_harness_g345.py 3 7        run just those mutants

This working tree is never written to (other work may be going on in it): ``desktop``, ``tests`` and ``fixtures`` go to a
temporary folder, each mutant is applied there and undone byte for byte, the copy is removed at the end, and the files
the mutants are about are compared with their hashes from before the run.

A mutant is run against the new tests alone first, so that what is printed says which of them caught it; only one they
let through is run against the older compaction tests too.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv").exists() else sys.executable
H = Path("desktop") / "core" / "harness"
COMPACT, LOOP, STORE = H / "compact.py", H / "loop.py", H / "store.py"
CONTROLLER = Path("desktop") / "core" / "chatgpt" / "controller.py"
NEW = ["tests/test_harness_g3_g5_compaction.py"]
OLDER = ["tests/test_harness_compact.py", "tests/test_chatgpt_compaction.py", "tests/test_harness_store.py"]

M = [
    # ---- G3: a compaction says it worked only when it did
    ("G3 compact: success is reported whatever came of it", COMPACT, "words, after < before and after <= room)", "words, True)"),
    ("G3 compact: a summary that made nothing smaller is kept", COMPACT, "words, after < before and after <= room)", "words, after <= room)"),
    ("G3 compact: the whole request is not held to the window", COMPACT, "words, after < before and after <= room)", "words, after < before)"),
    ("G3 compact: the instructions and the tools are not counted", COMPACT, "    room = budget(window) - fixed\n", "    room = budget(window)\n"),
    ("G3 compact: the answer's share of the window is not left", COMPACT, "    return window - reserve(window)\n", "    return window\n"),
    # ---- G3: a step too big to keep is summarised whole, the user's words going with it
    ("G3 cut: a step too big to keep is never summarised whole", COMPACT, "    end = len(view) if room is not None and ends_whole(view, synthetic) else None\n", "    end = None\n"),
    ("G3 cut: the whole view is summarised though the last cut would fit", COMPACT, "    if end is not None and rest[points[-1]] > room:\n", "    if end is not None:\n"),
    ("G3 cut: a view that ends with the user's message is summarised whole", COMPACT, "    return not is_user(last) and last.get(\"type\") != \"reasoning\" and boundary_ok(view, len(view))", "    return last.get(\"type\") != \"reasoning\" and boundary_ok(view, len(view))"),
    ("G3 compact: the user's words do not go with the summary", COMPACT, "    words = _joined(carried, *(_said(item) for item in view[max(turn, synthetic) : cut] if is_user(item)))\n", "    words = \"\"\n"),
    ("G3 compact: the words kept with a summary are dropped though their turn goes on", COMPACT, "    carried = kept if turn < synthetic else \"\"\n", "    carried = \"\"\n"),
    ("G3 compact: the words of an earlier turn are not given to the summary model", COMPACT, "    told = _joined(previous, \"\" if carried or not kept.strip() else f\"{KEPT_HEAD}\\n\\n{kept}\")\n", "    told = _joined(previous)\n"),
    ("G3 compact: the running turn is taken to begin at the first message", COMPACT, "        turn = len(view) if whole else next((index for index in range(len(view) - 1, synthetic - 1, -1) if is_user(view[index])), len(view))\n", "        turn = synthetic\n"),
    # ---- G3: the results of a step share the room the request has
    ("G3 loop: the results of a step are not shared out", LOOP, "        room = self.observer.tool_room(self.estimate(history), self._answer)\n", "        room = None\n"),
    ("G3 loop: every result takes its share of the whole room", LOOP, "                    room = max(0, room - estimate_item(item))\n", "                    pass\n"),
    ("G3 loop: a result gets the floor whatever the room", LOOP, "max(OUTPUT_FLOOR, room // (len(calls) - index))", "OUTPUT_FLOOR"),
    ("G3 loop: a result keeps twice its share", LOOP, "        if estimate_text(kept(middle)) <= tokens:\n", "        if estimate_text(kept(middle)) <= tokens * 2:\n"),
    ("G3 loop: the observer is not told the answer that made the calls", LOOP, "        self._answer = estimate_items(kept)\n", "        self._answer = 0\n"),
    ("G3 controller: the results get no room below the point of compacting", CONTROLLER, "        return max(0, self._compact_at(window) - 1 - estimate, compact.keep_tokens(window) - step)\n", "        return max(0, compact.keep_tokens(window) - step)\n"),
    ("G3 controller: the results may fill the window though a summary comes first", CONTROLLER, "        return max(0, self._compact_at(window) - 1 - estimate, compact.keep_tokens(window) - step)\n", "        return max(0, compact.budget(window) - estimate)\n"),
    ("G3 controller: with compaction off the results are not held to the window", CONTROLLER, "            return max(0, compact.budget(window) - estimate)\n", "            return None\n"),
    # ---- G3: what cannot be made to fit ends the turn, with the reason
    ("G3 controller: a request bigger than the window is sent as it is", CONTROLLER, "            over = window is not None and estimate > compact.budget(window)\n", "            over = False\n"),
    ("G3 controller: what cannot be made to fit is sent anyway", CONTROLLER, "            if must_fit:\n                raise self._too_big(", "            if False:\n                raise self._too_big("),
    ("G3 controller: the instructions and the tools are left out", CONTROLLER, "history, estimate, overflow, fixed, must_fit=overflow or over)", "history, estimate, overflow, 0, must_fit=overflow or over)"),
    ("G3 controller: a refusal nothing can help with ends as the server's error", CONTROLLER, "must_fit=overflow or over)", "must_fit=over)"),
    ("G3 controller: the turn's instructions are not counted", CONTROLLER, "_Watch(self, tools, turn, instructions=instructions),", "_Watch(self, tools, turn),"),
    ("G3 controller: the place of the running turn in the view is not given", CONTROLLER, "                turn=len(history) if manual else synthetic + self._turn_start - start,\n", "                turn=None,\n"),
    ("G3 controller: a compaction that could not help is tried again at once", CONTROLLER, "        self._compacted_to = compact.estimate_items(history) + fixed if done else estimate\n", "        self._compacted_to = compact.estimate_items(history) + fixed if done else 0\n"),
    ("G3 controller: the user is not told that the whole step was summarised", CONTROLLER, "        if len(result.view) == 1:\n", "        if False:\n"),
    # ---- G4: the user's words reach the summary model whole
    ("G4 render: the user's words are cut in the middle again", COMPACT, "            return f\"USER: {text}\"\n", "            return f\"USER: {_clip(text, TEXT_LIMIT)}\"\n"),
    ("G4 parts: a message too big for one request is not given in pieces", COMPACT, "        if is_user(item) and estimate_text(text) > budget:\n", "        if False:\n"),
    ("G4 pieces: the end of each piece is left out", COMPACT, "        out.append(text[start:end])\n", "        out.append(text[start:end - 1])\n"),
    ("G4 pieces: no room is made for the label", COMPACT, "            split = pieces(_said(item), budget - PIECE_LABEL)\n", "            split = pieces(_said(item), budget)\n"),
    # ---- G5: which items are the view's own is told, never read from a message
    ("G5 controller: whether the view begins with a summary is read from its text", CONTROLLER, "        synthetic = compact.synthetic_count(record)\n", "        synthetic = 1 if history and compact.is_summary(history[0]) else 0\n"),
    ("G5 log_index: the view's own items are not counted", COMPACT, "    return upto + cut - synthetic\n", "    return upto + cut\n"),
    ("G5 compact: the summary of ours is summarised as a message of the user", COMPACT, "    covered = view[synthetic:cut]\n", "    covered = view[:cut]\n"),
    ("G5 cut: the summary of ours can be cut off alone", COMPACT, "    points = [point for point in cut_points(view) if point > synthetic]\n", "    points = [point for point in cut_points(view) if point > 0]\n"),
    ("G5 controller: the earlier summary is read back from the view", CONTROLLER, "                previous=str(record.get(\"summary\") or \"\") if record else \"\",\n", "                previous=None,\n"),
    ("G5 controller: a summary is kept though its place would split a step", CONTROLLER, "            if not compact.boundary_ok(self.saved.items, upto) or compact.view_of(self.saved.items, entry) != result.view:\n", "            if False:\n"),
    ("G5 store: a file whose summary stops inside a step is not mended", STORE, "        mended = mend_boundary(session.items, upto)\n", "        mended = upto\n"),
    ("G5 boundary: a step may be split", COMPACT, "    return not pending and items[upto - 1].get(\"type\") != \"reasoning\"\n", "    return items[upto - 1].get(\"type\") != \"reasoning\"\n"),
    ("G5 boundary: a reasoning item may be parted from what it led to", COMPACT, "    return not pending and items[upto - 1].get(\"type\") != \"reasoning\"\n", "    return not pending\n"),
    ("G5 mend: the place is not moved", COMPACT, "    while upto < len(items) and not boundary_ok(items, upto):\n        upto += 1\n", ""),
]


def _plain(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _found(root: Path, path: Path, old: str) -> int:
    return _plain((root / path).read_bytes())[0].count(old)


def _digest(root: Path) -> dict[str, str]:
    return {str(path): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sorted({path for _, path, _, _ in M})}


def _pytest(copy: Path, tests: list[str]) -> tuple[bool, list[str], str]:
    done = subprocess.run(
        [PY, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", *tests], cwd=copy, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    failed = re.findall(r"^FAILED (\S+)", done.stdout, re.MULTILINE)
    tail = (done.stdout.strip().splitlines() or ["?"])[-1]
    return done.returncode != 0, failed, tail


def main() -> int:
    args = sys.argv[1:]
    only = {int(arg) for arg in args if arg.isdigit()}
    chosen = [(number, mutant) for number, mutant in enumerate(M, 1) if not only or number in only]
    missing = [(number, name, count) for number, (name, path, old, _) in chosen if (count := _found(ROOT, path, old)) != 1]
    for number, name, count in missing:
        print(f"[{number}] {name}: PATTERN FOUND {count} TIMES", flush=True)
    if "--check" in args:
        print(f"{len(chosen) - len(missing)} of {len(chosen)} patterns found once")
        return 1 if missing else 0
    if missing:
        return 1
    before = _digest(ROOT)
    copy = Path(tempfile.mkdtemp(prefix="wittycode-g345-"))
    survivors: list[str] = []
    try:
        for folder in ("desktop", "tests", "fixtures"):
            if (ROOT / folder).is_dir():
                shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "pyproject.toml", copy / "pyproject.toml")
        broken, failed, tail = _pytest(copy, NEW + OLDER)
        if broken:
            print(f"the copy does not pass its tests as it is: {tail}\n" + "\n".join(failed))
            return 1
        print(f"copy at {copy}: {tail}", flush=True)
        for number, (name, path, old, new) in chosen:
            target = copy / path
            original = target.read_bytes()
            text, crlf = _plain(original)
            mutated = text.replace(old, new)
            target.write_bytes((mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))
            started = time.time()
            try:
                caught, failed, tail = _pytest(copy, NEW)
                by = "new tests"
                if not caught:
                    caught, failed, tail = _pytest(copy, OLDER)
                    by = "older tests"
            except subprocess.TimeoutExpired:
                caught, failed, tail, by = True, [], "timeout (hangs: caught)", ""
            finally:
                target.write_bytes(original)
            names = ", ".join(name.split("::")[-1] for name in failed) or tail
            print(f"[{number}] {'caught  ' if caught else 'SURVIVED'} {name}  ({time.time() - started:.0f}s) {by}: {names}", flush=True)
            if not caught:
                survivors.append(name)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    after = _digest(ROOT)
    changed = [path for path in before if before[path] != after.get(path)]
    print(f"\n{len(chosen) - len(survivors)} of {len(chosen)} caught")
    for name in survivors:
        print("  survivor:", name)
    print("working tree: " + ("unchanged (" + ", ".join(before) + ")" if not changed else "CHANGED: " + ", ".join(changed)))
    print("copy removed" if not copy.exists() else f"copy NOT removed: {copy}")
    return 1 if survivors or changed else 0


if __name__ == "__main__":
    sys.exit(main())
