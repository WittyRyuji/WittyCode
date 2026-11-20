"""Local clean validation, CI and releases share one list of commands, not drifting copies."""

import base64
import fnmatch
import json
import os
import shutil
import subprocess
import sys
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


RELEASE_VERSION = "1.2.3"
RELEASE_ASSETS = tuple(f"WittyCode-{RELEASE_VERSION}-win-x64{suffix}" for suffix in (".zip", ".zip.sha256", ".exe", ".exe.sha256"))


def release_attest_subjects() -> list[str]:
    block = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8").split("subject-path: |", 1)[1]
    subjects = []
    for line in block.splitlines()[1:]:
        if line.strip() and not line.startswith(" " * 12):
            break
        if line.strip():
            subjects.append(line.strip())
    return subjects


def release_publish_script() -> str:
    block = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8").split("- name: Publish", 1)[1]
    script = []
    for line in block.split("run: |", 1)[1].splitlines()[1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        script.append(line[10:])
    return "\n".join(script)


def release_repo(tmp_path: Path) -> Path:
    # The non-ASCII folder keeps the gh log's asset paths on the UTF-8 decoding path.
    return tmp_path / "发布"


def run_release_publish(tmp_path: Path, version: str, tag: str) -> tuple[subprocess.CompletedProcess, Path]:
    # The Publish step is PowerShell; record the arguments the gh stub receives instead of publishing.
    repo = release_repo(tmp_path)
    (repo / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "no PowerShell found to exercise the Publish step"
    log = repo / "gh-argv.txt"
    stub = "function gh { [IO.File]::WriteAllLines('" + str(log).replace("'", "''") + "', [string[]]($args | ForEach-Object { $_ })) }"
    encoded = base64.b64encode((stub + "\n" + release_publish_script()).encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, TAG=tag, GH_TOKEN="stubbed")
    path_key = next(key for key in env if key.lower() == "path")
    env[path_key] = str(Path(sys.executable).parent) + os.pathsep + env[path_key]
    done = subprocess.run([shell, "-NoProfile", "-EncodedCommand", encoded], cwd=repo, env=env, capture_output=True, text=True, errors="replace")
    return done, log


def stage_release_dist(tmp_path: Path, assets: list[str]) -> None:
    dist = release_repo(tmp_path) / "dist"
    dist.mkdir(parents=True)
    for name in assets:
        (dist / name).write_text(name + "\n", encoding="ascii")


def test_release_attest_globs_cover_exactly_the_zip_and_exe():
    # The pinned attest action expands subject-path with @actions/glob nobrace, so braces never expand.
    subjects = release_attest_subjects()
    assert subjects == ["dist/WittyCode-*-win-x64.zip", "dist/WittyCode-*-win-x64.exe"]
    names = [f"WittyCode-{RELEASE_VERSION}-win-x64{suffix}" for suffix in ("", ".txt", ".zip", ".zip.sha256", ".exe", ".exe.sha256")]
    attested = {name for name in names for pattern in subjects if fnmatch.fnmatch(name, pattern.removeprefix("dist/"))}
    assert attested == {f"WittyCode-{RELEASE_VERSION}-win-x64.zip", f"WittyCode-{RELEASE_VERSION}-win-x64.exe"}


def test_release_publish_uploads_only_the_tagged_versions_four_assets(tmp_path):
    stage_release_dist(tmp_path, [*RELEASE_ASSETS, "WittyCode-0.0.1-win-x64.zip", "notes.txt"])
    (release_repo(tmp_path) / "dist" / f"WittyCode-{RELEASE_VERSION}-win-x64").mkdir()
    done, log = run_release_publish(tmp_path, RELEASE_VERSION, "v1.2.3")
    assert done.returncode == 0, done.stderr
    argv = log.read_text(encoding="utf-8").splitlines()
    assert argv[:3] == ["release", "create", "v1.2.3"]
    assert [Path(arg).name for arg in argv[3:7]] == list(RELEASE_ASSETS)
    assert argv[7:] == ["--verify-tag", "--title", "WittyCode v1.2.3", "--generate-notes"]


def test_release_publish_marks_dash_tags_prerelease_and_names_assets_by_pyproject(tmp_path):
    version = "1.2.3b1"
    assets = [f"WittyCode-{version}-win-x64{suffix}" for suffix in (".zip", ".zip.sha256", ".exe", ".exe.sha256")]
    stage_release_dist(tmp_path, assets)
    done, log = run_release_publish(tmp_path, version, "v1.2.3-beta.1")
    assert done.returncode == 0, done.stderr
    argv = log.read_text(encoding="utf-8").splitlines()
    assert argv[:3] == ["release", "create", "v1.2.3-beta.1"]
    assert [Path(arg).name for arg in argv[3:7]] == assets
    assert argv[-1] == "--prerelease"


@pytest.mark.parametrize("missing", RELEASE_ASSETS, ids=[name.split("-win-x64", 1)[1] for name in RELEASE_ASSETS])
def test_release_publish_fails_before_creating_a_release_when_an_asset_is_missing(tmp_path, missing):
    stage_release_dist(tmp_path, [name for name in RELEASE_ASSETS if name != missing])
    done, log = run_release_publish(tmp_path, RELEASE_VERSION, "v1.2.3")
    assert done.returncode != 0, "the step must fail instead of publishing an incomplete release"
    assert missing in done.stderr
    assert not log.exists(), "gh release create must not run when an asset is missing"
