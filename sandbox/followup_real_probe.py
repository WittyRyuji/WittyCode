"""The real engines through the real AppCore: a queued message, a steer with an attachment, and the conversation's token
total across a close and a reopen. Usage: followup_real_probe.py droid | codex  (a few cheap turns of real model use)."""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desktop.core.app_core import AppCore  # noqa: E402
from desktop.core.cache import Cache  # noqa: E402
from desktop.core.codex.engine import CodexEngine  # noqa: E402
from desktop.core.session_manager import SessionManager, SessionState  # noqa: E402
from desktop.core.settings import AppSettings  # noqa: E402
from desktop.core.transcript import UserBlock  # noqa: E402
from tests.app_support import Events, FakePlatform  # noqa: E402

START = time.time()
WHICH = sys.argv[1]
CWD = ROOT / "sandbox" / ("phase1/demo" if WHICH == "droid" else "codex-live-cwd")
CACHE = Path(tempfile.mkdtemp(prefix="followup-cache-"))


def say(*parts) -> None:
    print(f"{time.time() - START:6.1f}", *parts, flush=True)


async def wait(predicate, timeout=240.0, what="") -> None:
    end = time.time() + timeout
    while not predicate():
        if time.time() > end:
            raise TimeoutError(what or "condition")
        await asyncio.sleep(0.1)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def show(controller) -> None:
    for number, turn in enumerate(controller.transcript.turns, 1):
        parts = []
        for block in turn.blocks:
            if isinstance(block, UserBlock):
                parts.append(("STEER" if block.steer else "USER") + f"({block.text[:40]!r}{', pending' if block.pending else ''})")
            else:
                parts.append(type(block).__name__ + (f"({block.text[:60]!r})" if hasattr(block, "text") else ""))
        say(f"turn {number} [{turn.status.value}]", " | ".join(parts))


def make_core(settings, manager=None, codex=None):
    events = Events()
    core = AppCore(manager or SessionManager(settings), FakePlatform(), codex=codex, persist=False, emit=events, cache=Cache(CACHE))
    return core, events


