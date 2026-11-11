"""One check against the real ``codex app-server`` installed in ``sandbox/codex-cli`` (skipped when it is not there).

No account, no API keys, and ``CODEX_HOME`` in ``tmp_path``: nothing of the user's own Codex is touched and no model
is called.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from desktop.core.codex.models import models_from_result
from desktop.core.codex.rpc import JsonRpcClient
from tests.codex_support import run

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "sandbox" / "codex-cli" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"


@pytest.mark.skipif(not REAL.is_file(), reason="no real Codex CLI in sandbox/codex-cli (npm install --prefix sandbox/codex-cli @openai/codex)")
def test_the_real_app_server_initializes_and_lists_models_signed_out(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.upper().endswith("_API_KEY") and not key.upper().startswith("OPENAI")}
    env["CODEX_HOME"] = str(tmp_path / "codex-home")
    (tmp_path / "codex-home").mkdir()

    async def scenario():
        client = JsonRpcClient([str(REAL), "app-server"], env=env, cwd=tmp_path)
        await client.start()
        try:
            hello = await client.request("initialize", {"clientInfo": {"name": "wittycode-test", "title": "WittyCode tests", "version": "0"}, "capabilities": None}, timeout=60)
            assert Path(hello["codexHome"]).resolve() == (tmp_path / "codex-home").resolve()
            await client.notify("initialized")
            account = await client.request("account/read", {"refreshToken": False}, timeout=30)
            assert account["account"] is None and account["requiresOpenaiAuth"] is True
            models = models_from_result(await client.request("model/list", {"limit": 100, "includeHidden": False}, timeout=60))
            assert models and all(model.efforts and model.display_name for model in models)
            assert any(model.is_default for model in models)
        finally:
            await client.close()
        assert client.exit_code == 0

    run(scenario())
