"""Codex-style Streamable HTTP MCP settings, adapted to the existing stdio engine contract."""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from desktop.core.plugin_http import PluginError, PluginHttp, validate_url

VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def validate_http_config(config):
    supported = {"url", "type", "bearer_token_env_var", "http_headers", "env_http_headers", "env", "env_vars"}
    if set(config) - supported:
        raise PluginError("HTTP MCP 包含尚未支持的选项：" + "、".join(sorted(set(config) - supported)))
    url = config.get("url")
    if not isinstance(url, str) or len(url) > 4000:
        raise PluginError("HTTP MCP 需要 HTTPS 服务地址。")
    parsed = validate_url(url, loopback_http=True)
    if any(k.lower() in {"token", "access_token", "key", "api_key", "secret"} for k, _ in urllib.parse.parse_qsl(parsed.query)):
        raise PluginError("不要把凭据写进 URL，请使用环境变量。")
    if config.get("type") not in (None, "http", "streamable-http"):
        raise PluginError("这里支持 Streamable HTTP，不支持旧 SSE 传输配置。")
    bearer = config.get("bearer_token_env_var", "")
    if not isinstance(bearer, str) or bearer and not VARIABLE.fullmatch(bearer):
        raise PluginError("Bearer 凭据只填写环境变量名称。")
    for key in ("http_headers", "env_http_headers"):
        headers = config.get(key, {})
        if not isinstance(headers, dict):
            raise PluginError("HTTP 头配置必须是对象。")
        for name, value in headers.items():
            if not isinstance(name, str) or not HEADER.fullmatch(name) or not isinstance(value, str) or any(c in value for c in ("\r", "\n", "\x00")):
                raise PluginError("HTTP 头配置不合法。")
            if name.lower() in ("host", "content-length", "cookie", "proxy-authorization", "mcp-session-id", "mcp-protocol-version"):
                raise PluginError("这个 HTTP 头不能由 MCP 配置覆盖。")
            if key == "env_http_headers" and not VARIABLE.fullmatch(value):
                raise PluginError("env_http_headers 的值必须是环境变量名称。")
            if key == "http_headers" and re.search(r"authorization|token|secret|api.?key|cookie", name, re.I):
                raise PluginError("授权 HTTP 头必须通过 env_http_headers 或 bearer_token_env_var 设置。")


class HttpMcp:
    def __init__(self, config, http=None):
        validate_http_config(config)
        self.config, self.http = config, http or PluginHttp()
        self.session = ""
        self.version = ""

    def headers(self):
        headers = {"Accept": "application/json, text/event-stream", **self.config.get("http_headers", {})}
        for name, variable in self.config.get("env_http_headers", {}).items():
            value = os.environ.get(variable)
            if not value or any(c in value for c in ("\r", "\n", "\x00")):
                raise PluginError("MCP 授权环境变量未设置或内容不合法。")
            headers[name] = value
        variable = self.config.get("bearer_token_env_var")
        if variable:
            token = os.environ.get(variable)
            if not token or any(c in token for c in ("\r", "\n", "\x00")):
                raise PluginError("MCP Bearer 环境变量未设置或内容不合法。")
            headers["Authorization"] = "Bearer " + token
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        return headers

    async def forward(self, message):
        response = await self.http.request("POST", self.config["url"], headers=self.headers(), json_body=message, timeout=120, allow_loopback=True, rpc_id=message.get("id"))
        if not 200 <= response.status < 300:
            raise PluginError(f"HTTP MCP 失败（HTTP {response.status}），请检查服务地址与授权。")
        if response.headers.get("mcp-session-id"):
            session = response.headers["mcp-session-id"]
            if len(session) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                raise PluginError("MCP 服务返回了无效的会话标识。")
            self.session = session
        if "id" not in message:
            return None
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                answer = None
                for event in response.text().replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        value = json.loads(data)
                        if isinstance(value, dict) and value.get("id") == message["id"]:
                            answer = value
            else:
                answer = response.json()
            if not isinstance(answer, dict) or answer.get("id") != message["id"] or answer.get("jsonrpc") != "2.0":
                raise ValueError()
            if message.get("method") == "initialize" and isinstance(answer.get("result"), dict):
                version = answer["result"].get("protocolVersion", "2025-06-18")
                if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
                    raise ValueError()
                self.version = version
            return answer
        except (ValueError, UnicodeError) as error:
            raise PluginError("HTTP MCP 没有返回匹配的 JSON-RPC 响应。") from error

    async def close(self):
        if self.session:
            try:
                await self.http.request("DELETE", self.config["url"], headers=self.headers(), timeout=5, allow_loopback=True)
            except PluginError:
                pass
