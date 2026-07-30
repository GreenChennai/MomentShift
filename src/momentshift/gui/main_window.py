"""Application main window (FluentWindow) wiring navigation + theme + i18n."""

from PyQt6.QtCore import QTimer

from ..core.qt_compat import QApplication, QIcon, QSize
from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    FluentIcon as FIF,
    SplashScreen,
    SystemThemeListener,
    setTheme,
    Theme,
)
from ..core.config import cfg
from ..i18n.translator import tr, translator, LocaleKey
from qfluentwidgets import ConfigItem
from qfluentwidgets import qconfig
from .theme import LIGHT_BG, DARK_BG
from .convert_interface import ConvertInterface
from .compress_interface import CompressInterface
from .upscale_interface import UpscaleInterface
from .setting_interface import SettingInterface
from .about_interface import AboutInterface


class MainWindow(FluentWindow):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.initWindow()

        self.themeListener = SystemThemeListener(self)

        # Build the default (Convert) view eagerly so the window has content
        # the moment it appears. The other screens are constructed lazily after
        # the first paint (see _build_lazy) so startup stays instant.
        self.convertInterface = ConvertInterface(manager, self)
        self.navigationInterface.setAcrylicEnabled(True)
        self.addSubInterface(self.convertInterface, FIF.HOME, tr("nav.convert"))

        self.compressInterface = None
        self.upscaleInterface = None
        self.settingInterface = None
        self.aboutInterface = None
        self._lazy = [
            ("compress", CompressInterface, FIF.PHOTO, "nav.compress", None),
            ("upscale", UpscaleInterface, FIF.ZOOM, "nav.upscale", None),
            ("settings", SettingInterface, FIF.SETTING, "nav.settings", NavigationItemPosition.BOTTOM),
            ("about", AboutInterface, FIF.INFO, "nav.about", NavigationItemPosition.BOTTOM),
        ]

        self.splashScreen.finish()
        self.themeListener.start()
        self._connect_config()
        # Apply the saved language + theme immediately (config is loaded before
        # the signal connections exist, so valueChanged never fires on startup).
        translator.set_locale(LocaleKey(cfg.language.value))
        self.convertInterface.retranslateUi()
        self._on_theme(cfg.theme.value)

        # Defer building the secondary screens a few frames so the event loop is
        # free and the window is responsive immediately.
        for i, spec in enumerate(self._lazy):
            QTimer.singleShot(20 * (i + 1), lambda s=spec: self._build_lazy(*s))

    # -- config signals --------------------------------------------------
    def _connect_config(self):
        cfg.language.valueChanged.connect(self._on_language)
        cfg.theme.valueChanged.connect(self._on_theme)
        # Re-apply custom (non-qfluentwidgets) styles whenever the effective
        # theme changes — this also fires in "auto" mode when the OS theme
        # flips, which cfg.theme.valueChanged alone would miss.
        qconfig.themeChanged.connect(self._retheme_all)
        # Auto-save every config item change so users don't lose settings
        # (e.g. theme switched from "auto" to "dark").
        self._save_config_slot = lambda: qconfig.save()
        for name in dir(cfg.__class__):
            attr = getattr(cfg.__class__, name)
            if isinstance(attr, ConfigItem):
                attr.valueChanged.connect(self._save_config_slot)

    def _build_lazy(self, name, cls, icon, title_key, position):
        """Construct a secondary screen on demand and register its nav item."""
        if getattr(self, name + "Interface", None) is not None:
            return
        iface = cls(self)
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

    def _retheme_all(self):
        for iface in self._all_interfaces():
            iface.retheme()

    def _on_language(self, value):
        translator.set_locale(LocaleKey(value))
        self.retranslate_all()

    def _on_theme(self, value):
        setTheme({"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}.get(value, Theme.AUTO))

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
        # Portrait phone-like aspect ratio, enlarged per v0.1.5 request.
        self.resize(450, 1000)
        self.setMinimumWidth(360)
        # Disable Mica: it makes the window background transparent on Win11 and
        # the content area inherits a grey-ish Mica material instead of the dark
        # theme color. With Mica off, FluentWindow paints the solid custom bg.
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
        self.themeListener.terminate()
        self.themeListener.deleteLater()
        super().closeEvent(event)
