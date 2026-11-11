"""``DroidFactory.list_models``: a model the SDK has never heard of must not hide every other model."""

from __future__ import annotations

import asyncio
from pathlib import Path

import droid_sdk
import pytest
from pydantic import ValidationError

from desktop.core import session_manager as sm


def wire_model(model_id: str, provider: str, *, custom: bool = False) -> dict:
    return {
        "id": model_id,
        "displayName": model_id.upper(),
        "shortDisplayName": model_id,
        "modelProvider": provider,
        "supportedReasoningEfforts": ["low", "high"],
        "defaultReasoningEffort": "low",
        "isCustom": custom,
    }


class FakeProtocol:
    def __init__(self, models: list[dict]) -> None:
        self.models = models
        self.requests: list[tuple[str, dict]] = []

    async def send_request(self, *, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {"result": {"models": self.models}}


class FakeClient:
    protocol: FakeProtocol
    closed = 0

    def __init__(self, transport=None) -> None:
        pass

    async def connect(self) -> None:
        pass

    def _ensure_protocol(self) -> FakeProtocol:
        return FakeClient.protocol

    async def close(self) -> None:
        FakeClient.closed += 1


def run(coro):
    return asyncio.run(coro)


def broken_list(*args, **kwargs):
    async def fail():
        try:
            droid_sdk.schemas.models.ListModelsResult.model_validate({"models": [wire_model("x", "bedrock-converse")]})
        except ValidationError as error:
            raise error

    return fail()


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setattr(sm, "DroidClient", FakeClient)
    monkeypatch.setattr(sm, "ProcessTransport", lambda **kw: object())
    FakeClient.closed = 0
    return sm.DroidFactory(lambda: Path("droid.exe"))


def test_a_model_list_that_parses_is_returned_as_it_is(factory, monkeypatch):
    sentinel = [object()]

    async def fine(**kwargs):
        return sentinel

    monkeypatch.setattr(droid_sdk, "list_models", fine)
    assert run(factory.list_models(Path("."))) is sentinel


def test_one_unknown_provider_no_longer_hides_the_whole_list(factory, monkeypatch):
    monkeypatch.setattr(droid_sdk, "list_models", broken_list)
    FakeClient.protocol = FakeProtocol(
        [
            wire_model("claude-haiku-5-5", "anthropic"),
            wire_model("custom:Bedrock-Sonnet-0", "bedrock-converse", custom=True),
            wire_model("custom:Local-Qwen-0", "generic-chat-completion-api", custom=True),
        ]
    )
    models = run(factory.list_models(Path(".")))
    assert [m.id for m in models] == ["claude-haiku-5-5", "custom:Bedrock-Sonnet-0", "custom:Local-Qwen-0"]
    bedrock = models[1]
    assert bedrock.is_custom and bedrock.model_provider.value == "generic-chat-completion-api"
    assert models[0].model_provider.value == "anthropic", "known providers are left alone"
    assert FakeClient.protocol.requests == [("droid.list_models", {})]
    assert FakeClient.closed == 1, "the one-shot droid process is closed"


def test_a_failure_that_is_not_about_parsing_still_fails(factory, monkeypatch):
    async def broken(**kwargs):
        raise droid_sdk.DroidConnectionError("gone")

    monkeypatch.setattr(droid_sdk, "list_models", broken)
    with pytest.raises(droid_sdk.DroidConnectionError):
        run(factory.list_models(Path(".")))
