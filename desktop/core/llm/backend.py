"""The API providers' models for the engine: which models there are, and a request to one of them, sent and read.

``ApiBackend`` is what ``ChatGPTEngine`` hands a request whose model is an ``api:...`` one (the engine is the loop's whole
view of "a model": ``stream``, ``model_info``, ``model_window``). The providers themselves (base URL, kind, key, headers, the
window and the answer's room the user gave) are the entries of ``ProviderStore``, which is also what the settings page edits, so
a model added there is usable at once and nothing has to be started or restarted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from desktop.core.chatgpt.errors import ChatGPTError, RequestNotSent
from desktop.core.chatgpt.http import HttpClient, stream_idle_timeout
from desktop.core.chatgpt.models import ChatGPTModel
from desktop.core.chatgpt.responses import (
    CallBegun, CallDone, Event, Finished, ItemDone, ReasoningDelta, ReasoningDone, TextDelta, TextDone, read_events,
)
from desktop.core.llm import aws, bedrock, grok
from desktop.core.llm.anthropic import VERSION, anthropic_body, read_anthropic
from desktop.core.llm.chat import chat_body, read_chat
from desktop.core.llm.responses_api import responses_body
from desktop.core.llm.wire import ANTHROPIC, CHAT, RESPONSES, Route, sse_data
from desktop.core.providers import BEDROCK, EFFORTS, ApiModelSpec, BedrockPlan, ProviderError

log = logging.getLogger("desktop.llm")

PREFIX = "api:"
"""What every model id of an API provider starts with (the page's model ids are ``api:<provider id>:<custom id>``)."""

KIND_OF = {"anthropic": ANTHROPIC, "openai": RESPONSES, "generic-chat-completion-api": CHAT}
LEVELS = EFFORTS
"""What the reasoning menu offers a model whose entry sets a level (the editor says so too)."""


class Providers(Protocol):
    def api_models(self) -> list[ApiModelSpec]: ...
    def api_stamp(self) -> tuple[int, int]: ...
    def api_key(self, spec: ApiModelSpec) -> str | None: ...
    def api_headers(self, spec: ApiModelSpec) -> dict[str, str]: ...
    def bedrock_plan(self, spec: ApiModelSpec) -> BedrockPlan: ...


def is_api_model(model_id: str) -> bool:
    return model_id.startswith(PREFIX)


def model_of(spec: ApiModelSpec) -> ChatGPTModel:
    """The model as the engine lists it."""
    return ChatGPTModel(
        spec.id, spec.display, LEVELS if spec.reasoning else (), spec.reasoning, spec.window, f"{spec.provider_name} · {spec.native}", spec.images
    )


def route_of(spec: ApiModelSpec, key: str | None, headers: Mapping[str, str]) -> Route:
    """Where a request to the model goes and how it is signed."""
    kind = KIND_OF[spec.kind]
    sent: dict[str, str] = {"Accept": "text/event-stream"}
    if kind == ANTHROPIC:
        sent["anthropic-version"] = VERSION
        if key:
            sent["Authorization" if spec.bearer else "x-api-key"] = f"Bearer {key}" if spec.bearer else key
    elif key:
        sent["Authorization"] = f"Bearer {key}"
    for name, value in headers.items():  # (the user's headers replace ours of the same name, whatever the case)
        for existing in [k for k in sent if k.lower() == name.lower()]:
            del sent[existing]
        sent[name] = value
    return Route(kind, spec.base_url, spec.native, sent, spec.extra_args, spec.max_output, spec.thinking,
                 spec.reasoning_args if spec.bedrock is None else {}, supports_images=spec.images)


def api_failure(error: ChatGPTError, spec: ApiModelSpec) -> ChatGPTError:
    """The error for the user: which provider said what, and what to look at. Failures whose own words say it best (a 400 about the
    request, a context that is too long) are left as they are."""
    who = spec.provider_name
    status = error.status
    detail = f"\n{error.message}" if error.message and error.code != "network" else ""
    if spec.bedrock is not None:
        return bedrock_failure(error, spec, detail)
    if error.code == "network":
        text = f"连不上 {who}：{error.message}"
    elif status in (401, 403):
        text = f"{who} 没有接受这个密钥（HTTP {status}）：检查密钥是否有效，是否有权使用「{spec.display}」。{detail}"
    elif status == 404:
        text = f"{who} 找不到这个接口或模型「{spec.native}」（HTTP 404）：检查 Base URL 和模型 ID。{detail}"
    elif status == 429:
        text = f"{who} 说请求太频繁或额度用完了（HTTP 429）。{detail}"
    elif status is not None and status >= 500:
        text = f"{who} 暂时出错了（HTTP {status}）。{detail}"
    else:
        return error
    return error.reworded(text, "api")


def _key_echo_error() -> ChatGPTError:
    return ChatGPTError("服务端响应包含 API Key，已停止接收。", code="api", transient=False)


class _KeyEchoGuard:
    """Block literal echoes at the output boundary, not protocol words. Not a defense against arbitrary encoding by a server."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.escaped = json.dumps(key)[1:-1]
        self.tails: dict[tuple[type, str], str] = {}

    def literal(self, text: str | None) -> bool:
        return bool(text) and (self.key in text or self.escaped in text)

    def _values(self, value: Any, *, replay_item: bool = False) -> bool:
        if isinstance(value, str):
            return self.literal(value)
        if isinstance(value, dict):
            for field, child in value.items():
                if replay_item:
                    if field in ("type", "role", "status"):  # protocol discriminators, not content
                        continue
                    if field == "arguments" and isinstance(child, str):
                        if self._arguments(child):
                            return True
                        continue
                if self._values(child, replay_item=replay_item):
                    return True
        elif isinstance(value, list):
            return any(self._values(child, replay_item=replay_item) for child in value)
        return False

    def _arguments(self, text: str) -> bool:
        try:
            args = json.loads(text)
        except ValueError:
            return self.literal(text)  # even an invalid argument string can become a tool diagnostic
        return self._values(args)

    def check(self, event: Event) -> Event:
        if isinstance(event, (TextDelta, ReasoningDelta)):
            channel = (type(event), event.item_id)
            text = self.tails.get(channel, "") + event.text
            if self.key in text or event.item_id == self.key:
                raise _key_echo_error()
            # No buffering latency: only keep a suffix that could still start a cross-fragment match.
            candidate = text.find(self.key[0], max(0, len(text) - len(self.key) + 1))
            if candidate == -1:
                self.tails.pop(channel, None)
            else:
                if channel not in self.tails and len(self.tails) >= 128:
                    raise ChatGPTError("服务端同时打开了太多输出项，已停止接收。", code="api", transient=False)
                self.tails[channel] = text[candidate:]
        elif isinstance(event, (TextDone, ReasoningDone)):
            self.tails.pop((TextDelta if isinstance(event, TextDone) else ReasoningDelta, event.item_id), None)
            if self.literal(event.text) or event.item_id == self.key:
                raise _key_echo_error()
        elif isinstance(event, (CallBegun, CallDone)):
            if self.literal(event.name) or self.literal(event.call_id) or event.item_id == self.key:
                raise _key_echo_error()
            if isinstance(event, CallDone) and self._arguments(event.arguments):
                raise _key_echo_error()
        elif isinstance(event, ItemDone) and self._values(event.item, replay_item=True):
            raise _key_echo_error()
        elif isinstance(event, Finished):
            # Keep usage and non-reflecting completion details; never put an echo in the request trace.
            return replace(event, response_id="" if self.literal(event.response_id) else event.response_id,
                           incomplete="incomplete" if self.literal(event.incomplete) else event.incomplete,
                           diagnostics={} if self._values(event.diagnostics) else event.diagnostics)
        return event


async def _quiet_sse(lines: AsyncIterator[str], guard: _KeyEchoGuard, kind: str) -> AsyncIterator[str]:
    # Wire readers can log malformed packets verbatim. Only an apparent literal in the wire text needs an extra parse;
    # valid JSON is left to the normal reader, regardless of its protocol field names.
    async for data in sse_data(lines):
        if data.strip() == "[DONE]":
            if kind != ANTHROPIC:
                yield "data: [DONE]"
                yield ""
            continue
        if guard.literal(data):
            try:
                valid = isinstance(json.loads(data), dict)
            except ValueError:
                valid = False
            if not valid:
                raise _key_echo_error()
        for line in data.split("\n"):
            yield f"data: {line}"
        yield ""


_EXPIRED = ("ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException", "InvalidSignatureException", "InvalidClientTokenId", "IncompleteSignature", "MissingAuthenticationToken")


def bedrock_failure(error: ChatGPTError, spec: ApiModelSpec, detail: str) -> ChatGPTError:
    """The error for a request to Bedrock: AWS names the error (``code``), and the usual causes have a usual cure."""
    who, status, code = spec.provider_name, error.status, error.code or ""
    text = error.message.lower()
    if error.code == "network":
        message = f"连不上 {who}：{error.message}"
    elif code in _EXPIRED or "security token" in text and "expired" in text:
        message = f"AWS 没有接受这次请求的凭据（{code or 'HTTP ' + str(status)}）：凭据可能过期了，重新登录（例如 aws sso login），或检查密钥和区域。{detail}"
    elif "api key" in text:
        message = f"Bedrock 不认这个 API Key（HTTP {status}）：检查环境变量 {aws.BEARER_VARIABLE} 的值是不是完整的 Bedrock API Key，以及它是否过期、区域对不对。{detail}"
    elif code == "AccessDeniedException" or status == 403:
        message = f"AWS 拒绝了这次请求（HTTP {status}）：这个账号有没有权限调用「{spec.native}」？到 Bedrock 控制台的 Model access 开通它，并检查区域。{detail}"
    elif "on-demand throughput" in text:
        message = f"这个模型不能按需直接调用，要用推理配置文件（inference profile）的 ID：在模型 ID 前加地区前缀（us. / eu. / apac.）或 global.，或填配置文件的 ARN。{detail}"
    elif status == 404 or code == "ResourceNotFoundException":
        message = f"Bedrock 找不到模型「{spec.native}」（HTTP {status}）：检查模型 ID 和区域，这个区域有没有它。{detail}"
    elif status == 429 or code in ("ThrottlingException", "throttlingException", "ServiceQuotaExceededException"):
        message = f"Bedrock 说请求太频繁或额度用完了（{code or 'HTTP 429'}）。{detail}"
    elif status is not None and status >= 500:
        message = f"Bedrock 暂时出错了（HTTP {status}）。{detail}"
    else:
        return error
    return error.reworded(message, "api")


class ApiBackend:
    def __init__(self, providers: Providers, http: HttpClient | None = None, *, grok_subscription: grok.GrokSubscription | None = None) -> None:
        self.providers = providers
        self.http = http or HttpClient()
        self.grok = grok_subscription
        self._specs: tuple[tuple[int, int], list[ApiModelSpec]] | None = None

    def specs(self) -> list[ApiModelSpec]:
        """The providers' models, read again only when their files changed (the loop asks for a window many times a turn)."""
        stamp = self.providers.api_stamp()
        if self._specs is None or self._specs[0] != stamp:
            self._specs = (stamp, [spec for spec in self.providers.api_models() if not spec.id.startswith(grok.PREFIX)])
        return self._specs[1] + self.grok.specs() if self.grok is not None else self._specs[1]

    def models(self) -> list[ChatGPTModel]:
        return [model_of(spec) for spec in self.specs()]

    def spec(self, model_id: str) -> ApiModelSpec | None:
        if model_id.startswith(grok.PREFIX):
            return next((spec for spec in self.grok.specs() if spec.id == model_id), None) if self.grok is not None else None
        return next((spec for spec in self.specs() if spec.id == model_id), None) if is_api_model(model_id) else None

    async def route(self, spec: ApiModelSpec) -> Route:
        """The route with the key read now (it may need a command or Windows DPAPI)."""
        route, _ = await self._route_and_protected_key(spec)
        return route

    async def _route_and_protected_key(self, spec: ApiModelSpec) -> tuple[Route, str | None]:
        if spec.id.startswith(grok.PREFIX):
            subscription = self.spec(spec.id)
            if self.grok is None or subscription is None:
                raise grok.GrokError("这个 Grok 订阅模型当前不可用，请检查登录和模型权限。")
            token = await self.grok.access_token()
            return route_of(subscription, token, {}), token  # never use custom provider endpoints or API keys

        def build() -> tuple[Route, str | None]:
            key = self.providers.api_key(spec)
            return route_of(spec, key, self.providers.api_headers(spec)), key if spec.key.mode == "protected" else None

        try:
            return await asyncio.to_thread(build)
        except ProviderError as error:
            raise RequestNotSent(str(error), code="api") from None

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[Event]:
        """Ask the model of the request: its body for the provider's wire format, and the events of the answer."""
        model_id = str(request.get("model") or "")
        spec = self.spec(model_id)
        subscription = model_id.startswith(grok.PREFIX)
        if spec is None:
            if subscription:
                raise grok.GrokError("这个 Grok 订阅模型当前不可用，请检查登录和模型权限。")
            raise ChatGPTError("这个模型已经不在 API Provider 的设置里了：在设置里把它加回去，或换一个模型。", code="api")
        if spec.window is None:
            raise ChatGPTError(f"不知道「{spec.native}」的上下文上限：服务端数据和内置规格表都没有提供。请在设置 › 账号与模型中填写这个模型的上下文窗口后再发送。", code="api")
        if spec.bedrock is not None:
            async for event in self._bedrock(spec, request):
                yield event
            return
        route, key = await self._route_and_protected_key(spec)
        if route.kind == CHAT:
            body, read = chat_body(request, route), lambda lines: read_chat(lines, route.origin)
        elif route.kind == ANTHROPIC:
            body, read = anthropic_body(request, route), lambda lines: read_anthropic(lines, route.origin)
        else:
            body, read = responses_body(request, route), read_events
            if subscription:
                for field in ("prompt_cache_key", "prompt_cache_retention", "service_tier"):
                    body.pop(field, None)  # OpenAI-specific cache/routing fields
        lines = self.http.stream_lines("POST", route.url, headers=route.headers, json_body=body, timeout=stream_idle_timeout(request))
        guard = _KeyEchoGuard(key) if key else None
        checked = _quiet_sse(lines, guard, route.kind) if guard is not None else None
        try:
            async for event in read(checked if checked is not None else lines):
                yield guard.check(event) if guard is not None else event
        except ChatGPTError as error:
            if guard is None:
                raise api_failure(error, spec) from error
            if any(guard.literal(text) for text in (error.message, error.code, error.request_id)):
                safe = ChatGPTError("服务端错误信息包含 API Key，已隐藏。", status=error.status, transient=False)
            else:
                # Keep safe error details, never the untrusted raw body (including arbitrary property names).
                safe = ChatGPTError(error.message, status=error.status, code=error.code, request_id=error.request_id,
                                    transient=error.transient, retry_after=error.retry_after)
            if subscription and (failure := grok.inference_failure(safe)) is not None:
                raise failure from None
            raise api_failure(safe, spec) from None
        except Exception:
            if key:
                raise ChatGPTError("处理 API Provider 响应时出错。", code="api") from None
            raise
        finally:
            if checked is not None:
                await checked.aclose()
            await lines.aclose()

    async def _bedrock(self, spec: ApiModelSpec, request: dict[str, Any]) -> AsyncIterator[Event]:
        """A request to a model on Amazon Bedrock: the region, endpoint and credentials looked up now, the body for the API the entry's
        ``provider`` names, signed (or sent with the Bedrock API key), and the answer's event stream read."""
        try:
            plan = await asyncio.to_thread(self.providers.bedrock_plan, spec)
            user_headers = await asyncio.to_thread(self.providers.api_headers, spec)
        except ProviderError as error:
            raise RequestNotSent(str(error), code="api") from error
        kind = spec.kind
        mantle = kind == "openai"
        base = plan.base_url.rstrip("/") if plan.base_url else (f"https://bedrock-mantle.{plan.region}.api.aws/v1" if mantle else bedrock.endpoint(plan.region))
        wire = {"anthropic": ANTHROPIC, "openai": RESPONSES}.get(kind, "converse")
        route = Route(wire, base, spec.native, {}, spec.extra_args, spec.max_output, spec.thinking, supports_images=spec.images)
        headers: dict[str, str] = {"Content-Type": "application/json", **user_headers}
        if kind == BEDROCK:
            url, body = bedrock.model_url(base, spec.native, "converse-stream"), bedrock.converse_body(request, route, plan.metadata)
        elif kind == "anthropic":
            url, body = bedrock.model_url(base, spec.native, "invoke-with-response-stream"), bedrock.invoke_body(request, route)
            if plan.metadata:
                headers["X-Amzn-Bedrock-Request-Metadata"] = json.dumps(plan.metadata, ensure_ascii=False, separators=(",", ":"))
        else:
            url, body = base + "/responses", responses_body(request, route)
            headers["Accept"] = "text/event-stream"
        data = await asyncio.to_thread(lambda: json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if plan.access.bearer:
            sent = {**headers, "Authorization": f"Bearer {plan.access.bearer}"}
        elif plan.access.credentials is not None:
            service = "bedrock-mantle" if mantle and "bedrock-mantle" in urlsplit(url).netloc else "bedrock"
            sent = aws.sign("POST", url, headers, data, plan.access.credentials, plan.region, service)
        else:
            raise ChatGPTError("没有可用的 AWS 凭据。", code="api")
        origin = urlsplit(base).netloc.lower()
        if kind == "openai":
            lines = self.http.stream_lines("POST", url, headers=sent, body=data, timeout=stream_idle_timeout(request))
            events = read_events(lines)
            closer = lines
        else:
            chunks = self.http.stream_chunks("POST", url, headers=sent, body=data, timeout=stream_idle_timeout(request))
            events = bedrock.read_converse(chunks, origin) if kind == BEDROCK else read_anthropic(bedrock.invoke_lines(chunks), origin)
            closer = chunks
        try:
            async for event in events:
                yield event
        except ChatGPTError as error:
            raise api_failure(error, spec) from error
        finally:
            await closer.aclose()
