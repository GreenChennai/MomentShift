"""Headless validation of the rewritten dark/light theme plumbing.

Verifies:
- theme.color helpers flip with isDarkTheme()
- setCustomBackgroundColor colours are QColors (sanity)
- map_theme maps config values to qfluentwidgets Theme
- ConvertInterface.retheme() runs through advancedPanel + formatGrid without error
  in both themes (staging-only, no painted queue rows -> safe for offscreen sandbox)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import InfoBar, Theme, isDarkTheme, setTheme

# Neutralise toasts (their paint kills the offscreen sandbox).
InfoBar.success = staticmethod(lambda *a, **k: None)
InfoBar.warning = staticmethod(lambda *a, **k: None)
InfoBar.error = staticmethod(lambda *a, **k: None)

from momentshift.core.config import cfg
from momentshift.core.queue import ConversionManager
from momentshift.gui.base import InterfaceBase
from momentshift.gui.convert_interface import ConvertInterface
from momentshift.gui.theme import (
    DARK_BG,
    LIGHT_BG,
    hint_text,
    map_theme,
    muted_text,
    sub_text,
)


def check(cond, msg):
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        raise SystemExit(1)


app = QApplication([])
mgr = ConversionManager(ffmpeg_path=None)
conv = ConvertInterface(mgr, None)
conv.show = lambda *a, **k: None  # guard against accidental paint

# --- colour helpers flip with the theme -------------------------------
setTheme(Theme.DARK)
check(isDarkTheme(), "isDarkTheme() is True after setTheme(DARK)")
check(sub_text() == "rgba(165, 165, 165, 1)", "sub_text dark variant")
check(hint_text() == "rgba(170, 170, 170, 1)", "hint_text dark variant")
check(muted_text() == "rgba(170, 170, 170, 1)", "muted_text dark variant")

setTheme(Theme.LIGHT)
check(not isDarkTheme(), "isDarkTheme() is False after setTheme(LIGHT)")
check(sub_text() == "rgba(96, 96, 96, 1)", "sub_text light variant")
check(hint_text() == "rgba(128, 128, 128, 1)", "hint_text light variant")
check(muted_text() == "rgba(140, 140, 140, 1)", "muted_text light variant")

# --- InterfaceBase gets a solid theme background ----------------------
setTheme(Theme.DARK)
base_dark = InterfaceBase("TestDark", "Title", "Subtitle", None)
base_dark.show = lambda *a, **k: None
dark_ss = (base_dark.view.styleSheet() or "").lower()
check("#202020" in dark_ss, "InterfaceBase view background is dark #202020")

setTheme(Theme.LIGHT)
base_light = InterfaceBase("TestLight", "Title", "Subtitle", None)
base_light.show = lambda *a, **k: None
light_ss = (base_light.view.styleSheet() or "").lower()
check("#f4f4f4" in light_ss, "InterfaceBase view background is light #f4f4f4")

# --- window bg + mapping sanity ---------------------------------------
check(hasattr(LIGHT_BG, "rgb") and hasattr(DARK_BG, "rgb"), "LIGHT_BG/DARK_BG are QColors")
check(map_theme("auto") == Theme.AUTO, "map_theme auto")
check(map_theme("dark") == Theme.DARK, "map_theme dark")
check(map_theme("bogus") == Theme.AUTO, "map_theme fallback")

# --- retheme() runs through real panels in both themes ----------------
import tempfile

d = tempfile.mkdtemp()
cfg.outputMode.value = "same"
f1 = os.path.join(d, "a.png")
f2 = os.path.join(d, "b.png")
open(f1, "wb").write(b"x")
open(f2, "wb").write(b"y")
conv._add_to_staging([f1, f2])  # builds advanced image panel + format cards

setTheme(Theme.DARK)
conv.retheme()  # exercises advancedPanel.retheme + formatGrid.retheme
check("image" in conv.advancedPanel._cat_panels, "advanced image panel present in dark")

setTheme(Theme.LIGHT)
conv.retheme()
check("image" in conv.advancedPanel._cat_panels, "advanced image panel present in light")

print("ALL THEME CHECKS PASSED")
sys.exit(0)
