"""Phase 0b A6: interrupted and permission-cancelled actions vs on-disk reality.

(a) Allow mode: the turn creates interrupt_probe.txt and then starts a slow
    Execute command; we interrupt mid-command (first ToolProgress). The
    follow-up answer about whether the file was created must match the disk.
(b) Deny mode: the Create permission for cancel_probe.txt is cancelled; the
    follow-up answer must match the disk (file absent).

Live turns: 3. Writes fixtures/context/actions.jsonl (+ .raw.jsonl) and merges
cases A6a/A6b into fixtures/context/state.json.
"""

from __future__ import annotations

import _pyguard  # noqa: F401  -- require Python 3.11.x

import argparse
import asyncio
import re
from typing import Any

from droid_sdk import (
    Autonomy,
    InteractionHandlers,
    PermissionResponse,
    Session,
    SessionConfig,
    ToolConfirmationOutcome,
    ToolProgress,
)

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    Recorder,
    environment_info,
    make_runtime,
    reasoning_effort,
    review_action,
    run_turn,
    setup_console,
)
from _ctx_common import (
    CTX_FIXTURES,
    CTX_SANDBOX,
    UsageTracker,
    load_json,
    new_canary,
    save_json,
    transport_pid,
)

PROMPT_A = (
    "Work in the current directory. Do these two steps strictly in order, "
    "waiting for each step to finish before starting the next:\n"
    "1. Use the Create tool to create the file interrupt_probe.txt whose "
    "entire content is: '{payload}'\n"
    "2. Use the Execute tool to run this command: Start-Sleep -Seconds 30\n"
    "Both steps are required."
)
PROMPT_B = (
    "Use the Create tool to create the file cancel_probe.txt in the current "
    "directory whose entire content is: '{payload}'."
)
VERIFY = (
    "Answer from what happened earlier in this conversation (do NOT read any "
    "files from disk): did you finish creating interrupt_probe.txt, and did "
    "you finish creating cancel_probe.txt? Reply with exactly two lines:\n"
    "interrupt_probe=yes or interrupt_probe=no\n"
    "cancel_probe=yes or cancel_probe=no"
)


def make_switchable_handler(recorder, policy):
    def on_permission(request):
        offered = [o.value for o in request.options]
        if policy["mode"] == "allow":
            reviews = [review_action(a) for a in request.actions]
            allowed = bool(request.actions) and all(ok for ok, _ in reviews)
            if allowed and ToolConfirmationOutcome.PROCEED_ONCE in offered:
                response = request.respond(ToolConfirmationOutcome.PROCEED_ONCE)
                decision = "allow"
            elif ToolConfirmationOutcome.CANCEL in offered:
                response = request.respond(ToolConfirmationOutcome.CANCEL)
                decision = "cancel(policy)"
            else:
                response = PermissionResponse(selected_option=ToolConfirmationOutcome.CANCEL)
                decision = "cancel(fallback)"
            detail = [{"allowed": ok, "reason": why} for ok, why in reviews]
        else:
            policy["denials"] += 1
            if ToolConfirmationOutcome.CANCEL in offered:
                response = request.respond(ToolConfirmationOutcome.CANCEL)
            else:
                response = PermissionResponse(selected_option=ToolConfirmationOutcome.CANCEL)
            decision = "deny-all"
            detail = "deny mode"
        recorder.handler(
            "PermissionRequest",
            {"request": request, "mode": policy["mode"], "decision": decision, "detail": detail, "response": response},
            f"[permission:{policy['mode']}] -> {response.selected_option.value}",
        )
        return response

    return on_permission


async def open_meta(session, recorder, **extra):
    import time

    started = time.perf_counter()
    await session.open()
    recorder.meta(
        "SessionOpened",
        session_id=session.id,
        cwd=session.cwd,
        settings=session.settings,
        open_seconds=round(time.perf_counter() - started, 3),
        **extra,
    )
    session.on_notification(recorder.raw)


