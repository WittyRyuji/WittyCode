"""The drop shadow of the frameless window (``ui/winframe.py``): what is asked of Windows, in which order, and when."""

from __future__ import annotations

from desktop.ui import winframe
from desktop.ui.winframe import (
    DWMWA_BORDER_COLOR,
    DWMWA_COLOR_NONE,
    FRAME_MARGIN,
    WINDOWS_11_BUILD,
    WS_THICKFRAME,
    apply,
    frame_kind,
    with_sizing_frame,
)

POPUP_STYLE = 0x960B0000  # what a frameless Qt window has on Windows 11 (measured): a pop-up with the system menu and boxes


class FakeWin32:
    """Records the calls instead of making them."""

    def __init__(self, style: int = POPUP_STYLE, extend_result: int = 0, border_result: int = 0) -> None:
        self.style = style
        self.extend_result = extend_result
        self.border_result = border_result
        self.calls: list[tuple] = []

    def get_style(self, hwnd: int) -> int:
        self.calls.append(("get_style", hwnd))
        return self.style

    def set_style(self, hwnd: int, style: int) -> None:
        self.calls.append(("set_style", hwnd, style))
        self.style = style

    def frame_changed(self, hwnd: int) -> None:
        self.calls.append(("frame_changed", hwnd))

    def extend_frame(self, hwnd: int, margin: int) -> int:
        self.calls.append(("extend_frame", hwnd, margin))
        return self.extend_result

    def set_dwm_value(self, hwnd: int, attribute: int, value: int) -> int:
        self.calls.append(("set_dwm_value", hwnd, attribute, value))
        return self.border_result


def test_the_page_is_told_which_corners_to_draw_only_for_a_real_windows_window():
    assert frame_kind("offscreen") == "" and frame_kind("xcb") == "" and frame_kind("cocoa") == ""
    assert frame_kind("windows", build=26200) == "round", "Windows 11 rounds the corners of a framed window"
    assert frame_kind("windows", build=WINDOWS_11_BUILD) == "round"
    assert frame_kind("windows", build=WINDOWS_11_BUILD - 1) == "square", "Windows 10: square corners"
    assert frame_kind("windows", build=19045) == "square"


def test_the_sizing_frame_is_added_and_nothing_else_changes():
    style = with_sizing_frame(POPUP_STYLE)
    assert style == POPUP_STYLE | WS_THICKFRAME and style & POPUP_STYLE == POPUP_STYLE
    assert with_sizing_frame(style) == style, "once is enough"


def test_the_shadow_is_asked_for_in_order_and_windows_11s_own_border_is_turned_off():
    api = FakeWin32()
    assert apply(1234, build=26200, api=api) is True
    assert api.calls == [
        ("get_style", 1234),
        ("set_style", 1234, POPUP_STYLE | WS_THICKFRAME),
        ("frame_changed", 1234),
        ("extend_frame", 1234, FRAME_MARGIN),
        ("set_dwm_value", 1234, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE),
    ]
    assert FRAME_MARGIN == 1 and DWMWA_COLOR_NONE == 0xFFFFFFFE


def test_windows_10_gets_the_shadow_without_the_border_attribute_it_does_not_have():
    api = FakeWin32()
    assert apply(7, build=19045, api=api) is True
    assert [call[0] for call in api.calls] == ["get_style", "set_style", "frame_changed", "extend_frame"]


def test_a_border_attribute_windows_refuses_is_not_fatal_but_a_refused_frame_extension_is_reported():
    assert apply(7, build=26200, api=FakeWin32(border_result=-2147024809)) is True, "the shadow is there, only the border stays"
    refused = FakeWin32(extend_result=-2147024809)
    assert apply(7, build=26200, api=refused) is False
    assert ("set_dwm_value", 7, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE) not in refused.calls, "no point in going on"


def test_a_window_that_is_not_there_gives_no_shadow_and_no_crash():
    class Gone(FakeWin32):
        def get_style(self, hwnd: int) -> int:
            raise OSError("no such window")

    assert apply(0, build=26200, api=Gone()) is False


def test_the_real_calls_on_a_handle_that_is_no_window_do_not_crash():
    """ctypes with real argument types on a made-up handle (Windows refuses; nothing is raised)."""
    import sys

    if sys.platform != "win32":
        return
    assert apply(0x7FFFFFF0) in (True, False)
    assert winframe.windows_build() >= 0
