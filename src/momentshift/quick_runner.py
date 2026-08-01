"""快速调用执行器（v0.7.12 重建）。

独立进程模式（``--quick <task> <files>``）：
1. 创建 GUI app + 主题/字体
2. 实例化对应大模块的队列界面（不显示，仅复用队列管理）
3. 弹出设置窗口（转换=ConvertSetupDialog；压缩/放大=quick_dialogs）
4. 确认后入队并自动开始任务
5. 右下角任务进度窗显示完成情况（设置可关）
6. 全部任务完成后自动退出
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QCoreApplication, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme, setThemeColor

from .core.config import cfg
from .i18n.translator import translator, LocaleKey
from .core.logger import get_logger
from .metadata import APP_NAME, VERSION

log = get_logger("quick")


def _setup_app() -> QApplication:
    """创建并初始化 GUI 应用（主题/字体/语言同主窗口）。"""
    from PyQt6.QtCore import Qt as _Qt
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        _Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)

    # 字体（与主窗口一致）
    _res = Path(__file__).resolve().parent.parent / "resources"
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


# ---------------------------------------------------------------------------
def run_quick(task: str, files: list[str]) -> int:
    """快速调用入口。返回进程退出码。"""
    log.info("Quick launch: task=%s files=%d first=%s", task, len(files),
             files[0] if files else "")
    app = _setup_app()
    result = {"code": 1}

    from .gui.task_progress_window import TaskProgressWindow
    progress = TaskProgressWindow() if cfg.quickLaunchProgressWindow.value else None
    if progress:
        progress.show()

    def _dispatch():
        try:
            if task == "convert":
                _run_convert(files, progress, result)
            elif task == "compress":
                _run_compress(files, progress, result)
            elif task == "upscale":
                _run_upscale(files, progress, result)
            else:
                log.error("quick: unknown task %s", task)
                QCoreApplication.quit()
        except Exception:
            import traceback
            log.critical("quick dispatch failed:\n" + traceback.format_exc())
            QCoreApplication.quit()

    QTimer.singleShot(0, _dispatch)
    app.exec()
    log.info("quick: %s done, exit=%d", task, result["code"])
    return result["code"]


def _quit_when_all_done(progress, result, iface) -> None:
    """任务全部完成后延迟退出。"""
    QTimer.singleShot(600, lambda: QCoreApplication.quit())


def _connect_convert_progress(manager, progress):
    if not progress:
        return
    manager.task_added.connect(
        lambda t: progress.add_task(t.id, Path(t.input_path).name))
    manager.progress_updated.connect(
        lambda tid, pct: progress.update_progress(tid, pct))
    manager.task_finished.connect(
        lambda tid, ok, saved, detail:
        progress.update_status(tid, "done" if ok else "failed"))


def _run_convert(files, progress, result):
    """右键 → 转换：弹「转换设置」→ 入转换队列 → 直接开始。"""
    from .core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category
    from .core.queue import ConversionManager
    from .gui.convert_interface import ConvertInterface
    from .gui.convert_setup_dialog import ConvertSetupDialog

    valid_exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        log.warning("quick: no valid media files for convert")
        QCoreApplication.quit()
        return

    # 按类别分组，取第一个类别弹窗
    from collections import defaultdict
    cat_files: dict = defaultdict(list)
    for f in valid_files:
        cat = guess_category(f)
        if cat:
            cat_files[cat].append(f)
    if not cat_files:
        QCoreApplication.quit()
        return

    manager = ConversionManager()
    gpu = True
    if cfg.hardware.value == "cpu":
        gpu = False
    elif cfg.hardware.value == "auto":
        gpu = bool(manager.hw)

    _connect_convert_progress(manager, progress)

    # 全部完成退出
    def _idle():
        if not manager.is_running and not manager.tasks:
            QCoreApplication.quit()
    manager.state_changed.connect(_idle)

    cat = next(iter(cat_files))
    paths = cat_files[cat]
    dlg = ConvertSetupDialog(None, manager, paths, {}, lambda: gpu, cat)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

    orig_confirm = dlg._on_confirm
    def _confirm():
        orig_confirm()
        result["code"] = 0
        manager.start()
    dlg._on_confirm = _confirm

    # 取消（reject, result=0）也要退出；确认（accept, result=1）靠 _idle 退出
    dlg.finished.connect(
        lambda r: QCoreApplication.quit() if r == 0 else None)
    dlg.show()


def _run_compress(files, progress, result):
    """右键 → 压缩：弹「创建图片压缩任务」→ 入压缩队列 → 直接开始。"""
    from .core.presets import IMAGE_EXTS
    from .gui.compress_interface import CompressInterface
    from .gui.quick_dialogs import QuickCompressDialog

    valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
    if not valid_files:
        log.warning("quick: no valid image files for compress")
        QCoreApplication.quit()
        return

    iface = CompressInterface(None)
    total = {"n": 0, "done": 0}

    if progress:
        iface.taskAdded.connect(lambda iid, name: progress.add_task(iid, name))
        iface.taskProgress.connect(progress.update_progress)

    def _on_task_finished(iid, status):
        if progress:
            progress.update_status(iid, status)
        total["done"] += 1
        if total["done"] >= total["n"] > 0:
            QTimer.singleShot(800, QCoreApplication.quit)
    iface.taskFinished.connect(_on_task_finished)

    def _confirm(paths, settings):
        iface._program = settings["backend"]
        iface._output_mode = settings["output_mode"]
        iface._folder = settings["folder"]
        if settings.get("mode") == "lossy":
            if "jpegoptim" in iface._tool_opts:
                iface._tool_opts["jpegoptim"]["jo_mode"] = "lossy"
            if "pillow" in iface._tool_opts:
                iface._tool_opts["pillow"]["pil_quality"] = 85
        for f in paths:
            iface._add_item(f)
        total["n"] = len(paths)
        result["code"] = 0
        iface._on_start()

    dlg = QuickCompressDialog(None, valid_files, _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(
        lambda r: QCoreApplication.quit() if r == 0 else None)
    dlg.show()


def _run_upscale(files, progress, result):
    """右键 → 放大：弹「创建图片放大任务」→ 入放大队列 → 直接开始。"""
    from .core.presets import IMAGE_EXTS
    from .core import engines as eng_mod
    from .gui.upscale_interface import UpscaleInterface
    from .gui.quick_dialogs import QuickUpscaleDialog

    valid_exts = IMAGE_EXTS | eng_mod.ANIM_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        log.warning("quick: no valid files for upscale")
        QCoreApplication.quit()
        return
    if not eng_mod.installed_engines():
        log.error("quick: no upscale engine found")
        QCoreApplication.quit()
        return

    iface = UpscaleInterface(None)
    total = {"n": 0, "done": 0}

    if progress:
        iface.taskAdded.connect(lambda iid, name: progress.add_task(iid, name))
        iface.taskProgress.connect(progress.update_progress)

    def _on_task_finished(iid, status):
        if progress:
            progress.update_status(iid, status)
        total["done"] += 1
        if total["done"] >= total["n"] > 0:
            QTimer.singleShot(800, QCoreApplication.quit)
    iface.taskFinished.connect(_on_task_finished)

    def _confirm(paths, settings):
        iface._engine_id = settings["engine_id"]
        iface._fmt = settings["fmt"]
        iface._output_mode = settings["output_mode"]
        iface._folder = settings["folder"]
        iface._add_to_queue(paths)
        total["n"] = len(paths)
        result["code"] = 0
        iface._on_start()

    dlg = QuickUpscaleDialog(None, valid_files, _confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(
        lambda r: QCoreApplication.quit() if r == 0 else None)
    dlg.show()
