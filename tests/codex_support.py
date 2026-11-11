"""Helpers for the Codex engine tests: an engine wired to ``tests/fake_codex.py`` (no network, no real Codex)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from desktop.core.codex.engine import CodexEngine
from desktop.core.codex.locate import CodexBinary
from desktop.core.codex.rpc import JsonRpcClient
from desktop.core.settings import AppSettings

FAKE = Path(__file__).with_name("fake_codex.py")
_ENGINES: list[CodexEngine] = []


def run(coroutine: Any) -> Any:
    """``asyncio.run``, closing every engine made meanwhile even when the test fails (no orphaned fake processes)."""

    async def main() -> Any:
        try:
            return await coroutine
        finally:
            engines, _ENGINES[:] = list(_ENGINES), []
            for engine in engines:
                with contextlib.suppress(Exception):
                    await engine.close_all()

    return asyncio.run(main())


def fake_env(home: Path, **extra: Any) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_CODEX_")}
    env["FAKE_CODEX_HOME"] = str(home)
    env.update({key: str(value) for key, value in extra.items()})
    return env


def fake_binary() -> CodexBinary:
    return CodexBinary(path=FAKE, version="0.165.0", source="test", command=(sys.executable, str(FAKE)))


def fake_client(home: Path, **extra: Any) -> JsonRpcClient:
    return JsonRpcClient([sys.executable, str(FAKE), "app-server"], env=fake_env(home, **extra))


@dataclass
class Harness:
    engine: CodexEngine
    home: Path
    opened: list[str] = field(default_factory=list)
    clients: list[JsonRpcClient] = field(default_factory=list)
    accounts: list[dict[str, Any]] = field(default_factory=list)

    def received(self) -> list[dict[str, Any]]:
        path = self.home / "received.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def requests(self, method: str) -> list[dict[str, Any]]:
        """The params of every request ``method`` the fake got (all its processes)."""
        return [message.get("params") or {} for message in self.received() if message.get("method") == method]

    def answers(self) -> list[dict[str, Any]]:
        """What the engine answered to the fake's own requests (approvals, questions)."""
        return [message for message in self.received() if "method" not in message]


def make_engine(
    tmp_path: Path,
    *,
    account: str = "chatgpt",
    settings: AppSettings | None = None,
    home: Path | None = None,
    login_timeout: float = 300.0,
    **env: Any,
) -> Harness:
    home = home or tmp_path / "codex-home"
    environment = fake_env(home, FAKE_CODEX_ACCOUNT=account, **env)
    harness = Harness(engine=None, home=home)  # type: ignore[arg-type]

    def factory(binary: CodexBinary, **handlers: Any) -> JsonRpcClient:
        client = JsonRpcClient([*binary.command, "app-server"], env=environment, **handlers)
        harness.clients.append(client)
        return client

    harness.engine = CodexEngine(
        settings or AppSettings(),
        locate=lambda explicit: fake_binary(),
        client_factory=factory,
        open_url=harness.opened.append,
        login_timeout=login_timeout,
    )
    harness.engine.add_account_listener(harness.accounts.append)
    _ENGINES.append(harness.engine)
    return harness


async def until(predicate: Any, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def finish_turn(controller: Any, text: str, attachments: Any = (), timeout: float = 10.0) -> Any:
    """Send ``text`` and wait until the session is ready again; returns the turn."""
    assert controller.send_prompt(text, attachments), f"send_prompt refused in state {controller.state}"
    await until(lambda: controller.state.value != "running", timeout)
    return controller.transcript.turns[-1]
