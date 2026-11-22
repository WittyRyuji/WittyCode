"""The logical-session archive across an engine switch (``app_core._switch_engine`` hands the trusted sid to the
freshly opened controller): GPT → native Devin → GPT stays one ``<sid>`` directory, every segment binds its real
native session id, and the timeline keeps the entry order — no switch invents a second logical session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from desktop.core import session_archive
from desktop.core.app_core import AppError
from desktop.core.chatgpt.controller import ARCHIVE_ORIGIN
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.devin_support import make_engine as make_devin
from tests.fakes import until
from tests.fake_responses import say, usage


def run(coro):
    return asyncio.run(coro)


def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "session"


def manifest_of(tmp_path: Path, sid: str) -> dict[str, Any]:
    return session_archive.read_manifest(archive_root(tmp_path), sid)


def sessions_of(events) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for payload in events.of("session"):
        merged.setdefault(payload["id"], {}).update(payload["patch"])
    return merged


async def new_session(core, events, project) -> str:
    cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
    await until(lambda: sessions_of(events).get(cid, {}).get("mark") == "ready")
    return cid


async def say_and_wait(core, events, cid: str, text: str) -> None:
    turns = len(core._by_cid[cid].controller.transcript.turns)
    await core.call("session.send", {"id": cid, "text": text})
    await until(
        lambda: len(core._by_cid[cid].controller.transcript.turns) == turns + 1
        and core._by_cid[cid].controller.state is SessionState.READY,
        timeout=10,
    )


# ----------------------------------------------------------------------------------------------- engine switch


def test_a_switch_to_native_devin_and_back_keeps_one_logical_archive(tmp_path):
    async def scenario() -> None:
        harness = make_devin(tmp_path)
        core, plan, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        plan.script(
            say("the answer", used=usage(1000, 100)),
            say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)),
            say("back on the plan", used=usage(700, 50)),
        )
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")

        conv = core._by_cid[cid]
        gpt_first = conv.controller
        sid = gpt_first.saved.origin[ARCHIVE_ORIGIN]
        assert sid and gpt_first._archive_binding is not None and gpt_first._archive_binding.archive.sid == sid

        await core._switch_engine(conv, "devin:swe-2-high")
        devin = conv.controller
        assert devin is not gpt_first and devin.engine == "devin"
        assert devin._archive_sid == sid and devin._archive.sid == sid, "the same sid follows into the Devin segment"
        manifest = manifest_of(tmp_path, sid)
        (devin_identity,) = [item for item in manifest["identities"] if item["backend"] == "devin"]
        assert (devin_identity["connection"], devin_identity["native"]) == ("devin", devin.session_id), (
            "the Devin segment binds the native id its endpoint actually returned"
        )

        await core._switch_engine(conv, "chatgpt:gpt-6-luna")
        gpt_second = conv.controller
        assert gpt_second is not gpt_first and gpt_second.engine == "chatgpt"
        assert gpt_second.saved.origin[ARCHIVE_ORIGIN] == sid, "the restored binding lands in the checkpointed origin"
        assert gpt_second._archive_binding is not None and gpt_second._archive_binding.archive.sid == sid
        assert not gpt_second._archive_pending
        assert gpt_second._outputs.root == archive_root(tmp_path) / sid / "output", (
            "one output store per logical session"
        )

        await say_and_wait(core, events, cid, "continuing")
        manifest = manifest_of(tmp_path, sid)
        assert [entry["backend"] for entry in manifest["timeline"]] == ["gpt", "devin", "gpt"], (
            "three entries in entry order under one sid"
        )
        natives = {(item["backend"], item["native"]) for item in manifest["identities"]}
        assert natives == {
            ("gpt", gpt_first.session_id), ("devin", devin.session_id), ("gpt", gpt_second.session_id)
        }, "each segment binds its own native id; the new GPT session is a new iid, not a grafted old one"
        await core.shutdown()

    run(scenario())


def test_a_failed_switch_checkpoint_never_enters_the_archive(tmp_path):
    """``history.save`` failing rolls the switch back: the GPT controller stays the conversation's, and the
    archive's timeline and current never point at a Devin segment that never took over."""
    async def scenario() -> None:
        harness = make_devin(tmp_path)
        core, plan, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        plan.script(say("the answer", used=usage(1000, 100)), say("SUMMARY OF THE EARLIER PART", used=usage(900, 40)))
        cid = await new_session(core, events, tmp_path / "proj")
        await say_and_wait(core, events, cid, "hello")

        conv = core._by_cid[cid]
        gpt_first = conv.controller
        sid = gpt_first.saved.origin[ARCHIVE_ORIGIN]

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise OSError("the checkpoint disk is full")

        core.history.save = refuse  # type: ignore[method-assign]
        with pytest.raises(AppError):
            await core._switch_engine(conv, "devin:swe-2-high")

        assert conv.controller is gpt_first and conv.controller._archive_binding.archive.sid == sid
        manifest = manifest_of(tmp_path, sid)
        assert all(item["backend"] != "devin" for item in manifest["identities"]), "no devin identity was grafted"
        assert all(entry["backend"] != "devin" for entry in manifest["timeline"]), "no devin entry was recorded"
        assert manifest["current"]["backend"] == "gpt", "current still names the live GPT segment"
        await core.shutdown()

    run(scenario())
