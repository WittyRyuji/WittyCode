"""One live side of the inventory resume case: a real product session interrupted mid-post, natively reopened,
then finished by the real model through the scene's single restricted business tool.

Run as a module against an unpacked frozen product tree (one ``--source`` per side; the pair is two invocations)::

    python -m evals.inventory_resume_case.live_side \
        --source <frozen product tree> --work <new root> --account <chatgpt account dir> \
        --budget-file <shared budget file> --mode pre_commit|pre_receipt \
        --model <model id> --reasoning high --out <side result.json>

What is real and what is staged, per the eval brief:

- the scene is ``case.py``'s fixed one: the builder posts batches 1-3 for real; no batch-4 attempt is scripted
  into the books;
- batch-4's post is a *deterministic proposal* through the frozen product's own controller, tools, permission and
  hook path — the transport fake answers once with a fixed ``shell`` call, and that turn is never counted as a
  model turn. The ledger child dies at the mode's own ``LEDGER_DIE_AT`` boundary (a real interrupted transaction),
  and the file kept is the product's own pre-dispatch checkpoint: a ``function_call`` with ``dispatched`` intent
  and no receipt. No desktop process is killed — the evidence is the kept checkpoint bytes plus the dead child;
- the reopen is the frozen version's own ``engine.open_saved`` on a fresh engine — the metering group's
  ``plan_meter.make_engine(account, root, budget, model=..., reasoning=...)`` for a real run (``root`` is the
  side's engine state root; the engine must keep sessions under ``root/'chatgpt'/'sessions'``, the layout
  ``tests.chatgpt_support.make_engine`` uses), or an injected factory for an offline smoke;
- the resume input is only ``继续``; every proposal after the reopen is the model's — no scripted recovery exists;
- permission prompts and the resume-gate hold are the product's own, answered by the driver as the simulated user
  (each decision traced with its phase); approvals are limited to actions inside the workspace;
- the verdict is ``case.check`` on the real books and ``reconcile.csv``. ``passed`` additionally requires the
  resumed turn was not stopped. ``status`` is ``scored`` or ``cannot_run`` — a failure, a stop and a never-run
  side are recorded differently and a score is never fabricated.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from evals.inventory_resume_case import case

SAVE_DELAY = 0.05
BOUNDARY_SECONDS = 60.0
RESUME_SECONDS = 600.0
READY_WAIT = 15.0
APPROVE, REFUSE = "proceed_once", "cancel"
RESUME = case.RESUME_INPUT
POST_COMMAND = f"python tools/ledger.py --db books.db post inbox/{case.LAST}"
TASK_PROMPT_SUFFIX = "\n\n## 本会话可用工具\n\n只有一个 `shell` 工具，只接受下列字面命令（其他一律拒绝）：\n\n    python tools/ledger.py --db books.db batches\n    python tools/ledger.py --db books.db balances\n    python tools/ledger.py --db books.db post inbox/<批文件>\n    python tools/ledger.py --db books.db reconcile --out reconcile.csv\n    type <文件>   # 读取本仓库固定文件（TASK.md、books.journal、inbox/*、reconcile.csv、tools/ledger.py）\n    dir | dir inbox   # 列目录\n"


class CannotRun(Exception):
    """The side could not be driven to or through the checkpoint (never counted as a product failure)."""


def _unpaired(items: list[dict[str, Any]]) -> list[str]:
    """Call ids that break 'every function_call has exactly one output after it'."""
    waiting, wrong = [], []
    for item in items:
        if item.get("type") == "function_call":
            waiting.append(item.get("call_id"))
        elif item.get("type") == "function_call_output":
            (waiting.remove(item["call_id"]) if item["call_id"] in waiting else wrong.append(item["call_id"]))
    return waiting + wrong


def _tokens(usage: Any) -> dict[str, int]:
    if usage is None:
        return {"input": 0, "cacheWrite": 0, "cacheRead": 0, "output": 0, "total": 0}
    parts = {}
    for name, attr in (("input", "input_tokens"), ("cacheWrite", "cache_creation_tokens"), ("cacheRead", "cache_read_tokens"), ("output", "output_tokens")):
        value = getattr(usage, attr, 0)
        parts[name] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0
    return {**parts, "total": sum(parts.values())}


async def _until(what: Any, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not what():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.03)
    return True


def _phase2_factory() -> Any:
    """The metering group's engine factory (``evals.inventory_resume_case.plan_meter.make_engine``),
    imported lazily so this module and its offline smoke do not depend on it."""
    try:
        from evals.inventory_resume_case.plan_meter import make_engine
    except ImportError as error:
        raise CannotRun(f"evals.inventory_resume_case.plan_meter.make_engine is unavailable: {error}") from error
    return make_engine


class Runner:
    """Drives one side to its result; keeps the trace."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.work = Path(cfg["work"])
        self.case_dir = self.work / "case"
        self.ws = self.case_dir / "workspace"
        self.engine_root = self.work / "engine"
        self.mono0 = time.monotonic()
        self.t0 = time.time()
        self.trace: list[dict[str, Any]] = []
        self.stopped = ""
        self.approvals = self.refusals = self.questions = 0
        self.phase = "scene_build"
        self.decisions = {"scene_build": {"approved": 0, "refused": 0}, "resumed": {"approved": 0, "refused": 0}}
        self.real = cfg.get("phase2_factory") is None  # the real run goes through plan_meter, never a script
        self.smoke = cfg.get("smoke")  # test hook: scripts the post-reopen transport; banned for a real run
        if self.real and self.smoke:
            raise ValueError("a real run takes no scripted post-reopen turns")

    def event(self, name: str, **data: Any) -> None:
        self.trace.append({"t": round(time.monotonic() - self.mono0, 3), "at": round(time.time(), 3), "event": name, **data})

    # ------------------------------------------------------------------ the scene (operator-side; the model sees only workspace/)

    def build_scene(self) -> None:
        """``case.prepare`` minus the interrupted post: batches 1-3 land for real, batch-4 waits for the product."""
        if self.case_dir.exists():
            raise FileExistsError(f"refusing to write into an existing directory: {self.case_dir}")
        ws = self.ws
        (ws / "inbox").mkdir(parents=True)
        (ws / "tools").mkdir()
        (ws / "TASK.md").write_text(case.TASK_MD, encoding="utf-8")
        shutil.copyfile(case.TOOL, ws / "tools" / "ledger.py")
        for name, rows in case.BATCHES.items():
            case._write_csv(ws / "inbox" / name, rows)
        for name in sorted(case.BATCHES):
            if name == case.LAST:
                break
            done = case._tool(ws, "post", f"inbox/{name}")
            if done.returncode != 0:
                raise CannotRun(f"scene build failed posting {name}: {done.stderr or done.stdout}")
        meta = {"case": case.CASE_ID, "mode": self.mode, "workspace": "workspace", "resume_input": RESUME,
                "batches": sorted(case.BATCHES), "interrupted": case.LAST,
                "note": "batch-4 is posted by the product session itself, through the restricted tool"}
        (self.case_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        self.event("scene_ready", workspace=str(ws), posted=sorted(case.BATCHES)[:-1])

    # ------------------------------------------------------------------ inside the conversation

    def _allowed(self, prompt: Any) -> bool:
        for action in prompt.actions:
            if action.path and not Path(action.path).resolve().is_relative_to(self.ws.resolve()):
                return False
        return True

    def _answer_prompts(self, controller: Any, *, approve: bool) -> None:
        """The simulated user of a turn: allows what stays inside the workspace, refuses the rest, skips questions.
        ``approve=False`` while the crash boundary must leave a call unanswered (never used here — the crash call
        is approved like any other), and every decision is traced with its phase and the simulated mark."""
        for prompt in controller.transcript.pending_prompts():
            if prompt.kind == "permission":
                allowed = approve and self._allowed(prompt)
                if controller.answer_permission(prompt.id, APPROVE if allowed else REFUSE):
                    self.approvals, self.refusals = self.approvals + allowed, self.refusals + (not allowed)
                    self.decisions[self.phase]["approved" if allowed else "refused"] += 1
                    self.event("permission", phase=self.phase, decision="approved" if allowed else "refused",
                               simulated_user=True, gate_armed=bool(getattr(controller, "_resume_pending", None)))
            elif controller.answer_question(prompt.id, None):
                self.questions += 1
                self.event("question_skipped", phase=self.phase, simulated_user=True)

    async def pump(self, controller: Any, *, seconds: float) -> None:
        from desktop.core.session_state import SessionState

        deadline = time.monotonic() + seconds
        while controller.state is SessionState.RUNNING:
            self._answer_prompts(controller, approve=True)
            if time.monotonic() >= deadline:
                self.stopped = "timeout"
            if self.stopped:
                await controller.interrupt()
                break
            await asyncio.sleep(0.05)
        await _until(lambda: controller.state is SessionState.READY, seconds=READY_WAIT)

    # ------------------------------------------------------------------ phase 1: the deterministic proposal, through the real product

    async def crash(self, engine: Any, controller: Any, session_file: Path, hold: Any) -> None:
        """Wait for the post's real effect while the receipt is still held, keep the checkpoint file bytes, close
        cleanly, put the kept file back — the file a crash leaves, written by the product itself."""
        from desktop.core.session_state import SessionState

        try:
            deadline = time.monotonic() + BOUNDARY_SECONDS
            while not (hold.effect.is_set() and self._has_call(session_file)):
                if time.monotonic() > deadline:
                    detail = (f"effect={hold.effect.is_set()}, file_has_call={self._has_call(session_file)}, "
                              f"state={getattr(controller.state, 'value', controller.state)}")
                    raise CannotRun(f"the crash boundary never held within {BOUNDARY_SECONDS:.0f}s ({detail})")
                self._answer_prompts(controller, approve=True)
                await asyncio.sleep(0.03)
            self.event("boundary_ready", mode=self.mode, ledger_child="killed by its own LEDGER_DIE_AT hook")
            kept = session_file.read_bytes()
        finally:
            hold.release.set()  # never leave the scene-build call blocked, whatever the outcome
        await engine.close_all()
        differed = (not session_file.exists()) or session_file.read_bytes() != kept
        session_file.write_bytes(kept)
        self.event("crash_checkpoint",
                   mechanism="kept the product's pre-dispatch checkpoint bytes while the tool's receipt was held; "
                             "the ledger child really died at the commit boundary; a clean engine close ran in between",
                   killed_desktop_process=False, clean_close_differed=differed)
        if not differed:
            raise CannotRun("the clean close left the session file unchanged — nothing interrupted-looking was written")

    def _file_items(self, file: Path) -> list[dict[str, Any]]:
        try:
            return json.loads(file.read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            return []

    def _has_call(self, file: Path) -> bool:
        return any(item.get("type") == "function_call" for item in self._file_items(file))

    def _file_unreceipted(self, file: Path) -> list[str]:
        """The call ids the checkpoint shows as unreceipted, judged only from the file's own items: a call with no
        ``function_call_output`` (the earlier file version leaves none), one whose only output is the product's
        LOST placeholder (``_snapshot`` pairs a still-running call), or one whose intent record says
        ``dispatched`` (newer files, when they record it). Never inferred, never written by this driver."""
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") or []
        tools = data.get("tools") or {}
        answered = {item.get("call_id") for item in items
                    if item.get("type") == "function_call_output"
                    and not str(item.get("output") or "").startswith("Result unknown")}
        unreceipted = []
        for item in items:
            if item.get("type") != "function_call":
                continue
            call_id = str(item.get("call_id"))
            entry = tools.get(call_id)
            if call_id not in answered or (isinstance(entry, dict) and entry.get("status") == "dispatched"):
                unreceipted.append(call_id)
        return unreceipted

    # ------------------------------------------------------------------ the run

    async def run(self) -> dict[str, Any]:
        import desktop.core.chatgpt.controller as controller_module
        from desktop.core.settings import AppSettings
        from tests.chatgpt_support import ACCESS_TOKEN, make_engine
        from tests.fake_responses import DEFAULT_MODELS, FakeResponses, call

        from evals.inventory_resume_case import live_tools

        controller_module.SAVE_DELAY_SECONDS = SAVE_DELAY
        self.build_scene()
        cfg = self.cfg
        result: dict[str, Any] = {"side": cfg.get("side", "side"), "case": case.CASE_ID, "mode": self.mode,
                                  "status": "scored", "metering": "plan_meter" if self.real else "fake"}
        hold = live_tools.Hold()
        model = cfg["model"]
        entry = {**DEFAULT_MODELS[0], "slug": model, "display_name": model}
        with FakeResponses(models=[entry], token=ACCESS_TOKEN) as fake:
            # The deterministic scene-build proposal: exactly one scripted answer through the frozen product's own
            # controller, tools, permission and hook path — labelled fake metering, never a model score.
            fake.script(call("shell", {"command": POST_COMMAND}, call_id="call_crash_post", item_id="fc_crash_post"))
            engine = make_engine(self.engine_root, fake, settings=AppSettings(think_chinese=False))
            engine.registry = live_tools.registry(self.ws, die_at=self.mode, hold=hold)
            controller = await engine.open_new(self.ws, model=model, reasoning=cfg.get("reasoning"), autonomy="code")
            session_file = self.engine_root / "chatgpt" / "sessions" / f"{controller.session_id}.json"
            prompt = case.TASK_MD + TASK_PROMPT_SUFFIX
            if not controller.send_prompt(prompt):
                raise CannotRun("the scene-build conversation did not take the prompt")
            self.event("user_message", text="TASK.md + allowed-tool syntax", phase="scene_build")
            await self.crash(engine, controller, session_file, hold)
            session_id = controller.session_id
            unreceipted = self._file_unreceipted(session_file)
            self.event("evidence_kept", session_id=session_id, unreceipted=unreceipted,
                       session_file=session_file.name)
            if "call_crash_post" not in unreceipted:
                raise CannotRun(f"the kept checkpoint does not leave the post unreceipted: {unreceipted}")

            # Phase 2: the version's own open_saved; from here on every proposal is the model's.
            factory = cfg.get("phase2_factory") or _phase2_factory()
            engine2 = factory(Path(cfg["account"]), self.engine_root, Path(cfg["budget_file"]),
                              model=model, reasoning=cfg.get("reasoning") or "")
            engine2.registry = live_tools.registry(self.ws)  # the trusted copy: no die hook, no hold
            self.phase = "resumed"
            reopened = await engine2.open_saved(self.ws, session_id)
            pending = getattr(reopened, "_resume_pending", None)
            turn = reopened.transcript.turns[-1] if reopened.transcript.turns else None
            self.event("reopen", session_id=session_id, native="engine.open_saved",
                       pending_candidates=len(pending or []),
                       turn_status=getattr(getattr(turn, "status", None), "value", ""),
                       unpaired_after_recover=_unpaired(list(reopened.saved.items)))
            items_at_reopen = len(reopened.saved.items)
            requests_at_reopen = len([r for r in reopened.saved.requests if isinstance(r, dict)])
            if self.smoke:
                self.event("smoke_scripted_turns", note="offline smoke only: the post-reopen transport is scripted")
                self.smoke()
            t_resume = time.time()
            if not reopened.send_prompt(RESUME):
                raise CannotRun("the reopened conversation did not take the resume message")
            self.event("user_message", text=RESUME, phase="resumed")
            await self.pump(reopened, seconds=float(cfg.get("seconds") or RESUME_SECONDS))
            self.event("turn_end", stopped=self.stopped or "done",
                       seconds=round(time.time() - t_resume, 3))
            result.update(self._measure(reopened, items_at_reopen, requests_at_reopen))
            (self.work / "transcript_items.json").write_text(
                json.dumps(list(reopened.saved.items), ensure_ascii=False, indent=1), encoding="utf-8")
            result["artifacts"] = {"transcript_items": "transcript_items.json", "session_file": session_file.name}
            await engine2.close_all()

        # The external verdict: real books + reconcile.csv against the fixed inbox ground truth.
        t_check = time.time()
        verdict = case.check(self.case_dir)
        self.event("oracle", ok=verdict["ok"], checks=len(verdict["checks"]), at_seconds=round(time.time() - t_check, 3))
        result["oracle"] = verdict
        result["posts"] = self._posted()
        result["journal"] = self._journal()
        result["passed"] = bool(verdict["ok"]) and not self.stopped and result.get("end") == "done"
        result["stopped"] = self.stopped
        result["seconds"] = {"total": round(time.time() - self.t0, 3),
                             "resume_to_turn_end": next((e.get("seconds") for e in self.trace if e["event"] == "turn_end"), None)}
        result["prompts"] = {"approvals": self.approvals, "refusals": self.refusals, "questions": self.questions,
                             "decisions": self.decisions, "simulated_user": True,
                             "assisted": self.decisions["resumed"]["approved"] > 0}
        result["trace"] = self.trace
        return result

    def _posted(self) -> list[dict[str, Any]]:
        import sqlite3

        db = self.ws / case.DB
        if not db.is_file():
            return []
        try:
            with sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True) as con:
                return [{"id": i, "name": n, "rows": r, "posted_at": t} for i, n, r, t in
                        con.execute("SELECT id, name, rows, posted_at FROM batches ORDER BY id")]
        except sqlite3.Error:
            return []

    def _journal(self) -> list[str]:
        try:
            return [line for line in (self.ws / "books.journal").read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []

    def _measure(self, controller: Any, items_at_reopen: int, requests_at_reopen: int) -> dict[str, Any]:
        saved = controller.saved
        calls = []
        for item in list(saved.items)[items_at_reopen:]:
            if item.get("type") == "function_call":
                calls.append({"name": item.get("name"), "args": str(item.get("arguments") or "")[:200]})
        # Only the resumed phase's own requests: the checkpoint file also holds the scene build's fake request,
        # which is deterministic proposal traffic, never the model's recovery usage.
        records = [r for r in saved.requests if isinstance(r, dict)][requests_at_reopen:]
        turn = controller.transcript.turns[-1] if controller.transcript.turns else None
        return {
            "usage": {"tokens": _tokens(controller.usage), "requests": records},
            "end": self.stopped or getattr(getattr(turn, "status", None), "value", "?"),
            "post_reopen_calls": calls,
            "unpaired_after": _unpaired(list(saved.items)),
        }


# ------------------------------------------------------------------ the CLI

PRODUCT_MODULES = (
    "desktop.core.chatgpt.engine",
    "desktop.core.chatgpt.controller",
    "tests.fake_responses",
    "tests.chatgpt_support",
)


def load_product(source: Path) -> None:
    """Put the frozen product tree first on ``sys.path`` and prove the product actually loads from it —
    never the active checkout."""
    sys.path.insert(0, str(source))
    for name in PRODUCT_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as error:
            raise CannotRun(f"the frozen tree lacks {name} ({error}); it must ship product code and test fixtures") from error
        file = Path(module.__file__ or "").resolve()
        if not file.is_relative_to(source):
            raise CannotRun(f"{name} resolves to {file}, outside {source} — the active product must not be loaded")


def build_parser() -> argparse.ArgumentParser:
    arg = argparse.ArgumentParser(prog="python -m evals.inventory_resume_case.live_side",
                                description=__doc__.splitlines()[0] if __doc__ else "")
    arg.add_argument("--source", required=True, help="unpacked frozen product tree under test (its desktop/ + tests/)")
    arg.add_argument("--work", required=True, help="new root for this side's scene, engine state and artifacts")
    arg.add_argument("--account", required=True, help="the authorized ChatGPT account directory (credentials only)")
    arg.add_argument("--budget-file", required=True, help="the shared budget file both sides meter against")
    arg.add_argument("--mode", required=True, choices=case.MODES)
    arg.add_argument("--model", required=True)
    arg.add_argument("--reasoning", default="high")
    arg.add_argument("--out", required=True, help="where the side result JSON is written")
    arg.add_argument("--seconds", type=float, default=RESUME_SECONDS, help="deadline for the resumed turn")
    arg.add_argument("--side", default="side", help="label in the result JSON")
    return arg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.source).resolve()
    work = Path(args.work)
    out = Path(args.out)

    def emit(result: dict[str, Any]) -> int:
        result.setdefault("trace", [])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        line = f"{result.get('side', 'side')}/{result.get('mode', '?')}: {result.get('status')}"
        if result.get("status") == "scored":
            line += f" passed={result.get('passed')}"
        else:
            line += f" ({str(result.get('note', ''))[:160]})"
        print(line)
        return 0 if result.get("status") == "scored" else 2

    if not (source / "desktop" / "core").is_dir():
        return emit({"side": args.side, "mode": args.mode, "status": "cannot_run",
                     "note": f"{source} holds no desktop/core — not a frozen product tree"})
    if work.exists() and any(work.iterdir()):
        return emit({"side": args.side, "mode": args.mode, "status": "cannot_run",
                     "note": f"--work {work} is not empty; the side needs a fresh root"})
    work.mkdir(parents=True, exist_ok=True)
    account = Path(args.account)
    if not (account / "state.json").is_file():
        return emit({"side": args.side, "mode": args.mode, "status": "cannot_run",
                     "note": f"refused before any request: no signed-in account in {account} (state.json missing)"})
    try:
        load_product(source)
        cfg = {"source": source, "work": work, "mode": args.mode, "model": args.model,
               "reasoning": args.reasoning, "account": account, "budget_file": Path(args.budget_file),
               "seconds": args.seconds, "side": args.side, "phase2_factory": None}
        result = asyncio.run(Runner(cfg).run())
    except CannotRun as error:
        result = {"side": args.side, "mode": args.mode, "status": "cannot_run", "note": str(error)}
    except Exception as error:
        result = {"side": args.side, "mode": args.mode, "status": "cannot_run",
                  "note": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()[-3000:]}
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
