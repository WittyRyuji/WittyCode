"""Spike (item 3): exact shapes of SDK/droid errors and exceptions.

Cases:
  bad_cwd_missing / bad_cwd_file  -> local exception from open(), no process spawned
  bad_exe_missing / bad_exe_text  -> connect-time exception from open()
  invalid_model                   -> model id that does not exist (open or turn)
  kill_mid_turn                   -> TerminateProcess on the droid.exe PID during a
                                     running Execute; records what the stream raises
  handler_raises / invalid_response -> permission handler errors in one session,
                                     two turns (ErrorEvent + fallback cancel expected)

The failing-Read ToolResult shape and the AskUser-handler exception are covered
by robust_multiedit.py (same turn) to save live turns.

Writes fixtures/robust/errors_*.jsonl and errors_summary.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    PermissionRequest,
    PermissionResponse,
    Runtime,
    Session,
    SessionConfig,
    ToolConfirmationOutcome,
    ToolProgress,
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
    to_jsonable,
)
from _robust_common import (
    ROBUST_FIXTURES,
    ROBUST_SANDBOX,
    alive,
    make_tick_command,
    session_droid_pid,
    terminate,
)

RESULTS: list[dict] = []


def record(case: str, **data) -> None:
    RESULTS.append(to_jsonable({"case": case, **data}))
    print(f"[case {case}] {preview(data, 260)}", flush=True)


def exc_info(exc: BaseException) -> dict:
    info = {"exception_type": type(exc).__name__, "message": str(exc)}
    for attr in ("cwd", "exec_path", "exit_code", "signal", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            info[attr] = value
    return info


async def case_bad_cwd() -> None:
    base = ROBUST_SANDBOX / "errors"
    base.mkdir(parents=True, exist_ok=True)
    a_file = base / "a-file.txt"
    a_file.write_text("not a directory", encoding="utf-8")
    for label, cwd in (("bad_cwd_missing", base / "no-such-dir"), ("bad_cwd_file", a_file)):
        try:
            session = Session(cwd=cwd, model=DEFAULT_MODEL)
            await session.open()
            record(label, unexpected="open succeeded")
            await session.close()
        except Exception as exc:
            record(label, **exc_info(exc))


async def case_bad_exe() -> None:
    cwd = ROBUST_SANDBOX / "errors"
    text_file = cwd / "a-file.txt"
    for label, exe in (
        ("bad_exe_missing", cwd / "no-such-droid.exe"),
        ("bad_exe_text", text_file),
    ):
        try:
            session = Session(cwd=cwd, model=DEFAULT_MODEL, runtime=Runtime(executable=exe))
            await session.open()
            record(label, unexpected="open succeeded")
            await session.close()
        except Exception as exc:
            record(label, **exc_info(exc))


async def case_invalid_model(timeout: float) -> None:
    cwd = ROBUST_SANDBOX / "errors"
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "errors_invalid_model.jsonl",
        raw_path=ROBUST_FIXTURES / "errors_invalid_model.raw.jsonl",
    )
    recorder.meta("Environment", **environment_info(droid, model="definitely-not-a-model-0b", cwd=str(cwd)))
    session = Session(
        cwd=cwd,
        model="definitely-not-a-model-0b",
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    opened = False
    try:
        await open_recorded(session, recorder)
        opened = True
    except Exception as exc:
        record("invalid_model", stage="open", **exc_info(exc))
    result = None
    if opened:
        try:
            result = await run_turn(session, recorder, "Reply with the word ok.", timeout=timeout)
            record(
                "invalid_model",
                stage="turn",
                result_type=type(result).__name__,
                subtype=result.subtype,
                error=None
                if result.error is None
                else {"message": result.error.message, "error_type": result.error.error_type.value},
                text=preview(result.text, 120),
            )
        except Exception as exc:
            record("invalid_model", stage="turn", **exc_info(exc))
    try:
        await close_recorded(session, recorder)
    except Exception as exc:
        recorder.meta("CloseError", **exc_info(exc))
    recorder.close()


async def case_kill_mid_turn(timeout: float) -> None:
    cwd = ROBUST_SANDBOX / "errors" / "kill"
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "errors_kill_midturn.jsonl",
        raw_path=ROBUST_FIXTURES / "errors_kill_midturn.raw.jsonl",
    )
    recorder.meta("Environment", **environment_info(droid, model=DEFAULT_MODEL, cwd=str(cwd)))
    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    state: dict = {"pid": None, "killed": False}

    def on_event(event: object) -> None:
        if state["killed"] or not isinstance(event, ToolProgress):
            return
        state["killed"] = True
        pid = session_droid_pid(session)
        state["pid"] = pid
        recorder.meta("KillingDroid", pid=pid, turn_t=round(recorder.turn_now(), 3))
        print(f"[kill] TerminateProcess({pid}) after first ToolProgress", flush=True)
        ok = terminate(pid) if pid else False
        recorder.meta("KillResult", pid=pid, terminated=ok, alive_right_after=alive(pid) if pid else None)

    try:
        await open_recorded(session, recorder)
        prompt = (
            "Use the Execute tool to run exactly this command and nothing else: "
            + make_tick_command(60)
            + " . Reply with one short sentence when it finishes."
        )
        result = await run_turn(session, recorder, prompt, timeout=timeout, on_event=on_event)
        record(
            "kill_mid_turn",
            unexpected="turn completed",
            result_type=type(result).__name__,
            subtype=result.subtype,
            droid_pid=state["pid"],
        )
    except Exception as exc:
        record(
            "kill_mid_turn",
            droid_pid=state["pid"],
            droid_alive=alive(state["pid"]) if state["pid"] else None,
            **exc_info(exc),
        )
    try:
        await close_recorded(session, recorder)
        record("kill_mid_turn_close", closed=True)
    except Exception as exc:
        record("kill_mid_turn_close", closed=False, **exc_info(exc))
    recorder.close()


async def case_handler_errors(timeout: float) -> None:
    cwd = ROBUST_SANDBOX / "errors" / "handlers"
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "errors_handlers.jsonl",
        raw_path=ROBUST_FIXTURES / "errors_handlers.raw.jsonl",
    )
    recorder.meta("Environment", **environment_info(droid, model=DEFAULT_MODEL, cwd=str(cwd)))
    calls = {"n": 0}

    def on_permission(request: PermissionRequest) -> PermissionResponse:
        calls["n"] += 1
        n = calls["n"]
        offered = [o.value for o in request.options]
        recorder.handler(
            "PermissionRequest",
            {"request": request, "call": n},
            f"[perm #{n}] offered={offered}",
        )
        if n == 1:
            raise RuntimeError("spike: permission handler exploded")
        # Not among the offered options; the dispatcher must fall back to cancel.
        return PermissionResponse(selected_option=ToolConfirmationOutcome.PROCEED_AUTO_RUN)

    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=on_permission),
        runtime=runtime,
    )
    try:
        await open_recorded(session, recorder)
        r1 = await run_turn(session, recorder, "Create the file a.txt containing exactly: a", timeout=timeout)
        record(
            "handler_raises",
            result_type=type(r1).__name__,
            subtype=r1.subtype,
            a_txt_exists=(cwd / "a.txt").exists(),
            text=preview(r1.text, 120),
        )
        r2 = await run_turn(session, recorder, "Create the file b.txt containing exactly: b", timeout=timeout)
        record(
            "invalid_response",
            result_type=type(r2).__name__,
            subtype=r2.subtype,
            b_txt_exists=(cwd / "b.txt").exists(),
            text=preview(r2.text, 120),
        )
    except Exception as exc:
        record("handler_errors", **exc_info(exc))
    try:
        await close_recorded(session, recorder)
    except Exception as exc:
        recorder.meta("CloseError", **exc_info(exc))
    recorder.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=150.0)
    args = parser.parse_args()
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    await case_bad_cwd()
    await case_bad_exe()
    await case_invalid_model(args.timeout)
    await case_kill_mid_turn(args.timeout)
    await case_handler_errors(args.timeout)
    summary = ROBUST_FIXTURES / "errors_summary.json"
    summary.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {summary}", flush=True)
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))