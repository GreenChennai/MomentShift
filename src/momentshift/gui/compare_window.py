"""放大前后对比弹窗。

职责边界：
- 做：以独立弹窗展示放大前后对比；图片/视频/GIF 统一用 **FFmpeg 抽帧**
  驱动播放（v0.8.25 起不再用 QMediaPlayer/QMovie，换成可控的帧序列）。
- 不做：不解析媒体元数据；不解码（交给 core/ffmpeg_frames）。

依赖：core/ffmpeg_frames、gui/theme、i18n/translator；被依赖：gui/upscale_interface。

v0.8.24 重构：
- 支持视频 / GIF 对比；删除窗口内自定义标题栏，恢复原生窗口按钮；
  窗口 1280×720。

v0.8.25 重构：
- 全部媒体（图片/视频/GIF）用 FFmpeg 抽帧呈现，播放完全可控。
- 两种对比模式：**叠放分割**（默认，src/out 叠放 + 分割线跟随鼠标）与
  **左右并排**（并排两栏），工具条按钮切换。
- 视频模式：进度条 + 暂停/继续；GIF 模式：上一帧 / 暂停 / 下一帧。
- 动态媒体循环播放（播完回到第一帧）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core import ffmpeg_frames as frames_mod
from ..core.qt_compat import QObject, QRunnable, QThreadPool, Signal
from ..i18n.translator import tr
from . import tokens
from .theme import accent_color

# 视频扩展名（走 FFmpeg 抽帧）；其余按图片 / GIF 处理
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv",
    ".m4v", ".3gp", ".ts", ".mts", ".m2ts",
}

_MODE_SPLIT = "split"  # 叠放分割（默认）
_MODE_SIDE = "side"    # 左右并排


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXTS


def _is_gif(path: str) -> bool:
    return Path(path).suffix.lower() == ".gif"


# --------------------------------------------------------------------------
class _FrameView(QLabel):
    """单侧画面：静态图 / 帧序列播放 / 单帧显示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frames: list[str] = []
        self._pixmaps: list[QPixmap] = []
        self._idx = 0
        self._fps = 10.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMinimumSize(1, 1)

    # -- 内容装载 ---------------------------------------------------------
    def set_static(self, path: str | None) -> None:
        """静态图片（单帧）。"""
        self.stop()
        self._frames = []
        self._pixmaps = []
        pm = QPixmap(path) if path else QPixmap()
        if pm.isNull():
            self.setPixmap(QPixmap())
        else:
            self._pixmaps = [pm]
            self._idx = 0
            self._show_scaled()

    def set_frames(self, frames: list[str], fps: float) -> None:
        """帧序列（视频 / GIF）。``fps<=0`` 或帧为空时按单帧处理。"""
        self.stop()
        self._frames = list(frames)
        self._fps = max(1.0, float(fps or 10.0))
        self._pixmaps = [QPixmap(f) for f in self._frames]
        self._idx = 0
        if self._pixmaps and not self._pixmaps[0].isNull():
            self._show_scaled()
        if len(self._pixmaps) > 1:
            self._timer.start(int(1000.0 / self._fps))

    # -- 播放控制 ---------------------------------------------------------
    def play(self) -> None:
        if len(self._pixmaps) > 1 and not self._timer.isActive():
            self._timer.start(int(1000.0 / self._fps))

    def pause(self) -> None:
        self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()

    def is_playing(self) -> bool:
        return self._timer.isActive()

    def frame_count(self) -> int:
        return len(self._pixmaps)

    def current_index(self) -> int:
        return self._idx

    def set_index(self, idx: int) -> None:
        n = len(self._pixmaps)
        if n == 0:
            return
        self._idx = max(0, min(n - 1, idx))
        self._show_scaled()

    def step(self, delta: int) -> None:
        """帧步进（v0.8.25 对比#4）：边界处**循环**（GIF 上一帧/下一帧）。"""
        n = len(self._pixmaps)
        if n <= 1:
            return
        self._idx = (self._idx + delta) % n
        if self._idx < 0:
            self._idx += n
        self._show_scaled()

    def _next(self) -> None:
        n = len(self._pixmaps)
        if n <= 1:
            return
        self._idx += 1
        if self._idx >= n:
            self._idx = 0  # 循环播放（v0.8.25 对比#2）
        self._show_scaled()

    def _show_scaled(self) -> None:
        """按当前尺寸等比缩放显示当前帧。"""
        pm = self._pixmaps[self._idx]
        if pm.isNull():
            return
        self.setPixmap(
            pm.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmaps:
            self._show_scaled()


# --------------------------------------------------------------------------
class _CompareArea(QWidget):
    """对比显示区：两种模式共用。

    - ``split``：src / out 两个 :class:`_FrameView` 叠放，右侧半透明遮罩盖住
      src 的右半，露出下层 out 的右半；分割线跟随鼠标。
    - ``side``：左右各一个 view 并排。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = _MODE_SPLIT
        self._split = 0.5
        self.setStyleSheet(f"background: {tokens.COMPARE_BG};")
        self.setMouseTracking(True)

        # 叠放分割：out 在下层、src 在上层、遮罩盖住 src 右半
        self._out_view = _FrameView(self)
        self._src_view = _FrameView(self)
        self._mask = QWidget(self)
        self._mask.setStyleSheet(f"background: {tokens.COMPARE_BG};")

        # 左右并排：两个 view 各占一半
        self._side_layout = QHBoxLayout(self)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(4)
        self._side_src = _FrameView(self)
        self._side_out = _FrameView(self)
        self._side_layout.addWidget(self._side_src, 1)
        self._side_layout.addWidget(self._side_out, 1)

        self._apply_mode()

    # -- 内容 -------------------------------------------------------------
    def set_static(self, src: str | None, out: str | None) -> None:
        self._src_view.set_static(src)
        self._out_view.set_static(out)
        self._side_src.set_static(src)
        self._side_out.set_static(out)

    def set_frames(self, src_frames: list[str], out_frames: list[str], fps: float) -> None:
        self._src_view.set_frames(src_frames, fps)
        self._out_view.set_frames(out_frames, fps)
        self._side_src.set_frames(src_frames, fps)
        self._side_out.set_frames(out_frames, fps)

    # -- 播放（同步两侧） -------------------------------------------------
    def play(self) -> None:
        self._src_view.play()
        self._out_view.play()

    def pause(self) -> None:
        self._src_view.pause()
        self._out_view.pause()

    def is_playing(self) -> bool:
        return self._src_view.is_playing()

    def frame_count(self) -> int:
        return self._src_view.frame_count()

    def current_index(self) -> int:
        return self._src_view.current_index()

    def set_index(self, idx: int) -> None:
        self._src_view.set_index(idx)
        self._out_view.set_index(idx)

    def step(self, delta: int) -> None:
        # v0.8.25 对比#4：帧步进走 view 的 step（边界循环），两侧同步
        self._src_view.step(delta)
        self._out_view.set_index(self._src_view.current_index())

    # -- 模式 -------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._apply_mode()

    def mode(self) -> str:
        return self._mode

    def _apply_mode(self) -> None:
        if self._mode == _MODE_SIDE:
            self._out_view.hide()
            self._src_view.hide()
            self._mask.hide()
            self._side_src.show()
            self._side_out.show()
        else:
            self._side_src.hide()
            self._side_out.hide()
            self._out_view.show()
            self._src_view.show()
            self._mask.show()
            self._layout_split()

    def set_split(self, val: float) -> None:
        self._split = max(0.0, min(1.0, val))
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def _layout_split(self) -> None:
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        self._out_view.setGeometry(0, 0, w, h)
        self._src_view.setGeometry(0, 0, w, h)
        split_x = int(w * self._split)
        self._mask.setGeometry(split_x, 0, w - split_x, h)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def paintEvent(self, _event):
        if self._mode != _MODE_SPLIT:
            return
        x = self._mask.geometry().left()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(accent_color().name()), 2)
        painter.setPen(pen)
        painter.drawLine(x, 0, x, self.height())
        painter.end()


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.8.24：支持视频 / GIF 对比；删除窗口内自定义标题栏；窗口 1280×720。
    v0.8.25：FFmpeg 抽帧播放（循环）；叠放分割 / 左右并排双模式切换
    （默认叠放分割）；视频带进度条 + 暂停；GIF 带上一帧 / 暂停 / 下一帧。
    """

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src_path = src
        self._out_path = out
        self._frames_src: list[str] = []
        self._frames_out: list[str] = []
        self._fps = 10.0
        self._is_video = False
        self._is_gif = False
        self.setWindowTitle(tr("upscale.compare.title"))
        self.resize(1280, 720)
        self.setMinimumSize(960, 540)
        self.setStyleSheet(f"CompareWindow{{background:{tokens.COMPARE_SURFACE};}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 对比显示区
        self._area = _CompareArea(self)
        root.addWidget(self._area, 1)

        # 工具条：模式切换 + 媒体控制
        self._toolbar = QWidget(self)
        self._toolbar.setStyleSheet(
            f"background:{tokens.COMPARE_SURFACE};"
            f"border-top:1px solid {tokens.COMPARE_BORDER};"
        )
        tb = QHBoxLayout(self._toolbar)
        tb.setContentsMargins(12, 8, 12, 8)
        tb.setSpacing(8)

        self._splitBtn = QPushButton(tr("upscale.compare.mode.split"))
        self._splitBtn.setCheckable(True)
        self._splitBtn.setChecked(True)
        self._splitBtn.clicked.connect(lambda: self._set_mode(_MODE_SPLIT))
        self._sideBtn = QPushButton(tr("upscale.compare.mode.side"))
        self._sideBtn.setCheckable(True)
        self._sideBtn.clicked.connect(lambda: self._set_mode(_MODE_SIDE))
        tb.addWidget(self._splitBtn)
        tb.addWidget(self._sideBtn)

        # 视频控制：进度条 + 暂停/继续（v0.8.25 对比#3）
        self._progress = QSlider(Qt.Orientation.Horizontal)
        self._progress.setRange(0, 1)
        self._progress.setFixedWidth(220)
        self._progress.sliderMoved.connect(self._on_seek)
        self._pauseBtn = QPushButton(tr("upscale.compare.pause"))
        self._pauseBtn.clicked.connect(self._on_pause)
        self._stepBackBtn = QPushButton(tr("upscale.compare.prev"))
        self._stepBackBtn.clicked.connect(lambda: self._area.step(-1))
        self._stepFwdBtn = QPushButton(tr("upscale.compare.next"))
        self._stepFwdBtn.clicked.connect(lambda: self._area.step(1))
        for b in (self._pauseBtn, self._stepBackBtn, self._stepFwdBtn):
            b.setStyleSheet(self._btn_style())

        self._mediaControls = QWidget(self)
        mc = QHBoxLayout(self._mediaControls)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(8)
        mc.addWidget(self._stepBackBtn)
        mc.addWidget(self._pauseBtn)
        mc.addWidget(self._stepFwdBtn)
        mc.addWidget(self._progress)
        tb.addStretch(1)
        tb.addWidget(self._mediaControls)
        root.addWidget(self._toolbar)
        self._toolbar.hide()  # 静态图片无控制条

        # 进度定时刷新
        self._prog_timer = QTimer(self)
        self._prog_timer.setInterval(200)
        self._prog_timer.timeout.connect(self._sync_progress)

        # 鼠标事件（叠放分割模式用）
        self._area.mouseMoveEvent = self._on_mouse_move
        self._area.leaveEvent = self._on_mouse_leave
        self._area.enterEvent = self._on_mouse_enter

    def _btn_style(self) -> str:
        return (
            f"QPushButton{{background:{tokens.COMPARE_BORDER};"
            f"color:{tokens.COMPARE_TEXT};border:none;"
            f"border-radius:{tokens.RADIUS_SM}px;padding:4px 12px;"
            f"font-size:{tokens.FONT_SMALL}px;}}"
            f"QPushButton:hover{{background:{tokens.COMPARE_BTN_HOVER};}}"
            f"QPushButton:checked{{background:{accent_color().name()};color:white;}}"
        )

    def showEvent(self, event):
        super().showEvent(event)
        if not hasattr(self, "_loaded"):
            self._loaded = True
            self._load_media()

    # -- 模式与鼠标 -------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        self._area.set_mode(mode)
        self._splitBtn.setChecked(mode == _MODE_SPLIT)
        self._sideBtn.setChecked(mode == _MODE_SIDE)

    def _on_mouse_move(self, event):
        if self._area.mode() != _MODE_SPLIT:
            return
        w = self._area.width()
        if w <= 0:
            return
        self._area.set_split(event.pos().x() / max(1, w))

    def _on_mouse_enter(self, _event):
        if self._area.mode() == _MODE_SPLIT:
            self._area.setCursor(Qt.CursorShape.BlankCursor)

    def _on_mouse_leave(self, _event):
        self._area.unsetCursor()
        self._area.set_split(0.5)

    # -- 媒体装载 ---------------------------------------------------------
    def _load_media(self):
        src = self._src_path
        out = self._out_path
        if not src or not Path(src).exists():
            return

        self._is_gif = _is_gif(src) or _is_gif(out)
        self._is_video = _is_video(src) or _is_video(out)

        if self._is_gif or self._is_video:
            # 动态媒体：后台线程抽帧（避免 GUI 卡顿），完成回主线程装载。
            self._show_toolbar(dynamic=True)
            self._worker_signals = _ExtractSignals(self)
            self._worker_signals.ready.connect(self._on_frames_ready)
            self._extract_pool = QThreadPool(self)
            self._extract_pool.setMaxThreadCount(1)
            for slot, path in (("src", src), ("out", out)):
                self._extract_pool.start(
                    _ExtractTask(slot, path, self._is_gif, self._worker_signals)
                )
            return

        # 静态图片
        self._area.set_static(src, out)
        self._show_toolbar(dynamic=False)

    def _on_frames_ready(self, slot: str, frames: list, fps: float) -> None:
        if slot == "src":
            self._frames_src = frames
            self._fps = fps or self._fps
        else:
            self._frames_out = frames
        if self._frames_src and self._frames_out:
            self._area.set_frames(self._frames_src, self._frames_out, self._fps)
            self._area.play()
            self._sync_progress()
            self._prog_timer.start()
            # 视频/GIF 都循环播放（帧序列天然循环）
            self._update_pause_text()

    def _show_toolbar(self, dynamic: bool) -> None:
        self._toolbar.setVisible(dynamic)
        if dynamic:
            is_video = self._is_video
            self._stepBackBtn.setVisible(not is_video)   # GIF 才有帧步进
            self._stepFwdBtn.setVisible(not is_video)
            self._progress.setVisible(is_video)          # 视频才有进度条
        self._prog_timer.start() if dynamic else self._prog_timer.stop()

    # -- 播放控制 ---------------------------------------------------------
    def _on_pause(self):
        if self._area.is_playing():
            self._area.pause()
        else:
            self._area.play()
        self._update_pause_text()

    def _update_pause_text(self):
        self._pauseBtn.setText(
            tr("upscale.compare.resume") if not self._area.is_playing()
            else tr("upscale.compare.pause")
        )

    def _on_seek(self, pos: int) -> None:
        n = self._area.frame_count()
        if n > 1:
            idx = int(pos * (n - 1) / max(1, self._progress.maximum()))
            self._area.set_index(idx)

    def _sync_progress(self):
        n = self._area.frame_count()
        if n > 1 and self._progress.isVisible():
            self._progress.setValue(
                int(self._area.current_index() * self._progress.maximum() / (n - 1))
            )
        self._update_pause_text()

    def closeEvent(self, event):
        self._area.pause()
        self._prog_timer.stop()
        frames_mod.cleanup_frames(self._frames_src)
        frames_mod.cleanup_frames(self._frames_out)
        super().closeEvent(event)

    # ----- 外部调用入口（保持兼容）-----
    @classmethod
    def show_compare(cls, src: str, out: str, parent=None):
        w = cls(src, out, parent)
        w.exec()


# --------------------------------------------------------------------------
# 后台抽帧
# --------------------------------------------------------------------------
class _ExtractSignals(QObject):
    """抽帧完成信号（挂在窗口上防 GC）。"""

    ready = Signal(str, list, float)  # (slot, frames, fps)


class _ExtractTask(QRunnable):
    """在线程池里抽一帧序列（视频/GIF）。"""

    def __init__(self, slot: str, path: str, is_gif: bool, signals: _ExtractSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._slot = slot
        self._path = path
        self._is_gif = is_gif
        self._signals = signals

    def run(self) -> None:
        try:
            if not self._path or not Path(self._path).exists():
                self._signals.ready.emit(self._slot, [], 0.0)
                return
            frames, fps = frames_mod.extract_frames(self._path, fps=None)
            self._signals.ready.emit(self._slot, frames, fps)
        except Exception:
            self._signals.ready.emit(self._slot, [], 0.0)
