"""Spike (items 6 + 3 leftovers): one turn covering
  - a Read of a nonexistent file (failing-tool shape; ToolResult is_error)
  - a two-file edit in one step (Edit x2; permission request + result shapes,
    per-file diff reconstruction)
  - an AskUser whose question handler raises (fallback cancelled + ErrorEvent)

ApplyPatch is not offered for claude-haiku-5-5 sessions (fixtures/tools.json has
no ApplyPatch entry), so two Edit calls stand in; the prompt still invites
ApplyPatch in case the tool appears.

Writes fixtures/robust/multiedit.jsonl (+ .raw.jsonl).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    QuestionRequest,
    QuestionResponse,
    Session,
    SessionConfig,
)

from _common import (
    DEFAULT_MODEL,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    preview,
    reasoning_effort,
    run_turn,
    setup_console,
)
from _robust_common import ROBUST_FIXTURES, ROBUST_SANDBOX

CWD = ROBUST_SANDBOX / "multiedit"

PROMPT = (
    "Do these steps in order:\n"
    "1. Read the file notes-a.txt and the file notes-b.txt.\n"
    "2. Read the file missing-notes.txt — it does not exist; note the error and continue anyway.\n"
    "3. In one step, apply BOTH of these edits (use the Edit tool twice, or the ApplyPatch tool "
    "once if you have it): in notes-a.txt replace 'alpha' with 'ALPHA'; in notes-b.txt replace "
    "'beta' with 'BETA'.\n"
    "4. Use the AskUser tool to ask me one multiple-choice question with two options about which "
    "edited file looks better.\n"
    "5. Reply with one short sentence."
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    CWD.mkdir(parents=True, exist_ok=True)
    (CWD / "notes-a.txt").write_text("alpha\n", encoding="utf-8")
    (CWD / "notes-b.txt").write_text("beta\n", encoding="utf-8")
    for name in ("missing-notes.txt",):
        target = CWD / name
        if target.exists():
            target.unlink()

    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "multiedit.jsonl",
        raw_path=ROBUST_FIXTURES / "multiedit.raw.jsonl",
    )
    recorder.meta("Environment", **environment_info(droid, model=DEFAULT_MODEL, autonomy="off", cwd=str(CWD)))
    observations: dict = {"permission_calls": [], "question_calls": 0}

    def on_permission(request):
        response = make_permission_handler(recorder)(request)
        observations["permission_calls"].append(
            [{"type": type(a).__name__, "file": getattr(a, "file_path", None)} for a in request.actions]
        )
        return response

    def on_question(request: QuestionRequest) -> QuestionResponse:
        observations["question_calls"] += 1
        recorder.handler("QuestionRequest", {"request": request}, "[question] handler raising on purpose")
        raise RuntimeError("spike: question handler exploded")

    session = Session(
        cwd=CWD,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=on_permission, on_question=on_question),
        runtime=runtime,
    )
    result = None
    try:
        await open_recorded(session, recorder)
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout)
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    recorder.print_summary()

    a_ok = (CWD / "notes-a.txt").read_text(encoding="utf-8") == "ALPHA\n"
    b_ok = (CWD / "notes-b.txt").read_text(encoding="utf-8") == "BETA\n"
    events = []
    with (ROBUST_FIXTURES / "multiedit.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            events.append(json.loads(line))
    tool_results = [e for e in events if e["type"] == "ToolResult"]
    read_missing = [e for e in tool_results if e["data"].get("tool_name") == "Read" and e["data"].get("is_error")]
    edit_results = [e for e in tool_results if e["data"].get("tool_name") == "Edit"]
    error_events = [e for e in events if e["type"] == "ErrorEvent"]
    diff_shapes = []
    for e in edit_results:
        try:
            payload = json.loads(e["data"]["content"])
            diff_shapes.append(
                {"file_path": payload.get("file_path"), "linesAdded": payload.get("linesAdded"),
                 "linesRemoved": payload.get("linesRemoved"), "diffLines_n": len(payload.get("diffLines") or [])}
            )
        except Exception:
            diff_shapes.append({"raw": str(e["data"]["content"])[:120]})
    print(f"notes-a ALPHA: {a_ok}; notes-b BETA: {b_ok}", flush=True)
    print(f"permission calls (actions per call): {json.dumps(observations['permission_calls'])}", flush=True)
    print(f"read-missing ToolResult is_error entries: {len(read_missing)} -> "
          f"{preview(read_missing[0]['data']['content'], 120) if read_missing else None}", flush=True)
    print(f"edit results: {json.dumps(diff_shapes, ensure_ascii=False)}", flush=True)
    print(f"ErrorEvents: {json.dumps([e['data']['message'] + ' / ' + e['data']['error_type'] for e in error_events])}", flush=True)
    print(f"question handler calls: {observations['question_calls']}", flush=True)
    print(f"result: {result.subtype if result else None} text={preview(result.text, 100) if result else None}", flush=True)
    ok = (
        result is not None
        and a_ok
        and b_ok
        and len(read_missing) >= 1
        and len(edit_results) >= 2
        and observations["question_calls"] >= 1
        and any("Question" in e["data"]["message"] for e in error_events)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))