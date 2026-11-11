"""Read-only product review probes; all writes and Git repositories are disposable.

Run with Python 3.11. No real model, account, external network, or user settings
are used. Findings are observations of bugs, not claims that the product passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import string
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import call_output, user_message
from desktop.core.harness import compact
from desktop.core.harness.permissions import decide, git_runs_own_programs
from desktop.core.harness.shell import SHELL
from desktop.core.harness.store import SavedTurn
from desktop.core.harness.tools import ToolContext, ToolRegistry
from tests.chatgpt_support import finish_turn, until
from tests.fake_responses import FakeResponses, call, calls, say, usage
from tests.test_chatgpt_controller import opened
from tests.test_harness_loop import Script

SOL = "gpt-6.1-sol"
ASTRA = "gpt-6-astra"


def is_summary(request):
    return request["instructions"] == compact.INSTRUCTIONS


def request_size(request):
    return compact.estimate_items(request["input"]) + compact.estimate_text(request["instructions"]) + compact.estimate_text(json.dumps(request.get("tools", [])))


def function_call(cid="old"):
    return {"type": "function_call", "call_id": cid, "name": "read_file", "arguments": '{"file_path":"old.txt"}'}


def assistant(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


async def tail_budget(root):
    engine, ctl = await opened(root, None)
    engine._windows[SOL] = 272_000
    for n in range(6):
        (ctl.project / f"part-{n}.txt").write_text(("\u6587" * 600 + "\n") * 100, encoding="utf-8")
    observed = []
    main_count = 0

    async def stream(request):
        nonlocal main_count
        summary = is_summary(request)
        observed.append({"summary": summary, "estimated_tokens": request_size(request)})
        if summary:
            events = say("The user asked to read six files.")
        else:
            main_count += 1
            if main_count == 1:
                events = calls([("read_file", json.dumps({"file_path": f"part-{n}.txt"}), f"c{n}", f"i{n}") for n in range(6)], used=usage(1000, 200))
            elif request_size(request) > 272_000:
                raise ChatGPTError("context window exceeded", code="context_length_exceeded", status=400)
            else:
                events = say("done")
        async for event in Script(events)(request):
            yield event

    engine.stream = stream
    try:
        ctl.send_prompt("Read the six files and report their contents.")
        await finish_turn(ctl)
        view = compact.view_of(ctl.saved.items, ctl.saved.compaction)
        return {"requests": observed, "turn_status": ctl.transcript.turns[-1].status.value,
                "compaction_before": ctl.saved.compaction.get("before"), "compaction_after": ctl.saved.compaction.get("after"),
                "next_cut": compact.choose_cut(view, int(272_000 * .25)), "window": 272_000}
    finally:
        await engine.close_all()


async def user_constraint(root):
    marker = "USER_CONSTRAINT_DO_NOT_EDIT_BILLING"
    message = "background " * 430 + marker + "\n" + "more detail " * 430
    view = [user_message(message), assistant("old answer " * 2000), user_message("continue")]
    stream = Script(say("Summary of the fragments that were provided."))
    result = await compact.compact(view, stream=stream, model=SOL, reasoning=None, window=4000)
    body = json.dumps(stream.requests[0]["input"])
    return {"source_chars": len(message), "constraint_in_original": marker in message,
            "constraint_reaches_summarizer": marker in body, "source_tokens": compact.estimate_text(message),
            "summary_chunk_budget": compact.chunk_budget(4000), "cut": result.cut, "log_still_has_constraint": marker in str(view)}


async def pasted_summary(root):
    engine, ctl = await opened(root, None)
    # (the whole request has to fit the window since G3: here the instructions alone are about 15K, this repository's AGENTS.md)
    engine._windows[SOL] = 60_000
    ctl.saved.items = [compact.summary_item("A user copied a previous conversation's summary into a new chat."), function_call(), call_output("old", "word " * 26_000)]
    ctl.saved.turns = [SavedTurn(0, 1.0, 2.0, "done", "", SOL)]
    requests = []

    async def stream(request):
        requests.append(request)
        if is_summary(request):
            events = say("The old read completed.")
        else:
            called = {i.get("call_id") for i in request["input"] if i.get("type") == "function_call"}
            orphan = [i.get("call_id") for i in request["input"] if i.get("type") == "function_call_output" and i.get("call_id") not in called]
            if orphan:
                raise ChatGPTError("Output without call: " + str(orphan), code="invalid_request_error", status=400)
            events = say("answer")
        async for event in Script(events)(request):
            yield event

    engine.stream = stream
    try:
        ctl.send_prompt("continue")
        await finish_turn(ctl)
        first_status = ctl.transcript.turns[-1].status.value
        upto = ctl.saved.compaction["upto"]
        engine._windows[SOL] = 400_000
        ctl.send_prompt("next")
        await finish_turn(ctl)
        final = requests[-1]["input"]
        return {"first_turn_status": first_status, "saved_upto": upto, "expected_upto": 3,
                "saved_boundary_type": ctl.saved.items[upto]["type"], "next_request_types": [i["type"] for i in final],
                "second_turn_status": ctl.transcript.turns[-1].status.value}
    finally:
        await engine.close_all()


async def measurement(root):
    engine, ctl = await opened(root, None)
    engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
    script = Script(say("first", used=usage(383_000, 100)), say("second"))
    engine.stream = script
    text = "".join(random.Random(7).choices(string.ascii_letters + string.digits, k=650_000))
    try:
        ctl.send_prompt(text)
        await finish_turn(ctl)
        measured = ctl.context.used
        await ctl.update_settings(model=ASTRA)
        ctl.send_prompt("continue")
        await finish_turn(ctl)
        return {"last_server_measure": measured, "new_window": 272_000, "second_request_estimate": request_size(script.requests[1]),
                "request_kinds": ["summary" if is_summary(r) else "main" for r in script.requests]}
    finally:
        await engine.close_all()


async def switch_mid_turn(root):
    engine, ctl = await opened(root, None)
    engine._windows.update({SOL: 1_000_000, ASTRA: 272_000})
    for n in range(3):
        ctl.saved.turns.append(SavedTurn(len(ctl.saved.items), float(n + 1), float(n + 2), "done", "", SOL))
        ctl.saved.items += [user_message("\u6587" * 50_000), assistant("\u6587" * 50_000)]
    (ctl.project / "large.txt").write_text(("\u6587" * 100 + "\n") * 100, encoding="utf-8")
    # (two summary requests since G4: the user's 50K-character messages are no longer cut before they are summarised)
    script = Script([("hold", "first"), *call("read_file", {"file_path": "large.txt"}, used=usage(320_000, 100))], say("SUMMARY"), say("SUMMARY"), say("ANSWER"))
    engine.stream = script
    try:
        ctl.send_prompt("read the file")
        await until(lambda: "first" in script.gates)
        await ctl.update_settings(model=ASTRA)
        script.gates["first"].set()
        await finish_turn(ctl)
        return {"requests": [{"model": r["model"], "summary": is_summary(r)} for r in script.requests],
                "display_model": ctl.context.model, "display_window": ctl.context.window, "turn_status": ctl.transcript.turns[-1].status.value}
    finally:
        await engine.close_all()


async def cache_state(root):
    from tests.test_app_chatgpt import chatgpt_core, new_chatgpt
    with FakeResponses() as fake:
        core, _, events, engine = await chatgpt_core(root, fake)
        try:
            cid = await new_chatgpt(core, events, root)
            conv = core._by_cid[cid]
            ctl = conv.controller
            core.settings.memory_curate = False
            engine._windows[SOL] = 272_000
            old = {"used": 383_000, "window": 272_000, "model": SOL, "compacted": False}
            conv.cached_meta = {"context": old}
            for n in range(3):
                ctl.saved.turns.append(SavedTurn(len(ctl.saved.items), float(n + 1), float(n + 2), "done", "", SOL))
                ctl.saved.items += [user_message("\u6587" * 50_000), assistant("\u6587" * 50_000)]
            fake.script(say("SUMMARY"), [("hold", "after-summary"), *say("answer")])
            ctl.send_prompt("continue")
            await until(lambda: len(fake.requests) >= 2)
            sent = core._session_meta(conv)["context"]
            result = {"controller_compacted": ctl.context.compacted, "controller_used": ctl.context.used,
                      "page_context": sent, "old_cache_wins": sent == old}
            fake.release("after-summary")
            await finish_turn(ctl)
            return result
        finally:
            fake.release("after-summary")
            await core.shutdown()


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed ({result.returncode}): {result.stderr[:300]}")
    return result.stdout


async def git_default_hook(root):
    git(root, "init", "-q")
    path = root / "a.txt"
    path.write_text("alpha\n", encoding="utf-8")
    git(root, "add", "a.txt")
    hook = root / ".git" / "hooks" / "post-index-change"
    hook.write_text('#!/bin/sh\necho REVIEW_DEFAULT_HOOK > review-marker.txt\n', encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))
    ctx = ToolContext(root)
    decision = decide("code", SHELL, {"command": "git status --short"}, ctx, remembered=set())
    found = git_runs_own_programs(root)
    result = await ToolRegistry.default().run("shell", {"command": "git status --short"}, ctx)
    ran = (root / "review-marker.txt").exists()
    git(root, "config", "core.hooksPath", ".git/hooks")
    explicit = decide("code", SHELL, {"command": "git status --short"}, ctx, remembered=set())
    return {"verdict": decision.verdict, "detected_config": found, "explicit_hooks_path_verdict": explicit.verdict,
            "marker_created": ran, "shell_output": result.output[-350:]}


async def git_tab_config(root):
    git(root, "init", "-q")
    (root / "a.txt").write_text("before\n", encoding="utf-8")
    (root / ".gitattributes").write_text("*.txt diff=review\n", encoding="utf-8")
    git(root, "add", "a.txt", ".gitattributes")
    (root / "a.txt").write_text("after\n", encoding="utf-8")
    (root / "review-helper.sh").write_text('#!/bin/sh\necho REVIEW_TAB_CONFIG > review-marker.txt\ncat "$1"\n', encoding="utf-8", newline="\n")
    config = root / ".git" / "config"
    with config.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write('[diff\t"review"]\n\ttextconv = sh review-helper.sh\n')
    raw_value = git(root, "config", "--get", "diff.review.textconv").strip()
    ctx = ToolContext(root)
    decision = decide("code", SHELL, {"command": "git diff"}, ctx, remembered=set())
    found = git_runs_own_programs(root)
    result = await ToolRegistry.default().run("shell", {"command": "git diff"}, ctx)
    ran = (root / "review-marker.txt").exists()
    config.write_text(config.read_text(encoding="utf-8").replace('[diff\t"review"]', '[diff "review"]'), encoding="utf-8")
    normal = decide("code", SHELL, {"command": "git diff"}, ctx, remembered=set())
    return {"git_reads_value": raw_value, "verdict": decision.verdict, "detected_config": found, "space_header_verdict": normal.verdict,
            "marker_created": ran, "shell_output": result.output[-350:]}


async def git_remote_probe(root):
    (root / ".git").write_text("gitdir: \\\\review.invalid\\share\\repo\n", encoding="utf-8")
    original = Path.read_text
    attempted = []

    def spy(path, *args, **kwargs):
        if str(path).startswith("\\\\review.invalid"):
            attempted.append(str(path))
            raise FileNotFoundError("blocked by the review spy; no network")
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", spy):
        decision = decide("code", SHELL, {"command": "git status"}, ToolContext(root), remembered=set())
    return {"verdict": decision.verdict, "remote_read_attempts_before_verdict": len(attempted), "attempted_names": [Path(p).name for p in attempted], "network_used": False}


CASES = {f.__name__: f for f in (tail_budget, user_constraint, pasted_summary, measurement, switch_mid_turn, cache_state, git_default_hook, git_tab_config, git_remote_probe)}


async def main(names):
    for name in names:
        with tempfile.TemporaryDirectory(prefix="review-20261108-", dir=ROOT / "sandbox") as temp:
            result = await CASES[name](Path(temp))
            print(json.dumps({"case": name, **result}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", metavar="CASE", help="Optional case names: " + ", ".join(CASES))
    args = parser.parse_args()
    unknown = sorted(set(args.cases) - CASES.keys())
    if unknown:
        parser.error("unknown cases: " + ", ".join(unknown))
    asyncio.run(main(args.cases or list(CASES)))
