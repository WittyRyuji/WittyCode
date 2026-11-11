"""A loopback stand-in for OpenAI's sign-in server and for the user's browser, for the tests of ``core.chatgpt``.

``FakeOpenAIAuth`` serves on 127.0.0.1 what WittyCode talks to at ``auth.openai.com``: the discovery document, the
JWKS, the token endpoint (authorization code, with PKCE checked against the challenge of the authorization request;
refresh token, with rotation and reuse detection) and the revocation endpoint. Answers can be replaced per endpoint
(``fail_next``) or the connection dropped (``drop_next``: a network failure). ID tokens and access tokens are signed
with RSA keys made here in pure Python (Miller-Rabin primes from a seeded generator: the same keys every run, each one
made once per test session).

``FakeBrowser`` is the browser and the user: it checks the authorization URL the way OpenAI's documentation describes
it, has the fake server approve the request (a test may change the answer: decline it, another state, no client id),
and follows the redirect to the app's loopback listener with a real HTTP request from its own thread, as a browser
does (the listener runs on the event loop that is waiting for the answer).

Nothing here contacts the internet: every URL is 127.0.0.1 and the HTTP clients ignore the proxy settings.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import random
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from desktop.core.chatgpt.accounts import AccountRecord
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.oauth import CALLBACK_HOST, CALLBACK_PATH, REGISTRATION_CLIENT_ID, RESOURCE, SCOPES, Endpoints, SignIn

APP_NAME = "WittyCode"
HOST_ID = "urn:uuid:5b0f8a3e-2c1d-4e6f-9a7b-0c1d2e3f4a5b"

# Written out again rather than imported: a mistake in the code under test must not be shared by its tests.
_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def no_proxy_client() -> HttpClient:
    """An ``HttpClient`` that never goes through a proxy (the machine's settings must not decide a loopback test)."""
    return HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))


