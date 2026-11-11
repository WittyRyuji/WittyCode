r"""Start WittyCode without a console window; the same as ``pythonw.exe -m desktop``.

Make a shortcut whose target is the project's own interpreter, so the packages in .venv are found whatever
folder the shortcut starts in:

    "<project>\.venv\Scripts\pythonw.exe" "<project>\WittyCode.pyw"

Opening this file on its own runs whichever interpreter Windows associates with .pyw, usually one that has not
got the packages in .venv. A program without a console has nowhere to print an error, so a failure to start is
shown in a message box. Once running, the application logs to WittyCode.log next to its settings file.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def show_failure(text: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text[-1800:], "WittyCode 无法启动", 0x10)


try:
    from desktop.ui.app import main

    code = main(sys.argv)
except SystemExit as stop:
    if isinstance(stop.code, str):  # the Python version guard refuses to start this way
        show_failure(stop.code)
    raise
except BaseException:
    show_failure(traceback.format_exc())
    raise
sys.exit(code)
