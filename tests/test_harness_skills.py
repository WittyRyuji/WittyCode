"""Skills and custom slash commands (``harness/skills``): the files they are read from, who wins, how a message names one, and how
they reach the model: by name for every engine (the prompt hook), by the model's choice in WittyCode's own loop (the ``skill``
tool, the list in the instructions, the folders readable without asking)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from desktop.core.context_envelope import OPEN as ENVELOPE
from desktop.core.harness import skills
from desktop.core.harness.loop import ALLOW, ASK, Call
from desktop.core.harness.permissions import decide
from desktop.core.harness.prompt import build_instructions
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools
from desktop.core.hooks import PromptEvent
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn
from tests.fake_responses import FakeResponses, call, say
from tests.test_chatgpt_controller import opened, run


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def skill_file(folder: Path, name: str, description: str = "Does the thing. Use when asked.", body: str = "Step one.\nStep two.", extra: str = "") -> Path:
    return write(folder / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", folder)
    return folder


@pytest.fixture
def project(home) -> Path:
    """A project inside the (made-up) home folder, so that the walk up from it ends there and meets nothing of the real user's."""
    folder = home / "work" / "project"
    folder.mkdir(parents=True)
    return folder


def names(found) -> list[str]:
    return [item.name for item in found]


# ----------------------------------------------------------------------------------------------- front matter


def test_front_matter_reads_plain_quoted_folded_and_listed_values():
    text = (
        "\ufeff---\nname: my-skill\ndescription: \"Quoted: with a colon\"\nenabled: false\nuser-invocable: 'false'\n"
        "allowed-tools:\n  - Read\n  - Grep\nlicense: MIT\nmetadata:\n  owner: team\n  version: 2\n---\n\nBody line.\n"
    )
    data, body = skills.split_front_matter(text)
    assert data["name"] == "my-skill" and data["description"] == "Quoted: with a colon"
    assert data["allowed-tools"] == ["Read", "Grep"] and data["license"] == "MIT"
    assert "metadata" not in data and "owner" not in data, "a nested mapping is skipped, not flattened into the top level"
    assert body == "Body line.\n".rstrip("\n") or body.strip() == "Body line."


def test_front_matter_folds_and_keeps_block_text_and_inline_lists():
    data, _ = skills.split_front_matter("---\ndescription: >\n  First part\n  second part.\nnotes: |\n  line one\n  line two\ntools: [Read, \"Grep\"]\ntop:\n- a\n- b\n---\nx")
    assert data["description"] == "First part second part."
    assert data["notes"] == "line one\nline two"
    assert data["tools"] == ["Read", "Grep"] and data["top"] == ["a", "b"], "a list may start at the left edge"


def test_a_plain_value_goes_on_over_indented_lines_and_three_dots_may_close_the_block():
    data, body = skills.split_front_matter("---\ndescription: Does a thing and\n  keeps going here.\nname: x\n...\nbody\n")
    assert data == {"description": "Does a thing and keeps going here.", "name": "x"}
    assert body == "body"


def test_a_file_without_front_matter_or_with_an_unclosed_one_is_all_body():
    for text in ("just text\n", "---\nname: x\nno end of the block\n", ""):
        assert skills.split_front_matter(text) == ({}, text.lstrip("\ufeff"))


def test_a_name_is_what_can_be_typed_after_a_slash():
    assert skills.slug("Review API_Change v2") == "review-api-change-v2"
    assert skills.slug("--x--") == "x" and skills.slug("你好") == ""


# ----------------------------------------------------------------------------------------------- finding skills


def test_the_project_wins_over_the_user_who_wins_over_a_plugin(project, home):
    skill_file(project / ".factory" / "skills", "shared", "From the project.")
    skill_file(home / ".factory" / "skills", "shared", "From the user.")
    skill_file(home / ".factory" / "skills", "mine", "Only the user's.")
    plugin = home / ".factory" / "plugins" / "cache" / "market" / "tools" / "1.0.0"
    write(plugin / ".factory-plugin" / "plugin.json", json.dumps({"name": "tools"}))
    skill_file(plugin / "skills", "shared", "From a plugin.")
    skill_file(plugin / "skills", "plugged", "Only the plugin's.")
    found = {skill.name: skill for skill in skills.discover(project, home)}
    assert set(found) == {"shared", "mine", "plugged"}
    assert (found["shared"].description, found["shared"].source) == ("From the project.", "project")
    assert found["mine"].source == "personal" and found["plugged"].source == "plugin" and found["plugged"].plugin == plugin


