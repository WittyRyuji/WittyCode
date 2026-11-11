"""``tests/fake_codex.py`` with what ``tests/test_codex_side.py`` needs, run the same way
(``python tests/fake_codex_side.py app-server``; the state, ``received.jsonl`` and the markers are the base fake's):

- ``thread/start`` honours ``ephemeral``, as codex-cli 0.166 does: such a thread lives in this process only (never in
  ``state.json``, so neither ``thread/list`` nor a later process knows it) and comes back with ``ephemeral: true,
  path: null``. ``FAKE_SIDE_OLD=1`` ignores the flag, as a Codex that does not know it would: the thread is kept and
  comes back ``ephemeral: false``; ``thread/delete`` removes a kept thread;
- ``FAKE_SIDE_FAIL=<method>``: that request is answered with an error;
- ``FAKE_SIDE_SLOW=<method>:<seconds>``: that request is answered so much later;
- ``FAKE_SIDE_MODELS``: a JSON list that replaces the model list;
- the marker ``[said-interrupted]``: an answer, then the turn ends ``interrupted`` (as when someone stopped it).

Every thread made is also written to ``$FAKE_CODEX_HOME/made.jsonl`` (``{"id", "ephemeral"}``): the tests learn the id of a
thread whose turn never started from there.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_codex  # noqa: E402


class SideFake(fake_codex.Fake):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop)
        self.memory: dict[str, dict[str, Any]] = {}

    def record(self, thread_id: Any) -> dict[str, Any]:
        found = self.memory.get(thread_id)
        return found if found is not None else super().record(thread_id)

    def thread_object(self, record: dict[str, Any], *, turns: bool = False) -> dict[str, Any]:
        data = super().thread_object(record, turns=turns)
        if record["id"] in self.memory:
            data.update(ephemeral=True, path=None)
        return data

    def m_thread_start(self, params: dict[str, Any]) -> Any:
        opened = super().m_thread_start(params)
        thread_id = opened["thread"]["id"]
        if params.get("ephemeral") and os.environ.get("FAKE_SIDE_OLD") != "1":
            self.memory[thread_id] = self.state["threads"].pop(thread_id)
            self.save()
            opened["thread"].update(ephemeral=True, path=None)
        with (fake_codex.HOME / "made.jsonl").open("a", encoding="utf-8") as made:
            made.write(json.dumps({"id": thread_id, "ephemeral": opened["thread"]["ephemeral"]}) + "\n")
        return opened

    def m_thread_delete(self, params: dict[str, Any]) -> Any:
        if params.get("threadId") in self.memory:
            raise fake_codex.RpcError(-32600, "ephemeral root threads cannot be deleted")
        self.state["threads"].pop(self.record(params.get("threadId"))["id"])
        self.save()
        return {}

    async def run_turn(self, record: dict[str, Any], turn: dict[str, Any], inputs: list[dict[str, Any]], text: str) -> None:
        if "[said-interrupted]" not in text:
            await super().run_turn(record, turn, inputs, text)
            return
        self.notify("turn/started", {"threadId": record["id"], "turn": self.turn_object(turn)})
        await self.say(record, turn, "half an answer")
        self.running.pop(record["id"], None)
        turn.update(status="interrupted", completedAt=int(time.time()), durationMs=5)
        self.notify("turn/completed", {"threadId": record["id"], "turn": self.turn_object(turn)})

    async def on_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method == os.environ.get("FAKE_SIDE_FAIL"):
            self.send({"id": message["id"], "error": {"code": -32603, "message": f"{method} failed on purpose"}})
            return
        slow, _, seconds = os.environ.get("FAKE_SIDE_SLOW", "").partition(":")
        if slow == method:
            await asyncio.sleep(float(seconds))
        await super().on_request(message)


def main() -> None:
    models = os.environ.get("FAKE_SIDE_MODELS")
    if models:
        fake_codex.MODELS[:] = json.loads(models)
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print("codex-cli 0.166.0")
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(SideFake(loop).run())


if __name__ == "__main__":
    main()
