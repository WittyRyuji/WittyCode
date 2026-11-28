"""Platform proof tests for the Windows MXC backend (desktop.sandbox.windows).

These tests launch the real Microsoft MXC ProcessContainer through the
official ``@microsoft/mxc-sdk`` Node SDK and are meant for the managed test
runner::

    python desktop/core/test_runs.py -- python -m desktop.sandbox.test_windows -v

Bootstrap (per run, inside WITTYCODE_TEST_WORK only):

* download ``@microsoft/mxc-sdk`` 1.1.0 from the official npm registry,
  verify the tarball against the pinned SHA-512 below, extract it without
  running any install scripts, then Authenticode-check the shipped binaries;
* resolve the host's Node >= 24 interpreter once and pin it, plus the
  extracted SDK layout, into WITTYCODE_MXC_NODE / WITTYCODE_MXC_SDK — the two
  explicit, trusted configuration points the backend accepts.

Nothing is installed into the user environment and nothing outside
WITTYCODE_TEST_WORK (or a fallback temp dir when run without the managed
runner) is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
import zipfile
from base64 import b64decode
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desktop.sandbox import core  # noqa: E402
from desktop.sandbox import windows  # noqa: E402

SDK_VERSION = "1.1.0"
NODE_ENV, SDK_ENV = windows.NODE_ENV, windows.SDK_ENV
# Pinned npm tarballs (name, install path under node_modules/, version, integrity).
# The SDK's runtime deps are vendored the same way; install scripts never run.
_PINNED_PACKAGES = (
    ("node_modules/@microsoft/mxc-sdk", "mxc-sdk", "1.1.0",
     "https://registry.npmjs.org/@microsoft/mxc-sdk/-/mxc-sdk-1.1.0.tgz",
     "tSOPZ14ddCiXHc4hVNQp5Yi22HSQEfXh58Z4O/tLzERi+23rfRmMrJoxVK5jZYhlll68u4eF/UlQSaXWyUMj4w=="),
    ("node_modules/koffi", "koffi", "3.3.8",
     "https://registry.npmjs.org/koffi/-/koffi-3.3.8.tgz",
     "CTDlV3BDh8LFyCgv9vamD+5eu49HQ5wBetAJ9wxERAoAy6rfodahgo28tqhz010l2OGcZvrnBC6MOTyQUt4EIQ=="),
    ("node_modules/semver", "semver", "7.7.4",
     "https://registry.npmjs.org/semver/-/semver-7.7.4.tgz",
     "vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA=="),
    ("node_modules/node-pty", "node-pty", "1.1.0",
     "https://registry.npmjs.org/node-pty/-/node-pty-1.1.0.tgz",
     "20JqtutY6JPXTUnL0ij1uad7Qe1baT46lyolh2sSENDd4sTzKZ4nmAFkeAARDKwmlLjPx6XKRlwRUxwjOy+lUg=="),
    ("node_modules/@koromix/koffi-win32-x64", "koffi-win32-x64", "3.3.8",
     "https://registry.npmjs.org/@koromix/koffi-win32-x64/-/koffi-win32-x64-3.3.8.tgz",
     "Ef9XpIn/AfnZWMI1lvA6IOjCsTVRlako+BXosp4cRaLZZXkgjwiBVqYXXhqZix1G8e+27UAv97XBTmDEyfS7jA=="),
)
CMD = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "cmd.exe"
PWSH = (Path(os.environ.get("SystemRoot") or r"C:\Windows")
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

_WORK_ROOT: Path | None = None
_SDK_DIR: Path | None = None
_NODE: Path | None = None
_SIGNATURES: dict[str, str] = {}
_BOOTSTRAP_ERROR: str | None = None


def _work_root() -> Path:
    global _WORK_ROOT
    if _WORK_ROOT is None:
        base = os.environ.get(core.TEST_WORK)
        _WORK_ROOT = Path(base) if base else Path(tempfile.mkdtemp(prefix="wittycode-mxc-test-"))
        _WORK_ROOT.mkdir(exist_ok=True)
    return _WORK_ROOT


def _fetch_one(target: Path, name: str, version: str, url: str, sha512_b64: str) -> None:
    if (target / "package.json").is_file():
        return
    tarball = _work_root() / f"{name}-{version}.tgz"
    if not tarball.is_file():
        with urllib.request.urlopen(url, timeout=120) as response:
            tarball.write_bytes(response.read())
    digest = hashlib.sha512(tarball.read_bytes()).digest()
    if digest != b64decode(sha512_b64):
        raise RuntimeError(f"{name} {version} sha512 mismatch: {digest.hex()}")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if parts[:1] != ("package",) or len(parts) < 2:
                continue
            member.name = str(Path(*parts[1:]))
            if member.name.startswith(("..", "/", "\\")) or ":" in member.name:
                continue
            archive.extract(member, target)


def _fetch_sdk() -> Path:
    global _SDK_DIR
    if _SDK_DIR is not None:
        return _SDK_DIR
    sdk_dir = _work_root() / "mxc-sdk"
    for rel, name, version, url, sha512 in _PINNED_PACKAGES:
        _fetch_one(sdk_dir / rel, name, version, url, sha512)
    _SDK_DIR = sdk_dir
    return sdk_dir


def _signature_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    ps = shutil.which("powershell") or str(PWSH)
    command = (f"$s = Get-AuthenticodeSignature '{path}'; "
               "$s.Status.ToString() + '|' + $s.SignerCertificate.Subject")
    proc = subprocess.run(
        [ps, "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=60)
    return (proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr).strip() else "?"


def _node_tag(path: str | Path) -> tuple[int, int] | None:
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10)
        parts = proc.stdout.strip().lstrip("v").split(".")
        return int(parts[0]), int(parts[1])
    except (OSError, ValueError, IndexError):
        return None


def _node_supported(tag: tuple[int, int] | None) -> bool:
    # The SDK's native stdio requires Node 24.21+ within 24.x, or 26.8+.
    if tag is None:
        return False
    major, minor = tag
    return (major == 24 and minor >= 21) or (major > 26 or (major == 26 and minor >= 8))


def _fetch_pinned_node() -> Path:
    """Download the latest official Node 24.x win-x64 zip, sha256-verified."""
    dest = _work_root() / "node-runtime" / "node.exe"
    if dest.is_file():
        return dest
    base = "https://nodejs.org/dist/latest-v24.x/"
    shasums = urllib.request.urlopen(base + "SHASUMS256.txt", timeout=60).read().decode()
    name = sha = None
    for line in shasums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith("-win-x64.zip"):
            sha, name = parts
    if name is None or sha is None:
        raise RuntimeError("no win-x64 zip in Node SHASUMS256.txt")
    archive_path = _work_root() / name
    if not archive_path.is_file():
        archive_path.write_bytes(urllib.request.urlopen(base + name, timeout=300).read())
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != sha:
        raise RuntimeError(f"{name} sha256 mismatch")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.endswith("/node.exe"):
                dest.write_bytes(archive.read(member))
                break
        else:
            raise RuntimeError(f"no node.exe inside {name}")
    status = _signature_status(dest)
    if not status.startswith("Valid"):
        raise RuntimeError(f"downloaded node.exe signature not valid: {status}")
    return dest


def _resolve_trusted_node() -> Path:
    candidate = shutil.which("node")
    if candidate and _node_supported(_node_tag(candidate)):
        return Path(candidate)
    pinned = _fetch_pinned_node()
    if not _node_supported(_node_tag(pinned)):
        raise RuntimeError(f"pinned node.exe failed its version check: {pinned}")
    return pinned


def _bootstrap() -> None:
    """Resolve the trusted Node + verified SDK once; records _BOOTSTRAP_ERROR."""
    global _NODE, _BOOTSTRAP_ERROR
    if _BOOTSTRAP_ERROR is not None or _NODE is not None:
        return
    try:
        _NODE = _resolve_trusted_node()
        sdk = _fetch_sdk()
        arch = "x64" if "64" in os.environ.get("PROCESSOR_ARCHITECTURE", "") else "arm64"
        for name in ("wxc-exec.exe", "mxc_ffi.dll"):
            _SIGNATURES[name] = _signature_status(
                sdk / "node_modules" / "@microsoft" / "mxc-sdk" / "bin" / arch / name)
        os.environ[NODE_ENV] = str(_NODE)
        os.environ[SDK_ENV] = str(sdk)
    except Exception as error:  # bootstrap failure is reported, not hidden
        _BOOTSTRAP_ERROR = f"{type(error).__name__}: {error}"


class MxcBackendTest(unittest.TestCase):
    """Direct calls into desktop.sandbox.windows against the real container."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("WITTYCODE_TEST_MXC") != "1":
            raise unittest.SkipTest(
                "native MXC run disabled; rerun in the managed runner with WITTYCODE_TEST_MXC=1")
        cls._saved_env = {name: os.environ.get(name) for name in (NODE_ENV, SDK_ENV)}
        _bootstrap()
        cls.scratch = _work_root() / "scratch-run"
        cls.scratch.mkdir(exist_ok=True)
        cls.ws = _work_root() / "workspace"
        cls.ws.mkdir(exist_ok=True)
        cls.outside = _work_root() / "outside"
        cls.outside.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        saved = getattr(cls, "_saved_env", {})
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def setUp(self) -> None:
        if _BOOTSTRAP_ERROR is not None:
            self.fail(f"backend bootstrap blocked: {_BOOTSTRAP_ERROR}")

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return core.build_env(self.scratch, overrides=extra)

    def _execute(self, args: tuple[str, ...], **kwargs):
        return windows.execute(
            self.ws, (str(CMD), "/d", "/s", "/c", *args) if len(args) == 1 else args,
            env=self._env(kwargs.pop("env", None)), scratch=self.scratch,
            writable=kwargs.pop("writable", False), protected=kwargs.pop("protected", ()),
            readable=kwargs.pop("readable", ()), timeout=kwargs.pop("timeout", 60.0))

    # --- availability --------------------------------------------------------

    def test_probe_available(self):
        ok, detail = windows.probe()
        self.assertTrue(ok, f"probe rejected: {detail}")
        self.assertIn("processcontainer", detail)
        print(f"\nprobe: {detail}")
        print(f"signatures: {_SIGNATURES}")

    def test_probe_failure_never_launches(self):
        marker = self.ws / "should-not-exist.txt"
        node, sdk = os.environ.pop(NODE_ENV), os.environ.pop(SDK_ENV)
        self.addCleanup(os.environ.update, {NODE_ENV: node, SDK_ENV: sdk})
        os.environ[NODE_ENV] = str(self.ws / "no-such-node.exe")
        ok, detail = windows.probe()
        self.assertFalse(ok)
        self.assertIn(NODE_ENV, detail)
        with self.assertRaises((FileNotFoundError, RuntimeError)):
            windows.execute(self.ws, (str(CMD), "/d", "/s", "/c", "echo x> should-not-exist.txt"),
                            env=self._env(), scratch=self.scratch, writable=True,
                            protected=(), readable=(), timeout=30.0)
        self.assertFalse(marker.exists())

    # --- positive cases ------------------------------------------------------

    def test_echo_and_env(self):
        env = {"WITTY_MARK": "mxc-ok"}
        result = self._execute(("echo %WITTY_MARK%",), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"mxc-ok", result.stdout)

    def test_workspace_writable_and_scratch(self):
        # %TEMP% is virtualized by AppContainer; write the real granted path.
        # (paths under WITTYCODE_TEST_WORK have no spaces; inner " would break cmd /c)
        result = self._execute(
            (f"echo data>made.txt & echo scr>{self.scratch}\\scratch.txt 2>&1 & exit /b 0",),
            writable=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.ws / "made.txt").read_text().strip(), "data")
        self.assertEqual((self.scratch / "scratch.txt").read_text().strip(), "scr",
                        f"workload said: {result.stdout!r} {result.stderr!r}")

    def test_workspace_readonly_and_readable_grant(self):
        grant = _work_root() / "grant-root"
        grant.mkdir(exist_ok=True)
        (grant / "note.txt").write_text("granted")
        (self.ws / "ro.txt").write_text("readonly")
        cmd = ("type ro.txt 2>&1 & type %s\\note.txt 2>&1 & echo new>denied.txt 2>nul "
               "& if exist denied.txt (exit /b 9) else (exit /b 0)") % grant
        result = self._execute((cmd,), readable=(grant,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"readonly", result.stdout)
        self.assertIn(b"granted", result.stdout,
                      f"workload said: {result.stdout!r} {result.stderr!r}")
        self.assertFalse((self.ws / "denied.txt").exists())

    # --- negative cases ------------------------------------------------------

    def test_protected_denied(self):
        secret = self.ws / "protected.txt"
        secret.write_text("sensitive")
        read = self._execute(("type protected.txt",), writable=True, protected=(secret,))
        self.assertNotEqual(read.returncode, 0, read.stdout)
        write = self._execute(("echo evil>>protected.txt",), writable=True, protected=(secret,))
        self.assertNotEqual(write.returncode, 0, write.stdout + write.stderr)
        self.assertEqual(secret.read_text(), "sensitive")

    def test_outside_denied(self):
        (self.outside / "neighbor.txt").write_text("neighbor")
        result = self._execute(
            (f"type \"{self.outside / 'neighbor.txt'}\" 2>nul & if errorlevel 1 "
             "(exit /b 0) else (exit /b 7)",))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_network_denied_loopback(self):
        """Same trusted python.exe, single thread: on the host connect_ex
        returns 0 and the listener accepts; inside the container it returns
        a non-zero errno (10013 measured), the child still exits 0, and the
        listener sees no second connection."""
        py = Path(sys.base_prefix) / "python.exe"
        self.assertTrue(py.is_file(), "host python.exe missing")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2.0)
        port = listener.getsockname()[1]
        script = ("import socket; s = socket.socket(); "
                  f"print(s.connect_ex(('127.0.0.1', {port})), end='')")
        host = subprocess.run([str(py), "-I", "-S", "-c", script],
                              capture_output=True, timeout=15)
        self.assertEqual(host.returncode, 0, host.stderr)
        self.assertEqual(host.stdout.strip(), b"0")
        conn, _ = listener.accept()
        conn.close()
        result = core.run(
            (str(py), "-I", "-S", "-c", script), workspace=self.ws,
            readable=(py.parent,), scratch_root=_work_root(), timeout=45.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), b"0")
        print(f"\nnetwork: host errno 0 (accepted); container errno "
              f"{result.stdout.strip().decode()!r}; second accept must time out")
        with self.assertRaises(TimeoutError):
            listener.accept()
        listener.close()

    def test_core_run_workspace_writable_neighbor_denied(self):
        """Public core.run API: an absolute-path Python child writes inside
        the workspace (host verifies the file), and reading the neighbour
        fails with a non-zero child exit — not a host-side error."""
        py = Path(sys.base_prefix) / "python.exe"
        (self.outside / "neighbor.txt").write_text("neighbor")
        wrote = core.run(
            (str(py), "-I", "-S", "-c",
             f"open({str(self.ws / 'py.txt')!r}, 'w').write('ok')"),
            workspace=self.ws, writable=True, readable=(py.parent,),
            scratch_root=_work_root(), timeout=45.0)
        self.assertEqual(wrote.returncode, 0, wrote.stderr)
        self.assertEqual((self.ws / "py.txt").read_text(encoding="utf-8"), "ok")
        denied = core.run(
            (str(py), "-I", "-S", "-c",
             f"open({str(self.outside / 'neighbor.txt')!r}).read()"),
            workspace=self.ws, writable=True, readable=(py.parent,),
            scratch_root=_work_root(), timeout=45.0)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(b"Error", denied.stderr)
        self.assertFalse((self.outside / "py.txt").exists())

    def test_host_channel_immune_and_output_bounded(self):
        """A guest file named mxc-report.json inside the writable scratch is
        never read, overwritten or deleted by the host status channel; and a
        >1 MiB payload stream returns truncated-but-valid with the real exit
        code (helper frame stays intact)."""
        py = Path(sys.base_prefix) / "python.exe"
        fake = self.scratch / "mxc-report.json"
        fake.write_text("pre-seeded")
        fake_in_guest = str(self.scratch / "mxc-report.json")
        result = windows.execute(
            self.ws,
            (str(py), "-I", "-S", "-c",
             f"import sys; open({fake_in_guest!r}, 'w').write('guest-bytes');"
             f"sys.stdout.write('A' * {2 * 1024 * 1024}); sys.exit(3)"),
            env=self._env(), scratch=self.scratch, writable=False,
            protected=(), readable=(py.parent,), timeout=60.0)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(result.stdout), 512 * 1024)
        self.assertEqual(result.stdout.count(b"A"), 512 * 1024)
        self.assertEqual(fake.read_text(), "guest-bytes")

    def test_timeout_cleanup(self):
        # A cmd built-in infinite loop always hangs; the timeout must kill it.
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self._execute(("for /l %i in () do @set /a x+=1",), timeout=6.0)
        self.assertLess(time.monotonic() - started, 40)


