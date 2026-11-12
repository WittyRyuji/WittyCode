"""The window shows what was last known at once, and the user never waits for a connection (core level).

The cache side runs on the own loop (the loopback ChatGPT Plan fake); the connecting side on Devin, whose sessions take a
moment to open (``tests/fake_devin.py`` with ``FAKE_DEVIN_OPEN_DELAY``)."""

from __future__ import annotations

import asyncio

from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from tests.app_support import MODEL, make_core, ready_core
from tests.devin_support import make_engine as make_devin
from tests.fake_responses import say
from tests.fakes import until


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def answer(events, cid):
    for payload in reversed(events.of("turns")):
        if payload["id"] == cid and payload["turns"]:
            return "".join(b.get("markdown", "") for b in payload["turns"][-1]["blocks"] if b["kind"] == "text")
    return ""


# ----------------------------------------------------------------------------- what was last known


def test_the_window_is_shown_what_was_last_known_before_anything_is_connected(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"))
        plan.script(say("Hi there"))
        await core.call("project.add", {"path": str(proj)})
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: answer(events, cid) == "Hi there" and sessions_of(events)[cid].get("mark") == "ready")
        native = core._by_cid[cid].controller.session_id
        await core.call("session.close", {"id": cid})
        await until(lambda: events.of("sidebar") and f"cg:{native}" in events.last("sidebar")["sessions"])
        await core.shutdown()

        # the second start: nothing is connected yet when the page asks for its state
        again, _, _, _, _ = make_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"))
        again.settings.add_project(proj)
        init = await again.call("app.init")
        assert set(init["sessions"]) == {f"cg:{native}"} and init["sessions"][f"cg:{native}"]["engine"] == "chatgpt"
        assert init["projects"][0]["sessionIds"] == [f"cg:{native}"]
        assert MODEL in {m["id"] for m in init["models"]}, "the model list of last time, before the account is asked"
        assert again.catalog.target(MODEL).engine == "chatgpt"
        await again.shutdown()

    run(scenario())


def test_a_cache_that_is_not_there_changes_nothing(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        core, _, _, _, _ = make_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"))
        core.settings.add_project(proj)
        init = await core.call("app.init")
        assert init["sessions"] == {} and init["models"] == []
        await core.shutdown()

    run(scenario())


def test_a_conversation_is_cached_when_its_turn_has_ended_and_when_it_closes(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, cache=cache)
        plan.script(say("Hi there"))
        proj = (tmp_path / "proj").resolve()
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        native = core._by_cid[cid].controller.session_id
        assert cache.load_session(f"cg:{native}") is None and not (tmp_path / "cache" / "sessions").exists(), "an empty session is not worth a file"
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: cache.load_session(f"cg:{native}") is not None, timeout=6)  # written a moment after the turn ended
        saved = cache.load_session(f"cg:{native}")
        assert saved["turns"][0]["status"] == "done" and saved["turns"][0]["blocks"][-1]["markdown"] == "Hi there"
        assert saved["project"] == str(proj) and saved["engine"] == "chatgpt" and saved["meta"]["settings"]["model"] == MODEL
        await core.shutdown()

    run(scenario())


def test_what_is_still_unwritten_is_written_when_the_application_ends(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, cache=cache)
        plan.script(say("Hi"))
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        native = core._by_cid[cid].controller.session_id
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: answer(events, cid) == "Hi" and core._by_cid[cid].controller.state is SessionState.READY)
        await asyncio.sleep(0.2)  # flushed to the page, not yet written (that waits a moment)
        await core.shutdown()
        assert cache.load_session(f"cg:{native}")["turns"][0]["blocks"][-1]["markdown"] == "Hi"

    run(scenario())


# ----------------------------------------------------------------------------- never waiting for a connection


async def devin_core(tmp_path, **env):
    harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY=env.pop("delay", "0.8"), **env)
    core, _, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
    harness.engine.settings = core.settings
    return core, events, harness


