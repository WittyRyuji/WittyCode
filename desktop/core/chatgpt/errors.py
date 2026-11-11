"""What can go wrong between WittyCode and OpenAI's sign-in and Responses endpoints.

``ChatGPTError`` carries what the server said (HTTP status, machine-readable ``code``, request id) so a failure is shown
and logged with the facts OpenAI asks for; ``friendly_failure`` words it for the user. The documented usage-limit codes
(``subscription_sharing_*``) have their own texts: OpenAI never moves a request to another billing path by itself, so the
user must be told which limit stopped it and where to change it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

USAGE_URL = "https://chatgpt.com/settings/usage"
"""Where the user sees and sets the limits of the apps that use their plan."""


class ChatGPTError(Exception):
    """A request to OpenAI failed. ``code`` is the server's own (or ``network`` / ``stream`` for our own findings)."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
        body: Any = None,
        transient: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.request_id = request_id
        self.body = body
        self.transient = transient
        """``True``: asking again may well work (a connection that broke); ``None``: judged by the status and the code (``is_transient``)."""
        self.retry_after = retry_after
        """Seconds the server asked to wait before asking again (``Retry-After``)."""

    def reworded(self, message: str, code: str | None = None) -> ChatGPTError:
        """The same failure with a sentence of its own (and, when given, another code): what the user is told, with the facts kept."""
        return ChatGPTError(message, code=code or self.code, status=self.status, request_id=self.request_id, body=self.body, transient=self.transient, retry_after=self.retry_after)

    def __str__(self) -> str:
        parts = [self.message]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.code:
            parts.append(self.code)
        if self.request_id:
            parts.append(f"request {self.request_id}")
        return " · ".join(parts)


class AuthError(ChatGPTError):
    """Signing in or renewing the sign-in failed. ``terminal``: the tokens cannot be used again, sign in anew."""

    def __init__(self, message: str, *, terminal: bool = False, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.terminal = terminal


class PlanNotEnabled(AuthError):
    """The sign-in worked but ``chatgpt.tokens.use.direct`` was not granted: the plan cannot pay for requests."""


REFRESH_TERMINAL_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)
"""Refresh failures after which the refresh token is useless (the documented list)."""

USAGE_LIMIT_CODES = frozenset({"subscription_sharing_usage_limit_exceeded"})

TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
TRANSIENT_CODES = frozenset(
    {
        "network",  # (the connection broke)
        "stream",  # (the answer was cut off)
        "server_error",
        "rate_limit_exceeded",
        "rate_limit_error",
        "overloaded",
        "overloaded_error",
        "api_error",
        "service_unavailable",
        "timeout",
        "throttlingException",
        "ThrottlingException",
        "serviceUnavailableException",
        "ServiceUnavailableException",
        "internalServerException",
        "InternalServerException",
        "modelStreamErrorException",
        "modelTimeoutException",
        "ModelTimeoutException",
        "modelNotReadyException",
    }
)
PERMANENT_CODES = frozenset({"insufficient_quota", "billing_not_active", "invalid_api_key", "context_length_exceeded", "ServiceQuotaExceededException"})


def is_transient(error: BaseException) -> bool:
    """Whether asking again, a little later, may work: the connection broke, the server is busy or overloaded, an answer was cut off.
    A quota that ran out, a refused key or a request that is wrong stay wrong however often they are sent."""
    if not isinstance(error, ChatGPTError):
        return False
    code = error.code or ""
    if code in PERMANENT_CODES or code.startswith(("subscription_sharing_", "chatpass_")):
        return False
    if error.transient is not None:
        return error.transient
    return error.status in TRANSIENT_STATUSES or code in TRANSIENT_CODES

_TEXTS = {
    "subscription_sharing_user_not_eligible": "这个 ChatGPT 账号（或工作区）不能把套餐用量给应用使用。换一个账号，或改用别的接入方式。",
    "subscription_sharing_usage_limit_exceeded": "ChatGPT 套餐用量到了上限（可能是整个套餐，也可能是给这个应用设的上限）。到 ChatGPT 设置的用量页查看或调整。",
    "subscription_sharing_usage_unavailable": "暂时查不到 ChatGPT 套餐的可用量，稍后再试。",
    "subscription_sharing_unsupported_capability": "这次请求里有 ChatGPT 套餐不支持的内容。",
    "subscription_sharing_route_not_supported": "ChatGPT 套餐用量不支持这个接口。",
    "subscription_sharing_invalid_user": "ChatGPT 登录已经失效，请重新登录。",
    "subscription_sharing_user_unavailable": "暂时取不到你的 ChatGPT 账号信息，稍后再试。",
    "chatpass_v2_scope_not_authorized": "这次授权不包含这项操作的权限：请在设置里重新登录 ChatGPT。",
    "chatpass_v2_invalid_authorization_context": "这次授权不包含这项操作的权限：请在设置里重新登录 ChatGPT。",
}


