"""The ChatGPT Plan engine inside the application: the account, the models, the sessions, the sidebar, settings, an engine
change. The engine is the real one (real agent loop, real tools) talking to the loopback fake of OpenAI's Responses API;
Droid's side is the usual fake."""

from __future__ import annotations

import asyncio

from desktop.core.cache import Cache
from desktop.core.session_manager import SessionState
from tests.app_support import make_core, ready_core
from tests.chatgpt_support import ACCESS_TOKEN, make_engine
from tests.fake_responses import FakeResponses, call, say, usage
from tests.fakes import until
from tests.support import complete, delta, success


def run(coro):
    return asyncio.run(coro)


async def chatgpt_core(tmp_path, fake, **options):
    engine_options = options.pop("engine", {})
    engine = make_engine(tmp_path, fake, **engine_options)
    core, factory, events, platform, _ = await ready_core(tmp_path, chatgpt_engine=engine, **options)
    engine.settings = core.settings  # (one settings object in the application)
    return core, factory, events, engine


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def meta(events, cid):
    return sessions_of(events).get(cid, {})


async def new_chatgpt(core, events, tmp_path, model="chatgpt:gpt-6.1-sol", **more):
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": model, **more}))["id"]
    await until(lambda: meta(events, cid).get("mark") == "ready")
    return cid


def last_turn_blocks(events, cid):
    for payload in reversed(events.of("turns")):
        if payload["id"] == cid and payload["turns"]:
            return payload["turns"][-1]["blocks"]
    return []


def answer_of(events, cid):
    return "".join(b.get("markdown", "") for b in last_turn_blocks(events, cid) if b["kind"] == "text")


def chatgpt_models(events):
    return [m for m in events.last("models")["models"] if m["source"] == "chatgpt"]


# ---------------------------------------------------------------------------------------------- models and account


