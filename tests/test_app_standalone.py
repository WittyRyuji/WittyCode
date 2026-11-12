"""A fresh install: no default model chosen yet, only what the user signed in to. It starts quietly, offers what there is,
starts conversations of it, and says what to do when there is nothing yet."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import AppSettings
from tests.app_support import ready_core
from tests.chatgpt_support import ACCESS_TOKEN, make_engine
from tests.fake_responses import FakeResponses, say
from tests.fakes import until
from tests.test_app_chatgpt import answer_of, meta


def run(coro):
    return asyncio.run(coro)


def notices(events) -> list[str]:
    return [payload["text"] for payload in events.of("notice")]


async def fresh_core(tmp_path, fake):
    settings = AppSettings(think_chinese=False)
    engine = make_engine(tmp_path, fake, settings=settings)
    core, _, events, _, _ = await ready_core(tmp_path, chatgpt_engine=engine, settings=settings)
    return core, events


def test_a_fresh_install_starts_quietly_and_the_first_model_is_the_plans(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, events = await fresh_core(tmp_path, fake)
            assert not [text for text in notices(events) if "模型列表" in text], "nothing went wrong"
            defaults = events.last("models")["defaults"]
            assert defaults["model"] == "chatgpt:gpt-6.1-sol", "the first model there is"
            assert defaults["autonomy"] in ("ask", "code", "full"), "the own loop's permission scale"
            await core.shutdown()

    run(scenario())


def test_a_conversation_started_without_naming_a_model_is_the_plans_and_gets_an_answer(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Hello from the plan."))
            core, events = await fresh_core(tmp_path, fake)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            await until(lambda: meta(events, cid).get("mark") == "ready")
            assert meta(events, cid)["engine"] == "chatgpt"
            await core.call("session.send", {"id": cid, "text": "hi"})
            await until(lambda: "Hello from the plan." in answer_of(events, cid))
            await core.shutdown()

    run(scenario())


def test_with_nothing_signed_in_a_new_conversation_says_what_to_do(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, plan=False)
        assert events.last("models")["defaults"]["model"] == ""
        with pytest.raises(AppError, match="还没有可用的模型"):
            await core.call("session.new", {"projectId": str(tmp_path / "proj")})
        assert core.order == [], "no tab was opened"
        await core.shutdown()

    run(scenario())


def test_a_default_model_of_an_engine_that_is_gone_falls_back_to_what_there_is(tmp_path):
    """A settings file from an older version may name a model no engine of this one runs."""

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            settings = AppSettings(think_chinese=False, default_model="claude-haiku-5-5")
            engine = make_engine(tmp_path, fake, settings=settings)
            core, _, events, _, _ = await ready_core(tmp_path, chatgpt_engine=engine, settings=settings)
            assert events.last("models")["defaults"]["model"] == "chatgpt:gpt-6.1-sol"
            assert core.settings.default_model == "claude-haiku-5-5", "the setting itself is left as it was"
            await core.shutdown()

    run(scenario())