def new_devin(core, tmp_path):
    return asyncio.ensure_future(core.call("session.new", {"projectId": str((tmp_path / "proj").resolve()), "modelId": "devin:swe-2-high"}))


def test_a_message_typed_while_the_session_connects_is_shown_and_sent_by_itself(tmp_path):
    async def scenario():
        core, events, harness = await devin_core(tmp_path)
        opening = new_devin(core, tmp_path)
        await until(lambda: core.order)
        (conv,) = core.order
        assert conv.controller.state is SessionState.OPENING
        sent = await core.call("session.send", {"id": conv.cid, "text": "hello while connecting"})
        assert sent == {"ok": True, "queued": True}
        await until(lambda: [m["text"] for m in sessions_of(events).get(conv.cid, {}).get("queued") or []] == ["hello while connecting"])
        again = await core.call("session.send", {"id": conv.cid, "text": "and another"})
        assert again == {"ok": True, "queued": True}, "messages wait in line, each goes out as a turn of its own"
        await until(lambda: len(sessions_of(events)[conv.cid]["queued"]) == 2)

        await opening
        await until(lambda: len(harness.requests("session/prompt")) == 2, timeout=10)
        prompts = [request["prompt"][0]["text"] for request in harness.requests("session/prompt")]
        assert prompts == ["hello while connecting", "and another"], "one at a time, in order, each a turn of its own"
        await until(lambda: sessions_of(events)[conv.cid]["queued"] == [] and conv.controller.state is SessionState.READY)
        await core.shutdown()

    run(scenario())


def test_a_queued_message_can_be_taken_back(tmp_path):
    async def scenario():
        core, events, harness = await devin_core(tmp_path)
        opening = new_devin(core, tmp_path)
        await until(lambda: core.order)
        (conv,) = core.order
        await core.call("session.send", {"id": conv.cid, "text": "never mind"})
        assert (await core.call("session.unqueue", {"id": conv.cid})) == {"ok": True}
        assert events.last("restore")["text"] == "never mind"
        assert (await core.call("session.unqueue", {"id": conv.cid})) == {"ok": False}
        await opening
        await asyncio.sleep(0.2)
        assert harness.requests("session/prompt") == [], "nothing was sent"
        assert sessions_of(events)[conv.cid].get("queued") == []
        await core.shutdown()

    run(scenario())


def test_a_queued_message_of_a_session_that_could_not_start_is_told_not_lost(tmp_path):
    async def scenario():
        core, events, harness = await devin_core(tmp_path, delay="0.5", FAKE_DEVIN_OPEN_FAIL="1")
        opening = new_devin(core, tmp_path)
        await until(lambda: core.order)
        (conv,) = core.order
        await core.call("session.send", {"id": conv.cid, "text": "please"})
        await until(lambda: not core.order, timeout=10)  # the tab goes with the session that never started
        assert any("没有连上" in n["text"] and "please" in n["text"] for n in events.of("notice")), "the message is in the notice"
        await asyncio.gather(opening, return_exceptions=True)
        await core.shutdown()

    run(scenario())


def test_a_setting_chosen_while_connecting_is_shown_at_once_and_applied_when_ready(tmp_path):
    async def scenario():
        core, events, harness = await devin_core(tmp_path)
        opening = new_devin(core, tmp_path)
        await until(lambda: core.order)
        (conv,) = core.order
        choosing = asyncio.ensure_future(core.call("session.choose", {"id": conv.cid, "field": "reasoning", "value": "max"}))
        await until(lambda: (sessions_of(events).get(conv.cid, {}).get("settings") or {}).get("reasoning") == "max")
        assert conv.controller.state is SessionState.OPENING, "shown before the session is ready"
        assert (await choosing) == {"ok": True}
        await opening
        assert conv.controller.live_settings().reasoning == "max", "and applied once it was"
        await core.shutdown()

    run(scenario())
