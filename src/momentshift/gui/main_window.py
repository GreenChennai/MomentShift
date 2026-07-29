"""Application main window (FluentWindow) wiring navigation + theme + i18n."""

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
from qfluentwidgets import qconfig
from .theme import LIGHT_BG, DARK_BG
from .convert_interface import ConvertInterface
from .setting_interface import SettingInterface
from .about_interface import AboutInterface


class MainWindow(FluentWindow):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.initWindow()

        self.themeListener = SystemThemeListener(self)

        self.convertInterface = ConvertInterface(manager, self)
        self.settingInterface = SettingInterface(self)
        self.aboutInterface = AboutInterface(self)

        self.navigationInterface.setAcrylicEnabled(True)
        self.initNavigation()

        self.splashScreen.finish()
        self.themeListener.start()
        self._connect_config()
        # Apply the saved language + theme immediately (config is loaded before
        # the signal connections exist, so valueChanged never fires on startup).
        translator.set_locale(LocaleKey(cfg.language.value))
        self.retranslate_all()
        self._on_theme(cfg.theme.value)

    # -- config signals --------------------------------------------------
    def _connect_config(self):
        cfg.language.valueChanged.connect(self._on_language)
        cfg.theme.valueChanged.connect(self._on_theme)
        # Re-apply custom (non-qfluentwidgets) styles whenever the effective
        # theme changes — this also fires in "auto" mode when the OS theme
        # flips, which cfg.theme.valueChanged alone would miss.
        qconfig.themeChanged.connect(self._retheme_all)

    def _retheme_all(self):
        self.convertInterface.retheme()
        self.settingInterface.retheme()

    def _on_language(self, value):
        translator.set_locale(LocaleKey(value))
        self.retranslate_all()

    def _on_theme(self, value):
        setTheme({"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}.get(value, Theme.AUTO))

    def retranslate_all(self):
        self.convertInterface.retranslateUi()
        self.settingInterface.retranslateUi()
        self.aboutInterface.retranslateUi()
        nav = self.navigationInterface
        if hasattr(nav, "setItemText"):
            nav.setItemText("Convert", tr("nav.convert"))
            nav.setItemText("Settings", tr("nav.settings"))
            nav.setItemText("About", tr("nav.about"))
        self.setWindowTitle(tr("app.title"))

    # -- navigation ------------------------------------------------------
    def initNavigation(self):
        self.addSubInterface(self.convertInterface, FIF.HOME, tr("nav.convert"))
        self.addSubInterface(
            self.settingInterface, FIF.SETTING, tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.aboutInterface, FIF.INFO, tr("nav.about"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.setCurrentItem("Convert")

    # -- window ----------------------------------------------------------
    def initWindow(self):
        self.resize(1000, 720)
        self.setMinimumWidth(820)
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
        self.themeListener.terminate()
        self.themeListener.deleteLater()
        super().closeEvent(event)
