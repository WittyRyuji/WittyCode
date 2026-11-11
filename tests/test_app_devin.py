"""The Devin engine inside the application: models, sessions, the sidebar, settings, the token total, an engine change.

The engine here is the real one, talking to ``tests/fake_devin.py``; Droid's side is the usual fake."""

from __future__ import annotations

import asyncio

from desktop.core.cache import Cache
from desktop.core.session_manager import SessionState
from tests.app_support import make_core, ready_core
from tests.devin_support import make_engine
from tests.fakes import until
from tests.support import complete, delta, success


def run(coro):
    return asyncio.run(coro)


async def devin_core(tmp_path, **options):
    env = options.pop("env", {})
    account = options.pop("account", "signed-in")
    harness = make_engine(tmp_path, account=account, **env)
    core, factory, events, platform, _ = await ready_core(tmp_path, devin_engine=harness.engine, **options)
    harness.engine.settings = core.settings  # (one settings object in the application)
    return core, factory, events, harness


def sessions_of(events):
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def meta(events, cid):
    return sessions_of(events).get(cid, {})


async def new_devin(core, events, tmp_path, model="devin:swe-2-high", **more):
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


# ---------------------------------------------------------------------------------------------- models and accounts


def test_the_devin_models_are_in_the_catalog_under_their_own_prefix(tmp_path):
    async def scenario():
        core, _, events, _ = await devin_core(tmp_path)
        models = {m["id"]: m for m in events.last("models")["models"] if m["source"] == "devin"}
        assert list(models) == ["devin:adaptive", "devin:swe-2-high", "devin:claude-opus-5-5-medium", "devin:glm-5-2"]
        swe = models["devin:swe-2-high"]
        assert (swe["label"], swe["efforts"], swe["defaultEffort"], swe["window"], swe["note"], swe["group"]) == (
            "SWE-2", ["medium", "high", "max"], "high", 262000, "Free", "Cognition")
        assert models["devin:glm-5-2"]["noImages"] is True and "noImages" not in swe
        assert models["devin:adaptive"]["efforts"] == []
        target = core.catalog.target("devin:claude-opus-5-5-medium")
        assert (target.engine, target.model) == ("devin", "claude-opus-5-5-medium")
        assert core.catalog.engine_of("devin:swe-2-high") == "devin" and core.catalog.source_of("devin:swe-2-high") == "devin"
        assert core.catalog.page_id("devin", "glm-5-2") == "devin:glm-5-2"
        # a list read back from the cache before the engine is connected still leads to the right engine
        from desktop.core.catalog import Catalog

        cached = Catalog.from_items(events.last("models")["models"])
        assert cached.engine_of("devin:swe-2-high") == "devin" and cached.page_id("devin", "swe-2-high") == "devin:swe-2-high"
        await core.shutdown()

    run(scenario())


