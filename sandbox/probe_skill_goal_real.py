"""Real requests with the user's signed-in ChatGPT Plan through the real AppCore: a skill the model loads itself, a skill named in the
message, and /goal run to its end (a handful of small requests with a cheap model). Sessions and the made-up home folder are in a
temp folder; the account is the real one (its tokens are read and refreshed exactly as the application does). Never prints a token.

    .\\.venv\\Scripts\\python.exe sandbox\\probe_skill_goal_real.py [skill] [named] [goal]
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.core.chatgpt.engine import ChatGPTEngine  # noqa: E402
from desktop.core.harness import skills  # noqa: E402
from desktop.core.harness.store import SessionStore  # noqa: E402
from desktop.core.session_manager import SessionState  # noqa: E402
from desktop.core.settings import AppSettings, default_settings_path  # noqa: E402
from tests.app_support import make_core  # noqa: E402
from tests.fakes import until  # noqa: E402

ONLY = {a for a in sys.argv[1:] if not a.startswith("-")}
WORK = Path(tempfile.mkdtemp(prefix="wc-skill-goal-probe-"))
HOME = WORK / "home"
PROJECT = WORK / "project"
PROJECT.mkdir()
skills.USER_HOME = HOME  # (the made-up home: the user's real skills are not part of this)

SKILL = HOME / ".factory" / "skills" / "codeword-lookup"
SKILL.mkdir(parents=True)
(SKILL / "SKILL.md").write_text(
    "---\nname: codeword-lookup\ndescription: Looks up the secret codeword of the Pelican project. Use when the user asks for the codeword.\n---\n\n"
    "The codeword of the Pelican project is PELICAN-7. Answer with the codeword in capital letters and nothing else.\n",
    encoding="utf-8",
)


def texts(core, cid: str) -> list[str]:
    turns = core._by_cid[cid].controller.transcript.turns
    return [b.text for t in turns for b in t.blocks if b.kind == "text"]


def tools(core, cid: str) -> list[str]:
    turns = core._by_cid[cid].controller.transcript.turns
    return [getattr(call, "name", None) or getattr(call, "tool_name", "?") for t in turns for call in (t.tools.values() if isinstance(t.tools, dict) else t.tools)]


def notices(core, cid: str) -> list[str]:
    turns = core._by_cid[cid].controller.transcript.turns
    return [b.text for t in turns for b in t.blocks if b.kind == "notice"]


async def idle(core, cid: str, timeout: float = 240.0) -> None:
    end = time.monotonic() + timeout
    controller = core._by_cid[cid].controller
    await asyncio.sleep(0.5)
    while not (controller.state is SessionState.READY and getattr(controller, "_turn", None) is None and core._by_cid[cid].goal is None):
        if time.monotonic() > end:
            raise TimeoutError("did not settle")
        await asyncio.sleep(0.2)


async def main() -> None:
    api = "--api" in sys.argv
    providers = None
    if api:
        from desktop.core.llm.backend import ApiBackend
        from desktop.core.providers import ProviderStore

        providers = ProviderStore()
        engine = ChatGPTEngine(AppSettings(), WORK / "chatgpt", api=ApiBackend(providers))
    else:
        engine = ChatGPTEngine(AppSettings(), default_settings_path().parent / "chatgpt")
        await engine.ensure_ready()
    engine.sessions = SessionStore(WORK / "sessions")
    core, _factory, events, _platform, _ = make_core(WORK, chatgpt_engine=engine, providers=providers)
    await core.call("app.init")
    source = (lambda m: m["source"].startswith("api:")) if api else (lambda m: m["source"] == "chatgpt")
    await until(lambda: any(source(m) for payload in events.of("models") for m in payload["models"]), 90)
    plan_models = [m for m in events.last("models")["models"] if source(m)]
    cheap = next((m for m in plan_models if "luna" in m["id"].lower() or "astra" in m["id"].lower()), None) or plan_models[-1]
    print("model:", cheap["id"], "| route:", "API provider" if api else "ChatGPT Plan")

    async def fresh() -> str:
        cid = (await core.call("session.new", {"projectId": str(PROJECT), "modelId": cheap["id"]}))["id"]
        await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "full"})
        await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "low"})
        return cid

    if not ONLY or "skill" in ONLY:
        print("== the model loads a skill itself")
        cid = await fresh()
        await core.call("session.send", {"id": cid, "text": "What is the codeword of the Pelican project? Use whatever skill fits."})
        await idle(core, cid)
        print("  tools:", tools(core, cid), "| answer:", texts(core, cid)[-1:])

    if not ONLY or "named" in ONLY:
        print("== a skill named in the message")
        cid = await fresh()
        await core.call("session.send", {"id": cid, "text": "/codeword-lookup please"})
        await idle(core, cid)
        print("  tools:", tools(core, cid), "| answer:", texts(core, cid)[-1:], "| notices:", notices(core, cid))

    if not ONLY or "goal" in ONLY:
        print("== /goal")
        cid = await fresh()
        target = PROJECT / "goal_test.txt"
        result = await core.call("session.goal", {"id": cid, "text": "Create a file named goal_test.txt in the project containing exactly the word done, read it back to check it, then finish."})
        print("  start:", result)
        await idle(core, cid, timeout=420.0)
        controller = core._by_cid[cid].controller
        print("  turns:", len(controller.transcript.turns), "| file:", target.exists() and target.read_text(encoding="utf-8").strip())
        print("  notices:", [n[:100] for n in notices(core, cid)])

    await core.shutdown()
    print("work folder:", WORK)


asyncio.run(main())
