"""Publisher-supplied OAuth identities and native authorization, exercised with local fakes only."""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.parse

import pytest

from desktop.core.chatgpt.http import HttpResponse
from desktop.core import connector_auth, connector_clients
from desktop.core.connector_auth import ConnectorStore
from desktop.core.plugin_http import PluginError
from tests.test_connector_auth import FakeHttp, callback


@pytest.fixture(autouse=True)
def no_publisher_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_clients, "BUNDLED", tmp_path / "no-clients.json")
    for name in connector_clients.CLIENT_ENV.values():
        monkeypatch.delenv(name, raising=False)


class DeviceService:
    def __init__(self, answers=None, **device):
        self.calls = []
        self.device = {"device_code": "demo-device-code", "user_code": "ABCD-EFGH",
                       "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 5, **device}
        self.answers = list(answers or [{"access_token": "demo-" + "access", "token_type": "bearer"}])

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.device if url.endswith("/device/code") else self.answers.pop(0)
        return HttpResponse(200, {}, json.dumps(value).encode())


def test_missing_publisher_identity_is_explained_without_opening_or_network(tmp_path):
    async def scenario():
        http = DeviceService()
        store = ConnectorStore(tmp_path, http=http)
        assert all(not row["available"] for row in store.views())
        for provider in ("github", "google-workspace"):
            with pytest.raises(PluginError, match="注册|暂不可用"):
                await store.login(provider, lambda url: pytest.fail("no browser should open"))
        assert not http.calls and not store.file.exists()
    asyncio.run(scenario())


def test_invalid_public_client_identifier_is_not_bundled_or_used(monkeypatch):
    monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "bad\nidentifier")
    assert connector_clients.client_id("github") == ""
    with pytest.raises(ValueError, match="WITTYCODE_GITHUB_CLIENT_ID") as error:
        connector_clients.from_environment()
    assert "bad" not in str(error.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
def test_github_device_login_opens_browser_shows_only_user_code_and_saves_on_success(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
        http = DeviceService([{"error": "authorization_pending"}, {"access_token": "demo-" + "access", "token_type": "bearer"}])
        store = ConnectorStore(tmp_path, http=http)
        waits, opened = [], []
        async def pause(self, seconds):
            waits.append(seconds)
        monkeypatch.setattr(connector_auth.GitHubDeviceFlow, "_pause", pause)
        def browser(url):
            opened.append(url)
            row = next(row for row in store.views() if row["id"] == "github")
            assert row["signingIn"] and row["userCode"] == "ABCD-EFGH"
            assert "demo-device-code" not in json.dumps(row)
        row = next(row for row in await store.login("github", browser) if row["id"] == "github")
        assert opened == ["https://github.com/login/device"]
        assert row["connected"] and row["enabled"] and not row["signingIn"] and not row["userCode"]
        assert waits == [5, 5]
        assert len(http.calls) == 3
        assert http.calls[0][2]["form"] == {"client_id": "demo-client", "scope": connector_auth.PROVIDERS["github"]["scopes"]}
        for _, _, request in http.calls:
            assert "client_secret" not in request["form"]
        assert "demo-device-code" not in store.file.read_text(encoding="utf-8")
        assert "demo-access" not in store.file.read_text(encoding="utf-8")
        assert await store.access_token("github") == "demo-access"
        assert [command.name for command in store.commands()] == ["wittycode-github"]
    asyncio.run(scenario())


def test_github_obeys_slow_down_without_exposing_provider_error_details(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
        http = DeviceService([{"error": "slow_down"}, {"error": "access_denied"}])
        store = ConnectorStore(tmp_path, http=http)
        waits = []
        async def pause(self, seconds):
            waits.append(seconds)
        monkeypatch.setattr(connector_auth.GitHubDeviceFlow, "_pause", pause)
        with pytest.raises(PluginError, match="拒绝"):
            await store.login("github", lambda url: None)
        assert waits == [5, 10]
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["device", "poll"])
def test_cancelling_github_during_a_network_request_does_not_save_or_wait(tmp_path, monkeypatch, phase):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
        entered = asyncio.Event()
        class HeldService(DeviceService):
            async def request(self, method, url, **kwargs):
                if (phase == "device") == url.endswith("/device/code"):
                    entered.set()
                    await asyncio.Event().wait()
                return await super().request(method, url, **kwargs)
        async def pause(self, seconds):
            pass
        monkeypatch.setattr(connector_auth.GitHubDeviceFlow, "_pause", pause)
        store = ConnectorStore(tmp_path, http=HeldService())
        work = asyncio.create_task(store.login("github", lambda url: None))
        await asyncio.wait_for(entered.wait(), 1)
        store.cancel("github")
        with pytest.raises(PluginError, match="取消"):
            await asyncio.wait_for(work, 1)
        assert not store.flows and not store.file.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("answers,expected", [
    ([{"error": "access_denied"}], "拒绝"),
    ([{"error": "expired_token"}], "过期"),
    ([{"error": "unexpected", "error_description": "private demo detail"}], "失败"),
])
def test_device_denial_or_expiry_never_stores_credentials(tmp_path, monkeypatch, answers, expected):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
        http = DeviceService(answers)
        store = ConnectorStore(tmp_path, http=http)
        async def pause(self, seconds):
            pass
        monkeypatch.setattr(connector_auth.GitHubDeviceFlow, "_pause", pause)
        with pytest.raises(PluginError, match=expected) as error:
            await store.login("github", lambda url: None)
        assert "private demo detail" not in str(error.value)
        assert not store.file.exists() and not store.flows
    asyncio.run(scenario())


def test_device_authorization_rejects_untrusted_verification_url_and_cancels_promptly(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
        hostile = DeviceService(verification_uri="https://example.com/login/device")
        store = ConnectorStore(tmp_path, http=hostile)
        with pytest.raises(PluginError):
            await store.login("github", lambda url: pytest.fail("untrusted URL must never open"))
        assert not store.flows and not store.file.exists()
        store = ConnectorStore(tmp_path, http=DeviceService())
        def browser(url):
            store.cancel("github")
        with pytest.raises(PluginError, match="取消"):
            await asyncio.wait_for(store.login("github", browser), timeout=1)
        assert not store.flows and not store.file.exists()
    asyncio.run(scenario())


def test_google_cancel_before_callback_server_is_ready_does_not_open_browser(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["google-workspace"], "demo.apps.googleusercontent.com")
        started, release = asyncio.Event(), asyncio.Event()
        start_server = asyncio.start_server
        async def delayed_start(*args, **kwargs):
            started.set()
            await release.wait()
            return await start_server(*args, **kwargs)
        monkeypatch.setattr(asyncio, "start_server", delayed_start)
        store = ConnectorStore(tmp_path, http=FakeHttp())
        opened = []
        work = asyncio.create_task(store.login("google-workspace", opened.append))
        try:
            await asyncio.wait_for(started.wait(), 2)
            store.cancel("google-workspace")
            release.set()
            with pytest.raises(PluginError, match="取消"):
                await asyncio.wait_for(work, 2)
            assert not opened and not store.flows and not store.file.exists()
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
    asyncio.run(scenario())


def test_google_cancel_during_token_exchange_returns_promptly_without_saving(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv(connector_clients.CLIENT_ENV["google-workspace"], "demo.apps.googleusercontent.com")
        entered, release, peers = asyncio.Event(), asyncio.Event(), []
        class HeldHttp(FakeHttp):
            async def request(self, method, url, **kwargs):
                entered.set()
                await release.wait()
                return await super().request(method, url, **kwargs)
        store = ConnectorStore(tmp_path, http=HeldHttp())
        def browser(url):
            peers.append(asyncio.create_task(callback(url)))
        work = asyncio.create_task(store.login("google-workspace", browser))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            store.cancel("google-workspace")
            with pytest.raises(PluginError, match="取消"):
                await asyncio.wait_for(work, 1)
            assert not store.flows and not store.file.exists()
        finally:
            release.set()
            await asyncio.gather(work, *peers, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
def test_google_uses_public_identity_pkce_ephemeral_port_and_refreshes_after_restart(tmp_path, monkeypatch):
    async def scenario():
        variable = connector_clients.CLIENT_ENV["google-workspace"]
        monkeypatch.setenv(variable, "demo.apps.googleusercontent.com")
        http = FakeHttp()
        store = ConnectorStore(tmp_path, http=http)
        monkeypatch.setenv("DEMO_LEGACY_OAUTH_SECRET", "demo-" + "client-secret")
        store.configure("google-workspace", {"clientId": "legacy-client", "clientSecretEnv": "DEMO_LEGACY_OAUTH_SECRET"})
        urls, pending = [], []
        def browser(url):
            urls.append(url)
            pending.append(asyncio.create_task(callback(url)))
        row = next(row for row in await store.login("google-workspace", browser) if row["id"] == "google-workspace")
        await asyncio.gather(*pending)
        assert row["connected"] and row["enabled"] and row["available"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
        redirect = urllib.parse.urlsplit(query["redirect_uri"][0])
        assert redirect.hostname == "127.0.0.1" and redirect.port and redirect.path == "/"
        assert query["code_challenge_method"] == ["S256"]
        assert http.calls[0][2]["form"]["client_id"] == "demo.apps.googleusercontent.com"
        assert "client_secret" not in http.calls[0][2]["form"]
        assert store.config("google-workspace")["clientId"] == "legacy-client", "old settings are retained without being reused"
        token = store.tokens.load("google-workspace")
        token["expires_at"] = time.time() - 1
        store.tokens.save("google-workspace", token)
        monkeypatch.delenv(variable)
        restarted = ConnectorStore(tmp_path, http=http)
        assert await restarted.access_token("google-workspace") == http.value["access_token"]
        assert http.calls[-1][2]["form"]["client_id"] == "demo.apps.googleusercontent.com"
        assert "client_secret" not in http.calls[-1][2]["form"]
    asyncio.run(scenario())


def test_public_clients_are_injected_into_the_release_copy_only(tmp_path, monkeypatch):
    from scripts import build_release
    root = tmp_path / "source"
    (root / "desktop/ui_dist").mkdir(parents=True)
    (root / "desktop/ui_dist/index.html").write_text("demo", encoding="utf-8")
    (root / "WittyCode.pyw").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(build_release, "ROOT", root)
    monkeypatch.setenv(connector_clients.CLIENT_ENV["github"], "demo-client")
    monkeypatch.setenv(connector_clients.CLIENT_ENV["google-workspace"], "demo.apps.googleusercontent.com")
    target = tmp_path / "release"
    target.mkdir()
    build_release.copy_app(target)
    bundled = target / "desktop/core/connector-clients.json"
    assert json.loads(bundled.read_text(encoding="utf-8")) == {"github": "demo-client", "google-workspace": "demo.apps.googleusercontent.com"}
    assert not (root / "desktop/core/connector-clients.json").exists()
    monkeypatch.setattr(connector_clients, "BUNDLED", bundled)
    for variable in connector_clients.CLIENT_ENV.values():
        monkeypatch.delenv(variable)
    assert connector_clients.client_id("github") == "demo-client"
    assert connector_clients.client_id("google-workspace") == "demo.apps.googleusercontent.com"
