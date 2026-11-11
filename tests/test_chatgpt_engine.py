"""``ChatGPTEngine`` by itself: signing in and out against the loopback fake of OpenAI's sign-in server, the model list and
how long it is kept, the stream it hands the agent loop."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from desktop.core.chatgpt.errors import AuthError, ChatGPTError
from desktop.core.chatgpt.models import FALLBACK_MODELS
from desktop.core.chatgpt.responses import Finished, TextDone, build_request, user_message
from tests.chatgpt_support import ACCESS_TOKEN, CLIENT_ID, make_engine, sign_in
from tests.fake_openai_auth import FakeBrowser, FakeOpenAIAuth
from tests.fake_responses import FakeResponses, say


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.fixture()
def auth():
    with FakeOpenAIAuth() as fake:
        yield fake


async def until(predicate: Any, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------------------------------------ signing in and out


def test_signing_in_saves_the_account_and_makes_it_the_one_in_use(tmp_path, auth):
    async def go() -> None:
        engine = make_engine(tmp_path, None, signed_in=False, endpoints=auth.endpoints)
        engine._open_browser = FakeBrowser(auth, host_id=engine.accounts.host_id())
        assert engine.cached_account()["auth"] == "signed-out"
        view = await engine.login()
        assert (view["auth"], view["email"], view["planEnabled"]) == ("signed-in", "dev@example.com", True)
        assert engine.accounts.active_id() == auth.issued_client_id, "the account that signed in is the one conversations use"
        assert view["accounts"][0]["active"] is True and "signingIn" not in view

    run(go())


def test_an_account_added_while_another_is_in_use_becomes_the_one_in_use(tmp_path, auth):
    async def go() -> None:
        engine = make_engine(tmp_path, None, endpoints=auth.endpoints)  # (signed in as "me@example.com" already)
        assert engine.accounts.active_id() == CLIENT_ID
        engine._open_browser = FakeBrowser(auth, host_id=engine.accounts.host_id())
        view = await engine.login(add=True)
        assert engine.accounts.active_id() == auth.issued_client_id and view["email"] == "dev@example.com"
        assert sorted(a["clientId"] for a in view["accounts"]) == sorted([CLIENT_ID, auth.issued_client_id])

    run(go())


def test_signing_in_again_asks_for_the_registration_it_already_has(tmp_path, auth):
    async def go() -> None:
        engine = make_engine(tmp_path, None, signed_in=False, endpoints=auth.endpoints)
        host = engine.accounts.host_id()
        engine._open_browser = FakeBrowser(auth, host_id=host)
        await engine.login()
        record = engine.accounts.get(auth.issued_client_id)
        # (the fake browser checks the request: the issued client, the hints, no second name for the app)
        engine._open_browser = FakeBrowser(auth, host_id=host, client_id=record.client_id, id_token_hint=record.id_token, login_hint=record.email)
        view = await engine.login()
        assert view["auth"] == "signed-in" and len(view["accounts"]) == 1

    run(go())


def test_a_second_sign_in_while_one_is_open_is_refused_and_the_open_one_is_shown_and_can_be_cancelled(tmp_path, auth):
    async def go() -> None:
        engine = make_engine(tmp_path, None, signed_in=False, endpoints=auth.endpoints)
        seen: list[dict[str, Any]] = []
        engine.add_account_listener(seen.append)
        engine._open_browser = lambda url: None  # (the user is still at the browser)
        first = asyncio.ensure_future(engine.login())
        await until(lambda: engine._signin is not None)
        assert engine.cached_account()["signingIn"] is True
        assert any(view.get("signingIn") for view in seen), "the page is told that a sign-in is open"
        with pytest.raises(ChatGPTError) as caught:
            await engine.login()
        assert caught.value.code == "busy"
        await engine.cancel_login()
        with pytest.raises(AuthError):
            await first
        assert "signingIn" not in engine.cached_account() and engine._signin is None

    run(go())


def test_only_a_saved_account_can_be_made_the_one_in_use(tmp_path):
    async def go() -> None:
        engine = make_engine(tmp_path, None)
        with pytest.raises(ChatGPTError) as caught:
            await engine.use_account("oaiapp_unknown")
        assert caught.value.code == "unknown_account"
        assert engine.accounts.active_id() == CLIENT_ID
        sign_in(tmp_path / "chatgpt", client_id="oaiapp_test0002", email="second@example.com")
        assert engine.accounts.active_id() == "oaiapp_test0002"
        view = await engine.use_account(CLIENT_ID)
        assert engine.accounts.active_id() == CLIENT_ID and view["email"] == "me@example.com"

    run(go())


# ------------------------------------------------------------------------------------------------ the model list


def test_the_model_list_is_kept_for_a_while_and_read_again_after_it(tmp_path):
    async def go() -> None:
        now = [time.time()]
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake, clock=lambda: now[0])
            await engine.list_models()
            now[0] += 60
            await engine.list_models()
            assert fake.model_requests == 1
            now[0] += 600
            await engine.list_models()
            assert fake.model_requests == 2, "after ten minutes it is asked for again"

    run(go())


def test_the_model_list_of_another_account_is_not_served_to_this_one(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            engine = make_engine(tmp_path, fake)
            await engine.list_models()
            sign_in(tmp_path / "chatgpt", client_id="oaiapp_test0002", email="second@example.com")  # (another process did this)
            await engine.list_models()
            assert fake.model_requests == 2

    run(go())


def test_a_list_that_cannot_be_trusted_or_is_empty_shows_the_fallback(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as refused:
            refused.models_status = 503  # (with a body that would read as a list)
            engine = make_engine(tmp_path / "a", refused)
            assert [m.id for m in await engine.list_models()] == [m.id for m in FALLBACK_MODELS]
        with FakeResponses(token=ACCESS_TOKEN, models=[]) as empty:
            engine = make_engine(tmp_path / "b", empty)
            assert [m.id for m in await engine.list_models()] == [m.id for m in FALLBACK_MODELS]

    run(go())


# ------------------------------------------------------------------------------------------------ the stream


class Spy:
    """Wraps the HTTP client's line stream to see that it is closed."""

    def __init__(self, engine: Any) -> None:
        self.closed = 0
        real = engine.http.stream_lines
        spy = self

        def stream_lines(*args: Any, **kwargs: Any):
            inner = real(*args, **kwargs)

            class Lines:
                def __aiter__(self):
                    return inner.__aiter__()

                async def aclose(self):
                    spy.closed += 1
                    await inner.aclose()

            return Lines()

        engine.http.stream_lines = stream_lines


def test_the_connection_is_closed_when_the_answer_is_read_to_the_end_and_when_it_is_abandoned(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("whole answer"), say("abandoned answer"))
            engine = make_engine(tmp_path, fake)
            spy = Spy(engine)
            request = build_request(model="gpt-6.1-sol", instructions="", items=[user_message("hi")], tools=[], reasoning=None)
            events = [event async for event in engine.stream(request)]
            assert isinstance(events[-1], Finished) and any(isinstance(e, TextDone) for e in events)
            assert spy.closed == 1
            stream = engine.stream(request)
            await stream.__anext__()
            await stream.aclose()
            assert spy.closed == 2

    run(go())


def test_a_request_carries_the_token_and_names_the_application(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("ok"))
            engine = make_engine(tmp_path, fake)
            request = build_request(model="gpt-6.1-sol", instructions="", items=[user_message("hi")], tools=[], reasoning=None)
            [event async for event in engine.stream(request)]
            headers = fake.headers[0]
            assert headers["authorization"] == f"Bearer {ACCESS_TOKEN}" and headers["originator"] == "wittycode"
            assert headers["accept"] == "text/event-stream"

    run(go())
