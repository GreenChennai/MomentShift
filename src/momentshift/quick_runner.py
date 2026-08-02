"""快速调用执行器（v0.7.15 重构）。

1. 创建 GUI app + 启动完整 MainWindow
2. 弹出设置窗口（转换/压缩/放大）
3. 确认后把任务注入主窗口对应队列并自动开始
4. 全部完成后系统托盘通知
（已删除「任务进度」窗口，任务直接显示在主窗口队列中）
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

from PyQt6.QtCore import QTimer, QCoreApplication, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox
from qfluentwidgets import setTheme, Theme, setThemeColor

from .core.config import cfg
from .i18n.translator import translator, LocaleKey, tr
from .core.logger import get_logger
from .metadata import APP_NAME, VERSION

log = get_logger("quick")

# 持有弹窗引用，防止局部变量被 GC 导致窗口闪退（v0.7.13）
_KEEP_ALIVE: list = []


def _setup_app() -> QApplication:
    """创建并初始化 GUI 应用（主题/字体/语言同主窗口）。"""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    # v0.7.14：窗口关闭不退出事件循环，否则设置弹窗 accept 后进程直接退出
    app.setQuitOnLastWindowClosed(False)

    _res = Path(__file__).resolve().parent / "resources"
    for _name in ("HarmonyOS_Sans_SC_Regular.ttf", "FiraCode-Regular.ttf"):
        _fp = _res / _name
        if _fp.exists():
            QFontDatabase.addApplicationFont(str(_fp))
    _qfont = QFont("HarmonyOS Sans SC", 10)
    _qfont.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(_qfont)
    app.setStyleSheet(
        "* { font-family: 'HarmonyOS Sans SC', 'FiraCode', 'Microsoft YaHei', sans-serif; }")

    setTheme(Theme.LIGHT)
    setThemeColor(QColor("#238636"))
    translator.set_locale(LocaleKey(cfg.language.value))
    return app


def _notify(window, title: str, body: str, enabled_key: str = "quickNotifyDone") -> None:
    """任务开始/完成提示（v0.7.28）。

    - 通知：托盘 showMessage（Windows 通知弹窗**自带声音**，不再额外蜂鸣）
    - 开关：cfg.quickNotifyStart / quickNotifyDone（enabled_key 指定）
    - 开关关闭 → 不弹通知
    """
    from .core.config import cfg as _cfg
    sw = getattr(_cfg, enabled_key, None)
    if sw is not None and not sw.value:
        log.info("quick notify disabled (%s): %s — %s", enabled_key, title, body)
        return
    try:
        tray = getattr(window, "tray", None)
        if tray is None:
            tray = QSystemTrayIcon(window.windowIcon(), window)
            tray.show()
            window.tray = tray
        tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)
        log.info("quick notify shown: %s — %s", title, body)
    except Exception:
        log.warning("quick: notify failed (%s)", title)


def _load_files_async(dlg, files, chunk: int = 8, delay: int = 25) -> None:
    """v0.7.24：窗口秒弹后异步分段载入文件；载入期间确认按钮禁用并显示「载入中」。"""
    batch = list(files)
    dlg.set_loading(True)

    def _add_next():
        if not batch:
            dlg.set_loading(False)
            return
        part = batch[:chunk]
        del batch[:chunk]
        dlg.add_paths(part)
        QTimer.singleShot(delay, _add_next)

    QTimer.singleShot(0, _add_next)


def _fatal(msg: str) -> None:
    log.critical("quick fatal: %s", msg)
    try:
        QMessageBox.critical(None, APP_NAME, msg)
    except Exception:
        pass
    QCoreApplication.quit()


# ---------------------------------------------------------------------------
# v0.7.22：模块级一次性通知连接（多次 flush 不重复连接/重复通知）
# ---------------------------------------------------------------------------
def _ensure_convert_notify(window, manager) -> None:
    if getattr(manager, "_quick_notify_bound", False):
        return
    manager._quick_notify_bound = True
    state = {"n": 0, "done": 0, "notified": False}

    def _on_added(t):
        state["n"] += 1
        state["notified"] = False

    # v0.7.23 修复：task_finished 信号为 Signal(str, bool, str)（3 参数），
    # 之前写成 4 参数 (tid, ok, saved, detail) 导致 PyQt 静默连接失败，
    # 转换任务完成从未触发过通知！
    def _on_finished(tid, ok, _err):
        state["done"] += 1
        if (state["n"] > 0 and state["done"] >= state["n"]
                and not state["notified"]):
            state["notified"] = True
            log.info("convert notify: n=%d done=%d", state["n"], state["done"])
            # v0.7.26：下个主循环 tick 再通知，避免信号槽链深处延迟
            QTimer.singleShot(0, lambda: _notify(
                window, tr("quick.notify.title"),
                tr("quick.notify.convert_done")))

    manager.task_added.connect(_on_added)
    manager.task_finished.connect(_on_finished)


def _ensure_compress_notify(window, ci) -> None:
    if getattr(ci, "_quick_notify_bound", False):
        return
    ci._quick_notify_bound = True
    state = {"n": 0, "done": 0, "notified": False}

    def _on_added(iid, name):
        state["n"] += 1
        state["notified"] = False

    def _on_finished(iid, status):
        state["done"] += 1
        if (state["n"] > 0 and state["done"] >= state["n"]
                and not state["notified"]):
            state["notified"] = True
            log.info("compress notify: n=%d done=%d", state["n"], state["done"])
            QTimer.singleShot(0, lambda: _notify(
                window, tr("quick.notify.title"),
                tr("quick.notify.compress_done")))

    ci.taskAdded.connect(_on_added)
    ci.taskFinished.connect(_on_finished)


def _ensure_upscale_notify(window, ui) -> None:
    if getattr(ui, "_quick_notify_bound", False):
        return
    ui._quick_notify_bound = True
    state = {"n": 0, "done": 0, "notified": False}

    def _on_added(iid, name):
        state["n"] += 1
        state["notified"] = False

    def _on_finished(iid, status):
        state["done"] += 1
        if (state["n"] > 0 and state["done"] >= state["n"]
                and not state["notified"]):
            state["notified"] = True
            log.info("upscale notify: n=%d done=%d", state["n"], state["done"])
            QTimer.singleShot(0, lambda: _notify(
                window, tr("quick.notify.title"),
                tr("quick.notify.upscale_done")))

    ui.taskAdded.connect(_on_added)
    ui.taskFinished.connect(_on_finished)


# ---------------------------------------------------------------------------
_QUICK_IPC_NAME = "MomentShift_QuickIPC_v0716"


def _try_forward_to_running(task: str, files: list[str]) -> bool:
    """若已有 MomentShift 实例在运行，通过 QLocalServer 转发请求并退出。

    返回 True 表示已转发（本进程应直接退出）。
    """
    from PyQt6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(_QUICK_IPC_NAME)
    if not sock.waitForConnected(200):   # v0.7.23：缩短连接超时
        return False
    try:
        payload = json.dumps({"task": task, "files": files}).encode("utf-8")
        sock.write(payload)
        sock.flush()
        sock.waitForBytesWritten(300)   # v0.7.23：缩短写超时
        log.info("quick: forwarded %s (%d files) to running instance",
                 task, len(files))
        return True
    except Exception:
        log.exception("quick: forward failed")
        return False
    finally:
        try:
            sock.disconnectFromServer()
        except Exception:
            pass


def handle_quick_batch(batch, window, manager) -> None:
    """v0.7.22：批量快速调用请求 → 同 task 合并文件后弹窗。

    batch: list[(task, files)]，来自本地请求或 IPC 转发（多选 %1 逐文件）。
    """
    merged: dict[str, list[str]] = {}
    for task, files in batch:
        merged.setdefault(task, []).extend(files or [])
    for task, files in merged.items():
        log.info("quick batch dispatch: task=%s files=%d", task, len(files))
        try:
            if task == "convert":
                _run_convert(files, window, manager)
            elif task == "compress":
                _run_compress(files, window)
            elif task == "upscale":
                _run_upscale(files, window)
            else:
                log.error("quick: unknown task %s", task)
        except Exception:
            import traceback
            log.critical("quick batch dispatch failed:\n%s", traceback.format_exc())


# ---------------------------------------------------------------------------
def run_quick(task: str, files: list[str]) -> int:
    """快速调用入口。

    v0.7.22：%1 逐文件调用（可靠）；多选由已运行实例按时间窗口聚合。
    - 已有实例 → IPC 转发进批次队列
    - 无实例 → 启动主窗口，本地请求也进同一批次队列
    - 单实例锁被占（多选竞态）→ 轮询转发到刚启动的主窗口
    """
    log.info("Quick launch: task=%s files=%d first=%s", task, len(files),
             files[0] if files else "")
    app = _setup_app()

    # 已有实例运行 → 转发请求并退出（不创建第二个主窗口/托盘）
    if _try_forward_to_running(task, files):
        return 0

    # 无 IPC 转发（无实例或实例为旧版本）→ 获取单实例锁，防止后续重复启动
    lock = _acquire_instance_lock()
    if lock is None:
        # v0.7.22：多选时 N 个进程同时启动，锁被首个进程占用 → 轮询转发到
        # 刚启动的主窗口（IPC server 初始化约需 1s），不报错不闪退
        import time
        for _ in range(20):
            if _try_forward_to_running(task, files):
                return 0
            time.sleep(0.25)
        log.warning("quick: 无法转发到已运行实例，静默放弃本次请求")
        return 0
    _KEEP_ALIVE.append(lock)   # 持有锁，防止 GC 释放

    from .core.queue import ConversionManager
    from .gui.main_window import MainWindow

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    # v0.7.22：本地请求也进批次队列（等待懒加载完成后入队，与其他 IPC 请求聚合）
    def _enqueue():
        window.enqueue_quick_request(task, files)
    QTimer.singleShot(150, _enqueue)   # v0.7.23：缩短等待，尽快入批次

    app.exec()
    log.info("quick: %s done", task)
    return 0


def _acquire_instance_lock():
    """与主窗口共用同一把单实例锁（QSharedMemory）。

    --quick 分支绕过了 __main__ 的单实例检查，这里补上，
    保证快速调用启动的主窗口同样只允许一个实例。
    """
    from PyQt6.QtCore import QSharedMemory
    sm = QSharedMemory("MomentShift_SingleInstance_v031")
    if sm.attach():
        return None      # 已有实例
    if sm.create(1):
        return sm
    return None


# ---------------------------------------------------------------------------
def _run_convert(files, window, manager):
    """右键 → 转换：弹「转换设置」→ 注入主窗口转换队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category
    from .gui.convert_setup_dialog import ConvertSetupDialog

    valid_exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        # v0.7.20：IPC 上下文静默返回，绝不 _fatal（否则会把已运行主窗口 quit 闪退）
        log.warning("quick convert: 无有效媒体文件（共 %d 个输入）", len(files))
        return

    from collections import defaultdict
    cat_files: dict = defaultdict(list)
    for f in valid_files:
        cat = guess_category(f)
        if cat:
            cat_files[cat].append(f)
    if not cat_files:
        _fatal("无法识别文件类型。")
        return

    gpu = True
    if cfg.hardware.value == "cpu":
        gpu = False
    elif cfg.hardware.value == "auto":
        gpu = bool(manager.hw)

    # v0.7.22：通知连接一次（模块级），多次 flush 不重复
    _ensure_convert_notify(window, manager)

    for cat, paths in cat_files.items():
        # v0.7.24：空文件秒弹，文件异步分段载入
        dlg = ConvertSetupDialog(None, manager, [], {}, lambda: gpu, cat)
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        def _on_dialog_finished(r: int, _m=manager):
            if r == 1:
                _m.start()   # 主窗口转换队列自动开始
                # v0.7.26：任务已开始提示（v0.7.27：仅 toast 不蜂鸣，刺耳）
                QTimer.singleShot(0, lambda: _notify(
                    window, tr("quick.notify.title"), tr("quick.notify.started"),
                    enabled_key="quickNotifyStart"))
            # v0.7.16：取消不弹任何提示框，主窗口保留
        dlg.finished.connect(_on_dialog_finished)
        _KEEP_ALIVE.append(dlg)
        dlg.show()
        _load_files_async(dlg, paths)


