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
