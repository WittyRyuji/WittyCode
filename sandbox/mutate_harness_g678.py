"""Mutation checks for G6, G7 and G8 of the harness review of 2026-11-08 (``docs/harness-review-2026-11-08.md``) and for the
``ask_user`` tool: the model a turn is measured by, the size one turn tells the next, the cache that stands in for what is not
measured yet, and the question the model puts to the user. Each mutant undoes one part; the regression tests must catch it.

    python sandbox/mutate_harness_g678.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_harness_g678.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_harness_g678.py 3 7        run just those mutants

This working tree is never written to (other work may be going on in it): ``desktop``, ``tests`` and ``fixtures`` go to a
temporary folder, each mutant is applied there and undone byte for byte, the copy is removed at the end, and the files
the mutants are about are compared with their hashes from before the run.
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
LOOP, TOOLSET, ASKING, PROMPT = H / "loop.py", H / "toolset.py", H / "asking.py", H / "prompt.py"
APP = Path("desktop") / "core" / "app_core.py"
CONTROLLER = Path("desktop") / "core" / "chatgpt" / "controller.py"
NEW = ["tests/test_harness_g6_g7.py", "tests/test_app_context_cache.py", "tests/test_harness_asking.py"]
OLDER = ["tests/test_chatgpt_controller.py", "tests/test_app_chatgpt.py", "tests/test_chatgpt_compaction.py"]

M = [
    # ---- G6: the model a turn started with is the one it is measured by, the next turn's is the one chosen meanwhile
    ("G6 controller: the turn's model is not fixed when it starts", CONTROLLER, "        self.state = SessionState.RUNNING\n        self._turn_model, self._turn_reasoning, self._turn_mode = self.model, self.reasoning, self.mode\n        context = self.manager.hooks.prompt(", "        self.state = SessionState.RUNNING\n        context = self.manager.hooks.prompt("),
    ("G6 controller: the running model is always the chosen one", CONTROLLER, "        return self._turn_model or self.model\n", "        return self.model\n"),
    ("G6 controller: the turn's model is kept after the turn", CONTROLLER, "        self._turn_model = self._turn_reasoning = self._turn_mode = \"\"\n", "        pass\n"),
    ("G6 controller: the page is not given the new window when the turn ends", CONTROLLER, "        self.refresh_context()  # (a model chosen during the turn now gives its window)\n", "        pass\n"),
    ("G6 controller: the tool results are measured by the chosen model", CONTROLLER, "        window = self.ctl.manager.model_window(self.ctl.running_model)\n        if window is None:\n            return None", "        window = self.ctl.manager.model_window(self.ctl.model)\n        if window is None:\n            return None"),
    ("G6 controller: the compaction point is measured by the chosen model", CONTROLLER, "        window = self.ctl.manager.model_window(self.ctl.running_model)\n        over = False", "        window = self.ctl.manager.model_window(self.ctl.model)\n        over = False"),
    ("G6 controller: the summary is asked of the chosen model", CONTROLLER, "        model = self.running_model\n        window = self.manager.model_window(model)\n        # (refused although", "        model = self.model\n        window = self.manager.model_window(model)\n        # (refused although"),
    ("G6 controller: the summary's level is the chosen model's", CONTROLLER, "        info = self.manager.model_info(self.running_model)\n        efforts", "        info = self.manager.model_info(self.model)\n        efforts"),
    ("G6 controller: the usage is measured by the chosen model", CONTROLLER, "        model = self.running_model\n        window = self.manager.model_window(model) or self.context.window", "        model = self.model\n        window = self.manager.model_window(model) or self.context.window"),
    ("G6 controller: the page shows the chosen model's window at once", CONTROLLER, "    def refresh_context(self) -> None:\n        model = self.running_model\n", "    def refresh_context(self) -> None:\n        model = self.model\n"),
    ("G6 controller: the level of the turn is the chosen one", CONTROLLER, "                reasoning=reasoning or None,\n", "                reasoning=self.reasoning or None,\n"),
    ("G6 controller: nothing says the change waits for the next message", CONTROLLER, "        self._explain_pending()\n        self._changed()\n", "        self._changed()\n"),
    # ---- G7: what one turn measured starts the next turn's estimate
    ("G7 controller: the exact figure is never carried", CONTROLLER, "        if kept is not None and kept[0] == model and kept[1] == self._record_marker():\n", "        if False:\n"),
    ("G7 controller: the exact figure of another model is used", CONTROLLER, "        if kept is not None and kept[0] == model and kept[1] == self._record_marker():\n", "        if kept is not None and kept[1] == self._record_marker():\n"),
    ("G7 controller: the exact figure of another view is used", CONTROLLER, "        if kept is not None and kept[0] == model and kept[1] == self._record_marker():\n", "        if kept is not None and kept[0] == model:\n"),
    ("G7 controller: there is no floor", CONTROLLER, "        if used and not self.context.compacted:\n", "        if False:\n"),
    ("G7 controller: a floor from before a compaction is kept", CONTROLLER, "        if used and not self.context.compacted:\n", "        if used:\n"),
    ("G7 controller: the end of a turn keeps no figure", CONTROLLER, "                self._keep_measure(loop, model)\n", "                pass\n"),
    ("G7 controller: the figure is kept without its compaction record", CONTROLLER, "(model, self._record_marker(), measured[0], measured[1])", "(model, (), measured[0], measured[1])"),
    ("G7 loop: the carried figure is not used", LOOP, "        self._measured: tuple[int, int] | None = measured\n", "        self._measured: tuple[int, int] | None = None\n"),
    ("G7 loop: the floor is not used", LOOP, "fixed_tokens(self.instructions, self.tools.declarations(), self.shape), self._floor)", "fixed_tokens(self.instructions, self.tools.declarations(), self.shape), 0)"),
    ("G7 loop: the floor outlives a compaction", LOOP, "            self._measured = None\n            self._floor = 0\n", "            self._measured = None\n"),
    # ---- G8: the cache stands in only for what has not been measured, and never against the session's word
    ("G8 app: the cache overrides a compaction", APP, "if context.get(\"used\") is None and not context.get(\"compacted\") and cached.get", "if context.get(\"used\") is None and cached.get"),
    ("G8 app: the cache is used for any model or window", APP, " and self._cache_fits(context, cached[\"context\"]):", ":"),
    ("G8 app: the cache is used for another model", APP, "        for key in (\"model\", \"window\"):\n", "        for key in (\"window\",):\n"),
    ("G8 app: the cache is used for another window", APP, "        for key in (\"model\", \"window\"):\n", "        for key in (\"model\",):\n"),
    # ---- ask_user: the model can put a question to the user, and the answer comes back
    ("ask: the tool is not offered", TOOLSET, "        if self.asker is not None:\n            declared.append(dict(asking.DECLARATION))\n", "        if False:\n            declared.append(dict(asking.DECLARATION))\n"),
    ("ask: the tool is not known to the loop", TOOLSET, " or (name == asking.NAME and self.asker is not None)", ""),
    ("ask: the question needs an approval of its own", TOOLSET, "        if call.name == asking.NAME:\n            return Verdict(ALLOW)", "        if False:\n            return Verdict(ALLOW)"),
    ("ask: a skipped question is an empty result", TOOLSET, "                return Outcome(asking.SKIPPED, cancelled=True)\n", "                return Outcome(\"\")\n"),
    ("ask: a broken call is shown as a card", TOOLSET, "            if problem:\n                return Outcome(problem, True)\n", "            if False:\n                return Outcome(problem, True)\n"),
    ("ask: the page is shown the raw tool", TOOLSET, "        return \"AskUser\", {\"questionnaire\": asking.questionnaire(args)}", "        return asking.NAME, dict(args)"),
    ("ask: an option said twice is offered twice", ASKING, "            if label and label not in options:\n", "            if label:\n"),
    ("ask: any number of questions are asked", ASKING, "enumerate(raw[:MAX_QUESTIONS], 1)", "enumerate(raw, 1)"),
    ("ask: a header of any length", ASKING, "[:HEADER_LIMIT]", ""),
    ("ask: the answers are not given back to the model", ASKING, "        lines.append(f\"   Answer: {answer_text(answer).strip() or '(no answer)'}\")\n", "        pass\n"),
    ("ask: the answers of the card never reach the tool", CONTROLLER, "            future.set_result(None if answers is None else [answer if isinstance(answer, str) else list(answer) for answer in answers])\n", "            future.set_result(None)\n"),
    ("ask: a card is answered twice [EQUIVALENT: transcript.answer_question refuses any prompt that is not pending, so the _pending lookup only saves making a future that nothing waits for]", CONTROLLER, "        future = self._pending.get(prompt_id)\n        if future is None:\n            return False\n        texts = None if answers is None else [asking.answer_text", "        future = self._pending.get(prompt_id) or asyncio.get_running_loop().create_future()\n        texts = None if answers is None else [asking.answer_text"),
    ("ask: nobody is shown the card", CONTROLLER, "        prompt = self.transcript.add_question_prompt(call.call_id, views)\n        self._changed()\n        if prompt is None or prompt.status is not PromptStatus.PENDING:\n            return None\n        return await self._wait(prompt.id)", "        return None"),
    ("ask: the prompt does not tell the model about the tool", PROMPT, "call ask_user with short options and wait for the answer: do not guess, and do not end the turn with a question in plain text.", "ask the user."),
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
    copy = Path(tempfile.mkdtemp(prefix="wittycode-g678-"))
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