def test_the_chatgpt_plan_models_are_in_the_catalog_under_their_own_prefix(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            models = {m["id"]: m for m in chatgpt_models(events)}
            assert list(models) == ["chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"]
            sol = models["chatgpt:gpt-6.1-sol"]
            assert (sol["label"], sol["efforts"], sol["defaultEffort"], sol["window"]) == ("GPT-6.1-Sol", ["low", "medium", "high"], "medium", 400000)
            target = core.catalog.target("chatgpt:gpt-6-luna")
            assert (target.engine, target.model) == ("chatgpt", "gpt-6-luna")
            assert core.catalog.engine_of("chatgpt:gpt-6-luna") == "chatgpt" and core.catalog.source_of("chatgpt:gpt-6-luna") == "chatgpt"
            await core.shutdown()

    run(scenario())


def test_the_account_is_told_at_init_with_no_waiting_and_what_it_may_do(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake)
            core, _, events, _, _ = make_core(tmp_path, chatgpt_engine=engine)
            init = await core.call("app.init")
            account = init["chatgpt"]
            assert (account["auth"], account["email"], account["planEnabled"], account["available"], account["enabled"], account["welcomed"]) == (
                "signed-in", "me@example.com", True, True, True, False)
            assert account["accounts"][0]["active"] is True
            await core.shutdown()

    run(scenario())


def test_without_the_engine_the_account_is_unavailable_and_nothing_else_changes(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        init = await core.call("app.init")
        assert init["chatgpt"]["available"] is False and init["chatgpt"]["auth"] == "signed-out"
        assert not chatgpt_models(events)
        try:
            await core.call("chatgpt.login")
        except Exception as error:
            assert "不可用" in str(error)
        else:
            raise AssertionError("signing in without the engine must say so")
        await core.shutdown()

    run(scenario())


def test_signed_out_or_without_the_plan_there_are_no_models_to_offer(tmp_path):
    async def scenario():
        for options in ({"signed_in": False}, {"plan": False}):
            with FakeResponses(token=ACCESS_TOKEN) as fake:
                folder = tmp_path / ("a" if "plan" in options else "b")
                folder.mkdir()
                core, _, events, _ = await chatgpt_core(folder, fake, engine=options)
                assert not chatgpt_models(events), options
                await core.shutdown()

    run(scenario())


def test_signing_out_takes_the_models_away_and_hiding_them_with_the_switch_too(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            assert chatgpt_models(events)
            account = await core.call("chatgpt.setEnabled", {"on": False})
            assert account["enabled"] is False and account["auth"] == "signed-in"
            await until(lambda: not chatgpt_models(events))
            await core.call("chatgpt.setEnabled", {"on": True})
            await until(lambda: chatgpt_models(events))
            account = await core.call("chatgpt.logout", {})
            assert account["auth"] == "signed-out"
            assert any(p.get("chatgpt", {}).get("auth") == "signed-out" for p in events.of("accounts"))
            await until(lambda: not chatgpt_models(events))
            await core.shutdown()

    run(scenario())


def test_the_one_time_note_is_remembered(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            assert (await core.call("chatgpt.account"))["welcomed"] is False
            account = await core.call("chatgpt.welcomed", {})
            assert account["welcomed"] is True and core.settings.chatgpt_welcomed is True
            assert (await core.call("app.init"))["chatgpt"]["welcomed"] is True
            await core.shutdown()

    run(scenario())


def test_a_slow_account_refresh_does_not_bring_back_what_the_user_just_did(tmp_path):
    """The accounts are refreshed a few seconds after start (``droid --version`` and the like): the one-time note dismissed
    and the switch turned off in that time must not be undone by the refresh that was already on its way."""

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            gate = asyncio.Event()
            real = core._factory

            async def slow():
                await gate.wait()
                return await real()

            core._factory = slow
            refresh = asyncio.ensure_future(core._refresh_accounts())
            await asyncio.sleep(0.1)
            await core.call("chatgpt.welcomed", {})
            await core.call("chatgpt.setEnabled", {"on": False})
            gate.set()
            await refresh
            last = [p["chatgpt"] for p in events.of("accounts") if "chatgpt" in p][-1]
            assert last["welcomed"] is True and last["enabled"] is False
            await core.shutdown()

    run(scenario())


def test_a_sign_in_that_fails_is_a_sentence_for_the_user_not_a_crash(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, engine = await chatgpt_core(tmp_path, fake)

            async def refused(**kwargs):
                from desktop.core.chatgpt.errors import AuthError

                raise AuthError("the user said no", code="access_denied")

            engine.login = refused
            try:
                await core.call("chatgpt.login", {})
            except Exception as error:
                assert "拒绝了授权" in str(error)
            else:
                raise AssertionError("a refused sign-in must reach the user")
            await core.shutdown()

    run(scenario())


# ---------------------------------------------------------------------------------------------- sessions


def test_a_chatgpt_plan_session_opens_runs_and_reports_its_settings_and_total(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Hi from the plan.", used=usage(900, 12, cached=300)))
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            conv = core._by_cid[cid]
            assert conv.engine == "chatgpt" and meta(events, cid)["engine"] == "chatgpt"
            patch = meta(events, cid)
            assert patch["settings"]["model"] == "chatgpt:gpt-6.1-sol" and patch["settings"]["autonomy"] == "code"
            assert await core.call("session.send", {"id": cid, "text": "hello"}) == {"ok": True}
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "Hi from the plan.")
            await until(lambda: (meta(events, cid).get("usage") or {}).get("input") == 600)
            assert meta(events, cid)["usage"] == {"input": 600, "output": 12, "cacheWrite": None, "cacheRead": 300}
            assert meta(events, cid)["context"]["used"] == 912 and meta(events, cid)["context"]["window"] == 400000
            assert meta(events, cid)["title"] == "hello"
            await core.shutdown()

    run(scenario())


def test_a_new_session_starts_with_the_default_permission_of_this_engine_and_remembers_the_one_chosen(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            assert await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "full"}) == {"ok": True}
            await until(lambda: meta(events, cid)["settings"]["autonomy"] == "full")
            assert core.settings.chatgpt_autonomy == "full" and core.settings.autonomy == "off", "Droid's own level is not touched"
            cid2 = await new_chatgpt(core, events, tmp_path)
            assert meta(events, cid2)["settings"]["autonomy"] == "full"
            # a level of another engine's scale is not a level of this one
            assert core._open_settings("chatgpt:gpt-6.1-sol", {"autonomy": "readonly", "reasoning": "high"})["autonomy"] == "full"
            await core.shutdown()

    run(scenario())


def test_the_model_and_the_reasoning_level_are_changed_in_the_open_session(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            result = await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6-luna"})
            assert result["ok"] is True and not result.get("switched")
            await until(lambda: meta(events, cid)["settings"]["model"] == "chatgpt:gpt-6-luna")
            assert meta(events, cid)["settings"]["reasoning"] == "low", "the new model has no 'medium': it falls back to its own"
            controller = core._by_cid[cid].controller
            assert controller.model == "gpt-6-luna"
            await core.shutdown()

    run(scenario())


def test_saved_chatgpt_sessions_are_in_the_sidebar_and_open_from_there(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("first answer"))
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "first question"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid))
            session_id = core._by_cid[cid].controller.session_id
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh", {"id": str(tmp_path / "proj")})
            await until(lambda: ("cg:" + session_id) in events.last("sidebar")["sessions"])
            row = events.last("sidebar")["sessions"]["cg:" + session_id]
            assert row["title"] == "first question" and row["engine"] == "chatgpt"
            again = (await core.call("session.openSaved", {"id": "cg:" + session_id, "projectId": str(tmp_path / "proj"), "title": "first question"}))["id"]
            await until(lambda: meta(events, again).get("mark") == "ready")
            assert meta(events, again)["engine"] == "chatgpt" and meta(events, again)["nativeId"] == session_id
            assert [t.user_block.text for t in core._by_cid[again].controller.transcript.turns] == ["first question"]
            await core.call("project.refresh", {"id": str(tmp_path / "proj")})
            await asyncio.sleep(0.5)
            (project,) = events.last("sidebar")["projects"]
            assert project["sessionIds"] == [again], "the open session is the conversation's row, not a saved one as well"
            await core.shutdown()

    run(scenario())


def test_the_saved_sessions_of_the_plan_are_not_listed_while_its_switch_is_off(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("first answer"))
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "first question"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid))
            row = "cg:" + core._by_cid[cid].controller.session_id
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh", {"id": str(tmp_path / "proj")})
            await until(lambda: row in events.last("sidebar")["sessions"])
            await core.call("chatgpt.setEnabled", {"on": False})
            await core.call("project.refresh", {"id": str(tmp_path / "proj")})
            await until(lambda: row not in events.last("sidebar")["sessions"])
            await core.call("chatgpt.setEnabled", {"on": True})
            await core.call("project.refresh", {"id": str(tmp_path / "proj")})
            await until(lambda: row in events.last("sidebar")["sessions"])
            await core.shutdown()

    run(scenario())


def test_closing_the_application_closes_the_plan_conversations(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            controller = core._by_cid[cid].controller
            assert engine.controllers == [controller]
            await core.shutdown()
            assert controller.state is SessionState.CLOSED and engine.controllers == []

    run(scenario())


def test_the_speed_of_the_last_turn_is_in_the_session_and_comes_back_from_the_cache(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("sleep", 0.8), *say("An answer that is long enough to measure.", used=usage(500, 120))])
            core, _, events, engine = await chatgpt_core(tmp_path, fake, cache=cache)
            cid = await new_chatgpt(core, events, tmp_path)
            assert meta(events, cid).get("speed") is None, "nothing measured yet"
            controller = core._by_cid[cid].controller
            await core.call("session.send", {"id": cid, "text": "write"})
            await until(lambda: controller.state is SessionState.READY and controller._turn is None)
            await until(lambda: (meta(events, cid).get("speed") or {}).get("tokens") == 120)
            speed = meta(events, cid)["speed"]
            assert speed["tps"] > 0 and 0.8 <= speed["first"] < 5 and speed["seconds"] >= 0.8
            assert speed["genTps"] is None, "the answer came at once after the wait: nothing left to divide by"
            session_id = controller.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: ((cache.load_session("cg:" + session_id) or {}).get("meta") or {}).get("speed") == speed, timeout=10)
            await core.shutdown()

            again, _, events2, engine2 = await chatgpt_core(tmp_path, fake, cache=cache)
            cid2 = (await again.call("session.openSaved", {"id": "cg:" + session_id, "projectId": str(tmp_path / "proj"), "title": "x"}))["id"]
            await until(lambda: meta(events2, cid2).get("mark") == "ready")
            reopened = again._by_cid[cid2]
            await until(lambda: not reopened.preview and not reopened.dirty)  # (the real history has replaced the cached one)
            assert meta(events2, cid2)["speed"] == speed, "the last measurement stays until the next turn makes one"
            await again.shutdown()

    run(scenario())


