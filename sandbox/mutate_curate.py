"""Mutation checks for what the application writes to the memory itself: the note a message asks for (capture, the prompt
hook), the suggestions after a turn (curator, proposals, the bridge's accept) and their wiring in AppCore.

    python sandbox/mutate_curate.py            run every mutant
    python sandbox/mutate_curate.py --check    only check that every pattern is found exactly once
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
MEM = ROOT / "desktop" / "core" / "memory"
CAPTURE, CURATOR, PROPOSALS, BRIDGE, LAUNCH = (MEM / name for name in ("capture.py", "curator.py", "proposals.py", "bridge.py", "launch.py"))
APP = ROOT / "desktop" / "core" / "app_core.py"
SIDE = ROOT / "desktop" / "core" / "chatgpt" / "side.py"
TESTS = ["tests/test_memory_capture.py", "tests/test_memory_curator.py", "tests/test_app_memory_curate.py", "tests/test_app_memory.py"]
E2E = ["tests/test_app_e2e.py", "-k", "suggests_keeping"]

M = [
    # ---- capture
    ("capture: a question is taken as a request", CAPTURE, "        if _QUESTION.search(sentence):\n            continue\n", ""),
    ("capture: 以后再 is a standing instruction", CAPTURE, "以后(?!再)", "以后"),
    ("capture: a hedge does not stop a standing instruction", CAPTURE, "elif not _HEDGE.search(sentence) and (", "elif ("),
    ("capture: never mind is a rule", CAPTURE, "(?!mind\\b)", ""),
    ("capture: the standing words may be anywhere in the sentence", CAPTURE, "r\"(?:^|[\\s，,：:])(?:\"\n", "r\"(?:\"\n"),
    ("capture: 每次都 is an instruction", CAPTURE, "|(?:永远|每次|任何时候|无论何时)[^，,。！!？?；;\\n]{0,3}?(?:一律|", "|(?:永远|每次|任何时候|无论何时)[^，,。！!？?；;\\n]{0,3}?(?:都|一律|"),
    ("capture: a body shorter than four characters is a note", CAPTURE, "MIN_BODY = 4", "MIN_BODY = 0"),
    ("capture: the note after the request is not looked for", CAPTURE, "            body = _clean(sentence[asked.end() :])\n", "            body = ''\n"),
    ("capture: the words before the request are not tried", CAPTURE, "                body = _clean(sentence[: asked.start()])\n", "                pass\n"),
    ("capture: the next sentence is not tried", CAPTURE, "            if len(body) < MIN_BODY and index + 1 < len(sentences):", "            if False:"),
    ("capture: a long note is not cut", CAPTURE, "        body = _cap(body)\n", ""),
    ("capture: everything is global", CAPTURE, 'scope = "global" if _GLOBAL.search(body) or not has_project else "project"', 'scope = "global"'),
    ("capture: no project is still a project note", CAPTURE, 'scope = "global" if _GLOBAL.search(body) or not has_project else "project"', 'scope = "global" if _GLOBAL.search(body) else "project"'),
    ("capture: nothing is a preference", CAPTURE, 'kind = "preference" if _PREFERENCE.search(body) else', "kind = "),
    ("capture: nothing is a convention", CAPTURE, 'else "convention" if _RULE.search(body) else "fact"', 'else "fact"'),
    ("capture: the title is the whole note", CAPTURE, "    first = _CLAUSE_END.split(body, maxsplit=1)[0].strip()\n", "    first = body\n"),
    ("capture: a long title is cut without a mark", CAPTURE, 'return cut.rstrip("，,、 ") + "…"', 'return cut.rstrip("，,、 ")'),
    # ---- the hook
    ("hook: a secret is written", LAUNCH, '            if rejected.code == "secret":\n                return "这条里有像密钥的内容，没有记进共享记忆。", ""\n', ""),
    ("hook: a full memory is not said", LAUNCH, '            if rejected.code == "limit":\n                return "共享记忆已满，这条没有记下；在设置 › 共享记忆 里忘掉一些再试。", ""\n', ""),
    ("hook: the note is written as an agent", LAUNCH, 'kind=found.kind, tags=found.tags, by="user",', 'kind=found.kind, tags=found.tags, by={"engine": event.engine, "run": ""},'),
    ("hook: the switch is not looked at", LAUNCH, 'said, made = self._remember(event) if self.capture() else ("", "")', 'said, made = self._remember(event)'),
    ("hook: the note just written is offered back", LAUNCH, " if n.id != made]", "]"),
    ("hook: a project note goes without its project", LAUNCH, 'project=str(event.project) if found.scope == "project" else "",', 'project="",'),
    ("hook: the note is not announced", LAUNCH, 'return PromptContext("", said, frozenset()) if said else None', "return None"),
    # ---- curator
    ("curator: a short user message is asked about", CURATOR, "MIN_USER = 8", "MIN_USER = 0"),
    ("curator: a short answer is asked about", CURATOR, "MIN_ASSISTANT = 40", "MIN_ASSISTANT = 0"),
    ("curator: a conversation may be asked again at once", CURATOR, "return last is None or now - last >= GAP_SECONDS", "return True"),
    ("curator: any number of requests an hour", CURATOR, "if self._busy or len(self._asked) >= PER_HOUR:", "if self._busy:"),
    ("curator: requests may overlap", CURATOR, "if self._busy or len(self._asked) >= PER_HOUR:", "if len(self._asked) >= PER_HOUR:"),
    ("curator: busy is never cleared", CURATOR, "        finally:\n            self._busy = False\n", "        finally:\n            pass\n"),
    ("curator: a secret is suggested", CURATOR, " or find_secret(title, text):", ":"),
    ("curator: what the memory has is suggested again", CURATOR, "            if any(c.text.strip() == text or _same_subject(wanted, _normal_title(c.title)) for c in existing):\n                continue\n", ""),
    ("curator: the same words under another title are suggested", CURATOR, "any(c.text.strip() == text or _same_subject", "any(_same_subject"),
    ("curator: no cap on the notes of one answer", CURATOR, "            if len(out) >= MAX_NOTES:\n                break\n", ""),
    ("curator: an unknown kind is kept", CURATOR, 'kind = kind if kind in KINDS else "fact"', ""),
    ("curator: a project note without a project", CURATOR, '            if scope == "project" and not exchange.project:\n                scope = "global"\n', ""),
    ("curator: the user is not told", CURATOR, "        if kept:\n            self.changed()\n", ""),
    ("curator: a failure is raised", CURATOR, "        except Exception:\n            log.warning(\"the memory curator could not ask about a turn\", exc_info=True)\n            return []\n", "        except ZeroDivisionError:\n            return []\n"),
    ("curator: a cancel is swallowed", CURATOR, "        except asyncio.CancelledError:\n            raise\n        except Exception:", "        except Exception:"),
    ("curator: the titles it knows are not given", CURATOR, "    titles = [t for t in known if t.strip()][:KNOWN_TITLES]", "    titles = []"),
    ("curator: a fenced reply is not read", CURATOR, "    if fenced:\n        text = fenced.group(1).strip()\n", ""),
    # ---- proposals
    ("proposals: a dismissed one comes back", PROPOSALS, "known = {p.digest for p in pending} | set(dismissed)", "known = {p.digest for p in pending}"),
    ("proposals: a waiting one is kept twice", PROPOSALS, "known = {p.digest for p in pending} | set(dismissed)", "known = set(dismissed)"),
    ("proposals: no limit on the waiting ones", PROPOSALS, "pending = (pending + kept)[-MAX_PENDING:] if len(pending) + len(kept) > MAX_PENDING else pending + kept", "pending = pending + kept"),
    ("proposals: the oldest are kept, the newest go", PROPOSALS, "(pending + kept)[-MAX_PENDING:]", "(pending + kept)[:MAX_PENDING]"),
    ("proposals: dismissing forgets the words", PROPOSALS, "[*[d for d in dismissed if d != found.digest], found.digest])", "dismissed)"),
    ("proposals: dismissing all forgets the words", PROPOSALS, "self._write([], [*dismissed, *(p.digest for p in pending)])", "self._write([], dismissed)"),
    ("proposals: the user may change who it came from", PROPOSALS, 'if key in ("title", "text", "scope", "kind", "project", "tags") and value is not None', "if value is not None"),
    ("proposals: newest last", PROPOSALS, "return sorted(self._read()[0], key=lambda p: p.at, reverse=True)", "return sorted(self._read()[0], key=lambda p: p.at)"),
    ("proposals: a damaged entry is kept", PROPOSALS, "if not title.strip() or not text.strip() or scope not in SCOPES or kind not in KINDS:\n                return None", "if False:\n                return None"),
    # ---- accept
    ("accept: the suggestion stays after it became a note", BRIDGE, "        self.proposals.settle(proposal_id)\n        if news_before == 0:", "        if news_before == 0:"),
    ("accept: a duplicate leaves the suggestion waiting", BRIDGE, "            if rejected.code == \"duplicate\":\n                self.proposals.settle(proposal_id)", "            if rejected.code == \"duplicate\":\n                pass"),
    ("accept: the note is written as the user", BRIDGE, "by=agent(edited.engine),\n            )", 'by="user",\n            )'),
    ("accept: the news already waiting is marked as looked at", BRIDGE, "        if news_before == 0:\n            self.mark_seen()", "        if True:\n            self.mark_seen()"),
    ("accept: the new note is news", BRIDGE, "        if news_before == 0:\n            self.mark_seen()", "        if False:\n            self.mark_seen()"),
    ("accept: a global note keeps a project", BRIDGE, '        if edited is not None and edited.scope == "global":\n            edited = replace(edited, project="")\n', ""),
    ("state: the waiting ones are not counted", BRIDGE, '            "pending": self.proposals.count(),', '            "pending": 0,'),
    # ---- AppCore
    ("app: a turn that failed is asked about", APP, "        if turn.status is not TurnStatus.DONE or turn.id in conv.curated:\n            return", "        if turn.id in conv.curated:\n            return"),
    ("app: a turn is asked about again and again", APP, "        conv.curated.add(turn.id)\n", ""),
    ("app: the curator is asked at every state change", APP, "            if conv.ran:\n                self._curate(conv)\n", "            self._curate(conv)\n"),
    ("app: Droid is asked through ChatGPT", APP, '        if engine == "chatgpt" and self.chatgpt is not None:', "        if self.chatgpt is not None:"),
    ("app: the switch is not looked at", APP, "                    and self.settings.memory_curate\n                    and engine in self.settings.memory_engines\n", "                    and engine in self.settings.memory_engines\n"),
    ("app: the memory's master switch is not looked at", APP, "                    self.settings.memory_enabled\n                    and self.settings.memory_curate\n", "                    self.settings.memory_curate\n"),
    ("app: the conversation is not named", APP, "            conversation=controller.title or \"\",\n", "            conversation=\"\",\n"),
    # ---- the ChatGPT side request
    ("side: the heavy model is asked", SIDE, 'PREFERRED_MODEL = "gpt-6-luna"', 'PREFERRED_MODEL = "gpt-6.1-sol"'),
    ("side: a signed-out account is asked", SIDE, '    if account.get("auth") != "signed-in" or not account.get("planEnabled"):\n        return None\n', ""),
    ("side: the answer is the first piece only", SIDE, "                    text = event.text or text\n", "                    pass\n"),
]

args = sys.argv[1:]
check_only = "--check" in args
survivors = []
for number, (name, path, old, new) in enumerate(M, 1):
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
        done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if done.returncode == 0:
            done = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *E2E], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
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
