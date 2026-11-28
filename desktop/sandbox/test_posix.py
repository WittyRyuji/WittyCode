"""Tests for desktop.sandbox.posix: compilers run anywhere, launches mocked,
real isolation verified only on native Linux/macOS hosts.

The ``_linux_args``/``_seatbelt_profile`` compilers are pure string
builders, so their tests run on any host (this CI/dev machine is
Windows). Launch plumbing is tested with mocked ``subprocess``. The
``NativeIntegrationTests`` class runs the real sandbox end to end through
``core.run`` and is skipped unless ``sys.platform`` is linux/darwin and
``probe()`` reports the backend usable — on Windows it reports SKIPPED,
not passed.

Runs three ways from the repository root::

    python -m desktop.sandbox.test_posix -v
    python desktop/sandbox/test_posix.py -v
    python desktop/core/test_runs.py -- python -m desktop.sandbox.test_posix -v
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

try:
    from desktop.sandbox import core, posix
except ModuleNotFoundError:  # allow `python desktop/sandbox/test_posix.py` from anywhere
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from desktop.sandbox import core, posix

WS = "/work/project"
SCRATCH = "/tmp/wittycode-sandbox-aaaa"
TOOL = "/opt/tools/run"


def linux_args(**overrides):
    request = {
        "writable": False, "protected": (), "readable": (),
        "scratch": SCRATCH, "env": {"PATH": SCRATCH, "TMPDIR": SCRATCH},
    }
    request.update(overrides)
    argv = request.pop("argv") if "argv" in request else (TOOL, "--flag")
    return posix._linux_args(1000, 1000, WS, argv, **request)


def binds(args):
    """(mode, src, dest) triples for every bind-like op in a bwrap argv."""
    out = []
    for i, token in enumerate(args):
        if token in ("--bind", "--ro-bind", "--ro-bind-try"):
            out.append((token, args[i + 1], args[i + 2]))
    return out


class LinuxCompileTests(unittest.TestCase):

    def test_deterministic_same_inputs_same_argv(self):
        first = linux_args(protected=((WS + "/conf", True), (WS + "/key", False)),
                           readable=("/opt/a", "/opt/b"))
        second = linux_args(protected=((WS + "/conf", True), (WS + "/key", False)),
                            readable=("/opt/b", "/opt/a"))
        self.assertEqual(first, second)

    def test_network_and_ipc_namespaces_always_unshared(self):
        args = linux_args()
        for flag in ("--unshare-net", "--unshare-ipc", "--unshare-pid",
                     "--unshare-user", "--unshare-uts"):
            self.assertIn(flag, args)
        self.assertNotIn("--share-net", args)
        self.assertIn("--die-with-parent", args)
        self.assertIn("--new-session", args)

    def test_workspace_read_only_by_default_writable_on_request(self):
        self.assertIn(("--ro-bind", WS, WS), binds(linux_args()))
        self.assertIn(("--bind", WS, WS), binds(linux_args(writable=True)))

    def test_system_runtime_is_read_only(self):
        spec = binds(linux_args())
        for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
            self.assertIn(("--ro-bind-try", directory, directory), spec)

    def test_etc_tree_not_exposed_only_file_whitelist(self):
        spec = binds(linux_args())
        self.assertNotIn(("--ro-bind-try", "/etc", "/etc"), spec)
        self.assertNotIn(("--ro-bind", "/etc", "/etc"), spec)
        for item in ("/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/passwd",
                     "/etc/group", "/etc/hosts", "/etc/localtime"):
            self.assertIn(("--ro-bind-try", item, item), spec)
        for _mode, src, _dest in spec:
            self.assertFalse(src.startswith("/etc/ssh") or src == "/etc")

    def test_no_whole_machine_or_home_bind(self):
        args = linux_args(readable=("/data/toolset",))
        for mode, src, _dest in binds(args):
            self.assertNotIn(src, ("/", "/home", "/root", "/Users"))
        self.assertNotIn("/home", args)
        self.assertNotIn("/root", args)

    def test_readable_grants_and_scratch(self):
        spec = binds(linux_args(readable=("/opt/runtime", "/data/lib"),))
        self.assertIn(("--ro-bind", "/opt/runtime", "/opt/runtime"), spec)
        self.assertIn(("--ro-bind", "/data/lib", "/data/lib"), spec)
        self.assertIn(("--bind", SCRATCH, SCRATCH), spec)

    def test_protected_dir_masked_by_empty_read_only_dir(self):
        args = linux_args(writable=True, protected=((WS + "/conf", True),))
        spec = binds(args)
        mask_src = SCRATCH + posix._MASK_SUFFIX
        mask = ("--ro-bind", mask_src, WS + "/conf")
        self.assertIn(mask, spec)
        # mask sits on top of the writable workspace bind
        self.assertGreater(args.index(WS + "/conf"), args.index(WS))

    def test_mask_source_is_host_side_not_guest_writable(self):
        # the mask root must be a sibling of scratch — scratch itself is
        # bound writable, so a mask under it could be repopulated by the
        # child and the masked dir would no longer be empty
        mask_src = SCRATCH + posix._MASK_SUFFIX
        self.assertFalse(posix._posix_under(mask_src, SCRATCH))
        args = linux_args(protected=((WS + "/conf", True),))
        for _mode, src, _dest in binds(args):
            self.assertFalse(posix._posix_under(src, SCRATCH)
                             if src != SCRATCH else False)

    def test_protected_file_masked_by_empty_read_only_file(self):
        args = linux_args(writable=True, protected=((WS + "/secret.txt", False),))
        mask_file = SCRATCH + posix._MASK_SUFFIX + "/" + posix._MASK_FILE
        self.assertIn(("--ro-bind", mask_file, WS + "/secret.txt"), binds(args))
        # a ro-bind of a host-side empty regular file cannot leak the
        # original content and rejects writes with EROFS

    def test_protected_entries_inside_masked_dirs_are_skipped(self):
        args = linux_args(protected=((WS + "/conf", True),
                                     (WS + "/conf/deep/key", False),
                                     (WS + "/other", False)))
        spec = binds(args)
        mask_src = SCRATCH + posix._MASK_SUFFIX
        self.assertIn(("--ro-bind", mask_src, WS + "/conf"), spec)
        self.assertNotIn(WS + "/conf/deep/key", [dest for _, _, dest in spec])
        self.assertIn(("--ro-bind", mask_src + "/" + posix._MASK_FILE, WS + "/other"), spec)

    def test_tool_bound_only_when_outside_covered_roots(self):
        self.assertIn(("--ro-bind", TOOL, TOOL), binds(linux_args()))
        spec = binds(linux_args(argv=("/usr/bin/python3", "-c", "x")))
        self.assertNotIn("/usr/bin/python3", [src for _, src, _ in spec])
        spec = binds(linux_args(argv=(WS + "/inside.sh",)))
        self.assertNotIn(WS + "/inside.sh", [src for _, src, _ in spec])

    def test_env_is_whitelisted_and_sorted_without_clearenv(self):
        args = linux_args(env={"B": "2", "A": "1"})
        # no --clearenv: it needs bwrap >= 0.5; the empty outer env
        # passed to the bwrap process achieves the same on 0.4.x
        self.assertNotIn("--clearenv", args)
        self.assertLess(args.index("A"), args.index("B"))
        pairs = [(args[i + 1], args[i + 2]) for i, t in enumerate(args) if t == "--setenv"]
        self.assertEqual(pairs, [("A", "1"), ("B", "2")])

    def test_child_starts_in_workspace(self):
        args = linux_args()
        chdir = args.index("--chdir")
        self.assertEqual(args[chdir + 1], WS)
        self.assertLess(chdir, args.index("--"))

    def test_double_dash_separates_argv(self):
        args = linux_args(argv=(TOOL, "-x", "y"))
        dash = args.index("--")
        self.assertEqual(args[dash + 1:], [TOOL, "-x", "y"])


class SeatbeltCompileTests(unittest.TestCase):

    def profile(self, **overrides):
        request = {
            "workspace": WS, "argv": (TOOL, "run"),
            "writable": False, "protected": (), "readable": (),
            "scratch": SCRATCH,
        }
        request.update(overrides)
        argv = request.pop("argv")
        return posix._seatbelt_profile(request.pop("workspace"), argv, **request)

    def test_deterministic(self):
        a = self.profile(protected=(WS + "/b", WS + "/a"), readable=("/opt/x",))
        b = self.profile(protected=(WS + "/a", WS + "/b"), readable=("/opt/x",))
        self.assertEqual(a, b)

    def test_deny_default_and_never_any_network_allow(self):
        text = self.profile()
        self.assertIn("(deny default)", text)
        self.assertIn("(deny network*)", text)
        self.assertNotIn("allow network", text)

    def test_narrow_reads_system_workspace_grants_scratch_tool(self):
        text = self.profile(readable=("/opt/runtime",))
        for path in ("/usr", "/System", WS, SCRATCH, "/opt/runtime"):
            self.assertIn(f'(subpath "{path}")', text)
        self.assertIn(f'(literal "{TOOL}")', text)

    def test_no_broad_system_trees(self):
        text = self.profile()
        for broad in ('(subpath "/")', '(subpath "/Users")', '(subpath "/home")',
                      '(subpath "/Library")', '(subpath "/private/etc")',
                      '(subpath "/private")', '(subpath "/dev")'):
            self.assertNotIn(broad, text)
        # devices are narrowed to literal entries, not the /dev tree
        for dev in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            self.assertIn(f'(literal "{dev}")', text)

    def test_write_scope_matches_writable_flag(self):
        ro = self.profile()
        write_line = next(l for l in ro.splitlines() if l.startswith("(allow file-write* (subpath"))
        self.assertIn(f'(subpath "{SCRATCH}")', write_line)
        self.assertNotIn(f'(subpath "{WS}")', write_line)
        rw = self.profile(writable=True)
        write_line = next(l for l in rw.splitlines() if l.startswith("(allow file-write* (subpath"))
        self.assertIn(f'(subpath "{WS}")', write_line)
        self.assertIn(f'(subpath "{SCRATCH}")', write_line)

    def test_protected_denied_after_allows_for_read_and_write(self):
        text = self.profile(writable=True, protected=(WS + "/secret",))
        deny = text.index('(deny file-read* file-map-executable file-write* '
                          f'(subpath "{WS}/secret"))')
        allow = text.index("(allow file-write* (subpath")
        self.assertGreater(deny, allow)  # last match wins

    def test_path_quoting_escapes(self):
        weird = '/tmp/has "quote" \\ back'
        text = self.profile(readable=(weird,))
        self.assertIn('(subpath "/tmp/has \\"quote\\" \\\\ back")', text)

    def test_process_inheritance_minimal_ops(self):
        text = self.profile()
        self.assertIn("(allow process-fork)", text)
        self.assertIn("(allow process-exec)", text)
        self.assertIn("(allow signal (target self))", text)
        self.assertNotIn("allow mach-lookup", text)
        self.assertNotIn("allow sysctl", text)


class ProbeTests(unittest.TestCase):

    def test_unsupported_platform_reports_false(self):
        if posix._posix_platform() != "unsupported":
            self.skipTest("only for hosts without a POSIX backend")
        ok, detail = posix.probe()
        self.assertFalse(ok)
        self.assertIn("sys.platform", detail)

    def test_mock_platform_dispatch(self):
        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_probe_linux", return_value=(True, "x")) as stub:
            self.assertEqual(posix.probe(), (True, "x"))
            stub.assert_called_once()

    def test_probe_never_raises_on_backend_error(self):
        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_probe_linux", side_effect=RuntimeError("boom")):
            ok, detail = posix.probe()
        self.assertFalse(ok)
        self.assertIn("boom", detail)

    def test_linux_probe_requires_trusted_bwrap(self):
        with mock.patch.object(posix, "_trusted_tool", return_value=None):
            ok, detail = posix._probe_linux()
        self.assertFalse(ok)
        self.assertIn("bubblewrap", detail)

    def test_linux_probe_self_test_failure(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"bwrap: no permissions")
        with mock.patch.object(posix, "_trusted_tool", side_effect=lambda c: c[0]), \
             mock.patch.object(posix, "os") as fpos, \
             mock.patch.object(posix.subprocess, "run", return_value=failed):
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            ok, detail = posix._probe_linux()
        self.assertFalse(ok)
        self.assertIn("no permissions", detail)

    def test_linux_probe_self_test_pass(self):
        good = subprocess.CompletedProcess([], 0, b"", b"")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return good

        with mock.patch.object(posix, "_trusted_tool", side_effect=lambda c: c[0]), \
             mock.patch.object(posix, "os") as fpos, \
             mock.patch.object(posix.subprocess, "run", side_effect=fake_run):
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            ok, detail = posix._probe_linux()
        self.assertTrue(ok)
        self.assertIn("self-test", detail)
        # the self-test exercises the same namespaces, runtime and cwd
        # policy as execute() — the last call is it (first is --version)
        self_test = calls[-1]
        self.assertEqual(self_test[0], "/usr/bin/bwrap")
        for token in ("--unshare-net", "--unshare-user", "--chdir"):
            self.assertIn(token, self_test)
        self.assertNotIn("--clearenv", self_test)
        spec = binds(self_test)
        self.assertIn(("--ro-bind-try", "/usr", "/usr"), spec)
        self.assertIn(("--ro-bind-try", "/lib64", "/lib64"), spec)
        self.assertEqual(self_test[-1], "/usr/bin/true")

    def test_trusted_tool_accepts_system_install(self):
        file_info = mock.Mock(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        dir_info = mock.Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        with mock.patch.object(posix.os.path, "realpath", side_effect=lambda p: p), \
             mock.patch.object(posix.os, "stat",
                               side_effect=lambda p: file_info if p.endswith("tool") else dir_info), \
             mock.patch.object(posix.os, "access", return_value=True):
            self.assertEqual(posix._trusted_tool(("/usr/bin/tool",)), "/usr/bin/tool")

    def test_trusted_tool_rejects_writable_ancestry_and_owner(self):
        file_info = mock.Mock(st_mode=stat.S_IFREG | 0o755, st_uid=0)
        writable_dir = mock.Mock(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        with mock.patch.object(posix.os.path, "realpath", side_effect=lambda p: p), \
             mock.patch.object(posix.os, "stat",
                               side_effect=lambda p: file_info if p.endswith("tool") else writable_dir), \
             mock.patch.object(posix.os, "access", return_value=True):
            self.assertIsNone(posix._trusted_tool(("/usr/bin/tool",)))
        user_file = mock.Mock(st_mode=stat.S_IFREG | 0o755, st_uid=1000)
        with mock.patch.object(posix.os.path, "realpath", side_effect=lambda p: p), \
             mock.patch.object(posix.os, "stat", return_value=user_file), \
             mock.patch.object(posix.os, "access", return_value=True):
            self.assertIsNone(posix._trusted_tool(("/usr/bin/tool",)))

    def test_darwin_probe_requires_sandbox_exec(self):
        with mock.patch.object(posix, "_trusted_tool", return_value=None):
            ok, detail = posix._probe_darwin()
        self.assertFalse(ok)
        self.assertIn("sandbox-exec", detail)


class ExecutePlumbingTests(unittest.TestCase):
    """Launch-path tests; subprocess is mocked so they run on any host."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sb-posix-test-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()

    def test_execute_fails_closed_on_unsupported_platform(self):
        if posix._posix_platform() != "unsupported":
            self.skipTest("only for hosts without a POSIX backend")
        with self.assertRaises(core.BackendUnavailableError):
            posix.execute(self.workspace, ("/bin/true",), writable=False,
                          protected=(), readable=(), scratch=self.scratch,
                          env={}, timeout=5.0)

    def test_tool_under_protected_path_rejected(self):
        ws = PurePosixPath("/ws")
        with self.assertRaises(core.PolicyError):
            posix.execute(ws, ("/ws/conf/run",),
                          writable=True, protected=(ws / "conf",), readable=(),
                          scratch=PurePosixPath("/tmp/sc"), env={}, timeout=5.0)

    def test_linux_invokes_bwrap_with_compiled_args(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return subprocess.CompletedProcess(cmd, 0, b"out", b"err")

        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/bwrap"), \
             mock.patch.object(posix, "os") as fpos, \
             mock.patch.object(posix.subprocess, "run", side_effect=fake_run):
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            result = posix.execute(self.workspace, ("/opt/t",),
                                   writable=False, protected=(), readable=(),
                                   scratch=self.scratch,
                                   env={"PATH": str(self.scratch)}, timeout=7.5)
        self.assertEqual(captured["cmd"][0], "/usr/bin/bwrap")
        self.assertEqual(captured["kw"]["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["kw"]["timeout"], 7.5)
        self.assertEqual(captured["kw"]["env"], posix._LAUNCH_ENV)
        self.assertEqual(result.args, ("/opt/t",))
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.returncode, 0)

    def test_linux_mask_dir_lifecycle_next_to_scratch(self):
        (self.workspace / "conf").mkdir()
        mask_host = Path(str(self.scratch) + posix._MASK_SUFFIX)
        seen = {}

        def fake_run(cmd, **kw):
            seen["dir"] = mask_host.is_dir()
            seen["file"] = (mask_host / posix._MASK_FILE).is_file()
            seen["bind"] = ("--ro-bind", str(mask_host),
                            str(self.workspace / "conf")) in binds(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/bwrap"), \
             mock.patch.object(posix, "os") as fpos, \
             mock.patch.object(posix.subprocess, "run", side_effect=fake_run):
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            posix.execute(self.workspace, ("/opt/t",), writable=True,
                          protected=(self.workspace / "conf",), readable=(),
                          scratch=self.scratch, env={}, timeout=5.0)
        self.assertTrue(seen["dir"])   # mask source existed at launch
        self.assertTrue(seen["file"])  # regular-file mask existed too
        self.assertTrue(seen["bind"])  # bound over the protected dir
        self.assertFalse(mask_host.exists())  # host-side cleanup ran

    def test_linux_mask_dir_never_removes_preexisting(self):
        (self.workspace / "conf").mkdir()
        mask_host = Path(str(self.scratch) + posix._MASK_SUFFIX)
        mask_host.mkdir()
        sentinel = mask_host / "keep.txt"
        sentinel.write_text("x")
        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/bwrap"), \
             mock.patch.object(posix, "os") as fpos:
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            # mkdir on an existing path fails and nothing is deleted
            with self.assertRaises(FileExistsError):
                posix.execute(self.workspace, ("/opt/t",), writable=True,
                              protected=(self.workspace / "conf",), readable=(),
                              scratch=self.scratch, env={}, timeout=5.0)
        self.assertTrue(sentinel.is_file())

    def test_linux_missing_bwrap_raises_backend_unavailable(self):
        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_trusted_tool", return_value=None):
            with self.assertRaises(core.BackendUnavailableError):
                posix.execute(self.workspace, ("/opt/t",), writable=False,
                              protected=(), readable=(), scratch=self.scratch,
                              env={}, timeout=5.0)

    def test_linux_timeout_propagates(self):
        with mock.patch.object(posix.sys, "platform", "linux"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/bwrap"), \
             mock.patch.object(posix, "os") as fpos, \
             mock.patch.object(posix.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["bwrap"], 1.0)):
            fpos.getuid.return_value = 1000
            fpos.getgid.return_value = 1000
            with self.assertRaises(subprocess.TimeoutExpired):
                posix.execute(self.workspace, ("/opt/t",), writable=False,
                              protected=(), readable=(), scratch=self.scratch,
                              env={}, timeout=1.0)

    def test_darwin_invokes_sandbox_exec_with_profile(self):
        captured = {}
        proc = mock.Mock()
        proc.communicate.return_value = (b"out", b"err")
        proc.returncode = 3

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return proc

        with mock.patch.object(posix.sys, "platform", "darwin"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/sandbox-exec"), \
             mock.patch.object(posix.subprocess, "Popen", side_effect=fake_popen):
            result = posix.execute(self.workspace, ("/opt/t", "a"),
                                   writable=True, protected=(), readable=(),
                                   scratch=self.scratch, env={"TMPDIR": str(self.scratch)},
                                   timeout=9.0)
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "/usr/bin/sandbox-exec")
        self.assertEqual(cmd[1], "-p")
        self.assertIn("(deny default)", cmd[2])
        self.assertEqual(cmd[3:], ["/opt/t", "a"])
        self.assertTrue(captured["kw"]["start_new_session"])
        self.assertEqual(captured["kw"]["cwd"], str(self.workspace))
        self.assertEqual(captured["kw"]["env"], {"TMPDIR": str(self.scratch)})
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b"out")
        proc.communicate.assert_called_once_with(timeout=9.0)

    def test_darwin_timeout_kills_process_group(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["t"], 1.0), (b"partial", b"pe")]
        killpg = mock.Mock()
        with mock.patch.object(posix.sys, "platform", "darwin"), \
             mock.patch.object(posix, "_trusted_tool", return_value="/usr/bin/sandbox-exec"), \
             mock.patch.object(posix.subprocess, "Popen", return_value=proc), \
             mock.patch.object(posix.os, "killpg", killpg, create=True):
            with self.assertRaises(subprocess.TimeoutExpired) as ctx:
                posix.execute(self.workspace, ("/opt/t",), writable=False,
                              protected=(), readable=(), scratch=self.scratch,
                              env={}, timeout=1.0)
        killpg.assert_called_once_with(4242, posix._SIGKILL)
        self.assertEqual(ctx.exception.output, b"partial")

    def test_core_run_rejects_posix_backend_early_without_probe(self):
        # fail-closed: on a host without a backend, core.run raises
        # BackendUnavailableError instead of launching anything
        if posix._posix_platform() != "unsupported":
            self.skipTest("only for hosts without a POSIX backend")
        tool = self.tmp / "tool"
        tool.write_bytes(b"t")
        with self.assertRaises(core.BackendUnavailableError):
            core.run((str(tool),), workspace=self.workspace, timeout=5.0,
                     scratch_root=self.scratch, backend=posix)


# ----------------------------------------------------------------------
# Native integration — the same cases validate Linux bwrap and macOS
# Seatbelt wherever the backend actually exists.
# ----------------------------------------------------------------------

def _first_tool(candidates):
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_NATIVE_REQUIRED = "WITTYCODE_SANDBOX_NATIVE"


class NativeIntegrationTests(unittest.TestCase):
    """End-to-end checks against the real backend.

    Skipped everywhere except a native Linux/macOS host whose probe()
    passes. Setting WITTYCODE_SANDBOX_NATIVE=1 turns "unavailable" into
    a hard failure instead — explicit native acceptance runs must not be
    silently skipped.
    """

    @classmethod
    def setUpClass(cls):
        required = bool(os.environ.get(_NATIVE_REQUIRED))
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            if required:
                raise RuntimeError(
                    f"{_NATIVE_REQUIRED} is set but this host is {sys.platform}, "
                    "not a POSIX target")
            raise unittest.SkipTest("native POSIX host required")
        ok, detail = posix.probe()
        if not ok:
            if required:
                raise RuntimeError(
                    f"{_NATIVE_REQUIRED}: backend probe failed: {detail}")
            raise unittest.SkipTest(f"backend probe failed: {detail}")
        cls.probe_detail = detail

    def setUp(self):
        base = os.environ.get(core.TEST_WORK)
        self.tmp = Path(tempfile.mkdtemp(prefix="sb-posix-it-", dir=base)).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.scratch_root = self.tmp / "scratch-root"
        self.scratch_root.mkdir()
        self.sh = _first_tool(("/bin/sh", "/usr/bin/sh"))
        if self.sh is None:
            self.skipTest("no /bin/sh available")

    def run_cmd(self, argv, **kw):
        kw.setdefault("timeout", 30.0)
        return core.run(argv, workspace=self.workspace, backend=posix,
                        scratch_root=self.scratch_root, **kw)

    def test_probe_reports_backend(self):
        ok, detail = posix.probe()
        self.assertTrue(ok, detail)

    def test_workspace_writable_when_requested(self):
        target = self.workspace / "made.txt"
        result = self.run_cmd((self.sh, "-c", f'echo hi > "{target}" && echo done'),
                              writable=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text().strip(), "hi")

    def test_workspace_read_only_denies_write(self):
        target = self.workspace / "nope.txt"
        result = self.run_cmd((self.sh, "-c", f'echo x > "{target}"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_child_cwd_is_workspace(self):
        result = self.run_cmd((self.sh, "-c", "pwd"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().decode(), str(self.workspace))

    def test_protected_file_neither_readable_nor_writable(self):
        secret = self.workspace / "secret.txt"
        secret.write_text("TOP-SECRET-42")
        script = (f'cat "{secret}" 2>/dev/null; '
                  f'echo x >> "{secret}" 2>/dev/null; echo wr=$?')
        result = self.run_cmd((self.sh, "-c", script), writable=True,
                              protected=(secret,))
        self.assertNotIn(b"TOP-SECRET-42", result.stdout)
        self.assertNotIn(b"wr=0", result.stdout)
        self.assertEqual(secret.read_text(), "TOP-SECRET-42")

    def test_protected_dir_hidden_and_not_writable(self):
        conf = self.workspace / "conf"
        conf.mkdir()
        (conf / "hostfile").write_text("x")
        script = (f'ls "{conf}" 2>/dev/null; echo rc=$?; '
                  f'echo y > "{conf}/new" 2>/dev/null; echo wr=$?')
        result = self.run_cmd((self.sh, "-c", script), writable=True,
                              protected=(conf,))
        self.assertNotIn(b"hostfile", result.stdout)
        self.assertNotIn(b"wr=0", result.stdout)
        self.assertFalse((conf / "new").exists())
        self.assertTrue((conf / "hostfile").exists())

    def test_readable_grant_read_only(self):
        grant = self.tmp / "toolset"
        grant.mkdir()
        (grant / "data.txt").write_text("GRANT-OK")
        script = (f'cat "{grant}/data.txt"; '
                  f'echo z > "{grant}/new" 2>/dev/null; echo wr=$?')
        result = self.run_cmd((self.sh, "-c", script), writable=True,
                              readable=(grant,))
        self.assertIn(b"GRANT-OK", result.stdout)
        self.assertNotIn(b"wr=0", result.stdout)
        self.assertFalse((grant / "new").exists())

    def test_scratch_writable_via_tmpdir(self):
        result = self.run_cmd((self.sh, "-c",
                               'echo s > "$TMPDIR/s"; cat "$TMPDIR/s"; '
                               'echo "PATH=$PATH"; echo "TMPDIR=$TMPDIR"'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"s", result.stdout)
        # PATH is the fixed system tool path, TMPDIR the per-run scratch
        self.assertIn(b"PATH=/usr/bin:/bin", result.stdout)
        self.assertRegex(result.stdout, rb"TMPDIR=\S*wittycode-sandbox-")

    def test_env_isolated_and_whitelisted(self):
        os.environ["WITTYCODE_LEAK_MARKER"] = "SHOULD-NOT-APPEAR"
        self.addCleanup(lambda: os.environ.pop("WITTYCODE_LEAK_MARKER", None))
        result = self.run_cmd((self.sh, "-c",
                               'echo "FOO=$FOO"; echo "HOST=${WITTYCODE_LEAK_MARKER:-none}"'),
                              env={"FOO": "bar"})
        self.assertIn(b"FOO=bar", result.stdout)
        self.assertIn(b"HOST=none", result.stdout)

    def test_subprocess_inherits_restrictions(self):
        target = self.workspace / "deep.txt"
        # a grandchild cannot write the read-only workspace either
        result = self.run_cmd((self.sh, "-c",
                               f'{self.sh} -c "echo d > {target}"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        # but can still use the scratch
        result = self.run_cmd((self.sh, "-c",
                               f'{self.sh} -c "echo deep > $TMPDIR/d; cat $TMPDIR/d"'))
        self.assertIn(b"deep", result.stdout)

    def test_network_denied_even_loopback(self):
        py = _first_tool(("/usr/bin/python3", "/usr/local/bin/python3",
                          "/opt/homebrew/bin/python3", "/bin/python3"))
        if py is None:
            self.skipTest("no python3 to run the client")
        # a host listener created by this test run proves the sandbox
        # cannot reach host services even on loopback
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        # the same interpreter must reach it from the host first,
        # otherwise the sandbox denial below proves nothing
        host = subprocess.run(
            [py, "-c",
             "import socket\n"
             f"socket.create_connection(('127.0.0.1', {port}), 5)\n"
             "print('REACHABLE')\n"],
            capture_output=True, timeout=10)
        self.assertEqual(host.returncode, 0, host.stdout + host.stderr)
        self.assertIn(b"REACHABLE", host.stdout)
        # inside the sandbox the identical connect is refused — the
        # STARTED marker separates "denied" from "client never ran"
        script = (
            "import socket\n"
            "print('STARTED')\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), 2)\n"
            "except OSError:\n"
            "    print('DENIED')\n"
            "    raise SystemExit(1)\n"
            "print('CONNECTED')\n")
        result = self.run_cmd((py, "-c", script))
        self.assertIn(b"STARTED", result.stdout)
        self.assertNotIn(b"CONNECTED", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(b"DENIED", result.stdout)

    def test_timeout_kills_sandbox(self):
        sleep = _first_tool(("/bin/sleep", "/usr/bin/sleep"))
        if sleep is None:
            self.skipTest("no sleep binary found")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_cmd((sleep, "30"), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
