"""Grok device sign-in and routing against a fake subscription service; never use a real account."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from desktop.core.catalog import build_catalog
from desktop.core.chatgpt.errors import ChatGPTError, friendly_failure
from desktop.core.chatgpt.http import HttpClient, HttpResponse
from desktop.core.chatgpt.responses import Shape, TextDone, build_request
from desktop.core.llm import grok
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore
from tests.fake_responses import say

ACCESS = "access-" + "demo-subscription-token"
REFRESH = "refresh-" + "demo-subscription-token"
DEVICE = "device-" + "demo-subscription-token"
VERIFY = "https://accounts.x.ai/activate"
MODEL = grok.PREFIX + "grok-4.7"


def reply(status, **data):
    return HttpResponse(status, {}, json.dumps(data).encode())


def token_reply(access=ACCESS, refresh=REFRESH, expires=3600):
    return reply(200, access_token=access, refresh_token=refresh, expires_in=expires)


class FakeGrok(HttpClient):
    def __init__(self):
        self.auth = asyncio.Event()
        self.auth.set()
        self.device = reply(200, device_code=DEVICE, user_code="DEMO-2048", verification_uri=VERIFY, expires_in=60, interval=1)
        self.poll = [token_reply()]
        self.refresh = token_reply(ACCESS + "-rotated", REFRESH + "-rotated")
        self.rows = reply(200, data=[
            {"id": "grok-4.7", "api_backend": "responses", "context_window": 420_000, "input_modalities": ["text", "image"]},
            {"id": "grok-imagine-image", "api_backend": "image", "context_window": 100_000},
            {"id": "grok-4-multi-agent", "api_backend": "responses", "context_window": 100_000},
            {"id": "no-tools", "api_backend": "responses", "context_window": 100_000, "supports_tools": False},
        ])
        self.requests = []
        self.inference = []
        self.responses = [say("A subscription answer.")]

    async def request(self, method, url, *, form=None, headers=None, **kwargs):
        self.requests.append((method, url, form, headers))
        if url == grok.DEVICE_URL:
            return self.device
        if url == grok.TOKEN_URL and form["grant_type"] == "refresh_token":
            await asyncio.sleep(0)
            return self.refresh
        if url == grok.TOKEN_URL:
            await self.auth.wait()
            return self.poll.pop(0)
        assert url == grok.SUBSCRIPTION_URL + "/models"
        return self.rows

    async def stream_lines(self, method, url, *, headers=None, json_body=None, **kwargs):
        self.inference.append((method, url, headers, json_body))
        packets = self.responses.pop(0)
        if isinstance(packets, Exception):
            raise packets
        for packet in packets:
            yield "data: " + json.dumps(packet)
            yield ""


def make_store(tmp_path: Path) -> ProviderStore:
    return ProviderStore(models_file=tmp_path / "models.json", sidecar=tmp_path / "providers.json")


@pytest.fixture(autouse=True)
def fake_protection(monkeypatch):
    # This file exercises the persistence contract on every platform; the Qt scenario uses real Windows DPAPI.
    def crypt(data, *, decrypt=False, entropy=None):
        assert entropy == grok._ENTROPY
        return bytes(byte ^ 0xA9 for byte in data)

    monkeypatch.setattr(grok.secret_protection, "crypt", crypt)


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(grok, "sleep", sleep)
    return delays


async def sign_in(sub):
    sub.login()
    assert sub.account()["auth"] == "signing-in"
    await sub._login


def test_device_login_discovers_the_account_models_and_keeps_tokens_out_of_page_and_provider_files(tmp_path, no_poll_delay):
    async def go():
        http, pages = FakeGrok(), []
        http.poll[:0] = [reply(400, error="authorization_pending"), reply(400, error="slow_down")]
        store = make_store(tmp_path)
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=pages.append, http=http)
        await sign_in(sub)
        assert pages == [VERIFY] and no_poll_delay == [1, 1, 6]
        assert sub.account() == {"auth": "signed-in", "code": "", "canOpen": False, "error": ""}
        assert http.requests[0][:3] == ("POST", grok.DEVICE_URL, {"client_id": grok.CLIENT_ID, "scope": grok.SCOPE})
        assert http.requests[1][2] == {"grant_type": grok.DEVICE_GRANT, "client_id": grok.CLIENT_ID, "device_code": DEVICE}
        assert http.requests[-1] == ("GET", grok.SUBSCRIPTION_URL + "/models", None,
                                     {"Accept": "application/json", "Authorization": "Bearer " + ACCESS})
        assert ACCESS.encode() not in sub.path.read_bytes() and REFRESH.encode() not in sub.path.read_bytes()
        assert not store.models_file.exists() and not store.sidecar.exists()

        sub = grok.GrokSubscription(sub.path, open_browser=pages.append, http=http)
        backend = ApiBackend(store, http, grok_subscription=sub)
        (model,) = backend.specs()
        assert (model.id, model.window, model.images) == (MODEL, 420_000, True)
        # Looking up a model spec with a forged base URL/header cannot redirect a subscription bearer.
        route = await backend.route(replace(model, base_url="https://example.invalid", headers=(("Authorization", "wrong"),)))
        assert route.url == grok.SUBSCRIPTION_URL + "/responses" and route.headers["Authorization"] == "Bearer " + ACCESS
        assert route.headers["X-XAI-Token-Auth"] == "xai-grok-cli"
        assert route.headers["x-grok-model-override"] == model.native
        assert route.headers["x-grok-client-version"] == grok._PROXY_CLIENT_VERSION
        events = [e async for e in backend.stream({"model": MODEL, "input": [], "stream": True, "store": False, "service_tier": "priority"})]
        assert any(isinstance(e, TextDone) and e.text == "A subscription answer." for e in events)
        ((method, url, headers, body),) = http.inference
        assert (method, url, headers["Authorization"]) == ("POST", route.url, "Bearer " + ACCESS)
        assert body == {"model": "grok-4.7", "input": [], "stream": True, "store": False}
        await sub.logout()
        assert sub.account()["auth"] == "signed-out" and not sub.path.exists() and backend.spec(MODEL) is None
        with pytest.raises(grok.GrokError):
            _ = [e async for e in backend.stream({"model": MODEL})]
        assert len(http.inference) == 1

    asyncio.run(go())


def test_expiring_token_refreshes_once_for_concurrent_turns_and_rotation_is_persistent(tmp_path):
    async def go():
        http = FakeGrok()
        http.poll = [token_reply(expires=15)]
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=lambda url: None, http=http)
        await sign_in(sub)
        assert await asyncio.gather(sub.access_token(), sub.access_token()) == [ACCESS + "-rotated"] * 2
        refreshes = [r for r in http.requests if r[2] and r[2].get("grant_type") == "refresh_token"]
        assert [r[2]["refresh_token"] for r in refreshes] == [REFRESH]
        restored = grok.GrokSubscription(sub.path, open_browser=lambda url: None, http=http)
        assert await restored.access_token() == ACCESS + "-rotated"
        assert restored._tokens.refresh == REFRESH + "-rotated"
        restored._tokens = replace(restored._tokens, expires=0)
        http.refresh = reply(400, error="invalid_grant", error_description=REFRESH)
        with pytest.raises(grok.GrokError, match="已失效"):
            await restored.access_token()
        assert not restored.path.exists() and not restored.specs()
        assert REFRESH not in str(restored.account())

    asyncio.run(go())


@pytest.mark.parametrize("status", [401, 403, 426, 429])
def test_rejected_subscription_never_sends_the_turn_to_an_api_key_route(tmp_path, status):
    async def go():
        http = FakeGrok()
        http.responses = [ChatGPTError(ACCESS, status=status, body={"private": REFRESH})]
        store = make_store(tmp_path)
        store.models_file.write_text(json.dumps({"customModels": [{
            "model": "grok-4.7", "baseUrl": "https://api.x.ai/v1", "provider": "openai", "maxContextLimit": 500_000,
        }]}), encoding="utf-8")
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=lambda url: None, http=http)
        await sign_in(sub)
        backend = ApiBackend(store, http, grok_subscription=sub)
        assert len(backend.models()) == 2
        (custom,) = [spec for spec in backend.specs() if spec.id != MODEL]
        custom_route = await backend.route(custom)
        assert custom_route.url == "https://api.x.ai/v1/responses"
        assert not set(grok.inference_headers(custom.native)) & custom_route.headers.keys()
        with pytest.raises(ChatGPTError) as caught:
            _ = [e async for e in backend.stream({"model": MODEL})]
        assert "Grok 订阅" in friendly_failure(caught.value) and ACCESS not in str(caught.value)
        assert caught.value.status == status and caught.value.body is None and caught.value.transient is False
        assert [r[1] for r in http.inference] == [grok.SUBSCRIPTION_URL + "/responses"]

    asyncio.run(go())


def test_reasoning_catalog_merges_documented_defaults_and_exact_account_capabilities(tmp_path):
    http = FakeGrok()
    # A pre-upgrade encrypted profile gets the documented levels without signing in again.
    path = tmp_path / "subscription.bin"
    before = {"tokens": {"access": ACCESS, "refresh": REFRESH, "expires": 2_000_000_000},
              "models": [{"id": "grok-4.7", "window": 500_000, "images": True}]}
    path.write_bytes(grok.secret_protection.crypt(json.dumps(before).encode(), entropy=grok._ENTROPY))
    sub = grok.GrokSubscription(path, open_browser=lambda url: None, http=http)
    backend = ApiBackend(make_store(tmp_path), http, grok_subscription=sub)
    offered = build_catalog(api_models=backend.specs()).find(MODEL)
    assert offered["efforts"] == ["low", "medium", "high", "xhigh"] and offered["defaultEffort"] == "high"
    (info,) = backend.models()
    assert info.efforts == tuple(offered["efforts"]) and info.default_effort == "high"

    base = {"api_backend": "responses", "context_window": 200_000}
    rows = [base | values for values in (
        {"id": "grok-4.7-latest", "reasoning_efforts": ["xhigh", "medium", "max", "none", "medium", 42], "default_reasoning_effort": "medium"},
        {"id": "grok-4.7", "supports_reasoning_effort": False},
        {"id": "grok-4.3"}, {"id": "grok-4.5"}, {"id": "grok-4.6"}, {"id": "other"},
    )]
    specs = [model.spec() for model in grok._models({"data": rows}, ACCESS)]
    assert {spec.native: (spec.efforts, spec.reasoning) for spec in specs} == {
        "grok-4.7-latest": (("medium", "xhigh"), "medium"), "grok-4.7": ((), ""),
        "grok-4.3": (("off", "low", "medium"), "low"), "grok-4.5": (("low", "medium", "high"), "high"),
        "grok-4.6": (("low", "medium", "high", "xhigh"), "high"), "other": ((), ""),
    }


def test_harness_effort_is_sent_unchanged_except_xai_none_and_never_sent_for_invalid_level(tmp_path):
    async def go():
        http = FakeGrok()
        http.rows = reply(200, data=[{"id": "grok-4.7"}, {"id": "grok-4.3"}])
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=lambda url: None, http=http)
        await sign_in(sub)
        backend = ApiBackend(make_store(tmp_path), http, grok_subscription=sub)
        http.responses = [say("OK")] * 5
        for model, levels in ((MODEL, ("low", "medium", "high", "xhigh")), (grok.PREFIX + "grok-4.3", ("off",))):
            for effort in levels:
                request = build_request(model=model, instructions="", items=[], tools=[], reasoning=effort, shape=Shape())
                _ = [event async for event in backend.stream(request)]
                _, _, headers, body = http.inference[-1]
                assert headers["x-grok-model-override"] == body["model"] == model.removeprefix(grok.PREFIX)
                assert "wc_reasoning_effort" not in body
                if effort == "off":
                    assert body["reasoning"] == {"effort": "none"} and "include" not in body
                else:
                    assert body["reasoning"] == {"effort": effort, "summary": "auto"}
        for effort in ("max", "off"):
            request = build_request(model=MODEL, instructions="", items=[], tools=[], reasoning=effort, shape=Shape())
            with pytest.raises(grok.GrokError, match="不支持所选思考等级"):
                _ = [event async for event in backend.stream(request)]
        assert len(http.inference) == 5

    asyncio.run(go())


def test_device_grant_without_subscription_model_access_is_not_saved(tmp_path):
    async def go():
        http = FakeGrok()
        http.rows = reply(403, error_description=ACCESS)
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=lambda url: None, http=http)
        await sign_in(sub)
        assert sub.account()["auth"] == "signed-out" and "HTTP 403" in sub.account()["error"]
        assert ACCESS not in sub.account()["error"] and not sub.path.exists() and not sub.specs()

    asyncio.run(go())


def test_cancelled_or_untrusted_device_login_writes_no_credentials(tmp_path):
    async def go():
        http, pages = FakeGrok(), []
        http.auth.clear()
        sub = grok.GrokSubscription(tmp_path / "subscription.bin", open_browser=pages.append, http=http)
        sub.login()
        await asyncio.sleep(0)
        assert sub.account()["code"] == "DEMO-2048" and DEVICE not in str(sub.account())
        sub.login()  # two clicks are one device grant
        await sub.cancel_login()
        assert pages == [VERIFY] and not sub.path.exists() and sub.account()["auth"] == "signed-out"
        assert [r[1] for r in http.requests] == [grok.DEVICE_URL]

        http.device = reply(200, device_code=DEVICE, user_code="DEMO-2048", verification_uri="https://accounts.x.ai@untrusted.example/activate")
        await sign_in(sub)
        assert pages == [VERIFY] and not sub.path.exists() and "无效" in sub.account()["error"]

    asyncio.run(go())
