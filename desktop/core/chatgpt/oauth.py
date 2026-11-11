"""Sign in with ChatGPT for an open-source app: OAuth 2.0 authorization code with PKCE (S256), as a public client.

The protocol (``docs/chatgpt-plan-harness.md`` section 2, from OpenAI's "ChatGPT plan usage" pages):

* The first sign-in of an account registers a client: ``client_id=dynamic_agent_client`` plus ``agent_name_hint``
  (the app's name). The callback brings the issued ``client_id`` (``oaiapp_...``); that one is used for the code
  exchange and for every later sign-in, refresh and revocation of the registration. Authorizing again sends the saved
  id and no ``agent_name_hint``, and may send ``id_token_hint`` (the last ID token: skips the account selector) and
  ``login_hint`` (the email). ``ext_agent_host_id`` (one stable ``urn:uuid:`` per machine) goes with every sign-in.
* The browser comes back to ``http://127.0.0.1:<port>/auth/callback`` (that host and path exactly; any port, but the
  same URI in the authorization request and in the code exchange). The listener lives for one attempt only.
* The ID token is verified (RS256 against the JWKS; issuer, audience = the issued client id, expiry, nonce) and its
  ``sub`` is the account; re-authorizing must come back with the same ``sub``. ``chatgpt.tokens.use.direct`` must be
  among the granted scopes for the plan to pay for requests. Without it the sign-in still counts (the record's
  ``plan_enabled`` is False and the app offers to authorize again), as the documentation asks.
* A refresh rotates the refresh token: the new one replaces the old together with the access token and the scopes.
  The documented unusable-refresh-token codes are terminal; ``invalid_client`` is a configuration error; a network
  failure changes nothing.
* Signing out revokes the refresh token (an empty 200 is success, also for a token that was already invalid); network
  failures and 5xx answers are retried a few times.

Nothing here logs a token, a code, a PKCE verifier or an authorization URL (which can carry the ID token as a hint).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import inspect
import logging
import math
import re
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeVar

from desktop.core.chatgpt import jwt
from desktop.core.chatgpt.errors import REFRESH_TERMINAL_CODES, AuthError, ChatGPTError, error_from_body
from desktop.core.chatgpt.http import HttpClient, HttpResponse

if TYPE_CHECKING:
    from desktop.core.chatgpt.accounts import AccountRecord, AccountStore

log = logging.getLogger("desktop.chatgpt")

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
IDENTITY_SCOPES = ("openid", "profile", "email")
PLAN_SCOPES = ("offline_access", "resource.invoke", PLAN_SCOPE)
"""The scopes of ChatGPT plan usage, in the documentation's words (the identity scopes come on top)."""
SCOPES = (*IDENTITY_SCOPES, *PLAN_SCOPES)
"""What every sign-in asks for: the complete set, also when authorizing again."""

REGISTRATION_CLIENT_ID = "dynamic_agent_client"
"""The client id of a first registration only: never saved, never used for a code exchange."""
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/auth/callback"

SIGN_IN_SECONDS = 300.0
DEFAULT_EXPIRES_IN = 3600.0
"""An access token's lifetime when the token response does not say (the documented one)."""
REVOKE_TIMEOUT = 10.0
REQUEST_SECONDS = 10.0
"""How long a browser connection may take to send its request (a speculative pre-connection sends nothing)."""
ANSWER_SECONDS = 90.0
"""How long the browser's callback request waits for the outcome (code exchange, ID token) before a neutral page."""
SETTLE_SECONDS = 2.0
"""How long ``run`` waits, once it knows the outcome, for the browser to be given its page."""
MAX_FIELDS = 64

