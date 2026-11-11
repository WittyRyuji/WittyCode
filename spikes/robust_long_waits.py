"""Spike (item 2): long (~10 min) waits inside async interaction handlers.

Two sessions run concurrently in this one Python process:
  perm: its turn calls Execute; the async permission handler sleeps --wait
        seconds, then answers proceed_once.
  ask:  its turn calls AskUser; the async question handler sleeps --wait
        seconds, then answers the first option.

Records (fixtures/robust/long_waits_{perm,ask}.jsonl + .raw.jsonl):
  - whether droid or the SDK times out / cancels during the wait
  - any keepalive/heartbeat notifications in the raw stream during the wait
  - whether the turn completes normally after the answer
This also exercises async handlers in general.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    PermissionRequest,
    PermissionResponse,
    QuestionRequest,
    QuestionResponse,
    Session,
    SessionConfig,
    ToolConfirmationOutcome,
)

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    Recorder,
    close_recorded,
    environment_info,
    first_option,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    preview,
    reasoning_effort,
    review_action,
    run_turn,
    setup_console,
)
from _robust_common import ROBUST_FIXTURES, ROBUST_SANDBOX

PERM_PROMPT = (
    "Use the Execute tool to run exactly this command and nothing else: "
    "Write-Output hello-after-wait . Then reply with one short sentence."
)
ASK_PROMPT = (
    "Before you answer, use your AskUser tool to ask me exactly one multiple-choice question "
    "with three short options: which color I prefer. Do not use any other tool. "
    "After I answer, reply with one short sentence naming the color I picked."
)


async def _reporter(tag: str, interval: float = 60.0) -> None:
    started = time.perf_counter()
    while True:
        await asyncio.sleep(interval)
        print(f"[{tag}] still waiting, elapsed {time.perf_counter() - started:.0f}s", flush=True)


async def run_perm(wait: float) -> dict:
    tag = "perm"
    cwd = ROBUST_SANDBOX / "waits" / "perm"
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "long_waits_perm.jsonl",
        raw_path=ROBUST_FIXTURES / "long_waits_perm.raw.jsonl",
        echo=False,
    )
    recorder.meta(
        "Environment",
        **environment_info(
            droid, model=DEFAULT_MODEL, reasoning=DEFAULT_REASONING, autonomy="off",
            cwd=str(cwd), wait_s=wait, kind="permission",
        ),
    )
    state: dict = {"slept": None, "error": None, "session_id": None}

    async def on_permission(request: PermissionRequest) -> PermissionResponse:
        reviews = [review_action(action) for action in request.actions]
        allowed = bool(request.actions) and all(ok for ok, _ in reviews)
        recorder.handler(
            "PermissionRequest",
            {"request": request, "policy": [{"allowed": ok, "reason": why} for ok, why in reviews]},
            f"[perm] handler entered; sleeping {wait}s before answering",
        )
        print(f"[perm] permission requested at t={recorder.now():.1f}s; sleeping {wait}s", flush=True)
        started = time.perf_counter()
        await asyncio.sleep(wait)
        state["slept"] = round(time.perf_counter() - started, 2)
        outcome = ToolConfirmationOutcome.PROCEED_ONCE if allowed else ToolConfirmationOutcome.CANCEL
        response = request.respond(outcome)
        recorder.handler(
            "PermissionAnswered",
            {"response": response, "slept_s": state["slept"]},
            f"[perm] answered {outcome.value} after {state['slept']}s",
        )
        print(f"[perm] answered {outcome.value} after sleeping {state['slept']}s", flush=True)
        return response

    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=on_permission),
        runtime=runtime,
    )
    reporter = asyncio.create_task(_reporter(tag))
    result = None
    try:
        await open_recorded(session, recorder)
        state["session_id"] = session.id
        print(f"[perm] session {session.id} open; starting turn", flush=True)
        result = await run_turn(session, recorder, PERM_PROMPT, timeout=wait + 900)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        recorder.meta("TurnException", error=type(exc).__name__, message=str(exc))
        print(f"[perm] EXCEPTION {state['error']}", flush=True)
    finally:
        reporter.cancel()
        try:
            await close_recorded(session, recorder)
        except Exception as exc:
            recorder.meta("CloseError", error=type(exc).__name__, message=str(exc))
        recorder.close()
    return {
        "kind": tag,
        "session_id": state["session_id"],
        "slept_s": state["slept"],
        "error": state["error"],
        "result": None
        if result is None
        else {
            "type": type(result).__name__,
            "subtype": result.subtype,
            "success": result.success,
            "duration_s": result.duration.total_seconds(),
            "text": preview(result.text, 120),
        },
        "counts": {str(k): v for k, v in recorder.counts.items()},
    }


async def run_ask(wait: float) -> dict:
    tag = "ask"
    cwd = ROBUST_SANDBOX / "waits" / "ask"
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "long_waits_ask.jsonl",
        raw_path=ROBUST_FIXTURES / "long_waits_ask.raw.jsonl",
        echo=False,
    )
    recorder.meta(
        "Environment",
        **environment_info(
            droid, model=DEFAULT_MODEL, reasoning=DEFAULT_REASONING, autonomy="off",
            cwd=str(cwd), wait_s=wait, kind="ask_user",
        ),
    )
    state: dict = {"slept": None, "error": None, "session_id": None}

    async def on_question(request: QuestionRequest) -> QuestionResponse:
        recorder.handler(
            "QuestionRequest",
            {"request": request},
            f"[ask] handler entered; sleeping {wait}s before answering",
        )
        print(f"[ask] question requested at t={recorder.now():.1f}s; sleeping {wait}s", flush=True)
        started = time.perf_counter()
        await asyncio.sleep(wait)
        state["slept"] = round(time.perf_counter() - started, 2)
        response = request.submit([question.answer(first_option(question)) for question in request.questions])
        recorder.handler(
            "QuestionAnswered",
            {"response": response, "slept_s": state["slept"]},
            f"[ask] answered after {state['slept']}s",
        )
        print(f"[ask] answered after sleeping {state['slept']}s", flush=True)
        return response

    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_question=on_question),
        runtime=runtime,
    )
    reporter = asyncio.create_task(_reporter(tag))
    result = None
    try:
        await open_recorded(session, recorder)
        state["session_id"] = session.id
        print(f"[ask] session {session.id} open; starting turn", flush=True)
        result = await run_turn(session, recorder, ASK_PROMPT, timeout=wait + 900)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        recorder.meta("TurnException", error=type(exc).__name__, message=str(exc))
        print(f"[ask] EXCEPTION {state['error']}", flush=True)
    finally:
        reporter.cancel()
        try:
            await close_recorded(session, recorder)
        except Exception as exc:
            recorder.meta("CloseError", error=type(exc).__name__, message=str(exc))
        recorder.close()
    return {
        "kind": tag,
        "session_id": state["session_id"],
        "slept_s": state["slept"],
        "error": state["error"],
        "result": None
        if result is None
        else {
            "type": type(result).__name__,
            "subtype": result.subtype,
            "success": result.success,
            "duration_s": result.duration.total_seconds(),
            "text": preview(result.text, 120),
        },
        "counts": {str(k): v for k, v in recorder.counts.items()},
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=float, default=600.0)
    args = parser.parse_args()
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    print(f"starting two sessions with {args.wait}s handler waits, concurrently", flush=True)
    started = time.perf_counter()
    outcomes = await asyncio.gather(run_perm(args.wait), run_ask(args.wait))
    total = time.perf_counter() - started
    print(f"both flows finished in {total:.1f}s", flush=True)
    print(json.dumps(outcomes, ensure_ascii=False, indent=2, default=str), flush=True)
    ok = all(
        o["result"] is not None and o["result"]["success"] and (o["slept_s"] or 0) >= args.wait * 0.95
        for o in outcomes
    )
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))