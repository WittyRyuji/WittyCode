"""Extra no-credit probe: fetch the message history of a saved session.

Session.resume() restores history inside Droid, but the high-level SDK neither
replays it on the stream nor exposes it. The public low-level DroidClient's
load_session() result carries it (result.session.messages). This loads the
stream_turn session without sending a message (no model call) and writes
fixtures/history.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter

from droid_sdk.low_level import DroidClient, ProcessTransport

from _common import FIXTURES, SANDBOX, setup_console, to_jsonable
from find_droid import find_droid
from resume import session_id_from_fixture


def _block_types(message) -> list[str]:
    types = (getattr(block, "type", type(block).__name__) for block in message.content)
    return [str(getattr(t, "value", t)) for t in types]


def _elide_hidden_context(messages: list[dict]) -> list[dict]:
    # llm_only messages carry injected system reminders (tool/skill catalogs,
    # machine info, workspace state); keep their shape, drop the text.
    for message in messages:
        if message.get("visibility") == "llm_only":
            for block in message.get("content") or []:
                if isinstance(block.get("text"), str):
                    block["text"] = f"<elided llm_only context: {len(block['text'])} chars>"
    return messages


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()
    target = args.session_id or session_id_from_fixture(FIXTURES / "stream_turn.jsonl")
    if not target:
        print("no session id: run spikes/stream_turn.py first or pass --session-id")
        return 2
    os.chdir(SANDBOX)
    client = DroidClient(transport=ProcessTransport(exec_path=str(find_droid()), cwd=str(SANDBOX)))
    started = time.perf_counter()
    await client.connect()
    try:
        result = await client.load_session(session_id=target)
        elapsed = time.perf_counter() - started
        messages = result.session.messages
        roles = Counter(str(getattr(m.role, "value", m.role)) for m in messages)
        print(f"load_session({target}): {len(messages)} messages in {elapsed:.2f}s; roles={dict(roles)}; "
              f"title={result.session.title!r}; agent_loop_in_progress={result.is_agent_loop_in_progress}")
        for message in messages:
            role = getattr(message.role, "value", message.role)
            print(f"   {role:9} {message.id[:13]:13} visibility={getattr(message.visibility, 'value', message.visibility)} "
                  f"blocks={_block_types(message)}")
        (FIXTURES / "history.json").write_text(
            json.dumps(
                {
                    "session_id": target,
                    "elapsed_s": round(elapsed, 3),
                    "title": result.session.title,
                    "settings": to_jsonable(result.settings),
                    "pending_permissions": to_jsonable(result.pending_permissions),
                    "pending_ask_user_requests": to_jsonable(result.pending_ask_user_requests),
                    "messages": _elide_hidden_context(to_jsonable(messages)),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"wrote {FIXTURES / 'history.json'}")
    finally:
        try:
            await client.close_session(reason="other")
        finally:
            await client.close()
    return 0 if messages else 1


if __name__ == "__main__":
    setup_console()
    raise SystemExit(asyncio.run(main()))
