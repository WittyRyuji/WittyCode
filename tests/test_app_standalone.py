"""The application without Factory: only a ChatGPT plan is signed in, there is no droid.exe on the computer (and no Codex, no
Devin). That is a way to use WittyCode, not an error: it starts quietly, offers what there is, and starts conversations of it."""

from __future__ import annotations

import asyncio

from desktop.core.droid_locator import DroidNotFoundError
from tests.chatgpt_support import ACCESS_TOKEN
from tests.fake_responses import FakeResponses, say
from tests.fakes import FakeFactory, until
from tests.test_app_chatgpt import answer_of, chatgpt_core, meta


def run(coro):
    return asyncio.run(coro)


class NoDroid(FakeFactory):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or DroidNotFoundError("找不到 droid：请安装 Factory 桌面版，或在设置中指定 droid.exe 的路径，或设置环境变量 WITTYCODE_DROID_EXECUTABLE。")

    async def list_models(self, cwd):
        raise self.error


def notices(events) -> list[str]:
    return [payload["text"] for payload in events.of("notice")]


def test_without_factory_it_starts_quietly_and_the_first_model_is_the_plans(tmp_path, monkeypatch):
    monkeypatch.delenv("WITTYCODE_DROID_EXECUTABLE", raising=False)

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake, factory=NoDroid())
            assert not [text for text in notices(events) if "模型列表" in text or "droid" in text.lower()], "no Factory is not a failure"
            defaults = events.last("models")["defaults"]
            assert defaults["model"] == "chatgpt:gpt-6.1-sol", "the first model there is"
            assert defaults["autonomy"] in ("ask", "code", "full"), "the plan's permission scale, not Droid's"
            await core.shutdown()

    run(scenario())


def test_a_conversation_started_without_naming_a_model_is_the_plans_and_gets_an_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("WITTYCODE_DROID_EXECUTABLE", raising=False)

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Hello from the plan."))
            core, factory, events, _ = await chatgpt_core(tmp_path, fake, factory=NoDroid())
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            await until(lambda: meta(events, cid).get("mark") == "ready")
            assert meta(events, cid)["engine"] == "chatgpt" and not factory.sessions, "no droid process was needed"
            await core.call("session.send", {"id": cid, "text": "hi"})
            await until(lambda: "Hello from the plan." in answer_of(events, cid))
            await core.shutdown()

    run(scenario())


def test_a_droid_path_the_user_named_that_does_not_exist_is_said(tmp_path, monkeypatch):
    monkeypatch.delenv("WITTYCODE_DROID_EXECUTABLE", raising=False)

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake, factory=NoDroid(DroidNotFoundError("设置中指定的 droid 路径不存在：C:\\nope\\droid.exe")))
            assert notices(events) == [], "nothing was named yet"
            core.settings.droid_path = "C:\\nope\\droid.exe"
            await core._load_models()
            assert any("读取模型列表失败" in text and "C:\\nope\\droid.exe" in text for text in notices(events))
            await core.shutdown()

    run(scenario())


def test_the_variable_that_names_droid_counts_as_naming_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WITTYCODE_DROID_EXECUTABLE", "C:\\nope\\droid.exe")

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            _, _, events, _ = await chatgpt_core(tmp_path, fake, factory=NoDroid(DroidNotFoundError("环境变量 WITTYCODE_DROID_EXECUTABLE 指向的文件不存在")))
            assert any("读取模型列表失败" in text for text in notices(events))

    run(scenario())


def test_a_droid_that_fails_in_another_way_is_said_whatever_was_named(tmp_path, monkeypatch):
    monkeypatch.delenv("WITTYCODE_DROID_EXECUTABLE", raising=False)

    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            _, _, events, _ = await chatgpt_core(tmp_path, fake, factory=NoDroid(RuntimeError("droid crashed")))
            assert any("读取模型列表失败" in text and "droid crashed" in text for text in notices(events))

    run(scenario())


def test_when_factory_is_there_its_first_model_is_still_the_default_and_hiding_all_of_it_is_a_choice(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            assert events.last("models")["defaults"]["model"] == "claude-haiku-5-5"
            factory_ids = [m["id"] for m in events.last("models")["models"] if m["source"] == "factory"]
            await core.call("models.setVisible", {"ids": factory_ids, "visible": False})
            assert events.last("defaults")["defaults"]["model"] == "", "Factory's models are there and hidden: Droid picks, as before"
            await core.shutdown()

    run(scenario())