def test_skills_are_found_in_the_compatible_folders_and_in_parents_up_to_the_repository_root(project, home):
    (project.parent / ".git").mkdir()
    skill_file(project / ".agents" / "skills", "from-agents")
    skill_file(project / ".claude" / "skills", "from-claude")
    skill_file(project.parent / ".factory" / "skills", "from-the-root")
    skill_file(home / ".agent" / "skills", "personal-agent")
    assert set(names(skills.discover(project, home))) == {"from-agents", "from-claude", "from-the-root", "personal-agent"}


def test_the_search_stops_at_the_repository_root_and_the_nearest_folder_wins(home):
    outer = home / "outer"
    repo = outer / "repo"
    (repo / ".git").mkdir(parents=True)
    package = repo / "packages" / "app"
    package.mkdir(parents=True)
    skill_file(outer / ".factory" / "skills", "above-the-repository")
    skill_file(repo / ".factory" / "skills", "twice", "From the root of the repository.")
    skill_file(repo / ".factory" / "skills", "root-only")
    skill_file(package / ".factory" / "skills", "twice", "From the folder itself.")
    found = {skill.name: skill for skill in skills.discover(package, home)}
    assert set(found) == {"twice", "root-only"}, "what lies above the repository's root is not the project's"
    assert found["twice"].description == "From the folder itself."


def test_the_search_goes_down_a_few_levels_but_not_below_a_skill(project, home):
    root = project / ".factory" / "skills"
    skill_file(root / "group" / "nested", "deep")
    write(root / "outer" / "SKILL.md", "---\nname: outer\ndescription: Outer one.\n---\nx")
    skill_file(root / "outer" / "inner", "inside-a-skill")
    skill_file(root / "a" / "b" / "c" / "d" / "e", "too-deep")
    assert set(names(skills.discover(project, home))) == {"deep", "outer"}


def test_a_skill_needs_a_description_and_takes_its_folder_name_without_a_name(project, home):
    root = project / ".factory" / "skills"
    write(root / "no-description" / "SKILL.md", "---\nname: no-description\n---\nbody")
    write(root / "Folder Named" / "SKILL.md", "---\ndescription: Named by its folder.\n---\nbody")
    write(root / "off" / "SKILL.md", "---\nname: off\ndescription: Switched off.\nenabled: false\n---\nbody")
    assert names(skills.discover(project, home)) == ["folder-named"]


def test_a_skill_turned_off_in_either_settings_file_is_left_out(project, home):
    skill_file(project / ".factory" / "skills", "one")
    skill_file(project / ".factory" / "skills", "two")
    skill_file(project / ".factory" / "skills", "three")
    write(home / ".factory" / "settings.json", json.dumps({"disabledSkills": ["one"]}))
    write(project / ".factory" / "settings.json", json.dumps({"disabledSkills": ["Two"]}))
    assert names(skills.discover(project, home)) == ["three"]
    write(home / ".factory" / "settings.json", "{ this is not json")
    assert set(names(skills.discover(project, home))) == {"one", "three"}, "a broken settings file turns nothing off"


def test_who_may_invoke_a_skill(project, home):
    root = project / ".factory" / "skills"
    skill_file(root, "both")
    skill_file(root, "user-only", extra="disable-model-invocation: true\n")
    skill_file(root, "model-only", extra="user-invocable: false\n")
    found = {skill.name: skill for skill in skills.discover(project, home)}
    assert (found["both"].user_invocable, found["both"].model_invocable) == (True, True)
    assert (found["user-only"].user_invocable, found["user-only"].model_invocable) == (True, False)
    assert (found["model-only"].user_invocable, found["model-only"].model_invocable) == (False, True)


