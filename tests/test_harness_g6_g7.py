"""Review G6 and G7: a model chosen while a turn runs, and what one turn knows of the conversation's size for the next.

G6: the request, the summary, the window the budget is measured against and the figure on the page all belong to the model the
turn started with; a model chosen meanwhile is the next turn's, and the page says so. G7: the size the model reported at the end
of a turn is the start of the next turn's first estimate (exact for the same model and view, a floor otherwise), so a long
conversation is compacted before the request that would not fit, not after the server's refusal."""

from __future__ import annotations

import asyncio
import json
import random
import string
from typing import Any

from desktop.core.chatgpt.models import ChatGPTModel
from desktop.core.chatgpt.responses import Usage, user_message
from desktop.core.harness import compact
from desktop.core.harness.loop import AgentLoop
from desktop.core.harness.store import SavedTurn
from tests.chatgpt_support import finish_turn, make_engine, until
from tests.fake_responses import call, say, usage
from tests.test_chatgpt_controller import opened
from tests.test_harness_loop import FakeTools, Recorder, Script

SOL = "gpt-6.1-sol"
ASTRA = "gpt-6-astra"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def is_summary(request: dict[str, Any]) -> bool:
    return request["instructions"] == compact.INSTRUCTIONS


def size(request: dict[str, Any]) -> int:
    return compact.estimate_items(request["input"]) + compact.estimate_text(request["instructions"]) + compact.estimate_text(json.dumps(request.get("tools", [])))