def test_the_devin_account_is_told_at_init_and_then_read(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        core, _, events, _, _ = make_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        init = await core.call("app.init")
        assert init["devin"] == {"cli": "found", "auth": "checking", "enabled": True}
        assert init["settings"]["devinPath"] == ""
        await until(lambda: any("devin" in p for p in events.of("accounts")))
        account = [p["devin"] for p in events.of("accounts") if "devin" in p][-1]
        assert account["auth"] == "signed-in" and account["email"] == "me@example.com" and account["plan"] == "Devin Pro" and account["enabled"] is True
        assert (await core.call("devin.account"))["auth"] == "signed-in"
        # what was read is what the next start shows first, before anything is connected
        assert (await core.call("app.init"))["devin"] == {"cli": "found", "auth": "signed-in", "email": "me@example.com", "plan": "Devin Pro", "enabled": True}
        await core.shutdown()

    run(scenario())


def test_without_the_engine_the_account_is_missing_and_nothing_else_changes(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        init = await core.call("app.init")
        assert init["devin"] == {"cli": "missing", "auth": "signed-out", "enabled": True}
        assert not [m for m in events.last("models")["models"] if m["source"] == "devin"]
        try:
            await core.call("devin.login")
        except Exception as error:
            assert "没有找到 Devin" in str(error)
        else:
            raise AssertionError("signing in without a CLI must say so")
        await core.shutdown()

    run(scenario())


def test_signing_in_and_out_changes_the_models_that_are_offered(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path, account="signed-out", env={"FAKE_DEVIN_LOGIN_DELAY": 0.05})
        assert not [m for m in events.last("models")["models"] if m["source"] == "devin"], "signed out: no models"
        account = await core.call("devin.login")
        assert account["auth"] == "signed-in" and account["enabled"] is True
        await until(lambda: [m for m in events.last("models")["models"] if m["source"] == "devin"])
        account = await core.call("devin.logout")
        assert account["auth"] == "signed-out"
        await until(lambda: not [m for m in events.last("models")["models"] if m["source"] == "devin"])
        await core.shutdown()

    run(scenario())


def test_hiding_the_devin_models_with_the_switch_takes_them_and_the_sessions_away(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path, projects=True)
        cid = await new_devin(core, events, tmp_path)
        await core.call("devin.setEnabled", {"on": False})
        await until(lambda: not [m for m in events.last("models")["models"] if m["source"] == "devin"])
        assert core.settings.devin_enabled is False and events.last("accounts")["devin"]["enabled"] is False
        await core.call("devin.setEnabled", {"on": True})
        await until(lambda: [m for m in events.last("models")["models"] if m["source"] == "devin"])
        assert cid in core._by_cid  # (an open conversation is not closed by it)
        await core.shutdown()

    run(scenario())


def test_the_program_path_is_a_setting_that_makes_the_engine_look_again(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        view = await core.call("settings.set", {"key": "devinPath", "value": "  D:\\tools\\devin.exe "})
        assert view["devinPath"] == "D:\\tools\\devin.exe" and core.settings.devin_path == "D:\\tools\\devin.exe"
        await until(lambda: harness.engine._models is None)  # (what was read from the old program is dropped)
        assert events.last("settings")["settings"]["devinPath"] == "D:\\tools\\devin.exe"
        await core.shutdown()
        # the process of the old path was let go in the middle of all this: shutting down sees to it that it is gone
        assert harness.clients and all(c._process.returncode is not None for c in harness.clients), "no process is left running"

    run(scenario())


# ---------------------------------------------------------------------------------------------- a session


def test_a_devin_session_opens_runs_and_reports_its_settings_and_total(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path, "devin:claude-opus-5-5-medium")
        conv = core._by_cid[cid]
        assert conv.engine == "devin"
        patch = meta(events, cid)
        assert patch["engine"] == "devin" and patch["nativeId"] == conv.controller.session_id
        # (the level is the one the user's defaults ask for, when the model offers it: Opus offers "low")
        assert patch["settings"] == {"model": "devin:claude-opus-5-5-medium", "reasoning": "low", "autonomy": "code", "mode": "auto"}
        assert await core.call("session.send", {"id": cid, "text": "hello"}) == {"ok": True}
        await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "echo: hello")
        # the figures of the prompt: input without the cache, output, the cache read; and the context of the last call
        await until(lambda: (meta(events, cid).get("usage") or {}).get("input") == 900)
        assert meta(events, cid)["usage"] == {"input": 900, "output": 12, "cacheWrite": None, "cacheRead": 300}
        assert meta(events, cid)["context"]["used"] == 1212 and meta(events, cid)["context"]["window"] == 1000000
        assert meta(events, cid)["title"] == "hello"
        await core.shutdown()

    run(scenario())


def test_each_message_to_devin_asks_for_chinese_thinking_until_the_setting_is_off(tmp_path):
    async def scenario():
        from desktop.core.language import THINK_CHINESE

        core, _, events, harness = await devin_core(tmp_path)
        await core.call("settings.set", {"key": "thinkChinese", "value": True})
        assert core.settings.think_chinese is True and core.settings_view()["thinkChinese"] is True
        cid = await new_devin(core, events, tmp_path)

        def sent(index):
            return "".join(block.get("text", "") for block in harness.requests("session/prompt")[index]["prompt"])

        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid).startswith("echo: hello"))
        assert THINK_CHINESE in sent(-1) and "<wittycode-context>" in sent(-1), "after the typed text, in the block the page never shows"
        await core.call("settings.set", {"key": "thinkChinese", "value": False})
        assert core.settings.think_chinese is False and core.settings_view()["thinkChinese"] is False
        await core.call("session.send", {"id": cid, "text": "again"})
        await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid).startswith("echo: again"))
        assert THINK_CHINESE not in sent(-1)
        await core.shutdown()

    run(scenario())


