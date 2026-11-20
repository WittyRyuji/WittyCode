r"""The single-file download: unpacks its bundled copy of the release once, then starts it.

    WittyCode-<version>-win-x64.exe = this program (frozen with PyInstaller) + the release zip + a footer.

The zip keeps the whole release folder (Python, packages, the application), because the application must spawn the real
interpreter for child processes and cannot be frozen into one exe. On the first run this stub extracts that zip under
``%LOCALAPPDATA%\WittyCode\app-<version>\`` (showing a console with progress), writes a ``.complete`` marker, then starts
``WittyCode.exe`` of the extracted folder and exits. Later runs see the marker, skip extraction, and hand the command
line over immediately. User data still goes to ``%APPDATA%\WittyCode\``; deleting the ``app-*`` folders removes what
this program put down.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

MAGIC = b"WCPKG1\0\0"
FOOTER_SIZE = 8 + 32 + len(MAGIC)
"""u64 payload size | 32-byte version (utf-8, zero padded) | MAGIC, read from the end of the file."""

TITLE = "WittyCode 无法启动"


def say(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)


def own_payload(exe: Path) -> tuple[int, int, str]:
    """(start, size, version) of the zip appended to this file."""
    with exe.open("rb") as handle:
        handle.seek(-FOOTER_SIZE, os.SEEK_END)
        footer = handle.read(FOOTER_SIZE)
    size, version, magic = struct.unpack("<Q", footer[:8])[0], footer[8:40].rstrip(b"\0").decode("utf-8"), footer[40:]
    if magic != MAGIC or not version:
        raise SystemExit(say("这个文件不完整或不是 WittyCode 的单文件包。") or 1)
    end = exe.stat().st_size - FOOTER_SIZE
    if size > end:
        raise SystemExit(say("这个文件不完整或不是 WittyCode 的单文件包。") or 1)
    return end - size, size, version


def console() -> None:
    """A console for first-run progress only; later runs never see one (the stub is a GUI program)."""
    if ctypes.windll.kernel32.AllocConsole():
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        ctypes.windll.kernel32.SetConsoleTitleW("WittyCode")


def extract(exe: Path, version: str) -> Path:
    target = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "WittyCode" / f"app-{version}"
    if (target / ".complete").is_file():
        return target
    console()
    print(f"WittyCode {version}：首次运行，正在释放文件到 {target} …")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".extracting-", dir=str(target.parent)))
    try:
        count = 0
        with zipfile.ZipFile(exe) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            for info in members:
                parts = Path(info.filename).parts[1:]  # drop the zip's top-level folder
                if not parts or ".." in parts:
                    continue
                destination = work.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                count += 1
                if count % 200 == 0:
                    print(f"  已释放 {count}/{len(members)} 个文件…")
        (work / ".complete").write_text("ok\n", encoding="ascii")
        try:
            os.replace(work, target)
        except OSError:
            if not (target / ".complete").is_file():
                raise
            shutil.rmtree(work, ignore_errors=True)  # another run finished first
        for sibling in target.parent.glob("app-*"):
            if sibling != target and sibling.is_dir():
                shutil.rmtree(sibling, ignore_errors=True)
        print("完成，正在启动…")
        return target
    except Exception as error:
        shutil.rmtree(work, ignore_errors=True)
        raise SystemExit(say(f"释放文件失败：{error}\n\n可以改用压缩包版本，解压后运行其中的 WittyCode.exe。") or 1)


def selftest(report: Path) -> int:
    """``--selftest <file>``: check the appended payload without installing; build_release.py runs this (a GUI program
    cannot write to the caller's console, so the outcome goes to a file and the exit code)."""
    try:
        exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
        start, size, version = own_payload(exe)
        with zipfile.ZipFile(exe) as archive:
            names = archive.namelist()
            launcher = any(name.endswith("/WittyCode.exe") for name in names)
            bad = archive.testzip()
        ok = launcher and bad is None
        report.write_text(f"{'OK' if ok else 'FAIL'} version={version} files={len(names)} launcher={launcher} corrupt={bad}\n", encoding="utf-8")
        return 0 if ok else 1
    except Exception as error:
        report.write_text(f"FAIL {error}\n", encoding="utf-8")
        return 1


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        return selftest(Path(sys.argv[2]))
    exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    start, size, version = own_payload(exe)
    try:
        target = extract(exe, version)
    except SystemExit:
        raise
    except Exception as error:
        return say(f"释放文件失败：{error}\n\n可以改用压缩包版本，解压后运行其中的 WittyCode.exe。") or 1
    launcher = target / "WittyCode.exe"
    if not launcher.is_file():
        return say(f"找不到 {launcher}：释放的文件不完整，请删除 {target} 后重试。") or 1
    try:
        subprocess.Popen([str(launcher), *sys.argv[1:]], cwd=str(target))
    except OSError as error:
        return say(f"启动失败：{error}") or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