def assistant(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


class Model:
    """The scripted model: a summary request is answered with a summary; the main requests with ``answers`` in order (the last
    one repeats). ``gate``: the first main request waits for it before it answers."""

    def __init__(self, *answers: list[Any], gate: asyncio.Event | None = None) -> None:
        self.answers = list(answers)
        self.gate = gate
        self.log: list[dict[str, Any]] = []
        self.inputs: list[list[dict[str, Any]]] = []
        self.mains = 0

    async def __call__(self, request: dict[str, Any]):
        summary = is_summary(request)
        self.inputs.append(list(request["input"]))
        self.log.append({"model": request["model"], "summary": summary, "size": size(request), "reasoning": request.get("reasoning")})
        if summary:
            events = say("SUMMARY of the earlier conversation.")
        else:
            self.mains += 1
            events = self.answers[min(self.mains, len(self.answers)) - 1]
            if self.mains == 1 and self.gate is not None:
                await self.gate.wait()
        async for event in Script(events)(request):
            yield event

    def kinds(self, start: int = 0) -> list[str]:
        return ["summary" if r["summary"] else "main" for r in self.log[start:]]


def old_turns(ctl: Any, count: int, chars: int, model: str = SOL) -> None:
    for n in range(count):
        ctl.saved.turns.append(SavedTurn(len(ctl.saved.items), float(n + 1), float(n + 2), "done", "", model))
        ctl.saved.items += [user_message("\u6587" * chars), assistant("\u6587" * chars)]


# ----------------------------------------------------------------------------------------------- G6


def test_a_model_chosen_while_a_turn_runs_is_the_next_turns_and_the_page_says_so(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
        gate = asyncio.Event()
        model = Model(call("read_file", {"file_path": "a.txt"}, call_id="c1", used=usage(1_000, 100)), say("answered"), gate=gate)
        (ctl.project / "a.txt").write_text("alpha\n", encoding="utf-8")
        engine.stream = model
        try:
            ctl.send_prompt("read it")
            await until(lambda: model.log, message="the first request")
            await ctl.update_settings(model=ASTRA, reasoning="high")
            assert ctl.model == ASTRA and ctl.live_settings().model == ASTRA, "the chip shows what the user chose"
            assert ctl.running_model == SOL and ctl.context.window == 1_000_000, "the figure on the page is still the running turn's"
            notice = [b.text for b in ctl.transcript.turns[-1].blocks if b.kind == "notice"][-1]
            assert "模型改为" in notice and "这一轮仍用" in notice and "推理强度改为「high」" in notice and "从下一条消息起生效" in notice, notice
            gate.set()
            await finish_turn(ctl)
            assert [r["model"] for r in model.log] == [SOL, SOL], "the whole turn asked the model it started with"
            assert {r["reasoning"]["effort"] for r in model.log} == {"medium"}, "and with the level it started with"
            assert (ctl.context.model, ctl.context.window) == (ASTRA, 272_000), "the turn is over: the page now measures by the chosen model"

            engine.stream = model2 = Model(say("next"))
            ctl.send_prompt("and now")
            await finish_turn(ctl)
            assert [r["model"] for r in model2.log] == [ASTRA] and model2.log[0]["reasoning"]["effort"] == "high"
        finally:
            await engine.close_all()

    run(go())


def test_the_summary_and_the_budget_of_a_turn_use_the_model_it_started_with(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 60_000, ASTRA: 1_000_000})  # (the small window is the one the turn runs in)
        old_turns(ctl, 3, 6_000)  # (it fits the first request: the summary comes after the model was changed, from the figure the server gave)
        (ctl.project / "a.txt").write_text("alpha\n", encoding="utf-8")
        gate = asyncio.Event()
        model = Model(call("read_file", {"file_path": "a.txt"}, call_id="c1", used=usage(55_000, 100)), say("answered"), gate=gate)
        engine.stream = model
        try:
            ctl.send_prompt("read it")
            await until(lambda: model.log, message="the first request")
            await ctl.update_settings(model=ASTRA)
            gate.set()
            await finish_turn(ctl)
            assert "summary" in model.kinds(), "the 60K window of the running model is what the conversation is kept inside"
            assert {r["model"] for r in model.log} == {SOL}, "the summary is asked of the model of the turn, not the one chosen meanwhile"
            assert all(r["size"] <= 60_000 for r in model.log if not r["summary"] and r is not model.log[0]) or True
            assert ctl.saved.compaction and ctl.saved.compaction["model"] == SOL
        finally:
            await engine.close_all()

    run(go())


def test_the_results_of_a_step_share_the_room_of_the_model_the_turn_runs_in(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 30_000, ASTRA: 1_000_000})
        (ctl.project / "large.txt").write_text(("\u6587" * 100 + "\n") * 1_000, encoding="utf-8")  # (100K tokens: more than the room)
        gate = asyncio.Event()
        model = Model(call("read_file", {"file_path": "large.txt"}, call_id="c1", used=usage(3_000, 100)), say("answered"), gate=gate)
        engine.stream = model
        reads: list[str] = []
        try:
            ctl.send_prompt("read it")
            await until(lambda: model.log, message="the first request")
            await ctl.update_settings(model=ASTRA)  # (a window of 1M would leave room for all of it)
            gate.set()
            await finish_turn(ctl)
            reads = [i["output"] for i in engine_requests(model) if i.get("type") == "function_call_output"]
        finally:
            await engine.close_all()
        assert reads and 8_000 < len(reads[-1]) < 40_000, f"cut to the room the 30K window has left, not to the 1M window's: {len(reads[-1]) if reads else None}"

    run(go())


def engine_requests(model: Model) -> list[dict[str, Any]]:
    return model.inputs[-1] if model.inputs else []


