"""``core.chatgpt.oauth`` against a fake OpenAI sign-in server and a fake browser, both on the loopback: registration and
re-authorization, every way a callback or an ID token can be wrong, timeouts and cancelling, refresh and revocation."""

from __future__ import annotations

import asyncio
import logging
import socket
import string
import threading
import time
import urllib.parse
from dataclasses import replace

import pytest

from desktop.core.chatgpt.accounts import AccountRecord, AccountStore, TokenProvider
from desktop.core.chatgpt.errors import REFRESH_TERMINAL_CODES, AuthError, ChatGPTError, PlanNotEnabled, friendly_failure
from desktop.core.chatgpt.oauth import (
    PLAN_SCOPE,
    SCOPES,
    Endpoints,
    SignIn,
    authorize_url,
    pkce_challenge,
    pkce_verifier,
    refresh,
    revoke,
)
from tests.fake_openai_auth import (
    APP_NAME,
    HOST_ID,
    FakeBrowser,
    FakeOpenAIAuth,
    no_proxy_client,
    rsa_key,
    sign_hs256,
    sign_rs256,
    signed_in,
    swap_payload,
    unsigned_jwt,
)


@pytest.fixture()
def auth():
    with FakeOpenAIAuth() as fake:
        yield fake


def attempt(auth: FakeOpenAIAuth, open_browser, **options) -> SignIn:
    return SignIn(no_proxy_client(), auth.endpoints, host_id=HOST_ID, app_name=APP_NAME, open_browser=open_browser, **options)


def run(sign_in: SignIn, timeout: float = 20.0) -> AccountRecord:
    return asyncio.run(sign_in.run(timeout))


def refused(sign_in: SignIn, timeout: float = 20.0) -> AuthError:
    with pytest.raises(AuthError) as caught:
        run(sign_in, timeout)
    return caught.value


def port_of(sign_in: SignIn) -> int:
    port = urllib.parse.urlsplit(sign_in.redirect_uri or "").port
    assert port
    return port


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def synthetic(**changes) -> AccountRecord:
    """A record the fake server does not know (for answers that are queued anyway)."""
    base = AccountRecord(
        email="dev@example.com",
        issuer="https://auth.openai.com",
        subject="user-fake-1",
        client_id="oaiapp_Synthetic",
        ext_agent_host_id=HOST_ID,
        id_token="id.token.value",
        access_token="access-token-value",
        refresh_token="refresh-token-value",
        token_type="Bearer",
        expires_in=3600.0,
        scopes=SCOPES,
        saved_at=time.time(),
    )
    return replace(base, **changes)


# ---------------------------------------------------------------------------------------------- the parts


def test_the_default_endpoints_are_openais():
    assert Endpoints.default() == Endpoints(
        issuer="https://auth.openai.com",
        authorize="https://auth.openai.com/api/accounts/authorize",
        token="https://auth.openai.com/api/accounts/oauth/token",
        revoke="https://auth.openai.com/api/accounts/oauth/revoke",
        jwks="https://auth.openai.com/.well-known/jwks.json",
        resource="https://api.openai.com/v1",
    )
    assert Endpoints.for_issuer("http://127.0.0.1:5/").token == "http://127.0.0.1:5/api/accounts/oauth/token"


def test_the_fake_server_describes_itself_like_openai(auth):
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(auth.base_url + "/.well-known/openid-configuration", timeout=10) as answer:
        discovery = json.load(answer)
    endpoints = auth.endpoints
    assert (discovery["issuer"], discovery["authorization_endpoint"], discovery["token_endpoint"]) == (
        endpoints.issuer,
        endpoints.authorize,
        endpoints.token,
    )
    assert (discovery["revocation_endpoint"], discovery["jwks_uri"]) == (endpoints.revoke, endpoints.jwks)