def error_from_body(status: int, body: bytes, headers: Mapping[str, str]) -> ChatGPTError:
    """The error for an HTTP failure. The body is either ``{"error": {"code", "message", "param"}}`` or, before a
    Responses request starts, ``{"detail": "..."}`` (diagnostic text only), or something else entirely."""
    import json

    request_id = headers.get("x-request-id") or headers.get("openai-request-id") or headers.get("x-amzn-requestid")
    text = body.decode("utf-8", "replace").strip()
    parsed: Any = None
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    code: str | None = None
    message = ""
    if isinstance(parsed, Mapping):
        error = parsed.get("error")
        if isinstance(error, Mapping):
            code = _text(error.get("code")) or _text(error.get("type")) or None
            message = _text(error.get("message"))
            param = _text(error.get("param"))
            if param:
                message = f"{message} (param: {param})" if message else f"param: {param}"
        elif isinstance(error, str):
            code = error
            message = _text(parsed.get("error_description"))
        if not message:
            message = _text(parsed.get("detail")) or _text(parsed.get("message")) or _text(parsed.get("Message"))  # (AWS capitalises it at times)
    if code is None and headers.get("x-amzn-errortype"):  # (AWS names the error in a header: "ValidationException:http://...")
        code = headers["x-amzn-errortype"].split(":", 1)[0].strip() or None
    if not message:
        message = text[:300] or f"HTTP {status}"
    wait = headers.get("retry-after-ms")
    seconds: float | None = None
    try:
        seconds = float(wait) / 1000 if wait else (float(headers["retry-after"]) if headers.get("retry-after") else None)
    except ValueError:
        seconds = None  # (a date: not worth parsing, the backoff is as good)
    return ChatGPTError(message, code=code, status=status, request_id=request_id, body=parsed if parsed is not None else text, retry_after=seconds)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


_SIGN_IN_TEXTS = {
    "timeout": "登录等了太久，已经取消。需要的话再点一次 Continue with ChatGPT。",
    "cancelled": "已取消登录。",
    "access_denied": "你在浏览器里拒绝了授权，没有登录。",
    "state_mismatch": "登录回调和这次请求对不上（可能是旧的页面），已丢弃。请重新点 Continue with ChatGPT。",
    "invalid_callback": "浏览器返回的登录结果不完整，请重新点 Continue with ChatGPT。",
    "registration_incomplete": "OpenAI 没有返回这个应用的注册信息，登录没有完成。稍后再试。",
    "client_mismatch": "OpenAI 返回的应用标识和这个账号保存的不一致，已拒绝。请重新登录。",
    "account_mismatch": "浏览器里登录的不是原来那个 ChatGPT 账号，已拒绝。要换账号请用「添加另一个账号」。",
    "invalid_id_token": "OpenAI 返回的登录凭证没有通过校验，已拒绝。稍后再试。",
    "invalid_jwks": "取不到 OpenAI 的验签公钥，登录没有完成。检查网络后再试。",
    "invalid_token_response": "OpenAI 返回的令牌格式不对，登录没有完成。",
    "browser": "打不开浏览器。请检查系统的默认浏览器设置后再试。",
    "listen": "本机的登录回调端口被占用，登录没有开始。关掉占用的程序后再试。",
    "storage": "登录成功了，但保存凭据失败（磁盘或权限问题）。",
    "signed_out": "ChatGPT 已退出登录，请重新登录。",
    "busy": "已经有一次登录在进行了：先在浏览器里完成或取消它。",
}


def friendly_failure(error: BaseException) -> str:
    """What to tell the user about a failed request (the facts are in the log; this is the sentence)."""
    if isinstance(error, PlanNotEnabled):
        return "ChatGPT 已登录，但没有允许应用使用你的套餐。在设置里重新登录并同意「使用 ChatGPT 套餐」。"
    if isinstance(error, ChatGPTError) and error.code == "api":
        return error.message  # (an API provider's: already a sentence that names the provider)
    if isinstance(error, ChatGPTError) and error.code in _SIGN_IN_TEXTS:
        return _SIGN_IN_TEXTS[error.code]
    if isinstance(error, AuthError):
        return "ChatGPT 登录已失效，请在设置里重新登录。" if error.terminal else f"ChatGPT 登录出错：{error.message}"
    if isinstance(error, ChatGPTError):
        text = _TEXTS.get(error.code or "")
        if text:
            return text
        if error.code == "network":
            return f"连不上 OpenAI：{error.message}"
        if error.status == 401:
            return "ChatGPT 没有接受这次登录凭据：请在设置里重新登录。"
        if error.status == 403:
            return f"OpenAI 拒绝了这次请求（可能是地区或权限限制）：{error.message}"
        if error.status == 429:
            return "请求太频繁或额度用尽，稍后再试；也可以到 ChatGPT 设置的用量页查看。"
        if error.status and error.status >= 500:
            return f"OpenAI 暂时不可用（HTTP {error.status}），稍后再试。"
        return error.message or "请求失败。"
    return f"{type(error).__name__}: {error}"
