"""全应用统一的日志。

职责边界：
- 做：按日期把带时间戳的日志写入可执行文件旁的 logs/ 目录（开发期为仓库根），
  单文件超过 :data:`LOG_MAX_BYTES` 按体积滚动，只保留最近 7 天；
  提供 get_logger 工厂。
- 不做：不引入 Qt（保证任意模块可安全导入）；不负责日志的上报/展示。

依赖：core/platform（目录解析）；被依赖：全项目。
"""

from __future__ import annotations

import glob
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

# core.platform 只依赖标准库，这里引用它不会破坏 "logger 不引入 Qt" 的约束。
from .platform import log_dir as log_dir  # noqa: PLC0414  再导出，供测试与外部调用
from .platform import writable_base_dir

_LOG = logging.getLogger("momentshift")
_configured = False

# 单个日志文件的体积上限与滚动份数（V0.8.21 E2）。
# 为什么必须限：日志级别是 DEBUG，ffmpeg 的每行非进度输出都会落盘。批量转几百个
# 视频时，"按天一个文件 + 只清 7 天前的" 挡不住**当天**这一个文件涨到几百 MB ——
# 保留策略管的是天数，管不了单日体量。8 MB × 4 份 ≈ 32 MB 封顶，够排查最近一次
# 故障，也不会把用户磁盘吃掉。
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def app_root() -> Path:
    """应用根目录（``logs/`` 的父目录）。

    Returns:
        与 :func:`momentshift.core.platform.writable_base_dir` 相同的路径。

    Notes:
        v0.8.16 起指向**可写**根目录而非安装目录：安装到 Program Files 时
        日志会落到 ``%APPDATA%/MomentShift/logs``，否则仍在 exe 旁边。
    """
    return writable_base_dir()


def _cleanup_old_logs(days: int = 7) -> None:
    """尽力删除超过 ``days`` 天的旧日志文件。

    Args:
        days: 保留天数，修改时间早于此的 ``*.log`` 会被删除。
    Notes:
        单个文件删除失败不影响其余文件，整体失败也不向上抛——日志清理不该
        阻断应用启动。

        V0.8.21 E2：连 ``*.log.1`` 这类滚动备份一起清。只 glob ``*.log`` 的话，
        滚动出来的备份永远匹配不上，会在 logs/ 里一直堆着。
    """
    try:
        cutoff = time.time() - days * 86400
        d = log_dir()
        stale = glob.glob(str(d / "*.log")) + glob.glob(str(d / "*.log.*"))
        for f in stale:
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass  # 静默原因：过期日志文件删除失败非致命，下次启动再清
    except Exception:
        # 静默原因：日志系统自身初始化失败，已无可用 logger，无法继续记录
        print("日志系统初始化失败", file=sys.stderr)


def init_logging(level: int = logging.DEBUG) -> None:
    """初始化全局日志配置；重复调用安全，只有第一次真正生效。

    Args:
        level: 文件日志级别。控制台处理器固定只输出 WARNING 及以上，避免刷屏。
    Notes:
        日志目录创建与文件处理器挂载失败都不抛异常，仅退化为「只有控制台输出」
        ——日志系统本身不该成为应用启动的单点故障。
    """
    global _configured
    if _configured:
        return
    _configured = True

    d = log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:  # 静默原因：日志目录创建失败非致命，日志会回退到控制台
        pass

    _cleanup_old_logs(7)

    date = time.strftime("%Y-%m-%d")
    logfile = d / f"momentshift-{date}.log"
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    try:
        # V0.8.21 E2：换成按体积滚动。delay=True 让文件在第一条日志真正写出时
        # 才创建，避免「只是导入了模块」就在磁盘上留下一个空日志。
        fh = logging.handlers.RotatingFileHandler(
            logfile,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            errors="replace",
            delay=True,
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)
    except OSError:  # 静默原因：文件日志处理器添加失败，仍保留控制台输出
        pass

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    _LOG.addHandler(ch)

    _LOG.setLevel(level)
    _LOG.propagate = False
    _LOG.info("日志已初始化，日志文件：%s", logfile)


def get_logger(name: str = "momentshift") -> logging.Logger:
    """按名字取子 logger，首次调用时自动完成日志初始化。

    Args:
        name: 子 logger 名，通常用模块短名如 ``queue`` / ``ffmpeg``。
    Returns:
        对应的 :class:`logging.Logger`；传 ``momentshift`` 时返回根 logger。
    """
    if not _configured:
        init_logging()
    if name == "momentshift":
        return _LOG
    return _LOG.getChild(name)
