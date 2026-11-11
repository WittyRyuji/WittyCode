"""``core.chatgpt.accounts``: the credential record, the store on disk (atomic writes, hostile names, unreadable files,
the host id, the active account), and the token provider against the fake OpenAI sign-in server (renewal before expiry,
one renewal for many callers and for two processes, dead refresh tokens, failures that keep the tokens, signing out)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from desktop.core.chatgpt import accounts as accounts_module
from desktop.core.chatgpt.accounts import AccountRecord, AccountStore, TokenProvider, sign_out
from desktop.core.chatgpt.errors import AuthError, ChatGPTError, PlanNotEnabled
from desktop.core.chatgpt.oauth import SCOPES
from tests.fake_openai_auth import HOST_ID, FakeOpenAIAuth, no_proxy_client, signed_in


def record(client_id: str = "oaiapp_Alpha1", **changes) -> AccountRecord:
    base = AccountRecord(
        email="dev@example.com",
        issuer="https://auth.openai.com",
        subject="user-1",
        client_id=client_id,
        ext_agent_host_id=HOST_ID,
        id_token="id-token-1",
        access_token="access-token-1",
        refresh_token="refresh-token-1",
        token_type="Bearer",
        expires_in=3600.0,
        scopes=SCOPES,
        saved_at=1_000_000.0,
    )
    return replace(base, **changes)


@pytest.fixture()
def auth():
    with FakeOpenAIAuth() as fake:
        yield fake


class Clock:
    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now


def provider(store: AccountStore, auth: FakeOpenAIAuth, clock=time.time, **options) -> TokenProvider:
    return TokenProvider(store, no_proxy_client(), auth.endpoints, clock=clock, **options)


# ---------------------------------------------------------------------------------------------- the record


def test_a_record_survives_json_and_is_read_tolerantly():
    original = record()
    data = original.to_json()
    assert AccountRecord.from_json(json.loads(json.dumps(data))) == original
    assert set(data) == {
        "email",
        "issuer",
        "subject",
        "client_id",
        "ext_agent_host_id",
        "id_token",
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "scopes",
        "saved_at",
    }
    assert data["scopes"] == list(SCOPES) and data["expires_in"] == 3600
    sparse = AccountRecord.from_json(
        {"client_id": "oaiapp_B", "scopes": "openid email", "saved_at": "2026-11-04T10:00:00Z", "expires_in": "3600", "email": 5, "extra": [1]}
    )
    assert (sparse.client_id, sparse.email, sparse.token_type, sparse.refresh_token) == ("oaiapp_B", "", "Bearer", "")
    assert sparse.scopes == ("openid", "email") and sparse.expires_in == 3600
    assert sparse.saved_at == datetime(2026, 11, 4, 10, tzinfo=timezone.utc).timestamp()
    empty = AccountRecord.from_json({"scopes": [1, "openid", "openid"], "expires_in": True, "saved_at": "never"})
    assert (empty.client_id, empty.scopes, empty.expires_in, empty.saved_at) == ("", ("openid",), 0.0, 0.0)


def test_what_a_record_says_about_itself():
    one = record()
    assert one.expires_at == 1_000_000.0 + 3600 and one.plan_enabled and one.signed_in
    assert not replace(one, scopes=("openid", "email", "offline_access")).plan_enabled
    assert not replace(one, refresh_token="").signed_in
    # two registrations of one email have different labels
    twin = record("oaiapp_Beta22")
    assert one.label != twin.label and one.label.startswith("dev@example.com") and twin.label.endswith("Beta22")
    shown = repr(one)
    assert "oaiapp_Alpha1" in shown and not any(token in shown for token in ("access-token-1", "refresh-token-1", "id-token-1"))


# ---------------------------------------------------------------------------------------------- the store


def test_the_store_saves_and_lists_the_newest_first(tmp_path):
    store = AccountStore(tmp_path)
    assert store.accounts() == [] and store.get("oaiapp_Alpha1") is None and store.active_id() is None
    old, new = record("oaiapp_Old", saved_at=100.0), record("oaiapp_New", saved_at=200.0)
    store.save(old)
    store.save(new)
    assert store.accounts() == [new, old] and store.get("oaiapp_Old") == old
    assert store.path_of("oaiapp_Old") == tmp_path / "accounts" / "oaiapp_Old.json"
    assert json.loads(store.path_of("oaiapp_Old").read_text("utf-8"))["refresh_token"] == "refresh-token-1"
    assert AccountStore(tmp_path).accounts() == [new, old]
    assert store.remove("oaiapp_Old") and not store.remove("oaiapp_Old")
    assert store.accounts() == [new]


def test_writes_are_atomic(tmp_path, monkeypatch):
    store = AccountStore(tmp_path)
    store.save(record())
    folder = tmp_path / "accounts"
    assert [path.name for path in folder.iterdir()] == ["oaiapp_Alpha1.json"]  # (no temporary file left behind)

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(accounts_module.os, "replace", refuse)
    with pytest.raises(OSError):
        store.save(record(access_token="newer"))
    monkeypatch.undo()
    with pytest.raises(TypeError):  # (a write that fails half way)
        accounts_module._write_json(store.path_of("oaiapp_Alpha1"), {"value": object()})
    assert store.get("oaiapp_Alpha1").access_token == "access-token-1"
    assert [path.name for path in folder.iterdir()] == ["oaiapp_Alpha1.json"]


def test_unusable_files_are_skipped_and_reported_once(tmp_path, caplog):
    store = AccountStore(tmp_path)
    good = record()
    store.save(good)
    folder = tmp_path / "accounts"
    (folder / "broken.json").write_text("{not json", "utf-8")
    (folder / "list.json").write_text("[1, 2]", "utf-8")
    (folder / "nameless.json").write_text(json.dumps({"email": "x@y.z"}), "utf-8")
    (folder / "binary.json").write_bytes(b"\xff\xfe\x00")
    (folder / "oaiapp_Mislabeled.json").write_text(json.dumps(record("oaiapp_Other", refresh_token="secret-rt").to_json()), "utf-8")
    (tmp_path / "state.json").write_text("garbage", "utf-8")
    caplog.set_level(logging.WARNING)
    assert store.accounts() == [good] and store.active_id() == good.client_id
    assert store.accounts() == [good]
    assert store.get("oaiapp_Mislabeled") is None  # (its file holds another client)
    warned = [entry.getMessage() for entry in caplog.records]
    assert len(warned) == 6, warned  # (each file once, though read more than once)
    assert not any("secret-rt" in message for message in warned)


def test_the_host_id_is_made_once_and_kept(tmp_path):
    store = AccountStore(tmp_path)
    assert not (tmp_path / "host.json").exists()
    host = store.host_id()
    assert host.startswith("urn:uuid:") and uuid.UUID(host.removeprefix("urn:uuid:")).version == 4
    assert json.loads((tmp_path / "host.json").read_text("utf-8")) == {"ext_agent_host_id": host}
    assert store.host_id() == host and AccountStore(tmp_path).host_id() == host
    store.save(record())
    store.clear_tokens("oaiapp_Alpha1")
    store.remove("oaiapp_Alpha1")
    assert AccountStore(tmp_path).host_id() == host


def test_stores_that_start_at_the_same_time_agree_on_the_host_id(tmp_path):
    barrier = threading.Barrier(8)
    found: list[str] = []

    def start() -> None:
        store = AccountStore(tmp_path)
        barrier.wait()
        found.append(store.host_id())

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(found) == 8 and len(set(found)) == 1
    assert AccountStore(tmp_path).host_id() == found[0]
    assert [path.name for path in tmp_path.iterdir()] == ["host.json"]


def test_an_unreadable_host_id_is_replaced_once(tmp_path):
    (tmp_path / "host.json").write_text("{}", "utf-8")
    host = AccountStore(tmp_path).host_id()
    assert host.startswith("urn:uuid:") and AccountStore(tmp_path).host_id() == host


@pytest.mark.parametrize(
    "client_id",
    ["..\\evil", "../evil", "..", ".", "CON", "nul", "com1", "a:b", "x/y", "C:\\temp\\x", "名字", "oaiapp_ok.json", " lead", "a" * 300, "x\x00y"],
)
def test_client_ids_from_the_server_cannot_escape_the_folder(tmp_path, client_id):
    store = AccountStore(tmp_path / "chatgpt")
    path = store.path_of(client_id)
    assert path.parent == store.folder and path.suffix == ".json"
    store.save(record(client_id))
    assert store.get(client_id) == record(client_id) and store.accounts() == [record(client_id)]
    files = [each for each in tmp_path.rglob("*") if each.is_file()]
    assert files and all(each.parent == store.folder for each in files), files
    assert store.remove(client_id) and store.accounts() == []


def test_plain_client_ids_name_their_files_and_an_empty_one_is_refused(tmp_path):
    store = AccountStore(tmp_path)
    assert store.path_of("oaiapp_AbC-1_2").name == "oaiapp_AbC-1_2.json"
    assert store.path_of("CON").name != "CON.json" and store.path_of("..").name != "...json"
    with pytest.raises(ValueError):
        store.save(record(""))


def test_the_active_account_falls_back_when_it_is_gone(tmp_path):
    store = AccountStore(tmp_path)
    store.save(record("oaiapp_Older", saved_at=100.0))
    store.save(record("oaiapp_Chosen", saved_at=200.0))
    store.save(record("oaiapp_Newest", saved_at=300.0, refresh_token="", access_token=""))
    assert store.active_id() == "oaiapp_Chosen"  # (nothing chosen yet: the newest that is signed in)
    store.set_active("oaiapp_Older")
    assert store.active_id() == "oaiapp_Older" == AccountStore(tmp_path).active_id()
    store.set_active("oaiapp_Newest")
    assert store.active_id() == "oaiapp_Newest"  # (a signed-out account may be the chosen one)
    store.remove("oaiapp_Newest")
    assert store.active_id() == "oaiapp_Chosen"
    store.clear_tokens("oaiapp_Older")
    store.clear_tokens("oaiapp_Chosen")
    assert store.active_id() == "oaiapp_Chosen"  # (none signed in: the newest)


def test_clearing_tokens_keeps_the_registration(tmp_path):
    store = AccountStore(tmp_path)
    store.save(record())
    cleared = store.clear_tokens("oaiapp_Alpha1")
    assert cleared == store.get("oaiapp_Alpha1") and not cleared.signed_in
    assert (cleared.access_token, cleared.refresh_token, cleared.expires_in) == ("", "", 0)
    assert (cleared.client_id, cleared.email, cleared.subject, cleared.ext_agent_host_id) == ("oaiapp_Alpha1", "dev@example.com", "user-1", HOST_ID)
    assert cleared.id_token == "id-token-1" and cleared.scopes == SCOPES
    assert store.clear_tokens("oaiapp_Alpha1", keep_id_token=False).id_token == ""
    assert store.clear_tokens("oaiapp_Nobody") is None


# ---------------------------------------------------------------------------------------------- tokens


def test_a_fresh_token_is_handed_out_as_it_is(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    assert asyncio.run(provider(store, auth).access_token()) == account.access_token
    assert asyncio.run(provider(store, auth).access_token(account.client_id)) == account.access_token
    assert auth.forms("token", "refresh_token") == []


def test_a_token_near_its_expiry_is_renewed_and_saved_before_it_is_handed_out(auth, tmp_path):
    events: list[tuple[str, str]] = []

    class Recording(AccountStore):
        def save(self, record: AccountRecord) -> None:
            super().save(record)
            events.append(("saved", record.access_token))

    store = Recording(tmp_path)
    account = signed_in(auth, store)
    clock = Clock(account.expires_at - 299)  # (less than the 300 s margin left)
    tokens = provider(store, auth, clock)

    async def ask() -> str:
        token = await tokens.access_token()
        events.append(("handed out", token))
        return token

    token = asyncio.run(ask())
    stored = store.get(account.client_id)
    assert token == stored.access_token != account.access_token
    assert events[-2:] == [("saved", token), ("handed out", token)]
    assert stored.refresh_token != account.refresh_token and stored.saved_at == clock.now
    assert len(auth.forms("token", "refresh_token")) == 1
    clock.now = stored.expires_at - 301  # (just more than the margin left: no renewal)
    assert asyncio.run(tokens.access_token()) == token
    assert len(auth.forms("token", "refresh_token")) == 1


def test_many_callers_at_once_cause_one_renewal(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    auth.refresh_delay = 0.3
    tokens = provider(store, auth, Clock(account.expires_at - 10))

    async def many() -> list[str]:
        return await asyncio.gather(*(tokens.access_token() for _ in range(8)))

    handed = asyncio.run(many())
    assert len(set(handed)) == 1 and handed[0] == store.get(account.client_id).access_token
    assert len(auth.forms("token", "refresh_token")) == 1


def test_two_processes_sharing_the_store_renew_once(auth, tmp_path):
    account = signed_in(auth, AccountStore(tmp_path))
    auth.refresh_delay = 0.3
    clock = Clock(account.expires_at - 10)
    # (two providers with their own stores and locks, as two WittyCode windows have: only the files are shared)
    first, second = provider(AccountStore(tmp_path), auth, clock), provider(AccountStore(tmp_path), auth, clock)

    async def both() -> list[str]:
        return await asyncio.gather(first.access_token(), second.access_token())

    one, two = asyncio.run(both())
    assert one == two and len(auth.forms("token", "refresh_token")) == 1
    assert AccountStore(tmp_path).get(account.client_id).signed_in


def test_a_caller_that_gives_up_does_not_lose_the_renewal(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    auth.refresh_delay = 0.4
    tokens = provider(store, auth, Clock(account.expires_at - 10))

    async def scenario() -> str:
        caller = asyncio.create_task(tokens.access_token())
        await asyncio.sleep(0.1)  # (the refresh request is on its way)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        return await tokens.access_token()  # (waits for the same renewal)

    token = asyncio.run(scenario())
    stored = store.get(account.client_id)
    assert stored.refresh_token != account.refresh_token and token == stored.access_token
    assert len(auth.forms("token", "refresh_token")) == 1


@pytest.mark.parametrize("how", ["reused", "unknown"])
def test_a_dead_refresh_token_clears_the_tokens(auth, tmp_path, how):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    if how == "reused":
        auth.fail_next("token", 400, {"error": "refresh_token_reused", "error_description": "used already"})
    else:
        auth.live.clear()  # (OpenAI no longer knows it: invalid_grant)
    tokens = provider(store, auth, Clock(account.expires_at))
    with pytest.raises(AuthError) as caught:
        asyncio.run(tokens.access_token())
    assert caught.value.terminal and caught.value.code == ("refresh_token_reused" if how == "reused" else "invalid_grant")
    cleared = store.get(account.client_id)
    assert not cleared.signed_in and cleared.access_token == "" and cleared.id_token == account.id_token
    assert (cleared.client_id, cleared.email, cleared.subject) == (account.client_id, account.email, account.subject)
    with pytest.raises(AuthError) as caught:  # (signed out from now on, without asking OpenAI)
        asyncio.run(tokens.access_token())
    assert caught.value.terminal and caught.value.code == "signed_out"
    assert len(auth.forms("token", "refresh_token")) == 1


def test_a_renewal_that_fails_for_now_keeps_the_tokens(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    tokens = provider(store, auth, Clock(account.expires_at - 60))
    auth.drop_next("token")
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(tokens.access_token())
    assert caught.value.code == "network" and not isinstance(caught.value, AuthError)
    assert store.get(account.client_id) == account
    for status, body in ((503, b"busy"), (401, {"error": "invalid_client"}), (400, {"error": "invalid_request"})):
        auth.fail_next("token", status, body)
        with pytest.raises(ChatGPTError) as caught:
            asyncio.run(tokens.access_token())
        assert not getattr(caught.value, "terminal", False)
        assert store.get(account.client_id) == account
    assert asyncio.run(tokens.access_token()) != account.access_token  # (the same refresh token, once OpenAI answers)


def test_without_a_usable_account_there_is_no_token(auth, tmp_path):
    store = AccountStore(tmp_path)
    tokens = provider(store, auth)
    for client_id in (None, "oaiapp_Nobody"):
        with pytest.raises(AuthError) as caught:
            asyncio.run(tokens.access_token(client_id))
        assert caught.value.terminal and caught.value.code == "signed_out"
    store.save(record(refresh_token="", access_token=""))
    with pytest.raises(AuthError) as caught:
        asyncio.run(tokens.access_token())
    assert caught.value.terminal and not isinstance(caught.value, PlanNotEnabled)
    store.save(record(scopes=("openid", "email", "offline_access")))
    with pytest.raises(PlanNotEnabled) as caught:
        asyncio.run(tokens.access_token())
    assert not caught.value.terminal and auth.requests == []


def test_a_sign_in_saved_while_a_renewal_runs_is_not_overwritten(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    clock = Clock(account.expires_at - 10)
    newer = replace(account, access_token="from-a-new-sign-in", refresh_token="rt-new-sign-in", saved_at=clock.now)
    auth.on_refresh = lambda: store.save(newer)
    assert asyncio.run(provider(store, auth, clock).access_token()) == "from-a-new-sign-in"
    assert store.get(account.client_id) == newer


def test_signing_out_revokes_and_clears_but_keeps_the_registration(auth, tmp_path):
    store = AccountStore(tmp_path)
    host = store.host_id()
    account = signed_in(auth, store)
    assert asyncio.run(sign_out(no_proxy_client(), auth.endpoints, store, account.client_id)) is True
    assert auth.revoked == [account.refresh_token]
    after = store.get(account.client_id)
    assert not after.signed_in and (after.access_token, after.id_token) == ("", "")
    kept = ("client_id", "email", "subject", "ext_agent_host_id")
    assert [getattr(after, name) for name in kept] == [getattr(account, name) for name in kept]
    assert store.host_id() == host == AccountStore(tmp_path).host_id()
    assert asyncio.run(sign_out(no_proxy_client(), auth.endpoints, store, account.client_id)) is False  # (nothing to revoke)
    assert asyncio.run(sign_out(no_proxy_client(), auth.endpoints, store, "oaiapp_Nobody")) is False


def test_signing_out_clears_the_tokens_even_when_revoking_fails(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    auth.drop_next("revoke", times=3)
    assert asyncio.run(sign_out(no_proxy_client(), auth.endpoints, store, account.client_id, sleep=sleep)) is False
    assert pauses == [0.5, 1.0] and not store.get(account.client_id).signed_in


def test_the_provider_signs_out_the_active_account(auth, tmp_path):
    store = AccountStore(tmp_path)
    account = signed_in(auth, store)
    tokens = provider(store, auth)
    assert asyncio.run(tokens.sign_out()) is True
    with pytest.raises(AuthError) as caught:
        asyncio.run(tokens.access_token())
    assert caught.value.terminal
    assert store.active_id() == account.client_id  # (the registration stays, for the next sign-in)