def _run_compress(files, window):
    """右键 → 压缩：弹「创建图片压缩任务」→ 注入主窗口压缩队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS
    from .gui.quick_dialogs import QuickCompressDialog

    valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
    if not valid_files:
        # v0.7.20：IPC 上下文静默返回，绝不 _fatal（否则 quit 掉主窗口）
        log.warning("quick compress: 无有效图片文件（共 %d 个输入）", len(files))
        return

    ci = window.compressInterface
    # v0.7.22：通知连接一次（模块级），多次 flush 不重复
    _ensure_compress_notify(window, ci)

    def _confirm(paths, iface_settings):
        # 应用对话框设置到主窗口压缩队列
        ci._program = iface_settings._program
        ci._tool_opts = iface_settings._tool_opts
        ci._target = iface_settings._target
        ci._output_mode = iface_settings._output_mode
        ci._suffix = iface_settings._suffix
        ci._folder = iface_settings._folder
        for f in paths:
            ci._add_item(f)
        ci._on_start()   # 主窗口压缩队列自动开始
        # v0.7.28：开始通知（受 quickNotifyStart 开关控制，不蜂鸣）
        QTimer.singleShot(0, lambda: _notify(
            window, tr("quick.notify.title"), tr("quick.notify.started"),
            enabled_key="quickNotifyStart"))

    # v0.7.24：空文件秒弹，文件异步分段载入
    dlg = QuickCompressDialog(None, [], _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(lambda r: None)   # v0.7.16：取消不弹提示框
    _KEEP_ALIVE.append(dlg)
    dlg.show()
    _load_files_async(dlg, valid_files)


def _run_upscale(files, window):
    """右键 → 放大：弹「创建图片放大任务」→ 注入主窗口放大队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS
    from .core import engines as eng_mod
    from .gui.quick_dialogs import QuickUpscaleDialog

    valid_exts = IMAGE_EXTS | eng_mod.ANIM_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        # v0.7.20：IPC 上下文静默返回，绝不 _fatal
        log.warning("quick upscale: 无有效图片文件（共 %d 个输入）", len(files))
        return
    if not eng_mod.installed_engines():
        _fatal("尚未安装任何放大引擎，请先到「关于」页下载引擎。")
        return

    ui = window.upscaleInterface
    # v0.7.22：通知连接一次（模块级），多次 flush 不重复
    _ensure_upscale_notify(window, ui)

    def _confirm(paths, iface_settings):
        # 应用对话框设置到主窗口放大队列
        ui._engine_id = iface_settings._engine_id
        ui._fmt = iface_settings._fmt
        ui._output_mode = iface_settings._output_mode
        ui._suffix = iface_settings._suffix
        ui._folder = iface_settings._folder
        # 引擎参数面板（scale/denoise 等）一并应用
        try:
            ui._run_values = iface_settings.paramPanel.values()
        except Exception:
            ui._run_values = None
        ui._add_to_queue(paths)
        ui._on_start()   # 主窗口放大队列自动开始
        # v0.7.28：开始通知（受 quickNotifyStart 开关控制，不蜂鸣）
        QTimer.singleShot(0, lambda: _notify(
            window, tr("quick.notify.title"), tr("quick.notify.started"),
            enabled_key="quickNotifyStart"))

    # v0.7.24：空文件秒弹，文件异步分段载入
    dlg = QuickUpscaleDialog(None, [], _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(lambda r: None)   # v0.7.16：取消不弹提示框
    _KEEP_ALIVE.append(dlg)
    dlg.show()
    _load_files_async(dlg, valid_files)
