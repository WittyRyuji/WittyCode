r"""Builds the Windows release: a folder (and a zip of it) that runs WittyCode without installing anything.

    .\.venv\Scripts\python.exe scripts\build_release.py [--no-smoke] [--no-zip] [--keep-work]

What goes into ``dist\WittyCode-<version>-win-x64\``:

    WittyCode.exe, launcher\   the launcher (scripts/launcher.py, frozen with PyInstaller)
    python\                    the embeddable Python from python.org (checked against the MD5 python.org publishes)
    site-packages\             the packages ``requirements.txt`` names and what they depend on, copied from this .venv
    app\                       the desktop package (with the built page) and WittyCode.pyw
    LICENSE, NOTICE, THIRD_PARTY_NOTICES.txt, licenses\, README.txt

The release runs the same interpreter version and the same package versions as the tests do; it is the project's own files,
not a re-implementation of them. Needs: the page built (``cd frontend; npm run build:app``), Python 3.11 (this interpreter),
and PyInstaller in ``.venv-build`` (made on the first run). Python and React only; nothing else is compiled.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.11.9"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_MD5 = "6d9aa08531d48fcc261ba667e2df17c4"
"""From https://www.python.org/downloads/release/python-3119/ (the embeddable package, 64-bit)."""
RUNTIME_ROOTS = ("PySide6", "qasync")
PRUNED = ("PySide6/examples/", "PySide6/include/", "PySide6/glue/", "PySide6/scripts/", "PySide6/typesystems/")
"""Parts of the PySide6 wheels that only developers of PySide6 programs use."""
QT_LICENSED_AS_LGPL = ("pyside6", "pyside6-addons", "pyside6-essentials", "pyside6-pdf", "pyside6-webengine", "shiboken6")
SKIPPED_LICENSE_FILES = ("LicenseRef-Qt-Commercial.txt",)
"""The wheels carry the commercial Qt license text; the release uses Qt under the LGPL, whose text is shipped instead."""


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def runtime_distributions() -> list[metadata.Distribution]:
    """The distributions the application needs to run: ``RUNTIME_ROOTS`` and what they require (markers evaluated here)."""
    from packaging.requirements import Requirement

    found: dict[str, metadata.Distribution] = {}
    stack = list(RUNTIME_ROOTS)
    while stack:
        name = stack.pop()
        if normalized(name) in found:
            continue
        dist = metadata.distribution(name)
        found[normalized(name)] = dist
        for line in dist.requires or []:
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            stack.append(requirement.name)
    return [found[key] for key in sorted(found)]