_ACCEPT_JSON = {"Accept": "application/json"}
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,256}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Where the sign-in happens. ``resource`` is the API the tokens are for (sent with the code exchange and refresh)."""

    issuer: str
    authorize: str
    token: str
    revoke: str
    jwks: str
    resource: str

    @classmethod
    def default(cls) -> Endpoints:
        """OpenAI's (the same URLs as ``https://auth.openai.com/.well-known/openid-configuration``)."""
        return cls.for_issuer(ISSUER)

    @classmethod
    def for_issuer(cls, base_url: str, *, resource: str = RESOURCE) -> Endpoints:
        """OpenAI's paths under another origin (a test server)."""
        base = base_url.rstrip("/")
        return cls(
            issuer=base,
            authorize=f"{base}/api/accounts/authorize",
            token=f"{base}/api/accounts/oauth/token",
            revoke=f"{base}/api/accounts/oauth/revoke",
            jwks=f"{base}/.well-known/jwks.json",
            resource=resource,
        )


def pkce_verifier(length: int = 64) -> str:
    """A fresh PKCE code verifier: ``length`` (43 to 128) characters of the URL-safe alphabet, from ``secrets``."""
    if not 43 <= length <= 128:
        raise ValueError("a PKCE code verifier has 43 to 128 characters")
    return secrets.token_urlsafe(96)[:length]


def pkce_challenge(verifier: str) -> str:
    """The S256 challenge: base64url (no padding) of the verifier's SHA-256."""
    return jwt.b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(
    endpoints: Endpoints,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    host_id: str,
    app_name: str,
    client_id: str | None = None,
    id_token_hint: str | None = None,
    login_hint: str | None = None,
    force_consent: bool = False,
) -> str:
    """The authorization request. ``client_id`` None is a first registration (``agent_name_hint`` = ``app_name``);
    otherwise the hints may be sent. ``force_consent`` adds ``prompt=consent`` (only when the user asked to enable plan
    usage after declining it: not on ordinary sign-ins)."""
    registering = not client_id
    params = {
        "response_type": "code",
        "client_id": client_id or REGISTRATION_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "resource": endpoints.resource,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "ext_agent_host_id": host_id,
    }
    if registering:
        params["agent_name_hint"] = app_name
    else:
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if login_hint:
            params["login_hint"] = login_hint
    if force_consent:
        params["prompt"] = "consent"
    separator = "&" if urllib.parse.urlsplit(endpoints.authorize).query else "?"
    return endpoints.authorize + separator + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


@dataclass(frozen=True, slots=True)
class _Callback:
    code: str = field(repr=False)
    client_id: str
    scopes: tuple[str, ...]


