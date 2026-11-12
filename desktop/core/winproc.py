"""Windows process flags: what every child process WittyCode starts gets, so that no console window opens under
``pythonw.exe`` (the shell tool, the memory server, an engine's process)."""

from __future__ import annotations

CREATE_NO_WINDOW = 0x08000000