def test_pkce_follows_rfc_7636():
    # the example of RFC 7636 appendix B
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier = pkce_verifier()
    assert 43 <= len(verifier) <= 128 and set(verifier) <= set(string.ascii_letters + string.digits + "-_")
    assert pkce_verifier() != verifier
    assert (len(pkce_verifier(43)), len(pkce_verifier(128))) == (43, 128)
    for wrong in (42, 129):
        with pytest.raises(ValueError):
            pkce_verifier(wrong)


def test_the_authorization_url_of_a_registration_and_of_a_re_authorization():
    endpoints = Endpoints.default()
    common = {
        "redirect_uri": "http://127.0.0.1:1455/auth/callback",
        "state": "s",
        "nonce": "n",
        "code_challenge": "c",
        "host_id": HOST_ID,
        "app_name": APP_NAME,
    }
    first = authorize_url(endpoints, **common)
    assert first.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(first).query)) == {
        "response_type": "code",
        "client_id": "dynamic_agent_client",
        "redirect_uri": "http://127.0.0.1:1455/auth/callback",
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": "https://api.openai.com/v1",
        "state": "s",
        "nonce": "n",
        "code_challenge": "c",
        "code_challenge_method": "S256",
        "ext_agent_host_id": HOST_ID,
        "agent_name_hint": APP_NAME,
    }
    # every value URL-encoded: spaces as %20, the callback URL's ':' and '/' too
    assert "scope=openid%20profile%20email%20" in first
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback" in first
    hints = {"id_token_hint": "h.i.nt", "login_hint": "user@example.com"}
    again = authorize_url(endpoints, **common, client_id="oaiapp_X", **hints, force_consent=True)
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(again).query))
    assert params["client_id"] == "oaiapp_X" and "agent_name_hint" not in params
    assert (params["id_token_hint"], params["login_hint"], params["prompt"]) == ("h.i.nt", "user@example.com", "consent")
    assert params["ext_agent_host_id"] == HOST_ID
    assert "login_hint=user%40example.com" in again
    # the hints belong to a re-authorization only
    assert "hint=" not in authorize_url(endpoints, **common, **hints).replace("agent_name_hint=", "")


def test_a_sign_in_checks_what_it_is_given(auth):
    with pytest.raises(ValueError):
        attempt(auth, print, client_id="dynamic_agent_client")
    with pytest.raises(ValueError):
        SignIn(no_proxy_client(), auth.endpoints, host_id="", app_name=APP_NAME, open_browser=print)
    with pytest.raises(ValueError):
        SignIn(no_proxy_client(), auth.endpoints, host_id=HOST_ID, app_name="", open_browser=print)


# ---------------------------------------------------------------------------------------------- signing in


def test_a_first_registration_gives_the_verified_account(auth, tmp_path):
    store = AccountStore(tmp_path)
    browser = FakeBrowser(auth)
    sign_in = attempt(auth, browser, store=store)
    record = run(sign_in)
    browser.wait()
    assert record.client_id == "oaiapp_FakeClient0001" == sign_in.issued_client_id
    assert (record.email, record.subject, record.issuer) == ("dev@example.com", "user-fake-1", auth.base_url)
    assert record.ext_agent_host_id == HOST_ID
    assert record.plan_enabled and record.signed_in and set(record.scopes) == set(SCOPES)
    assert (record.token_type, record.expires_in) == ("Bearer", 3600)
    assert abs(record.saved_at - time.time()) < 60
    assert {record.access_token, record.refresh_token, record.id_token} <= auth.issued
    # exchanged by the issued client, with the same callback URI, the resource and the PKCE verifier, and no secret
    [form] = auth.forms("token", "authorization_code")
    assert form["client_id"] == record.client_id
    assert form["redirect_uri"] == browser.params["redirect_uri"] == sign_in.redirect_uri
    assert form["resource"] == "https://api.openai.com/v1"
    assert pkce_challenge(form["code_verifier"]) == browser.params["code_challenge"]
    assert "client_secret" not in form
    assert store.get(record.client_id) == record and store.active_id() == record.client_id
    [(status, page)] = browser.pages
    assert status == 200 and "登录完成" in page and "Signed in" in page and "WittyCode" in page