def test_the_total_goes_on_across_a_restart_and_nothing_is_counted_twice(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("one", used=usage(1000, 10, cached=400)), say("two", used=usage(1200, 20, cached=600)))
            core, _, events, engine = await chatgpt_core(tmp_path, fake, cache=cache)
            cid = await new_chatgpt(core, events, tmp_path)
            controller = core._by_cid[cid].controller
            for text in ("a", "b"):
                await core.call("session.send", {"id": cid, "text": text})
                await until(lambda: controller.state is SessionState.READY and controller._turn is None)
            await until(lambda: (meta(events, cid).get("usage") or {}).get("input") == 1200)
            total = meta(events, cid)["usage"]
            assert total == {"input": 1200, "output": 30, "cacheWrite": None, "cacheRead": 1000}
            session_id = controller.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: cache.load_session("cg:" + session_id) is not None, timeout=10)
            await core.shutdown()

            fake.script(say("three", used=usage(500, 5, cached=100)))
            again, _, events2, engine2 = await chatgpt_core(tmp_path, fake, cache=cache)
            cid2 = (await again.call("session.openSaved", {"id": "cg:" + session_id, "projectId": str(tmp_path / "proj"), "title": "x"}))["id"]
            await until(lambda: meta(events2, cid2).get("mark") == "ready")
            assert meta(events2, cid2)["usage"] == total, "the same total, not twice it"
            await again.call("session.send", {"id": cid2, "text": "c"})
            await until(lambda: (meta(events2, cid2).get("usage") or {}).get("input") == 1600)
            assert meta(events2, cid2)["usage"] == {"input": 1600, "output": 35, "cacheWrite": None, "cacheRead": 1100}
            await again.shutdown()

    run(scenario())


