"""The outline of the frameless window on Windows: the system's own drop shadow, and rounded corners where it has them.

To Windows a frameless Qt window is a plain pop-up, and a pop-up has no shadow: on a white page over a white window there
was no telling where the app ended. Giving it the sizing-frame style (``WS_THICKFRAME``) and extending the frame one pixel
into the client area (``DwmExtendFrameIntoClientArea``) makes the compositor treat it as a framed window: it draws the
shadow and, on Windows 11, rounds the corners. The page still fills the whole window (checked on Windows 11, build 26200:
the client area stays the window's rectangle, so nothing of the layout moves), and moving, resizing and snapping stay the
ones of ``QWindow.startSystemMove`` / ``startSystemResize``.

The thin outline itself is drawn by the page (``.frame-line`` in ``layout.css``), in the colours of its theme and gone
while the window is maximized; Windows 11's own 1 px border is switched off so that there is one line, not two.
``frame_kind`` tells the page which of the two corner shapes to draw (``?frame=`` in its address).

No Qt here, and every call is a no-op where there is no Windows window (the offscreen platform of the tests).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Protocol

log = logging.getLogger("desktop.winframe")

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0004, 0x0010, 0x0020
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE
WINDOWS_11_BUILD = 22000
"""From this build on Windows rounds the corners of a framed window and lets the app choose its border colour."""
FRAME_MARGIN = 1


class Win32(Protocol):
    """The few calls this module makes, so that the order of them can be checked without a window."""

    def get_style(self, hwnd: int) -> int: ...
    def set_style(self, hwnd: int, style: int) -> None: ...
    def frame_changed(self, hwnd: int) -> None: ...
    def extend_frame(self, hwnd: int, margin: int) -> int: ...
    def set_dwm_value(self, hwnd: int, attribute: int, value: int) -> int: ...


def windows_build() -> int:
    """The Windows build number (0 elsewhere)."""
    return int(getattr(sys.getwindowsversion(), "build", 0)) if sys.platform == "win32" else 0


def frame_kind(platform_name: str, build: int | None = None) -> str:
    """How the window's corners look, for the page's outline: ``round`` (Windows 11 rounds them), ``square`` (Windows 10)
    or ``""`` (no Windows window: the page draws no outline)."""
    if platform_name != "windows":
        return ""
    return "round" if (windows_build() if build is None else build) >= WINDOWS_11_BUILD else "square"


def with_sizing_frame(style: int) -> int:
    """``style`` plus the sizing frame, everything else as it was."""
    return style | WS_THICKFRAME


class _Api:
    """The real calls (ctypes, 64-bit pointers)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        self._user32, self._dwm = user32, dwm

        class Margins(ctypes.Structure):
            _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

        self._margins = Margins

    def get_style(self, hwnd: int) -> int:
        return int(self._user32.GetWindowLongPtrW(self._wintypes.HWND(hwnd), GWL_STYLE))

    def set_style(self, hwnd: int, style: int) -> None:
        self._user32.SetWindowLongPtrW(self._wintypes.HWND(hwnd), GWL_STYLE, style)

    def frame_changed(self, hwnd: int) -> None:
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        self._user32.SetWindowPos(self._wintypes.HWND(hwnd), None, 0, 0, 0, 0, flags)

    def extend_frame(self, hwnd: int, margin: int) -> int:
        margins = self._margins(margin, margin, margin, margin)
        return int(self._dwm.DwmExtendFrameIntoClientArea(self._wintypes.HWND(hwnd), ctypes.byref(margins)))

    def set_dwm_value(self, hwnd: int, attribute: int, value: int) -> int:
        number = ctypes.c_uint(value)
        return int(self._dwm.DwmSetWindowAttribute(self._wintypes.HWND(hwnd), attribute, ctypes.byref(number), ctypes.sizeof(number)))


def apply(hwnd: int, *, build: int | None = None, api: Win32 | None = None) -> bool:
    """Give the window the system's drop shadow. ``False`` when Windows would not (the window then simply has none)."""
    try:
        api = api if api is not None else _Api()
        api.set_style(hwnd, with_sizing_frame(api.get_style(hwnd)))
        api.frame_changed(hwnd)
        result = api.extend_frame(hwnd, FRAME_MARGIN)
        if result != 0:
            log.warning("the window has no drop shadow: DwmExtendFrameIntoClientArea answered %#x", result & 0xFFFFFFFF)
            return False
        if (windows_build() if build is None else build) >= WINDOWS_11_BUILD:
            # one outline, the page's own: Windows 11 would draw a second, 1 px one in its own colour (not fatal if refused)
            api.set_dwm_value(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
        return True
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError) as error:  # not Windows, or no such window
        log.warning("the window has no drop shadow: %s", error)
        return False


__all__ = ["apply", "frame_kind", "with_sizing_frame", "windows_build"]
