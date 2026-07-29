"""Entry point: ``python -m momentshift`` or the installed ``momentshift`` CLI."""

import sys
import threading
import traceback

# NOTE: use ABSOLUTE imports here. PyInstaller freezes this file as the
# top-level script ``__main__`` (no parent package context), so relative
# imports (``from .core ...``) raise "attempted relative import with no known
# parent package". Absolute imports work both frozen and via ``python -m momentshift``.
from momentshift.core.logger import init_logging, get_logger
from momentshift.core.qt_compat import QApplication, Qt
from qfluentwidgets import setTheme, Theme
from momentshift.core.config import cfg
from momentshift.i18n.translator import translator, LocaleKey
from momentshift.core.queue import ConversionManager
from momentshift.gui.main_window import MainWindow
from momentshift.metadata import APP_NAME, VERSION


class Application(QApplication):
    """QApplication that logs (instead of crashing on) exceptions raised inside
    Qt event handlers / slots. This turns a silent "闪退" into a logged,
    diagnosable event written to ``logs/``.
    """

    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            get_logger("app").critical(
                "Exception in Qt event handler:\n" + traceback.format_exc()
            )
            return False


def _excepthook(exc_type, exc, tb):
    get_logger("app").critical(
        "Unhandled exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb))
    )


def _threading_excepthook(args):
    get_logger("app").critical(
        f"Unhandled exception in thread ({args.thread.name}):\n"
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


def main():
    init_logging()
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook

    log = get_logger("app")
    log.info("Starting %s %s", APP_NAME, VERSION)

    # Crisp rendering on high-DPI displays (PyQt6 handles HiDPI natively).
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # AA_UseHighDpiPixmaps was removed in Qt6 (high-DPI pixmaps are automatic),
    # so guard it to stay compatible across PyQt6 versions.
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = Application(sys.argv)
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