async def run_droid() -> None:
    notes = CWD.parent / "attach-inputs"
    settings = AppSettings(max_open_sessions=2, default_model="claude-haiku-5-5", reasoning_effort="low", autonomy="high")
    settings.add_project(CWD)
    core, events = make_core(settings)
    await core.call("app.init")
    await wait(lambda: events.of("models"), 60, "models")
    cid = (await core.call("session.new", {"projectId": str(CWD.resolve())}))["id"]
    await wait(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", 60, "ready")
    controller = core._by_cid[cid].controller
    say("session", controller.session_id, controller.model)
    await core.call("session.send", {"id": cid, "text": "Run exactly this with the Execute tool: ping -n 14 127.0.0.1 . Then reply with one short sentence starting with ALPHA."})
    await wait(lambda: any(t.status.value == "running" for t in controller.transcript.turns[-1].tools.values()), 60, "the tool to run")
    say("queue:", await core.call("session.send", {"id": cid, "text": "Next turn: reply with only the word ZULU.", "how": "queue"}))
    secret = base64.b64encode(b"The secret code is PINEAPPLE-42.").decode()
    await core.call("attachments.add", {"id": cid, "files": [{"name": "notes.txt", "type": "text/plain", "data": secret}]})
    say("steer:", await core.call("session.send", {"id": cid, "text": "Also put the secret code from the attached notes.txt in your sentence.", "how": "steer"}))
    await wait(lambda: len(controller.transcript.turns) >= 2 and controller.state is SessionState.READY and not core._by_cid[cid].queue, 240, "both turns")
    show(controller)
    say("total after the two turns:", sessions_of(events)[cid].get("usage"))
    first, second = controller.transcript.turns[:2]
    answer = " ".join(getattr(b, "text", "") for b in first.blocks if b.kind == "text")
    say("steer echoed (not pending):", [b.pending for b in first.blocks if isinstance(b, UserBlock) and b.steer], "| answer has the code:", "PINEAPPLE-42" in answer, "| second is ZULU:", "ZULU" in " ".join(getattr(b, "text", "") for b in second.blocks if b.kind == "text"))
    live_total = sessions_of(events)[cid]["usage"]
    native = controller.session_id
    await core.call("session.close", {"id": cid})
    await wait(lambda: Cache(CACHE).load_session(native) is not None, 20, "the cache")
    await core.shutdown()
    saved = json.loads((Path.home() / ".factory" / "sessions" / ("-" + str(CWD.resolve()).replace(":", "").replace("\\", "-").replace("/", "-")) / f"{native}.settings.json").read_text(encoding="utf-8"))
    say("droid's own file: tokenUsage", saved["tokenUsage"], "| inclusive", saved.get("inclusiveTokenUsage"))

    # a new run of the application
    core, events = make_core(settings)
    await core.call("app.init")
    await wait(lambda: events.of("models") and events.of("sidebar"), 60, "sidebar")
    cid = (await core.call("session.openSaved", {"id": native, "projectId": str(CWD.resolve()), "title": "t"}))["id"]
    await wait(lambda: sessions_of(events).get(cid, {}).get("usage"), 30, "a total")
    say("reopened, before anything was said:", sessions_of(events)[cid]["usage"])
    await wait(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", 60, "ready")
    await asyncio.sleep(1.0)
    reopened = sessions_of(events)[cid]["usage"]
    say("reopened and ready:", reopened, "| same as before the close:", reopened == live_total)
    controller = core._by_cid[cid].controller
    await core.call("session.send", {"id": cid, "text": "Reply with only the word OK."})
    await wait(lambda: controller.state is SessionState.READY and controller.transcript.turns[-1].status.value == "done", 90, "the small turn")
    await asyncio.sleep(1.0)
    after = sessions_of(events)[cid]["usage"]
    say("after one more small turn:", after)
    say("grew by:", {k: (after[k] or 0) - (live_total[k] or 0) for k in after})
    native = controller.session_id
    await core.call("session.close", {"id": cid})
    await core.shutdown()
    saved = json.loads((Path.home() / ".factory" / "sessions" / ("-" + str(CWD.resolve()).replace(":", "").replace("\\", "-").replace("/", "-")) / f"{native}.settings.json").read_text(encoding="utf-8"))
    say("droid's own file now: tokenUsage", saved["tokenUsage"], "| inclusive", saved.get("inclusiveTokenUsage"))


async def run_codex() -> None:
    CWD.mkdir(exist_ok=True)
    settings = AppSettings(max_open_sessions=2, default_model="claude-haiku-5-5", codex_autonomy="full")
    settings.add_project(CWD)
    engine = CodexEngine(settings)
    core, events = make_core(settings, SessionManager(settings), engine)
    await core.call("app.init")
    await wait(lambda: any(m["source"] == "codex" for p in events.of("models") for m in p["models"]), 90, "codex models")
    cid = (await core.call("session.new", {"projectId": str(CWD.resolve()), "modelId": "codex:gpt-6-luna"}))["id"]
    await wait(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", 90, "ready")
    conv = core._by_cid[cid]
    controller = conv.controller
    await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "full"})
    await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "low"})
    thread = controller.session_id
    say("thread", thread, controller.model)
    try:
        await core.call("session.send", {"id": cid, "text": "Run the shell command `ping -n 14 127.0.0.1` and wait for it. Then reply with one short sentence starting with ALPHA."})
        await wait(lambda: any(t.status.value == "running" for t in controller.transcript.turns[-1].tools.values()), 90, "the tool to run")
        say("queue:", await core.call("session.send", {"id": cid, "text": "Next turn: reply with only the word ZULU.", "how": "queue"}))
        say("steer:", await core.call("session.send", {"id": cid, "text": "Also include the word BRAVO in your sentence.", "how": "steer"}))
        await wait(lambda: len(controller.transcript.turns) >= 2 and controller.state is SessionState.READY and not conv.queue, 300, "both turns")
        show(controller)
        live_total = sessions_of(events)[cid]["usage"]
        say("total after the two turns:", live_total, "| the thread's own:", controller.usage)
        first, second = controller.transcript.turns[:2]
        say("answer has BRAVO:", "BRAVO" in " ".join(getattr(b, "text", "") for b in first.blocks if b.kind == "text"), "| second is ZULU:", "ZULU" in " ".join(getattr(b, "text", "") for b in second.blocks if b.kind == "text"))
        await core.call("session.close", {"id": cid})
        await wait(lambda: Cache(CACHE).load_session("cx:" + thread) is not None, 20, "the cache")
        await core.shutdown()
        await engine.close_all()  # (the application does this when it quits: a thread has one writer)

        engine = CodexEngine(settings)
        core, events = make_core(settings, SessionManager(settings), engine)
        await core.call("app.init")
        await wait(lambda: events.of("sidebar") and any(r.startswith("cx:") for r in events.last("sidebar")["sessions"]), 90, "sidebar")
        cid = (await core.call("session.openSaved", {"id": "cx:" + thread, "projectId": str(CWD.resolve()), "title": "t"}))["id"]
        await wait(lambda: sessions_of(events).get(cid, {}).get("usage"), 30, "a total")
        say("reopened, before anything was said:", sessions_of(events)[cid]["usage"])
        await wait(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready", 90, "ready")
        controller = core._by_cid[cid].controller
        show(controller)
        await core.call("session.send", {"id": cid, "text": "Reply with only the word OK."})
        await wait(lambda: controller.state is SessionState.READY and controller.transcript.turns[-1].status.value == "done", 120, "the small turn")
        await asyncio.sleep(1.0)
        after = sessions_of(events)[cid]["usage"]
        engine_total = controller.usage
        say("after one more turn:", after, "| the thread's own:", engine_total)
        say("grew by:", {k: (after[k] or 0) - (live_total[k] or 0) for k in after})
        await core.call("session.close", {"id": cid})
        await core.shutdown()
        await engine.close_all()
    finally:
        engine2 = CodexEngine(settings)
        try:
            client = await engine2.connection()
            await client.request("thread/archive", {"threadId": thread})
            say("archived", thread)
        except Exception as error:
            say("archive failed", error)
        await engine2.close_all()


async def main() -> None:
    try:
        await (run_droid() if WHICH == "droid" else run_codex())
    finally:
        shutil.rmtree(CACHE, ignore_errors=True)


asyncio.run(main())
