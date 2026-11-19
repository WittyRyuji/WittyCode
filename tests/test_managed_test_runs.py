"""The built-in test hook owns one invocation, not another session's files."""

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from desktop.core import test_runs
from desktop.core.hooks import MANAGED_TEST_HOOK, Hooks, PromptEvent


@pytest.fixture(autouse=True)
def isolated_runner(tmp_path, monkeypatch):
    # Exercise a fresh runner in the test's own fixtures, even under managed CI.
    monkeypatch.delenv(test_runs.WORK, raising=False)
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(test_runs.tempfile, "gettempdir", lambda: str(scratch))
    return scratch


def project(root):
    path = root / "project"
    path.mkdir()
    return path


def report(run):
    return json.loads((run.evidence / "result.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("engine", ["chatgpt", "devin", "droid"])
def test_client_calls_for_per_session_cleanup_with_no_history_sweep(tmp_path, engine):
    hooks = Hooks()
    hooks.on_prompt(lambda _event: None)
    assert hooks.builtin_prompt_hooks() == []
    hooks.on_prompt(MANAGED_TEST_HOOK)
    assert hooks.builtin_prompt_hooks() == [{"id": MANAGED_TEST_HOOK.id, "name": "测试收尾", "description": MANAGED_TEST_HOOK.description}]
    result = hooks.prompt(PromptEvent(engine, tmp_path, "continue"))
    assert "test_runs.py" in result.text
    assert "Each session, thread, and delegated agent" in result.text
    assert "Never scan or delete pre-existing files or session history" in result.text


@pytest.mark.parametrize("exit_code", [0, 7])
def test_cli_keeps_explicit_evidence_but_removes_scratch(tmp_path, monkeypatch, exit_code):
    root = project(tmp_path)
    monkeypatch.chdir(root)
    old = root / "sandbox"
    old.mkdir()
    (old / "history.json").write_text("keep")
    child = (
        "import os, sys; from pathlib import Path; "
        "work=Path(os.environ['WITTYCODE_TEST_WORK']); (work/'discard.txt').write_text('temporary'); "
        "Path(os.environ['WITTYCODE_TEST_EVIDENCE'], 'proof.txt').write_text(str(work)); "
        "assert os.environ['PYTEST_DEBUG_TEMPROOT'] == str(work/'tmp'); "
        f"raise SystemExit({exit_code})"
    )
    assert test_runs.main(["--", sys.executable, "-c", child]) == exit_code
    saved, = (root / "outputs" / "test-runs").iterdir()
    assert not Path((saved / "proof.txt").read_text()).exists()
    result = json.loads((saved / "result.json").read_text(encoding="utf-8"))
    assert (result["exit_code"], result["cleanup"]) == (exit_code, "done")
    assert (old / "history.json").read_text() == "keep"


def test_interrupt_cleans_only_its_run_while_another_thread_is_active(tmp_path):
    root = project(tmp_path)
    with test_runs.ManagedTestRun(root) as parent, ThreadPoolExecutor(max_workers=1) as pool:
        def another_run():
            with pytest.raises(KeyboardInterrupt), test_runs.ManagedTestRun(root) as interrupted:
                assert parent.work != interrupted.work
                raise KeyboardInterrupt
            return report(interrupted), interrupted.work.exists()

        result, exists = pool.submit(another_run).result()
        assert result["status"] == "cancelled" and result["cleanup"] == "done" and not exists
        assert parent.work.exists()
    assert not parent.work.exists()


def test_refuses_to_close_another_threads_run(tmp_path):
    root = project(tmp_path)
    with test_runs.ManagedTestRun(root) as run, ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="Only the thread that started"):
            pool.submit(run.__exit__, None, None, None).result()
        assert report(run)["status"] == "running" and run.work.exists()
    assert not run.work.exists()


def test_refuses_to_adopt_or_nest_a_run_from_another_session(tmp_path, monkeypatch):
    root = project(tmp_path)
    with test_runs.ManagedTestRun(root) as original:
        monkeypatch.setenv(test_runs.WORK, str(original.work))
        with pytest.raises(RuntimeError, match="Do not nest"):
            with test_runs.ManagedTestRun(root):
                pytest.fail("nested run must not start")
        assert original.work.exists()


def test_cleanup_refuses_a_replaced_directory(tmp_path):
    root = project(tmp_path)
    with pytest.raises(RuntimeError, match="finalization"), test_runs.ManagedTestRun(root) as run:
        run.work.rename(run.work.with_name("moved"))
        run.work.mkdir()
        (run.work / "other-session.txt").touch()
    assert (run.work / "other-session.txt").is_file()
    assert report(run)["cleanup"] == "blocked"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attributes")
def test_cleanup_removes_owned_readonly_files_but_not_a_shared_readonly_hardlink(tmp_path):
    root = project(tmp_path)
    with test_runs.ManagedTestRun(root) as owned:
        readonly = owned.work / "git-object"
        readonly.touch()
        readonly.chmod(stat.S_IREAD)
    assert not owned.work.exists() and report(owned)["cleanup"] == "done"

    with test_runs.ManagedTestRun(root) as other:
        readonly = other.work / "git-object"
        readonly.touch()
        readonly.chmod(stat.S_IREAD)
        with pytest.raises(RuntimeError, match="finalization"), test_runs.ManagedTestRun(root) as run:
            os.link(readonly, run.work / "hardlink")
        assert report(run)["cleanup"] == "blocked"
        assert readonly.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        # Both files are fixtures owned by this test; finish their teardown explicitly.
        readonly.chmod(stat.S_IREAD | stat.S_IWRITE)
        run._cleanup()


def test_cleanup_does_not_follow_directory_links_to_other_runs(tmp_path):
    root = project(tmp_path)
    with test_runs.ManagedTestRun(root) as outside:
        marker = outside.work / "other-session.txt"
        marker.touch()
        with test_runs.ManagedTestRun(root) as run:
            link = run.work / "link"
            if os.name == "nt":
                command = [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(outside.work)]
                subprocess.run(command, check=True, capture_output=True)
            else:
                link.symlink_to(outside.work, target_is_directory=True)
        assert marker.is_file() and not run.work.exists()
