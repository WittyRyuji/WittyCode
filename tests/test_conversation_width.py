"""The resizable conversation rail persists a bounded preference, not a viewport-specific width."""
import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import ready_core


@pytest.mark.parametrize("value", [240, 280, 380, 600])
def test_conversation_width_round_trips(value):
    settings = settings_from_dict({"conversation_width": value})
    assert settings.conversation_width == value
    assert settings_from_dict(settings_to_dict(settings)).conversation_width == value


@pytest.mark.parametrize("value", [None, True, "400", 239, 601, 280.5])
def test_invalid_saved_width_uses_default(value):
    assert settings_from_dict({"conversation_width": value}).conversation_width == 280


def test_width_bridge_validates_and_is_in_initial_settings(tmp_path):
    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            for value in (240, 380, 600):
                await core.call("settings.set", {"key": "conversationWidth", "value": value})
                assert core.settings.conversation_width == value
                assert core.settings_view()["conversationWidth"] == value
            for value in (True, "400", 239, 601):
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "conversationWidth", "value": value})
                assert core.settings.conversation_width == 600
        finally:
            await core.shutdown()
    asyncio.run(go())
