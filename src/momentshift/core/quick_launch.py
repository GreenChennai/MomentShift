"""快速调用模块 —— Windows 右键菜单集成。

职责边界：
- 做：读写 HKCU 下的注册表项以增删右键菜单、查询当前注册状态。
- 不做：不处理 --quick 命令行分支（交给 quick_runner）；不写 HKLM，避免要管理员权限。

依赖：core/logger、core/platform；被依赖：gui/quick_launch_interface、gui/setting_interface。

通过写入 HKCU\\Software\\Classes 下的注册表项，实现：
- 选中文件 → 右键 → 瞬变工坊 → 转换/压缩/放大
- 毫秒级响应：直接调用 exe --quick <task> <files>
- 无需管理员权限（HKCU 写入）
- 程序内设置总开关 + 功能独立开关

注册表路径::
    HKCU\\Software\\Classes\\*\\shell\\MomentShift.Convert\\
        command = "MomentShift.exe --quick convert %1"
    HKCU\\Software\\Classes\\*\\shell\\MomentShift.Compress\\
        command = "MomentShift.exe --quick compress %1"
"""

from __future__ import annotations

import os
import subprocess
import sys

from .logger import get_logger
from .platform import IS_WINDOWS, run_silent

log = get_logger("quick_launch")


def supported() -> bool:
    """快速调用（Windows 右键菜单）是否在本平台可用。

    注册表集成仅 Windows 支持；Linux / macOS 无对应机制，调用方应隐藏入口
    （界面显示占位说明，设置页不加载）。
    """
    return IS_WINDOWS

# 注册表根键（HKEY_CURRENT_USER，无需管理员权限）
_REG_ROOT = r"HKCU\Software\Classes"

# 支持的右键菜单功能定义
_TASKS: dict[str, dict] = {
    "convert": {
        "name": "MomentShift.Convert",
        "label": "瞬变工坊 — 转换",
        "icon": "",
    },
    "compress": {
        "name": "MomentShift.Compress",
        "label": "瞬变工坊 — 压缩",
        "icon": "",
    },
    "upscale": {
        "name": "MomentShift.Upscale",
        "label": "瞬变工坊 — 放大",
        "icon": "",
    },
}


def _exe_path() -> str:
    """返回当前 MomentShift.exe 的完整路径（冻结环境）或脚本路径（开发环境）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    # 开发环境：用 python -m momentshift
    return sys.executable


def _icon_value() -> str:
    """右键菜单条目前面的软件图标（REG_SZ ``Icon`` 值）。

    优先用随包图标文件（与软件 Logo 完全一致，``app_logo.ico``）；
    缺失时退回 EXE 内嵌图标资源（``"<exe>,0"``，build.spec 已把图标
    烧进 EXE）。返回带引号的字符串，兼容路径含空格的情况。
    """
    exe = _exe_path()
    # onedir 结构：exe 在 dist/<APP>/<APP>.exe，图标在
    # dist/<APP>/<APP>/resources/icons/app_logo.ico
    candidates = [
        os.path.join(os.path.dirname(exe), "momentshift", "resources", "icons", "app_logo.ico"),
        os.path.join(os.path.dirname(exe), "resources", "icons", "app_logo.ico"),
    ]
    for ico in candidates:
        if os.path.isfile(ico):
            return f'"{ico}"'
    return f'"{exe}",0'


def _command_for(task: str) -> str:
    """构造右键菜单命令：MomentShift.exe --quick <task> "%1"。

    v0.7.22 重构：%* 在含空格路径的 exe 命令中展开不稳定（实测多版本 files=0），
    回退 %1（单选 100% 可靠）。多选时 Shell 逐文件调用 exe，由已运行实例
    按时间窗口（1.2s）聚合同一批文件合并进一个设置窗。
    """
    exe = _exe_path()
    if getattr(sys, "frozen", False):
        return f'"{exe}" --quick {task} "%1"'
    else:
        # 开发环境：用 python -m momentshift
        return f'"{exe}" -m momentshift --quick {task} "%1"'


def register_context_menu(task: str) -> bool:
    """为指定功能注册 Windows 右键菜单项。

    Args:
        task: 功能名称 ("convert" / "compress" / "upscale")

    Returns:
        True 表示注册成功，False 表示失败。
    """
    if task not in _TASKS:
        log.warning("quick_launch: unknown task %s", task)
        return False

    if not supported():
        return False

    info = _TASKS[task]
    try:
        # 1. 创建菜单项（显示名）
        key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
        run_silent(
            ["reg", "add", key, "/ve", "/d", info["label"], "/f"],
            capture_output=True,
            text=True,
            check=True,
        )
        # 1.5 设置图标（v0.8.15：此前漏写，右键菜单条目前面缺少软件图标）
        run_silent(
            ["reg", "add", key, "/v", "Icon", "/d", _icon_value(), "/f"],
            capture_output=True,
            text=True,
            check=True,
        )
        # 2. 创建命令子键
        cmd_key = f"{key}\\command"
        cmd = _command_for(task)
        run_silent(
            ["reg", "add", cmd_key, "/ve", "/d", cmd, "/f"],
            capture_output=True,
            text=True,
            check=True,
        )
        log.info("quick_launch: registered %s → %s", task, info["name"])
        return True
    except subprocess.CalledProcessError as exc:
        log.error("quick_launch: failed to register %s: %s", task, exc.stderr)
        return False


def unregister_context_menu(task: str) -> bool:
    """移除指定功能的 Windows 右键菜单项。

    Args:
        task: 功能名称

    Returns:
        True 表示移除成功。
    """
    if task not in _TASKS:
        return False

    if not supported():
        return True

    info = _TASKS[task]
    try:
        key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
        run_silent(
            ["reg", "delete", key, "/f"],
            capture_output=True,
            text=True,
            check=True,
        )
        log.info("quick_launch: unregistered %s", task)
        return True
    except subprocess.CalledProcessError:
        # 键不存在 = 已经卸载，视为成功
        return True


def is_context_menu_registered(task: str) -> bool:
    """检查指定功能的右键菜单是否已注册。

    Args:
        task: 功能名称

    Returns:
        True 表示注册表中存在对应的菜单项。
    """
    if task not in _TASKS:
        return False
    if not supported():
        return False
    info = _TASKS[task]
    key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
    result = run_silent(["reg", "query", key], capture_output=True, text=True)
    return result.returncode == 0


def apply_all(enabled: bool, tasks_on: dict[str, bool]) -> dict[str, bool]:
    """根据开关状态批量注册/注销右键菜单。

    Args:
        enabled: 总开关（False 则注销全部）
        tasks_on: 各功能独立开关，如 {"convert": True, "compress": False}

    Returns:
        实际变更后的注册状态，如 {"convert": True, "compress": False}
    """
    result: dict[str, bool] = {}
    if not supported():
        return {t: False for t in _TASKS}
    for task in _TASKS:
        should_be_on = enabled and tasks_on.get(task, False)
        if should_be_on:
            # 注册失败（例如注册表被安全软件锁住）时如实返回 False。
            # 之前无论成败都回填"期望状态"，界面因此会显示"已开启"
            # 但右键菜单里其实什么都没有。
            result[task] = register_context_menu(task)
        else:
            # 注销失败通常只意味着键本来就不存在，两种情况都算"已关闭"。
            # 之前 unregister_context_menu 在这里被连续调用了两次。
            unregister_context_menu(task)
            result[task] = False
    return result


def available_tasks() -> list[str]:
    """返回所有可用的快速调用功能列表。"""
    return list(_TASKS.keys())