def test_a_plugin_that_enabled_plugins_turns_off_brings_nothing_and_both_layouts_are_read(project, home):
    base = home / ".factory" / "plugins" / "cache" / "market"
    native, claude, off = base / "native" / "1", base / "claude" / "2", base / "off" / "3"
    write(native / ".factory-plugin" / "plugin.json", json.dumps({"name": "native"}))
    write(claude / ".claude-plugin" / "plugin.json", json.dumps({"name": "claude"}))
    write(off / ".factory-plugin" / "plugin.json", json.dumps({"name": "off"}))
    for plugin in (native, claude, off):
        skill_file(plugin / "skills", f"from-{plugin.parent.name}")
        write(plugin / "commands" / f"cmd-{plugin.parent.name}.md", "Do it.")
    write(home / ".factory" / "settings.json", json.dumps({"enabledPlugins": {"off@market": False, "native@market": True}}))
    assert set(names(skills.discover(project, home))) == {"from-native", "from-claude"}
    assert {command.name for command in skills.discover_commands(project, home)} == {"cmd-native", "cmd-claude"}


def test_a_plugin_skill_may_name_its_own_folder_in_its_instructions(project, home):
    plugin = home / ".factory" / "plugins" / "p" / "1"
    write(plugin / ".factory-plugin" / "plugin.json", json.dumps({"name": "p"}))
    skill_file(plugin / "skills", "runner", body="Run ${DROID_PLUGIN_ROOT}/scripts/go.ps1 and $CLAUDE_PLUGIN_ROOT/x.")
    (skill,) = skills.discover(project, home)
    assert skill.body() == f"Run {plugin}/scripts/go.ps1 and {plugin}/x."


def test_a_plugin_is_known_by_the_name_in_either_manifest_or_else_by_its_folder(project, home):
    market = home / ".factory" / "plugins" / "market"
    legacy, bare, kept = market / "legacy-folder", market / "bare", market / "kept"
    write(legacy / ".claude-plugin" / "plugin.json", json.dumps({"name": "legacy"}))
    write(bare / ".factory-plugin" / "plugin.json", "{}")
    write(kept / ".factory-plugin" / "plugin.json", json.dumps({"name": "kept"}))
    for plugin in (legacy, bare, kept):
        skill_file(plugin / "skills", f"from-{plugin.name}")
    write(home / ".factory" / "settings.json", json.dumps({"enabledPlugins": {"legacy@market": False, "bare@market": False}}))
    assert names(skills.discover(project, home)) == ["from-kept"]


def test_plugins_are_looked_for_only_a_few_levels_down(project, home, monkeypatch):
    monkeypatch.setattr(skills, "PLUGIN_DEPTH", 2)
    base = home / ".factory" / "plugins"
    for plugin in (base / "market" / "near", base / "a" / "b" / "c" / "far"):
        write(plugin / ".factory-plugin" / "plugin.json", json.dumps({"name": plugin.name}))
        skill_file(plugin / "skills", f"from-{plugin.name}")
    assert names(skills.discover(project, home)) == ["from-near"]


def test_a_skill_that_is_not_a_plugins_keeps_a_plugin_variable_as_written(project, home):
    skill_file(project / ".factory" / "skills", "plain", body="Use ${DROID_PLUGIN_ROOT}/x.")
    (skill,) = skills.discover(project, home)
    assert skill.body() == "Use ${DROID_PLUGIN_ROOT}/x."


def test_the_files_beside_a_skill_are_a_few_and_leave_out_dependency_folders(project, home, monkeypatch):
    path = skill_file(project / ".factory" / "skills", "many")
    for name in ("a.md", "b.md", "c.md", "d.md"):
        write(path.parent / name, "x")
    write(path.parent / "node_modules" / "pkg" / "index.js", "x")
    (skill,) = skills.discover(project, home)
    assert skill.files() == ["a.md", "b.md", "c.md", "d.md"]
    monkeypatch.setattr(skills, "FILES_LISTED", 2)
    assert skill.files() == ["a.md", "b.md"]


def test_a_skill_hands_over_its_instructions_without_the_front_matter_and_lists_what_is_beside_it(project, home):
    path = skill_file(project / ".factory" / "skills", "report", body="Write the report.")
    write(path.parent / "checklist.md", "- a")
    write(path.parent / "schemas" / "out.json", "{}")
    (skill,) = skills.discover(project, home)
    assert skill.body() == "Write the report."
    assert skill.files() == ["checklist.md", "schemas/out.json"]
    text = skills.load_text(skill)
    assert str(skill.folder) in text and "checklist.md, schemas/out.json" in text and "<instructions>\nWrite the report.\n</instructions>" in text


