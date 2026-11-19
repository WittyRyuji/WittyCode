"""Local clean validation, CI and releases share one list of commands, not drifting copies."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.check_ci import NODE_VERSION, PYTHON_VERSION, clean_copy, commands

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def independent_runner(monkeypatch):
    # These fixtures exercise a fresh lifecycle even when pytest itself runs under check_ci.
    monkeypatch.delenv("WITTYCODE_TEST_WORK", raising=False)


def test_ci_and_release_invoke_the_shared_entry_point():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "../scripts/check_ci.py --part frontend" in ci
    assert "scripts/check_ci.py --part python" in ci
    assert "scripts/check_ci.py --part all" in release
    assert "node scripts/check.mjs" not in ci and "python -m pytest tests" not in ci
    assert NODE_VERSION.removeprefix("v") in ci
    assert ".".join(map(str, PYTHON_VERSION)) in ci


def test_check_list_builds_before_tests_and_checks_both_real_pages():
    frontend = commands("frontend", "python")
    assert frontend[:3] == [("frontend", ["npm", "ci"]), ("frontend", ["npm", "run", "build:app"]), ("frontend", ["npm", "run", "unit"])]
    assert [args[-1] for _, args in frontend[3:]] == ["scripts/check-page-readiness.mjs", "scripts/check.mjs", "scripts/check-live.mjs"]
    python = commands("python", "python")
    assert "--ignore-glob=tests/test_app*e2e*.py" in python[0][1]
    assert "--ignore=tests/test_shell.py" in python[0][1]
    assert not any("basetemp" in arg for arg in python[0][1])
    assert commands("all", "python") == frontend + python


def test_local_windows_entry_builds_and_runs_every_module_excluded_by_ci():
    rows = commands("windows", "python")
    assert rows[:2] == [("frontend", ["npm", "ci"]), ("frontend", ["npm", "run", "build:app"])]
    selected = set(rows[2][1][3:-3])
    expected = {p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_app*e2e*.py")} | {"tests/test_shell.py"}
    assert selected == expected
    assert "tests/test_app_plan_e2e.py" in selected and "tests/test_app_connections_e2e.py" in selected


def test_clean_copy_leaves_out_ignored_build_and_dependency_directories(tmp_path):
    source, target = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".gitignore").write_text("node_modules/\ndist/\nsandbox/\n", encoding="utf-8")
    (source / "app.py").write_text("pass\n", encoding="utf-8")
    for folder in ("node_modules", "dist", "sandbox"):
        (source / folder).mkdir()
        (source / folder / "old.txt").write_text("must not hide clean-runner bugs", encoding="utf-8")
    clean_copy(source, target)
    assert (target / "app.py").read_text() == "pass\n"
    assert all(not (target / folder).exists() for folder in ("node_modules", "dist", "sandbox", ".git"))


@pytest.mark.usefixtures("independent_runner")
def test_clean_entry_uses_an_isolated_git_root_and_the_same_python_for_qt(tmp_path, monkeypatch):
    import scripts.check_ci as entry

    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(entry, "ROOT", source)
    monkeypatch.setattr(entry.sys, "argv", ["check_ci.py", "--clean", "--part", "python"])
    def copy(_source, target):
        target.mkdir(parents=True)
    monkeypatch.setattr(entry, "clean_copy", copy)
    called = []
    monkeypatch.setattr(entry, "run", lambda argv, cwd, env: called.append((argv, cwd, dict(env))))
    assert entry.main() == 0
    assert called[0][0][:3] == ["git", "init", "--quiet"], "a nested clean copy must not discover the outer working tree"
    checks = called[-1]
    assert checks[0][1:3] == ["-m", "pytest"]
    assert checks[2]["WITTYCODE_TEST_PYTHON"] == checks[0][0]
    assert checks[2]["WITTYCODE_REQUIRE_QT"] == "1"
    assert "ci-profile" in checks[2]["WITTYCODE_SETTINGS"]
    assert any("requirements-dev.txt" in args for args, _, _ in called), "frontend parity also needs Qt's real client"
    workspace = Path(checks[2]["WITTYCODE_TEST_WORK"])
    assert checks[1] == workspace / "repo" and not workspace.is_relative_to(source)
    assert checks[0][-2:] == ["--basetemp", str(workspace / "p")]
    assert not workspace.exists()
    report = json.loads((Path(checks[2]["WITTYCODE_TEST_EVIDENCE"]) / "result.json").read_text(encoding="utf-8"))
    assert report["exit_code"] == 0 and report["cleanup"] == "done"


@pytest.mark.usefixtures("independent_runner")
def test_clean_entry_reaps_the_copy_when_dependency_install_fails(tmp_path, monkeypatch):
    import scripts.check_ci as entry

    monkeypatch.setattr(entry, "ROOT", tmp_path)
    monkeypatch.setattr(entry.sys, "argv", ["check_ci.py", "--clean", "--part", "python"])
    monkeypatch.setattr(entry, "clean_copy", lambda _source, target: target.mkdir(parents=True))
    paths = []

    def run(argv, cwd, env):
        paths.append(Path(env["WITTYCODE_TEST_WORK"]))
        if "pip" in argv:
            raise subprocess.CalledProcessError(42, argv)

    monkeypatch.setattr(entry, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        entry.main()
    assert len(paths) == 3 and not any(path.exists() for path in paths)
    report, = (tmp_path / "outputs" / "test-runs").glob("*/result.json")
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert (saved["status"], saved["exit_code"], saved["cleanup"]) == ("failed", 42, "done")
