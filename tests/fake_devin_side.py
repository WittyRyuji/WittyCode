"""``tests/fake_devin.py`` with what ``tests/test_devin_side.py`` needs, run the same way (``python tests/fake_devin_side.py
acp`` and the CLI commands; the state, ``received.jsonl`` and the markers are the base fake's):

- one more free model, ``swe-2-flash-medium`` ("SWE-2 Flash"), whose thought levels are not listed weakest first and
  whose default is not the weakest;
- a new session starts on a model that is not free (``claude-opus-5-5-medium``), as an account's default may be;
- ``FAKE_SIDE_FREE=<id,id>``: exactly these models are free; ``FAKE_SIDE_DROP=<id,id>``: these are not offered;
- ``FAKE_SIDE_FAIL=<method>``: that request is answered with an error;
- ``FAKE_SIDE_SLOW=<method>:<seconds>``: that request is answered so much later.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_devin  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5-medium"
fake_devin.MODELS.append(("swe-2-flash-medium", "SWE-2 Flash", True, ["high", "max", "medium"], "high", 262144, True))
_free = os.environ.get("FAKE_SIDE_FREE")
_drop = set(filter(None, os.environ.get("FAKE_SIDE_DROP", "").split(",")))
fake_devin.MODELS[:] = [
    (*row[:6], row[0] in _free.split(",")) if _free is not None else row for row in fake_devin.MODELS if row[0] not in _drop
]


class SideFake(fake_devin.Fake):
    def m_session_new(self, params: dict[str, Any]) -> Any:
        created = super().m_session_new(params)
        session = self.state["sessions"][created["sessionId"]]
        session["model"], session["thought_level"] = DEFAULT_MODEL, fake_devin.model_row(DEFAULT_MODEL)[4]
        self.save()
        created["configOptions"] = self.options(session)
        return created

    async def on_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method == os.environ.get("FAKE_SIDE_FAIL"):
            self.send({"id": message["id"], "error": {"code": -32603, "message": f"{method} failed on purpose"}})
            return
        slow, _, seconds = os.environ.get("FAKE_SIDE_SLOW", "").partition(":")
        if slow == method:
            await asyncio.sleep(float(seconds))
        await super().on_request(message)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] != ["acp"]:
        return fake_devin.cli(arguments)
    if os.environ.get("FAKE_DEVIN_BROKEN") == "1":
        return 2
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(SideFake(loop).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
