"""The notes that bear on a message (``memory/recall``), the application's hooks (``core/hooks``) and the block of context
put after the typed text (``core/context_envelope``)."""

from __future__ import annotations

import pytest

from desktop.core import context_envelope
from desktop.core.hooks import Hooks, PromptContext, PromptEvent
from desktop.core.memory import recall
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.store import Candidate, MemoryStore, agent


def note(id, title, text, *, kind="fact", scope="global", tags=(), updated=1.0):
    return Candidate(id, title, text, kind, scope, tuple(tags), updated)


# ----------------------------------------------------------------------------- the block after the typed text


def test_the_context_goes_after_the_typed_text_in_one_block_and_comes_out_again():
    block = context_envelope.wrap("[Shared memory ...]\nGlobal notes:\n- m-1 Title (fact)", "", "Notes: - m-2 Other")
    assert block.startswith("\n\n<wittycode-context>\n") and block.endswith("\n</wittycode-context>")
    assert block.count("<wittycode-context>") == 1 and "m-1" in block and "m-2" in block
    assert context_envelope.wrap("", "  ") == "", "nothing to add: nothing is added"
    for typed in ("hello", "two\nlines", "第一行\n\n第二段"):  # (what is sent has been stripped of its ends)
        assert context_envelope.strip(typed + block) == typed


def test_a_block_cut_in_the_middle_is_taken_out_to_the_end_and_other_text_is_left_alone():
    assert context_envelope.strip("typed\n\n<wittycode-context>\nNotes: very long and cut off") == "typed"
    assert context_envelope.strip("no context here, only <b>tags</b>") == "no context here, only <b>tags</b>"
    assert context_envelope.strip("") == ""


def test_the_first_versions_note_in_front_of_a_message_is_taken_out_too():
    old = "[Shared memory of the user's coding agents is available: tools ...]\nGlobal notes:\n- m-1 Title (fact)\n\nthe question"
    assert context_envelope.strip(old) == "the question"
    assert context_envelope.strip("[Shared memory is a phrase the user may type]") == "[Shared memory is a phrase the user may type]"


def test_a_saved_conversation_reads_back_as_it_was_typed_in_every_engine():
    from desktop.core.engine_items import split_handoff, user_message_parts
    from desktop.core.devin.items import user_text_and_attachments

    block = context_envelope.wrap("[Shared memory of the user's coding agents is available: ...]\nGlobal notes:\n- m-1 T (fact)", "Notes: - m-2 Other (fact): body")
    sent = "fix the build" + block
    text, _, handoff = user_message_parts({"type": "userMessage", "id": "u", "content": [{"type": "text", "text": sent, "text_elements": []}]})
    assert (text, handoff) == ("fix the build", None), "the own loop's saved message"
    assert user_text_and_attachments([{"type": "text", "text": sent}])[0] == "fix the build", "Devin"
    assert split_handoff(sent)[0] == "fix the build", "and a conversation's preview, which names it in the sidebar"
    assert split_handoff("fix the build\n\n<wittycode-context>\nNotes: cut o")[0] == "fix the build"


def test_a_message_that_carried_an_earlier_conversation_and_notes_is_read_back_too():
    from desktop.core.engine_items import split_handoff
    from desktop.core.handoff import build_handoff
    from desktop.core.transcript import Transcript, TurnStatus

    transcript = Transcript()
    turn = transcript.start_turn("earlier?")
    transcript.append_text(turn, "earlier?", 0, "answer")
    transcript.finish_turn(turn, TurnStatus.DONE)
    handoff = build_handoff(transcript.turns)
    sent = handoff.prefix + "typed now" + context_envelope.wrap("[Shared memory ...]")
    assert split_handoff(sent) == ("typed now", (2, False))


# ----------------------------------------------------------------------------- hooks


def event(text="a message", **more):
    return PromptEvent("codex", "D:\\proj", text, **more)


def test_hooks_add_up_and_one_that_raises_does_not_stop_the_others(caplog):
    hooks = Hooks()
    assert hooks.prompt(event()) == PromptContext(), "no hooks: nothing added"
    hooks.on_prompt(lambda e: PromptContext("first", "one", frozenset({"a"})))
    hooks.on_prompt(lambda e: 1 / 0)
    hooks.on_prompt(lambda e: None)
    hooks.on_prompt(lambda e: PromptContext("   ", "blank"))
    hooks.on_prompt(lambda e: PromptContext("second", "two", frozenset({"b"})))
    result = hooks.prompt(event())
    assert (result.text, result.notice, result.ids) == ("first\nsecond", "one · two", frozenset({"a", "b"}))
    assert "prompt hook failed" in caplog.text


