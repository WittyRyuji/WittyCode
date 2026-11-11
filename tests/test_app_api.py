"""The API providers' models inside the application, end to end: the real ``AppCore``, the real engine with the real agent loop and
tools, and the real ``ApiBackend``, talking to servers on this machine. Nobody is signed in to ChatGPT and no Droid is started: an
API provider's model needs its key and nothing else."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from desktop.core.chatgpt.http import HttpClient
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore
from tests.app_support import make_core, ready_core
from tests.chatgpt_support import make_engine
from tests.fakes import until
from tests.llm_support import FakeApi, http_error


def run(coro):
    return asyncio.run(coro)


def chunk(delta: dict | None = None, finish: str | None = None, **extra: Any) -> dict:
    payload = {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    payload.update(extra)
    return payload


def says(text: str, used: tuple[int, int] = (200, 20)) -> list[Any]:
    return [chunk({"content": text}, finish="stop"), {"id": "c", "choices": [], "usage": {"prompt_tokens": used[0], "completion_tokens": used[1]}}, "[DONE]"]


def reads(path: str, thought: str = "", call_id: str = "call_1") -> list[Any]:
    first = chunk({"reasoning_content": thought}) if thought else chunk({"content": "Let me look."})
    return [
        first,
        chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"file_path": path})}}]}, finish="tool_calls"),
        {"id": "c", "choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 30}},
        "[DONE]",
    ]


async def api_core(tmp_path: Path, api: FakeApi, *, key: str | None = "sk-test-1", **entry: Any):
    path = tmp_path / "factory.json"
    base = {"model": "deep-chat", "displayName": "Deep", "baseUrl": api.base_url, "apiKey": "${TEST_API_KEY}", "provider": "generic-chat-completion-api", "maxContextLimit": 64_000}
    path.write_text(json.dumps({"customModels": [{**base, **entry}]}), encoding="utf-8")
    store = ProviderStore(factory_settings=path, sidecar=tmp_path / "providers.json", environ={"TEST_API_KEY": key} if key else {}, registry_env=lambda: {})
    no_proxy = HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    engine = make_engine(tmp_path, None, signed_in=False, api=ApiBackend(store, http=no_proxy))
    core, factory, events, platform, _ = await ready_core(tmp_path, chatgpt_engine=engine, providers=store)
    engine.settings = core.settings
    return core, factory, events, engine, store


def sessions_of(events) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


def meta(events, cid: str) -> dict:
    return sessions_of(events).get(cid, {})


def blocks(events, cid: str) -> list[dict]:
    for payload in reversed(events.of("turns")):
        if payload["id"] == cid and payload["turns"]:
            return payload["turns"][-1]["blocks"]
    return []


def answer_of(events, cid: str) -> str:
    return "".join(b.get("markdown", "") for b in blocks(events, cid) if b["kind"] == "text")


async def new_session(core, events, tmp_path: Path, model: str, **more: Any) -> str:
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": model, **more}))["id"]
    await until(lambda: meta(events, cid).get("mark") == "ready")
    return cid


def api_model_id(events) -> str:
    (model,) = [m["id"] for m in events.last("models")["models"] if m["source"].startswith("api:")]
    return model


# ------------------------------------------------------------------------------------------------ the list


def test_the_providers_models_are_in_the_list_for_the_own_loop_without_any_sign_in_or_droid(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api, reasoningEffort="medium")
            (model,) = [m for m in events.last("models")["models"] if m["source"].startswith("api:")]
            assert (model["label"], model["engine"], model["window"], model["defaultEffort"]) == ("Deep", "chatgpt", 64_000, "medium")
            assert core.catalog.target(model["id"]).engine == "chatgpt" and core.catalog.engine_of(model["id"]) == "chatgpt"
            assert not [m for m in events.last("models")["models"] if m["source"] == "chatgpt"], "no ChatGPT plan models: nobody is signed in"
            await core.shutdown()

    run(scenario())


# ------------------------------------------------------------------------------------------------ a conversation


def test_a_conversation_with_an_api_model_is_asked_at_the_provider_and_no_droid_is_started(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Four."))
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            assert meta(events, cid)["engine"] == "chatgpt"
            await core.call("session.send", {"id": cid, "text": "what is 2+2"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "Four.")
            ((path, headers, body),) = api.posts
            assert path == "/v1/chat/completions" and headers["authorization"] == "Bearer sk-test-1" and body["model"] == "deep-chat"
            assert body["messages"][0]["role"] == "system" and body["messages"][-1] == {"role": "user", "content": "what is 2+2"}
            assert not [call for call in factory.calls if call[0] in ("create", "resume")], "Droid was not asked for anything"
            context = meta(events, cid)["context"]
            assert (context["window"], context["used"]) == (64_000, 220), "the window is the user's and the usage is what the provider counted"
            await core.shutdown()

    run(scenario())


def test_a_tool_call_runs_in_the_project_and_its_result_goes_back_to_the_model(tmp_path):
    async def scenario():
        with FakeApi() as api:
            (tmp_path / "proj").mkdir(exist_ok=True)
            (tmp_path / "proj" / "a.txt").write_text("alpha\n", encoding="utf-8")
            api.script(reads("a.txt"), says("It says alpha."))
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events), autonomy="full")
            await core.call("session.send", {"id": cid, "text": "read a.txt"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid).endswith("It says alpha."))
            assert len(api.posts) == 2
            second = api.posts[1][2]["messages"]
            assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "read_file"
            assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1" and "alpha" in second[-1]["content"]
            await core.shutdown()

    run(scenario())


def test_a_models_thoughts_in_a_tool_loop_go_back_to_the_same_provider_in_the_next_request(tmp_path):
    async def scenario():
        with FakeApi() as api:
            (tmp_path / "proj").mkdir(exist_ok=True)
            (tmp_path / "proj" / "a.txt").write_text("alpha\n", encoding="utf-8")
            api.script(reads("a.txt", thought="I should read the file first."), says("Done."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events), autonomy="full")
            await core.call("session.send", {"id": cid, "text": "read a.txt"})
            await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "Done.")
            second = api.posts[1][2]["messages"]
            assistant = [m for m in second if m["role"] == "assistant"][-1]
            assert assistant["reasoning_content"] == "I should read the file first.", "DeepSeek refuses the next request of a tool loop without them"
            await core.shutdown()

    run(scenario())


def test_a_second_message_carries_the_conversation_so_far(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("One."), says("Two."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": cid, "text": "first"})
            await until(lambda: answer_of(events, cid) == "One.")
            await until(lambda: meta(events, cid).get("mark") == "ready")
            await core.call("session.send", {"id": cid, "text": "second"})
            await until(lambda: answer_of(events, cid) == "Two.")
            roles = [(m["role"], m.get("content")) for m in api.posts[1][2]["messages"][1:]]
            assert roles == [("user", "first"), ("assistant", "One."), ("user", "second")]
            await core.shutdown()

    run(scenario())


# ------------------------------------------------------------------------------------------------ failures and the sidebar


def test_a_refused_key_is_told_with_the_providers_name_and_a_missing_variable_with_its_own(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(http_error(401, {"error": {"message": "Incorrect API key"}}))
            core, _, events, _, store = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": cid, "text": "hi"})
            provider = store.api_models()[0].provider_name
            await until(lambda: any(b["kind"] == "error" and "没有接受这个密钥" in json.dumps(b, ensure_ascii=False) and provider in json.dumps(b, ensure_ascii=False) for b in blocks(events, cid)))
            await core.shutdown()
        with FakeApi() as api:
            core, _, events, _, _ = await api_core(tmp_path, api, key=None)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": cid, "text": "hi"})
            await until(lambda: any(b["kind"] == "error" and "TEST_API_KEY" in json.dumps(b, ensure_ascii=False) for b in blocks(events, cid)))
            assert api.posts == [], "nothing was sent without a key"
            await core.shutdown()

    run(scenario())


def test_the_conversation_is_in_the_sidebar_even_when_the_chatgpt_plan_is_switched_off(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Hi."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            core.settings.chatgpt_enabled = False
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": cid, "text": "hello"})
            await until(lambda: answer_of(events, cid) == "Hi." and meta(events, cid).get("mark") == "ready")
            await core.call("session.close", {"id": cid})
            await core._reload_project(str((tmp_path / "proj").resolve()))
            rows = core._saved[str((tmp_path / "proj").resolve())]
            assert [row.engine for row in rows] == ["chatgpt"], "the saved conversation is listed: it belongs to the own loop, not to the plan"
            model = api_model_id(events)
            assert rows[0].model == model
            shown = core._saved_json(rows[0], str((tmp_path / "proj").resolve()))
            assert shown["settings"]["model"] == model, "the row says which model it was on: opening it asks for that model, not for the default"
            await core.shutdown()

    run(scenario())


def test_an_imported_conversation_goes_on_with_a_plan_model_never_with_an_api_one(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, _, events, _, _ = await api_core(tmp_path, api)
            core.settings.default_model = api_model_id(events)
            assert core._import_defaults()["model"] == "", "the text of another app is not sent to an API provider that happens to be the default"
            await core.shutdown()

    run(scenario())


# ------------------------------------------------------------------------------------------------ privacy and wording


def test_what_was_said_to_an_api_model_is_never_offered_to_the_memory_curator(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Hi."))
            core, factory, events, _, _ = await api_core(tmp_path, api)
            offered: list[str] = []
            core.curator = SimpleNamespace(worth_asking=lambda exchange: offered.append(exchange.engine) or True, consider=lambda exchange: asyncio.sleep(0))
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": cid, "text": "my secret plan"})
            await until(lambda: answer_of(events, cid) == "Hi." and meta(events, cid).get("mark") == "ready")
            await asyncio.sleep(0.2)
            assert offered == [], "another company's model must not be asked to sort what was said to this one"
            await core.shutdown()

    run(scenario())


def test_changing_from_a_droid_conversation_to_an_api_model_says_api_and_hands_the_text_over(tmp_path):
    from desktop.core.session_manager import SessionState
    from tests.support import complete, delta, success

    async def scenario():
        with FakeApi() as api:
            api.script(says("Six."))
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            await until(lambda: meta(events, cid).get("mark") == "ready")
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "what is 2+2"})
            await until(lambda: droid.active is not None)
            droid.active.push(delta("It is four.", "m1"), complete("m1"), success("It is four."))
            droid.active.end()
            await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
            result = await core.call("session.choose", {"id": cid, "field": "model", "value": api_model_id(events)})
            assert result["switched"] is True and core._by_cid[cid].engine == "chatgpt"
            notes = [n["text"] for n in events.of("notice") if "已换到" in n["text"]]
            assert notes and "（API）" in notes[-1] and "ChatGPT Plan" not in notes[-1] and "Droid" not in notes[-1]
            switches = [b for payload in events.of("turns") for turn in payload["turns"] for b in turn["blocks"] if b["kind"] == "switch"]
            assert switches[-1]["from"]["engine"] == "Droid" and switches[-1]["to"]["engine"] == "API"
            await core.call("session.send", {"id": cid, "text": "and 3+3?"})
            await until(lambda: answer_of(events, cid) == "Six.")
            first = api.posts[0][2]["messages"][-1]["content"]
            assert "what is 2+2" in first and "It is four." in first and "and 3+3?" in first
            await core.shutdown()

    run(scenario())