def fetch_embedded_python(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / Path(EMBED_URL).name
    if not target.is_file():
        print(f"downloading {EMBED_URL}")
        with urllib.request.urlopen(EMBED_URL, timeout=120) as response:
            target.write_bytes(response.read())
    digest = hashlib.md5(target.read_bytes()).hexdigest()  # noqa: S324 (the checksum python.org publishes is an MD5)
    if digest != EMBED_MD5:
        target.unlink()
        raise SystemExit(f"{target.name}: MD5 {digest} is not the {EMBED_MD5} python.org publishes; removed it")
    return target


def copy_packages(dists: list[metadata.Distribution], target: Path) -> None:
    count = 0
    for dist in dists:
        for entry in dist.files or []:
            parts = Path(str(entry)).parts
            posix = "/".join(parts)
            if ".." in parts or "__pycache__" in parts or posix.endswith((".pyc", ".pyi")) or posix.startswith(PRUNED):
                continue
            source = Path(str(dist.locate_file(entry)))
            if not source.is_file():
                continue
            destination = target / Path(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            count += 1
    print(f"copied {count} files of {len(dists)} packages")


def copy_app(target: Path) -> None:
    if not (ROOT / "desktop" / "ui_dist" / "index.html").is_file():
        raise SystemExit("the page is not built: cd frontend; npm run build:app")
    shutil.copytree(ROOT / "desktop", target / "desktop", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "WittyCode.pyw", target / "WittyCode.pyw")


def write_path_file(python_folder: Path) -> None:
    """The embeddable Python reads ``python311._pth`` instead of looking for PYTHONPATH, the registry or site-packages."""
    (python_folder / "python311._pth").write_text("python311.zip\n.\n..\\site-packages\n..\\app\n", encoding="utf-8")


def precompile(python: Path, folders: list[Path]) -> None:
    """Byte code that is never compared with its source: the release is read-only in spirit, and a zip drops file times."""
    done = subprocess.run([str(python), "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", *map(str, folders)], capture_output=True, text=True)
    if done.returncode != 0:  # (a data file with a .py name that is not Python: it then compiles at the first import that needs it, or never)
        print("compileall reported:\n" + "\n".join(done.stdout.splitlines()[-5:]))


def build_launcher(target: Path, work: Path) -> None:
    build_python = ROOT / ".venv-build" / "Scripts" / "python.exe"
    if not build_python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv-build")], check=True)
    pinned = ROOT / "requirements-build.txt"
    wanted = next(line.split("==", 1)[1].strip() for line in pinned.read_text(encoding="utf-8").splitlines() if line.startswith("pyinstaller=="))
    found = subprocess.run([str(build_python), "-m", "PyInstaller", "--version"], capture_output=True, text=True)
    if found.returncode != 0 or found.stdout.strip() != wanted:
        subprocess.run([str(build_python), "-m", "pip", "install", "--quiet", "-r", str(pinned)], check=True)
    subprocess.run(
        [
            str(build_python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole", "--name", "WittyCode",
            "--icon", str(ROOT / "desktop" / "assets" / "icon.ico"), "--contents-directory", "launcher",
            "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work), str(ROOT / "scripts" / "launcher.py"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    built = work / "dist" / "WittyCode"
    for item in built.iterdir():
        if item.is_dir():
            shutil.copytree(item, target / item.name)
        else:
            shutil.copy2(item, target / item.name)


def write_notices(dists: list[metadata.Distribution], target: Path, python_folder: Path) -> None:
    licenses = target / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", target / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", target / "NOTICE")
    shutil.copy2(ROOT / "scripts" / "third_party" / "LGPL-3.0.txt", licenses / "LGPL-3.0.txt")
    shutil.copy2(ROOT / "scripts" / "third_party" / "GPL-3.0.txt", licenses / "GPL-3.0.txt")
    lines = [
        "WittyCode " + project_version() + " — third-party software in this release",
        "",
        "WittyCode itself is under the Apache License 2.0 (LICENSE, NOTICE). The folders next to it hold software of other authors,",
        "each under its own license; the license texts are in the folder licenses\\.",
        "",
        "Python " + PYTHON_VERSION + " (python\\) — PSF License (licenses\\python\\LICENSE.txt). https://www.python.org/",
    ]
    packaged = licenses / "python"
    packaged.mkdir(exist_ok=True)
    if (python_folder / "LICENSE.txt").is_file():
        shutil.copy2(python_folder / "LICENSE.txt", packaged / "LICENSE.txt")
    lines += ["", "Python packages (site-packages\\):", ""]
    for dist in dists:
        meta = dist.metadata
        name, version = meta["Name"], dist.version
        expression = meta.get("License-Expression") or meta.get("License") or "see the package"
        home = next((value.split(",", 1)[1].strip() for value in meta.get_all("Project-URL") or [] if value.lower().startswith(("homepage", "source", "repository"))), meta.get("Home-page") or "")
        lines.append(f"- {name} {version} — {expression}" + (f" — {home}" if home else ""))
        if normalized(name) in QT_LICENSED_AS_LGPL:
            continue
        kept = [entry for entry in dist.files or [] if "licenses" in Path(str(entry)).parts and Path(str(entry)).name not in SKIPPED_LICENSE_FILES]
        for entry in kept:
            folder = licenses / normalized(name)
            folder.mkdir(exist_ok=True)
            shutil.copy2(Path(str(dist.locate_file(entry))), folder / Path(str(entry)).name)
    lines += [
        "",
        "Qt and PySide6 (site-packages\\PySide6\\, shiboken6\\): used under the GNU Lesser General Public License, version 3 (licenses\\LGPL-3.0.txt,",
        "which refers to the GPL, licenses\\GPL-3.0.txt). They are dynamically linked and stay separate files: you may replace them with",
        "your own build of the same versions (PySide6 6.12.0). Their source code:",
        "  Qt for Python: https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://download.qt.io/official_releases/QtForPython/",
        "  Qt: https://download.qt.io/official_releases/qt/",
        "Qt WebEngine includes Chromium and other third-party components under their own licenses: https://doc.qt.io/qt-6/qtwebengine-licensing.html",
        "",
        "The launcher (WittyCode.exe, launcher\\) was made with PyInstaller (GPL-2.0-or-later with the exception that lets it be used for any",
        "program) and contains Python's runtime and the standard library: https://pyinstaller.org/",
        "",
    ]
    (target / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines), encoding="utf-8")


README = """WittyCode {version}

运行：双击 WittyCode.exe。不用安装，也不用另外装 Python；整个文件夹必须放在一起。
Run: double-click WittyCode.exe. Nothing to install; keep the whole folder together.

* 数据放在 %APPDATA%\\WittyCode\\（设置、日志 WittyCode.log、ChatGPT 登录凭据、会话、记忆）。删掉这个文件夹就等于重置。
* 本包没有代码签名，Windows SmartScreen 第一次可能提示“未知发布者”。
* 不要放进需要管理员权限才能写的位置（例如 C:\\Program Files）：程序要在自己的文件夹旁写缓存。
* 可选：装了 Devin 的话，它的模型会出现在引擎列表里；只用 ChatGPT 套餐或 API Provider 的话不需要任何别的程序。
* 命令行参数原样传给程序：WittyCode.exe --project D:\\code\\myproject
* 许可证：Apache-2.0（LICENSE、NOTICE）；第三方软件见 THIRD_PARTY_NOTICES.txt 和 licenses\\。
* 源码和问题反馈：见仓库主页。
"""


def zip_folder(folder: Path, target: Path) -> str:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            archive.write(path, Path(folder.name) / path.relative_to(folder))
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    (target.parent / (target.name + ".sha256")).write_text(f"{digest.hexdigest()} *{target.name}\n", encoding="ascii")
    return digest.hexdigest()


def smoke(folder: Path) -> None:
    python = folder / "python" / "python.exe"
    print("smoke: the release's own Python starts the application offscreen on a recorded conversation")
    done = subprocess.run([str(python), str(ROOT / "scripts" / "smoke_release.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    shown = [line for line in done.stdout.splitlines() if line.strip()]
    print("\n".join("    " + line for line in shown[-6:]))
    if done.returncode != 0 or not shown or shown[-1] != "OK":
        print(done.stderr[-1500:])
        raise SystemExit("the smoke test failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-smoke", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    options = parser.parse_args()
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("run this with the project's Python 3.11 (.venv)")
    version = project_version()
    name = f"WittyCode-{version}-win-x64"
    out = ROOT / "dist" / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="wittycode-release-"))
    try:
        dists = runtime_distributions()
        print("packages: " + ", ".join(f"{d.metadata['Name']} {d.version}" for d in dists))
        embedded = fetch_embedded_python(ROOT / "build" / "cache")
        with zipfile.ZipFile(embedded) as archive:
            archive.extractall(out / "python")
        write_path_file(out / "python")
        copy_packages(dists, out / "site-packages")
        copy_app(out / "app")
        precompile(out / "python" / "python.exe", [out / "site-packages", out / "app"])
        build_launcher(out, work)
        write_notices(dists, out, out / "python")
        (out / "README.txt").write_text(README.format(version=version), encoding="utf-8")
        size = sum(path.stat().st_size for path in out.rglob("*") if path.is_file())
        print(f"folder: {out} ({size / 1e6:.0f} MB)")
        if not options.no_smoke:
            smoke(out)
        if not options.no_zip:
            archive = ROOT / "dist" / f"{name}.zip"
            if archive.exists():
                archive.unlink()
            digest = zip_folder(out, archive)
            print(f"zip: {archive} ({archive.stat().st_size / 1e6:.0f} MB)\nsha256: {digest}")
    finally:
        if not options.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