def test_a_session_without_a_signed_in_account_says_so_instead_of_opening(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            await core.call("chatgpt.logout", {})
            try:
                await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": "chatgpt:gpt-6.1-sol"})
            except Exception as error:
                assert "重新登录" in str(error) or "登录" in str(error)
            else:
                raise AssertionError("a conversation must not open without a sign-in")
            assert not core.order
            await core.shutdown()

    run(scenario())


# ---------------------------------------------------------------------------------------------- engine changes


def test_a_droid_conversation_moves_to_chatgpt_plan_with_its_earlier_text_and_back(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Six."))
            core, factory, events, engine = await chatgpt_core(tmp_path, fake)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            await until(lambda: meta(events, cid).get("mark") == "ready")
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "what is 2+2"})
            await until(lambda: droid.active is not None)
            droid.active.push(delta("It is four.", "m1"), complete("m1"), success("It is four."))
            droid.active.end()
            await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
            result = await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6.1-sol"})
            assert result["switched"] is True
            assert core._by_cid[cid].engine == "chatgpt" and meta(events, cid)["engine"] == "chatgpt"
            assert any("已换到 GPT-6.1-Sol（ChatGPT Plan）" in p["text"] for p in events.of("notice"))
            assert meta(events, cid)["handoff"]["carried"] == 2
            await core.call("session.send", {"id": cid, "text": "and 3+3?"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "Six.")
            first = "".join(part["text"] for part in fake.requests[0]["input"][0]["content"])
            assert "what is 2+2" in first and "It is four." in first and "and 3+3?" in first
            assert meta(events, cid)["handoff"] is None
            assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "claude-haiku-5-5"}))["switched"] is True
            assert core._by_cid[cid].engine == "droid"
            await core.shutdown()

    run(scenario())