class RuntimeGuardTest(unittest.TestCase):
    """Host-side guard rails: no downloads, no container, no network."""

    def setUp(self) -> None:
        for name in (NODE_ENV, SDK_ENV):
            old = os.environ.get(name)
            self.addCleanup(
                lambda n=name, v=old:
                    os.environ.__setitem__(n, v) if v is not None else os.environ.pop(n, None))
        self.work = _work_root()

    def _fake_sdk(self, name: str = "fake-sdk") -> Path:
        sdk = self.work / name
        package = sdk / "node_modules" / "@microsoft" / "mxc-sdk"
        package.mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text("{}")
        return sdk

    def test_relative_env_paths_are_rejected(self):
        os.environ[NODE_ENV] = "node.exe"
        os.environ[SDK_ENV] = "sdk"
        self.assertIsNone(windows.resolve_backend())
        ok, detail = windows.probe()
        self.assertFalse(ok)
        self.assertIn(NODE_ENV, detail)

    def test_workspace_inside_sdk_refused_before_probe(self):
        node = self.work / "fake-node.exe"
        node.write_bytes(b"MZ")
        sdk = self._fake_sdk()
        ws = sdk / "inner-ws"          # workspace inside the trusted SDK dir
        ws.mkdir()
        os.environ[NODE_ENV] = str(node)
        os.environ[SDK_ENV] = str(sdk)
        with self.assertRaises(RuntimeError):
            windows.validate_runtime(ws)
        with self.assertRaises(core.SandboxError):
            core.run((str(CMD), "/d", "/s", "/c", "echo hi"), workspace=ws,
                     scratch_root=self.work, timeout=5.0)
        self.assertFalse((ws / "hi").exists())

    def test_absolute_trusted_paths_still_resolve(self):
        node = self.work / "abs-node.exe"
        node.write_bytes(b"MZ")
        sdk = self._fake_sdk("abs-sdk")
        os.environ[NODE_ENV] = str(node)
        os.environ[SDK_ENV] = str(sdk)
        resolved = windows.resolve_backend()
        self.assertIsNotNone(resolved)
        node_path, sdk_path = resolved
        self.assertTrue(node_path.is_absolute() and sdk_path.is_absolute())
        self.assertEqual(node_path, node.resolve())
        self.assertEqual(sdk_path, sdk.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
