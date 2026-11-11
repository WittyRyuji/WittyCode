"""Spike (item 3, hooks): project-level .factory/hooks.json inside the sandbox.

Registers PreToolUse/PostToolUse (matcher Execute), UserPromptSubmit,
SessionStart and Stop hooks that append their JSON stdin to hook-log.txt,
then runs one turn with a single Execute call and records HookExecution
stream events. Only files inside sandbox/robust/hooks are touched.

Writes fixtures/robust/hooks.jsonl (+ .raw.jsonl).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig

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

HOOK_CWD = ROBUST_SANDBOX / "hooks"
LOG = HOOK_CWD / "hook-log.txt"

PROMPT = (
    "Use the Execute tool to run exactly this command and nothing else: "
    "Write-Output hook-test . Then reply with one short sentence."
)

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


def write_hooks_config() -> None:
    (HOOK_CWD / ".factory").mkdir(parents=True, exist_ok=True)
    command = (
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        "\"[Console]::In.ReadToEnd() | Add-Content -LiteralPath '"
        + str(LOG)
        + "' -Encoding utf8\""
    )
    config: dict[str, list] = {}
    for event in EVENTS:
        group: dict = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        if event in ("PreToolUse", "PostToolUse"):
            group["matcher"] = "Execute"
        config[event] = [group]
    (HOOK_CWD / ".factory" / "hooks.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    if LOG.exists():
        LOG.unlink()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=150.0)
    args = parser.parse_args()
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)
    write_hooks_config()
    print(f"hooks config: {HOOK_CWD / '.factory' / 'hooks.json'}", flush=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "hooks.jsonl",
        raw_path=ROBUST_FIXTURES / "hooks.raw.jsonl",
    )
    recorder.meta(
        "Environment",
        **environment_info(droid, model=DEFAULT_MODEL, autonomy="off", cwd=str(HOOK_CWD), events=list(EVENTS)),
    )
    hooks_seen: list[dict] = []

    def on_event(event: object) -> None:
        if type(event).__name__ == "HookExecution":
            hooks_seen.append(
                {
                    "event_name": getattr(event, "event_name", None),
                    "status": getattr(event, "status", None),
                    "exit_code": getattr(event, "exit_code", None),
                    "command": getattr(event, "command", None),
                }
            )

    session = Session(
        cwd=HOOK_CWD,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    result = None
    try:
        await open_recorded(session, recorder)
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout, on_event=on_event)
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    recorder.print_summary()
    log_text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    log_events = []
    for line in log_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            log_events.append(json.loads(line).get("hook_event_name"))
        except json.JSONDecodeError:
            log_events.append("<unparsable line>")
    print(f"HookExecution stream events: {json.dumps(hooks_seen, ensure_ascii=False)}", flush=True)
    print(f"hook-log.txt events: {json.dumps(log_events, ensure_ascii=False)}", flush=True)
    print(f"result: {result.subtype if result else None}", flush=True)
    ok = result is not None and result.success and len(log_events) >= 2
    return 0 if ok else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))