async def close_meta(session, recorder):
    import time

    started = time.perf_counter()
    await session.close()
    recorder.meta("SessionClosed", session_id=session.id, close_seconds=round(time.perf_counter() - started, 3))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    a_dir = CTX_SANDBOX / "actions"
    a_dir.mkdir(parents=True, exist_ok=True)
    for name in ("interrupt_probe.txt", "cancel_probe.txt"):
        target = a_dir / name
        if target.exists():
            target.unlink()

    used: set[str] = set()
    payload_a = new_canary(used)
    used.add(payload_a)
    payload_b = new_canary(used)

    runtime, droid = make_runtime()
    recorder = Recorder(CTX_FIXTURES / "actions.jsonl", raw_path=CTX_FIXTURES / "actions.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", scope="A6"),
    )
    policy: dict[str, Any] = {"mode": "allow", "denials": 0}
    tracker = UsageTracker()
    session = Session(
        cwd=a_dir,
        model=args.model,
        reasoning_effort=reasoning_effort(args.reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_switchable_handler(recorder, policy)),
        runtime=runtime,
    )
    interrupt: dict[str, Any] = {"requested": False, "at_turn_t": None}
    cases: dict[str, Any] = {}
    try:
        await open_meta(session, recorder)
        session.on_notification(tracker.callback())
        recorder.meta("Process", pid=transport_pid(session))

        def on_event(event):
            if isinstance(event, ToolProgress) and not interrupt["requested"]:
                interrupt["requested"] = True
                interrupt["at_turn_t"] = round(recorder.turn_now(), 3)
                recorder.meta("InterruptRequested", trigger="ToolProgress", turn_t=interrupt["at_turn_t"])
                asyncio.create_task(session.interrupt())

        r1 = await run_turn(
            session, recorder, PROMPT_A.format(payload=payload_a), timeout=args.timeout, on_event=on_event
        )
        file_a = a_dir / "interrupt_probe.txt"
        exists_a = file_a.exists()
        content_a = file_a.read_text(encoding="utf-8") if exists_a else None
        recorder.meta("DiskAfterA", exists=exists_a, content_matches=bool(content_a is not None and payload_a in content_a))
        cases["A6a"] = {
            "interrupt_requested": interrupt["requested"],
            "result_subtype": r1.subtype,
            "file_exists": exists_a,
            "content_matches_payload": bool(content_a is not None and payload_a in content_a),
        }

        policy["mode"] = "deny"
        r2 = await run_turn(session, recorder, PROMPT_B.format(payload=payload_b), timeout=args.timeout)
        exists_b = (a_dir / "cancel_probe.txt").exists()
        cases["A6b"] = {
            "result_subtype": r2.subtype,
            "permission_denials": policy["denials"],
            "file_exists": exists_b,
        }

        r3 = await run_turn(session, recorder, VERIFY, timeout=args.timeout)
        answers = dict(re.findall(r"(interrupt_probe|cancel_probe)\s*=\s*(yes|no)", (r3.text or "").lower()))
        recorder.meta("VerifyAnswers", answers=answers, reply=r3.text)
        for key, case, exists in (
            ("interrupt_probe", "A6a", exists_a),
            ("cancel_probe", "A6b", exists_b),
        ):
            said = answers.get(key)
            cases[case]["model_answer"] = said
            cases[case]["answer_matches_disk"] = (said == "yes") == exists if said in ("yes", "no") else None
        cases["A6a"]["pass"] = bool(
            interrupt["requested"] and r1.subtype == "interrupted" and cases["A6a"]["answer_matches_disk"]
        )
        cases["A6b"]["pass"] = bool(
            policy["denials"] >= 1 and not exists_b and cases["A6b"]["answer_matches_disk"]
        )
    except Exception as exc:
        cases["fatal"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        print(f"FATAL: {cases['fatal']}", flush=True)
    finally:
        try:
            await close_meta(session, recorder)
        except Exception as exc:
            recorder.meta("CloseError", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        recorder.meta("UsageTracker", **tracker.summary())
        recorder.close()

    state_path = CTX_FIXTURES / "state.json"
    state = load_json(state_path) if state_path.exists() else {}
    state.setdefault("canaries", {})["payload_interrupt"] = payload_a
    state["canaries"]["payload_cancel"] = payload_b
    state.setdefault("sessions", {})["actions"] = session.id
    state.setdefault("cases", {}).update(cases)
    save_json(state_path, state)
    for name in ("A6a", "A6b"):
        verdict = cases.get(name, {}).get("pass")
        print(f"case {name}: {'PASS' if verdict else 'FAIL'} {cases.get(name)}", flush=True)
    ok = "fatal" not in cases and all(cases.get(n, {}).get("pass") for n in ("A6a", "A6b"))
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))