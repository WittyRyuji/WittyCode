"""The visible app and both package toolchains must ship the same requested version."""

import json
from pathlib import Path
import tomllib

from desktop import __version__


def test_release_version_matches_python_and_frontend_metadata():
    root = Path(__file__).resolve().parents[1]
    python = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    frontend = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    assert __version__ == "0.2.1"
    assert python["project"]["version"] == frontend["version"] == lock["version"] == lock["packages"][""]["version"] == __version__