def test_a_chatgpt_plan_session_cannot_change_engine_while_it_runs(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script([("hold", "slow"), *say("late")])
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "wait"})
            await until(lambda: core._by_cid[cid].controller.state is SessionState.RUNNING and fake.requests)
            try:
                await core.call("session.choose", {"id": cid, "field": "model", "value": "claude-haiku-5-5"})
            except Exception as error:
                assert "本轮还在运行" in str(error)
            else:
                raise AssertionError("an engine change while a turn runs must be refused")
            await core.call("session.stop", {"id": cid})
            fake.release("slow")
            await core.shutdown()

    run(scenario())


def test_the_shared_memory_reaches_a_chatgpt_plan_conversation_when_the_engine_is_switched_on(tmp_path):
    async def scenario():
        from desktop.core.memory.launch import MemoryLaunch

        memory = MemoryLaunch(tmp_path / "memory", enabled=lambda engine: engine in ("chatgpt",))
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            args = {"title": "Release steps", "text": "Tag, then push.", "tags": ["release"]}
            fake.script(call("mcp__wittycode-memory__memory_write", args, call_id="c1"), say("noted"))
            core, _, events, engine = await chatgpt_core(tmp_path, fake, memory=memory)
            assert engine.memory is memory
            cid = await new_chatgpt(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "remember the release steps"})
            controller = core._by_cid[cid].controller
            await until(lambda: controller.state is SessionState.READY and controller._turn is None and answer_of(events, cid) == "noted")
            (note,) = memory.store.entries(project=None)
            assert note.title == "Release steps" and note.source["engine"] == "chatgpt"
            # the page learns whose note it is: this conversation
            state = await core.call("memory.list", {})
            entry = next(e for e in state["entries"] if e["title"] == "Release steps")
            assert entry["source"]["kind"] == "agent" and entry["source"]["engine"] == "chatgpt"
            await core.shutdown()

    run(scenario())


def pending_card(events, cid):
    for payload in reversed(events.of("turns")):
        if payload["id"] == cid and payload["turns"]:
            return next((p for p in payload["turns"][-1].get("prompts", []) if p["kind"] == "question" and p["status"] == "pending"), None)
    return None


def test_a_question_of_the_model_reaches_the_page_as_a_card_and_the_answer_goes_back(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            ask = {"questions": [{"question": "Which database?", "header": "Storage", "options": ["SQLite", "Postgres"]}]}
            fake.script(call("ask_user", ask, call_id="c1", text="One decision first."), say("SQLite then."))
            core, _, events, engine = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            assert await core.call("session.send", {"id": cid, "text": "set up storage"}) == {"ok": True}
            await until(lambda: pending_card(events, cid) is not None)
            card = pending_card(events, cid)
            assert card["questions"] == [{"topic": "Storage", "question": "Which database?", "options": ["SQLite", "Postgres"], "multiSelect": False}]
            assert meta(events, cid)["mark"] == "attention", "the tab says the session is waiting for the user"
            assert await core.call("session.answerQuestion", {"id": cid, "promptId": card["id"], "answers": ["Postgres"]}) == {"ok": True}
            assert await core.call("session.answerQuestion", {"id": cid, "promptId": card["id"], "answers": ["SQLite"]}) == {"ok": False}, "a card is answered once"
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid).endswith("SQLite then."))
            assert "Answer: Postgres" in fake.requests[1]["input"][-1]["output"]
            await core.shutdown()

    run(scenario())
