"""A Devin account whose guarded Fusion processes use a local scripted ACP + real MCP bridge."""
from __future__ import annotations

import sys
from pathlib import Path

from desktop.core.devin.rpc import AcpClient
from tests.devin_support import make_engine


def make_guarded_engine(tmp_path, **options):
    harness = make_engine(tmp_path, **options)
    ordinary = harness.engine._client_factory

    def factory(binary, **handlers):
        env, cwd = handlers.pop("env", None), handlers.pop("cwd", None)
        if "--config" not in binary.command:
            return ordinary(binary, **handlers)
        env = dict(env or {})
        env["FAKE_DEVIN_HOME"] = str(tmp_path / "guarded" / Path(cwd).name)
        config = binary.command[binary.command.index("--config") + 1]
        argv = [sys.executable, "-m", "tests.fake_fusion_acp", "--config", config]
        client = AcpClient(argv, env=env, cwd=cwd, **handlers)
        harness.clients.append(client)
        return client

    harness.engine._client_factory = factory
    return harness
