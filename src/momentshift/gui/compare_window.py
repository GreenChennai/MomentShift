"""放大前后对比弹窗。

职责边界：
- 做：以独立弹窗展示放大前后对比；图片走鼠标分割，视频 / GIF 走
  QMediaPlayer / QMovie 真实解码播放（v0.8.26 起弃用 FFmpeg 抽帧）。
- 不做：不解析媒体元数据；不管理下载。

依赖：core/ffmpeg、gui/theme、i18n/translator；被依赖：gui/upscale_interface。

v0.8.24：支持视频 / GIF 对比；删除窗口内自定义标题栏；窗口 1280×720。
v0.8.25：FFmpeg 抽帧播放（循环）；叠放分割 / 左右并排双模式。
v0.8.26 重构：
- **弃用 FFmpeg 抽帧**：抽帧性能低、画质对比不准确。视频改用
  ``QMediaPlayer``（Qt 内置，底层就是 FFmpeg 解码——与 ffplay 同源，
  画质准确、性能好），GIF 用 ``QMovie``。进度条 / 暂停 / 循环 / 帧步进
  全部由播放器原生支持，不再自己逐帧搬图。
- 提供「用 ffplay 打开」按钮：需要 ffplay 独立窗口播放时一键呼出
  （ffplay 是交互式播放器，无法嵌入 Qt 分割对比，故保留 Qt 播放为主、
  ffplay 独立窗口为辅）。
- 叠放分割对视频 / GIF 生效：两个 QVideoWidget / QLabel 叠放，各自
  ``setMask`` 裁剪到分割线左右半，Qt 原生支持（不再依赖原生窗口裁剪）。
- 恢复窗口原生按钮（-、口、X）：显式 setWindowFlags 加上最小化/最大化。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRect, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.ffmpeg import find_ffmpeg
from ..i18n.translator import tr
from . import tokens
from .theme import accent_color

# 视频扩展名（走 QMediaPlayer）；其余按图片 / GIF 处理
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


def _ffplay_cmd() -> list[str] | None:
    """定位 ffplay（与 ffmpeg 同目录）。找不到返回 None。"""
    ff = find_ffmpeg()
    if not ff:
        return None
    p = Path(ff).parent
    cand = p / "ffplay.exe"
    if cand.exists():
        return [str(cand)]
    cand2 = p / "ffplay"
    if cand2.exists():
        return [str(cand2)]
    return None


# --------------------------------------------------------------------------
class _MediaView(QWidget):
    """单侧画面：静态图 / 视频播放器 / GIF 播放器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._video: QVideoWidget | None = None
        self._player: QMediaPlayer | None = None
        self._movie = None
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("background: transparent; border: none;")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._label, 1)

    # -- 内容装载 ---------------------------------------------------------
    def set_static(self, path: str | None) -> None:
        """静态图片（单帧）。"""
        self._clear_media()
        pm = QPixmap(path) if path else QPixmap()
        if pm.isNull():
            self._label.setPixmap(QPixmap())
        else:
            self._label.setPixmap(
                pm.scaled(
                    self._label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def set_video(self, path: str) -> None:
        """视频：QMediaPlayer 循环播放（画质准确、支持进度/暂停）。"""
        self._clear_media()
        self._video = QVideoWidget(self)
        self._video.setStyleSheet("background: transparent; border: none;")
        self._layout.addWidget(self._video, 1)
        self._label.hide()
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._video)
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._player.setLoops(QMediaPlayer.Loops.Infinite)  # 循环播放
        self._player.play()

    def set_gif(self, path: str) -> None:
        """GIF：QMovie 循环播放 + 帧步进支持。"""
        from PyQt6.QtGui import QMovie  # noqa: PLC0415 - 局部导入

        self._clear_media()
        self._movie = QMovie(path)
        self._movie.setCacheMode(QMovie.CacheMode.CacheAll)
        self._label.setMovie(self._movie)
        self._movie.start()
        self._label.show()

    # -- 播放控制 ---------------------------------------------------------
    def play(self) -> None:
        if self._player is not None:
            self._player.play()
        if self._movie is not None and not self._movie.state() == 2:  # Running
            self._movie.start()

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()
        if self._movie is not None:
            self._movie.setPaused(True)

    def stop(self) -> None:
        if self._player is not None:
            self._player.pause()
        if self._movie is not None:
            self._movie.setPaused(True)

    def is_playing(self) -> bool:
        if self._player is not None:
            return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if self._movie is not None:
            return self._movie.state() == 2  # QMovie.MovieState.Running
        return False

    def is_video(self) -> bool:
        return self._player is not None

    def duration_ms(self) -> int:
        return int(self._player.duration()) if self._player is not None else 0

    def position_ms(self) -> int:
        return int(self._player.position()) if self._player is not None else 0

    def seek(self, pos_ms: int) -> None:
        if self._player is not None:
            self._player.setPosition(pos_ms)

    def set_frame(self, idx: int) -> None:
        """GIF 跳到第 ``idx`` 帧（供上一帧/下一帧）。"""
        if self._movie is not None:
            total = self._movie.frameCount()
            if total > 0:
                self._movie.jumpToFrame(max(0, min(total - 1, idx)))

    def current_frame(self) -> int:
        return self._movie.currentFrameNumber() if self._movie is not None else 0

    def frame_count(self) -> int:
        return self._movie.frameCount() if self._movie is not None else 0

    def _clear_media(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.deleteLater()
            self._player = None
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        if self._video is not None:
            self._video.deleteLater()
            self._video = None
        self._label.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._label.pixmap() is not None and not self._label.pixmap().isNull():
            pm = self._label.pixmap()
            self._label.setPixmap(
                pm.scaled(
                    self._label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


# --------------------------------------------------------------------------
class _CompareArea(QWidget):
    """对比显示区：两种模式共用。

    - ``split``：src / out 两个视图叠放，各自 ``setMask`` 裁剪到分割线
      左右半（Qt 原生支持，视频/GIF/图片通用）。
    - ``side``：左右各一个视图并排。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = _MODE_SPLIT
        self._split = 0.5
        self._is_dynamic = False
        self.setStyleSheet(f"background: {tokens.COMPARE_BG};")
        self.setMouseTracking(True)

        # 叠放分割：out 在下层、src 在上层，各自 mask 到左右半
        self._out_view = _MediaView(self)
        self._src_view = _MediaView(self)
        self._out_view.show()
        self._src_view.show()

        # 左右并排：两个视图各占一半（用同一对视图重新布局）
        self._side_layout = QHBoxLayout(self)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(4)

        self._apply_mode()

    # -- 内容 -------------------------------------------------------------
    def set_content(self, src: str | None, out: str | None, kind: str) -> None:
        """装载内容。``kind``: ``image`` / ``video`` / ``gif``。"""
        self._is_dynamic = kind in ("video", "gif")
        # 先把两个视图从 side 布局摘下来（若已挂）
        self._side_layout.removeWidget(self._src_view)
        self._side_layout.removeWidget(self._out_view)
        self._src_view.setParent(self)
        self._out_view.setParent(self)

        if kind == "video":
            self._src_view.set_video(src or "")
            self._out_view.set_video(out or "")
        elif kind == "gif":
            self._src_view.set_gif(src or "")
            self._out_view.set_gif(out or "")
        else:
            self._src_view.set_static(src)
            self._out_view.set_static(out)
        self._apply_mode()

    # -- 播放（同步两侧） -------------------------------------------------
    def play(self) -> None:
        self._src_view.play()
        self._out_view.play()

    def pause(self) -> None:
        self._src_view.pause()
        self._out_view.pause()

    def is_playing(self) -> bool:
        return self._src_view.is_playing()

    def is_video(self) -> bool:
        return self._src_view.is_video()

    def is_dynamic(self) -> bool:
        return self._is_dynamic

    # 视频进度（以 src 为准，两侧同时播放）
    def duration_ms(self) -> int:
        return self._src_view.duration_ms()

    def position_ms(self) -> int:
        return self._src_view.position_ms()

    def seek(self, pos_ms: int) -> None:
        self._src_view.seek(pos_ms)
        self._out_view.seek(pos_ms)

    # GIF 帧（两侧同步）
    def set_frame(self, idx: int) -> None:
        self._src_view.set_frame(idx)
        self._out_view.set_frame(idx)

    def current_frame(self) -> int:
        return self._src_view.current_frame()

    def frame_count(self) -> int:
        return self._src_view.frame_count()

    # -- 模式 -------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._apply_mode()

    def mode(self) -> str:
        return self._mode

    def _apply_mode(self) -> None:
        if self._mode == _MODE_SIDE:
            # 并排：摘下来重挂到 side 布局
            self._src_view.setParent(None)
            self._out_view.setParent(None)
            self._side_layout.addWidget(self._src_view, 1)
            self._side_layout.addWidget(self._out_view, 1)
            self._src_view.show()
            self._out_view.show()
        else:
            # 叠放：直接叠在本控件上
            self._src_view.setParent(self)
            self._out_view.setParent(self)
            self._src_view.show()
            self._out_view.show()
            self._layout_split()

    def set_split(self, val: float) -> None:
        self._split = max(0.0, min(1.0, val))
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def _layout_split(self) -> None:
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        split_x = int(w * self._split)
        # 各自 setMask 到分割线左右半：src 左半、out 右半
        self._src_view.setGeometry(0, 0, w, h)
        self._out_view.setGeometry(0, 0, w, h)
        self._src_view.setMask(QRegion(QRect(0, 0, max(1, split_x), h)))
        self._out_view.setMask(QRegion(QRect(split_x, 0, max(1, w - split_x), h)))
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def paintEvent(self, _event):
        if self._mode != _MODE_SPLIT:
            return
        x = int(self.width() * self._split)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(accent_color().name()), 2)
        painter.setPen(pen)
        painter.drawLine(x, 0, x, self.height())
        painter.end()


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.8.24：支持视频 / GIF 对比；窗口 1280×720。
    v0.8.25：叠放分割 / 左右并排双模式。
    v0.8.26：弃用 FFmpeg 抽帧——视频用 QMediaPlayer（FFmpeg 解码，画质准确、
    性能好，进度条/暂停/循环原生支持），GIF 用 QMovie；「用 ffplay 打开」
    按钮一键呼出 ffplay 独立窗口；叠放分割用 setMask 对视频/GIF 生效；
    恢复原生窗口按钮（-、口、X）。
    """

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src_path = src
        self._out_path = out
        self._kind = "image"
        self.setWindowTitle(tr("upscale.compare.title"))
        # v0.8.26 对比#4：显式补上最小化/最大化，恢复原生 -、口、X
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
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

        # 视频控制：进度条 + 暂停/继续（QMediaPlayer 原生支持）
        self._progress = QSlider(Qt.Orientation.Horizontal)
        self._progress.setRange(0, 1000)
        self._progress.setFixedWidth(240)
        self._progress.sliderMoved.connect(self._on_seek)
        self._pauseBtn = QPushButton(tr("upscale.compare.pause"))
        self._pauseBtn.clicked.connect(self._on_pause)
        # GIF 帧控制
        self._stepBackBtn = QPushButton(tr("upscale.compare.prev"))
        self._stepBackBtn.clicked.connect(lambda: self._area.set_frame(self._area.current_frame() - 1))
        self._stepFwdBtn = QPushButton(tr("upscale.compare.next"))
        self._stepFwdBtn.clicked.connect(lambda: self._area.set_frame(self._area.current_frame() + 1))
        # 用 ffplay 打开（独立窗口，真实播放器）
        self._ffplayBtn = QPushButton(tr("upscale.compare.ffplay"))
        self._ffplayBtn.clicked.connect(self._open_ffplay)
        for b in (self._pauseBtn, self._stepBackBtn, self._stepFwdBtn, self._ffplayBtn):
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
        tb.addWidget(self._ffplayBtn)
        root.addWidget(self._toolbar)
        self._toolbar.hide()  # 静态图片无控制条

        # 进度定时刷新（视频用）
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

        if _is_video(src) or _is_video(out):
            self._kind = "video"
        elif _is_gif(src) or _is_gif(out):
            self._kind = "gif"
        else:
            self._kind = "image"

        dynamic = self._kind in ("video", "gif")
        self._toolbar.setVisible(dynamic)
        if dynamic:
            self._stepBackBtn.setVisible(self._kind == "gif")
            self._stepFwdBtn.setVisible(self._kind == "gif")
            self._progress.setVisible(self._kind == "video")
            self._pauseBtn.setText(tr("upscale.compare.pause"))
            if self._kind == "video":
                self._prog_timer.start()
        self._area.set_content(src, out, self._kind)
        if dynamic:
            self._area.play()

    def _open_ffplay(self):
        """用 ffplay 独立窗口播放源与放大结果（真实播放器）。"""
        import subprocess  # noqa: PLC0415

        from ..core.platform import popen_silent  # noqa: PLC0415

        cmd = _ffplay_cmd()
        if not cmd:
            return
        for path in (self._src_path, self._out_path):
            if path and Path(path).exists():
                try:
                    popen_silent([*cmd, "-loop", "0", "-i", path])
                except OSError:
                    pass

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

    def _on_seek(self, value: int) -> None:
        dur = self._area.duration_ms()
        if dur > 0:
            self._area.seek(int(value * dur / 1000))

    def _sync_progress(self):
        dur = self._area.duration_ms()
        if dur > 0 and self._progress.isVisible():
            pos = self._area.position_ms()
            self._progress.setValue(int(pos * 1000 / dur))
        self._update_pause_text()

    def closeEvent(self, event):
        self._area.pause()
        self._prog_timer.stop()
        super().closeEvent(event)

    # ----- 外部调用入口（保持兼容）-----
    @classmethod
    def show_compare(cls, src: str, out: str, parent=None):
        w = cls(src, out, parent)
        w.exec()
