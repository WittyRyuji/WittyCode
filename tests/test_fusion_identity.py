"""The built-in Fusion identities and their permission ceilings."""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from desktop.core.fusion.identity import DECOMPOSITION, Identity, display_name, load, parse_controlled


@pytest.mark.parametrize("name, role, tools, reuse, instruction", [
    ("Coder", "worker", {"read", "edit", "exec"}, True, "Implement"),
    ("Runner", "light", {"read", "exec"}, False, "Do not edit or decide"),
    ("QualitySampler", "worker", {"read", "exec"}, False, "without the Coder's process history"),
    ("Observer", "light", {"read"}, False, "loops, out-of-scope writes and stalls"),
])
def test_builtin_identity_fields(name, role, tools, reuse, instruction):
    identities = load(None)
    assert identities.names == ("Coder", "Runner", "QualitySampler", "Observer")
    assert identities.main == DECOMPOSITION
    assert identities.problems == ()
    assert identities.get("main") is None
    expert = identities.get(name.lower())
    assert isinstance(expert, Identity)
    assert expert.name == name
    assert expert.description
    assert expert.role == role
    assert isinstance(expert.tools, frozenset) and expert.tools == tools
    assert expert.reuse is reuse
    assert expert.reasoning == ""
    assert expert.compact_at is None
    assert instruction in expert.instructions
    assert "Do not delegate or write to long-term memory." in expert.instructions
    with pytest.raises(FrozenInstanceError):
        expert.reuse = not reuse


def test_display_name():
    assert display_name("Coder", "Login flow") == "Coder-Login flow"


