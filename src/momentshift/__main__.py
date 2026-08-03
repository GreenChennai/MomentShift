"""程序入口：``python -m momentshift`` 或安装后的 ``momentshift`` 命令。

职责边界：
- 做：解析命令行参数、建立单实例锁（QSharedMemory）、安装全局异常钩子、拉起主窗口；
  支持 ``--quick <task> <files...>`` 右键菜单快速调用模式。
- 不做：任何具体的转码/压缩/放大业务逻辑（交给对应界面与核心模块）。

依赖：core/platform、gui/main_window；被依赖：无（进程唯一入口）。
"""

import os
import sys
import threading
import traceback

from PyQt6.QtCore import QSharedMemory
from PyQt6.QtWidgets import QMessageBox

from momentshift.app_bootstrap import create_application
from momentshift.core.logger import get_logger, init_logging
from momentshift.core.qt_compat import QApplication
from momentshift.core.queue import ConversionManager
from momentshift.gui.main_window import MainWindow
from momentshift.metadata import APP_NAME, VERSION

# ---------------------------------------------------------------------------
# 单实例守卫：同一时刻只允许运行一个 MomentShift 进程
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
    """记录 Qt 事件处理器 / 槽函数内异常的 QApplication。

    为什么要覆写 notify：Qt 槽里抛出的异常默认会直接终止进程，用户看到的
    只是「闪退」，事后毫无线索。这里兜住异常并写入 ``logs/``，把闪退变成
    一条可排查的日志。

    Notes:
        这是最外层兜底，必须捕获 Exception 而非具体异常类型：
        任何一个界面回调出错都不应该带走整个程序。
    """

    def notify(self, receiver, event):
        """转发事件，并兜住槽函数抛出的任何异常。"""
        try:
            return super().notify(receiver, event)
        except Exception:
            get_logger("app").critical("Qt 事件处理器内发生异常：\n" + traceback.format_exc())
            return False


def _excepthook(exc_type, exc, tb):
    """主线程未捕获异常的兜底钩子，只记录不退出。"""
    get_logger("app").critical(
        "未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb))
    )


def _threading_excepthook(args):
    """子线程未捕获异常的兜底钩子。

    Notes:
        threading.excepthook 与 sys.excepthook 是两套机制，
        只装后者的话子线程崩溃仍然静默无声。
    """
    get_logger("app").critical(
        f"线程 {args.thread.name} 内发生未捕获异常：\n"
        + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )


# =============================================================================
# 快速调用（：委托 quick_runner 执行完整 GUI 流程）
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
    # 快速调用模式：无 GUI，右键菜单触发
    # MomentShift.exe --quick convert file1.png file2.jpg
    # =========================================================================
    if "--quick" in sys.argv:
        idx = sys.argv.index("--quick")
        task = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        # 过滤空串/纯空白参数（%* 展开异常时的防御）
        files = [f for f in sys.argv[idx + 2 :] if f and f.strip()]
        # 即使 files 为空也不直接退出（旧版 `"%*"` 注册命令展开异常会
        # 导致无参数）——交给 run_quick 静默处理，避免右键"没反应"。
        # HiDPI 策略由 quick_runner 内的 app_bootstrap.create_application 统一设置。
        sys.exit(_quick_launch_task(task, files))

    # =========================================================================
    # 单实例检查：禁止重复启动
    # =========================================================================
    if not _check_single_instance():
        log.warning("Another instance is running (pid=%d), exiting.", os.getpid())
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(
            None,
            APP_NAME,
            "MomentShift 已在运行。\n\n如需重新打开，请从系统托盘唤起或退出后再启动。",
        )
        sys.exit(0)

    # =========================================================================
    # 正常 GUI 模式
    # =========================================================================
    # HiDPI / 字体 / 主题 / 语言 / qfluentwidgets 补丁全部由 app_bootstrap 负责，
    # 与 quick_runner 共用同一份实现，避免两条启动路径的初始化再次漂移。
    app = create_application(sys.argv, app_cls=Application)

    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