def test_authorizing_again_uses_the_saved_client_and_the_hints(auth, tmp_path):
    store = AccountStore(tmp_path)
    first = signed_in(auth, store)
    hints = {"client_id": first.client_id, "id_token_hint": first.id_token, "login_hint": first.email}
    # (the fake browser checks the request: the saved client id, no agent_name_hint, the hints, the host id)
    again = signed_in(auth, store, **hints)
    assert (again.client_id, again.subject) == (first.client_id, first.subject)
    assert again.refresh_token != first.refresh_token
    assert store.get(first.client_id) == again and len(store.accounts()) == 1
    # a callback that names the same client is fine too (the default fake leaves it out, as OpenAI may)
    auth.echo_client_id = True
    assert signed_in(auth, store, **hints).client_id == first.client_id


def test_consent_can_be_asked_for_again(auth):
    assert signed_in(auth, force_consent=True).plan_enabled  # (the fake browser checks prompt=consent)


def test_a_chosen_port_is_used(auth):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sign_in = attempt(auth, FakeBrowser(auth), port=port)
    run(sign_in)
    assert sign_in.redirect_uri == f"http://127.0.0.1:{port}/auth/callback"


def test_a_port_in_use_fails_the_attempt_at_once(auth):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        opened: list[str] = []
        error = refused(attempt(auth, opened.append, port=taken.getsockname()[1]))
    assert error.code == "listen" and opened == []


def declined(callback: dict) -> dict:
    return {"error": "access_denied", "error_description": "The user declined.", "state": callback["state"]}


def without(name: str):
    return lambda callback: {key: value for key, value in callback.items() if key != name}


@pytest.mark.parametrize(
    "answer, code, text",
    [
        (lambda callback: {**callback, "state": "forged"}, "state_mismatch", "state"),
        (without("state"), "state_mismatch", "no state"),
        (declined, "access_denied", "declined: The user declined."),
        (lambda callback: {**declined(callback), "state": "forged"}, "state_mismatch", "state"),
        (lambda callback: {"error": "server_error", "error_description": "It broke", "state": callback["state"]}, "server_error", "It broke"),
        (without("client_id"), "registration_incomplete", "registration incomplete"),
        (lambda callback: {**callback, "client_id": "dynamic_agent_client"}, "registration_incomplete", "registration incomplete"),
        (without("code"), "invalid_callback", "no authorization code"),
        (lambda callback: [*callback.items(), ("state", callback["state"])], "invalid_callback", "repeats"),
    ],
    ids=["other state", "no state", "declined", "declined, other state", "error", "no client id", "registration id", "no code", "twice"],
)
def test_a_wrong_callback_ends_the_attempt_without_an_exchange(auth, tmp_path, answer, code, text):
    store = AccountStore(tmp_path)
    browser = FakeBrowser(auth, answer=answer)
    error = refused(attempt(auth, browser, store=store))
    browser.wait()
    assert error.code == code and text in error.message and not error.terminal
    assert auth.forms("token") == [] and store.accounts() == []
    [(status, page)] = browser.pages
    assert status == 200 and "登录没有完成" in page and "did not complete" in page


def test_a_re_authorization_refuses_a_callback_for_another_client(auth, tmp_path):
    store = AccountStore(tmp_path)
    first = signed_in(auth, store)
    browser = FakeBrowser(auth, client_id=first.client_id, answer=lambda callback: {**callback, "client_id": "oaiapp_Somebody"})
    error = refused(attempt(auth, browser, store=store, client_id=first.client_id))
    assert error.code == "client_mismatch"
    assert len(auth.forms("token")) == 1  # (only the first sign-in's exchange)
    assert store.get(first.client_id) == first


