"""Local clean validation, CI and releases share one list of commands, not drifting copies."""

from pathlib import Path

from scripts.check_ci import NODE_VERSION, PYTHON_VERSION, clean_copy, commands

ROOT = Path(__file__).resolve().parents[1]


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
    import subprocess

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