def test_overrides_inherit_but_can_only_tighten_permissions(tmp_path):
    coder_body = "Inspect the brief.\n\n    Preserve Markdown indentation.\n\n---\nCheck the result."
    (tmp_path / "not-the-identity-name.md").write_bytes((
        "---\nname: cOdEr\ndescription: Apply changes: focused work only.\nrole: light\nreasoning: high\n"
        "tools: read, exec\ncompact_at: 75\nreuse: false\nunknown: ignored\n---\n" + coder_body
    ).replace("\n", "\r\n").encode("utf-8-sig"))
    (tmp_path / "runner.md").write_text("---\nname: RUNNER\ntools: read, edit, exec\nreuse: true\n---\nRun checks.", encoding="utf-8")
    (tmp_path / "observer.md").write_text("---\nname: Observer\ntools: exec, edit\n---\nObserve.", encoding="utf-8")
    (tmp_path / "main.md").write_text("---\nname: MaIn\nrole: ignored\n---\nNew routing skill.", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("Not a Markdown identity", encoding="utf-8")

    identities = load(tmp_path)
    assert identities.problems == ()
    assert identities.names == load(None).names
    assert identities.get("Coder") == Identity("Coder", "Apply changes: focused work only.", "light", "high",
                                               frozenset({"read", "exec"}), 75, False, coder_body)
    runner = identities.get("Runner")
    assert runner.tools == {"read", "exec"}
    assert runner.reuse is False
    assert runner.description == load(None).get("Runner").description
    assert runner.instructions == "Run checks."
    assert identities.get("Observer").tools == frozenset()
    assert identities.get("QualitySampler") == load(None).get("QualitySampler")
    assert identities.get("main") is None
    assert identities.main == "New routing skill."


@pytest.mark.parametrize("value, expected", [("", None), ("1", 1), ("100", 100)])
def test_valid_compact_points_and_empty_tools(tmp_path, value, expected):
    (tmp_path / "coder.md").write_text(f"---\nname: Coder\ncompact_at: {value}\ntools:\n---\nWork.", encoding="utf-8")
    identities = load(tmp_path)
    assert identities.problems == ()
    assert identities.get("Coder").compact_at == expected
    assert identities.get("Coder").tools == frozenset()


@pytest.mark.parametrize("content, problem", [
    ("name: Coder\n---\nBody", "opening"),
    ("---\nname: Coder\nBody", "key: value"),
    ("---\nname: Coder\n", "closing"),
    ("---\ndescription: Missing name\n---\nBody", "needs a name"),
    ("---\n: value\n---\nBody", "key: value"),
    ("---\nname: Coder\nname: Runner\n---\nBody", "Duplicate"),
    ("---\nname: Coder\nrole: main\n---\nBody", "worker or light"),
    ("---\nname: Coder\ntools: read, memory\n---\nBody", "tools"),
    ("---\nname: Coder\nreuse: maybe\n---\nBody", "true or false"),
    ("---\nname: Coder\ncompact_at: 0\n---\nBody", "1 to 100"),
    ("---\nname: Coder\ncompact_at: 101\n---\nBody", "1 to 100"),
    ("---\nname: Coder\ncompact_at: 10.5\n---\nBody", "1 to 100"),
    ("---\nname: Mystery\n---\nBody", "Unknown identity"),
])
def test_bad_override_is_reported_and_builtin_is_kept(tmp_path, content, problem):
    (tmp_path / "custom.md").write_text(content, encoding="utf-8")
    identities = load(tmp_path)
    assert identities.get("Coder") == load(None).get("Coder")
    assert len(identities.problems) == 1
    assert "custom.md" in identities.problems[0] and problem in identities.problems[0]


def test_unreadable_broken_and_oversized_files_do_not_block_good_overrides(tmp_path, monkeypatch):
    folder = tmp_path / "agents"
    folder.mkdir()
    prefix = b"---\nname: Coder\n---\n"
    (folder / "exact.md").write_bytes(prefix + b"x" * (64 * 1024 - len(prefix)))
    (folder / "huge.md").write_bytes(prefix + b"x" * (64 * 1024 + 1 - len(prefix)))
    (folder / "broken.md").write_bytes(prefix + b"\xff")
    unreadable = folder / "unreadable.md"
    unreadable.write_bytes(prefix)
    original_open = Path.open

    def deny_one_file(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError(13, "Permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_one_file)
    identities = load(folder)
    assert len(identities.get("Coder").instructions) == 64 * 1024 - len(prefix)
    assert len(identities.problems) == 3
    assert "broken.md" in identities.problems[0] and "UTF-8" in identities.problems[0]
    assert "huge.md" in identities.problems[1] and "64 KiB" in identities.problems[1]
    assert "unreadable.md" in identities.problems[2] and "Cannot read" in identities.problems[2]


def test_missing_folder_is_the_usual_case_and_an_unreadable_one_is_a_problem(tmp_path):
    identities = load(tmp_path / "missing")
    assert identities.names == load(None).names
    assert identities.main == DECOMPOSITION
    assert identities.problems == ()  # Found with a real model: every conversation without the folder showed a notice.
    not_a_folder = tmp_path / "agents"
    not_a_folder.write_text("a file where the folder should be", encoding="utf-8")
    blocked = load(not_a_folder)
    assert blocked.names == load(None).names and len(blocked.problems) == 1
    assert "Cannot read identity folder" in blocked.problems[0]


def test_controlled_identity_keeps_custom_name_and_tightened_permissions():
    ceiling = load(None).get("Coder")
    parsed = parse_controlled(
        "---\nname: 定制执行\ndescription: Focused.\nrole: light\nreasoning: high\ntools: read\ncompact_at: 75\nreuse: false\n---\nInspect first.",
        name="定制执行", ceiling=ceiling,
    )
    assert parsed == Identity("定制执行", "Focused.", "light", "high", frozenset({"read"}), 75, False,
                              "Inspect first.\nDo not delegate or write to long-term memory.")


@pytest.mark.parametrize("markdown, problem", [
    ("---\nname: Another\n---\nBody", "must match"),
    ("---\ntools: read, edit\n---\nBody", "tools cannot exceed"),
    ("---\nrole: worker\n---\nBody", "role cannot exceed"),
    ("---\nreuse: true\n---\nBody", "reuse cannot exceed"),
])
def test_controlled_identity_rejects_elevation_instead_of_widening(markdown, problem):
    with pytest.raises(ValueError, match=problem):
        parse_controlled(markdown, name="Read-only helper", ceiling=load(None).get("Runner"))


def test_controlled_identity_accepts_legacy_document_without_catalog_duplicated_name():
    parsed = parse_controlled("---\nunknown: ignored\ncompact_at: 80\n---\nLegacy body.",
                              name="Legacy", ceiling=load(None).get("Coder"))
    assert parsed.name == "Legacy"
    assert parsed.compact_at == 80
    assert parsed.tools == {"read", "edit", "exec"}
    assert parsed.instructions.endswith("Legacy body.\nDo not delegate or write to long-term memory.")
