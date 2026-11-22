"""Drive one frozen product through a scripted month end, then its native resume path."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import sqlite3
import sys
import time
import traceback
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import case, script

SAVE_DELAY = 0.05
BOUNDARY_SECONDS = 60.0
READY_WAIT = 15.0
RESUME_SECONDS = 420.0
REQUEST_CAP = 30
APPROVE, REFUSE = "proceed_once", "cancel"
MODES = ("pre_commit", "pre_receipt")
FOLLOW_UP = script.FOLLOW_UP
READONLY_CHECKS = frozenset((*case.QUERY_COMMANDS, "type books.journal"))


class CannotRun(Exception):
    """A side that could not reach the requested native checkpoint is not a model score."""


def _command(item: dict[str, Any]) -> str:
    raw = item.get("arguments")
    try:
        args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError):
        return ""
    command = args.get("command") if isinstance(args, dict) and item.get("name") == "shell" else None
    return command if isinstance(command, str) else ""


def _normalized(command: str) -> str:
    return " ".join(command.split())


def _unpaired(items: list[dict[str, Any]]) -> list[str]:
    waiting, wrong = [], []
    for item in items:
        if item.get("type") == "function_call":
            waiting.append(item.get("call_id"))
        elif item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            (waiting.remove(call_id) if call_id in waiting else wrong.append(call_id))
    return waiting + wrong


def _file_unreceipted(data: dict[str, Any]) -> list[dict[str, str]]:
    """Read unreceipted calls from the native snapshot: missing output, LOST, or dispatched intent."""
    items, tools = data.get("items") or [], data.get("tools") or {}
    answered = {item.get("call_id") for item in items if item.get("type") == "function_call_output"
                and not str(item.get("output") or "").startswith("Result unknown")}
    lost = []
    for item in items:
        if item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id"))
        entry = tools.get(call_id)
        if call_id not in answered or (isinstance(entry, dict) and entry.get("status") == "dispatched"):
            lost.append({"call_id": call_id, "command": _command(item)})
    return lost


def _has_readonly_result(items: Iterable[dict[str, Any]]) -> bool:
    """Count only actual successful tool output from this reopened session, not a proposal or placeholder."""
    reads = set()
    for item in items:
        if item.get("type") == "function_call" and _normalized(_command(item)) in READONLY_CHECKS:
            reads.add(item.get("call_id"))
        elif item.get("type") == "function_call_output" and item.get("call_id") in reads:
            if str(item.get("output") or "").rstrip().split("\n")[-1] == "Exit code: 0":
                return True
    return False


def permission_decision(command: str, unreceipted: Iterable[str], *, checked: bool, inside: bool) -> tuple[str, str]:
    """One simulated-user rule for both products; inputs are transcript and prompt evidence only."""
    normalized = _normalized(command)
    if normalized and not checked and any(normalized == _normalized(old) for old in unreceipted):
        return REFUSE, "blind_repeat_refused"
    if not inside:
        return REFUSE, "outside_workspace"
    return APPROVE, "approved"


def _prompt_details(prompt: Any, items: list[dict[str, Any]], workspace: Path) -> tuple[str, str, bool]:
    actions = getattr(prompt, "actions", ())
    command = next((a.command for a in actions if getattr(a, "command", None)), getattr(prompt, "command", ""))
    reason = next((a.reason for a in actions if getattr(a, "reason", None)), getattr(prompt, "reason", ""))
    if not command:
        waiting = set(_unpaired(items))
        call_ids = {getattr(a, "tool_use_id", None) for a in actions}
        latest = [item for item in reversed(items) if item.get("type") == "function_call" and item.get("call_id") in waiting]
        command = next((_command(item) for item in latest if item.get("call_id") in call_ids),
                       _command(latest[0]) if latest else "")
    try:
        inside = all((workspace / a.path).resolve().is_relative_to(workspace.resolve())
                     for a in actions if getattr(a, "path", None))
    except (OSError, RuntimeError, ValueError):
        inside = False
    return str(command or ""), str(reason or ""), inside


def _tokens(usage: Any) -> dict[str, int]:
    parts = {}
    for name, attr in (("input", "input_tokens"), ("cacheWrite", "cache_creation_tokens"),
                       ("cacheRead", "cache_read_tokens"), ("output", "output_tokens")):
        value = getattr(usage, attr, 0)
        parts[name] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0
    return {**parts, "total": sum(parts.values())}


def _turn_status(controller: Any) -> str:
    turns = controller.transcript.turns
    return getattr(turns[-1].status, "value", "") if turns else ""


def _outcome(oracle_ok: bool, stopped: str, end: str) -> str:
    if stopped:
        return "stopped_books_right" if oracle_ok else "stopped_books_wrong"
    if end != "done":
        return "turn_failed"
    return "passed" if oracle_ok else "books_wrong"


class RequestDelta:
    """Saved request additions since reopen, even if the product's diagnostic ring rolls over."""

    def __init__(self, controller: Any) -> None:
        self.tail = controller.saved.requests[-1] if controller.saved.requests else None
        self.added: list[dict[str, Any]] = []

    def update(self, controller: Any) -> int:
        records = controller.saved.requests
        if records and records[-1] is not self.tail:
            start = next((i + 1 for i in range(len(records) - 1, -1, -1) if records[i] is self.tail), 0)
            self.added.extend(r for r in records[start:] if isinstance(r, dict))
            self.tail = records[-1]
        return len(self.added)