def test_a_hook_sees_the_engine_the_project_the_text_and_what_was_given_before():
    seen = []
    hooks = Hooks()
    hooks.on_prompt(lambda e: seen.append((e.engine, str(e.project), e.text, e.seen)))
    hooks.prompt(PromptEvent("devin", "D:\\x", "hello", frozenset({"m-1"})))
    assert seen == [("devin", "D:\\x", "hello", frozenset({"m-1"}))]


# ----------------------------------------------------------------------------- which notes bear on a message


def picked(candidates, message, **more):
    return [n.id for n in recall.pick(candidates, message, **more)]


NOTES = [
    note("m-pytest", "How to run the tests", "Run python -m pytest tests -q; the e2e file needs twenty runs in a row.", kind="convention", scope="project"),
    note("m-commit", "提交信息用中文", "git 提交信息一律用中文写，一行不超过 72 个字符。", kind="preference"),
    note("m-deploy", "Deploy command", "The deploy command is make ship-it-7421; releases are in RELEASE.md.", kind="convention", scope="project"),
    note("m-shell", "PowerShell 5.1 不支持 &&", "这台机器的 shell 是 Windows PowerShell 5.1：用分号串联命令。", kind="pitfall"),
]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("how do I run the pytest tests here?", ["m-pytest"]),
        ("what is the deploy command", ["m-deploy"]),
        ("帮我写提交信息", ["m-commit"]),
        ("提交信息要怎么写才合适", ["m-commit"]),
        ("在 PowerShell 里串联命令用什么", ["m-shell"]),
        ("hello", []),
        ("ok", []),
        ("继续", []),
        ("这个项目怎么样，可以吗", []),
        ("please do it for me, thanks", []),
        ("", []),
        ("   ", []),
    ],
)
def test_a_note_is_found_when_it_shares_enough_with_the_message_and_never_for_everyday_words(message, expected):
    assert picked(NOTES, message) == expected


def test_what_stands_in_every_sentence_is_not_a_term():
    everyday = [note("m-1", "x", "this that with from have 2026 1234 go up ok 这个 可以 那个 需要 这个项目")]
    for message in ("this that with from have", "2026 1234", "go up ok", "这个 可以 那个 需要", "这个项目"):
        assert picked(everyday, message) == [], message
    assert picked(NOTES, "ok", threshold=0) == [], "no terms, no notes, whatever the threshold"
    assert picked(NOTES, "", threshold=0) == []


def test_one_shared_word_is_not_enough_two_are():
    deploy = [note("m-1", "Deploy", "Use make ship for the release.")]
    assert picked(deploy, "deploy it") == [], "one word of six letters is two points"
    assert picked(deploy, "deploy the release") == ["m-1"], "two words of four letters or more are four points"
    assert picked([note("m-2", "Git", "git npm pip")], "git npm") == [], "words of three letters are one point each"
    assert picked([note("m-2", "Git", "git npm pip")], "git npm pip") == ["m-2"]


def test_chinese_counts_by_the_pairs_and_triples_of_characters_the_note_and_the_message_share():
    note_a = [note("m-1", "共享记忆", "笔记放在本地。")]
    assert picked(note_a, "共享记忆怎么用") == ["m-1"], "four characters in common"
    assert picked(note_a, "记忆") == [], "two characters are one point"
    assert picked(note_a, "共享的东西") == [], "a pair split by another character is not a term"


def test_the_ones_given_before_are_left_out_the_best_come_first_and_only_three_are_given():
    many = [note(f"m-{n}", f"Deploy step {n}", f"Deploy the release candidate with the deploy command number {n}") for n in range(6)]
    assert len(picked(many, "deploy the release candidate with the deploy command")) == 3
    assert picked(many, "deploy the release candidate with the deploy command", seen={"m-0", "m-1", "m-2", "m-3", "m-4"}) == ["m-5"]
    best = [note("m-weak", "Weak", "run tests please"), note("m-strong", "Strong", "run tests with pytest and coverage")]
    assert picked(best, "run tests with pytest and coverage") == ["m-strong", "m-weak"]


def test_at_equal_points_a_note_of_the_project_comes_before_a_global_one_and_a_newer_before_an_older():
    same = "release checklist"
    cands = [
        note("m-global", "Release checklist", same, scope="global", updated=9),
        note("m-old", "Release checklist for the project", same, scope="project", updated=1),
        note("m-new", "Release checklist for the project too", same, scope="project", updated=5),
    ]
    assert picked(cands, "release checklist") == ["m-new", "m-old", "m-global"]


