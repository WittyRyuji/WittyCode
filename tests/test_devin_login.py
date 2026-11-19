"""CLI login uses an interactive console and owns its process until cancellation has finished; no real account."""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.devin.account import DevinStatus
from desktop.core.devin.errors import DevinSignedOutError
from desktop.core.devin.locate import DevinBinary
from tests.devin_support import make_engine, run, until


def test_new_cli_acp_is_owned_in_process_without_changing_older_commands():
    old = DevinBinary(Path("devin.exe"), "3000.11.238")
    new = DevinBinary(Path("devin.exe"), "3000.13.4")
    assert old.argv("acp") == ["devin.exe", "acp"]
    assert new.argv("acp") == ["devin.exe", "--process-model", "in-process", "acp"]
    assert new.argv("auth", "login") == ["devin.exe", "auth", "login"]


def test_login_uses_a_real_console_instead_of_hiding_the_native_picker(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path, account="signed-out")
        original = harness.engine._start_process
        seen = []

        async def start(argv, **options):
            if argv[-2:] == ["auth", "login"]:
                seen.append(options.copy())
                # Test the contract without opening a console for the fake CLI.
                options.update(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if sys.platform == "win32":
                    options["creationflags"] = subprocess.CREATE_NO_WINDOW
            return await original(argv, **options)

        monkeypatch.setattr(harness.engine, "_start_process", start)
        assert (await harness.engine.login())["auth"] == "signed-in"
        assert len(seen) == 1
        assert all(seen[0][stream] is None for stream in ("stdin", "stdout", "stderr"))
        if sys.platform == "win32":
            assert seen[0]["creationflags"] == subprocess.CREATE_NEW_CONSOLE
    run(go())


def test_already_signed_in_cli_does_not_launch_another_login(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        original = harness.engine._start_process
        async def start(argv, **options):
            assert argv[-2:] != ["auth", "login"]
            return await original(argv, **options)
        monkeypatch.setattr(harness.engine, "_start_process", start)
        assert (await harness.engine.login())["auth"] == "signed-in"
        assert [a["auth"] for a in harness.accounts] == ["signed-in"]
    run(go())


def test_cancelling_login_waits_for_its_process_and_transport(tmp_path):
    async def go():
        harness = make_engine(tmp_path, account="signed-out", FAKE_DEVIN_LOGIN_DELAY="never")
        task = asyncio.create_task(harness.engine.login())
        await until(lambda: harness.engine._login is not None)
        process = harness.engine._login.process
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
        assert process._transport._loop is None
        assert harness.engine._login is None
    run(go())


def test_concurrent_login_requests_share_one_process(tmp_path):
    async def go():
        harness = make_engine(tmp_path, account="signed-out", FAKE_DEVIN_LOGIN_DELAY="never")
        first = asyncio.create_task(harness.engine.login())
        second = asyncio.create_task(harness.engine.login())
        await until(lambda: harness.engine._login is not None)
        process = harness.engine._login.process
        await harness.engine.cancel_login()
        results = await asyncio.gather(first, second)
        assert all(result["auth"] == "signed-out" for result in results)
        assert process.returncode is not None and process._transport._loop is None
    run(go())


def test_cancel_during_cli_spawn_acquires_and_reaps_the_process(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        original = asyncio.create_subprocess_exec
        acquired = asyncio.Event()
        release = asyncio.Event()
        children = []
        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            children.append(process)
            acquired.set()
            await release.wait()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
        task = asyncio.create_task(harness.engine._cli("auth", "status"))
        await acquired.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(children) == 1
        assert children[0].returncode is not None and children[0]._transport._loop is None
    run(go())


def test_signed_out_account_cannot_cache_guest_models(tmp_path):
    async def go():
        harness = make_engine(tmp_path, account="signed-out")
        with pytest.raises(DevinSignedOutError):
            await harness.engine.list_models()
        assert harness.engine._models is None
        assert not harness.requests("session/new")
    run(go())


def test_account_transition_invalidates_the_model_catalog(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        await harness.engine.account()
        await harness.engine.list_models()
        assert harness.engine._models
        async def signed_out(*args, **kwargs):
            return 0, "Not logged in."
        monkeypatch.setattr(harness.engine, "_cli", signed_out)
        assert (await harness.engine.account())["auth"] == "signed-out"
        assert harness.engine._models is None
    run(go())


def test_status_failure_is_not_misreported_as_signed_out(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        await harness.engine.account()
        async def failed(*args, **kwargs):
            return 1, "unavailable"
        monkeypatch.setattr(harness.engine, "_cli", failed)
        result = await harness.engine.account()
        assert result["auth"] == "signed-in" and result["error"]
        assert harness.engine._status.signed_in
    run(go())
