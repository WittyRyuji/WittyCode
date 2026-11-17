"""Browser callbacks and encrypted credentials use local fakes only; no real account or model is contacted."""

import asyncio
import json
import socket
import sys
import time
import urllib.parse

import pytest

from desktop.core.chatgpt.http import HttpResponse
from desktop.core.connector_auth import ConnectorStore, PROVIDERS, _token_response
from desktop.core.plugin_http import PluginError


@pytest.fixture(autouse=True)
def no_publisher_client(monkeypatch):
    from desktop.core import connector_clients
    monkeypatch.setattr(connector_clients, "client_id", lambda provider: "")


class FakeHttp:
    def __init__(self, value=None, status=200):
        self.calls = []
        self.value = value or {"access_token": "demo-" + "access", "refresh_token": "demo-" + "refresh", "token_type": "Bearer", "expires_in": 3600}
        self.status = status

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        await asyncio.sleep(.03)
        return HttpResponse(self.status, {}, json.dumps(self.value).encode())


def free_port():
    with socket.socket() as peer:
        peer.bind(("127.0.0.1", 0))
        return peer.getsockname()[1]


async def callback(url, *, state=None, error=None):
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    redirect = urllib.parse.urlsplit(query["redirect_uri"][0])
    params = {"state": state if state is not None else query["state"][0]}
    params.update({"error": error} if error else {"code": "demo-authorization-code"})
    reader, writer = await asyncio.open_connection(redirect.hostname, redirect.port)
    writer.write(f"GET {redirect.path}?{urllib.parse.urlencode(params)} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    result = await reader.read()
    writer.close()
    await writer.wait_closed()
    return result


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
@pytest.mark.parametrize("provider", list(PROVIDERS))
def test_browser_login_checks_state_uses_pkce_and_keeps_tokens_out_of_views_and_plaintext(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("DEMO_OAUTH_SECRET", "demo-" + "client-secret")
    async def scenario():
        http = FakeHttp()
        store = ConnectorStore(tmp_path, http=http)
        store.configure(provider, {"clientId": "demo-client", "clientSecretEnv": "DEMO_OAUTH_SECRET", "port": free_port()})
        urls, replies, pending = [], [], []
        async def browser(url):
            replies.append(await callback(url, state="wrong-state"))
            assert provider in store.flows and not http.calls
            replies.append(await callback(url))
        def opened(url):
            urls.append(url); pending.append(asyncio.create_task(browser(url)))
        views = await store.login(provider, opened)
        await asyncio.gather(*pending)
        row = next(v for v in views if v["id"] == provider)
        assert row["connected"] and row["enabled"] and not row["signingIn"]
        assert replies[0].startswith(b"HTTP/1.1 400") and replies[1].startswith(b"HTTP/1.1 200")
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
        form = http.calls[0][2]["form"]
        assert auth["code_challenge_method"] == ["S256"] and form["code_verifier"]
        assert auth["redirect_uri"] == [form["redirect_uri"]]
        assert "client_secret" not in auth
        stored = store.file.read_text(encoding="utf-8")
        for value in (http.value["access_token"], http.value["refresh_token"], "demo-" + "client-secret"):
            assert value not in stored and value not in json.dumps(views)
        restarted = ConnectorStore(tmp_path, http=http)
        assert await restarted.access_token(provider) == http.value["access_token"]
        assert len(http.calls) == 1
        command, = restarted.commands()
        assert command.trusted_annotations and provider in " ".join(command.args)
        assert http.value["access_token"] not in repr(command)
        restarted.disconnect(provider)
        assert not restarted.commands()
    asyncio.run(scenario())


def test_cancel_and_browser_denial_leave_no_credentials_and_release_the_callback_port(tmp_path):
    async def scenario():
        for denied in (False, True):
            store = ConnectorStore(tmp_path, http=FakeHttp())
            port = free_port()
            store.configure("google-workspace", {"clientId": "demo-client", "port": port})
            pending = []
            def opened(url):
                if denied:
                    pending.append(asyncio.create_task(callback(url, error="access_denied")))
                else:
                    asyncio.get_running_loop().call_soon(store.cancel, "google-workspace")
            with pytest.raises(PluginError, match="取消|拒绝"):
                await store.login("google-workspace", opened)
            if pending:
                await asyncio.gather(*pending)
            assert not store.flows and store.tokens.load("google-workspace") is None
            with socket.socket() as peer:
                peer.bind(("127.0.0.1", port))
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
def test_refresh_is_serialized_across_store_instances_and_disconnect_cannot_be_resurrected(tmp_path):
    async def scenario():
        http = FakeHttp()
        first = ConnectorStore(tmp_path, http=http)
        first.configure("google-workspace", {"clientId": "demo-client", "enabled": True})
        first.tokens.save("google-workspace", {"access_token": "demo-old-access", "refresh_token": "demo-old-refresh", "expires_at": time.time() - 10})
        second = ConnectorStore(tmp_path, http=http)
        a, b = await asyncio.gather(first.access_token("google-workspace"), second.access_token("google-workspace"))
        assert a == b == http.value["access_token"] and len(http.calls) == 1
        first.tokens.save("google-workspace", {"access_token": "demo-old-access", "refresh_token": "demo-old-refresh", "expires_at": 1})
        task = asyncio.create_task(second.access_token("google-workspace"))
        while len(http.calls) < 2:
            await asyncio.sleep(.005)
        await asyncio.to_thread(first.disconnect, "google-workspace")
        await task
        assert first.tokens.load("google-workspace") is None
        assert not first.commands()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "3600"])
def test_invalid_token_lifetimes_are_rejected(value):
    with pytest.raises(PluginError):
        _token_response({"access_token": "demo-access", "expires_in": value})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
def test_token_errors_do_not_echo_service_bodies_or_injected_authorization_headers(tmp_path):
    async def scenario():
        store = ConnectorStore(tmp_path, http=FakeHttp({"error": "invalid_grant", "detail": "demo-private-detail"}, 400))
        store.configure("google-workspace", {"clientId": "demo-client"})
        store.tokens.save("google-workspace", {"access_token": "demo-old", "refresh_token": "demo-refresh", "expires_at": 1})
        with pytest.raises(PluginError) as raised:
            await store.access_token("google-workspace")
        assert "demo-private-detail" not in str(raised.value)
        for value in ("", "demo\r\nX-Demo: injected", "中文"):
            with pytest.raises(PluginError):
                _token_response({"access_token": value})
    asyncio.run(scenario())


def test_invalid_configuration_is_rejected_without_changing_an_existing_file(tmp_path):
    store = ConnectorStore(tmp_path)
    store.configure("github", {"clientId": "demo-client"})
    before = store.file.read_bytes()
    for value in ({"port": True}, {"port": 80}, {"clientSecretEnv": "${VALUE}"}, {"clientId": "bad\nvalue"}, {"unknown": True}):
        with pytest.raises(PluginError):
            store.configure("github", value)
        assert store.file.read_bytes() == before
    unregistered = ConnectorStore(tmp_path / "unregistered", http=FakeHttp())
    with pytest.raises(PluginError, match="注册"):
        asyncio.run(unregistered.login("github", lambda url: None))
    assert unregistered.tokens.load("github") is None and not unregistered.http.calls
