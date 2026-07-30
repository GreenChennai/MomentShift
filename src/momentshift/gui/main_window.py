"""Application main window (FluentWindow) wiring navigation + theme + i18n.

v0.2.5 startup optimization
----------------------------
Every sub-interface (including the home Convert view) is built *lazily* — after
the window and its splash have already painted. The heavy ``qfluentwidgets``
imports for each screen are also deferred until that moment, so the pre-show
import chain stays light and the window appears instantly ("秒速加载").

Theme switching restarts the process (deferred, so the current event settles
first). Because startup is now instant, the restart is seamless and guarantees
the whole UI re-themes correctly — this is what fixes the settings-page
background not following light<->dark (#5) and gives an instant switch (#2).
Language switching stays in-process (it is already instant).
"""

import importlib
from PyQt6.QtCore import QTimer

from ..core.qt_compat import QApplication, QIcon, QSize
from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    FluentIcon as FIF,
    SplashScreen,
    SystemThemeListener,
    Theme,
)
from ..core.config import cfg
from ..i18n.translator import tr, translator, LocaleKey
from qfluentwidgets import ConfigItem
from qfluentwidgets import qconfig
from .theme import LIGHT_BG, DARK_BG


class MainWindow(FluentWindow):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

        # Apply the saved language BEFORE the window paints so the first frame
        # already shows the right strings (theme is applied in __main__).
        translator.set_locale(LocaleKey(cfg.language.value))
        self.initWindow()

        self.themeListener = SystemThemeListener(self)

        # All sub-interfaces start as None and are built lazily (see _bootstrap /
        # _build_lazy). Storing them None also lets _all_interfaces() skip any
        # that haven't been constructed yet.
        self.convertInterface = None
        self.compressInterface = None
        self.upscaleInterface = None
        self.settingInterface = None
        self.aboutInterface = None
        # (attr, module, class, icon, title_key, position) — module/class are
        # strings so the heavy imports are deferred past the first paint.
        self._lazy = [
            ("compress", "compress_interface", "CompressInterface", FIF.PHOTO, "nav.compress", None),
            ("upscale", "upscale_interface", "UpscaleInterface", FIF.ZOOM, "nav.upscale", None),
            ("settings", "setting_interface", "SettingInterface", FIF.SETTING, "nav.settings", NavigationItemPosition.BOTTOM),
            ("about", "about_interface", "AboutInterface", FIF.INFO, "nav.about", NavigationItemPosition.BOTTOM),
        ]

        self.themeListener.start()
        self._connect_config()
        # Defer ALL interface construction until after the first paint so the
        # window (and its splash) is on screen immediately.
        QTimer.singleShot(0, self._bootstrap)

    # -- config signals --------------------------------------------------
    def _connect_config(self):
        cfg.language.valueChanged.connect(self._on_language)
        # Theme change => restart the app so the whole UI re-themes instantly
        # and correctly (v0.2.5 #2/#5). The saved theme is applied at startup
        # in __main__, so this slot only fires on a real user change — never at
        # launch (loading config does not emit valueChanged).
        cfg.theme.valueChanged.connect(self._restart_app)
        # Auto-save every config item change so users don't lose settings
        # (e.g. theme switched, language switched, ...).
        self._save_config_slot = lambda: qconfig.save()
        for name in dir(cfg.__class__):
            attr = getattr(cfg.__class__, name)
            if isinstance(attr, ConfigItem):
                attr.valueChanged.connect(self._save_config_slot)

    def _bootstrap(self):
        """Build the Convert view right after the first paint."""
        from .convert_interface import ConvertInterface
        self.convertInterface = ConvertInterface(self.manager, self)
        self.navigationInterface.setAcrylicEnabled(True)
        self.addSubInterface(self.convertInterface, FIF.HOME, tr("nav.convert"))
        self.convertInterface.retranslateUi()
        self.splashScreen.finish()
        # Build the secondary screens a few frames later so the UI stays
        # responsive and the splash clears as soon as the home view is ready.
        for i, spec in enumerate(self._lazy):
            QTimer.singleShot(20 * (i + 1), lambda s=spec: self._build_lazy(*s))

    def _build_lazy(self, name, mod, clsname, icon, title_key, position):
        """Construct a secondary screen on demand and register its nav item."""
        if getattr(self, name + "Interface", None) is not None:
            return
        module = importlib.import_module("momentshift.gui." + mod)
        iface = getattr(module, clsname)(self)
        setattr(self, name + "Interface", iface)
        if position is not None:
            self.addSubInterface(iface, icon, tr(title_key), position=position)
        else:
            self.addSubInterface(iface, icon, tr(title_key))
        iface.retranslateUi()

    def _all_interfaces(self):
        out = []
        for attr in ("convertInterface", "compressInterface", "upscaleInterface",
                    "settingInterface", "aboutInterface"):
            iface = getattr(self, attr, None)
            if iface is not None:
                out.append(iface)
        return out

    def _on_language(self, value):
        translator.set_locale(LocaleKey(value))
        self.retranslate_all()

    # -- theme switch => restart ----------------------------------------
    def _restart_app(self):
        """Schedule a process restart (deferred so the current event settles)."""
        QTimer.singleShot(0, self._do_restart)

    def _do_restart(self):
        try:
            qconfig.save()
        except Exception:
            pass
        try:
            self.themeListener.terminate()
            self.themeListener.deleteLater()
        except Exception:
            pass
        import os
        import sys
        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable, "-m", "momentshift"] + sys.argv[1:]
        # Replace the current process image with a fresh one. Because startup
        # is instant, this feels like an in-place UI refresh (#2/#5).
        os.execv(sys.executable, args)

    def retranslate_all(self):
        for iface in self._all_interfaces():
            iface.retranslateUi()
        nav = self.navigationInterface
        if hasattr(nav, "setItemText"):
            for route, text in {
                "Convert": tr("nav.convert"),
                "Compress": tr("nav.compress"),
                "Upscale": tr("nav.upscale"),
                "Settings": tr("nav.settings"),
                "About": tr("nav.about"),
            }.items():
                try:
                    nav.setItemText(route, text)
                except Exception:
                    pass
        self.setWindowTitle(tr("app.title"))
        self.navigationInterface.setCurrentItem("Convert")

    # -- window ----------------------------------------------------------
    def initWindow(self):
        # Portrait phone-like aspect ratio. v0.2.5: widen to 525 (525 x 1000).
        self.resize(525, 1000)
        self.setMinimumWidth(420)
        # Disable Mica: it makes the window background transparent on Win11 and
        # the content area inherits a grey-ish Mica material instead of the dark
        # theme colour. With Mica off, FluentWindow paints the solid custom bg.
        self.setMicaEffectEnabled(False)
        # Give the window (and the content area behind transparent scroll
        # views) a deterministic theme background instead of relying on the
        # default, which left the inner frame grey in dark mode.
        self.setCustomBackgroundColor(LIGHT_BG, DARK_BG)
        self.setWindowTitle(tr("app.title"))
        self.setWindowIcon(FIF.APPLICATION.icon())

        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(96, 96))
        self.splashScreen.raise_()

        desktop = QApplication.primaryScreen().availableGeometry()
        self.move(
            desktop.width() // 2 - self.width() // 2,
            desktop.height() // 2 - self.height() // 2,
        )
        self.show()
        QApplication.processEvents()

    def closeEvent(self, event):
        qconfig.save()
        try:
            self.themeListener.terminate()
            self.themeListener.deleteLater()
        except Exception:
            pass
        super().closeEvent(event)