def test_the_total_moves_while_a_devin_turn_runs_and_is_not_counted_twice_when_it_ends(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "[slow] wait"})
        live = {"input": 900, "output": 10, "cacheWrite": None, "cacheRead": 100}  # (the turn's one model call so far)
        await until(lambda: meta(events, cid).get("usage") == live)
        assert meta(events, cid)["mark"] == "running", "the figure is shown while the turn still runs"
        assert core._by_cid[cid].ledger.total is None, "shown, but not booked before Devin's own figure for the prompt"
        await core.call("session.stop", {"id": cid})
        await until(lambda: meta(events, cid).get("mark") == "ready")
        await asyncio.sleep(0.2)
        assert meta(events, cid)["usage"] == live, "the prompt's figure takes the place of what was shown: nothing twice"
        await core.shutdown()

    run(scenario())


def test_a_new_session_starts_with_the_default_permission_of_devin_and_remembers_the_one_chosen(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        assert meta(events, cid)["settings"]["autonomy"] == "code"
        assert await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "smart"}) == {"ok": True}
        await until(lambda: meta(events, cid)["settings"]["autonomy"] == "smart")
        assert core.settings.devin_autonomy == "smart" and core.settings.autonomy == "off", "Droid's own level is not touched"
        cid2 = await new_devin(core, events, tmp_path)
        assert meta(events, cid2)["settings"]["autonomy"] == "smart"
        assert harness.state()["sessions"][core._by_cid[cid2].controller.session_id]["mode"] == "smart"
        # a level of another engine's scale is not a level of this one
        assert core._open_settings("devin:swe-2-high", {"autonomy": "readonly", "reasoning": "high"})["autonomy"] == "smart"
        await core.shutdown()

    run(scenario())


def test_the_model_and_the_thought_level_are_changed_in_the_open_session(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        assert await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:glm-5-2"}) == {"ok": True}
        await until(lambda: meta(events, cid)["settings"]["model"] == "devin:glm-5-2")
        assert meta(events, cid)["settings"]["reasoning"] == "high"
        assert await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "max"}) == {"ok": True}
        await until(lambda: meta(events, cid)["settings"]["reasoning"] == "max")
        # a model without thought levels: the level is dropped, and said so
        assert await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:adaptive"}) == {"ok": True}
        await until(lambda: meta(events, cid)["settings"] == {"model": "devin:adaptive", "reasoning": "", "autonomy": "code", "mode": "auto"})
        assert any("没有推理档位" in p["text"] for p in events.of("notice"))
        await core.shutdown()

    run(scenario())


def test_an_approval_is_answered_from_the_page(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "[approve] clean"})
        await until(lambda: meta(events, cid).get("mark") == "attention")
        turn = [t for payload in events.of("turns") if payload["id"] == cid for t in payload["turns"]][-1]
        (prompt,) = [p for p in turn["prompts"] if p["status"] == "pending"]
        assert [o["value"] for o in prompt["options"]] == ["allow_once", "allow_session", "allow_always", "switch_bypass", "cancel"]
        assert meta(events, cid)["workingLabel"] == "等待你的确认"
        assert await core.call("session.answerPermission", {"id": cid, "promptId": prompt["id"], "value": "allow_session"}) == {"ok": True}
        await until(lambda: meta(events, cid).get("mark") == "ready")
        assert answer_of(events, cid).endswith("approved: allow_session")
        await core.shutdown()

    run(scenario())


def test_stop_ends_the_turn_and_a_waiting_message_goes_on(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "[slow] wait"})
        await until(lambda: meta(events, cid).get("mark") == "running" and core._by_cid[cid].controller.transcript.turns[-1].tools)
        assert await core.call("session.send", {"id": cid, "text": "after that", "how": "queue"}) == {"ok": True, "queued": True}
        await until(lambda: [m["text"] for m in meta(events, cid).get("queued") or []] == ["after that"])
        await core.call("session.stop", {"id": cid})
        await until(lambda: answer_of(events, cid) == "echo: after that")  # (the queue is still sent after a stop)
        await until(lambda: meta(events, cid).get("mark") == "ready" and not meta(events, cid).get("queued"))
        statuses = [t["status"] for payload in events.of("turns") if payload["id"] == cid for t in payload["turns"]]
        assert "interrupted" in statuses
        await core.shutdown()

    run(scenario())


