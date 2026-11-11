"""What a Codex thread's rollout file says about token use and settings, and a resumed thread showing it."""

from __future__ import annotations

import json

from desktop.core.codex import rollout
from desktop.core.codex.rollout import read_rollout
from desktop.core.session_manager import SessionState
from tests.codex_support import finish_turn, make_engine, run


def counts(total: int, *, cached: int = 0, output: int = 10) -> dict:
    return {
        "input_tokens": total - output, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
        "output_tokens": output, "reasoning_output_tokens": 3, "total_tokens": total,
    }


def token_count(total: int, last: int, *, window: int = 475_000, info: bool = True) -> str:
    event = {
        "type": "token_count",
        "info": {"total_token_usage": counts(total, cached=total // 2), "last_token_usage": counts(last), "model_context_window": window} if info else None,
        "rate_limits": None,
    }
    return json.dumps({"timestamp": "2026-10-30T07:11:48.220Z", "type": "event_msg", "payload": event}, separators=(",", ":"))


def turn_context(model: str = "gpt-6.1-sol", effort: str = "max", sandbox: str = "danger-full-access") -> str:
    payload = {"turn_id": "t", "model": model, "effort": effort, "approval_policy": "never", "sandbox_policy": {"type": sandbox}}
    return json.dumps({"timestamp": "2026-10-30T02:36:56.350Z", "type": "turn_context", "payload": payload}, separators=(",", ":"))


def filler(size: int = 100) -> str:
    return json.dumps({"type": "response_item", "payload": {"type": "message", "text": "x" * size}}, separators=(",", ":"))


def write(path, *lines: str) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_the_newest_figures_and_the_settings_of_the_last_turn_are_read_from_the_end(tmp_path):
    path = write(
        tmp_path / "rollout.jsonl",
        turn_context("old-model", "low", "read-only"), token_count(1_000, 400), filler(),
        turn_context("gpt-6.1-sol", "max", "danger-full-access"), token_count(2_500_000, 202_146), filler(),
    )
    facts = read_rollout(path)
    assert (facts.model, facts.effort, facts.level) == ("gpt-6.1-sol", "max", "full")
    usage = facts.token_usage
    assert usage["total"]["inputTokens"] == 2_499_990 and usage["total"]["cachedInputTokens"] == 1_250_000, "camelCase, as the notification has it"
    assert usage["total"]["cacheWriteInputTokens"] == 0 and usage["total"]["totalTokens"] == 2_500_000
    assert usage["last"]["totalTokens"] == 202_146 and usage["modelContextWindow"] == 475_000


def test_a_token_count_without_figures_is_skipped_for_the_one_before_it(tmp_path):
    path = write(tmp_path / "rollout.jsonl", token_count(900, 300), turn_context(), token_count(0, 0, info=False))
    assert read_rollout(path).token_usage["total"]["totalTokens"] == 900


def test_every_permission_the_file_can_say_is_the_page_level(tmp_path):
    levels = {}
    for name in ("read-only", "workspace-write", "danger-full-access", "something-new"):
        levels[name] = read_rollout(write(tmp_path / "r.jsonl", turn_context(sandbox=name))).level
    assert levels == {"read-only": "readonly", "workspace-write": "workspace", "danger-full-access": "full", "something-new": None}


def test_lines_longer_than_a_block_and_a_file_of_many_blocks_are_read_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(rollout, "CHUNK_BYTES", 512)
    path = write(
        tmp_path / "rollout.jsonl",
        turn_context("first-model"), token_count(1_000, 400), *[filler(300) for _ in range(30)], token_count(77_000, 5_000), filler(5_000),
    )
    facts = read_rollout(path)
    assert facts.token_usage["total"]["totalTokens"] == 77_000, "found behind a line ten blocks long"
    assert facts.model == "first-model", "and the first line of the file is read too"


def test_only_the_end_of_a_huge_file_is_looked_through(tmp_path, monkeypatch):
    monkeypatch.setattr(rollout, "CHUNK_BYTES", 512)
    monkeypatch.setattr(rollout, "SCAN_LIMIT_BYTES", 4_096)
    path = write(tmp_path / "rollout.jsonl", token_count(5, 5), *[filler(300) for _ in range(40)])
    assert read_rollout(path) is None, "the figure is further back than the limit: not known, and no error"


def test_a_file_that_is_missing_empty_or_not_json_is_not_known(tmp_path):
    assert read_rollout(None) is None and read_rollout("") is None
    assert read_rollout(tmp_path / "nothing.jsonl") is None
    assert read_rollout(write(tmp_path / "empty.jsonl")) is None
    assert read_rollout(write(tmp_path / "junk.jsonl", "not json at all", '{"type":"token_count"', '"type":"token_count" ]]')) is None


def seeded(tmp_path, rollout_path):
    """A thread with two turns whose saved record points at ``rollout_path``; returns (home, thread id)."""
    home = tmp_path / "codex-home"

    async def first_run() -> str:
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_new(tmp_path)
        await finish_turn(controller, "hello")
        thread_id = controller.session_id
        await harness.engine.close_all()
        return thread_id

    thread_id = run(first_run())
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    state["threads"][thread_id]["path"] = rollout_path
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return home, thread_id


def test_a_resumed_thread_has_its_token_total_and_context_before_it_says_a_word(tmp_path):
    path = write(tmp_path / "rollout.jsonl", turn_context(), token_count(4_650_072, 202_146), filler())
    home, thread_id = seeded(tmp_path, path)

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_saved(tmp_path, thread_id)
        assert controller.state is SessionState.READY
        tokens = controller.usage_series()["self"]
        assert tokens.total == 4_650_072 and tokens.cache_read_tokens == 2_325_036
        assert controller.context.used == 202_146 and controller.context.window == 475_000, "the context meter has its figures"
        assert harness.engine.model_window(controller.model) == 475_000, "and the window is learned for the model"
        await harness.engine.close_all()

    run(scenario())


def test_what_the_thread_reports_itself_replaces_the_saved_figures(tmp_path):
    path = write(tmp_path / "rollout.jsonl", token_count(1_000_000, 90_000))
    home, thread_id = seeded(tmp_path, path)

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_saved(tmp_path, thread_id)
        assert controller.usage_series()["self"].total == 1_000_000
        await finish_turn(controller, "next")
        assert controller.usage_series()["self"].total != 1_000_000, "the thread's own total, from its own report"
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_is_read_ahead_for_the_cache_without_being_resumed(tmp_path):
    path = write(tmp_path / "rollout.jsonl", turn_context("gpt-6.1-sol", "max", "read-only"), token_count(4_650_072, 202_146), filler())
    home, thread_id = seeded(tmp_path, path)

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        peek = await harness.engine.peek_saved(thread_id)
        (turn,) = peek.transcript.turns
        assert turn.user_block.text == "hello" and turn.from_history
        settings = peek.settings
        assert (settings.model, settings.reasoning, settings.autonomy, settings.mode) == ("gpt-6.1-sol", "max", "readonly", "auto"), (
            "the model from the thread, the reasoning level from its last turn, the permission from the same"
        )
        assert peek.usage.total == 4_650_072 and peek.context_used == 202_146 and peek.window == 475_000
        assert harness.engine.model_window("gpt-6.1-sol") == 475_000
        assert not harness.requests("thread/resume") and not harness.engine.controllers, "read, not opened"
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_without_a_readable_file_is_read_ahead_with_what_codex_itself_says(tmp_path):
    home, thread_id = seeded(tmp_path, str(tmp_path / "gone.jsonl"))

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        peek = await harness.engine.peek_saved(thread_id)
        assert peek.transcript.turns and peek.settings.model == "gpt-6.1-sol" and peek.settings.autonomy == ""
        assert peek.usage is None and peek.context_used is None and peek.window is None
        assert await harness.engine.peek_saved("no-such-thread") is None, "a thread that cannot be read is not an error"
        await harness.engine.close_all()

    run(scenario())


def test_a_thread_whose_file_cannot_be_read_still_opens_with_no_figures(tmp_path):
    home, thread_id = seeded(tmp_path, str(tmp_path / "gone.jsonl"))

    async def scenario():
        harness = make_engine(tmp_path, home=home)
        controller = await harness.engine.open_saved(tmp_path, thread_id)
        assert controller.state is SessionState.READY and controller.usage_series() == {} and controller.context.used is None
        await harness.engine.close_all()

    run(scenario())
