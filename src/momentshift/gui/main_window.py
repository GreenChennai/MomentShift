"""主窗口（FluentWindow）—— 导航 + i18n + 系统托盘。

职责边界：
- 做：装配导航栏与各功能页、托盘图标、语言与主题切换的全局广播。
- 不做：不实现任何具体功能页的业务逻辑。

依赖：core/config、core/logger、core/qt_compat、gui/convert_interface、gui/theme、i18n/translator、quick_runner；被依赖：quick_runner（复用主窗口实例）。

踩坑教训：托盘「退出」必须显式调 QApplication.quit()，只关窗口会留下
不可见的进程常驻，用户再次启动时会被单实例检查挡住。
"""

import importlib

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from qfluentwidgets import (
    FluentIcon as FIF,
)
from qfluentwidgets import (
    FluentWindow,
    NavigationItemPosition,
    SplashScreen,
)

from ..core.config import cfg, connect_autosave
from ..core.logger import get_logger
from ..core.qt_compat import QSize
from ..i18n.translator import LocaleKey, tr, translator
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
        self.asrInterface = None
        self.quickLaunchInterface = None
        self.settingInterface = None
        self.aboutInterface = None

        self._lazy = [
            (
                "compress",
                "compress_interface",
                "CompressInterface",
                FIF.PHOTO,
                "nav.compress",
                None,
            ),
            ("upscale", "upscale_interface", "UpscaleInterface", FIF.ZOOM, "nav.upscale", None),
            # v0.8.3：音频转文字（ASR）——「放大」与「快速调用」之间
            (
                "asr",
                "asr_interface",
                "AudioTranscribeInterface",
                FIF.MICROPHONE,
                "nav.asr",
                None,
            ),
            (
                "quickLaunch",
                "quick_launch_interface",
                "QuickLaunchInterface",
                FIF.SEND,
                "quicklaunch.title",
                None,
            ),
            (
                "settings",
                "setting_interface",
                "SettingInterface",
                FIF.SETTING,
                "nav.settings",
                NavigationItemPosition.BOTTOM,
            ),
            (
                "about",
                "about_interface",
                "AboutInterface",
                FIF.INFO,
                "nav.about",
                NavigationItemPosition.BOTTOM,
            ),
        ]

        self._connect_config()
        self._force_quit = False
        self._init_tray()
        self._init_quick_ipc()  # 接收快速调用 IPC 请求
        QTimer.singleShot(0, self._bootstrap)

    def _connect_config(self):
        """接语言切换 + 打开配置自动存盘（ODD-22 的唯一持久化入口）。

        Notes:
            v0.8.0 之前这里自己遍历 ``dir(cfg.__class__)`` 连存盘 lambda，业务代码
            里又散落着十几处手写 ``qconfig.save()``。现在收敛成一句
            ``connect_autosave()``：它是幂等的（主窗口万一被构造两次也不会把每次
            改值存两遍），覆盖范围由 ``tests/config_coverage.py`` 做全等断言兜底。
        """
        cfg.language.valueChanged.connect(self._on_language)
        connect_autosave()

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

    def goto_about(self):
        """跳转到「关于」页（v0.7.5：放大界面的「检测环境」按钮）。

        关于页是懒加载的，若还没建好就先立刻补建，避免按钮点了没反应。
        """
        if self.aboutInterface is None:
            for spec in self._lazy:
                if spec[0] == "about":
                    self._build_lazy(*spec)
                    break
        iface = self.aboutInterface
        if iface is None:
            return
        if hasattr(iface, "enginesCard") and iface.enginesCard is not None:
            iface.enginesCard.rescan()
        self.switchTo(iface)
        self.navigationInterface.setCurrentItem(iface.objectName())

    def _all_interfaces(self):
        out = []
        for attr in (
            "convertInterface",
            "compressInterface",
            "upscaleInterface",
            "asrInterface",
            "quickLaunchInterface",
            "settingInterface",
            "aboutInterface",
        ):
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
                "Asr": tr("nav.asr"),
                "QuickLaunch": tr("quicklaunch.title"),
                "Settings": tr("nav.settings"),
                "About": tr("nav.about"),
            }.items():
                try:
                    nav.setItemText(route, text)
                except Exception:
                    get_logger("app").debug(
                        "更新导航文案失败，忽略"
                    )  # 静默原因：导航项可能已随界面销毁
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
        # 调整2：全局取消鼠标悬停提示，托盘图标同样不再设置 ToolTip
        menu = QMenu(self)
        show_action = QAction(tr("tray.show"), self)
        quit_action = QAction(tr("tray.quit"), self)
        show_action.triggered.connect(self._tray_show)
        quit_action.triggered.connect(self._tray_quit)
        menu.addAction(show_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        # 双击托盘图标 → 显示主窗口 ( #3 +  确认)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    # =========================================================================
    # 快速调用 IPC：已运行实例接收右键快速调用请求
    # =========================================================================
    _QUICK_IPC_NAME = "MomentShift_QuickIPC_v0716"

    def _init_quick_ipc(self):

        from PyQt6.QtNetwork import QLocalServer

        self._ipc_server = QLocalServer(self)
        self._ipc_server.removeServer(self._QUICK_IPC_NAME)
        if self._ipc_server.listen(self._QUICK_IPC_NAME):
            self._ipc_server.newConnection.connect(self._on_ipc_connection)
            get_logger("app").info("quick IPC server listening on %s", self._QUICK_IPC_NAME)
        else:
            get_logger("app").warning("quick IPC server failed to listen")
        # 快速调用批次聚合（多选 %1 逐文件调用 → 1.2s 窗口合并）
        from PyQt6.QtCore import QTimer as _QT

        self._quick_pending: list[tuple] = []
        self._quick_timer = _QT(self)
        self._quick_timer.setSingleShot(True)
        self._quick_timer.timeout.connect(self._flush_quick_batch)

    def enqueue_quick_request(self, task: str, files: list[str]) -> None:
        """v0.7.22：入快速调用批次队列，1.2s 后统一弹窗（多选合并）。"""
        if not task or not files:
            get_logger("quick").warning("quick batch skip: task=%s files=%d", task, len(files))
            return
        self._quick_pending.append((task, list(files)))
        self._quick_timer.start(400)  # 400ms 聚合窗口，点击到弹窗 ~1s
        get_logger("quick").info(
            "quick batch enqueue: task=%s files=%d pending=%d",
            task,
            len(files),
            len(self._quick_pending),
        )

    def _flush_quick_batch(self) -> None:
        if not self._quick_pending:
            return
        pending, self._quick_pending = self._quick_pending, []
        get_logger("quick").info("quick batch flush: %d requests", len(pending))
        from ..quick_runner import handle_quick_batch

        handle_quick_batch(pending, self, self.manager)

    def _on_ipc_connection(self):
        conn = self._ipc_server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda c=conn: self._on_ipc_data(c))

    def _on_ipc_data(self, conn):
        import json

        try:
            raw = bytes(conn.readAll()).decode("utf-8", errors="replace")
            req = json.loads(raw)
            task = req.get("task", "")
            files = req.get("files", [])
            get_logger("quick").info("IPC request: task=%s files=%d", task, len(files))
            if task and files:
                # 不弹前台、不抢焦点 —— 后台静默处理任务
                # 进批次队列，与本地请求聚合
                self.enqueue_quick_request(task, files)
        except Exception:
            get_logger("app").exception("IPC quick request failed")
        finally:
            try:
                conn.disconnectFromServer()
                conn.deleteLater()
            except Exception:
                get_logger("app").debug(
                    "清理 IPC 连接失败，忽略"
                )  # 静默原因：关闭阶段连接可能已失效

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
                    tr("tray.title"),
                    tr("tray.hidden"),
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
            except Exception:
                get_logger("app").debug(
                    "托盘图标通知失败，忽略"
                )  # 静默原因：托盘在关闭阶段可能已不可用
            return
        self._cleanup_and_quit(event)

    def _cleanup_and_quit(self, event):
        """彻底退出：隐藏托盘 + 关闭窗口 + 退出 Qt 事件循环 (v0.3.1)。

        Notes:
            v0.8.0 ODD-22：这里原本还有一次「退出前保存配置」。它是双保险时代的
            遗留——每一项配置在改动瞬间就已经由 ``connect_autosave()`` 落盘了，
            退出时再存一遍存的是同样的内容。移除它顺带去掉了一个隐患：退出路径上
            的磁盘写入一旦抛异常，会把关窗流程整个卡住。
        """
        try:
            self.tray.hide()
        except Exception:
            get_logger("app").debug(
                "隐藏托盘图标失败，忽略"
            )  # 静默原因：托盘在关闭阶段可能已不可用
        event.accept()
        # 确保 Qt 事件循环退出，进程彻底终止
        QApplication.quit()