BAD_ID_TOKENS = [
    ("changed after signing", lambda auth: lambda h, c: swap_payload(auth.sign(h, c), {**c, "email": "x@evil.test"}), "does not verify"),
    ("signed by another key", lambda auth: lambda h, c: sign_rs256(c, rsa_key("other"), kid=auth.key.kid), "does not verify"),
    ("for another client", lambda auth: lambda h, c: auth.sign(h, {**c, "aud": "oaiapp_SomeoneElse"}), "not for this client"),
    ("expired", lambda auth: lambda h, c: auth.sign(h, {**c, "exp": int(time.time()) - 3600}), "expired"),
    ("another nonce", lambda auth: lambda h, c: auth.sign(h, {**c, "nonce": "replayed"}), "nonce"),
    ("another issuer", lambda auth: lambda h, c: auth.sign(h, {**c, "iss": "https://auth.openai.com"}), "issuer"),
    ("no subject", lambda auth: lambda h, c: auth.sign(h, {k: v for k, v in c.items() if k != "sub"}), "no subject"),
    ("alg none", lambda auth: lambda h, c: unsigned_jwt(c, kid=auth.key.kid), "'none'"),
    ("alg HS256", lambda auth: lambda h, c: sign_hs256(c, auth.key.public_jwk()["n"].encode(), kid=auth.key.kid), "'HS256'"),
]


@pytest.mark.parametrize("make, text", [(make, text) for _, make, text in BAD_ID_TOKENS], ids=[name for name, _, _ in BAD_ID_TOKENS])
def test_an_id_token_that_does_not_hold_fails_the_sign_in(auth, tmp_path, make, text):
    auth.mint_id_token = make(auth)
    store = AccountStore(tmp_path)
    browser = FakeBrowser(auth)
    error = refused(attempt(auth, browser, store=store))
    browser.wait()
    assert error.code == "invalid_id_token" and text in error.message
    assert store.accounts() == [] and store.active_id() is None
    assert "登录没有完成" in browser.pages[0][1]


def test_a_re_authorization_must_come_back_with_the_same_account(auth, tmp_path):
    store = AccountStore(tmp_path)
    first = signed_in(auth, store)
    auth.subject = "user-somebody-else"
    # (the account to expect comes from the store, or else from the ID token hint)
    for options in ({"store": store}, {"id_token_hint": first.id_token}):
        browser = FakeBrowser(auth, client_id=first.client_id, id_token_hint=options.get("id_token_hint"))
        assert refused(attempt(auth, browser, client_id=first.client_id, **options)).code == "account_mismatch"
    assert store.get(first.client_id) == first


def test_a_sign_in_without_the_plan_scope_is_kept_but_cannot_pay(auth, tmp_path):
    auth.granted = tuple(scope for scope in SCOPES if scope != PLAN_SCOPE)
    store = AccountStore(tmp_path)
    record = signed_in(auth, store)
    assert record.signed_in and not record.plan_enabled and PLAN_SCOPE not in record.scopes
    assert store.get(record.client_id) == record and store.active_id() == record.client_id
    provider = TokenProvider(store, no_proxy_client(), auth.endpoints)
    with pytest.raises(PlanNotEnabled) as caught:
        asyncio.run(provider.access_token())
    assert "套餐" in friendly_failure(caught.value)
    assert auth.forms("token", "refresh_token") == []


def ended(sign_in: SignIn, timeout: float = 20.0, *, during=None) -> tuple[AuthError, bool]:
    """The error that ends ``sign_in``, and whether its port was free right after it (checked while the event loop still
    runs: closing the loop would close a forgotten listener as well). ``during(sign_in)`` runs once it listens."""

    async def scenario() -> tuple[AuthError, bool]:
        task = asyncio.create_task(sign_in.run(timeout))
        if during is not None:
            while sign_in.url is None and not task.done():
                await asyncio.sleep(0.01)
            during(sign_in)
        with pytest.raises(AuthError) as caught:
            await asyncio.wait_for(task, timeout + 10)
        return caught.value, port_is_free(port_of(sign_in))

    return asyncio.run(scenario())