def test_a_long_skill_is_cut_and_a_missing_file_is_empty(project, home, monkeypatch):
    monkeypatch.setattr(skills, "BODY_LIMIT", 20)
    path = skill_file(project / ".factory" / "skills", "long", body="x" * 100)
    (skill,) = skills.discover(project, home)
    assert skill.body() == "x" * 20
    path.unlink()
    assert skill.body() == ""


# ----------------------------------------------------------------------------------------------- naming a skill


def test_a_message_names_a_skill_as_a_whole_word_with_a_slash(project, home):
    root = project / ".factory" / "skills"
    for name in ("summarize-diff", "review", "hidden"):
        skill_file(root, name, extra="user-invocable: false\n" if name == "hidden" else "")
    found = skills.discover(project, home)
    assert names(skills.named_in("/summarize-diff focus on migrations", found)) == ["summarize-diff"]
    assert names(skills.named_in("please do /review, then /summarize-diff.", found)) == ["review", "summarize-diff"]
    assert names(skills.named_in("(see /review) and /review again", found)) == ["review"], "once"
    for text in ("look at /tmp/review", "C:/review/x", "/review-more", "/Review", "a/review", "/review.txt", "/hidden", "no slash review", "https://x.io/review"):
        assert names(skills.named_in(text, found)) == [], text
    assert names(skills.named_in("end of sentence /review.", found)) == ["review"], "a full stop after the word does not hide it"


# ----------------------------------------------------------------------------------------------- custom commands


def test_a_command_is_a_markdown_file_whose_name_is_its_slug_and_the_project_wins(project, home):
    write(project / ".factory" / "commands" / "Code Review.md", "---\ndescription: Review a branch\nargument-hint: <branch>\n---\nReview `$ARGUMENTS` now.")
    write(home / ".factory" / "commands" / "code-review.md", "personal version")
    write(home / ".factory" / "commands" / "deploy.md", "# Deploy helper\n\nShip it.")
    write(home / ".factory" / "commands" / "script.sh", "#!/bin/sh\necho no")
    write(home / ".factory" / "commands" / "team" / "nested.md", "nested")
    found = {command.name: command for command in skills.discover_commands(project, home)}
    assert set(found) == {"code-review", "deploy", "nested"}
    review = found["code-review"]
    assert (review.description, review.hint, review.source) == ("Review a branch", "<branch>", "project")
    assert review.prompt("feature/login") == "Review `feature/login` now."
    assert review.prompt("") == "Review `` now."
    assert found["deploy"].description == "Deploy helper" and found["deploy"].prompt("x") == "# Deploy helper\n\nShip it."


def test_a_message_that_starts_with_a_command_is_that_commands_whole(project, home):
    write(home / ".factory" / "commands" / "deploy.md", "Deploy $ARGUMENTS")
    commands = skills.discover_commands(project, home)
    called = skills.called_command("/deploy feature/login\nand more", commands)
    assert called is not None and called[0].name == "deploy" and called[1] == "feature/login\nand more"
    assert skills.called_command("/deploy", commands)[1] == ""
    assert skills.called_command("please /deploy now", commands) is None
    assert skills.called_command("/other", commands) is None and skills.called_command("/deployed", commands) is None


# ----------------------------------------------------------------------------------------------- the hook (every engine)


def event(text: str, project: Path) -> PromptEvent:
    return PromptEvent("droid", project, text)


def test_the_hook_adds_the_instructions_of_the_skills_a_message_names_and_says_so(project, home):
    skill_file(project / ".factory" / "skills", "summarize-diff", body="Run git diff.")
    skill_file(project / ".factory" / "skills", "review", body="Check the API.")
    context = skills.prompt_hook(event("/summarize-diff and then /review", project))
    assert context is not None and context.notice == "已载入技能 summarize-diff、review"
    assert 'name="summarize-diff"' in context.text and "Run git diff." in context.text and "Check the API." in context.text
    assert context.text.startswith("The user named the skills summarize-diff, review.")
    one = skills.prompt_hook(event("/review", project))
    assert one is not None and one.text.startswith("The user named the skill review. Follow its instructions")


