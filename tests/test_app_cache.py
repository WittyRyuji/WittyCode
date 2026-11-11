"""The window shows what was last known at once, and the user never waits for a connection (core level, fake engines)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from desktop.core.cache import Cache
from desktop.core.session_manager import HistorySnapshot, SessionState
from tests.app_support import make_core, ready_core
from tests.fakes import FakeFactory, until
from tests.support import complete, delta, history_messages, saved_session, success, usage


def run(coro):
    return asyncio.run(coro)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


async def restart(tmp_path, proj, **more):
    """A fresh application on the same cache folder (what starting WittyCode again is)."""
    core, factory, events, platform, codex = make_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"), **more)
    core.settings.add_project(proj)
    return core, factory, events, codex


def test_the_window_is_shown_what_was_last_known_before_anything_is_connected(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        core, factory, events, _, codex = await ready_core(tmp_path, projects=False, codex=True, cache=Cache(tmp_path / "cache"))
        factory.saved = [saved_session("s-a", "Alpha", minutes_ago=30), saved_session("s-b", "Beta", minutes_ago=300)]
        codex.threads = [SimpleNamespace(id="t-1", title="Thread", modified_at=datetime.now(timezone.utc) - timedelta(hours=1), message_count=None)]
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and len(events.last("sidebar")["sessions"]) == 3)
        await until(lambda: any(e.get("codex", {}).get("auth") == "chatgpt" for e in events.of("accounts")))
        await core.shutdown()

        # the second start: nothing is connected yet when the page asks for its state
        again, _, _, _ = await restart(tmp_path, proj, codex=True)
        init = await again.call("app.init")
        assert set(init["sessions"]) == {"s-a", "s-b", "cx:t-1"}
        assert init["projects"][0]["sessionIds"] == ["s-a", "cx:t-1", "s-b"], "newest first, the Codex thread among them"
        assert init["sessions"]["cx:t-1"]["engine"] == "codex" and init["sessions"]["s-a"]["title"] == "Alpha"
        assert {"claude-haiku-5-5", "codex:gpt-5.3-codex"} <= {m["id"] for m in init["models"]}
        assert init["codex"]["auth"] == "chatgpt" and init["codex"]["plan"] == "Pro", "not 'checking' for the seconds the account takes"
        assert init["codex"]["limits"], "the limits are the last ones seen"
        assert again.catalog.target("codex:gpt-5.3-codex").engine == "codex" and again.catalog.target("claude-haiku-5-5").engine == "droid"
        await again.shutdown()

    run(scenario())


def test_a_cache_that_is_not_there_changes_nothing(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        core, factory, events, _ = await restart(tmp_path, proj, codex=True)
        init = await core.call("app.init")
        assert init["sessions"] == {} and init["models"] == [] and init["codex"]["auth"] == "checking"
        await core.shutdown()

    run(scenario())


async def first_visit(tmp_path, proj, *, with_usage=True):
    """Open the saved session s-1 in one run of the application, let it settle, close it: the cache holds it now."""
    core, factory, events, _, _ = await ready_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"))
    factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
    factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="File operations")
    await core.call("project.add", {"path": str(proj)})
    await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
    cid = (await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "File operations"}))["id"]
    await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
    controller = core._by_cid[cid].controller
    real_turns = events.of("turns")[-1]["turns"]
    if with_usage:
        controller.transcript.usage = usage(input_tokens=1500, output_tokens=420, cache_read=88000)
        core._by_cid[cid].dirty = True
        core._schedule_flush()
        await until(lambda: (sessions_of(events)[cid].get("usage") or {}).get("input") == 1500)
    await core.call("session.close", {"id": cid})
    await until(lambda: Cache(tmp_path / "cache").load_session("s-1") is not None)
    await core.shutdown()
    return real_turns


def test_a_saved_session_shows_its_cached_conversation_while_the_history_is_still_loading(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        real = await first_visit(tmp_path, proj)
        assert real, "the history has turns to cache"

        again, factory, events, _ = await restart(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="File operations")
        factory.history_delay = 0.8  # droid.exe is slow to load the history
        await again.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "File operations"}))
        await until(lambda: events.of("turns"))
        (cid,) = [c.cid for c in again.order]
        assert again._by_cid[cid].controller.state is SessionState.OPENING, "still connecting"
        shown = events.of("turns")[0]
        assert shown["all"] is True and [t["id"] for t in shown["turns"]] == [t["id"] for t in real], "the conversation is there already"
        patch = sessions_of(events)[cid]
        assert patch["usage"]["input"] == 1500 and patch["usage"]["cacheRead"] == 88000, "and the token figures of last time"
        assert patch["title"] == "File operations"

        await opening
        await until(lambda: sessions_of(events)[cid].get("mark") == "ready")
        replacing = [e for e in events.of("turns") if e["all"]]
        assert all(e["turns"] for e in replacing), "the page is never told 'no turns' on the way"
        assert replacing[-1]["turns"] == real, "the real history, replacing the cached one, is the same conversation"
        assert sessions_of(events)[cid]["usage"]["input"] == 1500, "usage is kept until the session reports its own"
        await again.shutdown()

    run(scenario())


def test_a_cached_conversation_that_the_session_has_outgrown_is_not_shown(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        await first_visit(tmp_path, proj)
        again, factory, events, _ = await restart(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=1)]  # used elsewhere since: newer than the cache
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="File operations")
        factory.history_delay = 0.4
        await again.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj)}))
        await asyncio.sleep(0.2)
        assert not events.of("turns"), "nothing from the cache: it would be replaced by a different conversation"
        await opening
        await again.shutdown()

    run(scenario())


def test_a_conversation_is_cached_when_its_turn_has_ended_and_when_it_closes(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, factory, events, _, _ = await ready_core(tmp_path, cache=cache)
        proj = (tmp_path / "proj").resolve()
        cid = (await core.call("session.new", {"projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        native = core._by_cid[cid].controller.session_id
        assert cache.load_session(native) is None and not (tmp_path / "cache" / "sessions").exists(), "an empty session is not worth a file"
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        session.active.push(delta("Hi there", "m1"), complete("m1"), success("Hi there"))
        session.active.end()
        await until(lambda: cache.load_session(native) is not None, timeout=6)  # written a moment after the turn ended
        saved = cache.load_session(native)
        assert saved["turns"][0]["status"] == "done" and saved["turns"][0]["blocks"][-1]["markdown"] == "Hi there"
        assert saved["project"] == str(proj) and saved["engine"] == "droid" and saved["meta"]["settings"]["model"] == "claude-haiku-5-5"
        await core.shutdown()

    run(scenario())


def test_what_is_still_unwritten_is_written_when_the_application_ends(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, factory, events, _, _ = await ready_core(tmp_path, cache=cache)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        native = core._by_cid[cid].controller.session_id
        session = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: session.active is not None)
        session.active.push(delta("Hi", "m1"), complete("m1"), success("Hi"))
        session.active.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        await asyncio.sleep(0.2)  # flushed to the page, not yet written (that waits a moment)
        await core.shutdown()
        assert cache.load_session(native)["turns"][0]["blocks"][-1]["markdown"] == "Hi"

    run(scenario())


# ----------------------------------------------------------------------------- never waiting for a connection


def slow_factory(delay=0.6):
    factory = FakeFactory()
    factory.default_options["open_delay"] = delay
    return factory


def test_a_message_typed_while_the_session_connects_is_shown_and_sent_by_itself(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, factory=slow_factory())
        proj = (tmp_path / "proj").resolve()
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str(proj)}))
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
        session = factory.sessions[-1]
        await until(lambda: session.active is not None)
        assert [s.prompt for s in session.streams] == ["hello while connecting"], "one at a time: the second waits for the first turn"
        await until(lambda: [m["text"] for m in sessions_of(events)[conv.cid]["queued"]] == ["and another"])
        user = events.of("turns")[-1]["turns"][0]["blocks"][0]
        assert user["kind"] == "user" and user["text"] == "hello while connecting"
        session.active.push(delta("ok", "m1"), complete("m1"), success("ok"))
        session.active.end()
        await until(lambda: len(session.streams) == 2, timeout=5)
        assert session.streams[-1].prompt == "and another", "sent by itself when the first turn was over"
        await until(lambda: sessions_of(events)[conv.cid]["queued"] == [])
        session.active.push(delta("done", "m2"), complete("m2"), success("done"))
        session.active.end()
        await until(lambda: conv.controller.state is SessionState.READY)
        await core.shutdown()

    run(scenario())


def test_a_queued_message_can_be_taken_back(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, factory=slow_factory(0.8))
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))
        await until(lambda: core.order)
        (conv,) = core.order
        await core.call("session.send", {"id": conv.cid, "text": "never mind"})
        assert (await core.call("session.unqueue", {"id": conv.cid})) == {"ok": True}
        assert events.last("restore")["text"] == "never mind"
        assert (await core.call("session.unqueue", {"id": conv.cid})) == {"ok": False}
        await opening
        await asyncio.sleep(0.2)
        assert not factory.sessions[-1].streams, "nothing was sent"
        assert sessions_of(events)[conv.cid].get("queued") == []
        await core.shutdown()

    run(scenario())


def test_a_queued_message_goes_back_to_the_composer_when_a_saved_session_will_not_resume(tmp_path):
    from droid_sdk import DroidProcessError

    async def scenario():
        proj = tmp_path / "proj"
        factory = slow_factory(0.3)
        factory.default_options["fail_open"] = DroidProcessError("cannot resume", exit_code=3)
        factory.saved = [saved_session("s-1", "Old chat", minutes_ago=9)]
        core, _, events, _, _ = await ready_core(tmp_path, factory=factory)
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        asyncio.ensure_future(core.call("session.openSaved", {"id": "s-1", "projectId": str(proj.resolve())}))
        await until(lambda: core.order)
        (conv,) = core.order
        await core.call("session.send", {"id": conv.cid, "text": "please"})
        await until(lambda: events.of("restore"), timeout=5)
        assert events.last("restore")["text"] == "please", "the tab stays (it can be reconnected): the text goes back into its composer"
        assert any("没有连上" in n["text"] for n in events.of("notice"))
        assert core.order and core.order[0].queue == []
        await core.shutdown()

    run(scenario())


def test_a_queued_message_of_a_session_that_could_not_start_at_all_is_told_not_lost(tmp_path):
    async def scenario():
        factory = slow_factory(0.3)
        factory.default_options["fail_open"] = RuntimeError("droid.exe would not start")
        core, _, events, _, _ = await ready_core(tmp_path, factory=factory)
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))
        await until(lambda: core.order)
        (conv,) = core.order
        await core.call("session.send", {"id": conv.cid, "text": "please"})
        await until(lambda: not core.order, timeout=5)  # the tab goes with the session that never started
        assert any("没有连上" in n["text"] and "please" in n["text"] for n in events.of("notice")), "the message is in the notice"
        await asyncio.gather(opening, return_exceptions=True)
        await core.shutdown()

    run(scenario())


def test_a_setting_chosen_while_connecting_is_shown_at_once_and_applied_when_ready(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, factory=slow_factory())
        opening = asyncio.ensure_future(core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))
        await until(lambda: core.order)
        (conv,) = core.order
        choosing = asyncio.ensure_future(core.call("session.choose", {"id": conv.cid, "field": "reasoning", "value": "high"}))
        await until(lambda: (sessions_of(events).get(conv.cid, {}).get("settings") or {}).get("reasoning") == "high")
        assert conv.controller.state is SessionState.OPENING, "shown before the session is ready"
        assert (await choosing) == {"ok": True}
        await opening
        assert conv.controller.live_settings().reasoning == "high", "and applied once it was"
        await core.shutdown()

    run(scenario())
