import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import ready_core


@pytest.mark.parametrize("value", [0, 340, 592, 4000])
def test_inspector_width_round_trips(value):
    settings = settings_from_dict({"inspector_width": value})
    assert settings.inspector_width == value
    assert settings_from_dict(settings_to_dict(settings)).inspector_width == value


@pytest.mark.parametrize("value", [None, True, False, "400", 399.5, -1, 339, 4001])
def test_invalid_inspector_width_uses_automatic_width(value):
    assert settings_from_dict({"inspector_width": value}).inspector_width == 0


def test_inspector_width_rpc_and_saved_preference(tmp_path):
    async def scenario():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            assert core.settings_view()["inspectorWidth"] == 0
            await core.call("settings.set", {"key": "inspectorWidth", "value": 592})
            assert core.settings_view()["inspectorWidth"] == 592
            for value in (True, 339, 4001):
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "inspectorWidth", "value": value})
            assert settings_from_dict(settings_to_dict(core.settings)).inspector_width == 592
            await core.call("settings.set", {"key": "inspectorWidth", "value": 0})
            assert core.settings_view()["inspectorWidth"] == 0
        finally:
            await core.shutdown()
    asyncio.run(scenario())
