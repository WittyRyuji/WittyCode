"""Sub-agents of a Codex thread: shown, followed, and asked about like the thread itself (fake app-server)."""

from __future__ import annotations

import pytest

from desktop.core.codex.controller import CodexAgent
from desktop.core.session_manager import SessionState
from desktop.core.transcript import PromptStatus, TextBlockView, ToolStatus, TurnStatus
from tests.codex_support import make_engine, run, until


async def opened(harness, project):
    controller = await harness.engine.open_new(project)
    assert controller.state is SessionState.READY, controller.last_error
    return controller


def test_a_sub_agent_that_the_thread_starts_is_followed_to_the_end(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[subagent-quiet] write the file")
        await until(lambda: controller.agents, timeout=10)
        (agent,) = controller.agents.values()
        assert isinstance(agent, CodexAgent) and agent.parent is controller and agent.path == "/root/coder_write_sub"
        assert agent.agent_model == "gpt-6.1-sol" and agent.agent_effort == "max"
        await until(lambda: not controller.busy, timeout=10)
        assert agent.status == "done" and agent.finished_at_s is not None
        (turn,) = agent.transcript.turns
        assert turn.status is TurnStatus.DONE
        assert turn.tools["cmd-child"].status is ToolStatus.DONE
        texts = [b.text for b in turn.blocks if isinstance(b, TextBlockView)]
        assert texts == ["Creating the file.", "done"]
        assert turn.user_block is not None and turn.user_block.who == "主智能体"
        # who it is and what it was asked come from the thread itself
        await until(lambda: agent.nickname == "Dirac", timeout=5)
        assert agent.role == "coder" and agent.task == "Write sub.txt with the word hi" and agent.name == "Dirac"
        assert turn.user_block.text == "Write sub.txt with the word hi"
        parent_text = [b.text for b in controller.transcript.turns[-1].blocks if isinstance(b, TextBlockView)]
        assert parent_text[-1] == "all-done (accept)"
        assert [t for t in controller.transcript.turns[-1].tools.values() if t.name == "Task"], "the thread's own wait is still a Task"
        await harness.engine.close_all()

    run(scenario())


@pytest.mark.parametrize(("value", "decision", "status"), [("proceed_once", "accept", "done"), ("proceed_always", "acceptForSession", "done"), ("cancel", "decline", "stopped")])
def test_what_a_sub_agent_asks_is_asked_of_the_user_and_the_answer_reaches_it(tmp_path, value, decision, status):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[subagent] write the file")
        await until(lambda: controller.agent_prompts(), timeout=10)
        ((agent, prompt),) = controller.agent_prompts()
        assert agent.status == "waiting" and controller.working_label == "子任务等待你的确认"
        assert not controller.transcript.pending_prompts(), "the thread's own transcript has nothing pending: it is the agent's"
        (action,) = prompt.actions
        assert action.kind == "execute" and action.command == "Set-Content -Path sub.txt -Value hi" and action.reason == "May I create sub.txt containing hi?"
        assert [o.value for o in prompt.options] == ["proceed_once", "proceed_always", "cancel"]
        assert not controller.answer_agent_permission("nobody", prompt.id, value), "an unknown agent is refused"
        assert controller.answer_agent_permission(agent.session_id, prompt.id, value)
        assert not controller.answer_agent_permission(agent.session_id, prompt.id, value), "a second click is refused"
        await until(lambda: not controller.busy, timeout=10)
        answers = [m["result"]["decision"] for m in harness.answers() if "result" in m and "decision" in (m["result"] or {})]
        assert answers == [decision], "the agent's request was answered with the user's choice, not silently declined"
        assert agent.status == status and prompt.status is PromptStatus.ANSWERED
        assert agent._pending == {} and not controller.agent_prompts()
        await harness.engine.close_all()

    run(scenario())


def test_the_thread_closing_does_not_leave_a_sub_agent_waiting(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("[subagent] write the file")
        await until(lambda: controller.agent_prompts(), timeout=10)
        ((agent, prompt),) = controller.agent_prompts()
        await controller.close()
        assert prompt.status is not PromptStatus.PENDING and agent._pending == {}
        assert agent.session_id not in harness.engine._threads, "its notifications are no longer routed to anyone"
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_without_sub_agents_has_none(tmp_path):
    async def scenario():
        harness = make_engine(tmp_path)
        controller = await opened(harness, tmp_path)
        assert controller.send_prompt("hello")
        await until(lambda: not controller.busy, timeout=10)
        assert controller.agents == {} and controller.agent_prompts() == []
        await harness.engine.close_all()

    run(scenario())
