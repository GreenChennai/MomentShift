"""Centralized, theme-aware palette + window background for MomentShift.

Previously the app scattered hardcoded greys (``rgba(128,128,128,1)``) across
several widgets. Those overrides fought qfluentwidgets' own theming and looked
wrong in dark mode. This module is the single source of truth: every custom
widget asks for a color here and re-applies it whenever ``qconfig.themeChanged``
fires (see ``gui.main_window._retheme_all``).
"""

from __future__ import annotations

from qfluentwidgets import isDarkTheme, qconfig, Theme, CardWidget
from PyQt6.QtGui import QColor

# Window chrome + content background per theme (neutral Material-like greys).
# Applied via ``FluentWindow.setCustomBackgroundColor`` so the whole window —
# including the content area behind transparent scroll views — stays coherent.
LIGHT_BG = QColor(244, 244, 244)
DARK_BG = QColor(32, 32, 32)

# Uniform "component" background used by cards. This is the colour that any
# transparent text/icon *inside* a card resolves to, so the patch behind a
# label always matches the card surface (no more ~#F4F4F4 vs #FBFBFB mismatch).
COMPONENT_LIGHT = QColor(251, 251, 251)   # #FBFBFB
COMPONENT_DARK = QColor(43, 43, 43)       # #2B2B2B
HOVER_LIGHT = QColor(242, 242, 242)
HOVER_DARK = QColor(54, 54, 54)
PRESS_LIGHT = QColor(236, 236, 236)
PRESS_DARK = QColor(38, 38, 38)


def component_bg() -> QColor:
    """Solid background colour a card (and the text/icons inside it) should use."""
    return COMPONENT_DARK if isDarkTheme() else COMPONENT_LIGHT


class ThemedCard(CardWidget):
    """A ``CardWidget`` that paints a *solid* theme-aware component colour.

    qfluentwidgets' default ``CardWidget`` paints a translucent white overlay
    over whatever is behind it, so a transparent label inside a card resolved to
    the content-view colour (e.g. ``#F4F4F4``) instead of the card surface
    (``#FBFBFB``). Painting an opaque component colour here keeps the whole card,
    and every label/icon on it, visually uniform.
    """

    def _normalBackgroundColor(self):
        return component_bg()

    def _hoverBackgroundColor(self):
        return HOVER_DARK if isDarkTheme() else HOVER_LIGHT

    def _pressedBackgroundColor(self):
        return PRESS_DARK if isDarkTheme() else PRESS_LIGHT


def content_bg() -> QColor:
    """Return the solid background color that should fill a content interface."""
    return DARK_BG if isDarkTheme() else LIGHT_BG

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
