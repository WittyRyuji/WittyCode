"""Durable output references, lossless compaction and bounded summary requests; no real services."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from desktop.core.chatgpt.responses import user_message
from desktop.core.harness import compact, outputs
from desktop.core.harness.outputs import OutputStore, read_output
from desktop.core.harness.tools import ToolContext, ToolError, ToolResult, ToolSpec
from tests.chatgpt_support import finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, calls, say
from tests.test_harness_loop import Script, assert_paired


def test_outputs_are_immutable_scoped_paged_and_checked(tmp_path):
    store = OutputStore(tmp_path / "one")
    text = "HEAD\n" + "a" * 70000 + "MIDDLE EVIDENCE" + "b" * 70000 + "\nTAIL"
    ref = store.put(text)
    assert store.put(text) == ref and store.read(ref) == text
    ctx = ToolContext(project=tmp_path, outputs=store)
    page = read_output({"reference": ref, "offset": 70006, "limit": 15}, ctx)
    assert "MIDDLE EVIDENCE" in page.output
    assert ref in read_output({}, ctx).output
    with pytest.raises(ToolError):
        OutputStore(tmp_path / "two").read(ref)
    for invalid in ("../one/output", "o-../file", "C:/demo/file", "o-" + "a" * 63):
        with pytest.raises(ToolError):
            store.read(invalid)
    (store.root / (ref + ".txt")).write_text("tampered", encoding="utf-8")
    with pytest.raises(ToolError, match="integrity"):
        store.read(ref)


def test_reopening_shares_repeated_output_but_checks_it_again_next_time(tmp_path, monkeypatch):
    store = OutputStore(tmp_path)
    full = "a" * (2 * 1024 * 1024) + "EVIDENCE"
    ref = store.put(full)
    items = [{"type": "function_call_output", "call_id": str(i), "output": "preview", "wc_output_ref": ref} for i in range(12)]
    original_read = store.read
    reads = []
    def counted(reference):
        reads.append(reference)
        return original_read(reference)
    monkeypatch.setattr(store, "read", counted)
    expanded = store.expand(items)
    assert reads == [ref]
    assert all(item["output"] == full + "\n" + outputs.hint(ref) for item in expanded)
    assert all(item["output"] is expanded[0]["output"] for item in expanded)
    assert all(item["output"] == "preview" for item in items)
    (store.root / (ref + ".txt")).write_text("tampered", encoding="utf-8")
    with pytest.raises(ToolError, match="integrity"):
        store.expand(items)
    assert reads == [ref, ref], "the cache must not survive a later reopen"
    damaged = store.expand(items, strict=False)
    assert all("unavailable or damaged" in item["output"] for item in damaged)
    assert reads == [ref, ref, ref], "a damaged reference is checked only once per reopen"
    invalid = [{"type": "function_call_output", "output": "preview", "wc_output_ref": ["bad"]}]
    assert "unavailable or damaged" in store.expand(invalid, strict=False)[0]["output"]
    with pytest.raises(ToolError):
        store.expand(invalid)


def test_quota_does_not_prune_evidence(tmp_path, monkeypatch):
    store = OutputStore(tmp_path)
    ref = store.put("retained evidence")
    monkeypatch.setattr(outputs, "MAX_SESSION_BYTES", 20)
    with pytest.raises(OSError, match="quota"):
        store.put("another long result")
    assert store.read(ref) == "retained evidence"
    monkeypatch.setattr(outputs, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(OSError, match="retention limit"):
        store.put("x" * 11)


def test_symlinks_cannot_read_other_sessions(tmp_path):
    target = tmp_path / "target"
    target.write_text("evidence", encoding="utf-8")
    store = OutputStore(tmp_path / "store")
    ref = store.put("evidence")
    path = store.root / (ref + ".txt")
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ToolError, match="links"):
        store.read(ref)


def test_long_result_survives_model_preview_and_reopening(tmp_path):
    async def go():
        full = "HEAD\n" + "a" * 70000 + "MIDDLE EVIDENCE" + "b" * 70000 + "\nTAIL"
        with FakeResponses() as fake:
            fake.script(call("demo_output", {}), say("Done."))
            engine = make_engine(tmp_path, fake)
            engine.registry.register(ToolSpec("demo_output", "demo", {"type": "object", "properties": {}}, "Demo", "read"), lambda args, ctx: ToolResult(full), ui_input=dict)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            from desktop.core.harness.loop import Verdict
            tools = await ctl._ensure_tools()
            tools.check = lambda call: Verdict("allow")
            ctl.send_prompt("Inspect demo output")
            await finish_turn(ctl)
            item = next(i for i in ctl.saved.items if i.get("type") == "function_call_output")
            ref = item["wc_output_ref"]
            assert ctl._outputs.read(ref) == full and len(item["output"]) < len(full)
            assert "read_output" in item["output"]
            assert "wc_output_ref" not in str(fake.requests)
            sid = ctl.session_id
            await ctl.close()
            reopened = await engine.open_saved(tmp_path / "project", sid)
            assert reopened._outputs.read(ref) == full
            assert any("MIDDLE EVIDENCE" in (tool.result or "") for turn in reopened.transcript.turns for tool in turn.tools.values())
            await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("window", [4096, 8192])
def test_summary_reads_middle_evidence_and_every_request_fits(tmp_path, window):
    async def go():
        store = OutputStore(tmp_path)
        full = "start " + "x" * 19000 + "MIDDLE EVIDENCE" + "y" * 19000 + " end"
        ref = store.put(full)
        item = {"type": "function_call_output", "call_id": "demo", "output": "preview", "wc_output_ref": ref}
        history = [user_message("Inspect evidence"), {"type": "function_call", "name": "demo", "call_id": "demo", "arguments": "{}"}, item, user_message("next")]
        script = Script(*[say("A checked running summary.") for _ in range(30)])
        result = await compact.compact(history, stream=script, model="demo", reasoning=None, window=window,
            output_reader=lambda reference: asyncio.to_thread(store.read, reference))
        assert result and not result.mechanical and "read_output" in result.summary
        assert any("MIDDLE EVIDENCE" in str(request) for request in script.requests)
        assert len(script.requests) > 1
        for request in script.requests:
            assert compact.fixed_tokens(request["instructions"], []) + compact.estimate_items(request["input"]) <= compact.budget(window)
        assert history[2]["output"] == "preview"
    asyncio.run(go())


# Patch OutputStore.put rather than the controller's initial store: the first checkpoint
# replaces that instance with the archive's metered store, which also delegates to OutputStore.put.
def test_retention_failure_keeps_the_live_result_but_stops_requests(tmp_path, monkeypatch):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        (project / "evidence.txt").write_text("EVIDENCE\n" * 1000, encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(call("read_file", {"file_path": "evidence.txt"}), say("Continued."))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            original_put = OutputStore.put
            attempts = []
            def full(store, text):
                attempts.append(text)
                raise OSError("demo output storage full")
            monkeypatch.setattr(OutputStore, "put", full)
            try:
                ctl.send_prompt("Read evidence.txt")
                await finish_turn(ctl)
                assert len(fake.requests) == 1 and ctl.saved.turns[-1].status == "failed"
                assert len(attempts) == 1, "cleanup must not retry the failed retention"
                assert any("EVIDENCE" in (tool.result or "") for turn in ctl.transcript.turns for tool in turn.tools.values())
                assert_paired(ctl.saved.items)
                result = next(item["output"] for item in ctl.saved.items if item.get("type") == "function_call_output")
                assert "EVIDENCE" in result and "not" in result.lower() and "retain" in result.lower()
                assert_paired(engine.sessions.load(ctl.session_id).items)
                monkeypatch.setattr(OutputStore, "put", original_put)
                ctl.send_prompt("Storage is available; continue")
                await finish_turn(ctl)
                assert len(fake.requests) == 2
                assert_paired(fake.requests[-1]["input"])
                assert ctl.saved.turns[-1].status == "done"
            finally:
                await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("fail_names", [{"first", "second", "third"}, {"first", "third"}])
def test_parallel_retention_failures_pair_every_completed_result(tmp_path, monkeypatch, fail_names):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        for name in ("first", "second", "third"):
            (project / f"{name}.txt").write_text((name + " evidence\n") * 500, encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(calls([("read_file", json.dumps({"file_path": f"{name}.txt"}), name, "f-" + name)
                               for name in ("first", "second", "third")]))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            original_put = OutputStore.put
            attempts = []
            def full(store, text):
                name = next(name for name in ("first", "second", "third") if name + " evidence" in text)
                attempts.append(name)
                if name in fail_names:
                    raise OSError("demo output storage full")
                return original_put(store, text)
            monkeypatch.setattr(OutputStore, "put", full)
            try:
                ctl.send_prompt("Read all three files")
                await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "failed" and len(fake.requests) == 1
                assert attempts == ["first", "second", "third"], "each completed result gets at most one retention attempt"
                assert_paired(ctl.saved.items)
                results = {item["call_id"]: item for item in ctl.saved.items if item.get("type") == "function_call_output"}
                assert set(results) == {"first", "second", "third"}
                assert all(name + " evidence" in item["output"] and "retain" in item["output"].lower() for name, item in results.items())
                for name, item in results.items():
                    assert ("wc_output_ref" in item) == (name not in fail_names)
                assert_paired(engine.sessions.load(ctl.session_id).items)
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_retention_failure_does_not_repeat_a_completed_write(tmp_path, monkeypatch):
    from desktop.core.harness.loop import Verdict
    async def go():
        with FakeResponses() as fake:
            fake.script(call("demo_write", {}), say("Continued."))
            engine = make_engine(tmp_path, fake)
            writes = []
            def write(args, ctx):
                writes.append("done")
                return ToolResult("WRITE COMPLETED\n" + "x" * 2000)
            engine.registry.register(ToolSpec("demo_write", "demo", {"type": "object", "properties": {}}, "Demo", "write"), write, ui_input=dict)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            tools = await ctl._ensure_tools()
            tools.check = lambda call: Verdict("allow")
            original_put = OutputStore.put
            def full(store, text):
                raise OSError("demo output storage full")
            monkeypatch.setattr(OutputStore, "put", full)
            try:
                ctl.send_prompt("Make one change")
                await finish_turn(ctl)
                assert writes == ["done"] and ctl.saved.turns[-1].status == "failed"
                assert_paired(ctl.saved.items)
                result = next(item["output"] for item in ctl.saved.items if item.get("type") == "function_call_output")
                assert "WRITE COMPLETED" in result and "before repeating" in result
                monkeypatch.setattr(OutputStore, "put", original_put)
                ctl.send_prompt("Check before continuing")
                await finish_turn(ctl)
                assert writes == ["done"] and ctl.saved.turns[-1].status == "done"
                assert_paired(fake.requests[-1]["input"])
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_cancel_during_retention_keeps_the_completed_reference(tmp_path, monkeypatch):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        (project / "evidence.txt").write_text("EVIDENCE\n" * 1000, encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(call("read_file", {"file_path": "evidence.txt"}))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            original_put = OutputStore.put
            started, release = threading.Event(), threading.Event()
            attempts = []
            def held(store, text):
                attempts.append(text)
                started.set()
                if not release.wait(10):
                    raise OSError("demo retention timed out")
                return original_put(store, text)
            monkeypatch.setattr(OutputStore, "put", held)
            try:
                ctl.send_prompt("Read evidence.txt")
                await until(started.is_set)
                interrupt = asyncio.create_task(ctl.interrupt())
                await until(lambda: ctl._turn_task.cancelling() > 0)
                release.set()
                await interrupt
                assert len(attempts) == 1
                assert_paired(ctl.saved.items)
                result = next(item for item in ctl.saved.items if item.get("type") == "function_call_output")
                assert "EVIDENCE" in ctl._outputs.read(result["wc_output_ref"])
                assert "read_output" in result["output"]
            finally:
                release.set()
                await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("legacy_preview", [False, True])
def test_output_preview_is_stable_across_repeated_save_and_load(tmp_path, legacy_preview):
    from desktop.core.harness.loop import clip_output
    from desktop.core.harness.store import SessionStore, SavedSession
    store = SessionStore(tmp_path)
    session = SavedSession(id="a" * 32, project="demo")
    output_store = store.outputs(session.id)
    full = "HEAD\n" + "x" * 10000 + "\nFAILED CHECK\nExit code: 1"
    reference = output_store.put(full)
    note = outputs.hint(reference)
    text = full + "\n" + note
    if legacy_preview:
        text = clip_output(text, outputs.RETAIN_CHARS) + "\n" + note
    session.items = [{"type": "function_call_output", "call_id": "demo", "output": text, "wc_output_ref": reference}]
    previews = []
    for _ in range(4):
        store.save(session)
        session = store.load(session.id)
        previews.append(session.items[0]["output"])
    assert len(set(previews)) == 1
    assert "FAILED CHECK" in previews[0] and "Exit code: 1" in previews[0]
    assert previews[0].count(note) == 1 and len(previews[0]) < 1600
    assert output_store.read(reference) == full


def test_deleting_a_session_removes_only_its_retained_outputs(tmp_path):
    from desktop.core.harness.store import SessionStore, SavedSession
    store = SessionStore(tmp_path)
    session = SavedSession(id="a" * 32, project=str(tmp_path))
    store.save(session)
    output = store.outputs(session.id)
    ref = output.put("evidence")
    other = store.outputs("b" * 32)
    other_ref = other.put("other evidence")
    store.delete(session.id)
    assert store.load(session.id) is None and not output.root.exists()
    assert other.read(other_ref) == "other evidence"


def test_an_oversized_previous_summary_sends_no_request():
    async def go():
        script = Script(say("not sent"))
        with pytest.raises(compact.CompactionFailed, match="previous summary"):
            await compact.summarise(script, "demo", None, "x" * 30000, ["evidence"], 4000, window=4096)
        assert script.requests == []
    asyncio.run(go())


def test_shell_retention_keeps_the_middle_before_model_clipping(tmp_path):
    from desktop.core.harness.shell import run_shell
    async def go():
        output, code, timed_out, aborted = await run_shell("Write-Output ('a' * 70000 + 'MIDDLE EVIDENCE' + 'b' * 70000)", tmp_path, 30, retain=True)
        assert code == 0 and not timed_out and not aborted
        assert len(output) > 140000 and "MIDDLE EVIDENCE" in output
    asyncio.run(go())
