r"""WittyCode.exe of the release: starts the application with the Python that lies beside it.

The release is a folder, not an installation::

    WittyCode.exe          this program, built with PyInstaller by scripts/build_release.py
    python\                Python 3.11 (the embeddable package from python.org)
    site-packages\         PySide6, droid-sdk, qasync and what they need
    app\                   the desktop package and WittyCode.pyw

Nothing here is the application: it only finds ``python\pythonw.exe`` and ``app\WittyCode.pyw`` and starts them without a
console window, passing the command line on. The application itself needs the real interpreter (it starts child processes
with it: the memory server, the search worker), which is why it is not frozen into this executable.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

TITLE = "WittyCode 无法启动"


def base_folder() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def say(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)


def main() -> int:
    base = base_folder()
    python = base / "python" / "pythonw.exe"
    script = base / "app" / "WittyCode.pyw"
    missing = [str(path) for path in (python, script) if not path.is_file()]
    if missing:
        say("找不到运行需要的文件：\n" + "\n".join(missing) + "\n\n请把整个压缩包解压到同一个文件夹后再运行，不要只拷出 WittyCode.exe。")
        return 1
    # What PyInstaller (and the user's own Python) put in the environment is not for the application's interpreter.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("_PYI", "_MEIPASS")) and key not in ("PYTHONHOME", "PYTHONPATH")}
    try:
        subprocess.Popen(
            [str(python), str(script), *sys.argv[1:]],
            cwd=str(base),
            env=env,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as error:
        say(f"启动失败：{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
