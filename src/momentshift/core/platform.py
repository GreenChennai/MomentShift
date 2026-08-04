"""平台相关常量与路径解析，全项目唯一的子进程静默标志与目录定位来源。

职责边界：
- 做：定义 Windows 子进程静默标志、解析应用各级目录、封装静默子进程调用。
- 不做：不读写配置、不创建 Qt 对象、不写日志文件（避免与 logger 循环依赖）。

依赖：仅标准库；被依赖：core 全部模块与 gui 中需要起子进程的模块。

历史背景：v0.8.0 之前 ``CREATE_NO_WINDOW`` 在 12 处被重复定义或内联，
"应用根目录" 有 3 种写法散落 6 处，``tools_dir()`` 存在两份实现。
任何一处遗漏都会在 Windows 上弹出黑色控制台窗口，因此收敛到本模块。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# Windows 下抑制子进程控制台窗口；其他平台该常量不存在，取 0 表示"无额外标志"。
WIN_SILENT: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 当前平台标识（多平台兼容的唯一事实源，core/ 与 gui/ 一律从这里取，
# 不再散落 sys.platform 判断）。v0.8.x 起支持从源码在 Linux / macOS 直接运行。
IS_WINDOWS: bool = sys.platform == "win32"
IS_MACOS: bool = sys.platform == "darwin"
IS_LINUX: bool = sys.platform.startswith("linux")


def platform_tag() -> str:
    """返回当前平台的简短标签：``"win"`` / ``"mac"`` / ``"linux"``。

    用于下载源按平台筛选、i18n 文案分支等场景。
    """
    if IS_WINDOWS:
        return "win"
    if IS_MACOS:
        return "mac"
    return "linux"


def binary_name(stem: str) -> str:
    """返回带平台正确后缀的可执行文件名。

    Windows 上可执行文件带 ``.exe`` 后缀；Linux / macOS 上没有后缀。

    Args:
        stem: 二进制名干，例如 ``"oxipng"``。
    Returns:
        Windows 返回 ``"oxipng.exe"``，其他平台返回 ``"oxipng"``。
    """
    return f"{stem}.exe" if IS_WINDOWS else stem


def strip_exe_suffix(name: str) -> str:
    """去掉文件名末尾的 ``.exe``（大小写不敏感），无后缀则原样返回。

    用于把 ``"oxipng.exe"`` 与 ``"oxipng"`` 统一成同一个 stem 再按平台拼回。
    """
    return name[:-4] if name.lower().endswith(".exe") else name


# 本文件位于 src/momentshift/core/platform.py，开发态下仓库根在第 3 层父目录。
_DEV_REPO_ROOT_DEPTH = 3


def app_base_dir() -> Path:
    """应用根目录。

    Returns:
        frozen（PyInstaller 打包）时返回 exe 所在目录；开发态返回仓库根目录。

    Notes:
        这是 ``config.json`` / ``logs/`` / ``tools/`` 的共同父目录，
        改动目录层级只需要改这一处。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[_DEV_REPO_ROOT_DEPTH]


def resources_dir() -> Path:
    """内置资源目录（打包后随 exe 分发的 oxipng / jpegoptim / 字体等）。

    Returns:
        资源目录路径。目录不一定存在，调用方需自行判断。

    Notes:
        PyInstaller >= 6 的 onedir 构建把包收集到 ``_internal/`` 子目录，
        因此需要同时探测新旧两种布局，否则发布版找不到内置二进制。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates = [
            base / "_internal" / "momentshift" / "resources",
            base / "momentshift" / "resources",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[0]
    # 开发态：src/momentshift/core/platform.py -> src/momentshift/resources
    return Path(__file__).resolve().parent.parent / "resources"


def tools_dir() -> Path:
    """外部工具目录 ``<app_base_dir>/tools``（用户下载的引擎与压缩器落在这里）。

    Returns:
        工具目录路径，已确保存在。

    Raises:
        OSError: 目录无法创建（例如安装在只读的 Program Files 下）。
            这里刻意向上抛而不是静默吞掉，让调用方决定如何提示用户。
    """
    directory = app_base_dir() / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_file() -> Path:
    """配置文件路径 ``<app_base_dir>/config.json``（不保证存在）。"""
    return app_base_dir() / "config.json"


def log_dir() -> Path:
    """日志目录 ``<app_base_dir>/logs``（不自动创建，由 logger 负责）。"""
    return app_base_dir() / "logs"


def _with_silent_defaults(kwargs: dict) -> dict:
    """为子进程调用补齐项目统一的默认参数。

    Args:
        kwargs: 调用方传入的原始关键字参数。
    Returns:
        补齐后的新字典。调用方显式给出的值一律优先，不做覆盖。
    """
    merged = dict(kwargs)
    # creationflags 用按位或合并：调用方可能还需要 DETACHED_PROCESS 之类的标志。
    merged["creationflags"] = merged.get("creationflags", 0) | WIN_SILENT
    # 只有在明确要文本模式时才注入编码，否则会和 capture 二进制输出的场景打架。
    if merged.get("text") or merged.get("universal_newlines"):
        merged.setdefault("encoding", "utf-8")
        merged.setdefault("errors", "replace")
    return merged


def run_silent(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` 的项目统一封装：自动带静默标志。

    Args:
        cmd: 完整命令行序列。
        **kwargs: 透传给 ``subprocess.run``；``creationflags`` 会与
            :data:`WIN_SILENT` 做按位或而非覆盖。
    Returns:
        ``subprocess.CompletedProcess``。
    Notes:
        项目内禁止直接调用 ``subprocess.run``，否则 Windows 上会弹黑框。
    """
    return subprocess.run(list(cmd), **_with_silent_defaults(kwargs))  # noqa: S603


def popen_silent(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` 的项目统一封装，语义同 :func:`run_silent`。

    Args:
        cmd: 完整命令行序列。
        **kwargs: 透传给 ``subprocess.Popen``。
    Returns:
        ``subprocess.Popen``。调用方应使用 ``with`` 语句，
        否则管道句柄要等 GC 才回收，批量取消时会短时堆积僵尸进程。
    """
    return subprocess.Popen(list(cmd), **_with_silent_defaults(kwargs))  # noqa: S603
