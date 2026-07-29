"""Entry point: ``python -m momentshift`` or the installed ``momentshift`` CLI."""

import sys

# NOTE: use ABSOLUTE imports here. PyInstaller freezes this file as the
# top-level script ``__main__`` (no parent package context), so relative
# imports (``from .core ...``) raise "attempted relative import with no known
# parent package". Absolute imports work both frozen and via ``python -m momentshift``.
from momentshift.core.qt_compat import QApplication, Qt
from qfluentwidgets import setTheme, Theme
from momentshift.core.config import cfg
from momentshift.i18n.translator import translator, LocaleKey
from momentshift.core.queue import ConversionManager
from momentshift.gui.main_window import MainWindow
from momentshift.metadata import APP_NAME, VERSION


def main():
    # Crisp rendering on high-DPI displays (PyQt6 handles HiDPI natively).
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)

    # Apply saved preferences before the window appears.
    theme_map = {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}
    setTheme(theme_map.get(cfg.theme.value, Theme.AUTO))
    translator.set_locale(LocaleKey(cfg.language.value))

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
