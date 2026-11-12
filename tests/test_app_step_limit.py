"""The app exposes a validated, persisted-shape step budget shared with its running own-loop engine."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import settings_to_dict
from tests.app_support import ready_core


def test_the_step_limit_is_in_init_settings_events_and_the_engine(tmp_path):
    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path)
        try:
            assert (await core.call("app.init"))["settings"]["maxSteps"] == 80
            view = await core.call("settings.set", {"key": "maxSteps", "value": 240})
            assert view["maxSteps"] == events.last("settings")["settings"]["maxSteps"] == 240
            assert engine.settings is core.settings and engine.settings.max_steps == 240
            assert settings_to_dict(core.settings)["max_steps"] == 240
            for value in (0, -1, True, "200", 200.5, 2**53):
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "maxSteps", "value": value})
            assert (await core.call("app.init"))["settings"]["maxSteps"] == 240
        finally:
            await core.shutdown()

    asyncio.run(scenario())