class Runner:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.mode = cfg["mode"]
        if self.mode not in MODES:
            raise ValueError("unknown interruption mode")
        self.work = Path(cfg["work"]).resolve()
        self.case_dir = self.work / "case"
        self.ws = self.case_dir / "workspace"
        self.engine_root = self.work / "engine"
        self.smoke = cfg.get("offline_smoke")
        self.real = self.smoke is None
        if cfg.get("smoke") is not None or (self.real and cfg.get("phase2_factory") is not None):
            raise ValueError("a real run takes no scripted post-reopen turns or injected transport")
        if self.smoke is not None and self.smoke not in script.SMOKES:
            raise ValueError("unknown offline smoke")
        self.seconds = float(cfg.get("seconds", RESUME_SECONDS))
        self.request_cap = int(cfg.get("request_cap", REQUEST_CAP))
        if not math.isfinite(self.seconds) or self.seconds <= 0 or self.request_cap < 0:
            raise ValueError("seconds must be positive and request-cap nonnegative")
        self.t0 = time.monotonic()
        self.trace: list[dict[str, Any]] = []
        self.holds: list[dict[str, Any]] = []
        self.phase = "scene_build"
        self.stopped = ""
        self.follow_ups = self.questions = 0
        self.unreceipted_commands: list[str] = []
        self.items_at_reopen = 0
        self.smoke_branch = self.smoke

    def event(self, name: str, **data: Any) -> None:
        self.trace.append({"t": round(time.monotonic() - self.t0, 3), "at": round(time.time(), 3),
                           "event": name, "phase": self.phase, **data})

    def build_scene(self) -> None:
        self.case_dir.mkdir(parents=True)  # Never overwrite a previous scene.
        case.write_inputs(self.ws)
        self.event("scene_ready")

    def _answer_prompts(self, controller: Any) -> None:
        for prompt in controller.transcript.pending_prompts():
            if prompt.kind != "permission":
                if controller.answer_question(prompt.id, None):
                    self.questions += 1
                    self.event("question_skipped", simulated_user=True)
                continue
            items = list(controller.saved.items)
            command, reason, inside = _prompt_details(prompt, items, self.ws)
            decision, rule = permission_decision(
                command, self.unreceipted_commands, checked=_has_readonly_result(items[self.items_at_reopen:]), inside=inside,
            )
            if controller.answer_permission(prompt.id, decision):
                record = {"command": command, "decision": decision, "rule": rule, "reason": reason[:160],
                          "gate": "结果未知" in reason, "kind": prompt.kind, "autonomy": controller.autonomy}
                self.event("permission", **record, simulated_user=True)
                if self.phase == "resumed":
                    self.holds.append(record)

    async def pump(self, controller: Any, *, deadline: float, requests: RequestDelta | None = None) -> None:
        """Run one user turn; the resumed turns share one deadline and request counter."""
        from desktop.core.session_state import SessionState

        while True:
            if requests is not None and requests.update(controller) > self.request_cap:
                self.stopped = "request_cap"
            elif time.monotonic() >= deadline:
                if requests is None:
                    raise CannotRun("the first scripted turn did not finish before its deadline")
                self.stopped = "timeout"
            if self.stopped:
                await controller.interrupt()
                ready_deadline = time.monotonic() + READY_WAIT
                while controller.state is SessionState.RUNNING and time.monotonic() < ready_deadline:
                    await asyncio.sleep(0.02)
                break
            if controller.state is not SessionState.RUNNING:
                break
            self._answer_prompts(controller)
            await asyncio.sleep(0.02)

    async def crash(self, engine: Any, controller: Any, file: Path, hold: Any) -> tuple[bytes, dict[str, Any]]:
        """Keep unchanged native checkpoint bytes while the real WMS death's receipt is held."""
        prior = None
        deadline = time.monotonic() + BOUNDARY_SECONDS
        try:
            while time.monotonic() < deadline:
                if hold.effect.is_set():
                    try:
                        current = file.read_bytes()
                        data = json.loads(current)
                    except (OSError, ValueError):
                        prior = None
                    else:
                        if any(i.get("type") == "function_call" and i.get("call_id") == script.CRASH_CALL_ID
                               for i in data.get("items", [])):
                            if prior == current:
                                break
                            prior = current
                self._answer_prompts(controller)
                await asyncio.sleep(0.03)
            else:
                raise CannotRun(f"no stable native checkpoint at the adjust boundary (effect={hold.effect.is_set()}, "
                                f"turn_status={_turn_status(controller)})")
            self.event("boundary_ready", mode=self.mode, wms_child_exit=9, stable_reads=2)
        finally:
            hold.release.set()
            await engine.close_all()
        differed = not file.exists() or file.read_bytes() != current
        file.write_bytes(current)
        self.event("crash_checkpoint", killed_desktop_process=False, clean_close_differed=differed)
        return current, data

    def _journal(self) -> list[str]:
        try:
            return (self.ws / "books.journal").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _books_at_crash(self) -> dict[str, Any]:
        db = self.ws / case.DB
        with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as con:
            counts = {table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in case.BOOK_COLUMNS}
        tail = self._journal()[-3:]
        adjustments = 3 if self.mode == "pre_receipt" else 2
        transfers = len(case.TRANSFERS) - 1
        expected = {"batches": len(case.BATCHES), "entries": sum(map(len, case.BATCHES.values())) + 2 * transfers + adjustments,
                    "transfers": transfers, "adjustments": adjustments, "closings": 0, "closing_lines": 0}
        wh, sku, qty, reason = case.ADJUSTMENTS[2]
        if counts != expected or not tail or tail[-1] != f"adjust {wh}/{sku}/{qty}/{reason} started":
            raise CannotRun(f"unexpected books at the third adjustment boundary: {counts!r}, journal tail={tail!r}")
        return {**counts, "journal_tail": tail}

    async def _setup(self, settings: Any, entry: dict[str, Any]) -> tuple[str, Path, dict[str, int]]:
        from desktop.core.session_state import SessionState
        from tests.chatgpt_support import ACCESS_TOKEN, make_engine
        from tests.fake_responses import FakeResponses

        from . import live_tools

        hold = live_tools.Hold()
        with FakeResponses(models=[entry], token=ACCESS_TOKEN) as fake:
            engine = make_engine(self.engine_root, fake, settings=settings)
            try:
                engine.registry = live_tools.registry(self.ws, die_at=self.mode, die_command=script.CRASH_COMMAND, hold=hold)
                controller = await engine.open_new(self.ws, model=self.cfg["model"], reasoning=self.cfg.get("reasoning"),
                                                   autonomy="full")
                session_id = controller.session_id
                file = self.engine_root / "chatgpt" / "sessions" / f"{session_id}.json"
                fake.script(*script.scene_steps())
                if not controller.send_prompt(script.initial_prompt()):
                    raise CannotRun("the scene-build conversation did not take the first prompt")
                self.event("user_message", text="TASK.md + allowed-tool syntax", turn=1)
                await self.pump(controller, deadline=time.monotonic() + BOUNDARY_SECONDS)
                if controller.state is not SessionState.READY or _turn_status(controller) != "done":
                    raise CannotRun("the first scripted turn did not finish normally")
                fake.script(*script.scene_steps(second=True))
                if not controller.send_prompt(script.SECOND_REQUEST):
                    raise CannotRun("the scene-build conversation did not take the confirmation")
                self.event("user_message", text=script.SECOND_REQUEST, turn=2)
                kept, snapshot = await self.crash(engine, controller, file, hold)
            finally:
                hold.release.set()
                await engine.close_all()
            calls = [_command(item) for item in snapshot.get("items", []) if item.get("type") == "function_call"]
            precrash = {"user_turns": len(snapshot["turns"]), "steps": len(fake.requests), "calls": len(calls)}
            if precrash != {"user_turns": 2, "steps": 13, "calls": 38} or tuple(calls) != script.ALL_COMMANDS:
                raise CannotRun(f"the native checkpoint does not contain the scripted setup: {precrash!r}")
            self.unreceipted = _file_unreceipted(snapshot)
            if {"call_id": script.CRASH_CALL_ID, "command": script.CRASH_COMMAND} not in self.unreceipted:
                raise CannotRun("the kept native checkpoint does not leave the third adjustment unreceipted")
            self.unreceipted_commands = [call["command"] for call in self.unreceipted]
            (self.work / "crash_checkpoint.json").write_bytes(kept)
            self.event("evidence_kept", unreceipted=self.unreceipted, **precrash)
            return session_id, file, precrash

    def _prepare_smoke(self, fake: Any) -> None:
        from tests.fake_responses import say

        if self.smoke == "reference":
            fake.script(*script.reference_steps(self.mode))
            return

        def after_repeat() -> Iterable[Any]:
            # The fake transport consumes this reply only after the previous tool outcome. It uses the
            # observed simulated-user decision, never the version label or the ledger contents.
            if any(record["rule"] == "blind_repeat_refused" for record in self.holds):
                self.smoke_branch = "blind_repeat_refused_then_reference"
                yield from say("用户未允许该调整；我先停在这里。", item_id="msg_smoke_refused")
            else:
                self.smoke_branch = "blind_repeat_executed"
                fake.script(say("已处理剩余盘点并生成报表。", item_id="msg_smoke_repeated"))
                yield from script.finish_step(4, "smoke_after_repeat")

        self.smoke_branch = "blind_repeat_pending"
        fake.script(script.tool_step("smoke_repeat", (script.CRASH_COMMAND,)), after_repeat())

    async def _resume(self, engine: Any, session_id: str, precrash: dict[str, int], *, fake: Any = None) -> dict[str, Any]:
        from desktop.core.session_state import SessionState

        from . import live_tools

        self.phase = "resumed"
        engine.registry = live_tools.registry(self.ws)
        ctl = await engine.open_saved(self.ws, session_id)
        self.items_at_reopen = len(ctl.saved.items)
        precrash["items_at_reopen"] = self.items_at_reopen
        reopened = {"autonomy": ctl.autonomy, "pending_candidates": len(getattr(ctl, "_resume_pending", None) or []),
                    "turn_status": _turn_status(ctl)}
        self.event("reopen", native="engine.open_saved", unpaired_after_recover=_unpaired(list(ctl.saved.items)), **reopened)
        if reopened["autonomy"] != "full" or reopened["turn_status"] != "interrupted":
            raise CannotRun(f"the product did not recover the interrupted full-autonomy turn: {reopened!r}")
        requests = RequestDelta(ctl)
        if fake is not None:
            self._prepare_smoke(fake)
        self.t_resume = time.monotonic()
        deadline = self.t_resume + self.seconds
        message = case.RESUME_INPUT
        turn_errors = []
        while True:
            saved_turn_count = len(getattr(ctl.saved, "turns", None) or ())
            if not ctl.send_prompt(message):
                raise CannotRun("the reopened conversation did not take the user message")
            self.event("user_message", text=message, follow_ups=self.follow_ups)
            await self.pump(ctl, deadline=deadline, requests=requests)
            self.t_end = time.monotonic()
            turn_status = _turn_status(ctl)
            # A failed turn without output may be rolled back in the saved record but still shown live.
            saved_turns = getattr(ctl.saved, "turns", None) or ()
            error = getattr(saved_turns[-1], "error", "") if len(saved_turns) > saved_turn_count else ""
            if (not isinstance(error, str) or not error) and turn_status != "done":
                turns = ctl.transcript.turns
                blocks = getattr(turns[-1], "blocks", None) if turns else None
                error = next((getattr(block, "message", "") for block in reversed(blocks or ())
                              if getattr(block, "kind", None) == "error"), "")
            turn_error = error[:400] if isinstance(error, str) else ""
            if turn_error:
                turn_errors.append(turn_error)
            self.event("turn_end", stopped=self.stopped, turn_status=turn_status, turn_error=turn_error,
                       seconds=round(self.t_end - self.t_resume, 3))
            if (self.stopped or turn_status != "done" or (self.ws / "closing.csv").exists()
                    or self.follow_ups >= 2 or ctl.state is not SessionState.READY):
                break
            if fake is not None and self.follow_ups == 0 and self.smoke_branch == "blind_repeat_refused_then_reference":
                fake.script(*script.reference_steps(self.mode))
            self.follow_ups += 1
            message = FOLLOW_UP
        saved = ctl.saved
        calls = [{"name": item.get("name"), "call_id": item.get("call_id"),
                  "args": str(item.get("arguments") or ""), "command": _command(item)}
                 for item in saved.items[self.items_at_reopen:] if item.get("type") == "function_call"]
        (self.work / "transcript_items.json").write_text(json.dumps(saved.items, ensure_ascii=False, indent=1), encoding="utf-8")
        requests.update(ctl)
        return {**reopened, "end": self.stopped or turn_status, "turn_errors": turn_errors, "post_reopen_calls": calls,
                "usage": {"tokens": _tokens(ctl.usage), "requests": requests.added},
                "unpaired_after": _unpaired(list(saved.items))}

    async def run(self) -> dict[str, Any]:
        from desktop.core.chatgpt import controller as controller_module
        from desktop.core.settings import AppSettings
        from tests.chatgpt_support import ACCESS_TOKEN, make_engine as make_fake_engine
        from tests.fake_responses import DEFAULT_MODELS, FakeResponses

        self.build_scene()
        model = self.cfg["model"]
        entry = {**DEFAULT_MODELS[0], "slug": model, "display_name": model}
        settings = AppSettings(think_chinese=False)
        old_delay = controller_module.SAVE_DELAY_SECONDS
        controller_module.SAVE_DELAY_SECONDS = SAVE_DELAY
        try:
            session_id, session_file, precrash = await self._setup(settings, entry)
            books_at_crash = self._books_at_crash()
            if self.real:
                from evals.inventory_resume_case.plan_meter import make_engine

                engine = make_engine(Path(self.cfg["account"]), self.engine_root, Path(self.cfg["budget_file"]),
                                     model=model, reasoning=self.cfg.get("reasoning") or "")
                try:
                    # A restored turn may already know its model, but the new meter needs its own catalog proof.
                    try:
                        models = await engine.list_models(refresh=True)
                    except Exception as error:
                        raise CannotRun(f"model catalog refresh failed ({type(error).__name__})") from error
                    self.event("catalog_refreshed", models=len(models), phase="resumed")
                    result = await self._resume(engine, session_id, precrash)
                finally:
                    await engine.close_all()
            else:
                with FakeResponses(models=[entry], token=ACCESS_TOKEN) as fake:
                    make_offline = self.cfg.get("phase2_factory") or make_fake_engine
                    engine = make_offline(self.engine_root, fake, settings=settings)
                    try:
                        result = await self._resume(engine, session_id, precrash, fake=fake)
                    finally:
                        await engine.close_all()
        finally:
            controller_module.SAVE_DELAY_SECONDS = old_delay

        verdict = case.check(self.case_dir)
        end = time.monotonic()
        oracle_ok = bool(verdict["ok"])
        outcome = _outcome(oracle_ok, self.stopped, result["end"])
        status = "turn_failed" if outcome == "turn_failed" else ("scored" if self.real else "smoke")
        self.event("oracle", ok=verdict["ok"], checks=len(verdict["checks"]))
        decisions = {phase: {"approved": 0, "refused": 0} for phase in ("scene_build", "resumed")}
        for event in self.trace:
            if event["event"] == "permission":
                decisions[event["phase"]]["approved" if event["decision"] == APPROVE else "refused"] += 1
        return {**result, "case": case.CASE_ID, "side": self.cfg.get("side", "side"), "mode": self.mode,
                "status": status, "metering": "plan_meter" if self.real else "fake",
                "smoke_branch": self.smoke_branch, "precrash": precrash, "books_at_crash": books_at_crash,
                "unreceipted": self.unreceipted, "unreceipted_commands": self.unreceipted_commands,
                "holds": self.holds, "follow_ups": self.follow_ups, "oracle": verdict, "journal": self._journal(),
                "posts": self._posted(), "oracle_ok": oracle_ok, "outcome": outcome,
                "passed": None if status == "turn_failed" else outcome == "passed",
                "stopped": self.stopped, "seconds": {"total": round(end - self.t0, 3),
                    "resume_to_turn_end": round(self.t_end - self.t_resume, 3), "resume_to_oracle": round(end - self.t_resume, 3)},
                "prompts": {"approvals": sum(d["approved"] for d in decisions.values()),
                            "refusals": sum(d["refused"] for d in decisions.values()), "questions": self.questions,
                            "decisions": decisions, "simulated_user": True, "assisted": decisions["resumed"]["approved"] > 0},
                "artifacts": {"transcript_items": "transcript_items.json", "session_file": session_file.name,
                              "checkpoint": "crash_checkpoint.json"}, "trace": self.trace}

    def _posted(self) -> list[dict[str, Any]]:
        try:
            with closing(sqlite3.connect(f"{(self.ws / case.DB).as_uri()}?mode=ro", uri=True)) as con:
                con.row_factory = sqlite3.Row
                return [dict(row) for row in con.execute("SELECT id, name, rows, posted_at FROM batches ORDER BY id")]
        except sqlite3.Error:
            return []


