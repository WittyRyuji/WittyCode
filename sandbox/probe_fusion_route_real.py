"""Real requests through the Fusion fixed route with the user's signed-in ChatGPT Plan: four small tasks (advice, a one-line
edit, a multi-step change, two independent deliverables), all three roles on one cheap model with different efforts.

Sessions, projects and the Fusion Drive are in a temp folder; only the account is the real one (its tokens are read and
refreshed exactly as the application does). Never prints a token. Stops before the token cap.

    ..\\..\\.venv\\Scripts\\python.exe sandbox\\probe_fusion_route_real.py [advice] [edit] [coder] [split] [--cap 5000000]
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from functools import cached_property
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.core.chatgpt.engine import ChatGPTEngine  # noqa: E402
from desktop.core.fusion.drive.retention import Retention  # noqa: E402
from desktop.core.fusion.drive.store import Drive  # noqa: E402
from desktop.core.fusion.runtime import FusionRuntime  # noqa: E402
from desktop.core.harness.store import SessionStore  # noqa: E402
from desktop.core.session_state import SessionState  # noqa: E402
from desktop.core.settings import AppSettings, default_settings_path  # noqa: E402
from tests.app_support import make_core  # noqa: E402
from tests.fakes import until  # noqa: E402

ARGS = sys.argv[1:]
CAP = int(ARGS[ARGS.index("--cap") + 1]) if "--cap" in ARGS else 5_000_000
STOP_AT = int(CAP * 0.9)
ONLY = {a for a in ARGS if not a.startswith("-") and not a.isdigit()}
WANT = "terra"
WORK = Path(tempfile.mkdtemp(prefix="wc-fusion-route-probe-"))

# The store belongs to the probe, not to the user's application data.
_drive = cached_property(lambda self: Drive(WORK / "fusion-drive", self.parent.project or None, self.owner))
_drive.__set_name__(FusionRuntime, "drive")
FusionRuntime.drive = _drive
FusionRuntime._drive_stats = lambda self: self.drive.stats(self.retention())

TASKS = {
    "advice": (
        {"calc.py": "def moving_average(values, window):\n    if window <= 0:\n        raise ValueError('window must be positive')\n"
                    "    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]\n"},
        "What does moving_average in calc.py return when window is larger than the list? Answer in two sentences. Do not change any file.",
    ),
    "edit": (
        {"README.md": "# Reports\n\nYou will recieve a summary every morning.\n"},
        "Fix the spelling mistake in README.md.",
    ),
    "coder": (
        {"stats.py": "def mean(values):\n    if not values:\n        raise ValueError('no values')\n    return sum(values) / len(values)\n",
         "test_stats.py": "import unittest\n\nfrom stats import mean\n\n\nclass MeanTest(unittest.TestCase):\n    def test_mean(self):\n"
                          "        self.assertEqual(mean([1, 2, 3]), 2)\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
        "Add a median(values) function to stats.py (ValueError for an empty list), add unittest cases to test_stats.py for odd, even "
        "and empty input, and make sure `python -m unittest -q` passes.",
    ),
    "split": (
        {"cli.py": "import argparse\n\n\ndef build():\n    parser = argparse.ArgumentParser(prog='greet')\n"
                   "    parser.add_argument('--name', default='world', help='who to greet')\n    return parser\n\n\n"
                   "def main(argv=None):\n    args = build().parse_args(argv)\n    print(f'hello {args.name}')\n\n\n"
                   "if __name__ == '__main__':\n    main()\n",
         "test_cli.py": "import unittest\n\nfrom cli import build\n\n\nclass CliTest(unittest.TestCase):\n    def test_name(self):\n"
                        "        self.assertEqual(build().parse_args(['--name', 'Ada']).name, 'Ada')\n\n\nif __name__ == '__main__':\n    unittest.main()\n"},
        "Two independent things: (1) add a --version flag to cli.py that prints 1.0.0, with a unittest in test_cli.py; (2) write "
        "docs/USAGE.md that documents the existing --name option. Verify with `python -m unittest -q`.",
    ),
}


def digest(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts}


def used(mains: list) -> int:
    total = 0
    for main in mains:
        total += main.usage.total if main.usage is not None else 0
        for view in main.agents.values():
            if view.controller is not None and view.controller.usage is not None:
                total += view.controller.usage.total
    return total


async def run_turn(core, cid: str, mains: list, timeout: float = 1500.0) -> str:
    """Wait for the turn; answer question cards (the map question: keep it in the project for `coder`, local otherwise)."""
    main = core._by_cid[cid].controller
    end = time.monotonic() + timeout
    await asyncio.sleep(0.5)
    while not (main.state is SessionState.READY and getattr(main, "_turn", None) is None):
        if used(mains) >= STOP_AT:
            await core.call("session.stop", {"id": cid})
            return "stopped: token cap"
        if time.monotonic() > end:
            await core.call("session.stop", {"id": cid})
            return "stopped: time"
        for prompt in main.transcript.pending_prompts():
            questions = getattr(prompt, "questions", None)
            if questions:
                answers = []
                for question in questions:
                    options = list(question.options)
                    pick = 0 if "\u9879\u76ee\u5730\u56fe" in question.question and main.project.name == "coder" else min(1, len(options) - 1)
                    if "\u9879\u76ee\u5730\u56fe" not in question.question:
                        pick = 0
                    answers.append(options[pick])
                    print(f"    question card: {question.question[:60]!r} -> {options[pick]!r}", flush=True)
                main.answer_question(prompt.id, answers)
        await asyncio.sleep(0.5)
    return "done"


def calls(controller) -> list[str]:
    return [item.get("name", "?") for item in controller.saved.items if item.get("type") == "function_call"]


def blocks(controller, kind: str) -> list[str]:
    return [b.text for t in controller.transcript.turns for b in t.blocks if b.kind == kind]


async def main() -> None:
    engine = ChatGPTEngine(AppSettings(), default_settings_path().parent / "chatgpt")
    await engine.ensure_ready()
    engine.sessions = SessionStore(WORK / "sessions")
    settings = AppSettings(max_open_sessions=8, think_chinese=False, fusion_max_seconds=900)
    if "--off" in ARGS:  # (the comparison: the same tasks with every gate off, i.e. Fusion as it was before the fixed route)
        from desktop.core.fusion.gates import GATES
        settings.fusion_gates = {name: False for name in GATES}
    print("gates:", "all off" if "--off" in ARGS else "all on", flush=True)
    core, _factory, events, _platform, _ = make_core(WORK, chatgpt_engine=engine, settings=settings)
    await core.call("app.init")
    await until(lambda: any(m["source"] == "chatgpt" for payload in events.of("models") for m in payload["models"]), 90)
    model = next((m for m in events.last("models")["models"] if m["source"] == "chatgpt" and WANT in m["id"].lower()), None)
    if model is None:
        print("no model matching", WANT, "in:", [m["id"] for m in events.last("models")["models"] if m["source"] == "chatgpt"])
        await core.shutdown()
        return
    efforts = list(model.get("efforts") or [])
    top = "max" if "max" in efforts else "xhigh" if "xhigh" in efforts else (efforts[-1] if efforts else "")
    wanted = {"main": top, "worker": "high", "light": "low"}
    missing = [f"{role}={effort}" for role, effort in wanted.items() if effort not in efforts]
    print("model:", model["id"], "| efforts offered:", efforts, "| used:", wanted, "| cap:", CAP, flush=True)
    if missing:
        print("efforts not offered by this model:", missing)
        await core.shutdown()
        return
    settings.fusion_main, settings.fusion_workers, settings.fusion_light = model["id"], [model["id"]], model["id"]
    settings.fusion_reasoning = {f"{role}:{model['id']}": effort for role, effort in wanted.items()}

    mains: list = []
    results: dict[str, dict] = {}
    for name, (files, request) in TASKS.items():
        if ONLY and name not in ONLY:
            continue
        if used(mains) >= STOP_AT:
            print("token cap reached; remaining tasks not run")
            break
        project = WORK / name
        for path, text in files.items():
            (project / path).parent.mkdir(parents=True, exist_ok=True)
            (project / path).write_text(text, encoding="utf-8")
        settings.add_project(project)
        for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-qm", "Baseline"]):
            subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)  # (a sampler can then read the diff)
        before = digest(project)
        print(f"== {name}: {request[:70]}", flush=True)
        started = time.monotonic()
        cid = (await core.call("session.new", {"projectId": str(project), "fusion": True}))["id"]
        await core.call("session.choose", {"id": cid, "field": "autonomy", "value": "full"})
        controller = core._by_cid[cid].controller
        mains.append(controller)
        await core.call("session.send", {"id": cid, "text": request})
        ended = await run_turn(core, cid, mains)
        fusion = controller._fusion
        records = await asyncio.to_thread(lambda: [c.record for c in fusion.drive.candidates(shared=False, retention=Retention())])
        traces = await asyncio.to_thread(lambda: fusion.drive.stats(Retention()))
        after = digest(project)
        check = None
        if name in ("coder", "split"):
            run = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=project, capture_output=True, text=True, timeout=120)
            check = {"exit": run.returncode, "tail": (run.stderr or run.stdout).strip().splitlines()[-1:]}
        results[name] = {
            "ended": ended, "seconds": round(time.monotonic() - started),
            "autonomy": controller.autonomy, "main_effort": controller.reasoning,
            "main_calls": calls(controller),
            "experts": [{"name": v.name, "model": v.agent_model, "effort": v.agent_effort, "status": v.record.status,
                         "calls": calls(v.controller) if v.controller is not None else None} for v in controller.agents.values()],
            "records": {kind: [r.summary[:160] for r in records if r.kind == kind] for kind in ("route", "report", "verdict", "finding", "map")},
            "record_counts": traces.get("records"), "ratings": {str(k): v for k, v in traces.get("ratings", {}).items()}, "unrated": traces.get("unrated"),
            "notices": [n[:160] for n in blocks(controller, "notice")],
            "errors": [e[:200] for e in blocks(controller, "error")],
            "answer": (blocks(controller, "text") or [""])[-1][:300],
            "metrics": fusion.registry.task_metrics,
            "files_changed": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)),
            "map_file": (project / ".witty_map.md").is_file(),
            "check": check,
            "tokens_so_far": used(mains),
        }
        print(f"   {ended} in {results[name]['seconds']}s | tokens so far: {results[name]['tokens_so_far']:,}", flush=True)

    (WORK / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("total tokens:", f"{used(mains):,}", "of cap", f"{CAP:,}")
    await core.shutdown()
    print("result:", WORK / "result.json")


asyncio.run(main())
