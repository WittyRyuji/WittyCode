"""The language the models think in (``core/language``): the setting, the own loop's instructions, the other engines' hook."""

from __future__ import annotations

from pathlib import Path

from desktop.core import language
from desktop.core.harness.prompt import BASE, build_instructions
from desktop.core.hooks import Hooks, PromptEvent
from desktop.core.settings import AppSettings, settings_from_dict, settings_to_dict


def test_the_other_engines_are_asked_after_each_message_and_the_own_loop_is_not():
    on = {"value": True}
    hooks = Hooks()
    hooks.on_prompt(language.prompt_hook(lambda: on["value"]))
    for engine in ("codex", "devin", "droid"):
        assert hooks.prompt(PromptEvent(engine, "C:/p", "hi")).text == language.THINK_CHINESE
    assert hooks.prompt(PromptEvent("chatgpt", "C:/p", "hi")).text == "", "the own loop has it in its instructions"
    on["value"] = False
    assert hooks.prompt(PromptEvent("devin", "C:/p", "hi")).text == "", "the setting is read at every message"


def test_the_own_loop_s_instructions_ask_for_chinese_thinking_only_when_the_setting_is_on(tmp_path: Path):
    asked = build_instructions(tmp_path, model="m", level="ask", think_chinese=True)
    plain = build_instructions(tmp_path, model="m", level="ask", think_chinese=False)
    assert language.THINK_CHINESE in asked and language.THINK_CHINESE not in plain
    answer_rule = next(line for line in BASE.splitlines() if line.startswith("- Answer in the language"))
    assert f"{answer_rule}\n- {language.THINK_CHINESE}" in asked, "next to the rule about the language of the answer"
    assert plain.startswith(BASE)


def test_the_setting_is_on_by_default_and_kept():
    assert AppSettings().think_chinese is True
    assert settings_from_dict({"think_chinese": False}).think_chinese is False
    assert settings_from_dict({"think_chinese": "no"}).think_chinese is True, "a value of the wrong type is the default"
    assert settings_to_dict(settings_from_dict({"think_chinese": False}))["think_chinese"] is False