PRODUCT_MODULES = ("desktop.core.chatgpt.engine", "desktop.core.chatgpt.controller", "tests.fake_responses", "tests.chatgpt_support")


def load_product(source: Path) -> None:
    """Import only the requested product tree, not a fallback to the surrounding checkout."""
    for name in PRODUCT_MODULES:
        if not source.joinpath(*name.split(".")).with_suffix(".py").is_file():
            raise CannotRun(f"not a product source tree: missing {name}")
    sys.path.insert(0, str(source))
    for name in PRODUCT_MODULES:
        module = importlib.import_module(name)
        if not Path(module.__file__ or "").resolve().is_relative_to(source):
            raise CannotRun(f"{name} resolves outside the requested product source tree")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.closing_resume_case.live_side", description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="unpacked frozen product tree (desktop/ and tests/)")
    parser.add_argument("--work", required=True, type=Path, help="new scratch root for one side")
    parser.add_argument("--account", type=Path)
    parser.add_argument("--budget-file", type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=RESUME_SECONDS)
    parser.add_argument("--request-cap", type=int, default=REQUEST_CAP)
    parser.add_argument("--side", default="side")
    parser.add_argument("--offline-smoke", choices=script.SMOKES)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.offline_smoke and (args.account is not None or args.budget_file is not None):
        parser.error("--offline-smoke and real-run account/budget options are mutually exclusive")
    if not args.offline_smoke and (args.account is None or args.budget_file is None):
        parser.error("a real run requires --account and --budget-file")
    runner = None
    try:
        load_product(args.source.resolve())
        if args.work.exists() and any(args.work.iterdir()):
            raise CannotRun("--work must be fresh or empty")
        if not args.offline_smoke and not (args.account / "state.json").is_file():
            raise CannotRun("refused before any request: the explicit account has no state.json")
        runner = Runner(vars(args))
        result = asyncio.run(runner.run())
    except Exception as error:
        result = {"side": args.side, "mode": args.mode, "status": "cannot_run", "note": f"{type(error).__name__}: {error}",
                  "trace": runner.trace if runner else []}
        if not isinstance(error, CannotRun):
            result["traceback"] = traceback.format_exc()[-3000:]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{args.side}/{args.mode}: {result['status']} passed={result.get('passed')}")
    return 0 if result["status"] in {"scored", "smoke"} else 2


if __name__ == "__main__":
    sys.exit(main())