# ---------------------------------------------------------------------------------------------- RSA and JWT


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _octets(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


def _small_primes(limit: int = 2000) -> list[int]:
    flags = bytearray([1]) * limit
    flags[0:2] = b"\x00\x00"
    for value in range(2, int(limit**0.5) + 1):
        if flags[value]:
            flags[value * value :: value] = bytearray(len(range(value * value, limit, value)))
    return [value for value in range(3, limit) if flags[value]]


_SMALL_PRIMES = _small_primes()


def _probably_prime(candidate: int, rng: random.Random, rounds: int = 40) -> bool:
    """Miller-Rabin with ``rounds`` random witnesses."""
    odd, twos = candidate - 1, 0
    while odd % 2 == 0:
        odd //= 2
        twos += 1
    for _ in range(rounds):
        value = pow(rng.randrange(2, candidate - 1), odd, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(twos - 1):
            value = pow(value, 2, candidate)
            if value == candidate - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random, exponent: int) -> int:
    while True:
        # (the top two bits set: the product of two such primes has exactly twice the bits)
        candidate = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if any(candidate % small == 0 for small in _SMALL_PRIMES) or (candidate - 1) % exponent == 0:
            continue
        if _probably_prime(candidate, rng):
            return candidate


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int
    d: int = field(repr=False)
    kid: str = "test"

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    def public_jwk(self, **extra: Any) -> dict[str, Any]:
        return {"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256", "n": b64url(_octets(self.n)), "e": b64url(_octets(self.e)), **extra}

    def sign(self, message: bytes) -> bytes:
        """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 section 8.2.1)."""
        size = (self.n.bit_length() + 7) // 8
        digest_info = _DIGEST_INFO + hashlib.sha256(message).digest()
        encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
        return pow(int.from_bytes(encoded, "big"), self.d, self.n).to_bytes(size, "big")


def generate_rsa_key(bits: int = 2048, *, kid: str = "test", seed: str = "") -> RsaKey:
    rng = random.Random(f"{seed}/{bits}")
    exponent = 65537
    while True:
        first = _prime(bits // 2, rng, exponent)
        second = _prime(bits - bits // 2, rng, exponent)
        if first != second:
            break
    return RsaKey(n=first * second, e=exponent, d=pow(exponent, -1, (first - 1) * (second - 1)), kid=kid)


@functools.lru_cache(maxsize=None)
def rsa_key(name: str = "main", bits: int = 2048) -> RsaKey:
    """The test key called ``name``: the same in every run, made once per session."""
    return generate_rsa_key(bits, kid=f"kid-{name}", seed=name)


def _json_part(value: Mapping[str, Any]) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def make_jwt(header: Mapping[str, Any], claims: Mapping[str, Any], signer: Callable[[bytes], bytes] | None) -> str:
    signing_input = f"{_json_part(header)}.{_json_part(claims)}"
    signature = signer(signing_input.encode("ascii")) if signer is not None else b""
    return f"{signing_input}.{b64url(signature)}"


def sign_rs256(claims: Mapping[str, Any], key: RsaKey, **header: Any) -> str:
    return make_jwt({"alg": "RS256", "typ": "JWT", "kid": key.kid, **header}, claims, key.sign)


def sign_hs256(claims: Mapping[str, Any], secret: bytes, **header: Any) -> str:
    return make_jwt({"alg": "HS256", "typ": "JWT", **header}, claims, lambda data: hmac.new(secret, data, hashlib.sha256).digest())


def unsigned_jwt(claims: Mapping[str, Any], **header: Any) -> str:
    return make_jwt({"alg": "none", "typ": "JWT", **header}, claims, None)


def swap_payload(token: str, claims: Mapping[str, Any]) -> str:
    """``token`` with other claims but its old signature (what a forger without the key can do)."""
    head, _, signature = token.split(".")
    return f"{head}.{_json_part(claims)}.{signature}"


# ---------------------------------------------------------------------------------------------- the server


@dataclass
class _Grant:
    client_id: str
    redirect_uri: str
    challenge: str
    nonce: str
    resource: str
    scope: str
    subject: str
    email: str


@dataclass
class _Session:
    client_id: str
    scope: str
    subject: str
    email: str
    family: str


class FakeOpenAIAuth:
    """The sign-in server. Settings a test may change at any time: ``issued_client_id`` (what a registration gets),
    ``subject`` / ``email`` (who the user signs in as), ``granted`` (the scopes the user grants), ``expires_in``,
    ``echo_client_id`` (re-authorization callbacks name the client), ``mint_id_token(header, claims) -> str`` (how ID
    tokens are made), ``refresh_delay`` (seconds a refresh takes) and ``on_refresh`` (called while a refresh runs)."""

    def __init__(
        self,
        *,
        issued_client_id: str = "oaiapp_FakeClient0001",
        subject: str = "user-fake-1",
        email: str = "dev@example.com",
        granted: tuple[str, ...] = SCOPES,
        key: RsaKey | None = None,
    ) -> None:
        self.key = key or rsa_key()
        self.jwks_keys: list[dict[str, Any]] = [rsa_key("other").public_jwk(), self.key.public_jwk()]
        self.issued_client_id = issued_client_id
        self.subject = subject
        self.email = email
        self.granted = tuple(granted)
        self.expires_in = 3600
        self.resource = RESOURCE
        self.echo_client_id = False
        self.mint_id_token: Callable[[dict[str, Any], dict[str, Any]], str] = self.sign
        self.refresh_delay = 0.0
        self.on_refresh: Callable[[], None] | None = None
        self.lock = threading.Lock()
        self.grants: dict[str, _Grant] = {}
        self.live: dict[str, _Session] = {}
        self.spent: dict[str, str] = {}
        """Rotated refresh tokens and their family (all tokens that descend from one sign-in)."""
        self.revoked: list[str] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        """(method, endpoint, form) of every request."""
        self.issued: set[str] = set()
        """Every code and token handed out, and every PKCE verifier received: what must never be logged."""
        self._queued: dict[str, deque[tuple[Any, ...]]] = defaultdict(deque)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ life

    def __enter__(self) -> FakeOpenAIAuth:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        server = ThreadingHTTPServer((CALLBACK_HOST, 0), _Handler)
        server.daemon_threads = True
        server.fake = self  # type: ignore[attr-defined]
        self._server = server
        # (a short poll: shutdown waits for the next one, and every test stops a server)
        self._thread = threading.Thread(target=server.serve_forever, args=(0.02,), name="fake-openai-auth", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "the fake server is not running"
        return f"http://{CALLBACK_HOST}:{self._server.server_address[1]}"

    @property
    def endpoints(self) -> Endpoints:
        return Endpoints.for_issuer(self.base_url, resource=self.resource)

    # ------------------------------------------------------------------ knobs and records

    def fail_next(self, endpoint: str, status: int, body: Any = b"", *, times: int = 1) -> None:
        """The next ``times`` requests to ``endpoint`` (``token``, ``revoke``, ``jwks``) get this answer instead."""
        with self.lock:
            self._queued[endpoint].extend([("answer", status, body)] * times)

    def drop_next(self, endpoint: str, *, times: int = 1) -> None:
        """The next ``times`` requests to ``endpoint`` get no answer: the connection is closed (a network failure)."""
        with self.lock:
            self._queued[endpoint].extend([("drop",)] * times)

    def _take_queued(self, endpoint: str) -> tuple[Any, ...] | None:
        with self.lock:
            queue = self._queued[endpoint]
            return queue.popleft() if queue else None

    def forms(self, endpoint: str, grant_type: str | None = None) -> list[dict[str, str]]:
        with self.lock:
            return [
                form
                for _, name, form in self.requests
                if name == endpoint and (grant_type is None or form.get("grant_type") == grant_type)
            ]

    def sign(self, header: dict[str, Any], claims: dict[str, Any]) -> str:
        return make_jwt(header, claims, self.key.sign)

    # ------------------------------------------------------------------ the browser's part

    def authorize(self, params: Mapping[str, str]) -> dict[str, str]:
        """What the server does with an authorization request the user approved: a code bound to the request, and the
        callback's query (scopes sorted, as OpenAI sends them)."""
        registering = params["client_id"] == REGISTRATION_CLIENT_ID
        client_id = self.issued_client_id if registering else params["client_id"]
        code = "code_" + secrets.token_urlsafe(18)
        scope = " ".join(sorted(self.granted))
        with self.lock:
            self.grants[code] = _Grant(
                client_id=client_id,
                redirect_uri=params["redirect_uri"],
                challenge=params["code_challenge"],
                nonce=params["nonce"],
                resource=params["resource"],
                scope=scope,
                subject=self.subject,
                email=self.email,
            )
            self.issued.add(code)
        callback = {"code": code, "scope": scope, "state": params["state"]}
        if registering or self.echo_client_id:
            callback["client_id"] = client_id
        return callback

    # ------------------------------------------------------------------ endpoints

    def discovery(self) -> dict[str, Any]:
        endpoints = self.endpoints
        return {
            "issuer": endpoints.issuer,
            "authorization_endpoint": endpoints.authorize,
            "token_endpoint": endpoints.token,
            "revocation_endpoint": endpoints.revoke,
            "jwks_uri": endpoints.jwks,
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        }

    def token(self, form: Mapping[str, str]) -> tuple[int, Any]:
        if "client_secret" in form:
            return 400, {"error": "invalid_request", "error_description": "a public client has no secret"}
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return self._exchange(form)
        if grant_type == "refresh_token":
            return self._refresh(form)
        return 400, {"error": "unsupported_grant_type", "error_description": str(grant_type)}

    def _exchange(self, form: Mapping[str, str]) -> tuple[int, Any]:
        verifier = form.get("code_verifier", "")
        with self.lock:
            grant = self.grants.pop(form.get("code", ""), None)  # (a code is good for one exchange)
            self.issued.add(verifier)
        if grant is None:
            return 400, {"error": "invalid_grant", "error_description": "unknown or already used code"}
        wrong = [
            name
            for name, holds in (
                ("client_id", form.get("client_id") == grant.client_id),
                ("redirect_uri", form.get("redirect_uri") == grant.redirect_uri),
                ("resource", form.get("resource") == grant.resource),
                ("code_verifier", 43 <= len(verifier) <= 128 and b64url(hashlib.sha256(verifier.encode()).digest()) == grant.challenge),
            )
            if not holds
        ]
        if wrong:
            return 400, {"error": "invalid_grant", "error_description": "not the authorization request's: " + ", ".join(wrong)}
        return 200, self._issue(grant.client_id, grant.scope, grant.subject, grant.email, nonce=grant.nonce)

    def _refresh(self, form: Mapping[str, str]) -> tuple[int, Any]:
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        if self.on_refresh is not None:
            self.on_refresh()
        token = form.get("refresh_token", "")
        with self.lock:
            if "scope" in form:
                return 400, {"error": "invalid_request", "error_description": "a refresh keeps the grant: no scope"}
            family = self.spent.get(token)
            if family is not None:
                # A rotated token came back: the whole family ends (what OAuth 2.1 advises for reuse).
                for live, session in list(self.live.items()):
                    if session.family == family:
                        del self.live[live]
                return 400, {"error": "refresh_token_reused", "error_description": "this refresh token was used already"}
            session = self.live.get(token)
            if session is None:
                return 400, {"error": "invalid_grant", "error_description": "unknown refresh token"}
            if form.get("client_id") != session.client_id:
                return 401, {"error": "invalid_client", "error_description": "unknown client"}
            if form.get("resource") != self.resource:
                return 400, {"error": "invalid_target", "error_description": "unknown resource"}
            del self.live[token]
            self.spent[token] = session.family
        return 200, self._issue(session.client_id, session.scope, session.subject, session.email, family=session.family)

    def _issue(self, client_id: str, scope: str, subject: str, email: str, *, nonce: str | None = None, family: str | None = None) -> dict[str, Any]:
        now = int(time.time())
        refresh_token = "rt_" + secrets.token_urlsafe(24)
        access_token = sign_rs256(
            {
                "sub": subject,
                "aud": self.resource,
                "client_id": client_id,
                "scope": scope,
                "https://api.openai.com/auth": {"per_user_salt": "salt", "encrypted_auth_metadata": "opaque"},
                "iss": self.base_url,
                "iat": now,
                "exp": now + self.expires_in,
                "jti": secrets.token_hex(8),
                "nbf": now,
            },
            self.key,
        )
        claims: dict[str, Any] = {
            "iss": self.base_url,
            "aud": client_id,
            "sub": subject,
            "email": email,
            "email_verified": True,
            "iat": now,
            "auth_time": now,
            "exp": now + 3600,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        id_token = self.mint_id_token({"alg": "RS256", "typ": "JWT", "kid": self.key.kid}, claims)
        with self.lock:
            self.live[refresh_token] = _Session(client_id, scope, subject, email, family or secrets.token_hex(8))
            self.issued.update({refresh_token, access_token, id_token})
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": scope,
            "earliest_refresh_at": now + 60,
        }

    def revoke(self, form: Mapping[str, str]) -> tuple[int, Any]:
        with self.lock:
            self.revoked.append(form.get("token", ""))
            self.live.pop(form.get("token", ""), None)
        return 200, b""  # (also for a token that was invalid already, as OpenAI does)


_ROUTES = {
    ("GET", "/.well-known/openid-configuration"): "discovery",
    ("GET", "/.well-known/jwks.json"): "jwks",
    ("POST", "/api/accounts/oauth/token"): "token",
    ("POST", "/api/accounts/oauth/revoke"): "revoke",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:
        self._route("GET", {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("ascii", "replace")
        if self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
            self._send(400, {"error": "invalid_request", "error_description": "not a form"})
            return
        self._route("POST", dict(urllib.parse.parse_qsl(raw, keep_blank_values=True)))

    def _route(self, method: str, form: dict[str, str]) -> None:
        fake: FakeOpenAIAuth = self.server.fake  # type: ignore[attr-defined]
        endpoint = _ROUTES.get((method, urllib.parse.urlsplit(self.path).path))
        if endpoint is None:
            self._send(404, {"error": "not_found"})
            return
        with fake.lock:
            fake.requests.append((method, endpoint, form))
        queued = fake._take_queued(endpoint)
        if queued is not None:
            if queued[0] == "drop":
                self.close_connection = True
                return
            self._send(queued[1], queued[2])
            return
        if endpoint == "discovery":
            self._send(200, fake.discovery())
        elif endpoint == "jwks":
            self._send(200, {"keys": fake.jwks_keys})
        elif endpoint == "token":
            self._send(*fake.token(form))
        else:
            self._send(*fake.revoke(form))

    def _send(self, status: int, body: Any) -> None:
        if isinstance(body, (dict, list)):
            data, kind = json.dumps(body).encode(), "application/json"
        elif isinstance(body, str):
            data, kind = body.encode(), "text/plain"
        else:
            data, kind = bytes(body), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("x-request-id", "req_fake")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------------------------- the browser

_AUTHORIZE_PARAMS = {
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "resource",
    "state",
    "nonce",
    "code_challenge",
    "code_challenge_method",
    "ext_agent_host_id",
    "agent_name_hint",
    "id_token_hint",
    "login_hint",
    "prompt",
}


class FakeBrowser:
    """``open_browser`` for ``SignIn``. ``client_id`` None expects a first registration; otherwise a re-authorization
    of that client with these hints. ``answer(callback) -> callback`` changes what the server sends back; ``favicon``
    asks the listener for ``/favicon.ico`` first, as browsers do."""

    def __init__(
        self,
        auth: FakeOpenAIAuth,
        *,
        host_id: str = HOST_ID,
        app_name: str = APP_NAME,
        client_id: str | None = None,
        id_token_hint: str | None = None,
        login_hint: str | None = None,
        consent: bool = False,
        answer: Callable[[dict[str, str]], dict[str, str]] | None = None,
        favicon: bool = False,
    ) -> None:
        self.auth = auth
        self.host_id = host_id
        self.app_name = app_name
        self.client_id = client_id
        self.id_token_hint = id_token_hint
        self.login_hint = login_hint
        self.consent = consent
        self.answer = answer
        self.favicon = favicon
        self.urls: list[str] = []
        self.params: dict[str, str] = {}
        self.pages: list[tuple[int, str]] = []
        """(status, body) of the listener's answer to the callback request."""
        self.favicon_status: int | None = None
        self._threads: list[threading.Thread] = []

    def __call__(self, url: str) -> None:
        self.urls.append(url)
        self.params = self.check(url)
        callback = self.auth.authorize(self.params)
        if self.answer is not None:
            callback = self.answer(callback)
        thread = threading.Thread(target=self._follow, args=(self.params["redirect_uri"], callback), name="fake-browser", daemon=True)
        self._threads.append(thread)
        thread.start()

    def check(self, url: str) -> dict[str, str]:
        """The authorization request's parameters, after checking them against the documentation."""
        authorize = self.auth.endpoints.authorize
        assert url.startswith(authorize + "?"), f"not the authorize endpoint: {url[: len(authorize) + 1]}"
        pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True)
        params = dict(pairs)
        assert len(params) == len(pairs), "a parameter appears twice"
        assert set(params) <= _AUTHORIZE_PARAMS, f"unexpected parameters: {sorted(set(params) - _AUTHORIZE_PARAMS)}"
        registering = self.client_id is None
        assert params.get("response_type") == "code"
        assert params.get("client_id") == (REGISTRATION_CLIENT_ID if registering else self.client_id)
        redirect = urllib.parse.urlsplit(params.get("redirect_uri", ""))
        assert (redirect.scheme, redirect.hostname, redirect.path) == ("http", "127.0.0.1", CALLBACK_PATH), params.get("redirect_uri")
        assert redirect.port and not redirect.query and not redirect.fragment
        assert params.get("scope", "").split() == list(SCOPES)
        assert params.get("resource") == self.auth.resource
        assert len(params.get("state", "")) >= 32 and len(params.get("nonce", "")) >= 32
        assert params["state"] != params["nonce"]
        assert params.get("code_challenge_method") == "S256"
        challenge = params.get("code_challenge", "")
        assert len(challenge) == 43 and set(challenge) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert params.get("ext_agent_host_id") == self.host_id  # (every sign-in, not only the first)
        if registering:
            assert params.get("agent_name_hint") == self.app_name
            assert "id_token_hint" not in params and "login_hint" not in params
        else:
            assert "agent_name_hint" not in params
            assert params.get("id_token_hint") == self.id_token_hint
            assert params.get("login_hint") == self.login_hint
        assert params.get("prompt") == ("consent" if self.consent else None)
        return params

    def _follow(self, redirect_uri: str, callback: Mapping[str, str]) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if self.favicon:
            origin = redirect_uri[: -len(CALLBACK_PATH)]
            self.favicon_status = _get(opener, origin + "/favicon.ico")[0]
        self.pages.append(_get(opener, redirect_uri + "?" + urllib.parse.urlencode(callback)))

    def wait(self, timeout: float = 15.0) -> None:
        for thread in self._threads:
            thread.join(timeout)


def _get(opener: urllib.request.OpenerDirector, url: str) -> tuple[int, str]:
    try:
        with opener.open(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read().decode("utf-8", "replace")
    except OSError as error:
        return 0, f"{type(error).__name__}: {error}"


def signed_in(auth: FakeOpenAIAuth, store: Any = None, **options: Any) -> AccountRecord:
    """A finished sign-in through ``SignIn`` and ``FakeBrowser``: a first registration, or (``client_id`` and the
    hints in ``options``) a re-authorization."""
    browser = FakeBrowser(
        auth,
        client_id=options.get("client_id"),
        id_token_hint=options.get("id_token_hint"),
        login_hint=options.get("login_hint"),
        consent=options.get("force_consent", False),
    )
    attempt = SignIn(no_proxy_client(), auth.endpoints, host_id=HOST_ID, app_name=APP_NAME, open_browser=browser, store=store, **options)
    record = asyncio.run(attempt.run(20.0))
    browser.wait()
    return record
