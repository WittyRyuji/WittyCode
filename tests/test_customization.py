"""Private global rules, explicit project edits, language preferences and stable prompt snapshots."""

import asyncio
from pathlib import Path

import pytest

from desktop.core import customization, language
from desktop.core.app_core import AppError
from desktop.core.harness.prompt import build_instructions, changed_context
from desktop.core.hooks import PromptEvent
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import MODEL, OTHER_MODEL, ready_core
from tests.test_app_core import new_session


def test_rules_are_scoped_and_conflicts_never_overwrite_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "profile/settings.json"))
    file = customization.global_rules_path()
    original = customization.read_rules(file)
    assert not original["exists"] and original["text"] == ""
    saved = customization.save_rules(file, "# Rules\nUse short answers.\n", original["revision"])
    assert saved["exists"] and saved["text"].startswith("# Rules")
    with pytest.raises(customization.RulesError, match="其他程序修改"):
        customization.save_rules(file, "wrong", original["revision"])
    assert file.read_text(encoding="utf-8") == saved["text"]
    file.unlink()
    with pytest.raises(customization.RulesError, match="其他程序修改"):
        customization.save_rules(file, "wrong", saved["revision"])
    assert not file.exists()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_editor_preserves_bom_line_endings_and_prompt_reads_the_saved_text(tmp_path, encoding):
    file = tmp_path / "AGENTS.md"
    file.write_bytes("# Rules\r\n用中文回答。\r\n".encode(encoding))
    before = customization.read_rules(file)
    customization.save_rules(file, "# Rules\nUse English.\n", before["revision"])
    raw = file.read_bytes()
    assert raw.decode(encoding) == "# Rules\r\nUse English.\r\n"
    assert (raw.startswith(b"\xef\xbb\xbf")) is (encoding == "utf-8-sig")
    assert "Use English." in build_instructions(tmp_path, model="demo", level="ask")


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
def test_editor_preserves_utf16_byte_order(tmp_path, encoding, bom):
    file = tmp_path / "AGENTS.md"
    file.write_bytes(bom + "# Rules\r\nKeep it short.\r\n".encode(encoding))
    before = customization.read_rules(file)
    customization.save_rules(file, "# Rules\nUse Chinese.\n", before["revision"])
    raw = file.read_bytes()
    assert raw.startswith(bom)
    assert raw[2:].decode(encoding) == "# Rules\r\nUse Chinese.\r\n"
    assert "Use Chinese." in build_instructions(tmp_path, model="demo", level="ask")


def test_rules_have_an_explicit_size_limit_and_unknown_encodings_are_untouched(tmp_path):
    file = tmp_path / "AGENTS.md"
    before = customization.read_rules(file)
    with pytest.raises(customization.RulesError, match="32 KiB"):
        customization.save_rules(file, "x" * (32 * 1024 + 1), before["revision"])
    assert not file.exists()
    file.write_bytes(b"\xffinvalid")
    with pytest.raises(customization.RulesError, match="不是 UTF"):
        customization.read_rules(file)
    assert file.read_bytes() == b"\xffinvalid"


def test_global_and_project_rules_are_distinct_snapshot_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "profile/settings.json"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("Project rules win.", encoding="utf-8")
    file = customization.global_rules_path()
    view = customization.read_rules(file)
    customization.save_rules(file, "Global rule A.", view["revision"])
    before = {}
    text = build_instructions(project, model="demo", level="ask", sections=before)
    assert text.index("Global rule A.") < text.index("Project rules win.")
    same = {}
    build_instructions(project, model="demo", level="ask", sections=same)
    assert changed_context(before, same) is None
    view = customization.read_rules(file)
    customization.save_rules(file, "Global rule B.", view["revision"])
    after = {}
    build_instructions(project, model="demo", level="ask", sections=after)
    delta = str(changed_context(before, after))
    assert "Global rule B." in delta and "Project rules win." not in delta and "PowerShell" not in delta
    event = PromptEvent("devin", project, "hello")
    assert "Global rule B." in customization.prompt_hook(event).text
    customization.save_rules(file, "Global rule A.", customization.read_rules(file)["revision"])
    assert "Global rule A." in customization.prompt_hook(event).text
    file.unlink(); (project / "AGENTS.md").unlink()
    assert "No WittyCode custom instructions" in customization.prompt_hook(event).text
    assert customization.prompt_hook(PromptEvent("chatgpt", project, "hello")) is None


