"""Standalone unit tests for the sandbox policy core: no Qt, no real backend.

Runs three ways from the repository root::

    python -m desktop.sandbox.test_core -v
    python desktop/sandbox/test_core.py -v
    python desktop/core/test_runs.py -- python -m desktop.sandbox.test_core -v
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from desktop.sandbox import __main__ as cli
    from desktop.sandbox import core
except ModuleNotFoundError:  # allow `python desktop/sandbox/test_core.py` from anywhere
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from desktop.sandbox import __main__ as cli
    from desktop.sandbox import core


class FakeBackend:
    """Stub honouring the fixed probe/execute contract; records its execute call."""

    __name__ = "fake_backend"

    def __init__(self, ok=True, detail="fake ok", returncode=0, error=None):
        self.probe_result = (ok, detail)
        self.returncode = returncode
        self.error = error
        self.probed = False
        self.calls = []
        self.scratch_existed = None

    def probe(self):
        self.probed = True
        return self.probe_result

    def execute(self, workspace, argv, *, writable, protected, readable, scratch, env, timeout):
        self.scratch_existed = scratch.is_dir()
        self.calls.append({
            "workspace": workspace, "argv": argv, "writable": writable, "protected": protected,
            "readable": readable, "scratch": scratch, "env": env, "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"")


class SandboxTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sb-test-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.tool = self.tmp / "tool" / "run.exe"
        self.tool.parent.mkdir()
        self.tool.write_bytes(b"tool")
        self.scratch_root = self.tmp / "scratch-root"
        self.scratch_root.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def policy(self, **overrides):
        request = {"workspace": self.workspace, "argv": (str(self.tool),)}
        request.update(overrides)
        return core.validate_policy(**request)


class BackendSelectionTests(SandboxTestCase):

    def test_nt_selects_windows_backend(self):
        with mock.patch.object(core.os, "name", "nt"):
            self.assertEqual(core.backend_module_name(), "desktop.sandbox.windows")

    def test_posix_selects_posix_backend(self):
        with mock.patch.object(core.os, "name", "posix"):
            self.assertEqual(core.backend_module_name(), "desktop.sandbox.posix")

    def test_unknown_platform_fails(self):
        with mock.patch.object(core.os, "name", "java"):
            with self.assertRaises(core.BackendUnavailableError):
                core.backend_module_name()

    def test_missing_backend_fails_explicitly(self):
        with self.assertRaises(core.BackendUnavailableError):
            core.load_backend("desktop.sandbox.no_such_backend")

    def test_incomplete_backend_fails_explicitly(self):
        with self.assertRaises(core.BackendUnavailableError):
            core.load_backend("json")

    def test_probe_reports_load_failure_without_raising(self):
        with mock.patch.object(core, "load_backend",
                               side_effect=core.BackendUnavailableError("nope")):
            ok, detail = core.probe()
        self.assertFalse(ok)
        self.assertIn("nope", detail)

    def test_probe_reports_backend_exception(self):
        backend = FakeBackend()
        backend.probe = mock.Mock(side_effect=RuntimeError("driver gone"))
        ok, detail = core.probe(backend=backend)
        self.assertFalse(ok)
        self.assertIn("driver gone", detail)


class WorkspacePolicyTests(SandboxTestCase):

    def test_valid_workspace_passes(self):
        policy = self.policy()
        self.assertEqual(policy.workspace, self.workspace)
        self.assertFalse(policy.writable)

    def test_workspace_must_exist(self):
        with self.assertRaises(core.PolicyError):
            self.policy(workspace=self.tmp / "missing")

    def test_workspace_must_be_a_directory(self):
        with self.assertRaises(core.PolicyError):
            self.policy(workspace=self.tool)

    def test_workspace_must_not_be_a_filesystem_root(self):
        with self.assertRaises(core.PolicyError):
            self.policy(workspace=Path(self.tmp.anchor))

    def test_workspace_must_not_be_the_home_directory(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(core.PolicyError):
                self.policy(workspace=self.home)

    def test_workspace_must_not_sit_inside_credential_dirs(self):
        nested = self.home / ".ssh" / "ws"
        nested.mkdir(parents=True)
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(core.PolicyError):
                self.policy(workspace=nested)

    def test_workspace_must_not_contain_home(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(core.PolicyError):
                self.policy(workspace=self.tmp)

    def test_writable_flag_is_recorded(self):
        self.assertTrue(self.policy(writable=True).writable)

    def test_writable_must_be_a_real_bool(self):
        for bad in ("false", "yes", 1, 0, None):
            with self.assertRaises(core.PolicyError, msg=repr(bad)):
                self.policy(writable=bad)

    def test_writable_workspace_must_not_contain_this_module(self):
        repo = Path(core.__file__).resolve().parents[2]
        self.assertEqual(self.policy(workspace=repo).workspace, repo)  # read-only is fine
        with self.assertRaises(core.PolicyError):
            self.policy(workspace=repo, writable=True)
        with self.assertRaises(core.PolicyError):
            self.policy(workspace=Path(core.__file__).resolve().parent, writable=True)

    def test_workspace_must_not_be_an_empty_or_nul_path(self):
        for bad in ("", "\0", "a\0b"):
            with self.assertRaises(core.PolicyError, msg=repr(bad)):
                self.policy(workspace=bad)


class ProtectedPolicyTests(SandboxTestCase):

    def test_protected_inside_workspace_passes(self):
        secret = self.workspace / "secret.txt"
        secret.write_bytes(b"x")
        policy = self.policy(protected=(secret,))
        self.assertEqual(policy.protected, (secret,))

    def test_protected_must_exist(self):
        with self.assertRaises(core.PolicyError):
            self.policy(protected=(self.workspace / "missing",))

    def test_protected_must_be_inside_workspace(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        with self.assertRaises(core.PolicyError):
            self.policy(protected=(outside,))

    def test_protected_must_not_be_the_workspace_itself(self):
        with self.assertRaises(core.PolicyError):
            self.policy(protected=(self.workspace,))

    def test_protected_directory_passes_and_dedupes(self):
        inner = self.workspace / "keep"
        inner.mkdir()
        policy = self.policy(protected=(inner, inner))
        self.assertEqual(policy.protected, (inner,))

    def _reject_as_special(self, path):
        """Make *path* look like a non-regular file to the policy layer."""
        real_lstat = Path.lstat
        resolved = path.resolve()

        def fake_lstat(self):
            if self == resolved:
                return SimpleNamespace(st_mode=stat.S_IFIFO | 0o644, st_ino=1, st_dev=1)
            return real_lstat(self)

        return mock.patch.object(Path, "lstat", autospec=True, side_effect=fake_lstat)

    def test_protected_rejects_non_regular_files(self):
        inside = self.workspace / "fifo"
        inside.write_bytes(b"x")
        with self._reject_as_special(inside):
            with self.assertRaises(core.PolicyError):
                self.policy(protected=(inside,))

    def test_protected_must_not_cover_the_tool(self):
        inside = self.workspace / "run.exe"
        inside.write_bytes(b"x")
        with self.assertRaises(core.PolicyError):
            self.policy(argv=(str(inside),), protected=(inside,))
        masked_dir = self.workspace / "masked"
        masked_dir.mkdir()
        tool = masked_dir / "run.exe"
        tool.write_bytes(b"x")
        with self.assertRaises(core.PolicyError):
            self.policy(argv=(str(tool),), protected=(masked_dir,))


class ReadablePolicyTests(SandboxTestCase):

    def grant_dir(self, name="runtime"):
        path = self.tmp / name
        path.mkdir()
        return path

    def test_valid_readable_dir_passes(self):
        grant = self.grant_dir()
        self.assertEqual(self.policy(readable=(grant,)).readable, (grant,))

    def test_readable_must_exist(self):
        with self.assertRaises(core.PolicyError):
            self.policy(readable=(self.tmp / "missing",))

    def test_readable_must_not_be_a_filesystem_root(self):
        with self.assertRaises(core.PolicyError):
            self.policy(readable=(Path(self.tmp.anchor),))

    def test_readable_must_not_be_home_or_credential_dir(self):
        credential = self.home / ".ssh"
        credential.mkdir()
        with mock.patch.object(Path, "home", return_value=self.home):
            for bad in (self.home, credential):
                with self.assertRaises(core.PolicyError, msg=str(bad)):
                    self.policy(readable=(bad,))

    def test_readable_must_not_contain_a_sensitive_dir(self):
        with mock.patch.object(Path, "home", return_value=self.home):
            with self.assertRaises(core.PolicyError):
                self.policy(readable=(self.tmp,))  # grants the fake home too

    def test_readable_must_not_overlap_workspace(self):
        inside = self.workspace / "inner"
        inside.mkdir()
        for bad in (self.workspace, inside, self.tmp):
            with self.assertRaises(core.PolicyError, msg=str(bad)):
                self.policy(readable=(bad,))

    def test_readable_grants_must_not_overlap_each_other(self):
        outer = self.grant_dir("outer")
        inner = outer / "inner"
        inner.mkdir()
        with self.assertRaises(core.PolicyError):
            self.policy(readable=(outer, inner))

    def test_readable_file_grant_passes(self):
        grant = self.tmp / "config.dat"
        grant.write_bytes(b"x")
        self.assertEqual(self.policy(readable=(grant,)).readable, (grant,))

    def test_readable_rejects_non_regular_files(self):
        grant = self.tmp / "fifo"
        grant.write_bytes(b"x")
        with ProtectedPolicyTests._reject_as_special(self, grant):
            with self.assertRaises(core.PolicyError):
                self.policy(readable=(grant,))

    def test_private_root_is_not_blanket_denied(self):
        # /private/var/folders/... is where macOS keeps per-user temp dirs.
        self.assertNotIn("/private", core._POSIX_SENSITIVE)
        self.assertIn("/etc", core._POSIX_SENSITIVE)

    def test_readable_subdir_of_shared_root_passes(self):
        with mock.patch.dict(os.environ, {"ProgramData": str(self.home)}):
            tool_dir = self.grant_dir()
            self.assertEqual(self.policy(readable=(tool_dir,)).readable, (tool_dir,))

    def test_readable_shared_root_itself_fails(self):
        with mock.patch.dict(os.environ, {"ProgramData": str(self.home)}):
            with self.assertRaises(core.PolicyError):
                self.policy(readable=(self.home,))


class ArgvAndTimeoutTests(SandboxTestCase):

    def test_argv_must_not_be_empty(self):
        with self.assertRaises(core.PolicyError):
            self.policy(argv=())

    def test_tool_must_be_absolute(self):
        with self.assertRaises(core.PolicyError):
            self.policy(argv=("run.exe",))

    def test_tool_must_exist(self):
        with self.assertRaises(core.PolicyError):
            self.policy(argv=(str(self.tmp / "missing.exe"),))

    def test_tool_must_be_a_file(self):
        with self.assertRaises(core.PolicyError):
            self.policy(argv=(str(self.tool.parent),))

    def test_tool_is_resolved_in_policy(self):
        self.assertEqual(self.policy().argv[0], str(self.tool))

    def test_argv_items_reject_empty_and_nul(self):
        for bad_argv in ((str(self.tool), ""), (str(self.tool), "a\0b"), (str(self.tool), "\0")):
            with self.assertRaises(core.PolicyError, msg=repr(bad_argv)):
                self.policy(argv=bad_argv)

    def test_tool_must_not_live_in_a_writable_workspace(self):
        inside = self.workspace / "run.exe"
        inside.write_bytes(b"x")
        with self.assertRaises(core.PolicyError):
            self.policy(argv=(str(inside),), writable=True)
        self.assertEqual(self.policy(argv=(str(inside),)).argv[0], str(inside))

    def test_timeout_must_be_positive_and_finite(self):
        for bad in (0, -1, float("inf"), float("nan"), "soon"):
            with self.assertRaises(core.PolicyError, msg=repr(bad)):
                self.policy(timeout=bad)


class EnvironmentTests(SandboxTestCase):

    def setUp(self):
        super().setUp()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()

    def test_env_inherits_nothing_from_the_host(self):
        host = {"HTTP_PROXY": "http://proxy", "MY_API_TOKEN": "t", "PYTHONPATH": "/x", "CUSTOM_VAR": "c"}
        with mock.patch.dict(os.environ, host):
            env = core.build_env(self.scratch)
        for name in host:
            self.assertNotIn(name, env)
        self.assertNotIn(str(self.scratch), env["PATH"])

    def test_env_path_is_a_fixed_system_tool_dir(self):
        with mock.patch.object(core.os, "name", "posix"):
            env = core.build_env(self.scratch)
            self.assertEqual(env["PATH"], "/usr/bin:/bin")
            self.assertEqual(env["TMPDIR"], str(self.scratch))
        with mock.patch.object(core.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"SystemRoot": r"D:\sysroot"}):
            env = core.build_env(self.scratch)
            self.assertEqual(env["PATH"], r"D:\sysroot\System32")
            self.assertEqual(env["TEMP"], str(self.scratch))

    def test_env_points_temp_at_scratch(self):
        env = core.build_env(self.scratch)
        temp_names = ("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)
        for name in temp_names:
            self.assertEqual(env[name], str(self.scratch))

    def test_env_override_accepted(self):
        env = core.build_env(self.scratch, overrides={"APP_MODE": "test"})
        self.assertEqual(env["APP_MODE"], "test")

    def test_env_reserved_names_rejected(self):
        for name in ("PATH", "temp", "TMPDIR", "SystemRoot", "PATHEXT"):
            with self.assertRaises(core.PolicyError, msg=name):
                core.build_env(self.scratch, overrides={name: "x"})

    def test_env_denied_names_rejected(self):
        denied = ("HTTP_PROXY", "no_proxy", "AWS_SECRET_ACCESS_KEY", "MY_TOKEN",
                  "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "LD_PRELOAD", "BASH_ENV")
        for name in denied:
            with self.assertRaises(core.PolicyError, msg=name):
                core.build_env(self.scratch, overrides={name: "x"})

    def test_env_rejects_malformed_names_and_values(self):
        for name in ("1ABC", "A-B", ""):
            with self.assertRaises(core.PolicyError, msg=name):
                core.build_env(self.scratch, overrides={name: "x"})
        with self.assertRaises(core.PolicyError):
            core.build_env(self.scratch, overrides={"APP_MODE": 1})


class RunTests(SandboxTestCase):

    def run_it(self, backend=None, **overrides):
        request = {"workspace": self.workspace, "scratch_root": self.scratch_root,
                   "backend": backend or FakeBackend()}
        request.update(overrides)
        return core.run((str(self.tool),), **request)

    def test_run_passes_policy_to_backend_and_cleans_scratch(self):
        backend = FakeBackend(returncode=3)
        result = self.run_it(backend=backend)
        self.assertEqual(result.returncode, 3)
        call = backend.calls[0]
        self.assertTrue(call["workspace"] == self.workspace)
        self.assertFalse(call["writable"])
        self.assertEqual(call["argv"][0], str(self.tool))
        self.assertTrue(backend.scratch_existed)
        self.assertFalse(call["scratch"].exists())  # single-use scratch cleaned up
        self.assertNotIn(str(call["scratch"]), call["env"]["PATH"])  # PATH is a fixed system dir
        self.assertEqual(call["env"]["TEMP" if os.name == "nt" else "TMPDIR"], str(call["scratch"]))

    def test_run_forwards_writable_and_grants(self):
        grant = self.tmp / "runtime"
        grant.mkdir()
        keep = self.workspace / "keep"
        keep.mkdir()
        backend = FakeBackend()
        self.run_it(backend=backend, writable=False, protected=(keep,), readable=(grant,))
        call = backend.calls[0]
        self.assertEqual(call["protected"], (keep,))
        self.assertEqual(call["readable"], (grant,))

    def test_run_forwards_whitelisted_env(self):
        backend = FakeBackend()
        self.run_it(backend=backend, env={"APP_MODE": "x"})
        self.assertEqual(backend.calls[0]["env"]["APP_MODE"], "x")

    def test_failed_probe_means_no_execute(self):
        backend = FakeBackend(ok=False, detail="no driver")
        with self.assertRaises(core.BackendUnavailableError):
            self.run_it(backend=backend)
        self.assertEqual(backend.calls, [])

    def test_policy_errors_happen_before_probing(self):
        backend = FakeBackend()
        with self.assertRaises(core.PolicyError):
            self.run_it(backend=backend, workspace=self.tmp / "missing")
        self.assertFalse(backend.probed)

    def test_backend_os_error_becomes_launch_error_and_scratch_is_removed(self):
        backend = FakeBackend(error=OSError("cannot start"))
        with self.assertRaises(core.LaunchError):
            self.run_it(backend=backend)
        self.assertFalse(backend.calls[0]["scratch"].exists())

    def test_timeout_expired_propagates_and_scratch_is_removed(self):
        backend = FakeBackend(error=subprocess.TimeoutExpired("x", 1))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_it(backend=backend)
        self.assertFalse(backend.calls[0]["scratch"].exists())

    def test_scratch_root_inside_workspace_rejected(self):
        backend = FakeBackend()
        with self.assertRaises(core.PolicyError):
            self.run_it(backend=backend, scratch_root=self.workspace)
        self.assertEqual(backend.calls, [])
        self.assertEqual(list(self.workspace.iterdir()), [])  # created scratch was removed

    def test_scratch_root_inside_readable_grant_rejected(self):
        root = self.tmp / "runtime"
        root.mkdir()
        backend = FakeBackend()
        with self.assertRaises(core.PolicyError):
            self.run_it(backend=backend, readable=(root,), scratch_root=root)
        self.assertEqual(backend.calls, [])

    def test_missing_scratch_root_rejected(self):
        with self.assertRaises(core.PolicyError):
            self.run_it(scratch_root=self.tmp / "missing")

    def test_load_failure_is_explicit_not_a_fallback(self):
        with mock.patch.object(core, "load_backend",
                               side_effect=core.BackendUnavailableError("missing")):
            with self.assertRaises(core.BackendUnavailableError):
                core.run((str(self.tool),), workspace=self.workspace, scratch_root=self.scratch_root)

    def test_replaced_scratch_is_not_removed(self):
        class ReplacingBackend(FakeBackend):
            def execute(self, workspace, argv, **kwargs):
                self.scratch_existed = kwargs["scratch"].is_dir()
                self.calls.append(kwargs)
                shutil.rmtree(kwargs["scratch"])
                kwargs["scratch"].mkdir()
                (kwargs["scratch"] / "impostor").write_bytes(b"x")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

        backend = ReplacingBackend()
        with self.assertRaises(OSError):
            self.run_it(backend=backend)
        scratch = backend.calls[0]["scratch"]
        self.assertTrue((scratch / "impostor").is_file())  # impostor dir left untouched
        shutil.rmtree(scratch)  # test's own cleanup of the impostor


class CliTests(SandboxTestCase):

    def test_probe_exit_codes(self):
        self.assertEqual(cli.main(["--probe"], backend=FakeBackend()), 0)
        self.assertEqual(cli.main(["--probe"], backend=FakeBackend(ok=False, detail="x")), 1)

    def test_child_returncode_is_the_exit_code(self):
        code = cli.main(["--workspace", str(self.workspace), "--scratch-root", str(self.scratch_root),
                         "--", str(self.tool)], backend=FakeBackend(returncode=7))
        self.assertEqual(code, 7)

    def test_tool_flag_supplies_argv0(self):
        backend = FakeBackend()
        code = cli.main(["--workspace", str(self.workspace), "--scratch-root", str(self.scratch_root),
                         "--tool", str(self.tool), "--", "--flag"], backend=backend)
        self.assertEqual(code, 0)
        self.assertEqual(backend.calls[0]["argv"], (str(self.tool), "--flag"))

    def test_write_and_grants_reach_the_backend(self):
        grant = self.tmp / "runtime"
        grant.mkdir()
        keep = self.workspace / "keep.txt"
        keep.write_bytes(b"x")
        backend = FakeBackend()
        code = cli.main(["--workspace", str(self.workspace), "--scratch-root", str(self.scratch_root),
                         "--write", "--protect", str(keep), "--read", str(grant),
                         "--env", "APP_MODE=x", "--timeout", "5", "--", str(self.tool)],
                        backend=backend)
        self.assertEqual(code, 0)
        call = backend.calls[0]
        self.assertTrue(call["writable"])
        self.assertEqual(call["protected"], (keep,))
        self.assertEqual(call["readable"], (grant,))
        self.assertEqual(call["env"]["APP_MODE"], "x")
        self.assertEqual(call["timeout"], 5.0)

    def test_policy_error_exits_2(self):
        self.assertEqual(cli.main(["--workspace", str(self.tmp / "missing"), "--", str(self.tool)],
                                  backend=FakeBackend()), 2)

    def test_backend_failure_exits_2(self):
        self.assertEqual(cli.main(["--workspace", str(self.workspace), "--scratch-root",
                                   str(self.scratch_root), "--", str(self.tool)],
                                  backend=FakeBackend(ok=False)), 2)

    def test_timeout_exits_124(self):
        self.assertEqual(cli.main(["--workspace", str(self.workspace), "--scratch-root",
                                   str(self.scratch_root), "--", str(self.tool)],
                                  backend=FakeBackend(error=subprocess.TimeoutExpired("x", 1))), 124)

    def test_missing_workspace_option_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            cli.main(["--", str(self.tool)], backend=FakeBackend())

    def test_missing_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            cli.main(["--workspace", str(self.workspace)], backend=FakeBackend())

    def test_bad_env_pair_exits_2(self):
        code = cli.main(["--workspace", str(self.workspace), "--scratch-root", str(self.scratch_root),
                         "--env", "BROKEN", "--", str(self.tool)], backend=FakeBackend())
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
