"""Centralized, theme-aware palette + window background for MomentShift.

Previously the app scattered hardcoded greys (``rgba(128,128,128,1)``) across
several widgets. Those overrides fought qfluentwidgets' own theming and looked
wrong in dark mode. This module is the single source of truth: every custom
widget asks for a color here and re-applies it whenever ``qconfig.themeChanged``
fires (see ``gui.main_window._retheme_all``).
"""

from __future__ import annotations

from qfluentwidgets import isDarkTheme, qconfig, Theme
from PyQt6.QtGui import QColor

# Window chrome + content background per theme (neutral Material-like greys).
# Applied via ``FluentWindow.setCustomBackgroundColor`` so the whole window —
# including the content area behind transparent scroll views — stays coherent.
LIGHT_BG = QColor(244, 244, 244)
DARK_BG = QColor(32, 32, 32)

# Secondary / hint / muted text. Kept readable on both themes: dark greys on
# light, lighter greys on dark (a plain near-white would look too loud for a hint).
def sub_text() -> str:
    return "rgba(96, 96, 96, 1)" if not isDarkTheme() else "rgba(165, 165, 165, 1)"


def hint_text() -> str:
    return "rgba(128, 128, 128, 1)" if not isDarkTheme() else "rgba(170, 170, 170, 1)"


def muted_text() -> str:
    return "rgba(140, 140, 140, 1)" if not isDarkTheme() else "rgba(170, 170, 170, 1)"


def map_theme(value: str) -> Theme:
    """Map our config value (auto/light/dark) to a qfluentwidgets ``Theme``."""
    return {
        "auto": Theme.AUTO,
        "light": Theme.LIGHT,
        "dark": Theme.DARK,
    }.get(value, Theme.AUTO)
