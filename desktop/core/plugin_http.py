"""Bounded, non-redirecting HTTP for plugin catalogues, OAuth and connector APIs."""

from __future__ import annotations

import asyncio
import json
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request

from desktop.core.chatgpt.http import HttpResponse


class PluginError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def validate_url(url, *, loopback_http=False):
    if not isinstance(url, str):
        raise PluginError("服务地址不合法。")
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError as error:
        raise PluginError("服务地址或端口不合法。") from error
    local = loopback_http and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if parsed.scheme != "https" and not local or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PluginError("服务地址只接受 HTTPS 或本机回环 HTTP，不包含账号密码及片段。")
    return parsed


def _read_rpc_stream(response, ident, limit, timeout):
    body, event = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() > deadline:
            raise PluginError("MCP 流式响应超时。")
        line = response.readline(min(65536, limit - len(body) + 1))
        if not line:
            break
        body.extend(line)
        event.extend(line)
        if len(body) > limit:
            raise PluginError("服务返回内容超过安全大小限制。")
        if line in (b"\n", b"\r\n"):
            data = b"\n".join(part[5:].lstrip() for part in event.splitlines() if part.startswith(b"data:"))
            event.clear()
            if data:
                try:
                    value = json.loads(data)
                except (ValueError, UnicodeError) as error:
                    raise PluginError("MCP 流式响应不是有效的 JSON。") from error
                if isinstance(value, dict) and value.get("id") == ident:
                    break  # the RPC is complete even if the server keeps its SSE connection open
    return bytes(body)


class PluginHttp:
    async def request(self, method, url, *, headers=None, form=None, json_body=None, limit=8_000_000, timeout=30, allow_loopback=False, rpc_id=None):
        validate_url(url, loopback_http=allow_loopback)
        def send():
            merged = {"User-Agent": "WittyCode", "Accept": "application/json", **(headers or {})}
            data = None
            if form is not None:
                data = urllib.parse.urlencode(form).encode("utf-8")
                merged["Content-Type"] = "application/x-www-form-urlencoded"
            elif json_body is not None:
                data = json.dumps(json_body).encode("utf-8")
                merged["Content-Type"] = "application/json"
            request = urllib.request.Request(url, data=data, headers=merged, method=method)
            try:
                response = urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            except (OSError, ValueError, http.client.HTTPException) as error:
                raise PluginError("无法连接服务，请检查网络或代理。") from error
            try:
                with response:
                    streamed = rpc_id is not None and "text/event-stream" in response.headers.get("Content-Type", "")
                    body = _read_rpc_stream(response, rpc_id, limit, timeout) if streamed else response.read(limit + 1)
                    if len(body) > limit:
                        raise PluginError("服务返回内容超过安全大小限制。")
                    return HttpResponse(response.status if hasattr(response, "status") else response.code, {k.lower(): v for k, v in response.headers.items()}, body)
            except (OSError, http.client.HTTPException) as error:
                raise PluginError("服务连接中断，请重试。") from error
        return await asyncio.to_thread(send)


def checked_json(response):
    if not 200 <= response.status < 300:
        raise PluginError(f"服务请求失败（HTTP {response.status}），请检查授权与服务配置。")
    try:
        return response.json()
    except (ValueError, UnicodeError) as error:
        raise PluginError("服务返回了无效的 JSON。") from error