def test_the_hook_runs_a_command_instead_when_the_message_starts_with_one(project, home):
    write(project / ".factory" / "commands" / "review.md", "Look at $ARGUMENTS closely.")
    skill_file(project / ".factory" / "skills", "other")
    context = skills.prompt_hook(event("/review the parser /other", project))
    assert context is not None and context.notice == "已载入命令 /review"
    assert "Look at the parser /other closely." in context.text and 'name="other"' not in context.text, "the command owns the rest of the line"


def test_the_hook_says_nothing_for_an_ordinary_message_and_never_raises(project, home, monkeypatch):
    skill_file(project / ".factory" / "skills", "review")
    assert skills.prompt_hook(event("hello", project)) is None
    assert skills.prompt_hook(event("/unknown thing", project)) is None
    monkeypatch.setattr(skills, "discover_commands", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert skills.prompt_hook(event("/review", project)) is None


def test_the_hook_context_is_after_the_typed_text_in_the_envelope_the_history_readers_strip(project, home):
    from desktop.core.context_envelope import strip, wrap
    from desktop.core.hooks import Hooks

    skill_file(project / ".factory" / "skills", "review", body="Check the API.")
    hooks = Hooks()
    hooks.on_prompt(skills.prompt_hook)
    context = hooks.prompt(event("/review now", project))
    sent = "/review now" + wrap("", context.text)
    assert ENVELOPE in sent and "Check the API." in sent
    assert strip(sent) == "/review now", "a saved conversation shows what was typed"


def test_the_page_is_given_the_commands_and_the_skills_a_user_may_name(project, home):
    write(home / ".factory" / "commands" / "deploy.md", "---\ndescription: Ship\nargument-hint: <env>\n---\nx")
    skill_file(project / ".factory" / "skills", "review", "Reviews the change.")
    skill_file(project / ".factory" / "skills", "deploy", "A skill that the command of the same name hides.")
    skill_file(project / ".factory" / "skills", "quiet", extra="user-invocable: false\n")
    assert skills.listing(project, home) == [
        {"name": "deploy", "description": "Ship", "hint": "<env>", "kind": "command", "source": "personal"},
        {"name": "review", "description": "Reviews the change.", "hint": "", "kind": "skill", "source": "project"},
    ]


# ----------------------------------------------------------------------------------------------- the own loop


def test_the_instructions_list_the_skills_the_model_may_load(tmp_path, project, home):
    skill_file(project / ".factory" / "skills", "review", "Reviews API changes. Use when routes change.")
    skill_file(project / ".factory" / "skills", "manual", extra="disable-model-invocation: true\n")
    found = skills.discover(project, home)
    text = build_instructions(project, model="m", level="code", skills=found)
    assert "call the `skill` tool" in text and "- review: Reviews API changes. Use when routes change." in text
    assert "manual" not in text, "a skill only the user may start is not offered to the model"
    assert "Available skills" not in build_instructions(project, model="m", level="code")


def test_a_long_list_is_cut_and_a_long_description_too(project, home, monkeypatch):
    monkeypatch.setattr(skills, "LISTED", 2)
    monkeypatch.setattr(skills, "DESCRIPTION_LIMIT", 12)
    for name in ("a", "b", "c"):
        skill_file(project / ".factory" / "skills", name, "d" * 40)
    text = skills.skills_prompt(skills.discover(project, home))
    assert text.count("\n- ") == 2 and "- a: ddddddddddd…" in text and "(1 more; the user can name them as /name.)" in text


def test_the_skill_tool_answers_with_the_skill_or_names_the_ones_there_are(project, home):
    skill_file(project / ".factory" / "skills", "review", body="Check it.")
    skill_file(project / ".factory" / "skills", "manual", extra="disable-model-invocation: true\n")
    registry = ToolRegistry.default()
    spec = registry.get("skill")
    assert spec is not None and spec.ui_name == "Skill" and spec.kind == "read"
    ctx = ToolContext(project=project)
    result = run_tool(registry, "skill", {"skill": "Review"}, ctx)
    assert not result.is_error and "<instructions>\nCheck it.\n</instructions>" in result.output and result.data == {"skill": "review"}
    missing = run_tool(registry, "skill", {"skill": "manual"}, ctx)
    assert missing.is_error and "Available skills: review." in missing.output, "the model cannot load a skill it was not offered"
    assert registry.ui_input("skill", {"skill": "review"}) == {"skill": "review"} and registry.ui_input("skill", {}) == {}


def run_tool(registry: ToolRegistry, name: str, args: dict[str, Any], ctx: ToolContext):
    import asyncio

    return asyncio.run(registry.run(name, args, ctx))


def test_reading_a_skills_folder_needs_no_question_and_nothing_else_outside_the_project_changes(project, home, tmp_path):
    folder = skill_file(home / ".factory" / "skills", "review").parent
    write(folder / "checklist.md", "- a")
    elsewhere = write(tmp_path / "elsewhere" / "note.txt", "x")
    read = ToolRegistry.default().get("read_file")
    edit = ToolRegistry.default().get("edit_file")
    plain = ToolContext(project=project)
    ctx = ToolContext(project=project, readable=skills.read_roots(skills.discover(project, home)))
    args = {"file_path": str(folder / "checklist.md")}
    assert decide("ask", read, args, plain, remembered=set()).verdict == ASK
    assert decide("ask", read, args, ctx, remembered=set()).verdict == ALLOW
    listing = ToolRegistry.default().get("list_dir")
    assert decide("ask", listing, {"directory_path": str(folder)}, plain, remembered=set()).verdict == ASK
    assert decide("ask", listing, {"directory_path": str(folder)}, ctx, remembered=set()).verdict == ALLOW, "the folder itself, not only what is in it"
    assert decide("ask", read, {"file_path": str(elsewhere)}, ctx, remembered=set()).verdict == ASK, "only the skills' folders"
    assert decide("code", edit, {**args, "old_str": "a", "new_str": "b"}, ctx, remembered=set()).verdict == ASK, "reading, not writing"
    protected = ToolContext(project=project, readable=ctx.readable, protected=(folder,))
    assert decide("full", read, args, protected, remembered=set()).verdict == "deny", "a protected place stays protected"


def test_the_skill_tool_asks_nothing_at_any_level_and_runs_in_plan_mode(project, home):
    skill_file(project / ".factory" / "skills", "review")
    tools = HarnessTools(ToolRegistry.default(), ToolContext(project=project), "ask", [])
    use = Call("i", "c", "skill", "{}", {"skill": "review"})
    assert tools.check(use).verdict == ALLOW
    tools.plan = True
    assert tools.check(use).verdict == ALLOW


def test_a_conversation_lists_its_skills_loads_one_and_reads_beside_it_without_a_question(tmp_path, home):
    project = tmp_path / "project"
    skill = skill_file(home / ".factory" / "skills", "history-handbook", "Makes exam handbooks. Use for revision PDFs.", body="Use the template.")
    write(skill.parent / "template.md", "TEMPLATE TEXT")

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(
                call("skill", {"skill": "history-handbook"}, call_id="c1", item_id="f1"),
                call("read_file", {"file_path": str(skill.parent / "template.md")}, call_id="c2", item_id="f2"),
                say("Done with the handbook."),
            )
            engine, controller = await opened(tmp_path, fake, autonomy="ask")
            controller.send_prompt("make a handbook for chapter 3")
            await finish_turn(controller)
            first = fake.requests[0]
            assert "- history-handbook: Makes exam handbooks. Use for revision PDFs." in first["instructions"]
            assert any(tool["name"] == "skill" for tool in first["tools"])
            outputs = [item["output"] for request in fake.requests[1:] for item in request["input"] if item.get("type") == "function_call_output"]
            assert any("Use the template." in output for output in outputs), "the skill came back as the tool's answer"
            assert any("TEMPLATE TEXT" in output for output in outputs), "a file of the skill's folder was read although the level asks about everything outside"
            assert not [prompt for turn in controller.transcript.turns for prompt in turn.prompts], "nobody was asked anything"
            await engine.close_all()

    run(go())


def test_a_skill_made_while_the_conversation_goes_on_is_listed_from_the_next_message(tmp_path, home):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("One."), say("Two."))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("first")
            await finish_turn(controller)
            skill_file(home / ".factory" / "skills", "made-later")
            controller.send_prompt("second")
            await finish_turn(controller)
            assert "made-later" not in fake.requests[0]["instructions"]
            assert fake.requests[1]["instructions"] == fake.requests[0]["instructions"]
            update = next(item for item in reversed(fake.requests[1]["input"]) if item.get("role") == "developer")
            assert "- made-later:" in update["content"][0]["text"], "new skills are appended without rewriting the cached prefix"
            await engine.close_all()

    run(go())


