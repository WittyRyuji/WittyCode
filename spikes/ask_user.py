"""Spike (c): a turn in which Droid has to call its AskUser tool.

The question handler answers every question with its first option.
Writes fixtures/ask_user.jsonl (+ .raw.jsonl).
"""

from __future__ import annotations

import argparse
import asyncio

from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    FIXTURES,
    SANDBOX,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_question_handler,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    reasoning_effort,
    run_turn,
    setup_console,
)

PROMPT = (
    "Before you answer, use your AskUser tool to ask me exactly one multiple-choice question "
    "with three short options: which greeting word I prefer. Do not use any other tool. "
    "After I answer, reply with one short sentence that uses the greeting I picked."
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    prepare_sandbox()
    runtime, droid = make_runtime()
    recorder = Recorder(FIXTURES / "ask_user.jsonl", raw_path=FIXTURES / "ask_user.raw.jsonl")
    recorder.meta(
        "Environment",
        **environment_info(droid, model=args.model, reasoning=args.reasoning, autonomy="off", cwd=str(SANDBOX)),
    )
    answers: list[str] = []

    def choose(question) -> str:
        answer = question.options[0] if question.options else "none"
        answers.append(answer)
        return answer

    session = Session(
        cwd=SANDBOX,
        model=args.model,
        reasoning_effort=reasoning_effort(args.reasoning),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(
            on_permission=make_permission_handler(recorder),
            on_question=make_question_handler(recorder, choose),
        ),
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
    used = bool(answers) and any(a.lower() in result.text.lower() for a in answers) if result else False
    print(
        f"result: {result.subtype if result else None}; questions answered: {answers}; "
        f"answer echoed in reply: {used}; reply={result.text if result else None!r}"
    )
    return 0 if result is not None and result.success and answers else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
