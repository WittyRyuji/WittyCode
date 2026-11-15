"""Browser OAuth for bundled connectors; tokens are protected by the current Windows user's DPAPI."""

from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import urllib.parse

from desktop.core.plugin_http import PluginError, PluginHttp, checked_json
from desktop.core.plugin_lock import lock_file as _credential_lock
from desktop.core.settings import default_settings_path

PROVIDERS = {
    "github": {"name": "GitHub", "authorize": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token", "scopes": "repo read:user", "port": 8766, "callbackPath": "/oauth/callback", "help": "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps"},
    "google-workspace": {"name": "Google Workspace", "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token", "scopes": "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly", "port": 8767, "callbackPath": "/", "help": "https://developers.google.com/identity/protocols/oauth2/native-app"},
}
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _credential(value, *, optional=False):
    return isinstance(value, str) and (bool(value) or optional) and len(value) <= 16384 and all(33 <= ord(c) <= 126 for c in value)


def _token_response(value, *, previous=None, default_expires=3600):
    if not isinstance(value, dict) or not _credential(value.get("access_token")) or str(value.get("token_type", "bearer")).lower() != "bearer":
        raise PluginError("服务没有返回有效的 Bearer 授权，请检查 OAuth 客户端配置。")
    refresh = value.get("refresh_token") or (previous or {}).get("refresh_token", "")
    expires = value.get("expires_in", default_expires)
    if not _credential(refresh, optional=True) or type(expires) not in (int, float) or not math.isfinite(expires) or not 0 <= expires <= 366 * 86400:
        raise PluginError("服务返回的授权凭据或有效期不合法。")
    return {"access_token": value["access_token"], "refresh_token": refresh, "expires_at": time.time() + expires if expires else 0}


def _read(file: Path, default):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise PluginError("插件账号配置不可读，未覆盖原文件。") from error


def _write(file: Path, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".connector-", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, file)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _crypt(data: bytes, *, decrypt=False) -> bytes:
    if sys.platform != "win32":
        raise PluginError("账号安全存储需要 Windows DPAPI。")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise PluginError("无法读取或保护账号凭据，请使用原 Windows 用户重新授权。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


class TokenStore:
    def __init__(self, file: Path):
        self.file = file

    def load(self, provider):
        raw = _read(self.file, {})
        if not isinstance(raw, dict):
            raise PluginError("账号凭据文件损坏，未覆盖。")
        record = raw.get(provider, {})
        if not isinstance(record, dict):
            raise PluginError("账号配置损坏，未覆盖。")
        value = record.get("credentials")
        if value is None:
            return None
        try:
            token = json.loads(_crypt(base64.b64decode(value, validate=True), decrypt=True))
            if not isinstance(token, dict) or not _credential(token.get("access_token")) or not _credential(token.get("refresh_token", ""), optional=True) or type(token.get("expires_at", 0)) not in (int, float) or not math.isfinite(token.get("expires_at", 0)):
                raise ValueError()
            return token
        except (ValueError, TypeError, UnicodeError) as error:
            raise PluginError("账号凭据损坏，请重新授权。") from error

    def save(self, provider, token, *, valid=None, activate=False):
        with _credential_lock(self.file):
            return self._save(provider, token, valid=valid, activate=activate)

    def _save(self, provider, token, *, valid=None, activate=False):
        raw = _read(self.file, {})
        if not isinstance(raw, dict) or not isinstance(raw.get(provider, {}), dict):
            raise PluginError("账号凭据文件损坏，未覆盖。")
        record = raw.setdefault(provider, {})
        before = {"credentials": record.get("credentials"), "enabled": record.get("enabled", False)}
        if token is None:
            record.pop("credentials", None)
        else:
            record["credentials"] = base64.b64encode(_crypt(json.dumps(token).encode("utf-8"))).decode("ascii")
        if activate:
            record["enabled"] = True
        if valid is not None and not valid():
            raise PluginError("授权已取消。")
        _write(self.file, raw)
        return before, record.get("credentials")

    def restore(self, provider, before, expected):
        with _credential_lock(self.file):
            raw = _read(self.file, {})
            record = raw.get(provider, {})
            if record.get("credentials") != expected:
                return  # a later disconnect or sign-in takes precedence
            if before["credentials"] is None:
                record.pop("credentials", None)
            else:
                record["credentials"] = before["credentials"]
            if record.get("enabled"):
                record["enabled"] = before["enabled"]
            _write(self.file, raw)


class OAuthFlow:
    def __init__(self, provider, config, http, open_browser):
        self.provider, self.config, self.http, self.open_browser = provider, config, http, open_browser
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.callback = None
        self.cancelled = False
        self.connections = set()

    def cancel(self):
        self.cancelled = True
        if self.callback is not None:
            def stop():
                if not self.callback.done():
                    self.callback.set_exception(PluginError("授权已取消。"))
            self.callback.get_loop().call_soon_threadsafe(stop)

    async def _receive(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        status, message = "400 Bad Request", "Invalid authorization callback."
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            first = raw.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _ = first.split(" ", 2)
            parsed = urllib.parse.urlsplit(target)
            query = urllib.parse.parse_qs(parsed.query, max_num_fields=16)
            state = query.get("state", [""])[0]
            if method == "GET" and not parsed.netloc and parsed.path == PROVIDERS[self.provider]["callbackPath"] and state.isascii() and hmac.compare_digest(state, self.state) and not self.callback.done():
                if query.get("error"):
                    self.callback.set_exception(PluginError("账号授权未完成或已被拒绝。"))
                elif query.get("code") and len(query["code"][0]) <= 4096:
                    self.callback.set_result(query["code"][0])
                    status, message = "200 OK", "Authorization received. You may return to WittyCode."
            body = message.encode("utf-8")
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            self.connections.discard(task)

    async def run(self):
        definition = PROVIDERS[self.provider]
        self.callback = asyncio.get_running_loop().create_future()
        try:
            server = await asyncio.start_server(self._receive, "127.0.0.1", self.config["port"], limit=16_384)
        except OSError as error:
            self.callback.cancel()
            raise PluginError("授权回调端口不可用，请更改端口或关闭占用该端口的程序。") from error
        redirect = f"http://127.0.0.1:{self.config['port']}{definition['callbackPath']}"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        params = {"client_id": self.config["clientId"], "redirect_uri": redirect, "response_type": "code", "scope": definition["scopes"], "state": self.state, "code_challenge": challenge, "code_challenge_method": "S256"}
        if self.provider == "google-workspace":
            params.update(access_type="offline", prompt="consent")
        try:
            self.open_browser(definition["authorize"] + "?" + urllib.parse.urlencode(params))
            try:
                code = await asyncio.wait_for(self.callback, 300)
            except TimeoutError as error:
                raise PluginError("账号授权超时，请重试。") from error
            form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": self.config["clientId"], "code_verifier": self.verifier}
            variable = self.config["clientSecretEnv"]
            if variable:
                secret = os.environ.get(variable)
                if not secret:
                    raise PluginError("客户端密钥环境变量尚未设置，请设置后重试。")
                form["client_secret"] = secret
            token = checked_json(await self.http.request("POST", definition["token"], form=form))
            if self.cancelled:
                raise PluginError("授权已取消。")
            return _token_response(token, default_expires=0 if self.provider == "github" else 3600)
        finally:
            server.close()
            await server.wait_closed()
            for task in list(self.connections):
                task.cancel()
            if self.connections:
                await asyncio.gather(*self.connections, return_exceptions=True)
            if not self.callback.done():
                self.callback.cancel()
            elif not self.callback.cancelled():
                self.callback.exception()


class ConnectorStore:
    def __init__(self, root: Path | None = None, *, http=None):
        self.root = root or default_settings_path().parent
        self.file = self.root / "connector-settings.json"
        self.tokens = TokenStore(self.file)
        self.http = http or PluginHttp()
        self.flows = {}

    def config(self, provider):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise PluginError("没有这种内置连接器。")
        raw = _read(self.file, {})
        if not isinstance(raw, dict):
            raise PluginError("连接器配置损坏，未覆盖。")
        stored = raw.get(provider, {})
        if not isinstance(stored, dict):
            raise PluginError("连接器客户端配置损坏，未覆盖。")
        defaults = {"clientId": "", "clientSecretEnv": "", "port": PROVIDERS[provider]["port"], "enabled": False}
        return {key: stored.get(key, value) for key, value in defaults.items()}

    def views(self):
        return [{"id": provider, "name": definition["name"], **self.config(provider), "connected": self.tokens.load(provider) is not None, "signingIn": provider in self.flows, "scopes": definition["scopes"], "helpUrl": definition["help"], "callbackUrl": f"http://127.0.0.1:{self.config(provider)['port']}{definition['callbackPath']}"} for provider, definition in PROVIDERS.items()]

    def configure(self, provider, config):
        with _credential_lock(self.file):
            return self._configure(provider, config)

    def _configure(self, provider, config):
        before = self.config(provider)
        if not isinstance(config, dict) or set(config) - {"clientId", "clientSecretEnv", "port", "enabled"}:
            raise PluginError("连接器配置不合法。")
        value = {**before, **config}
        if not isinstance(value["clientId"], str) or len(value["clientId"]) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in value["clientId"]):
            raise PluginError("Client ID 不合法。")
        variable = value["clientSecretEnv"]
        if not isinstance(variable, str) or variable and not VARIABLE.fullmatch(variable):
            raise PluginError("客户端密钥只填写环境变量名称，不填写密钥正文。")
        if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535 or type(value["enabled"]) is not bool:
            raise PluginError("授权端口必须为 1024–65535，启用状态必须为布尔值。")
        changed = any(before[k] != value[k] for k in ("clientId", "clientSecretEnv", "port"))
        if provider in self.flows and changed:
            raise PluginError("请先取消正在进行的授权，再修改客户端配置。")
        if config.get("enabled") is False:
            self.cancel(provider)
        raw = _read(self.file, {})
        raw[provider] = {**raw.get(provider, {}), **value}
        if changed:
            raw[provider].pop("credentials", None)
        _write(self.file, raw)
        return self.views()

    async def login(self, provider, open_browser):
        config = self.config(provider)
        if provider in self.flows:
            raise PluginError("这个连接器正在等待浏览器授权。")
        if not config["clientId"] or provider == "github" and not config["clientSecretEnv"]:
            raise PluginError("请先配置 OAuth Client ID 和客户端密钥环境变量。")
        if config["clientSecretEnv"] and not os.environ.get(config["clientSecretEnv"]):
            raise PluginError("客户端密钥环境变量尚未设置，请设置后重试。")
        flow = OAuthFlow(provider, config, self.http, open_browser)
        self.flows[provider] = flow
        try:
            token = await flow.run()
            if flow.cancelled:
                raise PluginError("授权已取消。")
            before, expected = await asyncio.to_thread(self.tokens.save, provider, token, valid=lambda: not flow.cancelled, activate=True)
            if flow.cancelled:
                await asyncio.to_thread(self.tokens.restore, provider, before, expected)
                raise PluginError("授权已取消。")
        except BaseException:
            flow.cancel()
            raise
        finally:
            self.flows.pop(provider, None)
        return self.views()

    def cancel(self, provider):
        self.config(provider)
        if provider in self.flows:
            self.flows[provider].cancel()

    def disconnect(self, provider):
        self.cancel(provider)
        self.tokens.save(provider, None)
        return self.views()

    def commands(self):
        from desktop.core.memory.launch import McpCommand, PACKAGE_ROOT, console_python
        commands = []
        for provider in PROVIDERS:
            try:
                config = self.config(provider)
                if not config["enabled"] or self.tokens.load(provider) is None:
                    continue
            except PluginError:
                continue  # an optional account must not suppress existing local MCP servers
            env = {"PYTHONPATH": str(PACKAGE_ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            variable = config["clientSecretEnv"]
            if variable and variable in os.environ:
                env[variable] = os.environ[variable]
            commands.append(McpCommand("wittycode-" + provider, console_python(), ("-m", "desktop.core.plugin_server", "--provider", provider, "--settings", str(self.root / "settings.json")), env, trusted_annotations=True))
        return commands

    async def access_token(self, provider):
        token = await asyncio.to_thread(self.tokens.load, provider)
        if token and (not token.get("expires_at") or token["expires_at"] > time.time() + 60):
            return token["access_token"]
        return await asyncio.to_thread(self._refresh, provider)

    def _refresh(self, provider):
        with _credential_lock(self.tokens.file):
            token = self.tokens.load(provider)
            if token is None:
                raise PluginError("请先在设置 → 插件中授权这个连接器。")
            if not token.get("expires_at") or token["expires_at"] > time.time() + 60:
                return token["access_token"]
            config = self.config(provider)
            if not token.get("refresh_token"):
                raise PluginError("账号授权已过期，请重新连接。")
            form = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": config["clientId"]}
            if config["clientSecretEnv"]:
                secret = os.environ.get(config["clientSecretEnv"])
                if not secret:
                    raise PluginError("刷新授权需要的客户端密钥环境变量尚未设置。")
                form["client_secret"] = secret
            refreshed = checked_json(asyncio.run(self.http.request("POST", PROVIDERS[provider]["token"], form=form)))
            updated = _token_response(refreshed, previous=token)
            self.tokens._save(provider, updated)
            return updated["access_token"]
