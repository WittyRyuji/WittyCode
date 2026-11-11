"""Mutation checks for the commands typed with a slash (``/compact``, ``/goal``, ``/plan``) in WittyCode's own loop (2026-11-10): the whole-
conversation compaction, the goal's rules and driver, Plan mode in the tools and the instructions, and the AppCore methods that
carry them. Each mutant undoes one part; the tests must catch it.

    python sandbox/mutate_commands.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_commands.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_commands.py 3 7        run just those mutants

The machinery is that of ``mutate_harness_g678.py`` (this working tree is never written to); only the table and the tests differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_harness_g678 as base  # noqa: E402

CORE = Path("desktop") / "core"
HARNESS = CORE / "harness"
COMPACT, GOAL, PROMPT, TOOLSET, STORE = (HARNESS / name for name in ("compact.py", "goal.py", "prompt.py", "toolset.py", "store.py"))
CONTROLLER, ENGINE, APP = CORE / "chatgpt" / "controller.py", CORE / "chatgpt" / "engine.py", CORE / "app_core.py"
SESSIONS, CODEX = CORE / "session_manager.py", CORE / "codex" / "controller.py"

NEW = ["tests/test_harness_commands.py", "tests/test_app_commands.py", "tests/test_codex_turns.py"]
OLDER = ["tests/test_chatgpt_compaction.py", "tests/test_chatgpt_controller.py", "tests/test_harness_compact.py", "tests/test_harness_toolset.py", "tests/test_harness_store.py"]

M = [
    # ---- compaction of the whole conversation
    ("compact: asked for, the newest part still stays", COMPACT, "    if whole and ends_whole(view, synthetic):\n", "    if False and ends_whole(view, synthetic):\n"),
    ("compact: the last message of the user is kept even when no turn runs", COMPACT, "        turn = len(view) if whole else next(", "        turn = next("),
    ("compact: a summary that fails is the digest even when asked for (words)", COMPACT, "    except CompactionFailed as error:\n        if not fallback:\n            raise\n", "    except CompactionFailed as error:\n"),
    ("compact: a request that fails is the digest even when asked for", COMPACT, "        if not fallback:\n            raise\n        log.warning(", "        log.warning("),
    # ---- the goal's rules
    ("goal: any line with the mark ends it", GOAL, "    lines = [line for line in answer.splitlines() if line.strip()]\n    found = _MARK.match(lines[-1]) if lines else None\n", "    found = re.search(_MARK.pattern, answer, re.IGNORECASE | re.MULTILINE)\n"),
    ("goal: done does not end it", GOAL, '    if mark == "done":\n', "    if False:\n"),
    ("goal: blocked does not end it", GOAL, '    if mark == "blocked":\n', "    if False:\n"),
    ("goal: a round that worked does not start the idle count again", GOAL, "    goal.idle = 0 if worked else goal.idle + 1\n", "    goal.idle = goal.idle + 1\n"),
    ("goal: idle rounds never end it", GOAL, "    if goal.idle >= IDLE:\n", "    if False:\n"),
    ("goal: it has no round limit", GOAL, "    if goal.rounds >= ROUNDS:\n", "    if False:\n"),
    ("goal: the rounds are not counted", GOAL, "    goal.rounds += 1\n", "    pass\n"),
    # ---- the instructions
    ("prompt: Plan mode is not in the instructions", PROMPT, "    if plan:\n        parts.append(PLAN_RULES)\n", "    if False:\n        parts.append(PLAN_RULES)\n"),
    ("prompt: the permission level's line stays in Plan mode", PROMPT, 'if plan else LEVEL_RULES.get(level, LEVEL_RULES["ask"])', 'if False else LEVEL_RULES.get(level, LEVEL_RULES["ask"])'),
    # ---- Plan mode in the tools
    ("toolset: Plan mode is ignored", TOOLSET, "            return self._planned(spec, call) if self.plan else self._decided(spec, call)\n", "            return self._decided(spec, call)\n"),
    ("toolset: any MCP tool runs in Plan mode", TOOLSET, "            return Verdict(ALLOW) if tool.harmless else Verdict(DENY, PLAN_REFUSAL)\n", "            return Verdict(ALLOW)\n"),
    ("toolset: a read follows the level in Plan mode", TOOLSET, '            return self._decided(spec, call, "ask", set())\n', "            return self._decided(spec, call)\n"),
    ("toolset: a command asks instead of being refused in Plan mode", TOOLSET, "            if verdict.verdict != ASK:\n                return verdict\n", "            return verdict\n"),
    ("toolset: Plan mode honours what the user allowed for the session", TOOLSET, '            verdict = self._decided(spec, call, "code", set())\n', '            verdict = self._decided(spec, call, "code")\n'),
    ("toolset: a change asks instead of being refused in Plan mode", TOOLSET, "        return Verdict(DENY, PLAN_REFUSAL)\n\n", "        return Verdict(ASK, PLAN_REFUSAL)\n\n"),
    # ---- the mode kept
    ("store: the mode is not written", STORE, '        "mode": session.mode,\n', ""),
    ("store: the mode is not read", STORE, '            mode=str(raw.get("mode") or ""),\n', '            mode="",\n'),
    ("controller: a mode that does not exist is accepted", CONTROLLER, "            if chosen not in MODES:\n", "            if False:\n"),
    ("controller: the mode is not changed", CONTROLLER, "            self.mode = chosen\n", "            pass\n"),
    ("controller: the page is told Auto", CONTROLLER, "        return LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode=self.mode)\n", '        return LiveSettings(model=self.model, reasoning=self.reasoning, autonomy=self.autonomy, mode="auto")\n'),
    ("controller: a reopened conversation forgets its mode", CONTROLLER, '        self.mode = PLAN if saved.mode == PLAN else "auto"\n', '        self.mode = "auto"\n'),
    ("controller: the tools do not know the mode", CONTROLLER, "            tools.plan = plan\n", "            tools.plan = False\n"),
    ("controller: the turn does not read the mode it began with", CONTROLLER, "        plan = (self._turn_mode if self._turn_model else self.mode) == PLAN\n", "        plan = False\n"),
    ("controller: the mode is not in the file (turn) [EQUIVALENT: _snapshot sets it again at every save, the file is only written from there]", CONTROLLER, '            self.saved.mode = PLAN if self.mode == PLAN else ""\n', "            pass\n"),
    ("controller: the mode is not in the file (snapshot) [EQUIVALENT: _run_turn has set it on the same object before any save of a turn; removing both is caught by the reopened-conversation test]", CONTROLLER, '        saved.mode = PLAN if self.mode == PLAN else ""\n        saved.modified_at', "        saved.modified_at"),
    ("controller: a mode chosen during a turn is not explained", CONTROLLER, "        if self.mode != self._turn_mode:\n", "        if False:\n"),
    # ---- /compact in the controller
    ("compaction: it starts while a turn runs", CONTROLLER, '        if self.state is not SessionState.READY:\n            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"\n', ""),
    ("compaction: it starts with nothing to compact", CONTROLLER, '        if not self.saved.items:\n            return "还没有对话可以压缩。"\n', ""),
    ("compaction: the page is not told what is going on", CONTROLLER, '        self.transcript.set_working_state("compacting_conversation")\n        self._turn_task = self._spawn(self._run_compaction(turn))\n', "        self._turn_task = self._spawn(self._run_compaction(turn))\n"),
    ("compaction: the user's last message is kept word for word", CONTROLLER, "turn=len(history) if manual else synthetic + self._turn_start - start", "turn=synthetic + self._turn_start - start"),
    ("compaction: only what does not fit is summarised", CONTROLLER, "                whole=manual,\n", "                whole=False,\n"),
    ("compaction: a failed summary becomes the digest", CONTROLLER, "                fallback=not manual,\n", "                fallback=True,\n"),
    ("compaction: the notice is the automatic one", CONTROLLER, "        if manual:\n            return f\"已按你的要求压缩", "        if False:\n            return f\"已按你的要求压缩"),
    ("compaction: a summary asked again is called too short", CONTROLLER, '("这段对话已经是一段摘要了。" if self.saved.compaction else "对话还太短，摘要不会比原文更小。")', '("这段对话已经是一段摘要了。" if not self.saved.compaction else "对话还太短，摘要不会比原文更小。")'),
    ("compaction: a stop is told as a failure", CONTROLLER, '                self.transcript.abort_turn("压缩已停止，对话没有改动。")\n', '                self.transcript.fail_turn("压缩已停止，对话没有改动。")\n'),
    # ---- /goal in the controller
    # ---- /goal, driven by AppCore for every engine
    ("goal app: it starts with no text", APP, '        if not text:\n            return {"ok": False, "text": "用法：/goal 目标，例如 /goal 让所有测试通过。"}\n', ""),
    ("goal app: it starts while a turn runs", APP, '        if controller.state is not SessionState.READY:\n            return {"ok": False, "text": "会话还没就绪，或这一轮还在运行：结束后再设目标。"}\n', ""),
    ("goal app: it starts in Plan mode", APP, '        if self._page_settings(conv).get("mode") == "spec":\n            return {"ok": False, "text": "Plan 模式只出方案、不动手，做不了目标：先用 /plan 回到 Auto。"}\n', ""),
    ("goal app: it is not told to the user", APP, '            f"目标已设定：{text}', '            f"目标：{text}'),
    ("goal app: cancelling leaves it in force", APP, '            conv.goal = None\n            return {"ok": True, "text": "目标已取消。"}\n', '            return {"ok": True, "text": "目标已取消。"}\n'),
    ("goal app: the first message of the turn is read, not the last", APP, 'goals.step(goal, texts[-1] if texts else "", bool(turn.tools))', 'goals.step(goal, texts[0] if texts else "", bool(turn.tools))'),
    ("goal app: a round never counts as work", APP, 'goals.step(goal, texts[-1] if texts else "", bool(turn.tools))', 'goals.step(goal, texts[-1] if texts else "", False)'),
    ("goal app: a stopped or failed turn leaves it in force", APP, "        if turn is None or turn.status is not TurnStatus.DONE:\n", "        if turn is None:\n"),
    ("goal app: a turn that ends well never goes on", APP, "            elif ended:\n                self._advance_goal(conv)\n", ""),
    ("goal app: a goal goes before the user's queued message", APP, "            elif conv.queue:\n                self._pump(conv)\n            elif ended:\n                self._advance_goal(conv)\n", "            elif ended:\n                self._advance_goal(conv)\n                self._pump(conv)\n"),
    ("goal app: the next turn that cannot be sent leaves the goal in force", APP, '            self._stop_goal(conv, "目标先停下了：下一轮没能发出（会话不能接收消息）。")\n', "            pass\n"),
    # ---- /compact on every engine
    ("compact app: an engine without the operation is asked all the same", APP, '        if start is None:\n            return {"ok": False, "text": f"{engine_label(conv.engine, None)} 没有提供手动压缩：它自己管上下文。"}\n', ""),
    ("compact app: a conversation with nothing in it is compacted", APP, '        if not any(turn.user_block is not None and turn.status is TurnStatus.DONE for turn in conv.controller.transcript.turns):\n            return {"ok": False, "text": "还没有对话可以压缩。"}\n', ""),
    ("compact app: a goal in force is compacted under", APP, '        if conv.goal is not None:\n            return {"ok": False, "text": "目标还在进行：先用「/goal off」取消它，或等它结束，再压缩。"}\n', ""),
    ("compact droid: it starts while a turn runs", SESSIONS, '        if self.state is not SessionState.READY or self._session is None:\n            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"\n', ""),
    ("compact droid: the session Droid made is not taken over", SESSIONS, "            self._session = outcome.session\n            self.session_id = outcome.session.id\n", ""),
    ("compact droid: the context is not marked compacted", SESSIONS, "            self._publish_context(replace(self.context, used=None, compacted=True))\n            self.transcript.add_notice(\n                f\"Droid 压缩了上下文", "            self.transcript.add_notice(\n                f\"Droid 压缩了上下文"),
    ("compact droid: the turn is never finished", SESSIONS, "            self.transcript.finish_turn(turn, TurnStatus.DONE)\n        except asyncio.CancelledError:\n            self.transcript.abort_turn(\"压缩已停止，对话没有改动。\")", "        except asyncio.CancelledError:\n            self.transcript.abort_turn(\"压缩已停止，对话没有改动。\")"),
    ("compact droid: a stop is sent to a session that has nothing to stop", SESSIONS, "        if self._compacting:\n", "        if False:\n"),
    ("compact droid: a failure is told as an internal error", SESSIONS, '            self.transcript.fail_turn("没有压缩，对话没有改动：" + friendly_failure(exc))\n', "            self.transcript.fail_turn(friendly_failure(exc))\n"),
    ("compact codex: it starts while a turn runs", CODEX, '        if self.state is not SessionState.READY or not self.session_id or self._client is None:\n            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"\n', ""),
    ("compact codex: the page is not told what is going on", CODEX, '        self.transcript.set_working_state("compacting_conversation")\n        self._spawn(self._start_compaction(turn))\n', "        self._spawn(self._start_compaction(turn))\n"),
    ("compact codex: a refusal is told as it came", CODEX, '                self.transcript.fail_turn("没有压缩，对话没有改动：" + friendly_failure(error))\n', "                self.transcript.fail_turn(friendly_failure(error))\n"),
    ("compact codex: another request", CODEX, '"thread/compact/start", {"threadId": self.session_id}', '"thread/compact/startx", {"threadId": self.session_id}'),
    # ---- AppCore and the engine
    ("app: /compact behind queued messages", APP, '        if conv.queue:\n            return {"ok": False, "text": "还有排队的消息没有发出：它们发完后再压缩。"}\n', ""),
    ("app: /goal behind queued messages", APP, '        if conv.queue:\n            return {"ok": False, "text": "还有排队的消息没有发出：它们发完后再设目标。"}\n', ""),
    ("app: /goal off starts a goal called off", APP, "        if text.lower() in goals.STOP_WORDS:\n", "        if False:\n"),
    ("app: a new conversation forgets the chosen mode", APP, '                    mode=settings.get("mode", self.settings.mode),\n                    transcript=transcript,\n                    handoff=handoff,\n                )\n            except ChatGPTError', '                    transcript=transcript,\n                    handoff=handoff,\n                )\n            except ChatGPTError'),
    ("engine: a new conversation always starts in Auto", ENGINE, '            mode=PLAN if str(getattr(mode, "value", mode) or "") == PLAN else "auto",\n', '            mode="auto",\n'),
]

if __name__ == "__main__":
    base.M[:] = M
    base.NEW[:] = NEW
    base.OLDER[:] = OLDER
    sys.exit(base.main())
