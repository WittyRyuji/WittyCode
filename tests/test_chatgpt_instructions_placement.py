"""Placement of stable instructions in Responses requests, without changing the saved history."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from desktop.core.chatgpt.engine import SHAPE_ENV, shape_from_env
from desktop.core.chatgpt.responses import Shape, build_request, can_cache_developer_instructions, developer_message, user_message
from desktop.core.harness import compact
from desktop.core.harness.diagnostics import RequestTrace, fingerprint
from desktop.core.llm.responses_api import responses_body
from desktop.core.llm.wire import RESPONSES, Route
from evals.metrics import prefix_breaks
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import DEFAULT_MODELS, FakeResponses, say

MODEL = "gpt-6.2-sol"
INSTRUCTIONS = "  Keep the original spacing.\r\n回答要简洁。\n"
DEVELOPER = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": INSTRUCTIONS}]}
TOOL = {"type": "function", "name": "read", "description": "Read a file.", "parameters": {"type": "object"}}


@pytest.mark.parametrize("model, expected", [
    ("gpt-6.2", True), ("gpt-6.2-sol", True), ("gpt-6.2-astra-2026-11-24", True),
    ("gpt-6.3", True), ("gpt-6.10-sol", True), ("gpt-7", True), ("gpt-7.0-sol", True), ("gpt-10-luna", True),
    ("gpt-6.1-sol", False), ("gpt-6.0", False), ("gpt-6-luna", False),
    ("gpt-5.25", False), ("gpt-62x", False), ("gpt-6.2x", False), ("gpt-6.2-", False),
    ("api:demo:gpt-6.2-sol", False), ("custom-gpt-7", False), ("chatgpt-latest", False), ("", False),
])
def test_model_generation(model, expected):
    assert can_cache_developer_instructions(model) is expected


def test_new_request_places_the_full_instructions_before_original_items():
    history = [user_message("one"), {"type": "function_call_output", "call_id": "c1", "output": "result"}]
    original = copy.deepcopy(history)
    body = build_request(model=MODEL, instructions=INSTRUCTIONS, items=history, tools=[TOOL], reasoning="low")
    assert "instructions" not in body
    assert body["input"] == [DEVELOPER, *original]
    assert history == original
    assert body["tools"] == [TOOL]


@pytest.mark.parametrize("model, effort, options, extra", [
    ("gpt-6.1-sol", "low", {"effort": "low", "summary": "auto"}, {}),
    ("gpt-6-luna", "low", {"effort": "low", "summary": "auto"}, {}),
    ("api:demo:gpt-6.2-sol", "off", {"summary": "auto"}, {"wc_reasoning_effort": "off"}),
    ("api:demo:claude-model", "low", {"effort": "low", "summary": "auto"}, {}),
])
def test_legacy_and_api_requests_are_unchanged(model, effort, options, extra):
    items = [user_message("one")]
    assert build_request(model=model, instructions=INSTRUCTIONS, items=items, tools=[TOOL], reasoning=effort) == {
        "model": model, "instructions": INSTRUCTIONS, "input": items, "store": False, "stream": True,
        "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": True, "reasoning": options,
        "include": ["reasoning.encrypted_content"], **extra,
    }


def test_shape_env_switch_can_restore_the_legacy_request():
    args = dict(model=MODEL, instructions=INSTRUCTIONS, items=[user_message("one")], tools=[TOOL], reasoning="low")
    current = build_request(**args, shape=shape_from_env({}))
    legacy = build_request(**args, shape=shape_from_env({SHAPE_ENV: '{"cache_developer_instructions": false}'}))
    assert "instructions" not in current and current["input"][0] == DEVELOPER
    assert legacy == {**current, "instructions": INSTRUCTIONS, "input": args["items"]}


@pytest.mark.parametrize("enabled", [True, False])
def test_session_replays_one_stable_wire_prefix_without_inserting_it_into_the_log(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv(SHAPE_ENV, json.dumps({"cache_developer_instructions": enabled}))

    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses(models=[{**DEFAULT_MODELS[0], "slug": MODEL}]) as fake:
            fake.script(say("first reply"), say("second reply", item_id="msg_2"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model=MODEL, reasoning="low")
            try:
                ctl.send_prompt("one")
                await finish_turn(ctl)
                instructions = ctl.saved.prompt["instructions"]
                before = copy.deepcopy(ctl.saved.items)
                ctl.send_prompt("two")
                await finish_turn(ctl)
                first, second = fake.requests
                assert all(r["model"] == MODEL for r in (first, second))
                if enabled:
                    assert all("instructions" not in r for r in (first, second))
                    expected = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": instructions}]}
                    first_message = json.dumps(first["input"][0], ensure_ascii=False).encode("utf-8")
                    second_message = json.dumps(second["input"][0], ensure_ascii=False).encode("utf-8")
                    assert first_message == second_message and first["input"][0] == expected
                else:
                    assert instructions == first["instructions"] == second["instructions"]
                assert first["input"][int(enabled):] == before[:1]
                assert second["input"][int(enabled):] == [*before, user_message("two")]
                assert ctl.saved.items[:-1] == second["input"][int(enabled):]
                assert ctl.saved.items[-1]["role"] == "assistant"
                assert engine.sessions.load(ctl.session_id).items == ctl.saved.items
            finally:
                await engine.close_all()

    asyncio.run(go())


def test_trace_counts_the_wire_prefix_and_keeps_the_instructions_hash():
    def request(items, instructions=INSTRUCTIONS):
        return build_request(model=MODEL, instructions=instructions, items=items, tools=[TOOL], reasoning="low")

    trace = RequestTrace()
    one, two = user_message("one"), user_message("two")
    first = trace.record(request([one]), None, 0)
    appended = trace.record(request([one, two]), None, 0)
    changed_history = trace.record(request([two]), None, 0)
    changed_instructions = trace.record(request([two], "Different instructions."), None, 0)
    assert (first["input_items"], appended["input_items"], appended["shared_prefix_items"]) == (2, 3, 2)
    assert prefix_breaks([first, appended]) == 0
    assert changed_history["shared_prefix_items"] == 1  # the stable developer message is still shared
    assert first["instructions_hash"] == appended["instructions_hash"] == fingerprint(INSTRUCTIONS)
    assert changed_instructions["instructions_hash"] != first["instructions_hash"]
    assert changed_instructions["shared_prefix_items"] == 0


def test_compaction_reuses_the_developer_prefix_and_counts_it_toward_the_budget():
    instructions = INSTRUCTIONS * 400
    history = [user_message("one")]
    base = build_request(model=MODEL, instructions=instructions, items=[], tools=[TOOL], reasoning="low")
    original = copy.deepcopy(base)
    summary = compact.prefix_summary_request(base, history, 20000)
    assert summary is not None
    assert "instructions" not in summary and summary["tool_choice"] == "none"
    assert summary["input"] == [*base["input"], *history, user_message(compact.SUMMARY_TASK + compact.INSTRUCTIONS)]
    assert original == base
    without_prefix = {**base, "input": []}
    assert compact.prefix_summary_request(without_prefix, history, 1000) is not None
    assert compact.prefix_summary_request(base, history, 1000) is None

    # The loop estimates from the history view, which lacks the wire-only instructions and relies on fixed_tokens.
    for shape in (Shape(), Shape(cache_developer_instructions=False)):
        tools_size = compact.fixed_tokens("", [TOOL], shape)
        assert compact.fixed_tokens(instructions, [TOOL], shape) - tools_size == compact.estimate_text(instructions) - compact.estimate_text("")


# ---------------------------------------------------------------------- the API-key route (``api:`` ids, ``llm.responses_api``)


def api_request(**kwargs) -> dict:
    args = dict(model="api:demo:gpt-6.2-sol", instructions=INSTRUCTIONS, items=[user_message("one")], tools=[TOOL], reasoning="low")
    return build_request(**{**args, **kwargs})


def api_route(base_url: str = "https://api.openai.com/v1", model: str = MODEL, **kwargs) -> Route:
    return Route(RESPONSES, base_url, model, **kwargs)


def test_the_official_api_route_places_instructions_like_the_plan_route():
    request = api_request()
    original = copy.deepcopy(request)
    body = responses_body(request, api_route())
    assert "instructions" not in body
    assert body["input"] == [DEVELOPER, user_message("one")]
    assert body["model"] == MODEL and body["tools"] == [TOOL]
    assert request == original, "the loop's request and its items are not changed"


@pytest.mark.parametrize("base_url, model", [
    ("https://api.openai.com/v1/", "gpt-6.1-sol"),      # an older generation keeps the documented field
    ("https://api.openai.com/v1", "claude-sonnet"),    # another provider's slug
    ("https://api.openai.com/v1", "o4"),               # another OpenAI family
    ("https://api.example.com/v1", MODEL),             # a compatible gateway
    ("https://openai.azure.com/v1", MODEL),
    ("http://api.openai.com/v1", MODEL),               # not HTTPS
    ("https://api.openai.com/v2", MODEL),              # not v1
    ("https://api.openai.com", MODEL),                 # no version path
])
def test_other_routes_and_models_keep_top_level_instructions(base_url, model):
    body = responses_body(api_request(), api_route(base_url, model))
    assert body["instructions"] == INSTRUCTIONS and body["input"] == [user_message("one")]


def test_empty_instructions_stay_a_fieldless_official_request_not_an_empty_message():
    body = responses_body(api_request(instructions=""), api_route())
    assert "instructions" not in body and body["input"] == [user_message("one")]


def test_a_developer_prefix_already_in_the_input_is_not_duplicated():
    items = [DEVELOPER, user_message("one")]
    body = responses_body(api_request(items=items), api_route())
    assert "instructions" not in body and body["input"] == items
    other = developer_message("other rules")
    body = responses_body(api_request(items=[other, user_message("one")]), api_route())
    assert body["input"] == [DEVELOPER, other, user_message("one")]


@pytest.mark.parametrize("extra_args, instructions", [
    ({"instructions": "my own"}, "my own"),            # an explicit field replaces, never duplicates
    ({"input": []}, INSTRUCTIONS),                     # an explicit input wins whole; the field is left
])
def test_user_extra_args_naming_instructions_or_input_win(extra_args, instructions):
    body = responses_body(api_request(), api_route(extra_args=extra_args))
    assert body["instructions"] == instructions and body["input"] == ([] if "input" in extra_args else [user_message("one")])


def test_reasoning_args_of_the_current_effort_also_win_and_none_does_not_resurrect():
    body = responses_body(api_request(), api_route(reasoning_args={"low": {"instructions": "per-effort"}}))
    assert body["instructions"] == "per-effort" and body["input"] == [user_message("one")]
    body = responses_body(api_request(), api_route(reasoning_args={"low": {"instructions": None}}))
    assert "instructions" not in body and body["input"] == [user_message("one")]
    # An override of another effort is not this request's and does not count.
    body = responses_body(api_request(), api_route(reasoning_args={"high": {"instructions": "per-effort"}}))
    assert "instructions" not in body and body["input"][0] == DEVELOPER


@pytest.mark.parametrize("native, override, moved", [
    ("gpt-6.1-sol", MODEL, True),     # extraArgs promotes: the model the body asks for decides
    (MODEL, "gpt-6.1-sol", False),   # extraArgs demotes: the older model keeps the field
    (MODEL, MODEL, True),
    ("gpt-6.1-sol", "custom-m", False),
])
def test_the_model_the_body_asks_for_decides_not_the_route(native, override, moved):
    body = responses_body(api_request(), api_route(model=native, extra_args={"model": override}))
    assert body["model"] == override
    if moved:
        assert "instructions" not in body and body["input"][0] == DEVELOPER
    else:
        assert body["instructions"] == INSTRUCTIONS and body["input"] == [user_message("one")]


def test_reasoning_args_model_of_the_current_effort_decides_the_same_way():
    promoted = responses_body(api_request(), api_route(model="gpt-6.1-sol", reasoning_args={"low": {"model": MODEL}}))
    assert promoted["model"] == MODEL and "instructions" not in promoted and promoted["input"][0] == DEVELOPER
    demoted = responses_body(api_request(), api_route(reasoning_args={"low": {"model": "gpt-6.1-sol"}}))
    assert demoted["model"] == "gpt-6.1-sol" and demoted["instructions"] == INSTRUCTIONS
    # extraArgs still has the last word over reasoningArgs: 6.2 by effort, 6.1 in the end.
    final = responses_body(api_request(), api_route(reasoning_args={"low": {"model": MODEL}}, extra_args={"model": "gpt-6.1-sol"}))
    assert final["model"] == "gpt-6.1-sol" and final["instructions"] == INSTRUCTIONS
