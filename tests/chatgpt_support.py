"""Test support for the ChatGPT Plan engine: a signed-in account on disk, an engine that talks to the loopback fake service."""

from __future__ import annotations

import asyncio
import time
import urllib.request
from pathlib import Path
from typing import Any

from desktop.core.chatgpt.accounts import AccountRecord, AccountStore
from desktop.core.chatgpt.engine import ChatGPTEngine
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.oauth import PLAN_SCOPE, Endpoints
from desktop.core.session_manager import SessionState
from desktop.core.settings import AppSettings
from tests.fake_responses import FakeResponses

ACCESS_TOKEN = "access-token-1"
CLIENT_ID = "oaiapp_test0001"


def record(*, plan: bool = True, signed_in: bool = True, email: str = "me@example.com", client_id: str = CLIENT_ID) -> AccountRecord:
    scopes = ("openid", "profile", "email", "offline_access", "resource.invoke") + ((PLAN_SCOPE,) if plan else ())
    return AccountRecord(
        email=email,
        issuer="https://auth.openai.com",
        subject="user-1",
        client_id=client_id,
        ext_agent_host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        id_token="id.token.value",
        access_token=ACCESS_TOKEN if signed_in else "",
        refresh_token="refresh-1" if signed_in else "",
        token_type="Bearer",
        expires_in=3600 if signed_in else 0,
        scopes=scopes,
        saved_at=time.time(),
    )


def sign_in(root: Path, **kwargs: Any) -> AccountStore:
    """A signed-in account in ``root`` (the engine's folder), its access token fresh for an hour."""
    store = AccountStore(root)
    saved = record(**kwargs)
    store.save(saved)
    store.set_active(saved.client_id)
    return store


def make_engine(root: Path, fake: FakeResponses | None, *, signed_in: bool = True, plan: bool = True, settings: AppSettings | None = None, **kwargs: Any) -> ChatGPTEngine:
    folder = root / "chatgpt"
    if signed_in:
        sign_in(folder, plan=plan)
    http = HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    base_url = fake.base_url if fake is not None else "http://127.0.0.1:9/v1"
    # The sign-in endpoints too stay on this machine (the fake answers 404 to them): a test never reaches auth.openai.com,
    # where signing out would send a made-up refresh token.
    kwargs.setdefault("endpoints", Endpoints.for_issuer(base_url.removesuffix("/v1")))
    engine = ChatGPTEngine(settings or AppSettings(), folder, http=http, base_url=base_url, **kwargs)
    return engine


async def until(predicate: Any, *, timeout: float = 15.0, message: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.02)


async def finish_turn(controller: Any, *, timeout: float = 30.0) -> None:
    """Wait until the running turn is over (the controller is READY again)."""
    await until(lambda: controller.state is SessionState.READY and controller._turn is None, timeout=timeout, message="the turn to end")
