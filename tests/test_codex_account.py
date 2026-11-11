"""The Codex account for the page: pure helpers, then the engine against the fake app-server (sign-in included)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from desktop.core.codex.account import (
    account_json,
    limit_windows,
    merge_rate_limits,
    plan_label,
    resets_in_text,
    window_label,
)
from desktop.core.codex.engine import CodexEngine
from desktop.core.settings import AppSettings
from tests.codex_support import FAKE, make_engine, run, until

NOW = 1_793_290_000.0
BINARY = SimpleNamespace(path=Path(r"C:\Users\me\AppData\Roaming\npm\codex.cmd"), version="0.165.0")


def limits(primary: dict | None, secondary: dict | None = None) -> dict:
    snapshot = {"limitId": "codex", "primary": primary, "secondary": secondary, "planType": "pro"}
    return {"rateLimits": snapshot, "rateLimitsByLimitId": {"codex": snapshot}}


def test_the_shapes_for_each_account_state():
    assert account_json(None) == {"cli": "missing", "auth": "signed-out", "limits": []}
    signed_out = account_json(BINARY, {"account": None, "requiresOpenaiAuth": True}, now=NOW)
    assert signed_out == {"cli": "found", "cliPath": str(BINARY.path), "version": "0.165.0", "auth": "signed-out", "limits": []}
    assert account_json(BINARY, {"account": None}, signing_in=True, now=NOW)["auth"] == "signing-in"
    api_key = account_json(BINARY, {"account": {"type": "apiKey"}}, limits({"usedPercent": 5}), now=NOW)
    assert api_key["auth"] == "api-key" and api_key["limits"] == [] and "email" not in api_key
    assert account_json(BINARY, {"account": {"type": "amazonBedrock"}}, now=NOW)["auth"] == "api-key"
    chatgpt = account_json(
        BINARY,
        {"account": {"type": "chatgpt", "email": "me@example.com", "planType": "pro"}},
        limits(
            {"usedPercent": 41.6, "windowDurationMins": 10080, "resetsAt": NOW + 4 * 86400 + 6 * 3600 + 59},
            {"usedPercent": 17, "windowDurationMins": 300, "resetsAt": NOW + 3 * 3600 + 12 * 60 + 5},
        ),
        now=NOW,
    )
    assert chatgpt["auth"] == "chatgpt" and chatgpt["email"] == "me@example.com" and chatgpt["plan"] == "ChatGPT Pro"
    assert chatgpt["limits"] == [
        {"label": "5 小时", "short": "5h", "usedPercent": 17, "resetsIn": "3 小时 12 分后重置"},
        {"label": "每周", "short": "1w", "usedPercent": 42, "resetsIn": "4 天 6 小时后重置"},
    ]
    assert "enabled" not in chatgpt
    assert account_json(BINARY, None, error="Codex 进程异常退出。")["error"] == "Codex 进程异常退出。"


def test_window_labels_reset_texts_and_plans():
    assert window_label(300) == ("5 小时", "5h") and window_label(10080) == ("每周", "1w")
    assert window_label(15) == ("15 分钟", "15m") and window_label(120) == ("2 小时", "2h")
    assert window_label(4320) == ("3 天", "3d") and window_label(20160) == ("2 周", "2w")
    assert window_label(None) == ("额度", "limit")
    assert resets_in_text(NOW + 30, NOW) == "即将重置" and resets_in_text(NOW - 5, NOW) == "即将重置"
    assert resets_in_text(NOW + 12 * 60 + 1, NOW) == "12 分钟后重置"
    assert resets_in_text(NOW + 3 * 3600, NOW) == "3 小时后重置"
    assert resets_in_text(NOW + 2 * 86400 + 30, NOW) == "2 天后重置"
    assert resets_in_text(None, NOW) == ""
    assert plan_label("plus") == "ChatGPT Plus" and plan_label("unknown") is None and plan_label(None) is None
    assert plan_label("self_serve_business_usage_based") == "ChatGPT Business" and plan_label("edu_pro") == "ChatGPT Edu"
    assert plan_label("ent26") == "ChatGPT Enterprise" and plan_label("sci_plus") == "ChatGPT Sci Plus"


def test_a_sparse_rate_limit_update_only_replaces_what_it_carries():
    read = limits({"usedPercent": 17, "windowDurationMins": 300, "resetsAt": NOW + 600}, {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": NOW + 9000})
    merged = merge_rate_limits(read, {"limitId": "codex", "primary": {"usedPercent": 31, "windowDurationMins": 300, "resetsAt": NOW + 900}, "secondary": None})
    windows = limit_windows(merged, now=NOW)
    assert [(w["short"], w["usedPercent"]) for w in windows] == [("5h", 31), ("1w", 41)]
    other = merge_rate_limits(merged, {"limitId": "codex_other", "primary": {"usedPercent": 99, "windowDurationMins": 60}})
    assert [(w["short"], w["usedPercent"]) for w in limit_windows(other, now=NOW)] == [("5h", 31), ("1w", 41)]
    assert limit_windows(merge_rate_limits(None, {"primary": {"usedPercent": 3, "windowDurationMins": 300}}), now=NOW)[0]["usedPercent"] == 3


def test_engine_account_states(tmp_path):
    async def scenario(account: str) -> dict:
        harness = make_engine(tmp_path / account, account=account)
        try:
            return await harness.engine.account()
        finally:
            await harness.engine.close_all()

    signed_out = run(scenario("signed-out"))
    assert signed_out == {"cli": "found", "cliPath": str(FAKE), "version": "0.165.0", "auth": "signed-out", "limits": []}
    chatgpt = run(scenario("chatgpt"))
    assert (chatgpt["auth"], chatgpt["email"], chatgpt["plan"]) == ("chatgpt", "me@example.com", "ChatGPT Pro")
    assert chatgpt["limits"] == [
        {"label": "5 小时", "short": "5h", "usedPercent": 17, "resetsIn": "3 小时 12 分后重置"},
        {"label": "每周", "short": "1w", "usedPercent": 41, "resetsIn": "4 天 6 小时后重置"},
    ]
    api_key = run(scenario("apikey"))
    assert api_key["auth"] == "api-key" and api_key["limits"] == []


def test_engine_account_without_a_cli_or_with_a_broken_one(tmp_path):
    async def scenario():
        missing = CodexEngine(AppSettings(), locate=lambda explicit: None)
        assert await missing.account() == {"cli": "missing", "auth": "signed-out", "limits": []}
        assert await missing.list_sessions(tmp_path) == []
        broken = make_engine(tmp_path, FAKE_CODEX_BROKEN=1)
        account = await broken.engine.account()
        assert account["cli"] == "found" and account["auth"] == "signed-out" and "Codex" in account["error"]
        await broken.engine.close_all()

    run(scenario())


def test_the_codex_path_setting_is_passed_to_locate_and_looked_up_again_when_it_changes(tmp_path):
    asked: list = []
    settings = AppSettings(codex_path="")

    async def scenario():
        engine = CodexEngine(settings, locate=lambda explicit: asked.append(explicit))
        await engine.account()
        await engine.account()
        settings.codex_path = r"D:\tools\codex.exe"
        await engine.account()

    run(scenario())
    assert asked == [None, None, r"D:\tools\codex.exe"], "a missing CLI is looked for again; a found one is kept"


def test_sign_in_opens_the_browser_and_returns_the_signed_in_account(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out")
        account = await harness.engine.login()
        assert account["auth"] == "chatgpt" and account["email"] == "me@example.com"
        assert len(harness.opened) == 1 and harness.opened[0].startswith("https://auth.openai.com/oauth/authorize")
        assert [a["auth"] for a in harness.accounts][:1] == ["signing-in"] and harness.accounts[-1]["auth"] == "chatgpt"
        assert harness.requests("account/login/start") == [{"type": "chatgpt"}]
        assert harness.engine.login_url is None
        await harness.engine.close_all()

    run(scenario())


def test_cancelling_the_sign_in(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out", FAKE_CODEX_LOGIN_DELAY="never")
        login = asyncio.ensure_future(harness.engine.login())
        await until(lambda: bool(harness.opened))
        assert (await harness.engine.account())["auth"] == "signing-in"
        assert harness.engine.login_url == harness.opened[0]
        cancelled = await harness.engine.cancel_login()
        assert cancelled["auth"] == "signed-out"
        assert (await asyncio.wait_for(login, 5))["auth"] == "signed-out"
        login_id = harness.opened[0].rsplit("=", 1)[1]
        assert harness.requests("account/login/cancel") == [{"loginId": login_id}]
        await harness.engine.close_all()

    run(scenario())


def test_a_sign_in_that_never_completes_times_out(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out", FAKE_CODEX_LOGIN_DELAY="never", login_timeout=0.3)
        account = await harness.engine.login()
        assert account["auth"] == "signed-out"
        assert len(harness.requests("account/login/cancel")) == 1
        await harness.engine.close_all()

    run(scenario())


def test_sign_out(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="chatgpt")
        assert (await harness.engine.account())["auth"] == "chatgpt"
        account = await harness.engine.logout()
        assert account["auth"] == "signed-out" and account["limits"] == []
        assert harness.accounts[-1]["auth"] == "signed-out"
        await harness.engine.close_all()

    run(scenario())
