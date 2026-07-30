"""主窗口（FluentWindow）—— 导航 + i18n + 系统托盘（v0.3.2 简化：仅浅色主题）。

v0.3.2: 移除深色主题，修复托盘退出不杀进程，确认双击托盘唤起。
"""

import importlib
import os, sys
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PyQt6.QtGui import QAction

from ..core.qt_compat import QApplication as QA, QIcon, QSize
from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    FluentIcon as FIF,
    SplashScreen,
)
from ..core.config import cfg
from ..i18n.translator import tr, translator, LocaleKey
from qfluentwidgets import ConfigItem, qconfig
from .theme import WINDOW_BG

class MainWindow(FluentWindow):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

        translator.set_locale(LocaleKey(cfg.language.value))
        self.initWindow()

        self.convertInterface = None
        self.compressInterface = None
        self.upscaleInterface = None
        self.quickLaunchInterface = None
        self.settingInterface = None
        self.aboutInterface = None

        self._lazy = [
            ("compress", "compress_interface", "CompressInterface", FIF.PHOTO, "nav.compress", None),
            ("upscale", "upscale_interface", "UpscaleInterface", FIF.ZOOM, "nav.upscale", None),
            ("quickLaunch", "quick_launch_interface", "QuickLaunchInterface", FIF.SEND, "quicklaunch.title", None),
            ("settings", "setting_interface", "SettingInterface", FIF.SETTING, "nav.settings", NavigationItemPosition.BOTTOM),
            ("about", "about_interface", "AboutInterface", FIF.INFO, "nav.about", NavigationItemPosition.BOTTOM),
        ]

        self._connect_config()
        self._force_quit = False
        self._init_tray()
        QTimer.singleShot(0, self._bootstrap)

    def _connect_config(self):
        cfg.language.valueChanged.connect(self._on_language)
        self._save_config_slot = lambda: qconfig.save()
        for name in dir(cfg.__class__):
            attr = getattr(cfg.__class__, name)
            if isinstance(attr, ConfigItem):
                attr.valueChanged.connect(self._save_config_slot)

    def _bootstrap(self):
        from .convert_interface import ConvertInterface
        self.convertInterface = ConvertInterface(self.manager, self)
        self.navigationInterface.setAcrylicEnabled(True)
        self.addSubInterface(self.convertInterface, FIF.HOME, tr("nav.convert"))
        self.convertInterface.retranslateUi()
        self.splashScreen.finish()
        for i, spec in enumerate(self._lazy):
            QTimer.singleShot(20 * (i + 1), lambda s=spec: self._build_lazy(*s))

    def _build_lazy(self, name, mod, clsname, icon, title_key, position):
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
                    "quickLaunchInterface", "settingInterface", "aboutInterface"):
            iface = getattr(self, attr, None)
            if iface is not None:
                out.append(iface)
        return out

    def _on_language(self, value):
        translator.set_locale(LocaleKey(value))
        self.retranslate_all()

    def retranslate_all(self):
        for iface in self._all_interfaces():
            iface.retranslateUi()
        nav = self.navigationInterface
        if hasattr(nav, "setItemText"):
            for route, text in {
                "Convert": tr("nav.convert"),
                "Compress": tr("nav.compress"),
                "Upscale": tr("nav.upscale"),
                "QuickLaunch": tr("quicklaunch.title"),
                "Settings": tr("nav.settings"),
                "About": tr("nav.about"),
            }.items():
                try:
                    nav.setItemText(route, text)
                except Exception:
                    pass
        self.setWindowTitle(tr("app.title"))
        self.navigationInterface.setCurrentItem("Convert")

    def initWindow(self):
        self.resize(525, 1000)
        self.setMinimumWidth(420)
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(WINDOW_BG, WINDOW_BG)
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

    # =========================================================================
    # 系统托盘
    # =========================================================================
    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.windowIcon())
        self.tray.setToolTip(tr("tray.title"))
        menu = QMenu(self)
        show_action = QAction(tr("tray.show"), self)
        quit_action = QAction(tr("tray.quit"), self)
        show_action.triggered.connect(self._tray_show)
        quit_action.triggered.connect(self._tray_quit)
        menu.addAction(show_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        # 双击托盘图标 → 显示主窗口 (v0.2.7 #3 + v0.3.2 确认)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def _tray_quit(self):
        self._force_quit = True
        self.close()

    def closeEvent(self, event):
        if self._force_quit:
            self._cleanup_and_quit(event)
            return
        if cfg.closeToTray.value:
            event.ignore()
            self.hide()
            try:
                self.tray.showMessage(
                    tr("tray.title"), tr("tray.hidden"),
                    QSystemTrayIcon.MessageIcon.Information, 2500)
            except Exception:
                pass
            return
        self._cleanup_and_quit(event)

    def _cleanup_and_quit(self, event):
        """彻底退出：保存配置 + 隐藏托盘 + 关闭窗口 + 退出 Qt 事件循环 (v0.3.1)。"""
        qconfig.save()
        try:
            self.tray.hide()
        except Exception:
            pass
        event.accept()
        # 确保 Qt 事件循环退出，进程彻底终止
        QApplication.quit()
