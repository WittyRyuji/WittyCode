"""Side requests count without changing context, and live block deltas do not resend retained evidence."""
import asyncio
import json
import threading


from desktop.core.chatgpt import http, side
from desktop.core.chatgpt.responses import Usage
from desktop.core.turn_delta import changed_blocks
from tests.chatgpt_support import make_engine
from tests.fake_responses import FakeResponses, say, usage


def test_side_usage_is_recorded_once_after_a_completed_response(tmp_path):
    async def go():
        with FakeResponses() as fake:
            fake.script([*say("Summary", used=usage(130, 20)), ("hold", "eof")])
            engine = make_engine(tmp_path, fake)
            counted = []
            try:
                answer = await side.ask(engine, "demo", on_usage=lambda spent, model: counted.append((spent, model)))
                assert answer == "Summary" and len(counted) == 1
                assert counted[0][0].input_tokens == 130
                engine.record_side_usage(*counted[0])
                ledger = json.loads((engine.root / "side-usage.json").read_text(encoding="utf-8"))
                assert ledger[counted[0][1]]["input"] == 130
            finally:
                fake.release("eof")
                await engine.close_all()
    asyncio.run(go())


def test_title_and_main_usage_are_both_counted_without_changing_context(tmp_path):
    from tests.app_support import ready_core
    from tests.test_app_usage_models import new
    from tests.chatgpt_support import until
    async def go():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            plan.fake.title_turn = say("Demo title", used=usage(120, 10))
            plan.script(say("Verified.", used=usage(100, 10)))
            cid = await new(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "Verify the demo"})
            ctl = core._by_cid[cid].controller
            await until(lambda: ctl.usage is not None and ctl.usage.total == 240)
            assert ctl.context.used == 110
            assert ctl.usage.input_tokens == 220 and ctl.usage.output_tokens == 20
            ctl._save_now()
            saved = engine.sessions.load(ctl.session_id)
            assert saved.usage["input"] == 220 and any(r.get("kind") == "title" for r in saved.requests)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_serial_compaction_still_counts_towards_turn_speed(tmp_path):
    async def go():
        engine = make_engine(tmp_path, None)
        ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
        try:
            ctl._add_usage(Usage(input_tokens=100, output_tokens=20), measures_context=False)
            ctl._add_usage(Usage(input_tokens=100, output_tokens=400), measures_context=False, counts_speed=False)
            ctl._add_usage(Usage(input_tokens=100, output_tokens=10))
            assert ctl._output_so_far() == 30 and ctl.usage.output_tokens == 430
            assert ctl.context.used == 110
        finally:
            await engine.close_all()
    asyncio.run(go())


def test_title_usage_never_enters_main_turn_speed_even_after_reopening(tmp_path):
    from desktop.core.session_state import SessionState
    from tests.app_support import ready_core
    from tests.chatgpt_support import finish_turn, until
    from tests.test_app_usage_models import meta, new
    async def go():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            title = say("Demo title", used=usage(120, 400))
            main = say("Main answer", used=usage(100, 10))
            plan.fake.title_turn = title
            plan.script([("hold", "main"), *main])
            cid = await new(core, events, tmp_path)
            await core.call("session.send", {"id": cid, "text": "Verify the demo"})
            ctl = core._by_cid[cid].controller
            assert ctl.usage.output_tokens == 400 and ctl.context.used is None
            plan.release("main")
            await finish_turn(ctl)
            assert ctl.transcript.turns[-1].output_tokens == 10
            await until(lambda: ctl.usage is not None and ctl.usage.output_tokens == 410)
            assert ctl.context.used == 110
            plan.script(say("Second answer", used=usage(100, 15)))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(ctl)
            assert ctl.transcript.turns[-1].output_tokens == 15 and ctl.usage.output_tokens == 425
            native = ctl.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: ctl.state is SessionState.CLOSED)
            reopened = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: meta(events, reopened).get("mark") == "ready")
            ctl = core._by_cid[reopened].controller
            plan.script(say("After reopening", used=usage(100, 7)))
            await core.call("session.send", {"id": reopened, "text": "Continue again"})
            await finish_turn(ctl)
            assert ctl.transcript.turns[-1].output_tokens == 7 and ctl.usage.output_tokens == 432
        finally:
            plan.release("main")
            plan.release("title")
            await core.shutdown()
    asyncio.run(go())


def test_live_delta_does_not_resend_large_unchanged_output():
    evidence = {"id": "tool", "kind": "tool", "output": "evidence" * 4000}
    before = {"id": "turn", "blocks": [evidence, {"id": "answer", "kind": "text", "markdown": "a"}], "status": "running"}
    after = {**before, "blocks": [evidence, {"id": "answer", "kind": "text", "markdown": "ab"}]}
    delta = changed_blocks(after, before)
    assert len(json.dumps(delta)) < 300
    assert delta["blocks"][0] == {"id": "tool", "kind": "unchanged"}
    assert after["blocks"][0] is evidence and changed_blocks(after, None) is after


def test_a_cache_snapshot_ahead_of_the_page_cannot_supply_delta_references(monkeypatch):
    from types import SimpleNamespace
    from desktop.core import app_core
    current = {"id": "turn", "blocks": [{"id": "tool", "kind": "tool", "output": "new evidence" * 1000}]}
    turn = SimpleNamespace(id="turn", version=2)
    conv = SimpleNamespace(dirty=True, controller=SimpleNamespace(transcript=SimpleNamespace(turns=[turn])),
        preview=False, sent_ids=["turn"], sent_versions={"turn": 1}, json_cache={"turn": (2, current)}, meta={}, cache_dirty=False, switch_task=None)
    emitted = []
    core = SimpleNamespace(_thumb=None, emit=lambda name, payload: emitted.append((name, payload)),
        _mark_cache_dirty=lambda conv: None, _session_meta=lambda conv: {"mark": "ready"}, _note_run=lambda conv: None,
        _notify_session=lambda conv, mark: None, _flush_agent=lambda conv: None, _settle_inflight=lambda conv: None)
    conv.cid = "demo"
    monkeypatch.setattr(app_core, "turn_json", lambda *args: current)
    app_core.AppCore._flush(core, conv)
    assert emitted[0][1]["turns"][0]["blocks"][0]["output"] == "new evidence" * 1000


def test_json_preparation_is_off_the_event_loop(monkeypatch):
    main = threading.get_ident()
    seen = []
    prepare = http._prepare
    def checked(*args, **kwargs):
        seen.append(threading.get_ident())
        return prepare(*args, **kwargs)
    monkeypatch.setattr(http, "_prepare", checked)
    class Client(http.HttpClient):
        def _send(self, request, timeout):
            return http.HttpResponse(200, {}, b"{}")
    asyncio.run(Client().request("POST", "http://localhost/demo", json_body={"demo": "x" * 100000}))
    assert seen and all(thread != main for thread in seen)