def test_a_message_that_names_a_skill_carries_it_for_the_model_and_the_page_shows_only_what_was_typed(tmp_path, home):
    project = tmp_path / "project"
    skill_file(home / ".factory" / "skills", "summarize-diff", body="Run git diff and list the risks.")

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("Summary."))
            engine, controller = await opened(tmp_path, fake)
            engine.hooks.on_prompt(skills.prompt_hook)
            controller.send_prompt("/summarize-diff focus on the migration")
            await finish_turn(controller)
            sent = str(fake.requests[0]["input"][-1])
            assert "/summarize-diff focus on the migration" in sent and "Run git diff and list the risks." in sent and "wittycode-context" in sent
            shown = [turn.user_block.text for turn in controller.transcript.turns if turn.user_block is not None]
            assert shown == ["/summarize-diff focus on the migration"]
            assert any("已载入技能 summarize-diff" in block.text for turn in controller.transcript.turns for block in turn.blocks if block.kind == "notice")
            await engine.close_all()

    run(go())


# ----------------------------------------------------------------------------------------------- more of the small rules


def test_the_search_leaves_out_dependency_folders_and_does_not_take_a_folder_called_skill_md_for_a_skill(project, home):
    root = project / ".factory" / "skills"
    skill_file(root / "node_modules" / "pkg", "from-a-dependency")
    (root / "weird" / "SKILL.md").mkdir(parents=True)
    skill_file(root / "weird" / "inner", "inside-weird")
    assert names(skills.discover(project, home)) == ["inside-weird"]


