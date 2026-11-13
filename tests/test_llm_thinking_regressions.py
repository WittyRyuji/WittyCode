"""Modern Claude request contracts and signature-only reasoning, without real credentials."""
from __future__ import annotations

from dataclasses import replace

import pytest

from desktop.core.chatgpt.responses import history_item
from desktop.core.llm.anthropic import anthropic_body, anthropic_messages, read_anthropic
from desktop.core.llm.bedrock import converse_body, converse_messages, invoke_body
from desktop.core.llm.wire import ANTHROPIC, Route
from tests.llm_support import collect, lines_of


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2", "claude-mythos-5-2", "claude-mythos-preview", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6"])
def test_modern_claude_uses_adaptive_thinking_in_every_adapter(model):
    request = {"model": model, "input": [], "instructions": "stable", "reasoning": {"effort": "high"}}
    route = Route(ANTHROPIC, "https://example.com", model, max_output=8000)
    for body in (anthropic_body(request, route), invoke_body(request, route)):
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "high"}
    bedrock = converse_body(request, replace(route, model="us.anthropic." + model + "-v1:0"))
    assert bedrock["additionalModelRequestFields"] == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}


@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-sonnet-4-6", "claude-mythos-preview"])
@pytest.mark.parametrize("effort", ["minimal", "xhigh"])
def test_unsupported_adaptive_effort_is_refused_instead_of_silently_changed(model, effort):
    from desktop.core.chatgpt.errors import ChatGPTError
    request = {"input": [], "reasoning": {"effort": effort}}
    with pytest.raises(ChatGPTError, match=effort):
        anthropic_body(request, Route(ANTHROPIC, "https://example.com", model))


def test_the_direct_anthropic_route_preserves_explicit_enable_thinking():
    from desktop.core.llm.backend import route_of
    from tests.test_llm_backend import spec_of
    route = route_of(spec_of("anthropic", native="claude-opus-4-7", thinking=4096), None, {})
    assert route.thinking == 4096
    assert anthropic_body({"input": []}, route)["thinking"] == {"type": "adaptive"}


def test_off_survives_the_internal_request_but_is_not_sent_as_an_openai_parameter():
    from desktop.core.chatgpt.responses import build_request
    from desktop.core.llm.responses_api import responses_body
    request = build_request(model="api:demo:model", instructions="", items=[], tools=[], reasoning="off")
    route = Route(ANTHROPIC, "https://example.com", "claude-opus-4-7", thinking=4096)
    assert anthropic_body(request, route)["thinking"] == {"type": "disabled"}
    assert converse_body(request, route)["additionalModelRequestFields"]["thinking"] == {"type": "disabled"}
    assert not any(key.startswith("wc_") for key in responses_body(request, route))


@pytest.mark.parametrize("model", ["claude-fable-5-2", "claude-mythos-preview", "claude-opus-5-5", "claude-sonnet-5-5"])
def test_off_is_refused_locally_when_a_model_cannot_disable_thinking(model):
    from desktop.core.chatgpt.errors import ChatGPTError
    request = {"input": [], "reasoning": {"effort": "off"}}
    with pytest.raises(ChatGPTError, match="low"):
        anthropic_body(request, Route(ANTHROPIC, "https://example.com", model))


def test_older_or_compatible_models_keep_manual_budgets():
    request = {"input": [], "reasoning": {"effort": "high"}}
    for model in ("claude-sonnet-4-5", "claude-3-7-sonnet-latest", "deepseek-v4-pro"):
        body = anthropic_body(request, Route(ANTHROPIC, "https://example.com", model, max_output=32000))
        assert body["thinking"]["type"] == "enabled" and "budget_tokens" in body["thinking"]
        assert "output_config" not in body


def test_signature_only_thinking_survives_parse_history_and_both_replay_formats():
    route = Route(ANTHROPIC, "https://example.com", "claude-opus-5-5")
    events = [
        {"type": "message_start", "message": {"id": "demo-message", "usage": {"input_tokens": 20}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": "demo-signature"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    from desktop.core.chatgpt.responses import ItemDone
    parsed = collect(read_anthropic(lines_of(events), route.origin))
    item = history_item(next(e.item for e in parsed if isinstance(e, ItemDone)))
    assert item["wc_signature"] == "demo-signature"
    assert anthropic_messages([item], route.origin)[0]["content"] == [{"type": "thinking", "thinking": "", "signature": "demo-signature"}]
    assert converse_messages([item], route.origin)[0]["content"] == [{"reasoningContent": {"reasoningText": {"text": "", "signature": "demo-signature"}}}]
