"""Entry point: ``python -m momentshift`` or the installed ``momentshift`` CLI.

v0.2.9: ``--quick <task> <files...>`` mode for Windows right-click context menu.
v0.3.1: single-instance enforcement via QSharedMemory."""

import sys
import threading
import traceback
import os

from momentshift.core.logger import init_logging, get_logger
from PyQt6.QtGui import QColor
from momentshift.core.qt_compat import QApplication, Qt
from PyQt6.QtCore import QSharedMemory
from PyQt6.QtWidgets import QMessageBox
from qfluentwidgets import setTheme, Theme, setThemeColor
from momentshift.core.config import cfg
from momentshift.i18n.translator import translator, LocaleKey
from momentshift.core.queue import ConversionManager
from momentshift.gui.main_window import MainWindow
from momentshift.metadata import APP_NAME, VERSION


# ---------------------------------------------------------------------------
# 单实例守卫（v0.3.1）：同一时刻只允许运行一个 MomentShift 进程
# ---------------------------------------------------------------------------
_instance_lock = QSharedMemory("MomentShift_SingleInstance_v031")
_IS_FIRST_INSTANCE = False

def _check_single_instance():
    """尝试获取共享内存锁。失败表示已有一个实例在运行。"""
    global _IS_FIRST_INSTANCE
    if _instance_lock.attach():
        # 已存在 → 不是第一个实例
        _IS_FIRST_INSTANCE = False
        return False
    if _instance_lock.create(1):
        _IS_FIRST_INSTANCE = True
        return True
    _IS_FIRST_INSTANCE = False
    return False


class Application(QApplication):
    """QApplication that logs (instead of crashing on) exceptions raised inside
    Qt event handlers / slots. This turns a silent "闪退" into a logged,
    diagnosable event written to ``logs/``.
    """

    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            get_logger("app").critical(
                "Exception in Qt event handler:\n" + traceback.format_exc()
            )
            return False


def _excepthook(exc_type, exc, tb):
    get_logger("app").critical(
        "Unhandled exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb))
    )


