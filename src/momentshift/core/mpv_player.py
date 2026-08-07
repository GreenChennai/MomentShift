"""python-mpv 封装：libmpv 定位 + 嵌入渲染面（v0.8.27 新增）。

背景：对比窗口先后试过 FFmpeg 抽帧（性能低、画质不准）、QMediaPlayer
（V0.8.26，分割对比失效——QVideoWidget 是独立原生窗口，父容器 ``setMask``
裁剪不到它）、ffplay（交互式播放器，无程序化控制 API）。最终引入
python-mpv（KDE 官方 mpvqt 的 Python 绑定，底层 libmpv，画质准确、性能好、
控制 API 齐全：进度 / 暂停 / 帧步进 / 循环）。

关键设计：
- ``MpvSurface`` 是普通 ``QWidget`` 容器，把 ``winId()`` 交给 mpv 的 ``--wid``，
  mpv **直接渲染进这个 hwnd**（没有子原生窗口）——因此父容器 ``setMask``
  裁剪显示区域时能裁到 mpv 的渲染内容，这是分割对比可行的基础。
- libmpv 定位：冻结（PyInstaller）时在 exe 旁 / ``_internal`` 找；源码环境在
  ``tools/libmpv_bin/``。找到后 prepend 到 ``PATH`` 再 ``import mpv``。

依赖：core/logger（可选）；被依赖：gui/compare_window。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QWidget

from .logger import get_logger

log = get_logger(__name__)

# mpv 的 Python 绑定（python-mpv，pip 包名 mpv）。延迟导入：只有真正要用
# 播放器才加载，避免没有 libmpv 的环境一启动就崩。
_mpv_mod = None


def _candidate_dirs() -> list[Path]:
    """libmpv-2.dll 可能存在的目录（按优先级）。"""
    dirs: list[Path] = []
    # 冻结（PyInstaller onedir）：exe 旁 → _internal
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        dirs.append(exe_dir)
        internal = exe_dir / "_internal"
        if internal.is_dir():
            dirs.append(internal)
    # 源码环境：tools/libmpv_bin（构建脚本会把 dll 复制到该目录）
    # 用 __file__ 相对定位，避免依赖 cwd。
    root = Path(__file__).resolve().parents[3]  # src/momentshift/core -> 项目根
    dirs.append(root / "tools" / "libmpv_bin")
    return dirs


def _find_libmpv() -> Path | None:
    for d in _candidate_dirs():
        p = d / "libmpv-2.dll"
        if p.is_file():
            return p
    return None


def ensure_libmpv_path() -> bool:
    """把 libmpv 所在目录 prepend 到 ``PATH``（python-mpv 靠 DLL 搜索加载）。

    返回 libmpv 是否可用。多次调用幂等。
    """
    if not _find_libmpv():
        return False
    path = os.environ.get("PATH", "")
    for d in _candidate_dirs():
        dstr = str(d)
        if dstr not in path:
            path = dstr + os.pathsep + path
    os.environ["PATH"] = path
    return True


def mpv_available() -> bool:
    """libmpv + python-mpv 是否可用（不创建播放器）。"""
    global _mpv_mod
    if _mpv_mod is not None:
        return True
    if not ensure_libmpv_path():
        return False
    try:
        import mpv  # noqa: PLC0415

        _mpv_mod = mpv
        return True
    except Exception:  # noqa: BLE001 - 任何导入失败都视为不可用
        log.debug("[mpv_player] python-mpv 导入失败", exc_info=True)
        return False


class MpvSurface(QWidget):
    """mpv 渲染面：普通 QWidget 容器 + 嵌入播放器。

    用法：把本控件放进布局，调用 :meth:`play` 播放；分割对比时对**本控件**
    调 ``setMask``（mpv 渲染进本控件 hwnd，mask 能裁到画面）。
    """

    def __init__(self, parent=None, vo: str = "gpu"):
        super().__init__(parent)
        self._player = None
        self._vo = vo
        self.setStyleSheet("background: black; border: none;")

    # -- 生命周期 ---------------------------------------------------------
    def create_player(self) -> bool:
        """创建 mpv 播放器并嵌入本控件。失败（无 libmpv）返回 False。"""
        if self._player is not None:
            return True
        global _mpv_mod
        if _mpv_mod is None and not mpv_available():
            return False
        try:
            import mpv  # noqa: PLC0415

            _mpv_mod = mpv
            # winId() 强制创建原生窗口并返回 hwnd；mpv 直接渲染进它。
            wid = str(int(self.winId()))
            self._player = mpv.MPV(wid=wid, vo=self._vo, loglevel="error")
            # 播完自动回到开头继续（视频 / GIF 都循环）
            self._player.loop_playlist = "inf"
            self._player.keepaspect = True
            return True
        except Exception:  # noqa: BLE001 - 初始化失败按不可用处理
            log.warning("[mpv_player] mpv 初始化失败", exc_info=True)
            self._player = None
            return False

    def is_ready(self) -> bool:
        return self._player is not None

    # -- 播放控制 ---------------------------------------------------------
    def play(self, path: str) -> None:
        if self._player is None:
            if not self.create_player():
                return
        try:
            self._player.play(path)
        except Exception:  # noqa: BLE001
            log.warning("[mpv_player] play 失败: %s", path, exc_info=True)

    def set_pause(self, paused: bool) -> None:
        if self._player is not None:
            self._player.pause = bool(paused)

    def is_paused(self) -> bool:
        if self._player is None:
            return True
        try:
            return bool(self._player.pause)
        except Exception:  # noqa: BLE001
            return True

    def seek(self, seconds: float) -> None:
        """绝对 seek（秒）。"""
        if self._player is not None:
            try:
                self._player.time_pos = float(seconds)
            except Exception:  # noqa: BLE001
                pass

    def time_pos(self) -> float:
        if self._player is None:
            return 0.0
        try:
            return float(self._player.time_pos or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def duration(self) -> float:
        if self._player is None:
            return 0.0
        try:
            return float(self._player.duration or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def frame_step(self, delta: int) -> None:
        """前进 / 后退一帧（GIF / 视频帧步进，边界由播放器自然处理）。"""
        if self._player is None:
            return
        cmd = "frame-step" if delta > 0 else "frame-back-step"
        try:
            for _ in range(max(1, abs(int(delta)))):
                self._player.command(cmd)
        except Exception:  # noqa: BLE001
            log.debug("[mpv_player] frame-step 失败", exc_info=True)

    def terminate(self) -> None:
        if self._player is not None:
            try:
                self._player.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._player = None
