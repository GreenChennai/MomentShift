"""Entry point: ``python -m momentshift`` or the installed ``momentshift`` CLI.

v0.2.9: ``--quick <task> <files...>`` mode for Windows right-click context menu.
v0.3.1: single-instance enforcement via QSharedMemory."""

import sys
import threading
import traceback
import os
from pathlib import Path

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
# 快速调用（v0.7.12：委托 quick_runner 执行完整 GUI 流程）
# =============================================================================
def _quick_launch_task(task: str, files: list[str]) -> int:
    """处理来自右键菜单的快速调用请求。

    v0.7.12 重构：弹设置窗 → 入对应队列 → 自动开始 → 右下角任务进度窗。
    """
    from momentshift.quick_runner import run_quick
    return run_quick(task, files)


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