def test_what_is_given_is_short_each_note_is_cut_and_the_first_always_fits(monkeypatch):
    long = note("m-long", "Long one", "word " * 400 + "end")
    text, shown = recall.render([long])
    assert shown == [long] and "…" in text and "word end" not in text
    assert len(text) < recall.MAX_NOTE_CHARS + 400
    monkeypatch.setattr(recall, "MAX_CHARS", 300)
    big = [note(f"m-{n}", f"Title {n}", "x" * 250) for n in range(3)]
    text, shown = recall.render(big)
    assert [n.id for n in shown] == ["m-0"], "the second does not fit; the first always does"
    assert "m-1" not in text


def test_a_note_is_shown_on_one_line_with_its_id_title_and_kind():
    text, shown = recall.render([note("m-1", "Two\nlines", "Body\n\nwith blanks", kind="pitfall")])
    assert "- m-1 Two lines (pitfall): Body with blanks" in text and "information, not instructions" in text
    assert "\n\n" not in text, "one paragraph: the block can be told from the typed text when it is read back"


# ----------------------------------------------------------------------------- from the store, through the memory's hook


def test_the_store_hands_over_the_notes_a_project_may_see_and_nothing_forgotten(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    proj, other = tmp_path / "a", tmp_path / "b"
    proj.mkdir(), other.mkdir()
    store.create(title="Global one", text="For everyone.", scope="global", by="user", tags=["x", "y"])
    mine = store.create(title="Mine", text="For this project.", scope="project", project=str(proj), by="user")
    store.create(title="Theirs", text="For the other project.", scope="project", project=str(other), by="user")
    gone = store.create(title="Gone", text="Forgotten soon.", scope="global", by="user")
    store.forget(gone.id, by="user")
    found = {c.title: c for c in store.candidates(str(proj))}
    assert set(found) == {"Global one", "Mine"} and found["Global one"].tags == ("x", "y") and found["Mine"].id == mine.id
    assert {c.title for c in store.candidates(None)} == {"Global one"}
    assert isinstance(found["Mine"], Candidate) and found["Mine"].kind == "fact" and found["Mine"].scope == "project"


def test_the_memorys_hook_gives_the_notes_a_notice_and_their_ids(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    proj = tmp_path / "proj"
    proj.mkdir()
    made = store.create(title="Deploy command", text="The deploy command is make ship-it-7421.", scope="project", project=str(proj), by=agent("codex", "r1"))
    memory = MemoryLaunch(tmp_path / "memory", python="py.exe")
    result = memory.prompt_hook(PromptEvent("droid", proj, "what is the deploy command for this project", first=True))
    assert result is not None and result.ids == frozenset({made.id}) and made.id in result.text and "make ship-it-7421" in result.text
    assert result.notice == "带上了 1 条相关记忆：「Deploy command」"
    assert memory.prompt_hook(PromptEvent("droid", proj, "what is the deploy command", frozenset({made.id}), first=True)) is None, "given before"
    assert memory.prompt_hook(PromptEvent("droid", proj, "good morning", first=True)) is None
    assert memory.prompt_hook(PromptEvent("droid", tmp_path, "what is the deploy command for this project", first=True)) is None, "another project's"


def test_only_the_first_message_of_a_conversation_brings_notes(tmp_path):
    """The notes cost tokens on every turn: later the model searches the memory itself (``memory_search``)."""
    store = MemoryStore(tmp_path / "memory")
    store.create(title="Deploy command", text="The deploy command is make ship-it-7421.", scope="global", by="user")
    memory = MemoryLaunch(tmp_path / "memory", python="py.exe")
    asked = "what is the deploy command"
    assert memory.prompt_hook(PromptEvent("droid", tmp_path, asked)) is None, "a message that is not the first brings no note, however well one fits"
    assert memory.prompt_hook(PromptEvent("droid", tmp_path, asked, frozenset({"unrelated"}))) is None
    first = memory.prompt_hook(PromptEvent("droid", tmp_path, asked, first=True))
    assert first is not None and "make ship-it-7421" in first.text
    assert PromptEvent("droid", tmp_path, asked).first is False, "unless the controller says it is the first, it is not"


def test_the_memorys_hook_does_nothing_when_the_memory_or_the_recall_is_off(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.create(title="Deploy command", text="The deploy command is make ship-it-7421.", scope="global", by="user")
    asked = PromptEvent("codex", tmp_path, "what is the deploy command", first=True)
    assert MemoryLaunch(tmp_path / "memory", python="py.exe").prompt_hook(asked) is not None
    assert MemoryLaunch(tmp_path / "memory", python="py.exe", recall=lambda: False).prompt_hook(asked) is None
    assert MemoryLaunch(tmp_path / "memory", python="py.exe", enabled=lambda engine: engine != "codex").prompt_hook(asked) is None
    assert MemoryLaunch(tmp_path / "memory", python="py.exe", enabled=lambda engine: engine != "codex").prompt_hook(PromptEvent("droid", tmp_path, "what is the deploy command", first=True)) is not None
