"""ffmpeg 发现、版本与能力探测。

职责边界：
- 做：按优先级在可执行文件旁与 PATH 中查找 ffmpeg/ffprobe，探测版本与可用编码器。
- 不做：不下载 ffmpeg（见 core/ffmpeg_download）；不执行转码。

依赖：core/platform；被依赖：全项目。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from .platform import run_silent


def _binary_names() -> tuple[str, str]:
    """返回当前平台下 ``(ffmpeg, ffprobe)`` 的可执行文件名。"""
    if sys.platform == "win32":
        return "ffmpeg.exe", "ffprobe.exe"
    return "ffmpeg", "ffprobe"


def bundled_locations() -> list[Path]:
    """返回可能存放本地 ffmpeg/ffprobe 的目录，按搜索优先级排列。

    Returns:
        打包运行时只含安装根目录（可执行文件旁）；开发运行时依次是仓库的
        ``tools/ffmpeg_bin`` 与包旁的 ``ffmpeg_bin``。
    """
    locations: list[Path] = []
    if getattr(sys, "frozen", False):
        # PyInstaller 单目录打包：二进制与主程序同级
        locations.append(Path(sys.executable).parent)
    else:
        here = Path(__file__).resolve()
        # 仓库根在四层之上：core/ffmpeg.py -> momentshift/core -> momentshift -> src
        repo_root = here.parents[3]
        locations.append(repo_root / "tools" / "ffmpeg_bin")
        # 再兜一个包旁的同名目录，方便开发时随手放二进制
        locations.append(here.parent.parent / "ffmpeg_bin")
    return locations


def ffmpeg_install_dir() -> Path:
    """返回一键下载应当把 ffmpeg/ffprobe 放到的目录。

    Notes:
        取的是 :func:`find_ffmpeg` 搜索链的第一站，因此下载完不必额外注册路径，
        下次检测会自动发现。
    """
    return bundled_locations()[0]


def find_ffmpeg(mode: str = "auto") -> str | None:
    """定位 ffmpeg 可执行文件。

    Args:
        mode: ``auto`` 先找安装根目录再找 ``PATH``；``path`` 只搜索系统 ``PATH``。
    Returns:
        绝对路径；都没找到返回 ``None``。
    """
    ffmpeg_name, _ = _binary_names()
    local = [p / ffmpeg_name for p in bundled_locations()]

    if mode == "path":
        return shutil.which(ffmpeg_name)

    # auto：随包附带的优先于系统装的，避免用户环境里的老版本 ffmpeg 缺编码器
    for p in local:
        if p.exists():
            return str(p)
    return shutil.which(ffmpeg_name)


def find_ffprobe() -> str | None:
    """定位 ffprobe 可执行文件，搜索策略与 :func:`find_ffmpeg` 的 auto 模式一致。"""
    _, ffprobe_name = _binary_names()
    bundled = [p / ffprobe_name for p in bundled_locations()]
    for p in bundled:
        if p.exists():
            return str(p)
    return shutil.which(ffprobe_name)


def get_version(ffmpeg_path: str) -> str | None:
    """返回 ``ffmpeg -version`` 输出的首行版本串，失败返回 ``None``。"""
    try:
        proc = run_silent(
            [ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return proc.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def get_encoders(ffmpeg_path: str) -> set[str]:
    """解析 ``ffmpeg -encoders`` 输出，返回所有编码器名称的集合。

    Returns:
        编码器名集合；ffmpeg 不可用或调用超时时返回空集合，交由调用方降级。
    """
    encoders: set[str] = set()
    try:
        proc = run_silent(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return encoders

    pattern = re.compile(r"^\s*[VAS]\.[.\w]*\s+(\S+)")
    for line in proc.stdout.splitlines():
        match = pattern.match(line)
        if match:
            encoders.add(match.group(1))
    return encoders


def get_hwaccels(ffmpeg_path: str) -> set[str]:
    """解析 ``ffmpeg -hwaccels`` 输出，返回所有硬件加速方式的集合。

    Returns:
        方式名集合；ffmpeg 不可用或调用超时时返回空集合，视为「无硬件加速」。
    """
    accels: set[str] = set()
    try:
        proc = run_silent(
            [ffmpeg_path, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return accels

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Hardware") or set(line) <= set("- "):
            continue
        accels.add(line)
    return accels
