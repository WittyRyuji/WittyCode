"""After a ChatGPT Plan turn the application asks the plan what is worth keeping and writes what is sure enough into the memory
itself; the user looks at the week's notes once, on the memory page."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque

import pytest

from desktop.core.app_core import AppError
from desktop.core.memory.curator import Exchange
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.proposals import ProposalStore, make
from desktop.core.memory.store import APP, MemoryStore
from tests.app_support import ready_core
from tests.chatgpt_support import ACCESS_TOKEN, make_engine
from tests.fake_responses import FakeResponses, say
from tests.fakes import until

ASK = "这个项目怎么发布到测试环境，步骤是什么"
ANSWER = "步骤是：先合并分支，再运行 make ship-it，最后检查部署状态，确认测试环境能打开。"
FOUND = {
    "title": "发布到测试环境",
    "text": "发布到测试环境用 make ship-it，之后要检查部署状态。",
    "kind": "convention",
    "scope": "project",
    "tags": ["发布", "deploy"],
    "why": "步骤不在代码里",
    "confidence": 0.9,
}


def run(coro):
    return asyncio.run(coro)


def reply(*notes):
    return json.dumps({"notes": list(notes)}, ensure_ascii=False)


async def started(tmp_path, fake, memory=None):
    memory = memory or MemoryLaunch(tmp_path / "memory", python=sys.executable)
    engine = make_engine(tmp_path, fake)
    core, factory, events, _, _ = await ready_core(tmp_path, chatgpt_engine=engine, memory=memory)
    engine.settings = core.settings
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": "chatgpt:gpt-6.1-sol"}))["id"]
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")))
    return core, events, cid, memory


async def talk(core, events, cid, text=ASK):
    before = len([p for p in events.of("turns") if p["id"] == cid])
    await core.call("session.send", {"id": cid, "text": text})
    await until(lambda: any(p["id"] == cid and p["patch"].get("mark") == "ready" for p in events.of("session")[-3:]) and len([p for p in events.of("turns") if p["id"] == cid]) > before)


def notes_of(memory):
    return MemoryStore(memory.root).entries()


def test_a_turn_leaves_a_note_the_application_wrote_itself_and_the_user_confirms_it_in_the_review(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say(ANSWER), say(reply(FOUND)))
            core, events, cid, memory = await started(tmp_path, fake)
            await talk(core, events, cid)
            await until(lambda: len(notes_of(memory)) == 1)
            assert len(fake.requests) == 2 and fake.requests[1]["model"] == "gpt-6-luna", "one small request on the fast model"
            assert fake.requests[1]["store"] is False and "tools" not in fake.requests[1]
            prompt = fake.requests[1]["input"][0]["content"][0]["text"]
            assert ASK in prompt and ANSWER in prompt and "proj" in prompt
            (entry,) = notes_of(memory)
            assert (entry.title, entry.kind, entry.scope, entry.source) == ("发布到测试环境", "convention", "project", APP), "in the memory at once, as the application's own"
            assert core.memory.review.count_pending() == 1

            state = (await core.call("app.init"))["memory"]
            assert state["pending"] == 0 and state["unseen"] == 0, "not due yet, and the application's own notes are not news"
            listed = await core.call("memory.review", {})
            (item,) = listed["items"]
            assert (item["id"], item["engine"], item["why"], item["projectName"], item["suggestKeep"]) == (entry.id, "chatgpt", "步骤不在代码里", "proj", True)
            assert listed["due"] is False and listed["pending"] == 1 and listed["total"] == 1 and listed["labels"] == 0

            done = await core.call("memory.reviewApply", {"keep": [entry.id], "drop": [], "edits": {entry.id: {"title": "发布步骤", "kind": "fact"}}})
            assert (done["kept"], done["dropped"], done["labels"], done["left"]) == (1, 0, 1, 0)
            assert events.of("memory")[-1]["pending"] == 0
            (entry,) = notes_of(memory)
            assert (entry.title, entry.kind, entry.text) == ("发布步骤", "fact", FOUND["text"]) and entry.status == "active"
            assert (await core.call("memory.review", {}))["items"] == []
            await core.shutdown()

    run(scenario())


def test_a_note_dropped_in_the_review_is_forgotten_and_does_not_come_back(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say(ANSWER), say(reply(FOUND)), say(ANSWER), say(reply(FOUND)))
            core, events, cid, memory = await started(tmp_path, fake)
            await talk(core, events, cid)
            await until(lambda: len(notes_of(memory)) == 1)
            (entry,) = notes_of(memory)
            done = await core.call("memory.reviewApply", {"keep": [], "drop": [entry.id]})
            assert (done["kept"], done["dropped"]) == (0, 1)
            assert MemoryStore(memory.root).read(entry.id).status == "forgotten", "forgotten, not deleted: it can be restored"
            core.curator._last.clear()  # (the quiet time between two requests about one conversation)
            await talk(core, events, cid, "那回滚呢，出了问题怎么退回去")
            await until(lambda: len(fake.requests) == 4)
            await asyncio.sleep(0.2)
            assert [e.status for e in MemoryStore(memory.root).entries(status=None)] == ["forgotten"], "dropped once, not written again"
            assert core.memory.review.count_pending() == 0
            await core.shutdown()

    run(scenario())


def test_a_note_the_model_is_not_sure_of_is_not_written_and_the_review_counts_it(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say(ANSWER), say(reply({**FOUND, "confidence": 0.2}, {**FOUND, "title": "回滚", "text": "回滚用 make rollback。", "confidence": 0.8})))
            core, events, cid, memory = await started(tmp_path, fake)
            await talk(core, events, cid)
            await until(lambda: len(notes_of(memory)) == 1)
            assert [e.title for e in notes_of(memory)] == ["回滚"], "the sure one was written, the other was left"
            assert (await core.call("memory.review", {}))["skipped"] == 1
            await core.shutdown()

    run(scenario())


def test_suggestions_that_waited_before_the_application_wrote_on_its_own_become_notes_in_the_first_review(tmp_path):
    async def scenario():
        memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
        waiting = ProposalStore(memory.root)
        waiting.add([
            make(title="提交信息用中文", text="这个项目的提交信息用中文写。", scope="global", project="", kind="convention", tags=["提交"], engine="chatgpt", conversation="测试对话", why="用户明确说了", now=time.time() - 60),
        ])
        core, factory, events, _, _ = await ready_core(tmp_path, memory=memory)
        (entry,) = notes_of(memory)
        assert (entry.title, entry.source) == ("提交信息用中文", APP) and waiting.count() == 0
        (item,) = (await core.call("memory.review", {}))["items"]
        assert item["id"] == entry.id and item["conversation"] == "测试对话"
        await core.call("app.init")
        assert len(notes_of(memory)) == 1, "moved once"
        await core.shutdown()

    run(scenario())


def test_a_review_that_is_not_in_shape_is_refused_and_changes_nothing(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say(ANSWER), say(reply(FOUND)))
            core, events, cid, memory = await started(tmp_path, fake)
            await talk(core, events, cid)
            await until(lambda: len(notes_of(memory)) == 1)
            (entry,) = notes_of(memory)
            for params in (
                {"keep": entry.id, "drop": []},
                {"keep": [entry.id], "drop": [entry.id]},
                {"keep": [5], "drop": []},
                {"keep": [entry.id], "drop": [], "edits": [1]},
                {"keep": [entry.id], "drop": [], "edits": {entry.id: {"colour": "red"}}},
            ):
                with pytest.raises(AppError):
                    await core.call("memory.reviewApply", params)
            with pytest.raises(AppError):
                await core.call("memory.review", {"size": 0})
            assert core.memory.review.count_pending() == 1 and notes_of(memory)[0].status == "active"
            with pytest.raises(AppError):
                await core.call("memory.accept", {"id": "p-nothing"})
            await core.shutdown()

    run(scenario())


def test_the_switch_and_the_memory_switch_stop_the_questions(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say(ANSWER), say(reply(FOUND)), say(ANSWER), say(reply(FOUND)))
            core, events, cid, memory = await started(tmp_path, fake)
            assert (await core.call("app.init"))["settings"]["memoryCurate"] is True and (await core.call("app.init"))["settings"]["memoryCapture"] is True
            await core.call("settings.set", {"key": "memoryCurate", "value": False})
            assert core.settings.memory_curate is False
            await talk(core, events, cid)
            await asyncio.sleep(0.3)
            assert len(fake.requests) == 1 and notes_of(memory) == [], "only the user's own turn went out"
            await core.call("settings.set", {"key": "memoryCurate", "value": True})
            await core.call("settings.set", {"key": "memoryEnabled", "value": False})
            await talk(core, events, cid, "再问一个别的问题，看看会怎样")
            await asyncio.sleep(0.3)
            assert len(fake.requests) == 2 and notes_of(memory) == []
            for key, value in (("memoryCurate", "yes"), ("memoryCapture", 1)):
                before = (core.settings.memory_curate, core.settings.memory_capture)
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": key, "value": value})
                assert (core.settings.memory_curate, core.settings.memory_capture) == before, "not a boolean: refused"
            await core.shutdown()

    run(scenario())


def test_a_note_the_user_asks_for_in_so_many_words_is_a_lesson_while_the_switch_is_on(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("好的，记下了。"), say("好的，这次不记。"))
            memory = MemoryLaunch(tmp_path / "memory", python=sys.executable)
            core, events, cid, _ = await started(tmp_path, fake, memory)
            memory.learn = lambda: core.settings.memory_curate
            memory.capture = lambda: True
            await talk(core, events, cid, "记住：发布到测试环境用 make ship-it，之后要检查部署状态。")
            await until(lambda: len(notes_of(memory)) == 1)
            assert memory.learner.labels == 1, "a note asked for is a note the user wants"
            await core.call("settings.set", {"key": "memoryCurate", "value": False})
            core.curator._last.clear()
            await talk(core, events, cid, "记住：回滚用 make rollback，回滚之后要重新检查部署状态。")
            await until(lambda: len(notes_of(memory)) == 2)
            assert memory.learner.labels == 1, "the switch is off: not learned from"
            await core.shutdown()

    run(scenario())


def test_a_turn_that_failed_or_was_restored_is_not_asked_about(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("好"), say(reply(FOUND)))
            core, events, cid, _ = await started(tmp_path, fake)
            await talk(core, events, cid, "好")
            await asyncio.sleep(0.3)
            assert len(fake.requests) == 1, "a short exchange is not worth a request"
            await core.shutdown()

    run(scenario())


def test_an_engine_that_cannot_be_asked_quietly_is_not_asked_and_nothing_goes_to_another_provider(tmp_path):
    async def scenario():
        core, factory, events, _, _ = await ready_core(tmp_path, plan=False, memory=MemoryLaunch(tmp_path / "memory", python=sys.executable))
        assert await core._extract_for_memory("some-other-engine", str(tmp_path), "prompt") is None
        assert await core._extract_for_memory("chatgpt", str(tmp_path), "prompt") is None, "no own loop here"
        assert await core._extract_for_memory("devin", str(tmp_path), "prompt") is None, "no Devin engine here"
        await core.shutdown()

    run(scenario())


def test_a_turn_of_an_engine_without_a_side_channel_does_not_take_the_curators_allowance(tmp_path):
    """An engine that cannot be asked aside (``_extract_for_memory``: here Devin, which this core does not have) must not
    count as "asked": a few of its turns would use the hour's limit and leave the others unread."""

    async def scenario():
        from tests.app_support import make_core

        core, _, _, _, _ = make_core(tmp_path, memory=MemoryLaunch(tmp_path / "memory", python=sys.executable))
        assert core.curator is not None
        assert core.curator.enabled("devin") is False, "nothing would come of asking"
        assert core.curator.worth_asking(Exchange(engine="devin", project=str(tmp_path), conversation="c", row="", user=ASK, assistant=ANSWER)) is False
        assert core.curator._asked == deque(), "the allowance is untouched"
        await core.shutdown()

    run(scenario())


def test_devin_is_asked_through_its_own_side_channel(tmp_path, monkeypatch):
    """The question goes to the engine of the conversation, over its side channel (core/devin/side)."""
    from types import SimpleNamespace

    from desktop.core.devin import side as devin_side
    from tests.app_support import make_core

    async def scenario():
        calls: list[tuple[str, str, str]] = []

        async def fake_ask(engine, project, prompt, **_):
            calls.append((engine.marker, project, prompt))
            return '{"notes": []}'

        monkeypatch.setattr(devin_side, "ask", fake_ask)
        quiet = lambda *_args, **_kwargs: None  # noqa: E731 (a stand-in engine only takes listeners and a shutdown)

        async def close_all():
            return None

        core, _, _, _, _ = make_core(
            tmp_path,
            devin_engine=SimpleNamespace(marker="devin", add_listener=quiet, add_account_listener=quiet, close_all=close_all),
            memory=MemoryLaunch(tmp_path / "memory", python=sys.executable),
        )
        project = str(tmp_path / "proj")
        assert await core._extract_for_memory("devin", project, "p2") == '{"notes": []}'
        assert calls == [("devin", project, "p2")]
        await core.shutdown()

    run(scenario())