def test_enter_can_steer_the_running_turn_and_it_is_one_turn_with_the_message_in_it(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "[slow] wait"})
        await until(lambda: meta(events, cid).get("mark") == "running" and core._by_cid[cid].controller.transcript.turns[-1].tools)
        assert await core.call("session.send", {"id": cid, "text": "go ahead", "how": "steer"}) == {"ok": True, "steered": True}
        await until(lambda: meta(events, cid).get("mark") == "ready")
        turns = core._by_cid[cid].controller.transcript.turns
        assert len(turns) == 1 and answer_of(events, cid).endswith("got: go ahead")
        blocks = last_turn_blocks(events, cid)
        assert [b["text"] for b in blocks if b["kind"] == "user" and b.get("steer")] == ["go ahead"]
        await core.shutdown()

    run(scenario())


# ---------------------------------------------------------------------------------------------- saved sessions


async def listed(core, events, tmp_path):
    proj = tmp_path / "proj"
    await core.call("project.refresh", {"id": str(proj)})
    await until(lambda: any(row["engine"] == "devin" for row in events.last("sidebar")["sessions"].values()))
    return {row_id: row for row_id, row in events.last("sidebar")["sessions"].items() if row["engine"] == "devin"}


def test_saved_devin_sessions_are_in_the_sidebar_and_open_from_there(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "first question"})
        await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid))
        session_id = core._by_cid[cid].controller.session_id
        await core.call("session.close", {"id": cid})
        rows = await listed(core, events, tmp_path)
        assert list(rows) == ["dv:" + session_id] and rows["dv:" + session_id]["title"] == "first question"
        again = (await core.call("session.openSaved", {"id": "dv:" + session_id, "projectId": str(tmp_path / "proj"), "title": "first question"}))["id"]
        await until(lambda: meta(events, again).get("mark") == "ready")
        assert meta(events, again)["engine"] == "devin" and meta(events, again)["nativeId"] == session_id
        assert [t.user_block.text for t in core._by_cid[again].controller.transcript.turns] == ["first question"]
        # an open saved session is listed once, as the conversation
        await core.call("project.refresh", {"id": str(tmp_path / "proj")})
        await asyncio.sleep(0.5)
        (project,) = events.last("sidebar")["projects"]
        assert project["sessionIds"] == [again], "the open session is the conversation's row, not a saved one as well"
        assert (await core.call("session.openSaved", {"id": "dv:" + session_id, "projectId": str(tmp_path / "proj")}))["id"] == again
        await core.shutdown()

    run(scenario())


def test_the_total_goes_on_across_a_restart_and_nothing_is_counted_twice(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, _, events, harness = await devin_core(tmp_path, cache=cache)
        cid = await new_devin(core, events, tmp_path)
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": "[full] one"})
        await until(lambda: controller.state is SessionState.READY and (meta(events, cid).get("usage") or {}).get("input") == 1400)
        await core.call("session.send", {"id": cid, "text": "two"})
        await until(lambda: controller.state is SessionState.READY and (meta(events, cid).get("usage") or {}).get("input") == 2300)
        total = meta(events, cid)["usage"]
        assert total == {"input": 2300, "output": 62, "cacheWrite": None, "cacheRead": 1400}
        session_id = core._by_cid[cid].controller.session_id
        await core.call("session.close", {"id": cid})
        await until(lambda: cache.load_session("dv:" + session_id) is not None, timeout=10)
        await core.shutdown()

        # a new run of the application, with a new process: the replay carries the same two figures, under the same names
        again, _, events2, harness2 = await devin_core(tmp_path, cache=cache, env={})
        harness2.engine.settings = again.settings
        cid2 = (await again.call("session.openSaved", {"id": "dv:" + session_id, "projectId": str(tmp_path / "proj"), "title": "x"}))["id"]
        await until(lambda: meta(events2, cid2).get("mark") == "ready")
        assert meta(events2, cid2)["usage"] == total, "the same total, not twice it"
        await again.call("session.send", {"id": cid2, "text": "three"})
        await until(lambda: (meta(events2, cid2).get("usage") or {}).get("input") == 3200)
        assert meta(events2, cid2)["usage"] == {"input": 3200, "output": 74, "cacheWrite": None, "cacheRead": 1700}
        await again.shutdown()

    run(scenario())


# ---------------------------------------------------------------------------------------------- engine changes


def test_a_droid_conversation_moves_to_devin_with_its_earlier_text_and_keeps_its_total(tmp_path):
    async def scenario():
        core, factory, events, harness = await devin_core(tmp_path)
        cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
        await until(lambda: meta(events, cid).get("mark") == "ready")
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "what is 2+2"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("It is four.", "m1"), complete("m1"), success("It is four."))
        droid.active.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        result = await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
        assert result["switched"] is True
        assert core._by_cid[cid].engine == "devin" and meta(events, cid)["engine"] == "devin"
        assert any("已换到 SWE-2（Devin）" in p["text"] for p in events.of("notice"))
        assert meta(events, cid)["handoff"]["carried"] == 2
        await core.call("session.send", {"id": cid, "text": "and 3+3?"})
        await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid).endswith("and 3+3?"))
        (sent,) = harness.requests("session/prompt")
        assert "what is 2+2" in sent["prompt"][0]["text"] and "It is four." in sent["prompt"][0]["text"] and sent["prompt"][0]["text"].endswith("and 3+3?")
        assert meta(events, cid)["handoff"] is None
        # and back to Droid
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "claude-haiku-5-5"}))["switched"] is True
        assert core._by_cid[cid].engine == "droid"
        await core.shutdown()

    run(scenario())