def test_a_browser_that_never_comes_back_times_out_and_frees_the_port(auth):
    opened: list[str] = []
    sign_in = attempt(auth, opened.append)
    started = time.monotonic()
    error, free = ended(sign_in, 0.5)
    assert error.code == "timeout" and free
    assert time.monotonic() - started < 5
    assert opened == [sign_in.url]


def test_cancel_ends_the_attempt_and_frees_the_port(auth):
    listening: list[bool] = []

    def cancel(sign_in: SignIn) -> None:
        listening.append(not port_is_free(port_of(sign_in)))
        sign_in.cancel()

    error, free = ended(attempt(auth, lambda url: None), 60, during=cancel)
    assert error.code == "cancelled" and listening == [True] and free


def test_cancel_works_from_another_thread_and_before_the_start(auth):
    sign_in = attempt(auth, lambda url: None)
    threading.Timer(0.3, sign_in.cancel).start()
    assert refused(sign_in, timeout=30).code == "cancelled"
    opened: list[str] = []
    early = attempt(auth, opened.append)
    early.cancel()
    assert refused(early).code == "cancelled"
    assert opened == []  # (the browser is not opened for an attempt that was cancelled already)


def test_other_requests_and_idle_connections_do_not_end_the_attempt(auth):
    browser = FakeBrowser(auth, favicon=True)
    sockets: list[socket.socket] = []

    def open_browser(url: str) -> None:
        port = port_of(sign_in)
        sockets.append(socket.create_connection(("127.0.0.1", port)))  # (a pre-connection that sends nothing)
        junk = socket.create_connection(("127.0.0.1", port))
        junk.sendall(b"\x16\x03\x01\x00\x05hello\r\n\r\n")  # (not HTTP)
        post = socket.create_connection(("127.0.0.1", port))
        post.sendall(b"POST /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n")
        sockets.extend((junk, post))
        browser(url)

    sign_in = attempt(auth, open_browser)
    started = time.monotonic()
    record = run(sign_in)
    browser.wait()
    try:
        assert record.signed_in and browser.favicon_status == 404
        assert time.monotonic() - started < 8  # (the idle connection did not hold up the end)
        sockets[2].settimeout(5)
        assert sockets[2].recv(100).startswith(b"HTTP/1.1 405")
    finally:
        for each in sockets:
            each.close()


def test_open_browser_may_be_a_coroutine_function(auth):
    browser = FakeBrowser(auth)

    async def open_browser(url: str) -> None:
        await asyncio.sleep(0)
        browser(url)

    assert run(attempt(auth, open_browser)).signed_in


def test_a_browser_that_cannot_be_opened_fails_the_attempt(auth):
    def open_browser(url: str) -> None:
        raise OSError("no browser here")

    error, free = ended(attempt(auth, open_browser))
    assert error.code == "browser" and "no browser here" in error.message and free


def test_a_failed_code_exchange_keeps_the_issued_client_id_for_the_retry(auth, tmp_path):
    store = AccountStore(tmp_path)
    auth.fail_next("token", 400, {"error": "invalid_grant", "error_description": "the code expired"})
    browser = FakeBrowser(auth)
    sign_in = attempt(auth, browser, store=store)
    error = refused(sign_in)
    browser.wait()
    assert (error.code, error.status, error.request_id) == ("invalid_grant", 400, "req_fake")
    assert "the code expired" in error.message
    assert sign_in.issued_client_id == auth.issued_client_id and store.accounts() == []
    assert "登录没有完成" in browser.pages[0][1]
    # the retry authorizes the issued client instead of registering another one
    assert signed_in(auth, store, client_id=sign_in.issued_client_id).client_id == auth.issued_client_id


