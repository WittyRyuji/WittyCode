"""Grok subscription device sign-in, separate from xAI Console API keys and Grok Build sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from asyncio import sleep
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from desktop.core import secret_protection
from desktop.core.chatgpt.errors import ChatGPTError, RequestNotSent
from desktop.core.chatgpt.http import HttpClient
from desktop.core.providers import ApiModelSpec, _NO_KEY
from desktop.core.session_archive_io import atomic_write

AUTH_ORIGIN = "https://auth.x.ai"
DEVICE_URL = f"{AUTH_ORIGIN}/oauth2/device/code"
TOKEN_URL = f"{AUTH_ORIGIN}/oauth2/token"
SUBSCRIPTION_URL = "https://cli-chat-proxy.grok.com/v1"
PROVIDER_ID = "grok-subscription"
PREFIX = f"api:{PROVIDER_ID}:"
# Public application identifier (not a client secret): the shared Grok Build device client used by OpenClaw.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_ENTROPY = b"WittyCode/GrokSubscription/v1"
_KNOWN_WINDOWS = {"grok-4.7": 500_000, "grok-4.3": 1_000_000, "grok-build": 128_000, "grok-build-0.1": 128_000}
# https://docs.x.ai/developers/model-capabilities/text/reasoning
_KNOWN_REASONING = {
    "grok-4.3": (("off", "low", "medium"), "low"),
    "grok-4.5": (("low", "medium", "high"), "high"),
    "grok-4.6": (("low", "medium", "high", "xhigh"), "high"),
    "grok-4.7": (("low", "medium", "high", "xhigh"), "high"),
}
_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh")
# Minimum compatibility version for the subscription proxy (see OpenClaw extensions/xai/stream.ts).
_PROXY_CLIENT_VERSION = "1.0.13"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_CREDENTIAL = re.compile(r"[!-~]{16,16384}")


def _positive(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 10_000_000 else None


def _verification_url(value: object) -> str:
    if isinstance(value, str) and len(value) <= 2048 and not any(c.isspace() or c == "\\" for c in value):
        try:
            url = urlsplit(value)
            if url.scheme == "https" and url.netloc in {"accounts.x.ai", "auth.x.ai"} and not url.fragment:
                return value
        except ValueError:
            pass
    return ""


class GrokError(RequestNotSent):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="grok_subscription", transient=False)


@dataclass(frozen=True)
class _Tokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires: int


def _tokens(data: dict[str, Any], previous_refresh: str = "") -> _Tokens:
    access = data.get("access_token")
    refresh = data.get("refresh_token", previous_refresh)
    expires = _positive(data.get("expires_in"))
    if not (isinstance(access, str) and _CREDENTIAL.fullmatch(access)
            and isinstance(refresh, str) and _CREDENTIAL.fullmatch(refresh) and expires is not None):
        raise GrokError("xAI 没有返回完整的 Grok 订阅凭据。")
    return _Tokens(access, refresh, int(time.time()) + expires)


def _effort(value: object) -> str:
    if not isinstance(value, str):
        return ""
    level = value.strip().lower()
    if level == "none":
        return "off"
    return level if level in _EFFORTS else ""


def _reasoning(model_id: str, listed: tuple[str, ...] | list[str] | None, default: str) -> tuple[tuple[str, ...], str]:
    known, documented_default = _KNOWN_REASONING.get(model_id.lower().removesuffix("-latest"), ((), ""))
    if listed is None:
        levels = known
    else:
        normalized = {_effort(value) for value in listed}
        levels = tuple(level for level in (known or _EFFORTS) if level in normalized)
    preferred = (_effort(default), documented_default, *levels)
    return levels, next((level for level in preferred if level in levels), "")


@dataclass(frozen=True)
class _Model:
    id: str
    window: int
    images: bool
    reasoning_efforts: tuple[str, ...] | list[str] | None = None  # old encrypted records have no catalog metadata
    default_effort: str = ""

    def spec(self) -> ApiModelSpec:
        efforts, default = _reasoning(self.id, self.reasoning_efforts, self.default_effort)
        return ApiModelSpec(
            id=PREFIX + self.id, native=self.id, display=self.id, provider_id=PROVIDER_ID, provider_name="Grok 订阅",
            kind="openai", base_url=SUBSCRIPTION_URL, window=self.window, max_output=None, reasoning=default, images=self.images,
            bearer=False, headers=(), extra_args={}, key=_NO_KEY, supported_efforts=efforts,
        )


def _models(payload: dict[str, Any], access: str) -> tuple[_Model, ...]:
    rows = payload.get("data")
    result: dict[str, _Model] = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("supports_tools") is False:
            continue
        model = row.get("id") or row.get("model")
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model) or access in model:
            continue
        native = model.lower()
        if native.startswith("grok-imagine") or "multi-agent" in native:
            continue
        backend = row.get("api_backend") or row.get("apiBackend") or row.get("backend")
        if not (isinstance(backend, str) and backend.lower() in {"responses", "chat", "language"}
                or backend is None and native in _KNOWN_WINDOWS):
            continue
        window = _positive(row.get("context_window")) or _positive(row.get("contextWindow")) or _KNOWN_WINDOWS.get(native)
        if window is not None:
            modalities = row.get("input_modalities")
            images = ("image" in modalities if isinstance(modalities, list)
                      else row.get("supports_images") is True or native in {"grok-4.7", "grok-4.3"})
            supports = row.get("supports_reasoning_effort", row.get("supportsReasoningEffort"))
            listed = row.get("reasoning_efforts", row.get("reasoningEfforts"))
            efforts = tuple(e for value in listed if (e := _effort(value))) if isinstance(listed, list) else None
            if supports is False:
                efforts = ()
            default = _effort(row.get("default_reasoning_effort", row.get("defaultReasoningEffort")))
            result.setdefault(model, _Model(model, window, images, efforts, default))
    return tuple(result.values())


def inference_headers(model: str) -> dict[str, str]:
    # This is the proxy's routing protocol, independent of how the app runs the agent or bills API-key models.
    return {"X-XAI-Token-Auth": "xai-grok-cli", "x-grok-client-version": _PROXY_CLIENT_VERSION, "x-grok-model-override": model}


def prepare_responses(body: dict[str, Any], request: Mapping[str, Any], spec: ApiModelSpec) -> None:
    for field in ("prompt_cache_key", "prompt_cache_retention", "service_tier"):
        body.pop(field, None)  # OpenAI-specific cache/routing fields
    reasoning = body.get("reasoning")
    effort = request.get("wc_reasoning_effort") or (reasoning.get("effort") if isinstance(reasoning, Mapping) else None)
    if not effort:
        return  # omit the field to use the subscription model's default
    level = _effort(effort)
    if level not in spec.efforts:
        raise GrokError(f"Grok 订阅模型「{spec.display}」不支持所选思考等级，请换一个档位。")
    if level == "off":
        body["reasoning"] = {"effort": "none"}
        body.pop("include", None)
    else:
        body["reasoning"] = {**(reasoning if isinstance(reasoning, Mapping) else {}), "effort": level}


class GrokSubscription:
    """Owns one account. The GUI sees only the user code and status; only this backend reads the tokens."""

    def __init__(self, path: Path, *, open_browser: Callable[[str], None], http: HttpClient | None = None) -> None:
        self.path = path
        self.http = http if http is not None else HttpClient()
        self.open_browser = open_browser
        self.on_models_changed: Callable[[], None] = lambda: None
        self._tokens: _Tokens | None = None
        self._models: tuple[_Model, ...] = ()
        self._login: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()
        self._code = self._verify = self._error = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(secret_protection.crypt(self.path.read_bytes(), decrypt=True, entropy=_ENTROPY))
            tokens = _Tokens(**data["tokens"])
            models = tuple(_Model(**row) for row in data["models"])
            self._tokens, self._models = tokens, models
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, secret_protection.ProtectionError):
            self._error = "无法读取已保存的 Grok 登录，请重新登录。"

    def _save(self, tokens: _Tokens, models: tuple[_Model, ...]) -> None:
        data = {"tokens": vars(tokens), "models": [vars(model) for model in models]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, secret_protection.crypt(json.dumps(data).encode("utf-8"), entropy=_ENTROPY))
        except (OSError, secret_protection.ProtectionError):
            raise GrokError("无法加密保存 Grok 登录。") from None
        self._tokens, self._models = tokens, models

    def specs(self) -> list[ApiModelSpec]:
        return [model.spec() for model in self._models] if self._tokens is not None else []

    def account(self) -> dict[str, Any]:
        auth = "signing-in" if self._login is not None else "signed-in" if self._tokens is not None else "signed-out"
        return {"auth": auth, "code": self._code, "canOpen": bool(self._verify), "error": self._error}

    def login(self) -> None:
        if self._login is None and self._tokens is None:
            self._error = ""
            self._login = asyncio.create_task(self._device_login())

    def reopen(self) -> None:
        if self._verify:
            self.open_browser(self._verify)

    async def cancel_login(self) -> None:
        if task := self._login:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self._login is task:
                self._login = None
                self._code = self._verify = ""

    async def logout(self) -> None:
        await self.cancel_login()
        self._forget()
        self._error = ""

    def _forget(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise GrokError("无法移除本机保存的 Grok 登录。") from None
        self._tokens, self._models = None, ()
        self.on_models_changed()

    async def _request(self, url: str, *, form: dict[str, str] | None = None, access: str = "") -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if access:
            headers["Authorization"] = f"Bearer {access}"
        try:
            r = await self.http.request("POST" if form is not None else "GET", url, form=form, headers=headers, timeout=12.0)
        except ChatGPTError:
            raise GrokError("连不上 xAI，检查网络后重试。") from None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status, data if isinstance(data, dict) else {}

    async def _device_login(self) -> None:
        try:
            status, data = await self._request(DEVICE_URL, form={"client_id": CLIENT_ID, "scope": SCOPE})
            if status != 200:
                raise GrokError(f"xAI 未接受 Grok 设备登录（HTTP {status}）。")
            code, user_code = data.get("device_code"), data.get("user_code")
            verify = _verification_url(data.get("verification_uri"))
            if not (isinstance(code, str) and 0 < len(code) <= 2048 and isinstance(user_code, str)
                    and re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code) and verify):
                raise GrokError("xAI 返回了无效的设备授权信息。")
            self._code = user_code
            self._verify = _verification_url(data.get("verification_uri_complete")) or verify
            with contextlib.suppress(OSError):  # the explicit browser button can be used again
                self.reopen()
            timeout = min(_positive(data.get("expires_in")) or 900, 1800)
            interval = _positive(data.get("interval")) or 5
            async with asyncio.timeout(timeout):
                tokens = await self._poll(code, interval)
            status, data = await self._request(SUBSCRIPTION_URL + "/models", access=tokens.access)
            if status in (401, 403):
                raise GrokError(f"xAI 不允许此账号访问 Grok 订阅模型（HTTP {status}）。请检查订阅、地区和模型权限。")
            if status != 200:
                raise GrokError(f"无法获取 Grok 订阅模型（HTTP {status}）。")
            models = _models(data, tokens.access)
            if not models:
                raise GrokError("这个账号没有返回可用于 WittyCode 的 Grok 订阅模型。")
            self._save(tokens, models)
            self.on_models_changed()
        except TimeoutError:
            self._error = "Grok 设备授权已过期，请重新登录。"
        except GrokError as error:
            self._error = error.message
        finally:
            self._login = None
            self._code = self._verify = ""

    async def _poll(self, device_code: str, interval: int) -> _Tokens:
        while True:
            await sleep(interval)
            status, data = await self._request(TOKEN_URL, form={
                "grant_type": DEVICE_GRANT, "client_id": CLIENT_ID, "device_code": device_code,
            })
            if status == 200:
                return _tokens(data)
            error = data.get("error")
            if error in ("authorization_pending", "slow_down"):
                interval += 5 if error == "slow_down" else 0
                continue
            if error == "expired_token":
                raise TimeoutError
            if error == "access_denied":
                raise GrokError("Grok 登录未授权。")
            raise GrokError(f"无法完成 Grok 设备授权（HTTP {status}）。")

    async def access_token(self) -> str:
        async with self._refresh_lock:
            old = self._tokens
            if old is None:
                raise GrokError("请先在「账号与模型」登录 Grok 订阅。")
            if old.expires > time.time() + 60:
                return old.access
            status, data = await self._request(TOKEN_URL, form={
                "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": old.refresh,
            })
            if self._tokens is not old:  # a sign-out or new login happened while the request was on the wire
                raise GrokError("Grok 登录已更改，未发送模型请求。")
            if status == 400 and data.get("error") == "invalid_grant":
                self._error = "Grok 登录已失效，请重新登录。"
                self._forget()
                raise GrokError(self._error)
            if status != 200:
                raise GrokError(f"无法续期 Grok 登录（HTTP {status}），请稍后重试。")
            tokens = _tokens(data, old.refresh)
            self._save(tokens, self._models)
            return tokens.access


def inference_failure(error: ChatGPTError) -> ChatGPTError | None:
    texts = {
        401: "Grok 订阅登录不再被接受，请在「账号与模型」退出后重新登录。",
        403: "xAI 拒绝了这个 Grok 订阅请求，请检查账号订阅、地区或该模型的访问权限。未改用 API Key。",
        426: "xAI 要求更新 Grok 订阅接入（HTTP 426）。请更新 WittyCode；未改用 API Key。",
        429: "Grok 订阅已达到用量上限或暂时被限流。未改用 API Key。",
    }
    if error.status in texts:
        return ChatGPTError(texts[error.status], status=error.status, code="api", transient=False)
    return None
