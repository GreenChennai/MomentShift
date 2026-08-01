"""快速调用执行器（v0.7.13 修复）。

独立进程模式（``--quick <task> <files>``）：
1. 创建 GUI app + 主题/字体
2. 弹出设置窗口（转换=ConvertSetupDialog；压缩/放大=quick_dialogs）
3. 确认后实例化对应大模块队列界面、入队并自动开始任务
4. 右下角任务进度窗显示完成情况（设置可关）
5. 全部任务完成后自动退出

v0.7.13 修复：
- 转换确认改用 finished 信号驱动 start（属性替换对已绑定信号无效）
- 压缩/放大延迟到确认回调才构造队列界面，构造失败弹窗提示
- 资源目录路径修正（resources 与 quick_runner 同目录）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QCoreApplication, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QMessageBox
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
    # v0.7.14：窗口关闭不退出事件循环，否则设置弹窗 accept 后进程直接退出，
    # 后台 worker（ffmpeg/压缩/放大）被连带终止 → WorkerSignals has been deleted
    app.setQuitOnLastWindowClosed(False)

    # 字体（与主窗口一致）—— v0.7.13：resources 与 quick_runner 同目录
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


def _fatal(msg: str) -> None:
    """可见错误提示 + 日志 + 退出。"""
    log.critical("quick fatal: %s", msg)
    try:
        QMessageBox.critical(None, APP_NAME, msg)
    except Exception:
        pass
    QCoreApplication.quit()


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
            tb = traceback.format_exc()
            log.critical("quick dispatch failed:\n%s", tb)
            try:
                QMessageBox.critical(
                    None, APP_NAME,
                    f"快速调用失败：\n{tb[-1500:]}")
            except Exception:
                pass
            QCoreApplication.quit()

    QTimer.singleShot(0, _dispatch)
    app.exec()
    log.info("quick: %s done, exit=%d", task, result["code"])
    return result["code"]


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


# ---------------------------------------------------------------------------
def _run_convert(files, progress, result):
    """右键 → 转换：弹「转换设置」→ 入转换队列 → 直接开始。"""
    from .core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category
    from .core.queue import ConversionManager
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

    manager = ConversionManager()
    gpu = True
    if cfg.hardware.value == "cpu":
        gpu = False
    elif cfg.hardware.value == "auto":
        gpu = bool(manager.hw)

    _connect_convert_progress(manager, progress)

    def _idle():
        if not manager.is_running and not manager.tasks:
            QCoreApplication.quit()
    manager.state_changed.connect(_idle)

    cat = next(iter(cat_files))
    paths = cat_files[cat]
    dlg = ConvertSetupDialog(None, manager, paths, {}, lambda: gpu, cat)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

    # v0.7.13：finished 信号驱动 start（属性替换对已绑定信号无效）
    # accept→result=1（已 add_files）→ start；reject→result=0 → 退出
    def _on_dialog_finished(r: int):
        if r == 1:
            result["code"] = 0
            manager.start()
        else:
            QCoreApplication.quit()
    dlg.finished.connect(_on_dialog_finished)
    _KEEP_ALIVE.append(dlg)
    dlg.show()


def _run_compress(files, progress, result):
    """右键 → 压缩：弹「创建图片压缩任务」→ 入压缩队列 → 直接开始。"""
    from .core.presets import IMAGE_EXTS
    from .gui.compress_interface import CompressInterface
    from .gui.quick_dialogs import QuickCompressDialog

    valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
    if not valid_files:
        _fatal("没有找到可压缩的图片文件。")
        return

    total = {"n": 0, "done": 0}

    def _confirm(paths, settings):
        # v0.7.13：确认后才构造队列界面，失败弹窗
        try:
            iface = CompressInterface(None)
        except Exception:
            import traceback
            log.critical("compress interface failed:\n%s", traceback.format_exc())
            _fatal("压缩队列初始化失败，请从主窗口「压缩」页重试。")
            return

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
    # accept→确认回调已启动任务；reject→退出
    dlg.finished.connect(
        lambda r: QCoreApplication.quit() if r == 0 else None)
    _KEEP_ALIVE.append(dlg)
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
        _fatal("没有找到可放大的图片文件。")
        return
    if not eng_mod.installed_engines():
        _fatal("尚未安装任何放大引擎，请先到「关于」页下载引擎。")
        return

    total = {"n": 0, "done": 0}

    def _confirm(paths, settings):
        try:
            iface = UpscaleInterface(None)
        except Exception:
            import traceback
            log.critical("upscale interface failed:\n%s", traceback.format_exc())
            _fatal("放大队列初始化失败，请从主窗口「放大」页重试。")
            return

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
    _KEEP_ALIVE.append(dlg)
    dlg.show()