def _threading_excepthook(args):
    get_logger("app").critical(
        f"Unhandled exception in thread ({args.thread.name}):\n"
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


# =============================================================================
# 快速调用（无 GUI 模式，v0.2.9）
# =============================================================================
def _quick_launch_task(task: str, files: list[str]) -> int:
    """处理来自右键菜单的快速调用请求。

    v0.7.9：弹设置窗口 → 确认后入队并启动 → 右下角任务进度窗口。
    """
    from pathlib import Path
    from PyQt6.QtCore import QCoreApplication, QTimer
    from momentshift.gui.quick_setup_dialog import QuickCompressDialog, QuickUpscaleDialog
    from momentshift.gui.convert_setup_dialog import ConvertSetupDialog
    from momentshift.gui.task_progress_window import TaskProgressWindow

    log = get_logger("quick")
    log.info("Quick launch: task=%s files=%d first=%s", task, len(files),
             files[0] if files else "")

    app = Application(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    setTheme(Theme.LIGHT)
    setThemeColor(QColor("#238636"))
    translator.set_locale(LocaleKey(cfg.language.value))

    progress = None
    if cfg.quickLaunchProgressWindow.value:
        progress = TaskProgressWindow()
        progress.show()
    result = {"code": 1}

    def _run():
        if task == "convert":
            _quick_convert(files, progress, result)
        elif task == "compress":
            _quick_compress(files, progress, result)
        elif task == "upscale":
            _quick_upscale(files, progress, result)
        else:
            log.error("quick: unknown task %s", task)
            result["code"] = 1
            QCoreApplication.quit()

    QTimer.singleShot(0, _run)
    app.exec()
    log.info("quick: %s done, exit=%d", task, result["code"])
    return result["code"]


def _quick_convert(files, progress, result):
    """快速调用 — 转换：弹设置窗口 → 入队 → 启动。"""
    from momentshift.core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category
    from momentshift.gui.convert_setup_dialog import ConvertSetupDialog

    valid_exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
    if not valid_files:
        log.warning("quick: no valid media files for convert")
        result["code"] = 1
        QCoreApplication.quit()
        return

    # 按类别分组，用第一个文件的类别弹设置窗口
    from collections import defaultdict
    cat_files = defaultdict(list)
    for f in valid_files:
        cat = guess_category(f)
        if cat:
            cat_files[cat].append(f)
    if not cat_files:
        result["code"] = 1
        QCoreApplication.quit()
        return

    manager = ConversionManager()
    gpu = True
    if cfg.hardware.value == "cpu":
        gpu = False
    elif cfg.hardware.value == "auto":
        gpu = bool(manager.hw)

    def _on_confirm():
        manager.start()
        result["code"] = 0

    # 取第一个类别弹出设置窗口
    cat = list(cat_files.keys())[0]
    cat_files_list = cat_files[cat]

    # 注册进度窗口信号
    def _on_task_added(task_obj):
        if progress:
            progress.add_task(task_obj.id, Path(task_obj.input_path).name)
    manager.task_added.connect(_on_task_added)

    def _on_progress(tid, pct):
        if progress:
            progress.update_progress(tid, pct)
    manager.progress_updated.connect(_on_progress)

    def _on_task_finished(tid, ok, saved, detail):
        if progress:
            progress.update_status(tid, "done" if ok else "failed")
    manager.task_finished.connect(_on_task_finished)

    def _quit_when_idle():
        if not manager.is_running and not manager.tasks:
            QCoreApplication.quit()
    manager.state_changed.connect(_quit_when_idle)

    # 弹出设置窗口
    selection = {cat: "jpg"}
    dlg = ConvertSetupDialog(None, manager, cat_files_list, selection,
                             lambda: gpu, cat)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.show()
    # 确认后自动启动
    orig = dlg._on_confirm
    def _wrapped():
        orig()
        _on_confirm()
    dlg._on_confirm = _wrapped
    # 用户点取消也退出
    dlg.finished.connect(lambda: QCoreApplication.quit())


def _quick_compress(files, progress, result):
    """快速调用 — 压缩：弹设置窗口 → 直接调用 compressor 压缩 → 进度窗口。"""
    from momentshift.core.presets import IMAGE_EXTS
    from momentshift.core import compressor

    valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
    if not valid_files:
        log.warning("quick: no valid image files for compress")
        result["code"] = 1
        QCoreApplication.quit()
        return

    from momentshift.gui.quick_setup_dialog import QuickCompressDialog

    def _on_confirm(files, quality, backend, mode, folder):
        total = len(files)
        completed = {"count": 0}
        errors = []

        # 注册进度
        if progress:
            for f in files:
                progress.add_task(f, Path(f).name)

        def _compress_one(idx, src):
            p = Path(src)
            out_dir = Path(folder) if mode != "same" and folder else p.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / (p.stem + "_compressed" + p.suffix)
            i = 1
            while out.exists():
                out = out_dir / f"{p.stem}_compressed_{i}{p.suffix}"
                i += 1
            try:
                ok, _, _ = compressor.compress_auto(src, str(out), "lossless", 100, {})
                if not ok:
                    errors.append(src)
                if progress:
                    progress.update_status(src, "done" if ok else "failed")
            except Exception as exc:
                log.error("quick: compress error %s: %s", src, exc)
                errors.append(src)
                if progress:
                    progress.update_status(src, "failed")
            completed["count"] += 1
            if progress:
                progress.update_progress(src, int(completed["count"] / total * 100))
            if completed["count"] >= total:
                result["code"] = 0 if not errors else 1
                QCoreApplication.quit()

        for i, f in enumerate(valid_files):
            QTimer.singleShot(i * 80, lambda f=f: _compress_one(None, f))

    dlg = QuickCompressDialog(None, valid_files, _on_confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(lambda r: (result.update(code=1) if r == 0 else None,
                                    QCoreApplication.quit()))
    dlg.show()


def _quick_upscale(files, progress, result):
    """快速调用 — 放大：弹设置窗口 → 直接调用 engine → 进度窗口。"""
    from momentshift.core.presets import IMAGE_EXTS, ANIM_EXTS
    from momentshift.core import engines as eng_mod

    upscale_exts = IMAGE_EXTS | ANIM_EXTS
    valid_files = [f for f in files if Path(f).suffix.lower() in upscale_exts]
    if not valid_files:
        log.warning("quick: no valid files for upscale")
        result["code"] = 1
        QCoreApplication.quit()
        return

    installed = eng_mod.installed_engines()
    if not installed:
        log.error("quick: no upscale engine found")
        result["code"] = 1
        QCoreApplication.quit()
        return

    from momentshift.gui.quick_setup_dialog import QuickUpscaleDialog

    def _on_confirm(files, engine_id, fmt, mode, folder):
        total = len(files)
        completed = {"count": 0}
        errors = []

        if progress:
            for f in files:
                progress.add_task(f, Path(f).name)

        def _upscale_one(idx, src):
            p = Path(src)
            out_dir = Path(folder) if mode != "same" and folder else p.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = "." + fmt if fmt else ".png"
            out = out_dir / (p.stem + "_upscaled" + ext)
            i = 1
            while out.exists():
                out = out_dir / f"{p.stem}_upscaled_{i}{ext}"
                i += 1
            try:
                ok, _ = eng_mod.process_media(engine_id, src, str(out), {})
                if not ok:
                    errors.append(src)
                if progress:
                    progress.update_status(src, "done" if ok else "failed")
            except Exception as exc:
                log.error("quick: upscale error %s: %s", src, exc)
                errors.append(src)
                if progress:
                    progress.update_status(src, "failed")
            completed["count"] += 1
            if progress:
                progress.update_progress(src, int(completed["count"] / total * 100))
            if completed["count"] >= total:
                result["code"] = 0 if not errors else 1
                QCoreApplication.quit()

        for i, f in enumerate(valid_files):
            QTimer.singleShot(i * 80, lambda f=f: _upscale_one(None, f))

    dlg = QuickUpscaleDialog(None, valid_files, _on_confirm)
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.finished.connect(lambda r: (result.update(code=1) if r == 0 else None,
                                    QCoreApplication.quit()))
    dlg.show()


def main():
    init_logging()
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook

    log = get_logger("app")
    log.info("Starting %s %s (pid=%d)", APP_NAME, VERSION, os.getpid())

    # =========================================================================
    # 快速调用模式（v0.2.9）：无 GUI，右键菜单触发
    #   MomentShift.exe --quick convert file1.png file2.jpg
    # =========================================================================
    if "--quick" in sys.argv:
        idx = sys.argv.index("--quick")
        if idx + 2 >= len(sys.argv):
            log.error("Usage: MomentShift --quick <task> <file1> [file2 ...]")
            sys.exit(1)
        task = sys.argv[idx + 1]
        files = sys.argv[idx + 2:]
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        sys.exit(_quick_launch_task(task, files))

    # =========================================================================
    # 单实例检查（v0.3.1）：禁止重复启动
    # =========================================================================
    if not _check_single_instance():
        log.warning("Another instance is running (pid=%d), exiting.", os.getpid())
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(
            None, APP_NAME,
            "MomentShift 已在运行。\n\n如需重新打开，请从系统托盘唤起或退出后再启动。")
        sys.exit(0)

    # =========================================================================
    # 正常 GUI 模式
    # =========================================================================
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # AA_UseHighDpiPixmaps was removed in Qt6 (high-DPI pixmaps are automatic),
    # so guard it to stay compatible across PyQt6 versions.
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = Application(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)

    # =========================================================================
    # 字体加载（v0.3.2）：简体中文 → HarmonyOS Sans SC，英文/数字 → FiraCode
    # =========================================================================
    from PyQt6.QtGui import QFont, QFontDatabase
    from pathlib import Path
    _res = Path(__file__).parent / "resources"
    for _name in ("HarmonyOS_Sans_SC_Regular.ttf", "FiraCode-Regular.ttf"):
        _fp = _res / _name
        if _fp.exists():
            _fid = QFontDatabase.addApplicationFont(str(_fp))
            log.info("Loaded font: %s (id=%s)", _name, _fid)
    # 设置默认字体：中文用 HarmonyOS Sans SC，fallback 用 FiraCode
    _qfont = QFont("HarmonyOS Sans SC", 10)
    _qfont.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(_qfont)
    # 全局样式表强制字体 — 覆盖 qfluentwidgets 等库的内部字体设置
    # v0.7.3 调整2：软件已取消全部鼠标悬停提示，原 QToolTip 配色规则一并移除。
    app.setStyleSheet(
        "* { font-family: 'HarmonyOS Sans SC', 'FiraCode', 'Microsoft YaHei', sans-serif; }"
    )

    # v0.3.2: 固定浅色主题，移除深色主题
    setTheme(Theme.LIGHT)
    setThemeColor(QColor("#238636"))   # GitHub 绿
    translator.set_locale(LocaleKey(cfg.language.value))

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
