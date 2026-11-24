"""A rebuilt instruction snapshot supersedes context updates still in the Responses history."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from desktop.core.chatgpt.controller import PLAN
from desktop.core.chatgpt.responses import Shape, user_message
from desktop.core.fusion.registry import Policy
from desktop.core.harness.compact import continuity_item, summary_item
from desktop.core.harness.prompt import context_update
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import DEFAULT_MODELS, FakeResponses, call, say, usage

OLDER = "gpt-6.1-sol"
NEWER = "gpt-6.2-sol"
REFRESH = (
    "WittyCode context update: the instruction snapshot at the start of the conversation now contains the complete current "
    "rules. All context-update messages before this one no longer apply."
)


@asynccontextmanager
async def opened(tmp_path, model):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    with FakeResponses(models=[{**DEFAULT_MODELS[0], "slug": model}, DEFAULT_MODELS[1]]) as fake:
        engine = make_engine(tmp_path, fake, shape=Shape())
        await engine.list_models()
        ctl = await engine.open_new(project, model=model, reasoning="low", mode="auto")
        try:
            yield ctl, fake
        finally:
            await engine.close_all()


async def send(ctl, fake, text, *, used=None):
    count = len(fake.requests)
    events = say("Acknowledged.", item_id=f"msg_{count + 1}", used=used)
    fake.script(events)
    assert ctl.send_prompt(text)
    await finish_turn(ctl)
    assert ctl.last_turn_end is not None and ctl.last_turn_end.reason == "done"
    assert len(fake.requests) == count + 1
    return fake.requests[-1], events[-2]["item"]


def developer(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def enable_fusion(ctl, model):
    worker = "chatgpt:gpt-6-luna"
    ctl.enable_fusion(Policy(main=f"chatgpt:{model}", workers=(worker,), light=worker, max_calls=4))
    assert ctl.model == model and not ctl.saved.prompt


@pytest.mark.parametrize("model", [OLDER, NEWER])
def test_refresh_follows_the_old_update_and_precedes_the_user(tmp_path, model):
    async def go():
        async with opened(tmp_path, model) as (ctl, fake):
            await send(ctl, fake, "Start in Auto.")
            initial = ctl.saved.prompt["instructions"]
            assert await ctl.update_settings(mode=PLAN)
            plan, reply = await send(ctl, fake, "Switch to Plan.")
            old_update = plan["input"][-2]
            assert old_update["role"] == "developer"
            assert "Plan mode is on." in old_update["content"][0]["text"]
            assert any(item.get("wc_context_update") for item in ctl.saved.items)
            assert ctl.saved.prompt["instructions"] == initial

            ordinary, ordinary_reply = await send(ctl, fake, "Continue planning.")
            assert ordinary == {**plan, "input": [*plan["input"], reply, user_message("Continue planning.")]}

            history = deepcopy(ctl.saved.items)
            assert await ctl.update_settings(mode="auto")
            enable_fusion(ctl, model)
            rebuilt, _ = await send(ctl, fake, "Continue in Fusion Auto.")
            assert rebuilt["model"] == model
            refreshed = ctl.saved.prompt["instructions"]
            assert "Fusion mode: you are the fixed main agent" in refreshed
            assert "Auto mode is on." in refreshed and "Plan mode is on." not in refreshed
            if model == NEWER:
                assert "instructions" not in rebuilt
                assert rebuilt["input"][0] == developer(refreshed)
                tail, previous = rebuilt["input"][1:], ordinary["input"][1:]
            else:
                assert rebuilt["instructions"] == refreshed
                tail, previous = rebuilt["input"], ordinary["input"]
            assert tail == [*previous, ordinary_reply, developer(REFRESH), user_message("Continue in Fusion Auto.")]
            saved = ctl.manager.sessions.load(ctl.session_id)
            assert saved is not None and saved.items[:len(history)] == history
            assert saved.items[len(history)] == {**developer(REFRESH), "wc_context_refresh": True}

    asyncio.run(go())


@pytest.mark.parametrize("history", ["no-update", "summarized", "rules-checkpoint"])
def test_refresh_checks_the_compacted_view_not_the_log(tmp_path, history):
    async def go():
        async with opened(tmp_path, OLDER) as (ctl, fake):
            first, reply = await send(ctl, fake, "Start in Auto.")
            expected = [*first["input"], reply]
            if history != "no-update":
                assert await ctl.update_settings(mode=PLAN)
                await send(ctl, fake, "Switch to Plan.")
                assert any(item.get("wc_context_update") for item in ctl.saved.items)
                summary = "The user switched modes."
                fake.script(say(summary))
                assert ctl.start_compaction() is None
                await finish_turn(ctl)
                assert ctl.saved.compaction["upto"] == len(ctl.saved.items)
                expected = [summary_item(summary)]
                if history == "rules-checkpoint":
                    # Older compaction records can synthesize a developer update ahead of the retained tail.
                    rules = "Follow previously saved project rules."
                    ctl.saved.compaction["rules"] = rules
                    update = context_update(rules)
                    del update["wc_context_update"]
                    expected += [update, developer(REFRESH)]
                assert await ctl.update_settings(mode="auto")

            log = deepcopy(ctl.saved.items)
            enable_fusion(ctl, OLDER)
            rebuilt, _ = await send(ctl, fake, "Continue in Fusion Auto.")
            assert "Fusion mode: you are the fixed main agent" in rebuilt["instructions"]
            assert rebuilt["input"] == [*expected, user_message("Continue in Fusion Auto.")]
            assert ctl.saved.items[:len(log)] == log

    asyncio.run(go())


def test_partial_compaction_keeps_recent_tool_turns_before_the_refresh(tmp_path):
    async def go():
        async with opened(tmp_path, OLDER) as (ctl, fake):
            await send(ctl, fake, "Older conversation:\n" + "Earlier work and decisions.\n" * 900)
            assert await ctl.update_settings(mode=PLAN)
            plan, _ = await send(ctl, fake, "Plan the next reads.")
            old_update = len(plan["input"]) - 2
            assert ctl.saved.items[old_update]["wc_context_update"] is True
            recent_start = len(ctl.saved.items)
            for number in range(3):
                name = f"batch-{number}.txt"
                (ctl.project / name).write_text("Recorded tool evidence for the current batch.\n" * 100, encoding="utf-8")
                fake.script(call("read_file", {"file_path": name}, call_id=f"call_{number}", item_id=f"fc_{number}"),
                            say("Read the batch.", item_id=f"msg_batch_{number}"))
                assert ctl.send_prompt(f"Read batch {number}.")
                await finish_turn(ctl)
                assert ctl.saved.items[-2]["wc_output_ref"]

            assert await ctl.update_settings(mode="auto")
            enable_fusion(ctl, OLDER)
            rebuilt, reply = await send(ctl, fake, "Continue in Fusion.", used=usage(25_000))
            assert rebuilt["input"][-2] == developer(REFRESH)
            assert not ctl.saved.compaction

            # The reported usage crosses the threshold on the next turn. The 8,000-token tail can hold all three reads.
            ctl.manager._windows[OLDER] = 32_000
            ctl.manager.settings.chatgpt_compact_at = 75
            count = len(fake.requests)
            summary = "The earlier work concluded with a plan to read the batches."
            fake.script(say(summary), say("Continuing.", item_id="msg_compacted"))
            assert ctl.send_prompt("Continue after compaction.")
            await finish_turn(ctl)
            first_summary, continued = fake.requests[count:]
            record = ctl.saved.compaction
            cut = record["upto"]
            assert old_update < cut <= recent_start
            assert first_summary["tool_choice"] == "none", "the retained tool results must not force a chunked summary"
            assert first_summary["input"][:-1] == [*rebuilt["input"][:cut], developer(REFRESH)]
            assert continued["input"] == [summary_item(summary), continuity_item(record["continuity"]),
                                           *rebuilt["input"][cut:], reply, user_message("Continue after compaction.")]

            # Only the old refresh remains in the view, so another rebuild must not add a second one.
            ctl.disable_fusion(OLDER, "low")
            assert not ctl.saved.prompt
            again, _ = await send(ctl, fake, "Continue outside Fusion.")
            assert again["input"].count(developer(REFRESH)) == 1
            assert plan["input"][old_update] not in again["input"]

    asyncio.run(go())


def test_a_rule_change_after_a_refresh_is_still_a_context_update(tmp_path):
    async def go():
        async with opened(tmp_path, OLDER) as (ctl, fake):
            await send(ctl, fake, "Start in Auto.")
            assert await ctl.update_settings(mode=PLAN)
            plan, _ = await send(ctl, fake, "Switch to Plan.")
            assert await ctl.update_settings(mode="auto")
            enable_fusion(ctl, OLDER)
            await send(ctl, fake, "Continue in Fusion Auto.")

            # The original update is still visible, so rebuilding again needs a new refresh.
            ctl.disable_fusion(OLDER, "low")
            rebuilt, reply = await send(ctl, fake, "Continue outside Fusion.")
            assert rebuilt["input"].count(developer(REFRESH)) == 2
            assert rebuilt["input"][-2] == developer(REFRESH)
            assert plan["input"][-2] in rebuilt["input"]

            rules = "Keep reports under five lines."
            (ctl.project / "AGENTS.md").write_text(rules, encoding="utf-8")
            changed, _ = await send(ctl, fake, "Continue with the new project rule.")
            update = changed["input"][-2]
            assert update["role"] == "developer"
            assert rules in update["content"][0]["text"]
            assert changed == {**rebuilt, "input": [*rebuilt["input"], reply, update, user_message("Continue with the new project rule.")]}
            assert ctl.saved.items[-3] == {**update, "wc_context_update": True}

    asyncio.run(go())
