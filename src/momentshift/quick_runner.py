"""快速调用执行器（v0.7.15 重构）。

1. 创建 GUI app + 启动完整 MainWindow
2. 弹出设置窗口（转换/压缩/放大）
3. 确认后把任务注入主窗口对应队列并自动开始
4. 全部完成后系统托盘通知
（已删除「任务进度」窗口，任务直接显示在主窗口队列中）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QCoreApplication, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox
from qfluentwidgets import setTheme, Theme, setThemeColor

from .core.config import cfg
from .i18n.translator import translator, LocaleKey
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


def _notify(window, title: str, body: str) -> None:
    """系统托盘通知任务完成。"""
    try:
        tray = getattr(window, "tray", None)
        if tray is None:
            tray = QSystemTrayIcon(window.windowIcon(), window)
            tray.show()
            window.tray = tray
        tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)
    except Exception:
        log.warning("quick: notify failed (%s)", title)


def _fatal(msg: str) -> None:
    log.critical("quick fatal: %s", msg)
    try:
        QMessageBox.critical(None, APP_NAME, msg)
    except Exception:
        pass
    QCoreApplication.quit()


# ---------------------------------------------------------------------------
def run_quick(task: str, files: list[str]) -> int:
    """快速调用入口。启动主窗口并把任务注入对应队列。"""
    log.info("Quick launch: task=%s files=%d first=%s", task, len(files),
             files[0] if files else "")
    app = _setup_app()

    from .core.queue import ConversionManager
    from .gui.main_window import MainWindow

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    # 确保目标大模块界面已构建（压缩/放大为懒加载）
    target = {"compress": "compress", "upscale": "upscale"}.get(task)
    if target:
        for spec in window._lazy:
            if spec[0] == target:
                window._build_lazy(*spec)
                break

    def _dispatch():
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
            tb = traceback.format_exc()
            log.critical("quick dispatch failed:\n%s", tb)
            try:
                QMessageBox.critical(None, APP_NAME, f"快速调用失败：\n{tb[-1500:]}")
            except Exception:
                pass

    QTimer.singleShot(80, _dispatch)
    app.exec()
    log.info("quick: %s done", task)
    return 0


# ---------------------------------------------------------------------------
def _run_convert(files, window, manager):
    """右键 → 转换：弹「转换设置」→ 注入主窗口转换队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category
    from .gui.convert_setup_dialog import ConvertSetupDialog

    valid_exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        _fatal("没有找到可转换的媒体文件（图片/音频/视频）。")
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

    cat = next(iter(cat_files))
    paths = cat_files[cat]

    dlg = ConvertSetupDialog(None, manager, paths, {}, lambda: gpu, cat)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

    # 任务全部完成后系统通知
    done = {"n": len(paths), "count": 0}
    def _on_task_finished(tid, ok, saved, detail):
        done["count"] += 1
        if done["count"] >= done["n"]:
            _notify(window, tr("quick.notify.title"),
                    tr("quick.notify.convert_done"))
    manager.task_finished.connect(_on_task_finished)

    def _on_dialog_finished(r: int):
        if r == 1:
            manager.start()   # 主窗口转换队列自动开始
        else:
            _fatal("已取消。")
    dlg.finished.connect(_on_dialog_finished)
    _KEEP_ALIVE.append(dlg)
    dlg.show()


def _run_compress(files, window):
    """右键 → 压缩：弹「创建图片压缩任务」→ 注入主窗口压缩队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS
    from .gui.quick_dialogs import QuickCompressDialog

    valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
    if not valid_files:
        _fatal("没有找到可压缩的图片文件。")
        return

    ci = window.compressInterface
    total = {"n": 0, "done": 0}

    def _on_task_finished(iid, status):
        total["done"] += 1
        if total["done"] >= total["n"] > 0:
            _notify(window, tr("quick.notify.title"),
                    tr("quick.notify.compress_done"))
    ci.taskFinished.connect(_on_task_finished)

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
        total["n"] = len(paths)
        ci._on_start()   # 主窗口压缩队列自动开始

    dlg = QuickCompressDialog(None, valid_files, _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(
        lambda r: _fatal("已取消。") if r == 0 else None)
    _KEEP_ALIVE.append(dlg)
    dlg.show()


def _run_upscale(files, window):
    """右键 → 放大：弹「创建图片放大任务」→ 注入主窗口放大队列 → 自动开始。"""
    from .core.presets import IMAGE_EXTS
    from .core import engines as eng_mod
    from .gui.quick_dialogs import QuickUpscaleDialog

    valid_exts = IMAGE_EXTS | eng_mod.ANIM_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        _fatal("没有找到可放大的图片文件。")
        return
    if not eng_mod.installed_engines():
        _fatal("尚未安装任何放大引擎，请先到「关于」页下载引擎。")
        return

    ui = window.upscaleInterface
    total = {"n": 0, "done": 0}

    def _on_task_finished(iid, status):
        total["done"] += 1
        if total["done"] >= total["n"] > 0:
            _notify(window, tr("quick.notify.title"),
                    tr("quick.notify.upscale_done"))
    ui.taskFinished.connect(_on_task_finished)

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
        total["n"] = len(paths)
        ui._on_start()   # 主窗口放大队列自动开始

    dlg = QuickUpscaleDialog(None, valid_files, _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(
        lambda r: _fatal("已取消。") if r == 0 else None)
    _KEEP_ALIVE.append(dlg)
    dlg.show()
