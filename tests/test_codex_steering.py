"""Saying something to a Codex turn that is running (``turn/steer``), and the token series Codex contributes."""

from __future__ import annotations

import asyncio
import base64

from desktop.core.attachments import image_attachment, pdf_attachment
from desktop.core.session_manager import SessionState, SteerOutcome
from desktop.core.transcript import TurnStatus, UserBlock
from desktop.core.usage import Tokens
from tests.codex_support import finish_turn, make_engine, run, until
from tests.support import tiny_png


async def opened(harness, project):
    controller = await harness.engine.open_new(project)
    assert controller.state is SessionState.READY, controller.last_error
    return controller


async def running(harness, project, text="[slow] count"):
    controller = await opened(harness, project)
    assert controller.send_prompt(text)
    await until(lambda: controller._turn_id)
    return controller


def steers(controller):
    return [b for b in controller.transcript.turns[-1].blocks if isinstance(b, UserBlock) and b.steer]


def test_a_steer_goes_to_the_running_turn_by_its_id_and_is_shown_at_once(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path)
        turn_id = controller._turn_id
        outcome = await controller.steer("  go left instead  ")
        assert outcome == SteerOutcome(True)
        (sent,) = harness.requests("turn/steer")
        assert sent == {
            "threadId": controller.session_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": "go left instead", "text_elements": []}],
        }
        (block,) = steers(controller)
        assert (block.text, block.pending) == ("go left instead", False), "Codex echoes it in the same breath: nothing to wait for"
        assert controller.transcript.turns[-1].blocks[0].steer is False
        assert controller.take_unadopted() == []
        await controller.interrupt()
        assert controller.state is SessionState.READY
        await harness.engine.close_all()

    run(scenario())


def test_the_echo_of_a_steer_does_not_become_another_message(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path)
        assert (await controller.steer("one")).ok
        await asyncio.sleep(0.2)  # (the server's echo of the steer arrives as userMessage items)
        shown = [b for b in controller.transcript.turns[-1].blocks if isinstance(b, UserBlock)]
        assert [b.text for b in shown] == ["[slow] count", "one"], "Codex's userMessage items are not shown a second time"
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_steer_with_nothing_running_says_the_turn_is_over(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert await controller.steer("hello") == SteerOutcome(False, over=True)
        assert harness.requests("turn/steer") == []
        await harness.engine.close_all()

    run(scenario())


def test_a_turn_that_ended_before_the_steer_arrived_is_told_as_over(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path, "[slow] [end-on-steer] count")
        outcome = await controller.steer("late")
        assert outcome == SteerOutcome(False, over=True), "the server said: no active turn to steer"
        assert steers(controller) == [], "nothing was left behind in the conversation"
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_turn_that_cannot_be_steered_says_why_and_leaves_no_trace(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path, "[slow] [nosteer] count")
        outcome = await controller.steer("hello")
        assert outcome.ok is False and outcome.over is False and outcome.message
        assert steers(controller) == []
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_steer_is_refused_while_the_turn_is_being_stopped(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path)
        controller.transcript.mark_stopping()
        outcome = await controller.steer("hello")
        assert outcome.ok is False and "停止" in outcome.message and harness.requests("turn/steer") == []
        controller.transcript.clear_stopping()
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_steer_carries_pictures_and_files_like_a_message(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path)
        picture = image_attachment("p.png", tiny_png(), "image/png")
        assert (await controller.steer("look", [picture])).ok
        (sent,) = harness.requests("turn/steer")
        text, image = sent["input"]
        assert text["text"] == "look" and image["type"] == "image" and image["url"].startswith("data:image/png;base64,")
        assert base64.b64decode(image["url"].split(",", 1)[1]) == tiny_png()
        (block,) = steers(controller)
        assert [a.name for a in block.attachments] == ["p.png"]
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_pdf_without_words_cannot_be_steered_in_and_nothing_is_sent(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await running(harness, tmp_path)
        scan = pdf_attachment("scan.pdf", b"%PDF-1.4 scan", parsed_text="")
        outcome = await controller.steer("read it", [scan])
        assert outcome.ok is False and "scan.pdf" in outcome.message
        assert harness.requests("turn/steer") == [] and steers(controller) == []
        await controller.interrupt()
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_counts_for_itself_and_each_sub_agent_for_itself(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.usage_series() == {}, "nothing is known before Codex reports"
        assert controller.send_prompt("[subagent-quiet] write the file")
        await until(lambda: controller.agents, timeout=10)
        (agent_id,) = controller.agents
        await until(lambda: not controller.busy, timeout=10)
        series = controller.usage_series()
        assert series[f"agent:{agent_id}"] == Tokens(3000, 1000, None, 1000), "the child's own total, cached input taken out of In"
        assert set(series) <= {"self", f"agent:{agent_id}"}
        await harness.engine.close_all()

    run(scenario())


def test_the_thread_total_is_a_series_with_codexs_numbers(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        turn = await finish_turn(controller, "hello")
        assert turn.status is TurnStatus.DONE
        assert controller.usage_series() == {"self": Tokens(3000, 3000, None, 9000)}
        assert turn.output_tokens == 3000, "what the thread counted while the turn ran: all of it, the first turn of the thread"
        await harness.engine.close_all()

    run(scenario())
