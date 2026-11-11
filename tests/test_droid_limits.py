"""Where Droid compacts a conversation with a model it does not know, and the window the page shows for it (``core/droid_limits``).
Droid reports 250 000 for both the window and the compaction point of an API provider's model and ignores ``maxContextLimit``; what
moves them is the session's own ``compaction_token_limit``, which stays on the session when the model is changed. The fake session
behaves that way (measured on a real droid 0.251.0)."""

from __future__ import annotations

import asyncio

import pytest
from droid_sdk import DroidError

from desktop.core.droid_limits import DEFAULT_LIMIT, DroidLimits, compaction_point
from tests.fakes import FakeSession
from tests.test_context import make, run, settled

CUSTOM = "custom:Deep-0"


class Providers:
    """What ``ProviderStore`` tells: the window and answer room of the custom models, the user's own compaction points."""

    def __init__(self, models: dict[str, tuple[int | None, int | None]], factory: dict[str, int] | None = None, default: int | None = None) -> None:
        self.models = models
        self.factory = factory or {}
        self.default = default

    def limits_of(self, model: str, display_name: str = "") -> tuple[int | None, int | None]:
        return self.models.get(model, (None, None))

    def factory_compaction(self, model: str) -> int | None:
        return self.factory.get(model, self.default)


# ------------------------------------------------------------------------------------------------ the figure


@pytest.mark.parametrize(
    ("window", "answer", "point"),
    [
        (1_024_000, 128_000, 870_400),  # 85% leaves more than the answer's room
        (128_000, 16_000, 108_800),
        (100_000, 64_000, 75_000),  # a big answer room: at most a quarter of the window is kept back for it
        (200_000, None, 170_000),  # none given: a tenth of the window, and 85% is the lower
        (1_000_000, 400_000, 750_000),
    ],
)
def test_the_compaction_point_leaves_the_answer_its_room(window, answer, point):
    assert compaction_point(window, answer) == point
    assert 0 < compaction_point(window, answer) < window


def test_a_tiny_window_still_has_a_point():
    assert compaction_point(1) >= 1


# ------------------------------------------------------------------------------------------------ the policy


def test_without_providers_nothing_is_decided():
    limits = DroidLimits()
    assert limits.window(CUSTOM) is None and limits.limit(CUSTOM) is None and limits.limit("claude-haiku-5-5") is None


def test_a_custom_model_with_a_window_gets_its_point_one_without_gets_droids_default_and_a_built_in_its_users_own():
    limits = DroidLimits(Providers({CUSTOM: (1_024_000, 128_000), "custom:Plain-0": (None, None)}, {"claude-haiku-5-5": 800_000}, default=300_000))
    assert limits.window(CUSTOM) == 1_024_000 and limits.window("custom:Plain-0") is None and limits.window("claude-haiku-5-5") is None
    assert limits.limit(CUSTOM) == 870_400
    assert limits.limit("custom:Plain-0") == DEFAULT_LIMIT == 250_000
    assert limits.limit("claude-haiku-5-5") == 800_000, "the user's per-model point"
    assert limits.limit("kimi-k3") == 300_000, "else the user's general one"
    assert DroidLimits(Providers({})).limit("kimi-k3") == DEFAULT_LIMIT, "else Droid's own"
    assert limits.limit("") is None


# ------------------------------------------------------------------------------------------------ the session


def open_with(model: str, providers: Providers, tmp_path, **options):
    factory, manager = make(model=model, **options)
    manager.limits = DroidLimits(providers)

    async def go():
        controller = await manager.open_new(tmp_path)
        return factory, manager, controller

    return go


def test_a_model_with_a_window_makes_droid_compact_at_the_point_and_the_page_shows_the_window(tmp_path):
    async def scenario():
        factory, manager, controller = await open_with(CUSTOM, Providers({CUSTOM: (1_024_000, 128_000)}), tmp_path)()
        session = factory.sessions[-1]
        assert session.session_limit == 870_400
        await settled(controller, window=1_024_000)
        assert controller.context.budget == 870_400, "what droid itself compacts at"
        session.last_call_tokens = 59_000
        session.notify({"type": "session_token_usage_changed", "lastCallTokenUsage": {"inputTokens": 59_000, "outputTokens": 0, "cacheReadTokens": 0}})
        await settled(controller, used=59_000)
        assert controller.context.window == 1_024_000, "the figure of a call does not bring droid's 870 400 back"

    run(scenario())


def test_a_model_without_a_window_stays_at_droids_250000(tmp_path):
    async def scenario():
        factory, manager, controller = await open_with(CUSTOM, Providers({}), tmp_path)()
        assert factory.sessions[-1].session_limit == 250_000
        await settled(controller, window=250_000)

    run(scenario())


def test_a_built_in_model_is_left_alone(tmp_path):
    async def scenario():
        factory, manager, controller = await open_with("claude-haiku-5-5", Providers({CUSTOM: (1_024_000, None)}, {"claude-haiku-5-5": 800_000}), tmp_path)()
        session = factory.sessions[-1]
        assert not any("compaction_token_limit" in call for call in session.settings_calls), "nothing was given: droid's own rule stands"
        await controller.update_settings(model="kimi-k3")
        assert not any("compaction_token_limit" in call for call in session.settings_calls)

    run(scenario())


def test_what_the_session_was_given_is_taken_back_when_the_model_is_changed_to_a_built_in_one_and_given_again_when_changed_back(tmp_path):
    async def scenario():
        providers = Providers({CUSTOM: (1_024_000, 128_000)}, {"claude-haiku-5-5": 800_000}, default=250_000)
        factory, manager, controller = await open_with(CUSTOM, providers, tmp_path)()
        session = factory.sessions[-1]
        await controller.update_settings(model="kimi-k3")
        assert session.session_limit == 250_000, "droid would otherwise keep 870 400 for a built-in model that has no entry of its own"
        await settled(controller, window=196_608)
        await controller.update_settings(model="claude-haiku-5-5")
        assert session.session_limit == 800_000
        await settled(controller, window=872_000)
        await controller.update_settings(model=CUSTOM)
        assert session.session_limit == 870_400
        await settled(controller, window=1_024_000)

    run(scenario())


def test_a_resumed_session_is_given_its_point_again(tmp_path):
    async def scenario():
        factory, manager, controller = await open_with(CUSTOM, Providers({CUSTOM: (128_000, 16_000)}), tmp_path)()
        await manager.close_all()
        again = await manager.open_saved(tmp_path, "saved-1")
        assert factory.sessions[-1].session_limit == 108_800
        await settled(again, window=128_000)

    run(scenario())


def test_when_the_point_cannot_be_set_the_page_does_not_claim_a_window_droid_will_not_keep(tmp_path, monkeypatch):
    async def refuse(self, **changes):
        if "compaction_token_limit" in changes:
            raise DroidError("not supported")

    async def scenario():
        monkeypatch.setattr(FakeSession, "update_settings", refuse)
        factory, manager, controller = await open_with(CUSTOM, Providers({CUSTOM: (1_024_000, 128_000)}), tmp_path)()
        assert controller.state.value == "ready", "the session opens all the same"
        await settled(controller, window=250_000)

    run(scenario())


def test_the_window_follows_the_model_even_with_no_providers_wired(tmp_path):
    async def scenario():
        factory, manager = make(model="claude-haiku-5-5")
        controller = await manager.open_new(tmp_path)
        await settled(controller, window=872_000)
        assert asyncio.get_running_loop() is not None

    run(scenario())
