"""The bundled official ``numbered-reply`` skill (``desktop/assets/skills``, ``harness/official_skill``): a real SKILL.md
that ships with the client, injected into every engine's user-facing message while the settings switch is on, left out
of internal delegation prompts and of a message that explicitly names a same-named skill of the user's own."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from desktop.core.harness import official_skill, skills
from desktop.core.hooks import Hooks, PromptEvent
from desktop.core.settings import AppSettings, load_settings, save_settings, settings_from_dict
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import FakeResponses, say
from tests.test_chatgpt_controller import opened, run


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", folder)
    return folder


@pytest.fixture
def project(home) -> Path:
    folder = home / "work" / "project"
    folder.mkdir(parents=True)
    return folder


def event(text: str, project: Path, *, internal: bool = False, engine: str = "chatgpt") -> PromptEvent:
    return PromptEvent(engine, project, text, internal=internal)


def user_skill(home: Path, name: str = official_skill.NAME, body: str = "The user's own take.") -> Path:
    path = home / ".factory" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: User's own skill.\n---\n\n{body}\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------------------------------- the file itself


def test_the_bundled_file_is_a_real_skill():
    assert official_skill.FILE.is_file()
    data, body = skills.split_front_matter(official_skill.FILE.read_text(encoding="utf-8"))
    assert data.get("name") == official_skill.NAME and str(data.get("description", "")).strip()
    assert body.strip() and "1." in body, "the conclusion-first, numbered shape is the skill's whole point"
    assert "JSON" in body, "it tells the model what not to wrap the answer into"
    assert len(body.split()) < 160, "the body is injected into every message; it stays a short standing rule"


# ----------------------------------------------------------------------------------------------- the shared hook


def test_every_engines_user_message_gets_the_bundled_instructions(project, home):
    hook = official_skill.prompt_hook(lambda: True)
    for engine in ("chatgpt", "devin", "factory"):
        context = hook(event("how do I do this?", project, engine=engine))
        assert context is not None, engine
        assert f'<skill name="{official_skill.NAME}" official="wittycode">' in context.text
        assert "conclusion-first" in context.text


def test_off_means_no_injection_and_missing_file_means_no_context(project, home, monkeypatch):
    assert official_skill.prompt_hook(lambda: False)(event("hi", project)) is None
    monkeypatch.setattr(official_skill, "FILE", home / "nowhere" / "SKILL.md")
    assert official_skill.prompt_hook(lambda: True)(event("hi", project)) is None


def test_internal_delegation_prompts_get_nothing(project, home):
    hook = official_skill.prompt_hook(lambda: True)
    internal = event("Your responsibility / deliverable: Coder-xyz.\nDeclared write paths: a.py.", project, internal=True)
    assert hook(internal) is None
    assert hook(event("same words, but the user's own message", project, internal=False)) is not None


def test_a_message_that_names_the_users_same_named_skill_is_left_to_it(project, home):
    user_skill(home, body="MY OWN NUMBERED-REPLY BODY.")
    found = skills.discover(project, home)
    assert skills.named_in("/numbered-reply go", found), "the user's skill answers an explicit /name"
    assert official_skill.prompt_hook(lambda: True)(event("/numbered-reply go", project)) is None
    named = skills.prompt_hook(event("/numbered-reply go", project))
    assert named is not None and "MY OWN NUMBERED-REPLY BODY." in named.text, "explicit invocation keeps the existing rules"
    plain = official_skill.prompt_hook(lambda: True)(event("no slash here", project))
    assert plain is not None and "MY OWN NUMBERED-REPLY BODY." not in plain.text, "the bundled body is never the user's file"


# ----------------------------------------------------------------------------------------------- the persisted switch


def test_the_setting_defaults_on_reads_back_and_round_trips(tmp_path):
    assert AppSettings().numbered_reply is True
    assert settings_from_dict({}).numbered_reply is True
    assert settings_from_dict({"numbered_reply": False}).numbered_reply is False
    assert settings_from_dict({"numbered_reply": "no"}).numbered_reply is True
    file = tmp_path / "settings.json"
    save_settings(AppSettings(numbered_reply=False), file)
    assert load_settings(file).numbered_reply is False


def test_the_app_registers_the_hook_and_the_switch_works_live(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", home)

    async def go() -> None:
        core, plan, events, platform, engine = await ready_core(tmp_path)
        try:
            prompt = event("a normal user message", Path(core.settings.projects[0]))
            assert 'official="wittycode"' in core.hooks.prompt(prompt).text
            view = await core.call("settings.set", {"key": "numberedReply", "value": False})
            assert view["numberedReply"] is False and core.settings.numbered_reply is False
            assert official_skill.NAME not in core.hooks.prompt(prompt).text
            view = await core.call("settings.set", {"key": "numberedReply", "value": True})
            assert official_skill.NAME in core.hooks.prompt(prompt).text
            with pytest.raises(Exception):
                await core.call("settings.set", {"key": "numberedReply", "value": "yes"})
        finally:
            await core.shutdown()

    run(go())


# ----------------------------------------------------------------------------------------------- the expert exclusion at the shared entry


def test_a_fusion_experts_send_is_marked_internal(tmp_path, home):
    async def go() -> None:
        with FakeResponses() as fake:
            fake.script(say("Done."), say("More."))
            engine, controller = await opened(tmp_path, fake)
            parent = await engine.open_new(controller.project, model="gpt-6.1-sol", reasoning="medium", autonomy="code")
            seen: list[PromptEvent] = []
            engine.hooks.on_prompt(lambda e: seen.append(e) or None)
            controller.send_prompt("an ordinary user turn")
            await finish_turn(controller)
            controller._fusion_parent = parent
            controller.send_prompt("Your responsibility / deliverable: Coder-x.")
            await finish_turn(controller)
            assert [item.internal for item in seen] == [False, True]
            await engine.close_all()

    run(go())