class SignIn:
    """One attempt to sign in, from opening the browser to a verified ``AccountRecord``.

    ``client_id`` None registers a new client (a new account, or another registration of one); otherwise it is the
    saved client id of the account to authorize again (with its last ID token as ``id_token_hint`` and its email as
    ``login_hint``, when known). ``open_browser(url)`` opens the system browser; it may be a coroutine function.
    ``store``, when given, gets the verified record (saved, then made the active account) before ``run`` returns, and
    tells which account (``sub``) a re-authorization must come back with. ``port`` 0 takes any free port.

    While ``run`` waits for the browser, ``redirect_uri`` and ``url`` are set (``url`` is for a "copy this link"
    fallback; it can carry the ID token, so it is never logged). ``issued_client_id`` is set as soon as a callback is
    accepted: a registration whose code exchange then fails is retried with that id, not registered again.
    """

    def __init__(
        self,
        http: HttpClient,
        endpoints: Endpoints,
        *,
        host_id: str,
        app_name: str,
        open_browser: Callable[[str], Any],
        client_id: str | None = None,
        id_token_hint: str | None = None,
        login_hint: str | None = None,
        force_consent: bool = False,
        port: int = 0,
        store: AccountStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        client_id = client_id or None
        if not host_id:
            raise ValueError("a sign-in needs this machine's host id (AccountStore.host_id())")
        if client_id == REGISTRATION_CLIENT_ID:
            raise ValueError("dynamic_agent_client only registers a client; it is not an account's client id")
        if client_id is None and not app_name:
            raise ValueError("a registration needs the app's name (agent_name_hint)")
        self._http = http
        self._endpoints = endpoints
        self._host_id = host_id
        self._app_name = app_name
        self._open = open_browser
        self._client_id = client_id
        self._id_token_hint = id_token_hint
        self._login_hint = login_hint
        self._force_consent = force_consent
        self._port = port
        self._store = store
        self._clock = clock
        self._state = secrets.token_urlsafe(32)
        self._nonce = secrets.token_urlsafe(32)
        self._verifier = pkce_verifier()
        self.redirect_uri: str | None = None
        self.url: str | None = None
        self.issued_client_id: str | None = client_id
        self._started = False
        self._cancel_requested = False
        self._callback: asyncio.Future[_Callback] | None = None
        self._outcome: asyncio.Future[bool] | None = None
        self._stop: asyncio.Future[None] | None = None
        self._connections: set[asyncio.Task[Any]] = set()
        self._answering: set[asyncio.Task[Any]] = set()

    def cancel(self) -> None:
        """End the attempt: ``run`` raises ``AuthError(code="cancelled")``. Any time, from any thread."""
        self._cancel_requested = True
        stop = self._stop
        if stop is None or stop.done():
            return
        loop = stop.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            stop.set_result(None)
        else:
            with contextlib.suppress(RuntimeError):  # (the loop is closed: nothing is waiting any more)
                loop.call_soon_threadsafe(_resolve, stop)

    async def run(self, timeout: float = SIGN_IN_SECONDS) -> AccountRecord:
        """Listen on the loopback, open the browser, wait for the callback (at most ``timeout`` seconds), exchange the
        code and verify the ID token. Raises ``AuthError`` (``timeout``, ``cancelled``, ``access_denied``, ...) or a
        ``ChatGPTError`` of the network; the listener is closed in every case."""
        if self._started:
            raise RuntimeError("a SignIn runs once; make a new one for another attempt")
        self._started = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        self._callback = loop.create_future()
        self._outcome = loop.create_future()
        self._stop = loop.create_future()
        if self._cancel_requested:
            self._stop.set_result(None)
        try:
            server = await asyncio.start_server(self._serve, CALLBACK_HOST, self._port)
        except OSError as error:
            log.warning("ChatGPT sign-in: cannot listen on port %d: %s", self._port, error)
            raise AuthError(f"cannot listen on {CALLBACK_HOST}:{self._port}: {error}", code="listen") from error
        succeeded = False
        try:
            port = server.sockets[0].getsockname()[1]
            redirect_uri = f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"
            self.redirect_uri = redirect_uri
            self.url = authorize_url(
                self._endpoints,
                redirect_uri=redirect_uri,
                state=self._state,
                nonce=self._nonce,
                code_challenge=pkce_challenge(self._verifier),
                host_id=self._host_id,
                app_name=self._app_name,
                client_id=self._client_id,
                id_token_hint=self._id_token_hint,
                login_hint=self._login_hint,
                force_consent=self._force_consent,
            )
            what = "a new registration" if self._client_id is None else f"client {self._client_id}"
            log.info("ChatGPT sign-in (%s): waiting for the browser on port %d", what, port)
            await self._until(self._open_browser(self.url), deadline)
            callback = await self._until(self._callback, deadline)
            record = await self._until(self._exchange(callback, redirect_uri), None)
            if self._store is not None:
                try:
                    self._store.save(record)
                    self._store.set_active(record.client_id)
                except OSError as error:
                    raise AuthError(f"could not save the sign-in: {error}", code="storage") from error
            succeeded = True
            plan = "allowed" if record.plan_enabled else f"NOT granted (no {PLAN_SCOPE})"
            log.info("ChatGPT sign-in done: client %s, plan usage %s", record.client_id, plan)
            return record
        except ChatGPTError as error:
            if error.code in ("cancelled", "timeout"):
                log.info("ChatGPT sign-in ended: %s", error)
            else:
                log.warning("ChatGPT sign-in failed: %s", error)
            raise
        finally:
            server.close()
            if not self._callback.done():
                self._callback.cancel()  # (a connection that is still open cannot hand in a callback any more)
            elif not self._callback.cancelled():
                self._callback.exception()  # (a failed callback nobody waited for is not "never retrieved")
            if not self._outcome.done():
                self._outcome.set_result(succeeded)
            await self._settle()

    async def _until(self, awaitable: Awaitable[_T], deadline: float | None) -> _T:
        """``awaitable``'s result, unless ``cancel`` or the deadline comes first."""
        assert self._stop is not None
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(awaitable)
        if self._stop.done() and not task.done():
            # (cancelled already: the step does not start at all, so no browser opens and no request goes out)
            task.cancel()
            await asyncio.wait({task}, timeout=5.0)
            raise AuthError("the sign-in was cancelled", code="cancelled")
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        try:
            done, _ = await asyncio.wait({task, self._stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.wait({task}, timeout=5.0)
        if self._stop.done():
            raise AuthError("the sign-in was cancelled", code="cancelled")
        raise AuthError("the browser did not come back in time", code="timeout")

    async def _open_browser(self, url: str) -> None:
        try:
            opened = self._open(url)
            if inspect.isawaitable(opened):
                await opened
        except Exception as error:
            raise AuthError(f"could not open the browser: {type(error).__name__}: {error}", code="browser") from error

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One browser connection. A request for anything but the callback (``/favicon.ico``, a pre-connection that
        sends nothing) is answered or dropped without touching the attempt; the callback request is answered once the
        outcome is known, so the page says whether signing in really worked."""
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            request = await asyncio.wait_for(_read_request(reader), REQUEST_SECONDS)
            if request is None:
                return
            method, target = request
            split = urllib.parse.urlsplit(target)
            if method != "GET":
                await _answer(writer, 405)
                return
            if split.path != CALLBACK_PATH:
                await _answer(writer, 404)
                return
            if task is not None:
                self._answering.add(task)
            self._take(split.query)
            assert self._outcome is not None
            try:
                outcome: bool | None = await asyncio.wait_for(asyncio.shield(self._outcome), ANSWER_SECONDS)
            except asyncio.TimeoutError:
                outcome = None
            await _answer(writer, 200, _page(outcome, self._app_name), kind="text/html; charset=utf-8")
        except Exception as error:  # (a browser that went away, garbage on the socket: nothing to tell anyone)
            log.debug("ChatGPT sign-in: a browser connection failed (%s)", type(error).__name__)
        finally:
            if task is not None:
                self._answering.discard(task)
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _take(self, query: str) -> None:
        if self._callback is None or self._callback.done():
            return
        try:
            self._callback.set_result(self._check(_query(query)))
        except AuthError as error:
            self._callback.set_exception(error)

    def _check(self, params: Mapping[str, str]) -> _Callback:
        state = params.get("state")
        if state is not None and not hmac.compare_digest(state.encode("utf-8"), self._state.encode("utf-8")):
            raise AuthError("the callback's state is not this sign-in's", code="state_mismatch")
        error = params.get("error")
        if error:
            description = _clean(params.get("error_description", ""))
            detail = f": {description}" if description else ""
            if error == "access_denied":
                raise AuthError(f"the authorization was declined{detail}", code="access_denied")
            code = _clean(error, 80)
            raise AuthError(f"the authorization failed ({code}){detail}", code=code)
        if state is None:
            raise AuthError("the callback carries no state", code="state_mismatch")
        code = params.get("code")
        if not code:
            raise AuthError("the callback carries no authorization code", code="invalid_callback")
        returned = params.get("client_id") or None
        if self._client_id is None:
            if returned is None or returned == REGISTRATION_CLIENT_ID or not _CLIENT_ID.fullmatch(returned):
                raise AuthError(
                    "registration incomplete: the callback carries no issued client_id", code="registration_incomplete"
                )
            client_id = returned
        else:
            # (re-authorization: the callback may leave the client id out, but must not bring another one)
            if returned is not None and returned != self._client_id:
                raise AuthError(
                    f"the callback is for client {_clean(returned, 80)!r}, not for this account's {self._client_id!r}",
                    code="client_mismatch",
                )
            client_id = self._client_id
        self.issued_client_id = client_id
        return _Callback(code=code, client_id=client_id, scopes=_scopes(params.get("scope")))

    async def _exchange(self, callback: _Callback, redirect_uri: str) -> AccountRecord:
        from desktop.core.chatgpt.accounts import AccountRecord

        endpoints = self._endpoints
        started = self._clock()
        response = await self._http.request(
            "POST",
            endpoints.token,
            headers=_ACCEPT_JSON,
            form={
                "grant_type": "authorization_code",
                "client_id": callback.client_id,
                "code": callback.code,
                "code_verifier": self._verifier,
                "redirect_uri": redirect_uri,
                "resource": endpoints.resource,
            },
        )
        if response.status != 200:
            raise _failure("the code exchange failed", response)
        tokens = _token_response(response, ("access_token", "refresh_token", "id_token"))
        claims = jwt.verify_id_token(
            tokens["id_token"],
            await fetch_jwks(self._http, endpoints),
            issuer=endpoints.issuer,
            client_id=callback.client_id,
            nonce=self._nonce,
            now=self._clock(),
        )
        expected = self._expected_subject()
        if expected is not None and claims["sub"] != expected:
            raise AuthError(
                "the browser signed in to another ChatGPT account than the one being authorized again", code="account_mismatch"
            )
        email = claims.get("email")
        return AccountRecord(
            email=email if isinstance(email, str) else "",
            issuer=endpoints.issuer,
            subject=claims["sub"],
            client_id=callback.client_id,
            ext_agent_host_id=self._host_id,
            id_token=tokens["id_token"],
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_type=_token_type(tokens),
            expires_in=_expires_in(tokens),
            scopes=_scopes(tokens.get("scope")) or callback.scopes or SCOPES,
            saved_at=started,
        )

    def _expected_subject(self) -> str | None:
        """The account a re-authorization must come back with: the saved one's ``sub``, else the hint's."""
        if self._client_id is None:
            return None
        if self._store is not None:
            saved = self._store.get(self._client_id)
            if saved is not None and saved.subject:
                return saved.subject
        if self._id_token_hint:
            with contextlib.suppress(AuthError):
                subject = jwt.decode(self._id_token_hint)[1].get("sub")
                if isinstance(subject, str) and subject:
                    return subject
        return None

    async def _settle(self) -> None:
        answering = {task for task in self._answering if not task.done()}
        if answering:
            await asyncio.wait(answering, timeout=SETTLE_SECONDS)
        rest = {task for task in self._connections if not task.done()}
        for task in rest:
            task.cancel()
        if rest:
            await asyncio.wait(rest, timeout=1.0)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


async def fetch_jwks(http: HttpClient, endpoints: Endpoints) -> dict[str, Any]:
    """The issuer's signing keys (fetched for every sign-in: keys rotate, and a sign-in is rare)."""
    response = await http.request("GET", endpoints.jwks, headers=_ACCEPT_JSON)
    if response.status != 200:
        raise _failure("could not read OpenAI's signing keys", response)
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise AuthError("OpenAI's signing keys (JWKS) are not a key set", code="invalid_jwks", status=response.status)
    return data


async def refresh(
    http: HttpClient,
    endpoints: Endpoints,
    record: AccountRecord,
    *,
    clock: Callable[[], float] = time.time,
) -> AccountRecord:
    """``record`` with renewed tokens. The new refresh token, access token, expiry and scopes come together; a new ID
    token replaces the old one only when it is for the same account and client. Raises ``AuthError(terminal=True)``
    when the refresh token is unusable, ``AuthError`` for other refusals, and a plain ``ChatGPTError`` when OpenAI is
    unreachable or unavailable (the stored tokens are still good then)."""
    if not record.refresh_token:
        raise AuthError("this ChatGPT account is signed out (no refresh token)", terminal=True, code="signed_out")
    started = clock()
    response = await http.request(
        "POST",
        endpoints.token,
        headers=_ACCEPT_JSON,
        form={
            "grant_type": "refresh_token",
            "client_id": record.client_id,
            "refresh_token": record.refresh_token,
            "resource": endpoints.resource,
        },
    )
    if response.status != 200:
        raise _refresh_failure(response, record.client_id)
    tokens = _token_response(response, ("access_token",))
    return replace(
        record,
        access_token=tokens["access_token"],
        refresh_token=_text(tokens.get("refresh_token")) or record.refresh_token,
        id_token=_replacement_id_token(_text(tokens.get("id_token")), record),
        token_type=_token_type(tokens),
        expires_in=_expires_in(tokens),
        scopes=_scopes(tokens.get("scope")) or record.scopes,
        saved_at=started,
    )


async def revoke(
    http: HttpClient,
    endpoints: Endpoints,
    record: AccountRecord,
    *,
    attempts: int = 3,
    delay: float = 0.5,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> bool:
    """End the renewable session (revoke the refresh token). True only when OpenAI answered 200; network failures and
    5xx are retried (``delay``, doubling, between ``attempts``). False means not confirmed: the caller clears the tokens
    anyway and tells the user they can disconnect the app in ChatGPT's settings."""
    if not record.refresh_token:
        return False
    form = {"token": record.refresh_token, "token_type_hint": "refresh_token", "client_id": record.client_id}
    for attempt in range(attempts):
        if attempt:
            await sleep(delay * 2 ** (attempt - 1))
        try:
            response = await http.request("POST", endpoints.revoke, form=form, timeout=REVOKE_TIMEOUT)
        except ChatGPTError as error:
            log.info("ChatGPT sign-out: revoking client %s failed (attempt %d): %s", record.client_id, attempt + 1, error)
            continue
        if response.status == 200:
            log.info("ChatGPT sign-out: OpenAI confirmed the revocation for client %s", record.client_id)
            return True
        error = error_from_body(response.status, response.body, response.headers)
        if response.status < 500:
            log.warning("ChatGPT sign-out: OpenAI refused the revocation for client %s: %s", record.client_id, error)
            return False
        log.info("ChatGPT sign-out: revoking client %s failed (attempt %d): %s", record.client_id, attempt + 1, error)
    log.warning("ChatGPT sign-out: the revocation for client %s was not confirmed", record.client_id)
    return False


def _failure(what: str, response: HttpResponse) -> AuthError:
    error = error_from_body(response.status, response.body, response.headers)
    return AuthError(
        f"{what}: {error.message}", code=error.code, status=error.status, request_id=error.request_id, body=error.body
    )


def _refresh_failure(response: HttpResponse, client_id: str) -> ChatGPTError:
    error = error_from_body(response.status, response.body, response.headers)
    facts: dict[str, Any] = {"code": error.code, "status": error.status, "request_id": error.request_id, "body": error.body}
    if error.code in REFRESH_TERMINAL_CODES:
        return AuthError(f"the refresh token can no longer be used: {error.message}", terminal=True, **facts)
    if error.code == "invalid_client":
        return AuthError(
            f"OpenAI does not accept the client id {client_id} (a client configuration error, not an expired sign-in): "
            f"{error.message}",
            **facts,
        )
    if response.status >= 500 or response.status == 429:
        return ChatGPTError(f"the token endpoint is unavailable: {error.message}", **facts)
    return AuthError(f"renewing the sign-in failed: {error.message}", **facts)


def _token_response(response: HttpResponse, required: tuple[str, ...]) -> dict[str, Any]:
    # (never put this body into an error: it holds the tokens)
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AuthError(
            "the token endpoint's answer is not a JSON object", code="invalid_token_response", status=response.status
        )
    missing = [name for name in required if not _text(data.get(name))]
    if missing:
        raise AuthError(
            f"the token endpoint's answer has no {', '.join(missing)}", code="invalid_token_response", status=response.status
        )
    return data


def _replacement_id_token(token: str, record: AccountRecord) -> str:
    """A refresh's new ID token, if it is the same account's for the same client (it is only a hint and a label)."""
    if not token:
        return record.id_token
    try:
        _, claims = jwt.decode(token)
    except AuthError:
        log.warning("ChatGPT token renewal for client %s: the new ID token is unreadable, kept the old one", record.client_id)
        return record.id_token
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    if (record.subject and claims.get("sub") != record.subject) or record.client_id not in audiences:
        log.warning("ChatGPT token renewal for client %s: the new ID token is someone else's, kept the old one", record.client_id)
        return record.id_token
    return token


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        return ()
    return tuple(dict.fromkeys(item for item in items if item))


def _expires_in(tokens: Mapping[str, Any]) -> float:
    value = tokens.get("expires_in")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            value = None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return DEFAULT_EXPIRES_IN


def _token_type(tokens: Mapping[str, Any]) -> str:
    return _text(tokens.get("token_type")) or "Bearer"


def _clean(text: str, limit: int = 300) -> str:
    return _CONTROL.sub(" ", text).strip()[:limit]


def _query(query: str) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=MAX_FIELDS)
    except ValueError:
        raise AuthError("the callback has too many parameters", code="invalid_callback") from None
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise AuthError(f"the callback repeats the parameter {_clean(key, 40)!r}", code="invalid_callback")
        params[key] = value
    return params


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str] | None:
    """The method and target of an HTTP request (its headers are read and dropped); None for anything else."""
    parts = (await reader.readline()).decode("latin-1").split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    for _ in range(100):
        if (await reader.readline()) in (b"\r\n", b"\n", b""):
            return parts[0], parts[1]
    return None


_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}


async def _answer(
    writer: asyncio.StreamWriter, status: int, body: bytes = b"", *, kind: str = "text/plain; charset=utf-8"
) -> None:
    lines = [
        f"HTTP/1.1 {status} {_REASONS[status]}",
        f"Content-Type: {kind}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
        "Referrer-Policy: no-referrer",
        "X-Content-Type-Options: nosniff",
        "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'",
    ]
    if status == 405:
        lines.append("Allow: GET")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
    await writer.drain()


_STYLE = (
    "body{font:16px/1.6 system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;max-width:32em;"
    "margin:18vh auto;padding:0 1.5em;color:#1f1f1f;background:#fff}h1{font-size:1.35em;margin:0 0 .25em}"
    "p{margin:0 0 1.8em;color:#555}@media (prefers-color-scheme:dark){body{color:#eee;background:#1f1f1f}p{color:#bbb}}"
)


def _page(outcome: bool | None, app_name: str) -> bytes:
    """What the browser shows at the end: signed in, not signed in, or (the outcome took too long) see the app."""
    app = html.escape(app_name or "WittyCode")
    if outcome is True:
        zh_title, zh_text = "登录完成", f"可以关闭这个标签页，回到 {app}。"
        en_title, en_text = "Signed in", f"You can close this tab and return to {app}."
    elif outcome is False:
        zh_title, zh_text = "登录没有完成", f"可以关闭这个标签页，回到 {app} 查看原因。"
        en_title, en_text = "Sign-in did not complete", f"You can close this tab and return to {app} to see why."
    else:
        zh_title, zh_text = "正在完成登录", f"结果请看 {app}；这个标签页可以关闭。"
        en_title, en_text = "Finishing sign-in", f"See {app} for the result. You can close this tab."
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{zh_title} · {en_title}</title><style>{_STYLE}</style></head><body>"
        f"<h1>{zh_title}</h1><p>{zh_text}</p>"
        f'<h1 lang="en">{en_title}</h1><p lang="en">{en_text}</p>'
        "</body></html>\n"
    )
    return document.encode("utf-8")