@pytest.mark.parametrize("answer,thinking", [("auto", "auto"), ("zh-CN", "en"), ("en", "zh-CN")])
def test_language_preferences_round_trip_and_apply_to_both_engine_paths(tmp_path, answer, thinking):
    settings = settings_from_dict({"answer_language": answer, "thinking_language": thinking})
    saved = settings_to_dict(settings)
    assert saved["answer_language"] == answer and saved["thinking_language"] == thinking
    rule = language.rules_for(answer, thinking)
    assert rule in build_instructions(tmp_path, model="demo", level="ask", answer_language=answer, thinking_language=thinking)
    hook = language.prompt_hook(lambda: True, lambda: answer, lambda: thinking)
    assert hook(PromptEvent("devin", tmp_path, "hi")).text == rule
    if answer != "auto":
        assert "Answer in the language the user writes in." not in rule
    assert hook(PromptEvent("chatgpt", tmp_path, "hi")) is None


def test_core_rules_can_only_edit_registered_projects_and_the_app_private_global_file(tmp_path):
    async def scenario():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            for project in ("", str(tmp_path / "proj")):
                view = await core.call("customization.read", {"projectId": project})
                result = await core.call("customization.save", {"projectId": project, "text": "Keep tests green.", "revision": view["revision"]})
                assert result["text"] == "Keep tests green."
            with pytest.raises(AppError):
                await core.call("customization.read", {"projectId": str(tmp_path / "unregistered")})
            await core.call("settings.set", {"key": "answerLanguage", "value": "en"})
            await core.call("settings.set", {"key": "thinkingLanguage", "value": "auto"})
            assert core.settings_view()["answerLanguage"] == "en" and not core.settings.think_chinese
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_temporary_session_options_do_not_change_the_saved_defaults(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        try:
            initial = dict(core.defaults())
            cid = await new_session(core, events, tmp_path / "proj", settings={"model": OTHER_MODEL, "reasoning": "low", "autonomy": "full", "mode": "spec"})
            assert core.defaults() == initial
            assert core._page_settings(core._by_cid[cid]) == {"model": OTHER_MODEL, "reasoning": "low", "autonomy": "full", "mode": "spec"}
            for field, value in (("model", MODEL), ("reasoning", "high"), ("autonomy", "ask"), ("mode", "auto")):
                await core.call("session.choose", {"id": cid, "field": field, "value": value})
                assert core.defaults() == initial
            next_id = await new_session(core, events, tmp_path / "proj")
            assert core._page_settings(core._by_cid[next_id]) == initial
            for value in (True, [], {"model": False}, {"mode": "unknown"}, {"other": "value"}):
                with pytest.raises(AppError):
                    await core.call("session.new", {"projectId": str(tmp_path / "proj"), "settings": value})
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_explicit_defaults_are_durable_but_live_model_changes_never_rewrite_them(tmp_path):
    import json
    from desktop.core.settings import default_settings_path
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path)
        core.persist = True
        try:
            for field, value in (("model", OTHER_MODEL), ("reasoning", "low"), ("autonomy", "code"), ("mode", "auto")):
                await core.call("session.choose", {"field": field, "value": value})
            before = json.loads(default_settings_path().read_text(encoding="utf-8"))
            cid = await new_session(core, events, tmp_path / "proj")
            await core.call("session.choose", {"id": cid, "field": "model", "value": MODEL})
            await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "high"})
            after = json.loads(default_settings_path().read_text(encoding="utf-8"))
            for key in ("default_model", "reasoning_effort", "chatgpt_autonomy", "mode"):
                assert before[key] == after[key]
            restored = settings_from_dict(after)
            assert restored.default_model == OTHER_MODEL and restored.reasoning_effort == "low"
        finally:
            await core.shutdown()
    asyncio.run(scenario())