def test_a_network_failure_during_the_exchange_is_a_network_error(auth):
    auth.drop_next("token")
    with pytest.raises(ChatGPTError) as caught:
        run(attempt(auth, FakeBrowser(auth)))
    assert caught.value.code == "network" and not isinstance(caught.value, AuthError)


def test_unreadable_signing_keys_or_token_answers_fail_the_sign_in(auth):
    auth.fail_next("jwks", 503, b"down")
    error = refused(attempt(auth, FakeBrowser(auth)))
    assert error.status == 503 and "signing keys" in error.message
    auth.fail_next("jwks", 200, {"no": "keys"})
    assert refused(attempt(auth, FakeBrowser(auth))).code == "invalid_jwks"
    auth.fail_next("token", 200, {"access_token": "a", "token_type": "Bearer"})
    error = refused(attempt(auth, FakeBrowser(auth)))
    assert error.code == "invalid_token_response" and "refresh_token, id_token" in error.message


def test_a_sign_in_runs_once(auth):
    sign_in = attempt(auth, FakeBrowser(auth))
    run(sign_in)
    with pytest.raises(RuntimeError):
        run(sign_in)


def test_signing_in_works_on_the_qt_event_loop(auth, loop):
    browser = FakeBrowser(auth)
    record = loop.run_until_complete(attempt(auth, browser).run(20))
    browser.wait()
    assert record.signed_in and browser.pages[0][0] == 200


# ---------------------------------------------------------------------------------------------- refresh


def test_a_refresh_rotates_the_tokens_and_keeps_the_identity(auth):
    record = signed_in(auth)
    renewed = asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert renewed.access_token != record.access_token and renewed.refresh_token != record.refresh_token
    assert renewed.id_token != record.id_token and renewed.scopes == record.scopes
    kept = ("client_id", "subject", "email", "issuer", "ext_agent_host_id")
    assert [getattr(renewed, name) for name in kept] == [getattr(record, name) for name in kept]
    assert renewed.saved_at >= record.saved_at and renewed.expires_in == 3600
    [form] = auth.forms("token", "refresh_token")
    assert form == {
        "grant_type": "refresh_token",
        "client_id": record.client_id,
        "refresh_token": record.refresh_token,
        "resource": "https://api.openai.com/v1",
    }
    # the old refresh token is spent: presenting it again is a reuse, and terminal
    with pytest.raises(AuthError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert caught.value.terminal and caught.value.code == "refresh_token_reused"


def test_a_refresh_takes_the_scopes_that_come_back(auth):
    record = signed_in(auth)
    with auth.lock:
        auth.live[record.refresh_token].scope = "openid email offline_access"
    renewed = asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert renewed.scopes == ("openid", "email", "offline_access") and not renewed.plan_enabled


def test_a_new_id_token_for_another_account_is_not_taken(auth):
    record = signed_in(auth)
    auth.mint_id_token = lambda header, claims: auth.sign(header, {**claims, "sub": "user-somebody-else"})
    renewed = asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert renewed.id_token == record.id_token and renewed.access_token != record.access_token


@pytest.mark.parametrize("code", sorted(REFRESH_TERMINAL_CODES))
def test_unusable_refresh_tokens_are_terminal(auth, code):
    auth.fail_next("token", 400, {"error": code, "error_description": "no"})
    with pytest.raises(AuthError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, synthetic()))
    assert caught.value.terminal and (caught.value.code, caught.value.status) == (code, 400)
    assert "重新登录" in friendly_failure(caught.value)


def test_invalid_client_is_a_configuration_error_not_a_dead_sign_in(auth):
    record = signed_in(auth)
    with pytest.raises(AuthError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, replace(record, client_id="oaiapp_Unknown")))
    assert not caught.value.terminal and caught.value.code == "invalid_client"
    assert "configuration" in caught.value.message and "oaiapp_Unknown" in caught.value.message