def test_a_conversation_goes_between_devin_and_codex_too(tmp_path):
    async def scenario():
        core, factory, events, harness = await devin_core(tmp_path, codex=True)
        cid = await new_devin(core, events, tmp_path)
        controller = core._by_cid[cid].controller
        await core.call("session.send", {"id": cid, "text": "hello"})
        await until(lambda: controller.state is SessionState.READY and answer_of(events, cid) == "echo: hello")
        codex = core.codex
        result = await core.call("session.choose", {"id": cid, "field": "model", "value": "codex:gpt-5.3-codex"})
        assert result["switched"] is True and core._by_cid[cid].engine == "codex"
        handoff = codex.opened[-1]["handoff"]
        assert handoff.carried == 2 and "echo: hello" in handoff.prefix, "what Devin said goes to Codex"
        assert any("已换到 GPT-5.3 Codex（Codex）" in p["text"] for p in events.of("notice"))
        # and from Codex to Devin: a new Devin session, the Codex session's text with it
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        result = await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:claude-opus-5-5-medium"})
        assert result["switched"] is True and core._by_cid[cid].engine == "devin"
        assert core._by_cid[cid].controller.session_id != controller.session_id
        assert core._by_cid[cid].controller.handoff.carried == 2
        # the old Devin session is not listed as a session of its own while its conversation is open
        assert controller.session_id in core._by_cid[cid].hidden
        await core.shutdown()

    run(scenario())


def test_a_devin_model_that_cannot_see_pictures_says_so_when_the_conversation_moves_there(tmp_path):
    async def scenario():
        core, factory, events, harness = await devin_core(tmp_path)
        cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
        await until(lambda: meta(events, cid).get("mark") == "ready")
        from tests.support import tiny_png
        import base64

        await core.call("attachments.add", {"id": cid, "files": [{"name": "shot.png", "data": base64.b64encode(tiny_png()).decode()}]})
        droid = factory.sessions[-1]
        await core.call("session.send", {"id": cid, "text": "look"})
        await until(lambda: droid.active is not None)
        droid.active.push(delta("seen", "m1"), complete("m1"), success("seen"))
        droid.active.end()
        await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
        asked = await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:glm-5-2"})
        (item,) = asked["choose"]["attachments"]
        assert item["as"] == "" and "不支持图片" in item["reason"]
        asked = await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
        assert asked["choose"]["attachments"][0]["as"] == "image"
        await core.shutdown()

    run(scenario())


def test_a_devin_session_cannot_change_engine_while_it_runs(tmp_path):
    async def scenario():
        core, _, events, harness = await devin_core(tmp_path)
        cid = await new_devin(core, events, tmp_path)
        await core.call("session.send", {"id": cid, "text": "[slow] wait"})
        await until(lambda: core._by_cid[cid].controller.state is SessionState.RUNNING)
        try:
            await core.call("session.choose", {"id": cid, "field": "model", "value": "claude-haiku-5-5"})
        except Exception as error:
            assert "本轮还在运行" in str(error)
        else:
            raise AssertionError("an engine change while a turn runs must be refused")
        await core.call("session.stop", {"id": cid})
        await core.shutdown()

    run(scenario())