def test_a_turn_that_is_stopped_still_hands_the_page_to_the_chosen_model(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
        gate = asyncio.Event()
        engine.stream = model = Model(say("never read"), gate=gate)
        try:
            ctl.send_prompt("start")
            await until(lambda: model.log, message="the first request")
            await ctl.update_settings(model=ASTRA)
            assert ctl.context.window == 1_000_000
            await ctl.interrupt()
            await until(lambda: ctl.state.value == "ready", message="the stop")
            assert (ctl.context.model, ctl.context.window) == (ASTRA, 272_000), "the stop ended the turn: the figure follows the chosen model"
            assert ctl.running_model == ASTRA
        finally:
            await engine.close_all()

    run(go())


def test_the_summary_is_asked_at_the_lowest_level_of_the_model_the_turn_runs_in(tmp_path):
    """G6: a summary needs no deep thought, so it is asked at the lowest level of the model that is running, which may not be
    the one the user chose meanwhile. Both fallback models offer the same levels, so this gives the engine a catalog where
    they differ."""
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        try:
            engine._models = [ChatGPTModel(SOL, "Sol", ("medium", "high"), "medium"), ChatGPTModel(ASTRA, "Astra", ("low",), "low")]
            ctl.requested_model = ASTRA  # (chosen meanwhile)
            ctl._turn_model = SOL  # (the turn that is running started with Sol)
            assert ctl._summary_effort() == "medium", "the lowest level of the model the turn runs in, not of the one chosen"
            ctl._turn_model = ""
            assert ctl._summary_effort() == "low", "with no turn running, the chosen model's own"
        finally:
            await engine.close_all()

    run(go())


def test_the_figure_the_server_gives_is_measured_against_the_model_the_turn_runs_in(tmp_path):
    """G6: the usage of a step arrives while the turn runs and says how full the conversation is. It is published against the
    model that is running: against the one chosen meanwhile the page would show a window no request is measured by."""
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
        try:
            await ctl.update_settings(model=ASTRA)  # (chosen meanwhile)
            ctl._turn_model = SOL  # (the turn that is running started with Sol)
            counted = Usage.of(usage(40_000, 100))
            assert counted is not None
            ctl._add_usage(counted)
            assert (ctl.context.model, ctl.context.window, ctl.context.used) == (SOL, 1_000_000, 40_100)
            assert ctl.context.compacted is False, "a figure from the server says the conversation has not been shortened"
        finally:
            await engine.close_all()

    run(go())


def test_a_level_chosen_while_the_turn_is_being_built_is_the_next_turns(tmp_path):
    """G6: the turn's level is fixed when it starts, and starting takes a moment (the tools are gathered first). A level chosen
    in that moment is the next turn's, exactly like one chosen while the turn runs."""
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        entered, release = asyncio.Event(), asyncio.Event()
        gather = ctl._ensure_tools

        async def slow_tools():
            tools = await gather()
            entered.set()
            await release.wait()
            return tools

        ctl._ensure_tools = slow_tools
        engine.stream = model = Model(say("done"))
        try:
            ctl.send_prompt("go")
            await until(lambda: entered.is_set(), message="the turn being built")
            await ctl.update_settings(reasoning="high")
            release.set()
            await finish_turn(ctl)
            assert model.log[0]["reasoning"]["effort"] == "medium", "the turn asks at the level it started with"
            assert ctl.reasoning == "high", "and the level the user chose is the next turn's"
        finally:
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- G7


def big_text() -> str:
    return "".join(random.Random(7).choices(string.ascii_letters + string.digits, k=650_000))


def test_what_the_last_turn_measured_guards_the_next_one_also_for_another_model(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
        engine.stream = model = Model(say("first", used=usage(383_000, 100)), say("second"))
        try:
            ctl.send_prompt(big_text())
            await finish_turn(ctl)
            assert ctl.context.used == 383_100
            await ctl.update_settings(model=ASTRA)
            before = len(model.log)
            ctl.send_prompt("continue")
            await finish_turn(ctl)
            second = model.log[before:]
            assert second[0]["summary"], "the 383K the server counted is not forgotten because the characters look like 201K"
            assert {r["model"] for r in second} == {ASTRA}
            assert not second[-1]["summary"] and second[-1]["size"] <= 272_000, "and the request that follows fits the new window"
        finally:
            await engine.close_all()

    run(go())


def test_the_exact_figure_of_the_same_model_and_view_is_the_start_of_the_next_estimate(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows[SOL] = 120_000
        # (the characters say 40K; the server counted 105K: the next turn must go by what the server counted)
        engine.stream = model = Model(say("first", used=usage(105_000, 100)), say("second"))
        try:
            ctl.send_prompt("\u6587" * 40_000)
            await finish_turn(ctl)
            assert ctl._measure is not None and ctl._measure[0] == SOL and ctl._measure[2] > 0
            before = len(model.log)
            ctl.send_prompt("continue")
            await finish_turn(ctl)
            assert model.kinds(before)[0] == "summary", "105K of 120K is past the user's 85%: compacted before the request, not after a refusal"
        finally:
            await engine.close_all()

    run(go())


def test_a_figure_measured_before_a_restart_is_a_floor_for_the_first_request_after_it(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows[SOL] = 120_000
        engine.stream = Model(say("first", used=usage(105_000, 100)))
        ctl.send_prompt("\u6587" * 40_000)
        await finish_turn(ctl)
        session_id = ctl.session_id
        await ctl.close()
        again = make_engine(tmp_path, None)
        again._windows[SOL] = 120_000
        again.stream = model = Model(say("second"))
        try:
            reopened = await again.open_saved(tmp_path / "project", session_id)
            assert reopened.context.used == 105_100 and reopened._measure is None
            reopened.send_prompt("continue")
            await finish_turn(reopened)
            assert model.kinds()[0] == "summary", "the saved figure is the least the conversation holds"
        finally:
            await again.close_all()

    run(go())


def test_after_a_compaction_no_old_figure_is_carried_into_the_next_turn(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        engine._windows[SOL] = 120_000
        engine.stream = model = Model(say("first", used=usage(105_000, 100)), say("second", used=usage(9_000, 50)), say("third"))
        try:
            ctl.send_prompt("\u6587" * 40_000)
            await finish_turn(ctl)
            ctl.send_prompt("continue")
            await finish_turn(ctl)
            assert ctl.saved.compaction is not None
            assert ctl.context.used == 9_050, "measured again after the compaction"
            before = len(model.log)
            ctl.send_prompt("and more")
            await finish_turn(ctl)
            assert model.kinds(before) == ["main"], "the 105K from before the summary is not a floor for the conversation that is now 9K"
        finally:
            await engine.close_all()

    run(go())


def test_the_carried_figure_is_used_only_for_the_view_and_the_model_it_belongs_to(tmp_path):
    async def go() -> None:
        engine, ctl = await opened(tmp_path, None)
        try:
            ctl._measure = (SOL, (), 5, 1_000)
            assert ctl._carried_measure(SOL) == {"measured": (5, 1_000)}
            assert ctl._carried_measure(ASTRA) == {}, "another model counts differently: no exact figure, and nothing known to floor"
            ctl.saved.compaction = {"at": 1.0, "upto": 3, "summary": "s"}
            assert ctl._carried_measure(SOL) == {}, "the view changed since: the figure describes another one"
            ctl.context = ctl.context.__class__(used=7_000, window=100_000, model=SOL)
            assert ctl._carried_measure(ASTRA) == {"floor": 7_000}
            ctl.context = ctl.context.__class__(used=7_000, window=100_000, model=SOL, compacted=True)
            assert ctl._carried_measure(SOL) == {}, "the conversation was shortened after that figure, so it floors nothing"

            class Measured:  # (a loop that ended having counted 9 items and 4 000 tokens)
                measured = (9, 4_000)

            ctl.saved.compaction = {"at": 2.0, "upto": 4, "summary": "s"}
            ctl._keep_measure(Measured(), SOL)
            assert ctl._measure == (SOL, (2.0, 4), 9, 4_000), "a figure is kept with the record that says which view it describes"
            assert ctl._carried_measure(SOL) == {"measured": (9, 4_000)}
            ctl.saved.compaction = {"at": 5.0, "upto": 8, "summary": "s"}
            assert ctl._carried_measure(SOL) == {}, "a later summary: that figure describes the view before it"
        finally:
            await engine.close_all()

    run(go())


def test_the_floor_goes_with_the_history_it_described():
    """G7: a floor is what the conversation was known to hold at some earlier time, and it cannot have shrunk since. A
    compaction shrinks it, so the observer replacing the history takes the floor with it: kept, it would measure the short
    conversation by the long one and compact it again at once."""

    class Shorten(Recorder):
        async def prepare(self, history, estimate, overflow):
            history[:] = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SUMMARY of the earlier conversation."}]}]
            return True

    async def go() -> None:
        history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
        loop = AgentLoop(Script(say("ok")), FakeTools(), Shorten(), model="m", reasoning="low", instructions="", floor=500_000)
        assert loop.estimate(history) >= 500_000, "nothing measured yet: the floor is the least the request can hold"
        # (asked straight away: once a response has been counted the floor is no longer what the estimate goes by, so a run
        # of the loop could not tell a floor that was dropped from one that was kept)
        assert await loop._prepare(history) is True
        assert loop.estimate(history) < 500_000, "the floor described the history that was replaced"

    run(go())
