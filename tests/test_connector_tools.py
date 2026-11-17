"""Bundled connectors expose real read-only API routes, never arbitrary URLs or write operations."""

import asyncio
import base64
import json
import sys

import pytest

from desktop.core.chatgpt.http import HttpResponse
from desktop.core.connector_auth import ConnectorStore
from desktop.core.connector_tools import ConnectorTools, TOOLS
from desktop.core.plugin_http import PluginError
from desktop.core.plugin_server import handle


class ApiFake:
    def __init__(self):
        self.calls = []
    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "/contents/" in url:
            value = {"type": "file", "encoding": "base64", "content": base64.b64encode(b"demo source").decode(), "download_url": "https://example.com/do-not-follow"}
        elif "/export?" in url:
            return HttpResponse(200, {}, b"demo document")
        elif "/drive/v3/files/" in url:
            value = {"mimeType": "application/vnd.google-apps.document"}
        elif "/gmail/v1/users/me/messages/" in url:
            value = {"id": "demo-message", "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"demo mail").decode()}}}
        else:
            value = {"items": [], "nextPageToken": "next-demo-page"}
        return HttpResponse(200, {}, json.dumps(value).encode())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI credential round trip")
@pytest.mark.parametrize("provider,name,args,expected", [
    ("github", "github_search_code", {"query": "repo:demo/inventory-api", "page": "2"}, "/search/code?"),
    ("github", "github_search_issues", {"query": "repo:demo/inventory-api"}, "/search/issues?"),
    ("github", "github_read_file", {"owner": "demo", "repo": "inventory-api", "path": "src/app.py"}, "/contents/src/app.py"),
    ("google-workspace", "google_drive_search", {}, "/drive/v3/files?"),
    ("google-workspace", "google_drive_read", {"fileId": "demo-file"}, "/export?"),
    ("google-workspace", "google_gmail_search", {"query": "is:unread"}, "/gmail/v1/users/me/messages?"),
    ("google-workspace", "google_gmail_read", {"messageId": "demo-message"}, "/gmail/v1/users/me/messages/demo-message?"),
    ("google-workspace", "google_calendar_events", {}, "/calendar/v3/calendars/primary/events?"),
])
def test_service_tools_use_fixed_read_only_routes(tmp_path, provider, name, args, expected):
    async def scenario():
        api = ApiFake()
        store = ConnectorStore(tmp_path, http=api)
        store.configure(provider, {"enabled": True})
        store.tokens.save(provider, {"access_token": "demo-" + "access", "expires_at": 0})
        tool = ConnectorTools(provider, store)
        result = await tool.call(name, args)
        assert result and expected in api.calls[-1][1]
        assert all(method == "GET" and "do-not-follow" not in url for method, url, _ in api.calls)
        assert all(call[2]["headers"]["Authorization"] == "Bearer demo-" + "access" for call in api.calls)
        assert all(t["annotations"]["readOnlyHint"] for t in TOOLS[provider])
        store.configure(provider, {"enabled": False})
        before = len(api.calls)
        with pytest.raises(PluginError, match="停用"):
            await tool.call(name, args)
        assert len(api.calls) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("html_only", [False, True])
def test_gmail_html_only_body_is_readable_and_plain_alternatives_are_not_duplicated(html_only):
    html = "<html><head><style>hidden style</style></head><body><p>Demo &amp; mail</p><script>hidden script</script><p>Second line.</p></body></html>"
    def part(mime, text):
        return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}
    payload = part("text/html", html) if html_only else {"mimeType": "multipart/alternative", "parts": [part("text/plain", "Preferred plain body."), part("text/html", html)]}
    class Store:
        def __init__(self):
            self.http = self
        def config(self, provider):
            return {"enabled": True}
        async def access_token(self, provider):
            return "demo-" + "access"
        async def request(self, method, url, **kwargs):
            return HttpResponse(200, {}, json.dumps({"id": "demo-message", "payload": payload}).encode())
    result = json.loads(asyncio.run(ConnectorTools("google-workspace", Store()).call("google_gmail_read", {"messageId": "demo-message"})))
    assert result["text"] == ("Demo & mail\n\nSecond line." if html_only else "Preferred plain body.")
    assert "hidden" not in result["text"] and "<p>" not in result["text"]


def test_unsafe_tool_arguments_and_errors_do_not_escape_into_transcripts(tmp_path):
    async def scenario():
        tool = ConnectorTools("github", ConnectorStore(tmp_path, http=ApiFake()))
        for args in ({"owner": "demo", "repo": "inventory-api", "path": "../other"}, {"url": "https://example.com"}):
            response = await handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "github_read_file", "arguments": args}}, tools=tool)
            assert response["result"]["isError"]
            assert "../other" not in json.dumps(response) and "https://example.com" not in json.dumps(response)
        for params in ([], None, "invalid"):
            response = await handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}, tools=tool)
            assert response["result"]["isError"]
    asyncio.run(scenario())
