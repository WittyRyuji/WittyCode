"""Durable output references, lossless compaction and bounded summary requests; no real services."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from desktop.core.chatgpt.responses import user_message
from desktop.core.harness import compact, outputs
from desktop.core.harness.outputs import OutputStore, read_output
from desktop.core.harness.tools import ToolContext, ToolError, ToolResult, ToolSpec
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say
from tests.test_harness_loop import Script


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


def test_retention_failure_keeps_the_live_result_but_stops_requests(tmp_path, monkeypatch):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        (project / "evidence.txt").write_text("EVIDENCE\n" * 1000, encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(call("read_file", {"file_path": "evidence.txt"}), say("must not run"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            def full(text):
                raise OSError("demo output storage full")
            monkeypatch.setattr(ctl._outputs, "put", full)
            try:
                ctl.send_prompt("Read evidence.txt")
                await finish_turn(ctl)
                assert len(fake.requests) == 1 and ctl.saved.turns[-1].status == "failed"
                assert any("EVIDENCE" in (tool.result or "") for turn in ctl.transcript.turns for tool in turn.tools.values())
            finally:
                await engine.close_all()
    asyncio.run(go())


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
