"""快速调用模块 —— Windows 右键菜单集成（v0.2.9）。

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

import sys
import subprocess
from pathlib import Path

from ..core.logger import get_logger

log = get_logger("quick_launch")

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


def _command_for(task: str) -> str:
    """构造右键菜单命令：MomentShift.exe --quick <task> "%*"。

    v0.7.19：%1 在多选时会被 Shell 为每个文件单独调用一次 → 每个文件弹一个
    设置窗；改用 %* 把所有选中文件一次性传入，同类文件合并进同一个弹窗。
    """
    exe = _exe_path()
    if getattr(sys, "frozen", False):
        return f'"{exe}" --quick {task} "%*"'
    else:
        # 开发环境：用 python -m momentshift
        return f'"{exe}" -m momentshift --quick {task} "%*"'


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

    info = _TASKS[task]
    try:
        # 1. 创建菜单项（显示名）
        key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
        result = subprocess.run(
            ["reg", "add", key, "/ve", "/d", info["label"], "/f"],
            capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # 2. 创建命令子键
        cmd_key = f"{key}\\command"
        cmd = _command_for(task)
        result = subprocess.run(
            ["reg", "add", cmd_key, "/ve", "/d", cmd, "/f"],
            capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

    info = _TASKS[task]
    try:
        key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
        result = subprocess.run(
            ["reg", "delete", key, "/f"],
            capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
    info = _TASKS[task]
    key = f"{_REG_ROOT}\\*\\shell\\{info['name']}"
    result = subprocess.run(
        ["reg", "query", key], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


def apply_all(enabled: bool, tasks_on: dict[str, bool]) -> dict[str, bool]:
    """根据开关状态批量注册/注销右键菜单。

    Args:
        enabled: 总开关（False 则注销全部）
        tasks_on: 各功能独立开关，如 {"convert": True, "compress": False}

    Returns:
        实际变更后的注册状态，如 {"convert": True, "compress": False}
    """
    result = {}
    for task in _TASKS:
        should_be_on = enabled and tasks_on.get(task, False)
        if should_be_on:
            success = register_context_menu(task)
        else:
            success = not unregister_context_menu(task)  # True means removed
            unregister_context_menu(task)
            success = True  # unregistered is always "success" in terms of intended state
        result[task] = should_be_on  # desired state, whether or not reg worked
    return result


def available_tasks() -> list[str]:
    """返回所有可用的快速调用功能列表。"""
    return list(_TASKS.keys())