def test_the_name_in_the_file_is_the_skills_and_its_description_is_one_line_of_single_spaces(project, home):
    write(project / ".factory" / "skills" / "folder-x" / "SKILL.md", "---\nname: Real Name\ndescription: Does   it\n  well.\n---\nbody")
    (skill,) = skills.discover(project, home)
    assert (skill.name, skill.description) == ("real-name", "Does it well.")


def test_the_folders_read_without_asking_are_those_of_the_skills_and_of_their_plugins_each_once(project, home):
    mine = skill_file(project / ".factory" / "skills", "mine")
    plugin = home / ".factory" / "plugins" / "p" / "1"
    write(plugin / ".factory-plugin" / "plugin.json", json.dumps({"name": "p"}))
    one = skill_file(plugin / "skills", "one")
    two = skill_file(plugin / "skills", "two")
    roots = skills.read_roots(skills.discover(project, home))
    assert len(roots) == 4 and set(roots) == {mine.parent, one.parent, two.parent, plugin}


def test_skills_the_model_may_not_load_are_not_listed_and_nothing_to_list_is_no_text(project, home):
    skill_file(project / ".factory" / "skills", "user-only", extra="disable-model-invocation: true\n")
    assert skills.skills_prompt(skills.discover(project, home)) == "" and skills.skills_prompt([]) == ""


def test_a_commands_arguments_lose_the_spaces_around_them(project, home):
    write(project / ".factory" / "commands" / "deploy.md", "Deploy to $ARGUMENTS now.")
    (command,) = skills.discover_commands(project, home)
    assert command.prompt("  staging  ") == "Deploy to staging now."


def test_a_message_that_starts_with_a_command_is_one_even_with_space_or_a_line_break_before_it(project, home):
    write(project / ".factory" / "commands" / "deploy.md", "Deploy to $ARGUMENTS.")
    commands = skills.discover_commands(project, home)
    for text in ("  /deploy staging", "\n/deploy staging  "):
        called = skills.called_command(text, commands)
        assert called is not None and called[1] == "staging"