def test_a_refresh_that_cannot_reach_openai_is_not_an_auth_error(auth):
    record = signed_in(auth)
    auth.drop_next("token")
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert caught.value.code == "network" and not isinstance(caught.value, AuthError)
    auth.fail_next("token", 503, b"busy")
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, record))
    assert caught.value.status == 503 and not isinstance(caught.value, AuthError)
    # nothing was used up: the same refresh token still works
    assert asyncio.run(refresh(no_proxy_client(), auth.endpoints, record)).signed_in


def test_other_refusals_of_a_refresh_are_not_terminal(auth):
    auth.fail_next("token", 400, {"error": "invalid_request", "error_description": "odd"})
    with pytest.raises(AuthError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, synthetic()))
    assert not caught.value.terminal and caught.value.code == "invalid_request"


def test_a_signed_out_record_is_not_refreshed(auth):
    with pytest.raises(AuthError) as caught:
        asyncio.run(refresh(no_proxy_client(), auth.endpoints, synthetic(refresh_token="")))
    assert caught.value.terminal and auth.requests == []


# ---------------------------------------------------------------------------------------------- revocation


def test_revoking_is_confirmed_by_a_200(auth):
    record = signed_in(auth)
    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, record)) is True
    [form] = auth.forms("revoke")
    assert form == {"token": record.refresh_token, "token_type_hint": "refresh_token", "client_id": record.client_id}
    assert record.refresh_token in auth.revoked and record.refresh_token not in auth.live


def test_revoking_retries_network_failures_and_server_errors(auth):
    auth.drop_next("revoke")
    auth.fail_next("revoke", 503, b"")
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, synthetic(), sleep=sleep)) is True
    assert pauses == [0.5, 1.0] and len(auth.forms("revoke")) == 3


def test_revoking_gives_up_without_confirmation(auth):
    async def sleep(seconds: float) -> None:
        pass

    auth.fail_next("revoke", 502, b"", times=3)
    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, synthetic(), sleep=sleep)) is False
    assert len(auth.forms("revoke")) == 3
    auth.fail_next("revoke", 400, {"error": "unsupported_token_type"})
    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, synthetic(), sleep=sleep)) is False
    assert len(auth.forms("revoke")) == 4  # (a refusal is not retried)
    auth.fail_next("revoke", 204, b"")
    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, synthetic(), sleep=sleep)) is False  # (only a 200 confirms)
    assert asyncio.run(revoke(no_proxy_client(), auth.endpoints, synthetic(refresh_token=""), sleep=sleep)) is False
    assert len(auth.forms("revoke")) == 5


# ---------------------------------------------------------------------------------------------- the log


def test_no_token_code_or_verifier_reaches_the_log(auth, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    store = AccountStore(tmp_path)
    browser = FakeBrowser(auth)
    first = run(attempt(auth, browser, store=store))
    hints = {"client_id": first.client_id, "id_token_hint": first.id_token, "login_hint": first.email}
    again = FakeBrowser(auth, **hints)
    run(attempt(auth, again, store=store, **hints))
    provider = TokenProvider(store, no_proxy_client(), auth.endpoints, clock=lambda: time.time() + 3500)
    asyncio.run(provider.access_token())
    auth.fail_next("token", 400, {"error": "invalid_grant", "error_description": "gone"})
    late = TokenProvider(store, no_proxy_client(), auth.endpoints, clock=lambda: time.time() + 7200)
    with pytest.raises(AuthError):
        asyncio.run(late.access_token())
    browser.wait()
    again.wait()
    logged = "\n".join(entry.getMessage() for entry in caplog.records)
    assert first.client_id in logged  # (what is logged instead)
    secrets = auth.issued | {browser.params["state"], browser.params["nonce"], again.params["state"], again.urls[0]}
    assert [secret for secret in secrets if secret in logged] == []
