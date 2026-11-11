"""Light or dark, taken from the operating system (for the colour the window shows before its page has painted)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette


def is_dark(app: QGuiApplication | None = None) -> bool:
    app = app or QGuiApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128
