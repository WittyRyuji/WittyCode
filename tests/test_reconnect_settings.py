from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import load_settings, save_settings, settings_from_dict
from tests.app_support import make_core


def test_reconnect_settings_default_and_invalid_values():
    defaults = settings_from_dict({})
    assert (defaults.reconnect_interval, defaults.reconnect_limit) == (30, 5)
    for invalid in (-1, 0, 3601, True, 30.0, "30"):
        assert settings_from_dict({"reconnect_interval": invalid}).reconnect_interval == 30
    for invalid in (-1, 6, False, 2.0, "2"):
        assert settings_from_dict({"reconnect_limit": invalid}).reconnect_limit == 5


def test_reconnect_settings_rpc_validation_and_json_round_trip(tmp_path):
    async def scenario():
        core, _, _, _, _ = make_core(tmp_path, plan=False)
        try:
            for key, invalids in (("reconnectInterval", (0, 3601, True)), ("reconnectLimit", (-1, 6, False))):
                for value in invalids:
                    with pytest.raises(AppError):
                        await core.call("settings.set", {"key": key, "value": value})
            await core.call("settings.set", {"key": "reconnectInterval", "value": 17})
            view = await core.call("settings.set", {"key": "reconnectLimit", "value": 0})
            assert (view["reconnectInterval"], view["reconnectLimit"]) == (17, 0)
            path = tmp_path / "settings.json"
            save_settings(core.settings, path)
            reopened = load_settings(path)
            assert (reopened.reconnect_interval, reopened.reconnect_limit) == (17, 0)
        finally:
            await core.shutdown()

    asyncio.run(scenario())
