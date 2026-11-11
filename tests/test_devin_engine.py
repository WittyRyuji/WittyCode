"""The Devin engine against the fake CLI: finding it, the account and signing in, the model list, the saved sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from desktop.core.devin.account import DevinStatus, account_json, parse_status
from desktop.core.devin.engine import DevinEngine, cwd_variants, saved_session
from desktop.core.devin.errors import DevinNotFoundError, DevinProcessError, DevinRpcError, DevinSignedOutError, friendly_failure
from desktop.core.devin.locate import DevinBinary, devin_candidates, locate_devin, probe_version
from desktop.core.devin.models import FamilyInfo, build_models, cli_info, known_about, model_entries, thought_levels
from desktop.core.session_manager import SessionState
from desktop.core.settings import AppSettings
from tests.devin_support import FAKE, fake_binary, finish_turn, make_engine, run, until

SIGNED_IN = """Logged in (via Devin).

Credentials:
  File:              C:\\Users\\x\\AppData\\Roaming\\devin\\credentials.toml

User:
  Name:              Someone
  Email:             someone@example.com

Account:
  Tier:              Devin Pro
  Plan:              Pro
"""


# ---------------------------------------------------------------------------------------------- pure pieces


def test_the_status_text_is_read_for_the_account():
    assert parse_status(SIGNED_IN) == DevinStatus(True, "someone@example.com", "Devin Pro")
    assert parse_status("Logged in (via Devin).\n\nAccount:\n  Plan:  Pro\n") == DevinStatus(True, "", "Pro")
    assert parse_status("Not logged in") == DevinStatus()
    assert parse_status("") == DevinStatus()
    assert parse_status("Logged out. You were not logged in") == DevinStatus()


def test_the_account_json_has_a_state_for_every_case():
    binary = fake_binary()
    assert account_json(None) == {"cli": "missing", "auth": "signed-out"}
    assert account_json(binary)["auth"] == "checking"
    assert account_json(binary, DevinStatus())["auth"] == "signed-out"
    signed = account_json(binary, DevinStatus(True, "a@b.c", "Devin Pro"))
    assert (signed["auth"], signed["email"], signed["plan"], signed["version"], signed["cli"]) == ("signed-in", "a@b.c", "Devin Pro", "3000.11.238", "found")
    assert account_json(binary, DevinStatus(True), signing_in=True)["auth"] == "signing-in"
    assert account_json(binary, None, error="boom")["error"] == "boom"


def test_the_cli_is_looked_for_in_the_documented_order(tmp_path):
    local = tmp_path / "Local"
    exe = local / "Programs" / "devin" / "resources" / "app" / "extensions" / "windsurf" / "devin" / "bin" / "devin.exe"
    on_path = tmp_path / "bin" / "devin.exe"
    env = {"LOCALAPPDATA": str(local)}
    assert devin_candidates(None, env=env, which=lambda name: str(on_path), home=tmp_path) == [(on_path, "path"), (exe, "desktop")]
    assert devin_candidates(None, env=env, which=lambda name: None, home=tmp_path) == [(exe, "desktop")]
    # a path in the settings is the only one tried: a wrong one shows up as missing
    assert devin_candidates(' "D:\\tools\\devin.exe" ', env=env, which=lambda name: str(on_path), home=tmp_path) == [(Path("D:\\tools\\devin.exe"), "settings")]


def test_the_version_comes_from_the_first_line():
    class Done:
        returncode = 0
        stdout = "devin 3000.11.238 (9e593c73d4ca)\n"

    assert probe_version(["devin"], run=lambda *a, **k: Done()) == "3000.11.238"
    Done.returncode = 1
    assert probe_version(["devin"], run=lambda *a, **k: Done()) is None

    def broken(*a, **k):
        raise OSError("no such file")

    assert probe_version(["devin"], run=broken) is None


def test_locating_runs_the_candidate_and_keeps_the_one_that_answers(tmp_path):
    exe = tmp_path / "devin.exe"
    exe.write_text("x")

    class Done:
        returncode = 0
        stdout = "devin 9.9.9 (abc)"

    found = locate_devin(str(exe), run=lambda *a, **k: Done())
    assert found == DevinBinary(path=exe, version="9.9.9", source="settings", command=(str(exe),))
    assert found.argv("acp") == [str(exe), "acp"]
    assert locate_devin(str(tmp_path / "nope.exe"), run=lambda *a, **k: Done()) is None


def test_session_rows_are_read_from_the_list():
    saved = saved_session(
        {
            "sessionId": "cliff-parrotfish",
            "cwd": "D:\\p",
            "title": " Fix the build ",
            "updatedAt": "2026-10-31T21:25:08+00:00",
            "_meta": {"cognition.ai/createdAt": "2026-10-31T21:25:03.000Z"},
        }
    )
    assert (saved.id, saved.title, saved.cwd) == ("cliff-parrotfish", "Fix the build", "D:\\p")
    assert saved.modified_at.isoformat() == "2026-10-31T21:25:08+00:00" and saved.created_at.second == 3
    assert cwd_variants("D:\\p") == ["D:\\p", "d:\\p"] and cwd_variants("/home/p") == ["/home/p"]


def test_models_are_read_from_the_config_options():
    options = [
        {"id": "model", "currentValue": "a", "options": [
            {"value": "a", "name": "A", "_meta": {"cognition.ai/supportsImages": True}},
            {"value": "b-high", "name": "B", "_meta": {"cognition.ai/supportsImages": False}},
            {"value": "fusion-a-sidekick-b", "name": "Fusion"},
            {"value": "c", "name": "C"},
        ]},
        {"id": "thought_level", "currentValue": "high", "options": [{"value": "medium"}, {"value": "high"}]},
    ]
    assert model_entries(options) == [("a", "A", True), ("b-high", "B", False), ("c", "C", True)]
    assert thought_levels(options) == (("medium", "high"), "high")
    assert thought_levels([{"id": "model", "options": []}]) == ((), "")
    info = cli_info(
        {"families": [{"slug": "b", "variants": [
            {"model_uid": "b-high", "max_context_tokens": 100, "cost_tier": "Free"},
            {"model_uid": "b-max", "max_context_tokens": 300},
        ]}, "garbage"]}
    )
    assert info["b-high"] == FamilyInfo(100, True) and info["b"] == FamilyInfo(100, False)
    assert known_about("b-max", info).window == 300 and known_about("b-low", info).window == 100 and known_about("zzz", info) == FamilyInfo()
    models = build_models(model_entries(options), {"a": (("medium", "high"), "high")}, info)
    assert [(m.id, m.efforts, m.default_effort, m.supports_images, m.context_window, m.note) for m in models] == [
        ("a", ("medium", "high"), "high", True, None, ""),
        ("b-high", (), "", False, 100, "Free"),
        ("c", (), "", True, None, ""),
    ]


def test_failures_are_put_into_words_with_devins_name():
    assert "Devin" in friendly_failure(DevinNotFoundError()) and "登录" in friendly_failure(DevinSignedOutError())
    assert friendly_failure(DevinProcessError("x", exit_code=3)) == "Devin 进程异常退出（exit code 3）。"
    assert friendly_failure(DevinProcessError("x", closed=True)) == "与 Devin 的连接已关闭。"
    assert friendly_failure(DevinRpcError(-32000, "Insufficient credits")) == "Devin 报错：Insufficient credits"
    assert friendly_failure(ValueError("boom")) == "ValueError: boom"


# ---------------------------------------------------------------------------------------------- the account


def test_the_account_is_read_from_the_cli(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        account = await harness.engine.account()
        assert account == {"cli": "found", "cliPath": str(FAKE), "version": "3000.11.238", "auth": "signed-in", "email": "me@example.com", "plan": "Devin Pro"}
        assert harness.engine.cached_account() == account
        signed_out = make_engine(tmp_path / "other", account="signed-out")
        assert (await signed_out.engine.account())["auth"] == "signed-out"

    run(scenario())


def test_without_a_cli_the_account_is_missing_and_nothing_opens(tmp_path):
    async def scenario():
        engine = DevinEngine(AppSettings(), locate=lambda explicit: None)
        assert await engine.account() == {"cli": "missing", "auth": "signed-out"}
        controller = await engine.open_new(tmp_path)
        assert controller.state is SessionState.CLOSED and "找不到 Devin" in controller.last_error
        assert engine.controllers == [] and await engine.list_sessions(tmp_path) == []

    run(scenario())


def test_signing_in_runs_the_cli_and_reports_the_account(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out", FAKE_DEVIN_LOGIN_DELAY=0.1)
        assert (await harness.engine.account())["auth"] == "signed-out"
        account = await harness.engine.login()
        assert account["auth"] == "signed-in" and account["email"] == "me@example.com"
        assert [a["auth"] for a in harness.accounts] == ["signing-in", "signed-in"]
        account = await harness.engine.logout()
        assert account["auth"] == "signed-out" and harness.accounts[-1]["auth"] == "signed-out"

    run(scenario())


def test_a_sign_in_can_be_cancelled_and_times_out(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out", FAKE_DEVIN_LOGIN_DELAY="never")
        waiting = asyncio.ensure_future(harness.engine.login())
        await until(lambda: harness.engine.cached_account()["auth"] == "signing-in")
        account = await harness.engine.cancel_login()
        assert account["auth"] == "signed-out"
        assert (await asyncio.wait_for(waiting, 10))["auth"] == "signed-out"
        timed = make_engine(tmp_path / "t", account="signed-out", FAKE_DEVIN_LOGIN_DELAY="never", login_timeout=0.3)
        assert (await timed.engine.login())["auth"] == "signed-out" and timed.engine._login is None

    run(scenario())


def test_a_session_is_not_opened_without_an_account(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, account="signed-out")
        controller = await harness.engine.open_new(tmp_path)
        assert controller.state is SessionState.CLOSED and "登录" in controller.last_error
        assert harness.requests("session/new") == [] and harness.engine.controllers == []
        saved = await harness.engine.open_saved(tmp_path, "nothing-here")
        assert saved.state is SessionState.DEAD and "登录" in saved.last_error

    run(scenario())


# ---------------------------------------------------------------------------------------------- models and sessions


def test_the_models_are_read_with_the_levels_each_one_offers(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        models = await harness.engine.list_models()
        by_id = {m.id: m for m in models}
        assert list(by_id) == ["adaptive", "swe-2-high", "claude-opus-5-5-medium", "glm-5-2"]  # (the fusion pairing is left out)
        assert by_id["adaptive"].efforts == () and by_id["adaptive"].default_effort == ""
        swe = by_id["swe-2-high"]
        assert (swe.efforts, swe.default_effort, swe.context_window, swe.note, swe.group) == (("medium", "high", "max"), "high", 262000, "Free", "Cognition")
        opus = by_id["claude-opus-5-5-medium"]
        assert opus.efforts == ("low", "medium", "high", "xhigh", "max") and opus.default_effort == "medium" and opus.group == "Anthropic"
        assert opus.context_window == 1000000 and opus.note == ""
        assert by_id["glm-5-2"].supports_images is False and by_id["glm-5-2"].efforts == ("none", "high", "max")
        # the scratch session the list was read from is gone again, and was not shown as a conversation
        assert harness.state()["sessions"] == {}
        assert harness.requests("session/delete")
        assert await harness.engine.list_models() is not models and harness.engine.model_info("glm-5-2").display_name == "GLM-5.2 High"
        assert len(harness.requests("session/new")) == 1  # (cached)
        await harness.engine.list_models(refresh=True)
        assert len(harness.requests("session/new")) == 2

    run(scenario())


def test_the_models_are_listed_without_the_cli_table(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, FAKE_DEVIN_NO_MODELS_JSON=1)
        models = await harness.engine.list_models()
        assert len(models) == 4 and all(m.context_window is None and m.note == "" for m in models)
        # the window a session reports is learned
        harness.engine._learn_window("swe-2-high", 123456)
        assert harness.engine.model_window("swe-2-high") == 123456 and harness.engine.model_info("swe-2-high").context_window == 123456
        assert harness.engine.model_window("") is None and harness.engine.model_window("unknown") is None

    run(scenario())


def test_saved_sessions_are_listed_for_their_project_without_the_empty_ones(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        used = await harness.engine.open_new(project)
        await finish_turn(used, "first question")
        idle = await harness.engine.open_new(project)  # opened and left: nothing to list
        other = await harness.engine.open_new(tmp_path)
        await finish_turn(other, "elsewhere")
        listed = await harness.engine.list_sessions(project)
        assert [s.id for s in listed] == [used.session_id]
        assert listed[0].title == "first question" and listed[0].modified_at.year >= 2025
        assert idle.session_id not in [s.id for s in listed]
        assert await harness.engine.list_sessions(tmp_path / "nowhere") == []

    run(scenario())


def test_the_process_that_will_not_start_is_reported_to_the_session(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path, FAKE_DEVIN_BROKEN=1)
        controller = await harness.engine.open_new(tmp_path)
        assert controller.state is SessionState.CLOSED and "Devin" in controller.last_error
        assert harness.engine.controllers == []

    run(scenario())


def test_every_message_to_the_process_names_the_protocol_version(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await harness.engine.open_new(tmp_path)
        assert controller.state is SessionState.READY
        raw = (harness.home / "received.jsonl").read_text(encoding="utf-8").splitlines()
        assert all('"jsonrpc"' in line for line in raw) and raw
        (init,) = harness.requests("initialize")
        assert init["protocolVersion"] == 1 and init["clientInfo"]["name"] == "wittycode"
        assert init["clientCapabilities"] == {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
            "elicitation": {"form": {}, "url": {}},
        }, "no files or terminal of ours; questions (elicitation) in both modes"

    run(scenario())
