from __future__ import annotations

import pytest

from desktop.core.fusion import gates
from desktop.core.settings import AppSettings, load_settings, save_settings, settings_from_dict


def test_fixture_disables_gates_by_default():
    assert gates.DEFAULT is False
    assert not any(gates.enabled(AppSettings(), name) for name in gates.GATES)
    assert gates.enabled(AppSettings(fusion_gates={"drive": True}), "drive")


@pytest.mark.fusion_gates
def test_default_and_independent_gate_overrides():
    assert gates.DEFAULT is True
    assert gates.GATES == ("drive", "brief", "report", "route", "acceptance", "inject", "map", "observer")
    settings = AppSettings()
    assert not settings.fusion_gates
    assert all(gates.enabled(settings, name) for name in gates.GATES)
    for off in gates.GATES:
        settings.fusion_gates = {off: False}
        assert [name for name in gates.GATES if not gates.enabled(settings, name)] == [off]
    with pytest.raises(ValueError, match="Unknown Fusion gate"):
        gates.enabled(settings, "unknown")


@pytest.mark.fusion_gates("drive", "brief")
def test_marker_with_names_turns_on_only_those_gates():
    settings = AppSettings()
    assert [name for name in gates.GATES if gates.enabled(settings, name)] == ["drive", "brief"]
    assert gates.enabled(AppSettings(fusion_gates={"route": True}), "route")
    assert not gates.enabled(AppSettings(fusion_gates={"drive": False}), "drive")


def test_drive_settings_defaults_and_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    defaults = load_settings(path)
    assert defaults == AppSettings()
    assert (defaults.fusion_drive_shared, defaults.fusion_drive_warm, defaults.fusion_drive_keep_days,
            defaults.fusion_drive_qk, defaults.fusion_drive_auto_delete, defaults.fusion_drive_max_gb,
            defaults.fusion_observer_judgement) == (False, 0.5, 7, 0.6, False, 5.0, False)
    settings = AppSettings(fusion_gates={"drive": False, "observer": True}, fusion_drive_shared=True,
                           fusion_drive_warm=0, fusion_drive_keep_days=3650, fusion_drive_qk=-1,
                           fusion_drive_auto_delete=True, fusion_drive_max_gb=0.1, fusion_observer_judgement=True)
    save_settings(settings, path)
    assert load_settings(path) == settings


@pytest.mark.parametrize(("name", "values"), [
    ("fusion_gates", [None, [], "drive"]),
    ("fusion_drive_shared", [0, "true", None]),
    ("fusion_drive_warm", [True, "0.5", -0.1, 1.1, float("nan")]),
    ("fusion_drive_keep_days", [True, "7", 7.0, 0, 3651]),
    ("fusion_drive_qk", [False, "0.6", -1.1, 1.1, float("inf")]),
    ("fusion_drive_auto_delete", [1, "false", None]),
    ("fusion_drive_max_gb", [True, "5", 0, 1001]),
    ("fusion_observer_judgement", [1, "true", None]),
])
def test_invalid_settings_use_default(name, values):
    for value in values:
        assert settings_from_dict({name: value}) == AppSettings()


def test_only_known_boolean_gates_are_loaded():
    settings = settings_from_dict({"fusion_gates": {"drive": False, "brief": True, "report": 1, "unknown": True}})
    assert settings.fusion_gates == {"drive": False, "brief": True}
