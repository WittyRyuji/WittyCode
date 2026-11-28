"""Only ChatGPT Plan requests with a conversation use its id for cache routing."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from desktop.core.chatgpt import side
from desktop.core.chatgpt.engine import SHAPE_ENV
from desktop.core.chatgpt.responses import Finished, Shape, build_request, user_message
from desktop.core.harness.compact import prefix_summary_request
from desktop.core.importers.base import saved_id
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine
from tests.fake_responses import DEFAULT_MODELS, FakeResponses, http_error, say

SESSION = "a" * 32


def test_session_routing_is_independent_of_the_optional_prompt_cache_key():
    args = dict(model="gpt-6.2-sol", instructions="Be brief.", items=[], tools=[], reasoning=None)
    base = build_request(**args)
    assert "wc_session" not in base and "prompt_cache_key" not in base
    assert build_request(**args, conversation=SESSION) == {**base, "wc_session": SESSION}
    assert build_request(**args, shape=Shape(session_header=False), conversation=SESSION) == base
    assert build_request(**args, shape=Shape(cache_key=True), conversation=SESSION) == {
        **base, "prompt_cache_key": SESSION, "wc_session": SESSION,
    }
    assert build_request(**args, shape=Shape(cache_key=True, session_header=False), conversation=SESSION) == {
        **base, "prompt_cache_key": SESSION,
    }
    assert build_request(**args, shape=Shape(cache_key=True), conversation="") == base


@pytest.mark.parametrize("enabled", [True, False])
def test_two_controller_turns_send_the_same_session_header_unless_disabled(tmp_path, monkeypatch, enabled):
    if enabled:
        monkeypatch.delenv(SHAPE_ENV, raising=False)
    else:
        monkeypatch.setenv(SHAPE_ENV, '{"session_header": false}')

    async def go():
        model = "gpt-6.2-sol"
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses(models=[{**DEFAULT_MODELS[0], "slug": model}]) as fake:
            fake.script(say("first reply"), say("second reply", item_id="msg_2"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model=model, reasoning="low")
            try:
                for prompt in ("one", "two"):
                    assert ctl.send_prompt(prompt)
                    await finish_turn(ctl)
                assert len(fake.requests) == 2
                if enabled:
                    assert [headers["session_id"] for headers in fake.headers] == [ctl.session_id] * 2
                else:
                    assert all("session_id" not in headers for headers in fake.headers)
                assert all(not key.startswith("wc_") for body in fake.requests for key in body)
                assert all("prompt_cache_key" not in body for body in fake.requests)
                saved = engine.sessions.load(ctl.session_id)
                assert saved is not None
                first, second = saved.requests
                assert second["shared_prefix_items"] == first["input_items"] > 0
                assert ctl.session_id not in json.dumps(saved.requests)
                assert all("wc_session" not in record for record in saved.requests)
            finally:
                await engine.close_all()

    asyncio.run(go())


@pytest.mark.parametrize("candidate, valid", [
    pytest.param("session\n", False, id="newline"),
    pytest.param("session-\u00e9", False, id="non-ascii"),
    pytest.param("session title", False, id="space"),
    pytest.param("a" * 129, False, id="too-long"),
    pytest.param(123, False, id="non-string"),
    pytest.param(uuid.uuid4().hex, True, id="new-session"),
    pytest.param(saved_id("codex:demo"), True, id="imported-session"),
])
def test_the_plan_sends_only_valid_session_header_values(tmp_path, candidate, valid):
    async def go():
        with FakeResponses() as fake:
            fake.script(say("done"))
            engine = make_engine(tmp_path, fake, shape=Shape())
            request = build_request(model="gpt-6.1-sol", instructions="", items=[], tools=[], reasoning=None)
            request["wc_session"] = candidate
            events = [event async for event in engine.stream(request)]
            assert isinstance(events[-1], Finished)
            (headers,) = fake.headers
            (body,) = fake.requests
            if valid:
                assert headers["session_id"] == candidate
            else:
                assert "session_id" not in headers
            assert not any(key.startswith("wc_") for key in body)

    asyncio.run(go())


def test_the_plan_401_retry_keeps_the_session_header(tmp_path, monkeypatch):
    async def go():
        with FakeResponses() as fake:
            # A scripted refusal is recorded, unlike the fake's authorization gate.
            fake.script(http_error(401, {"detail": "bad token"}), say("done"))
            engine = make_engine(tmp_path, fake, shape=Shape())

            async def refresh(client_id, record):
                return "renewed-demo-token"

            monkeypatch.setattr(engine.tokens, "_renew", refresh)
            request = build_request(model="gpt-6.1-sol", instructions="", items=[], tools=[], reasoning=None,
                                    shape=engine.shape, conversation=SESSION)
            events = [event async for event in engine.stream(request)]
            assert isinstance(events[-1], Finished)
            assert [headers["session_id"] for headers in fake.headers] == [SESSION, SESSION]
            assert [headers["authorization"] for headers in fake.headers] == [f"Bearer {ACCESS_TOKEN}", "Bearer renewed-demo-token"]
            assert all(not key.startswith("wc_") for body in fake.requests for key in body)

    asyncio.run(go())


def test_a_prefix_summary_keeps_the_session_header_but_a_side_request_has_none(tmp_path):
    async def go():
        with FakeResponses() as fake:
            fake.script(say("summary"), say("side answer"))
            engine = make_engine(tmp_path, fake, shape=Shape())
            model = "gpt-6.1-sol"
            base = build_request(model=model, instructions="Be brief.", items=[], tools=[], reasoning=None,
                                 shape=engine.shape, conversation=SESSION)
            summary = prefix_summary_request(base, [user_message("one")], 20000)
            assert summary is not None
            events = [event async for event in engine.stream(summary)]
            assert isinstance(events[-1], Finished)
            assert await side.ask(engine, "side question", model_id=model) == "side answer"
            prefixed, standalone = fake.headers
            assert prefixed["session_id"] == SESSION
            assert "session_id" not in standalone
            assert all(not key.startswith("wc_") for body in fake.requests for key in body)

    asyncio.run(go())
