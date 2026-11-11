"""``/compact``, ``/goal`` and Plan mode through the real ``AppCore``: a conversation on an API provider's model (WittyCode's own loop,
a server on this machine) and one on Droid (a scripted session): what the page asks for, what Python answers, and what the model is sent."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from droid_sdk import DroidError

from desktop.core.harness import compact, goal as goals, skills
from desktop.core.session_manager import SessionController
from desktop.core.transcript import TurnStatus
from tests.fakes import FakeSession, until
from tests.llm_support import FakeApi, http_error
from tests.support import complete, delta, success
from tests.test_app_api import answer_of, api_core, api_model_id, blocks, meta, new_session, reads, run, says


def system_of(post) -> str:
    return post[2]["messages"][0]["content"]


def last_words(post) -> str:
    return post[2]["messages"][-1]["content"]


async def ready(events, cid: str) -> None:
    await until(lambda: meta(events, cid).get("mark") == "ready")


def all_blocks(events, cid: str) -> list[dict]:
    """The blocks of every turn of a conversation, as the page last got them."""
    latest: dict[str, list[dict]] = {}
    for payload in events.of("turns"):
        if payload["id"] == cid:
            for turn in payload["turns"]:
                latest[turn["id"]] = turn["blocks"]
    return [block for turn_blocks in latest.values() for block in turn_blocks]


async def droid_session(core, events, tmp_path) -> str:
    """A conversation on Droid (the default model), ready."""
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
    await ready(events, cid)
    return cid


async def droid_answers(droid, number: int, text: str) -> None:
    """The ``number``-th message sent to the scripted Droid session is answered with ``text``."""
    await until(lambda: len(droid.streams) >= number)
    stream = droid.streams[number - 1]
    stream.push(delta(text, f"m{number}"), complete(f"m{number}"), success(text))
    stream.end()


async def ready_again(core, cid: str) -> None:
    from desktop.core.session_manager import SessionState

    await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)


async def say_and_wait(core, events, cid: str, text: str, answer: str) -> None:
    await core.call("session.send", {"id": cid, "text": text})
    await until(lambda: answer_of(events, cid) == answer and meta(events, cid).get("mark") == "ready")


# ------------------------------------------------------------------------------------------------ /compact


def test_compact_turns_the_conversation_into_a_summary_the_next_message_starts_from(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Four."), says("SUMMARY: the user asked 2+2, the answer was four."), says("Six."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await say_and_wait(core, events, cid, "what is 2+2? " + "background " * 400, "Four.")

            result = await core.call("session.compact", {"id": cid})
            assert result == {"ok": True, "text": ""}
            await until(lambda: any(b["kind"] == "notice" and "已按你的要求压缩" in b["text"] for b in blocks(events, cid)))
            await until(lambda: meta(events, cid).get("mark") == "ready" and meta(events, cid)["context"]["compacted"] is True)
            assert compact.INSTRUCTIONS[:40] in system_of(api.posts[1]), "the summary request is the compaction's own"
            assert any(b["kind"] == "user" and b["text"] == "/compact" for b in blocks(events, cid)), "the page shows the command"

            await say_and_wait(core, events, cid, "and 3+3?", "Six.")
            sent = api.posts[2][2]["messages"]
            assert [m["role"] for m in sent] == ["system", "user", "user"], "the summary, then the new question: nothing older"
            assert "SUMMARY: the user asked 2+2" in sent[1]["content"] and sent[2]["content"] == "and 3+3?"
            await core.shutdown()

    run(scenario())


def test_compact_says_why_when_it_cannot(tmp_path, monkeypatch):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            nothing = await core.call("session.compact", {"id": cid})
            assert nothing["ok"] is False and "还没有对话" in nothing["text"]
            assert api.posts == []

            droid = await droid_session(core, events, tmp_path)
            empty = await core.call("session.compact", {"id": droid})
            assert empty["ok"] is False and "还没有对话" in empty["text"], "an engine is not asked to compact a conversation with nothing in it"
            assert factory.sessions[-1].compacts == 0

            monkeypatch.delattr(SessionController, "start_compaction")
            lacking = await core.call("session.compact", {"id": droid})
            assert lacking["ok"] is False and "没有提供手动压缩" in lacking["text"] and "Droid" in lacking["text"]
            await core.shutdown()

    run(scenario())


def test_compact_waits_behind_messages_that_are_still_queued_and_behind_a_goal(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("one"))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await say_and_wait(core, events, cid, "first", "one")
            conv = core._by_cid[cid]
            conv.queue.append(type("Q", (), {"id": "q1", "text": "later", "attachments": ()})())
            refused = await core.call("session.compact", {"id": cid})
            assert refused["ok"] is False and "排队" in refused["text"]
            conv.queue.clear()
            conv.goal = goals.Goal("not finished")
            refused = await core.call("session.compact", {"id": cid})
            assert refused["ok"] is False and "目标" in refused["text"] and "/goal off" in refused["text"]
            await core.shutdown()

    run(scenario())


def test_compact_on_a_droid_conversation_is_droids_own_compaction_and_the_page_shows_it(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "what is 2+2"})
            await droid_answers(droid, 1, "It is four.")
            await ready_again(core, cid)
            result = await core.call("session.compact", {"id": cid})
            assert result == {"ok": True, "text": ""}
            await until(lambda: any(b["kind"] == "notice" and "Droid 压缩了上下文：较早的 4 条消息" in b["text"] for b in blocks(events, cid)))
            await ready_again(core, cid)
            assert droid.compacts == 1
            assert any(b["kind"] == "user" and b["text"] == "/compact" for b in blocks(events, cid)), "the page shows the command"
            assert meta(events, cid)["context"]["compacted"] is True
            assert core._by_cid[cid].controller.transcript.turns[-1].status is TurnStatus.DONE
            await core.call("session.send", {"id": cid, "text": "and 3+3?"})
            await droid_answers(droid, 2, "Six.")
            await core.shutdown()

    run(scenario())


def test_droid_cannot_be_asked_to_compact_while_a_turn_runs_and_the_session_it_hands_back_takes_over(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            controller = core._by_cid[cid].controller
            await core.call("session.send", {"id": cid, "text": "a slow one"})
            await until(lambda: droid.active is not None)
            assert "还在运行" in controller.start_compaction() and droid.compacts == 0
            await droid_answers(droid, 1, "Done.")
            await ready_again(core, cid)

            successor = FakeSession(droid.interactions, session_id="after-the-compaction")
            droid.compact_successor = successor
            assert (await core.call("session.compact", {"id": cid}))["ok"] is True
            await until(lambda: controller.session_id == "after-the-compaction")
            await ready_again(core, cid)
            await core.call("session.send", {"id": cid, "text": "and now?"})
            await droid_answers(successor, 1, "Now this.")
            assert len(droid.streams) == 1, "the next message goes to the session Droid made"
            await core.shutdown()

    run(scenario())


def test_a_compaction_droid_refuses_is_told_as_one_not_as_an_internal_error(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "hello"})
            await droid_answers(droid, 1, "Hi.")
            await ready_again(core, cid)
            droid.compact_error = DroidError("no room")
            assert (await core.call("session.compact", {"id": cid}))["ok"] is True
            await until(lambda: any(b["kind"] == "error" and "没有压缩，对话没有改动：" in b.get("message", "") for b in blocks(events, cid)))
            await core.shutdown()

    run(scenario())


def test_a_droid_compaction_that_fails_leaves_the_conversation_and_says_so_and_a_stop_ends_it(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "hello"})
            await droid_answers(droid, 1, "Hi.")
            await ready_again(core, cid)

            droid.compact_error = RuntimeError("no room")
            assert (await core.call("session.compact", {"id": cid}))["ok"] is True
            await until(lambda: any(b["kind"] == "error" and "no room" in b.get("message", "") for b in blocks(events, cid)))
            await ready_again(core, cid)
            assert core._by_cid[cid].controller.transcript.turns[-1].status is TurnStatus.FAILED

            droid.compact_error, droid.compact_delay = None, 30.0
            assert (await core.call("session.compact", {"id": cid}))["ok"] is True
            await until(lambda: droid.compacts == 2)
            await core.call("session.stop", {"id": cid})
            await ready_again(core, cid)
            assert core._by_cid[cid].controller.transcript.turns[-1].status is TurnStatus.INTERRUPTED
            await core.shutdown()

    run(scenario())


# ------------------------------------------------------------------------------------------------ /goal, on any engine


def test_a_goal_goes_on_by_itself_until_the_model_says_it_is_done_and_each_round_puts_the_goal_to_it_again(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Looking into it."), says("All of it passes.\nGOAL: DONE"))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            result = await core.call("session.goal", {"id": cid, "text": "make the tests pass"})
            assert result == {"ok": True, "text": ""}
            await until(lambda: len(api.posts) == 2 and meta(events, cid).get("mark") == "ready")
            await asyncio.sleep(0.2)
            assert len(api.posts) == 2, "it stops when the model says it is done"
            assert last_words(api.posts[0]) == goals.opening("make the tests pass")
            assert last_words(api.posts[1]) == goals.Goal("make the tests pass", rounds=1).next_message()
            shown = blocks(events, cid)
            assert any(b["kind"] == "notice" and "目标完成了（用了 2 轮）" in b["text"] for b in shown)
            assert any(b["kind"] == "notice" and b["text"].startswith("目标已设定：make the tests pass") for b in all_blocks(events, cid))
            assert core._by_cid[cid].goal is None
            await core.shutdown()

    run(scenario())


def test_a_goal_works_the_same_on_a_droid_conversation(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            assert (await core.call("session.goal", {"id": cid, "text": "ship it"}))["ok"] is True
            await droid_answers(droid, 1, "Working on it.")
            await until(lambda: len(droid.streams) == 2)
            assert droid.streams[0].prompt == goals.opening("ship it")
            assert droid.streams[1].prompt == goals.Goal("ship it", rounds=1).next_message()
            await droid_answers(droid, 2, "Checked it.\nGOAL: DONE")
            await until(lambda: any(b["kind"] == "notice" and "目标完成了（用了 2 轮）" in b["text"] for b in blocks(events, cid)))
            await asyncio.sleep(0.2)
            assert len(droid.streams) == 2 and core._by_cid[cid].goal is None
            await core.shutdown()

    run(scenario())


def test_a_goal_stops_when_the_model_needs_the_user_or_only_talks_or_runs_out_of_rounds(tmp_path, monkeypatch):
    async def scenario():
        with FakeApi() as api:
            api.script(says("I cannot log in.\nGOAL: BLOCKED I need the staging password"), says("Let me think."), says("Still thinking."), says("never"))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.goal", {"id": cid, "text": "deploy it"})
            await until(lambda: any("需要你" in b.get("text", "") and "I need the staging password" in b["text"] for b in blocks(events, cid)))
            assert len(api.posts) == 1 and core._by_cid[cid].goal is None

            await core.call("session.goal", {"id": cid, "text": "do the thing"})
            await until(lambda: any("连续 2 轮没有动手" in b.get("text", "") for b in blocks(events, cid)))
            assert len(api.posts) == 3 and core._by_cid[cid].goal is None
            await core.shutdown()

    run(scenario())


def test_a_goal_reads_the_last_message_of_a_turn_not_the_first(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.goal", {"id": cid, "text": "ship it"})
            await until(lambda: len(droid.streams) == 1)
            stream = droid.streams[0]
            stream.push(delta("Let me check.", "m1"), complete("m1"), delta("Checked.\nGOAL: DONE", "m2"), complete("m2"), success("Checked."))
            stream.end()
            await until(lambda: any("目标完成了（用了 1 轮）" in b.get("text", "") for b in blocks(events, cid)))
            await asyncio.sleep(0.2)
            assert len(droid.streams) == 1 and core._by_cid[cid].goal is None
            await core.shutdown()

    run(scenario())


def test_a_goal_reads_how_a_turn_ends_and_a_round_that_used_tools_is_work(tmp_path):
    async def scenario():
        with FakeApi() as api:
            (tmp_path / "proj").mkdir(exist_ok=True)
            (tmp_path / "proj" / "a.txt").write_text("alpha\n", encoding="utf-8")
            api.script(
                reads("a.txt", call_id="c1"), says("Not there yet."),
                reads("a.txt", call_id="c2"), says("Still not."),
                reads("a.txt", call_id="c3"), says("Closer."),
                reads("a.txt", call_id="c4"), says("All of it is done.\nGOAL: DONE"),
            )
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events), autonomy="full")
            await core.call("session.goal", {"id": cid, "text": "finish"})
            await until(lambda: any("目标完成了（用了 4 轮）" in b.get("text", "") for b in blocks(events, cid)))
            assert len(api.posts) == 8, "four rounds of a tool call and an answer: work is not idling"
            await core.shutdown()

    run(scenario())


def test_a_goal_stops_when_the_next_round_cannot_be_sent(tmp_path, monkeypatch):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Working on it."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            controller = core._by_cid[cid].controller
            real, sent = controller.send_prompt, []

            def send(text, *more, **options):
                sent.append(text)
                return real(text, *more, **options) if len(sent) == 1 else False

            monkeypatch.setattr(controller, "send_prompt", send)
            await core.call("session.goal", {"id": cid, "text": "anything"})
            await until(lambda: any("下一轮没能发出" in b.get("text", "") for b in blocks(events, cid)))
            assert core._by_cid[cid].goal is None
            await core.shutdown()

    run(scenario())


def test_stopping_or_a_failed_turn_ends_a_goal(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(http_error(400, {"error": {"message": "oops"}}))  # (a 5xx would be asked again after a pause)
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.goal", {"id": cid, "text": "anything"})
            await until(lambda: any("目标先停下了" in b.get("text", "") and "出错" in b["text"] for b in blocks(events, cid)))
            assert core._by_cid[cid].goal is None

            droid_cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.goal", {"id": droid_cid, "text": "slow"})
            await until(lambda: droid.active is not None)
            await core.call("session.stop", {"id": droid_cid})
            await until(lambda: any("目标先停下了" in b.get("text", "") and "被停止" in b["text"] for b in blocks(events, droid_cid)))
            assert core._by_cid[droid_cid].goal is None and len(droid.streams) == 1
            await core.shutdown()

    run(scenario())


def test_a_message_the_user_queued_goes_before_the_next_turn_of_the_goal(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("Working."), says("Your answer."), says("Done now.\nGOAL: DONE"))
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.goal", {"id": cid, "text": "ship it"})
            await core.call("session.send", {"id": cid, "text": "a question of mine"})
            await until(lambda: len(api.posts) == 3 and meta(events, cid).get("mark") == "ready")
            await asyncio.sleep(0.2)
            assert [last_words(post) for post in api.posts] == [
                goals.opening("ship it"), "a question of mine", goals.Goal("ship it", rounds=1).next_message(),
            ]
            await core.shutdown()

    run(scenario())


def test_a_goal_that_cannot_start_or_is_cancelled_says_so(tmp_path):
    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            core._by_cid[cid].queue.append(type("Q", (), {"id": "q1", "text": "later", "attachments": ()})())
            queued = await core.call("session.goal", {"id": cid, "text": "do it"})
            assert queued["ok"] is False and "排队" in queued["text"] and api.posts == []
            core._by_cid[cid].queue.clear()
            assert (await core.call("session.goal", {"id": cid, "text": "  "}))["text"].startswith("用法")
            assert await core.call("session.goal", {"id": cid, "text": "off"}) == {"ok": True, "text": "现在没有进行中的目标。"}
            assert api.posts == []

            await core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"})
            planned = await core.call("session.goal", {"id": cid, "text": "do it"})
            assert planned["ok"] is False and "Plan" in planned["text"]

            droid_cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            assert (await core.call("session.goal", {"id": droid_cid, "text": "first"}))["ok"] is True
            running = await core.call("session.goal", {"id": droid_cid, "text": "second"})
            assert running["ok"] is False and "还在运行" in running["text"]
            assert core._by_cid[droid_cid].goal.text == "first", "a refused goal leaves the one in force"
            for word in ("off", "STOP", "取消"):
                core._by_cid[droid_cid].goal = goals.Goal("again")
                assert await core.call("session.goal", {"id": droid_cid, "text": word}) == {"ok": True, "text": "目标已取消。"}
                assert core._by_cid[droid_cid].goal is None
            await core.shutdown()

    run(scenario())

# ------------------------------------------------------------------------------------------------ Plan mode


def test_the_mode_chosen_for_new_conversations_is_the_own_loops_too(tmp_path):
    async def scenario():
        with FakeApi() as api:
            api.script(says("A plan."))
            core, _, events, _, _ = await api_core(tmp_path, api)
            await core.call("session.choose", {"field": "mode", "value": "spec"})
            cid = await new_session(core, events, tmp_path, api_model_id(events))
            assert meta(events, cid)["settings"]["mode"] == "spec" and core._by_cid[cid].controller.mode == "spec"
            await say_and_wait(core, events, cid, "add a feature", "A plan.")
            assert "Plan mode is on" in system_of(api.posts[0])

            assert (await core.call("session.choose", {"id": cid, "field": "mode", "value": "auto"}))["ok"] is True
            assert core._by_cid[cid].controller.mode == "auto"
            await until(lambda: meta(events, cid)["settings"]["mode"] == "auto")
            await core.shutdown()

    run(scenario())


# ------------------------------------------------------------------------------------------------ skills and custom commands


def make_skill(home: Path, name: str, description: str, body: str) -> Path:
    path = home / ".factory" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_the_page_is_given_the_skills_and_commands_of_the_conversations_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(skills, "USER_HOME", home)
    make_skill(home, "weekly-report", "Writes the weekly report.", "Use the template.")
    command = home / ".factory" / "commands" / "deploy.md"
    command.parent.mkdir(parents=True)
    command.write_text("---\ndescription: Ship it\nargument-hint: <env>\n---\nDeploy $ARGUMENTS", encoding="utf-8")
    (tmp_path / "proj" / ".factory" / "skills" / "local").mkdir(parents=True)
    (tmp_path / "proj" / ".factory" / "skills" / "local" / "SKILL.md").write_text("---\nname: local\ndescription: Only here.\n---\nx", encoding="utf-8")

    async def scenario():
        with FakeApi() as api:
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            result = await core.call("skills.list", {"id": cid})
            by_name = {item["name"]: item for item in result["items"]}
            assert set(by_name) == {"deploy", "weekly-report", "local"}
            assert (by_name["deploy"]["kind"], by_name["deploy"]["hint"], by_name["deploy"]["source"]) == ("command", "<env>", "personal")
            assert (by_name["local"]["kind"], by_name["local"]["source"]) == ("skill", "project")
            by_folder = await core.call("skills.list", {"projectId": str(tmp_path / "elsewhere")})
            assert {item["name"] for item in by_folder["items"]} == {"deploy", "weekly-report"}, "another project has not the first one's skill"
            make_skill(home, "made-a-moment-ago", "Just made.", "x")
            again = await core.call("skills.list", {"id": cid})
            assert "made-a-moment-ago" in {item["name"] for item in again["items"]}, "read from the files every time"
            await core.shutdown()

    run(scenario())


def test_a_message_that_names_a_skill_carries_it_on_every_engine_and_the_page_shows_what_was_typed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(skills, "USER_HOME", home)
    make_skill(home, "summarize-diff", "Summarizes the diff.", "Run git diff and list the risks.")

    async def scenario():
        with FakeApi() as api:
            api.script(says("Summary."))
            core, factory, events, _, _ = await api_core(tmp_path, api)
            droid_cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": droid_cid, "text": "/summarize-diff focus on the migration"})
            await until(lambda: len(droid.streams) == 1)
            sent = droid.streams[0].prompt
            assert sent.startswith("/summarize-diff focus on the migration") and "Run git diff and list the risks." in sent and "<wittycode-context>" in sent
            await until(lambda: any(b["kind"] == "user" and b["text"] == "/summarize-diff focus on the migration" for b in all_blocks(events, droid_cid)))
            assert not any("Run git diff" in b.get("text", "") for b in all_blocks(events, droid_cid)), "the page shows what was typed, not the skill"
            await droid_answers(droid, 1, "Done.")

            own = await new_session(core, events, tmp_path, api_model_id(events))
            await core.call("session.send", {"id": own, "text": "please /summarize-diff"})
            await until(lambda: len(api.posts) == 1)
            assert "Run git diff and list the risks." in last_words(api.posts[0])
            assert "- summarize-diff: Summarizes the diff." in system_of(api.posts[0]), "and the model is told which skills there are"
            await core.shutdown()

    run(scenario())


def test_a_custom_command_runs_with_its_arguments_on_any_engine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(skills, "USER_HOME", home)
    command = home / ".factory" / "commands" / "code-review.md"
    command.parent.mkdir(parents=True)
    command.write_text("Review the branch `$ARGUMENTS` for bugs.", encoding="utf-8")

    async def scenario():
        with FakeApi() as api:
            core, factory, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            droid = factory.sessions[-1]
            await core.call("session.send", {"id": cid, "text": "/code-review feature/login"})
            await until(lambda: len(droid.streams) == 1)
            assert "Review the branch `feature/login` for bugs." in droid.streams[0].prompt
            await until(lambda: any("已载入命令 /code-review" in b.get("text", "") for b in all_blocks(events, cid)))
            await core.shutdown()

    run(scenario())


def test_the_skills_list_is_for_the_conversations_project_else_the_one_asked_for_else_the_last_one_used_else_home(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(skills, "listing", lambda project, home=None: seen.append(str(project)) or [])

    async def scenario():
        with FakeApi() as api:
            core, _, events, _, _ = await api_core(tmp_path, api)
            cid = await droid_session(core, events, tmp_path)
            core.settings.last_project = str(tmp_path / "last")
            await core.call("skills.list", {"id": cid})
            await core.call("skills.list", {"projectId": str(tmp_path / "asked")})
            await core.call("skills.list", {})
            core.settings.last_project = ""
            await core.call("skills.list", {})
            assert seen == [str(tmp_path / "proj"), str(tmp_path / "asked"), str(tmp_path / "last"), str(Path.home())]
            await core.shutdown()

    run(scenario())
