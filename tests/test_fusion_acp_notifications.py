"""Guarded-bridge rejection diagnosis from staged ACP tool notifications.

Verified real-CLI shapes (recorded traces in this project): a ``tool_call`` update carries the name and
``rawInput``; a pending call may complete its input on a later ``tool_call_update``; and
``session/request_permission`` repeats only the call id and approval meta, never the raw input.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from desktop.core.devin.policy import CANCELLED
from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.acp_server import MCP_CALL, MCP_LIST, PREFIX, SERVER, hook, tool_label

ONCE = {"outcome": {"outcome": "selected", "optionId": "once"}}


def make_run():
    run = AcpRun(SimpleNamespace())
    run.session, run.active = "private", True
    run.tools = SimpleNamespace(known=lambda tool: tool == "read_file")
    return run


def notify(run, kind="tool_call", **update):
    run._notify("session/update", {"sessionId": run.session, "update": {"sessionUpdate": kind, **update}})


def permit(run, call_id, meta=None, **call_fields):
    call = {"toolCallId": call_id, **call_fields}
    if meta:
        call["_meta"] = meta
    params = {"sessionId": run.session, "toolCall": call, "options": [{"kind": "allow_once", "optionId": "once"}]}
    return asyncio.run(run._request("session/request_permission", params, 0))


def meta(name):
    return {"cognition.ai/inferenceToolName": name}


def test_pending_wrapper_call_completes_before_its_permission_request():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", status="pending", _meta=meta(MCP_CALL))
    assert not run.violation and run._pending == {"mcp_call_tool:0#1": MCP_CALL}
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", status="in_progress",
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert not run.violation and not run._pending
    # The permission request repeats only the id and approval meta; the staged target was already verified.
    assert permit(run, "mcp_call_tool:0#1", meta={"cognition.ai/permissionRule": "MCP"}) == ONCE


def test_pending_wrapper_discovery_call_also_waits_for_its_input():
    run = make_run()
    notify(run, toolCallId="mcp_list_tools:0#1", status="pending", _meta=meta(MCP_LIST))
    assert run._pending == {"mcp_list_tools:0#1": MCP_LIST}
    notify(run, "tool_call_update", toolCallId="mcp_list_tools:0#1", rawInput={"server_name": SERVER})
    assert not run.violation and permit(run, "mcp_list_tools:0#1") == ONCE


def test_explicit_foreign_or_malformed_input_is_judged_at_once():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": "other", "tool_name": "read_file", "arguments": {}})
    assert "mcp_call_tool" in run.violation and "other" not in run.violation and not run._pending
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#2", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": []})
    assert run.violation and not run._pending


def test_native_tool_is_judged_immediately_and_named_without_leaking_input():
    run = make_run()
    notify(run, toolCallId="exec:0#1", rawInput={"command": "echo SECRET_TEXT && rm -rf x"}, _meta=meta("exec"))
    assert "exec" in run.violation and "guarded bridge" in run.violation
    assert "SECRET_TEXT" not in run.violation and "rm -rf" not in run.violation


@pytest.mark.parametrize(("name", "code"), [
    (None, "ACP_TOOL_UNNAMED"),
    ("mcp__unrelated-server__private-tool", "ACP_TOOL_UNSUPPORTED"),
])
def test_refusal_distinguishes_missing_and_unsupported_tool_names_without_leaking(name, code):
    run = make_run()
    notify(run, toolCallId="opaque", title=PREFIX + "read_file", _meta=meta(name) if name else {},
           rawInput={"file_path": "PRIVATE_INPUT"})
    assert run.violation.startswith(f"[{code}]") and "guarded bridge" in run.violation
    assert "PRIVATE_INPUT" not in run.violation and (name is None or name not in run.violation)
    assert not run._seen and not run._pending
    assert permit(run, "opaque", meta=meta(PREFIX + "read_file"), rawInput={}) == CANCELLED


@pytest.mark.parametrize(("name", "raw"), [
    (MCP_LIST, {"server_name": SERVER}),
    (PREFIX + "read_file", {"file_path": "example.py"}),
])
def test_an_opaque_call_id_uses_explicit_tool_metadata(name, raw):
    # A real Fable run gave these calls no tool name prefix in their ids.
    run = make_run()
    notify(run, toolCallId="opaque", _meta=meta(name), rawInput=raw)
    notify(run, "tool_call_update", toolCallId="opaque", _meta=meta(name), status="in_progress")
    notify(run, "tool_call_update", toolCallId="opaque", _meta=meta(name), status="completed")
    assert not run.violation and not run._seen and not run._pending


def test_unregistered_bridge_tool_gets_only_a_fixed_label():
    run = make_run()
    notify(run, toolCallId=PREFIX + "unregistered:0#1", rawInput={})
    assert "a private bridge tool" in run.violation and "guarded bridge" in run.violation
    assert PREFIX + "unregistered" not in run.violation


def test_permission_stays_denied_for_pending_unseen_or_foreign_calls():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#7", status="pending", _meta=meta(MCP_CALL))
    assert permit(run, "mcp_call_tool:0#7") == CANCELLED  # The target is not verified yet.
    assert "mcp_call_tool:0#7" not in run._pending  # A denied call is settled, not still waiting.
    assert permit(run, "mcp_call_tool:9#9") == CANCELLED  # Never announced.
    notify(run, toolCallId="mcp_call_tool:0#8", _meta=meta(MCP_CALL),
           rawInput={"server_name": "other", "tool_name": "x", "arguments": {}})
    assert run.violation and permit(run, "mcp_call_tool:0#8") == CANCELLED


def test_id_only_permission_uses_the_target_verified_on_the_tool_call():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#3", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert permit(run, "mcp_call_tool:0#3") == ONCE


def test_sparse_permission_spends_the_verified_record_once():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert permit(run, "mcp_call_tool:0#1") == ONCE
    assert permit(run, "mcp_call_tool:0#1") == CANCELLED  # A replay may not inherit the spent record.
    assert "mcp_call_tool:0#1" not in run._seen


def test_explicit_permission_input_is_judged_on_its_own_and_must_match_the_record():
    same = {"server_name": SERVER, "tool_name": "read_file", "arguments": {}}
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL), rawInput=same)
    foreign = {"server_name": "other", "tool_name": "read_file", "arguments": {}}
    assert permit(run, "mcp_call_tool:0#1", rawInput=foreign) == CANCELLED
    run = make_run()
    run.tools = SimpleNamespace(known=lambda tool: tool in ("read_file", "create_file"))
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL), rawInput=same)
    other = {"server_name": SERVER, "tool_name": "create_file", "arguments": {}}
    assert permit(run, "mcp_call_tool:0#1", rawInput=other) == CANCELLED  # Allowed target, wrong call.
    run = make_run()
    assert permit(run, "mcp_call_tool:0#1", rawInput=same) == ONCE  # No record: the explicit target is verified.


def test_a_fresh_or_finished_call_never_inherits_the_verified_record():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", status="completed")
    assert permit(run, "mcp_call_tool:0#1") == CANCELLED  # A finished call cannot spend the record.
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    notify(run, toolCallId="mcp_call_tool:0#1", status="pending", _meta=meta(MCP_CALL))  # Re-announced without input.
    assert run._pending == {"mcp_call_tool:0#1": MCP_CALL}
    assert permit(run, "mcp_call_tool:0#1") == CANCELLED  # The fresh report cleared the earlier verification.
    assert not run._pending


def test_only_an_unfinished_wrapper_call_without_input_may_wait():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", status="completed", _meta=meta(MCP_CALL))
    assert run.violation and not run._pending  # Already finished with its input never verified.
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", rawInput="not-a-dict", _meta=meta(MCP_CALL))
    assert run.violation and not run._pending  # A malformed input is judged at once.
    run = make_run()
    run.tool_free = True
    notify(run, toolCallId="mcp_call_tool:0#1", status="pending", _meta=meta(MCP_CALL))
    assert run.violation and not run._pending  # A tool-free turn runs no tools at all.


def test_tool_call_update_alone_never_authorizes_a_call():
    run = make_run()
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#4",
           rawInput={"server_name": "other", "tool_name": "x", "arguments": {}})
    assert not run.violation and permit(run, "mcp_call_tool:0#4") == CANCELLED


def test_hook_refusal_names_the_tool_without_its_input():
    result, code = hook({"hook_event_name": "PreToolUse", "tool_name": "exec",
                         "tool_input": {"command": "echo SECRET_TEXT && rm -rf x"}})
    assert code == 2 and result["decision"] == "block"
    assert "exec" in result["reason"] and "SECRET_TEXT" not in result["reason"]


def test_sparse_permission_must_match_the_verified_wire_identity():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert permit(run, "mcp_call_tool:0#1", meta=meta("exec")) == CANCELLED  # A different asserted name never inherits.
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert permit(run, "mcp_call_tool:0#1", meta=meta(MCP_CALL)) == ONCE


def test_a_sparse_permission_without_any_name_is_looked_up_by_id_alone():
    run = make_run()
    notify(run, toolCallId="opaque", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert permit(run, "opaque") == ONCE  # No explicit name: the recorded identity alone decides.


def test_updates_for_a_verified_call_are_rechecked_not_ignored():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", status="in_progress")  # Pure progress is benign.
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1",
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})  # The same input re-reported.
    assert not run.violation and permit(run, "mcp_call_tool:0#1") == ONCE


def test_a_contradicting_update_for_a_verified_call_is_refused():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", _meta=meta("exec"),
           rawInput={"command": "SECRET_TEXT"})
    assert run.violation and "SECRET_TEXT" not in run.violation
    assert permit(run, "mcp_call_tool:0#1") == CANCELLED
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1",
           rawInput={"server_name": SERVER, "tool_name": "create_file", "arguments": {}})
    assert run.violation  # The announced input changed after verification.


def test_a_pending_call_finished_without_verified_input_is_refused():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", status="pending", _meta=meta(MCP_CALL))
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", status="completed")
    assert run.violation and "mcp_call_tool" in run.violation and "mcp_call_tool:0#1" not in run._pending
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", status="pending", _meta=meta(MCP_CALL))
    notify(run, "tool_call_update", toolCallId="mcp_call_tool:0#1", status="failed")
    assert not run.violation and not run._pending  # An unsuccessful call just grants nothing.


def test_a_first_report_already_finished_produces_no_grant():
    run = make_run()
    notify(run, toolCallId="mcp_call_tool:0#1", status="completed", _meta=meta(MCP_CALL),
           rawInput={"server_name": SERVER, "tool_name": "read_file", "arguments": {}})
    assert not run.violation and "mcp_call_tool:0#1" not in run._seen
    assert permit(run, "mcp_call_tool:0#1") == CANCELLED


def test_refusal_label_names_only_known_tools():
    assert tool_label("exec") == "exec" and tool_label(MCP_CALL) == MCP_CALL
    assert tool_label(PREFIX + "read_file") == tool_label(PREFIX + "SECRET path/x") == "a private bridge tool"
    assert tool_label("mcp__other__run") == tool_label("exec SECRET(x)") == "an unrecognized tool"
    assert tool_label(None) == tool_label(3) == "an unrecognized tool"
    assert len(tool_label("x" * 500)) <= 60
