"""The project has rules in its AGENTS.md that the message does not repeat: type hints and a docstring, a line in the
changelog, a lint run. What it asks of a harness: bring the project's rules to the model, and of the model: keep them."""

from __future__ import annotations

from pathlib import Path

_HEADING = "## Unreleased"


def _notes(text: str) -> list[str]:
    """The entries under the Unreleased heading."""
    lines = text.replace("\r\n", "\n").split("\n")
    if _HEADING not in lines:
        return []
    found: list[str] = []
    for line in lines[lines.index(_HEADING) + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            found.append(line)
    return found


def check(root: Path, run) -> str:
    code, output = run(["python", "-m", "unittest", "-q"])
    if code:
        return output
    code, output = run(["python", "tools/lint.py"])
    if code:
        return "The project's lint fails (AGENTS.md asks for type hints and a docstring):\n" + output
    if len(_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"))) < 2:
        return "CHANGELOG.md has no new line under '## Unreleased' (AGENTS.md asks for one per change)."
    return ""
