"""The cache holds the sessions nobody has opened yet, and a saved session shows its settings and figures while it connects."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from droid_sdk import Autonomy, Mode, ReasoningEffort
from droid_sdk.schemas.client import LoadSessionResult

from desktop.core import app_core, session_manager
from desktop.core.cache import Cache
from desktop.core.context import ContextState
from desktop.core.session_manager import HistorySnapshot, LiveSettings, Peek, history_of
from desktop.core.transcript import Transcript
from desktop.core.usage import Tokens
from tests.app_support import make_core, ready_core
from tests.fakes import until
from tests.support import history_messages, saved_session, usage

SETTINGS = LiveSettings(model="claude-haiku-5-5", reasoning="high", autonomy="medium", mode="spec")
PAGE_SETTINGS = {"model": "claude-haiku-5-5", "reasoning": "high", "autonomy": "medium", "mode": "spec"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def quick_warm(monkeypatch):
    monkeypatch.setattr(app_core, "WARM_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(app_core, "WARM_PAUSE_SECONDS", 0.0)


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def left_as(title="File operations", **more):
    """What droid answers for a session left with other settings than the defaults, after a long conversation."""
    return HistorySnapshot(
        messages=history_messages(),
        title=title,
        usage=more.pop("usage", usage(input_tokens=1500, output_tokens=420, cache_read=88000)),
        settings=more.pop("settings", SETTINGS),
        context_used=more.pop("context_used", 38_088),
    )


async def fresh_core(tmp_path, proj, **more):
    core, factory, events, platform, codex = make_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"), read_ahead=True, **more)
    core.settings.add_project(proj)
    return core, factory, events, codex


def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


def histories_read(factory):
    return [ids for name, ids in factory.calls if name == "load_histories"]


# ----------------------------------------------------------------------------- reading sessions ahead


def test_the_saved_sessions_are_read_into_the_cache_without_being_opened(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7), saved_session("s-2", "Other", minutes_ago=90)]
        factory.history["s-1"] = left_as()
        factory.history["s-2"] = left_as("Other", settings=LiveSettings("claude-haiku-5-5", "low", "high", "auto"), context_used=900)
        cache = Cache(tmp_path / "cache")
        await core.call("app.init")
        await until(lambda: cache.load_session("s-1") and cache.load_session("s-2"), timeout=5)
        first = cache.load_session("s-1")
        assert first["engine"] == "droid" and first["native"] == "s-1" and first["title"] == "File operations" and first["turns"]
        assert first["meta"]["settings"] == PAGE_SETTINGS, "the model, reasoning, permission and mode it was left with"
        assert first["meta"]["usage"]["input"] == 1500 and first["meta"]["usage"]["cacheRead"] == 88000
        assert first["meta"]["context"]["used"] == 38_088
        assert first["ledger"]["total"]["input"] == 1500 and first["ledger"]["seen"]["self"]["input"] == 1500, "so the total goes on from here"
        (row,) = [r for r in core._saved[str(proj.resolve())] if r.id == "s-1"]
        assert first["activeAt"] == row.modified.timestamp(), "as new as the session itself: it is shown"
        assert cache.load_session("s-2")["meta"]["settings"]["reasoning"] == "low"
        assert histories_read(factory) == [["s-1", "s-2"]], "newest first, through one process"
        assert not [name for name, _ in factory.calls if name in ("resume", "create")] and not core.order, "nothing was opened"
        await core.shutdown()

    run(scenario())


def test_a_session_read_ahead_is_on_the_page_with_everything_the_moment_it_is_clicked(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        await core.call("app.init")
        await until(lambda: Cache(tmp_path / "cache").load_session("s-1"), timeout=5)
        await core.shutdown()

        again, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        factory.history_delay = 0.8
        factory.default_options["open_delay"] = 0.8
        await again.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj), "title": "File operations"}))
        await until(lambda: events.of("turns"))
        (cid,) = [c.cid for c in again.order]
        assert again._by_cid[cid].controller.state.value == "opening", "still connecting"
        shown = sessions_of(events)[cid]
        assert shown["settings"] == PAGE_SETTINGS
        assert shown["usage"]["input"] == 1500 and shown["usage"]["cacheRead"] == 88000
        assert shown["context"]["used"] == 38_088
        await opening
        await again.shutdown()

    run(scenario())


def test_a_session_that_is_in_the_cache_complete_and_up_to_date_is_not_read_again(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        for start in range(2):
            core, factory, events, _ = await fresh_core(tmp_path, proj)
            factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
            factory.history["s-1"] = left_as()
            cache = Cache(tmp_path / "cache")
            await core.call("app.init")
            await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
            await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
            assert cache.load_session("s-1")
            await core.shutdown()
            if start == 0:
                assert histories_read(factory) == [["s-1"]]
            else:
                assert histories_read(factory) == [], "the second start found it complete and as new as the session"

    run(scenario())


def test_a_session_used_elsewhere_since_is_read_again_and_its_total_goes_on(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=60)]
        factory.history["s-1"] = left_as(usage=usage(input_tokens=1500, output_tokens=400, cache_read=80_000))
        cache = Cache(tmp_path / "cache")
        await core.call("app.init")
        await until(lambda: cache.load_session("s-1"), timeout=5)
        await core.shutdown()

        again, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=2)]  # talked to in another program since
        factory.history["s-1"] = left_as(usage=usage(input_tokens=2000, output_tokens=900, cache_read=95_000), context_used=51_000)
        await again.call("app.init")
        await until(lambda: (cache.load_session("s-1") or {}).get("meta", {}).get("context", {}).get("used") == 51_000, timeout=5)
        fresh = cache.load_session("s-1")
        assert fresh["meta"]["usage"]["input"] == 2000 and fresh["meta"]["usage"]["cacheRead"] == 95_000
        assert fresh["ledger"]["total"]["output"] == 900, "what the engine now counts; only the part after the last figure was added"
        await again.shutdown()

    run(scenario())


def handwritten(proj, *, active_at, ledger, usage=None, settings=None, used=None):
    """A cached copy as an earlier version of the application (or an earlier visit) left it."""
    return {
        "engine": "droid", "native": "s-1", "project": str(proj.resolve()), "title": "File operations", "activeAt": active_at,
        "turns": [{"id": "t1"}],
        "meta": {"settings": settings or PAGE_SETTINGS, "usage": usage, "context": {"used": used, "window": 0, "model": "", "compacted": False}},
        "ledger": ledger,
    }


TOTAL = {"input": 1500, "output": 420, "cacheWrite": 0, "cacheRead": 88000}


@pytest.mark.parametrize(
    "missing, usage_figures, used",
    [("token figures", None, 38_088), ("context figure", TOTAL, None), ("both", None, None)],
)
def test_a_copy_that_lacks_something_the_page_shows_is_read_again_and_keeps_the_later_time_it_had(tmp_path, missing, usage_figures, used):
    import time

    async def scenario():
        proj = project(tmp_path)
        cache = Cache(tmp_path / "cache")
        later = time.time() + 30  # (Codex stamps a thread with the start of its last turn: a copy can be later than the listing)
        ledger = {"total": usage_figures, "seen": {"self": usage_figures} if usage_figures else {}}
        cache.save_session("s-1", handwritten(proj, active_at=later, ledger=ledger, usage=usage_figures, used=used))
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as(usage=usage(input_tokens=1700, output_tokens=420, cache_read=88_000))
        await core.call("app.init")
        await until(lambda: histories_read(factory), timeout=5)
        await until(lambda: core._warm_task.done(), timeout=5)
        fresh = cache.load_session("s-1")
        assert histories_read(factory) == [["s-1"]], f"a copy without its {missing} is not good enough"
        assert fresh["meta"]["usage"]["input"] >= 1500 and fresh["meta"]["context"]["used"] == 38_088
        assert fresh["activeAt"] == later, "never moved back to an earlier time (it would look out of date next time)"
        await core.shutdown()

    run(scenario())


def test_the_total_of_a_conversation_that_moved_between_engines_goes_on_when_it_is_read_ahead(tmp_path):
    import time

    async def scenario():
        proj = project(tmp_path)
        cache = Cache(tmp_path / "cache")
        elsewhere = {"input": 10_000, "output": 100, "cacheWrite": 0, "cacheRead": 50_000}  # counted by an engine it was on before
        mine = {"input": 1_500, "output": 400, "cacheWrite": 0, "cacheRead": 80_000}
        total = {key: elsewhere[key] + mine[key] for key in mine}
        cache.save_session("s-1", handwritten(proj, active_at=time.time() - 3600, ledger={"total": total, "seen": {"self": mine}}, usage=total))
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as(usage=usage(input_tokens=2_000, output_tokens=900, cache_read=95_000))
        await core.call("app.init")
        await until(lambda: ((cache.load_session("s-1") or {}).get("meta", {}).get("context") or {}).get("used") == 38_088, timeout=5)
        fresh = cache.load_session("s-1")
        assert fresh["meta"]["usage"]["input"] == 10_000 + 2_000 and fresh["meta"]["usage"]["cacheRead"] == 50_000 + 95_000
        assert fresh["ledger"]["seen"]["self"]["input"] == 2_000
        await core.shutdown()

    run(scenario())


def test_nothing_is_read_ahead_when_the_application_is_told_not_to(tmp_path):
    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        core, factory, events, _, _ = make_core(tmp_path, projects=False, cache=Cache(tmp_path / "cache"), read_ahead=False)
        core.settings.add_project(proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        await core.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        await asyncio.sleep(0.2)
        assert histories_read(factory) == [] and core._warm_task is None
        await core.shutdown()

    run(scenario())


def test_only_the_newest_sessions_of_a_project_are_read_ahead(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "WARM_ROWS", 2)

    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session(f"s-{n}", f"Session {n}", minutes_ago=10 * n) for n in range(1, 5)]
        for n in range(1, 5):
            factory.history[f"s-{n}"] = left_as(f"Session {n}")
        await core.call("app.init")
        await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
        assert histories_read(factory) == [["s-1", "s-2"]]
        await core.shutdown()

    run(scenario())


def test_a_session_that_is_open_in_a_tab_is_left_to_its_tab(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(app_core, "WARM_DELAY_SECONDS", 0.5)
        try:
            core, factory, events, _ = await fresh_core(tmp_path, proj)
            factory.saved = [saved_session("s-1", "File operations", minutes_ago=7), saved_session("s-2", "Other", minutes_ago=90)]
            factory.history["s-1"] = left_as()
            factory.history["s-2"] = left_as("Other")
            await core.call("app.init")
            await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
            await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj)})
            await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
            assert histories_read(factory) == [["s-2"]], "s-1 is being shown by its tab: the tab keeps its cache"
            await core.shutdown()
        finally:
            monkey.undo()

    run(scenario())


def test_what_a_tab_wrote_to_the_cache_is_never_overwritten_by_what_was_read_before_it_opened(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        factory.histories_delay = 0.8  # the read ahead is still under way when the tab opens, closes and has written its copy
        await core.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        cid = (await core.call("session.openSaved", {"id": "s-1", "projectId": str(proj)}))["id"]
        await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
        await core.call("session.close", {"id": cid})
        await until(lambda: not core.order)
        assert core._warm_task is not None and not core._warm_task.done(), "the tab is gone and the reading is still on its way"
        await until(lambda: core._warm_task.done(), timeout=5)
        await asyncio.sleep(0.1)
        saved = Cache(tmp_path / "cache").load_session("s-1")
        assert saved["meta"]["settings"]["autonomy"] == "off", "the tab's own copy (its session's settings), not the one read ahead (medium)"
        await core.shutdown()

    run(scenario())


def test_a_history_that_cannot_be_read_changes_nothing_and_is_not_asked_for_again_at_once(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history_error = RuntimeError("droid said no")
        await core.call("app.init")
        await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
        assert Cache(tmp_path / "cache").load_session("s-1") is None and not core.order
        core._schedule_warm(str(proj.resolve()))
        await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
        assert histories_read(factory) == [["s-1"]], "one try per change of the session"
        await core.shutdown()

    run(scenario())


def test_a_session_without_a_conversation_is_not_cached(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-1", "Empty", minutes_ago=7, messages=0)]
        factory.history["s-1"] = HistorySnapshot(messages=[], title="Empty")
        await core.call("app.init")
        await until(lambda: core._warm_task is not None and core._warm_task.done(), timeout=5)
        assert Cache(tmp_path / "cache").load_session("s-1") is None
        await core.shutdown()

    run(scenario())


def test_nothing_is_read_ahead_without_a_cache_to_put_it_in(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        await core.call("project.add", {"path": str(project(tmp_path))})
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        await asyncio.sleep(0.2)
        assert histories_read(factory) == [] and core._warm_task is None
        await core.shutdown()

    run(scenario())


def codex_peek(**more):
    transcript = Transcript()
    transcript.load_history(history_messages())
    fields = {
        "title": "Thread",
        "settings": LiveSettings(model="gpt-5.3-codex", reasoning="high", autonomy="full", mode="auto"),
        "usage": Tokens(1_200_000, 30_000, None, 3_400_000),
        "context_used": 202_146,
        "window": 400_000,
    }
    fields.update(more)
    return Peek(transcript, **fields)


def test_codex_threads_are_read_ahead_too(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, codex = await fresh_core(tmp_path, proj, codex=True)
        codex.threads = [SimpleNamespace(id="t-1", title="Thread", modified_at=datetime.now(timezone.utc) - timedelta(hours=1), message_count=None)]
        codex.peeks["t-1"] = codex_peek()
        cache = Cache(tmp_path / "cache")
        await core.call("app.init")
        await until(lambda: cache.load_session("cx:t-1"), timeout=5)
        saved = cache.load_session("cx:t-1")
        assert saved["engine"] == "codex" and saved["native"] == "t-1" and saved["turns"]
        assert saved["meta"]["settings"] == {"model": "codex:gpt-5.3-codex", "reasoning": "high", "autonomy": "full", "mode": "auto"}
        assert saved["meta"]["usage"] == {"input": 1_200_000, "output": 30_000, "cacheWrite": None, "cacheRead": 3_400_000}
        assert saved["meta"]["context"] == {"used": 202_146, "window": 400_000, "model": "gpt-5.3-codex", "compacted": False}
        assert codex.peeked == ["t-1"] and not core.order
        await core.shutdown()

    run(scenario())


def test_a_codex_thread_without_a_permission_level_shows_the_other_three(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, codex = await fresh_core(tmp_path, proj, codex=True)
        codex.threads = [SimpleNamespace(id="t-1", title="Thread", modified_at=datetime.now(timezone.utc) - timedelta(hours=1), message_count=None)]
        codex.peeks["t-1"] = codex_peek(settings=LiveSettings(model="gpt-5.3-codex", reasoning="high", autonomy="", mode="auto"))
        cache = Cache(tmp_path / "cache")
        await core.call("app.init")
        await until(lambda: cache.load_session("cx:t-1"), timeout=5)
        assert cache.load_session("cx:t-1")["meta"]["settings"] == {"model": "codex:gpt-5.3-codex", "reasoning": "high", "mode": "auto"}, "a level is never made up"
        await core.shutdown()

    run(scenario())


# ----------------------------------------------------------------------------- what a session was left with, while it connects


def test_what_droid_left_a_saved_session_with_is_shown_while_it_connects_even_with_no_cache(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, projects=False)
        proj = project(tmp_path)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        factory.default_options["open_delay"] = 1.0
        await core.call("project.add", {"path": str(proj)})
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(core.call("session.openSaved", {"id": "s-1", "projectId": str(proj)}))
        await until(lambda: sessions_of(events) and any((v.get("settings") or {}).get("reasoning") == "high" for v in sessions_of(events).values()), timeout=3)
        (cid,) = [c.cid for c in core.order]
        assert core._by_cid[cid].controller.state.value == "opening", "the session itself has not answered yet"
        shown = sessions_of(events)[cid]
        assert shown["settings"] == PAGE_SETTINGS
        assert shown["context"]["used"] == 38_088
        assert shown["usage"]["input"] == 1500
        await opening
        await core.shutdown()

    run(scenario())


def test_a_setting_chosen_while_a_cached_session_connects_leaves_the_other_three_in_place(tmp_path):
    from tests.test_app_cache import first_visit, restart

    async def scenario():
        proj = tmp_path / "proj"
        proj.mkdir()
        await first_visit(tmp_path, proj)
        again, factory, events, _ = await restart(tmp_path, proj)
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = HistorySnapshot(messages=history_messages(), title="File operations")
        factory.default_options["open_delay"] = 1.0
        await again.call("app.init")
        await until(lambda: events.of("sidebar") and "s-1" in events.last("sidebar")["sessions"])
        opening = asyncio.ensure_future(again.call("session.openSaved", {"id": "s-1", "projectId": str(proj)}))
        await until(lambda: events.of("turns"))
        (cid,) = [c.cid for c in again.order]
        before = dict(sessions_of(events)[cid]["settings"])
        assert set(before) == {"model", "reasoning", "autonomy", "mode"}
        choosing = asyncio.ensure_future(again.call("session.choose", {"id": cid, "field": "reasoning", "value": "high"}))
        await until(lambda: sessions_of(events)[cid]["settings"].get("reasoning") == "high")
        assert sessions_of(events)[cid]["settings"] == {**before, "reasoning": "high"}, "the choice is laid over what was there"
        await opening
        await choosing
        await again.shutdown()

    run(scenario())


# ----------------------------------------------------------------------------- the context windows that were learned


def test_a_model_window_a_connected_session_reported_is_remembered_across_starts(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        await core.call("app.init")

        def conv(window, served):
            return SimpleNamespace(engine="droid", controller=SimpleNamespace(context=ContextState(window=window, model=served)))

        core._learn_window(conv(None, ""), "claude-haiku-5-5")
        core._learn_window(conv(200_000, "some-other-model"), "claude-haiku-5-5")
        assert core._windows == {}, "no window yet, and the window of a model served in its place says nothing about this one"
        core._learn_window(conv(872_000, "claude-haiku-5-5"), "claude-haiku-5-5")
        assert core._windows == {"claude-haiku-5-5": 872_000}
        await asyncio.sleep(1.3)  # (the index is written a moment after it changes)
        await core.shutdown()

        again, _, _, _ = await fresh_core(tmp_path, proj)
        await again.call("app.init")
        assert again._windows == {"claude-haiku-5-5": 872_000}

    run(scenario())


def test_a_window_seen_in_another_cached_session_of_the_model_fills_the_context_of_one_read_ahead(tmp_path):
    import time

    async def scenario():
        proj = project(tmp_path)
        cache = Cache(tmp_path / "cache")
        cache.save_session(
            "s-old",
            {
                "engine": "droid", "native": "s-old", "project": str(proj.resolve()), "title": "Old", "activeAt": time.time(), "turns": [{"id": "t1"}],
                "meta": {"settings": PAGE_SETTINGS, "usage": {"input": 1, "output": 1, "cacheWrite": 0, "cacheRead": 1},
                         "context": {"used": 900, "window": 872_000, "model": "claude-haiku-5-5", "compacted": False}},
            },
        )
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        factory.saved = [saved_session("s-old", "Old", minutes_ago=30), saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        await core.call("app.init")
        await until(lambda: cache.load_session("s-1"), timeout=5)
        assert cache.load_session("s-1")["meta"]["context"]["window"] == 872_000, "no session of this run reported it: the cache's other sessions did"
        assert histories_read(factory) == [["s-1"]], "s-old was complete and is left alone"
        await core.shutdown()

    run(scenario())


def test_a_window_that_belongs_to_another_model_is_not_taken_for_this_one(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        await core.call("app.init")
        core._learn_window_from({"engine": "droid", "meta": {"settings": PAGE_SETTINGS, "context": {"window": 196_608, "model": "kimi-k3"}}})
        core._learn_window_from({"engine": "droid", "meta": {"settings": PAGE_SETTINGS, "context": {"window": 0, "model": "claude-haiku-5-5"}}})
        core._learn_window_from({"engine": "droid", "meta": {"settings": PAGE_SETTINGS, "context": {"window": True, "model": "claude-haiku-5-5"}}})
        core._learn_window_from({"engine": "droid", "meta": "garbage"})
        assert core._windows == {}
        core._learn_window_from({"engine": "droid", "meta": {"settings": PAGE_SETTINGS, "context": {"window": 872_000, "model": "claude-haiku-5-5"}}})
        core._learn_window_from({"engine": "droid", "meta": {"settings": PAGE_SETTINGS, "context": {"window": 100_000, "model": "claude-haiku-5-5"}}})
        assert core._windows == {"claude-haiku-5-5": 872_000}, "a live report is the truth; a cached figure only fills a gap"
        await core.shutdown()

    run(scenario())


def test_a_session_read_ahead_has_the_window_learned_for_its_model(tmp_path):
    async def scenario():
        proj = project(tmp_path)
        core, factory, events, _ = await fresh_core(tmp_path, proj)
        core._windows["claude-haiku-5-5"] = 872_000
        factory.saved = [saved_session("s-1", "File operations", minutes_ago=7)]
        factory.history["s-1"] = left_as()
        cache = Cache(tmp_path / "cache")
        await core.call("app.init")
        core._windows["claude-haiku-5-5"] = 872_000  # (init reads the cache, which has none yet)
        await until(lambda: cache.load_session("s-1"), timeout=5)
        assert cache.load_session("s-1")["meta"]["context"] == {"used": 38_088, "window": 872_000, "model": "claude-haiku-5-5", "compacted": False}
        await core.shutdown()

    run(scenario())


# ----------------------------------------------------------------------------- droid's answer to load_session


def test_load_session_answers_are_read_into_settings_and_a_context_figure():
    result = LoadSessionResult.model_validate(
        {
            "session": {"messages": [], "title": "Explain project structure"},
            "settings": {"modelId": "claude-opus-5-5", "reasoningEffort": "xhigh", "interactionMode": "auto", "autonomyLevel": "high"},
            "tokenUsage": {"inputTokens": 359, "outputTokens": 4537, "cacheCreationTokens": 19949, "cacheReadTokens": 77883, "thinkingTokens": 309},
            "lastCallTokenUsage": {"inputTokens": 1219, "cacheReadTokens": 33535, "outputTokens": 3334},
        }
    )
    snapshot = history_of(result)
    assert snapshot.settings == LiveSettings(model="claude-opus-5-5", reasoning="xhigh", autonomy="high", mode="auto")
    assert snapshot.context_used == 38_088, "input + cache reads + output of the last call: the figure droid compares with its budget"
    assert Tokens.of(snapshot.usage).total == 359 + 4537 + 19949 + 77883 and snapshot.title == "Explain project structure"


def test_a_load_session_answer_without_a_last_call_has_no_context_figure():
    result = LoadSessionResult.model_validate(
        {"session": {"messages": [], "title": None}, "settings": {"modelId": "m", "reasoningEffort": "off"}}
    )
    snapshot = history_of(result)
    assert snapshot.context_used is None and snapshot.settings.model == "m" and snapshot.settings.autonomy == ""


class FakeClient:
    """A droid process that answers ``load_session`` from a table (or raises what the table holds)."""

    instances: list["FakeClient"] = []

    def __init__(self, transport=None):
        self.transport = transport
        self.loaded: list[str] = []
        self.closed = 0
        self.session_closed = 0
        FakeClient.instances.append(self)

    async def connect(self):
        return None

    async def load_session(self, session_id):
        self.loaded.append(session_id)
        outcome = TABLE[session_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close_session(self, reason=None):
        self.session_closed += 1

    async def close(self):
        self.closed += 1


TABLE: dict = {}


def answer(title):
    return LoadSessionResult.model_validate({"session": {"messages": [], "title": title}, "settings": {"modelId": "m", "reasoningEffort": "low"}})


def test_many_histories_go_through_one_droid_process_and_a_bad_one_does_not_stop_the_rest(monkeypatch):
    FakeClient.instances.clear()
    TABLE.clear()
    TABLE.update({"a": answer("A"), "b": ValueError("not a session"), "c": answer("C")})
    monkeypatch.setattr(session_manager, "DroidClient", FakeClient)
    monkeypatch.setattr(session_manager, "ProcessTransport", lambda **kwargs: SimpleNamespace(**kwargs))

    async def scenario():
        from pathlib import Path

        factory = session_manager.DroidFactory(lambda: Path("droid.exe"))
        got = [(i, o) async for i, o in factory.load_histories(["a", "b", "c"], Path("."))]
        assert [i for i, _ in got] == ["a", "b", "c"]
        assert got[0][1].title == "A" and isinstance(got[1][1], ValueError) and got[2][1].title == "C"
        (client,) = FakeClient.instances
        assert client.loaded == ["a", "b", "c"] and client.closed == 1
        assert client.session_closed == 0, "closing a session ends droid's process: none is closed between two loads"

    run(scenario())


def test_a_lost_droid_process_ends_the_reading_and_is_closed_even_when_the_reader_stops_early(monkeypatch):
    from droid_sdk import DroidProcessError

    FakeClient.instances.clear()
    TABLE.clear()
    TABLE.update({"a": answer("A"), "b": DroidProcessError("gone"), "c": answer("C")})
    monkeypatch.setattr(session_manager, "DroidClient", FakeClient)
    monkeypatch.setattr(session_manager, "ProcessTransport", lambda **kwargs: SimpleNamespace(**kwargs))

    async def scenario():
        from pathlib import Path

        factory = session_manager.DroidFactory(lambda: Path("droid.exe"))
        got = [i async for i, _ in factory.load_histories(["a", "b", "c"], Path("."))]
        assert got == ["a", "b"], "nothing more is asked of a process that is gone"
        assert FakeClient.instances[-1].closed == 1

        stopped = FakeClient.instances.__len__()
        manager = SimpleNamespace(factory=factory)
        async with __import__("contextlib").aclosing(factory.load_histories(["a", "c"], Path("."))) as results:
            async for _ in results:
                break
        assert len(FakeClient.instances) == stopped + 1 and FakeClient.instances[-1].closed == 1, "a reader that stops early still closes the process"
        del manager

    run(scenario())
