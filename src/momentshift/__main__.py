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

    使用 QCoreApplication 事件循环驱动，不使用 MainWindow。
    所有输出文件默认保存到源文件同目录。
    """
    from pathlib import Path
    from PyQt6.QtCore import QCoreApplication, QTimer
    from momentshift.core.qt_compat import QCoreApplication as QA

    log = get_logger("quick")
    log.info("Quick launch: task=%s files=%d first=%s", task, len(files),
             files[0] if files else "")

    app = QA.instance() or QA(sys.argv)

    if task == "convert":
        from momentshift.core.presets import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS, guess_category

        valid_exts = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
        valid_files = [f for f in files if Path(f).suffix.lower() in valid_exts]
        if not valid_files:
            log.warning("quick: no valid media files for convert")
            return 1
        manager = ConversionManager()
        gpu = True
        if cfg.hardware.value == "cpu":
            gpu = False
        elif cfg.hardware.value == "auto":
            gpu = bool(manager.hw)
        manager.add_files(valid_files, None, None, gpu, "same", "_converted")
        # 任务队列空闲时退出事件循环
        def _quit_when_idle():
            if not manager.is_running and not manager.tasks:
                QCoreApplication.quit()
        manager.state_changed.connect(_quit_when_idle)
        manager.start()
        app.exec()
        log.info("quick: convert done")
        return 0

    elif task == "compress":
        from momentshift.core import compressor
        from momentshift.core.presets import IMAGE_EXTS

        valid_files = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
        if not valid_files:
            log.warning("quick: no valid image files for compress")
            return 1
        total = len(valid_files)
        completed = {"count": 0}
        errors = []
        def _compress(idx, src):
            p = Path(src)
            out = p.parent / (p.stem + "_compressed" + p.suffix)
            i = 1
            while out.exists():
                out = p.parent / f"{p.stem}_compressed_{i}{p.suffix}"
                i += 1
            try:
                ok, _, _ = compressor.compress_auto(src, str(out), "lossless", 100, {})
                if not ok:
                    errors.append(src)
            except Exception as exc:
                log.error("quick: compress error %s: %s", src, exc)
                errors.append(src)
            completed["count"] += 1
            if completed["count"] >= total:
                QCoreApplication.quit()
        for i, f in enumerate(valid_files):
            QTimer.singleShot(i * 50, lambda f=f: _compress(None, f))
        app.exec()
        log.info("quick: compress done, %d/%d ok", total - len(errors), total)
        return 0 if not errors else 1

    elif task == "upscale":
        from momentshift.core import upscaler

        upscale_exts = upscaler.IMAGE_EXTS | upscaler.ANIM_EXTS
        valid_files = [f for f in files if Path(f).suffix.lower() in upscale_exts]
        if not valid_files:
            log.warning("quick: no valid files for upscale")
            return 1
        if not upscaler.find_upscaler():
            log.error("quick: upscaler engine not found")
            return 1
        total = len(valid_files)
        completed = {"count": 0}
        errors = []
        def _upscale(src):
            p = Path(src)
            out = p.parent / (p.stem + "_upscaled.png")
            i = 1
            while out.exists():
                out = p.parent / f"{p.stem}_upscaled_{i}.png"
                i += 1
            try:
                ok, _ = upscaler.upscale_media(src, str(out), "realesrgan-x4plus", 4, 0, "auto")
                if not ok:
                    errors.append(src)
            except Exception as exc:
                log.error("quick: upscale error %s: %s", src, exc)
                errors.append(src)
            completed["count"] += 1
            if completed["count"] >= total:
                QCoreApplication.quit()
        for i, f in enumerate(valid_files):
            QTimer.singleShot(i * 50, lambda f=f: _upscale(f))
        app.exec()
        log.info("quick: upscale done, %d/%d ok", total - len(errors), total)
        return 0 if not errors else 1

    else:
        log.error("quick: unknown task %s", task)
        return 1


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

    # Apply saved preferences before the window appears.
    theme_map = {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}
    setTheme(theme_map.get(cfg.theme.value, Theme.AUTO))
    # v0.2.5: unify the global accent colour to GitHub green (#238636). This
    # recolours every qfluentwidgets primary element (PrimaryPushButton,
    # SwitchButton, ComboBox selection, ...) — replacing the old teal default
    # (#009FAA) and its derived variants in one call.
    setThemeColor(QColor("#238636"))
    translator.set_locale(LocaleKey(cfg.language.value))

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
