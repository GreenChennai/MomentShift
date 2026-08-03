"""Headless smoke test for MainWindow + theme wiring (no populated queue / InfoBar).

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe tests/window_smoke_test.py

InfoBar is patched to a no-op because its paint hard-kills the offscreen Qt
sandbox. We assert the effective theme flips correctly dark -> light and that
the new ``_retheme_all`` wiring survives a theme change without crashing.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import InfoBar, Theme, isDarkTheme, setTheme

# Neutralise toasts (their paint kills the offscreen sandbox).
InfoBar.success = staticmethod(lambda *a, **k: None)
InfoBar.warning = staticmethod(lambda *a, **k: None)
InfoBar.error = staticmethod(lambda *a, **k: None)

from momentshift.core.config import cfg
from momentshift.core.queue import ConversionManager
from momentshift.gui.main_window import MainWindow


def main():
    app = QApplication([])
    setTheme(Theme.DARK)
    cfg.theme.value = "dark"

    mgr = ConversionManager(ffmpeg_path=None)
    w = MainWindow(mgr)
    w.show = lambda *a, **k: None
    w._retheme_all()
    w.convertInterface.retheme()
    w.settingInterface.retheme()

    dark_ok = isDarkTheme()
    print(f"isDarkTheme()={dark_ok}", flush=True)
    assert dark_ok, "dark theme should be active"

    setTheme(Theme.LIGHT)
    w._retheme_all()
    print(f"after light, isDarkTheme()={isDarkTheme()}", flush=True)
    assert not isDarkTheme(), "light theme should be active"

    print("MAIN_SMOKE_OK", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
