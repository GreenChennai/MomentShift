"""放大前后对比弹窗。

职责边界：
- 做：以独立弹窗展示放大前后对比；图片走 QPixmap 分割，视频 / GIF 走 Qt
  内置 QtMultimedia 真实解码播放（v0.8.29 起弃用 python-mpv / libmpv）。
- 不做：不解析媒体元数据；不管理下载。

依赖：gui/theme、i18n/translator；被依赖：gui/upscale_interface。

v0.8.24：支持视频 / GIF 对比；删除窗口内自定义标题栏；窗口 1280×720。
v0.8.25：FFmpeg 抽帧播放（循环）；叠放分割 / 左右并排双模式。
v0.8.26：视频 QMediaPlayer + QVideoWidget，GIF QMovie；ffplay 按钮；setMask。
v0.8.27：python-mpv（libmpv）嵌入播放；分割线恢复；GIF 帧控制走 frame-step。
v0.8.29 重构（轻量化）：
- **弃用 python-mpv / libmpv**（112MB 依赖太重，且需系统装有 mpv）。改用
  **Qt 内置 QtMultimedia**——它就是 Qt 官方的「FFmpeg-Qt」封装（PyQt6 自带、
  零额外打包、底层 FFmpeg 解码，画质与 mpv 同源）：
  - 视频：``QMediaPlayer`` + ``QVideoSink``，``videoFrameChanged`` 每帧转
    ``QImage`` 显示到普通 ``QLabel``（进度 / 暂停 / seek / 循环原生支持）；
  - GIF：``QMovie`` 播放，暂停态 ``jumpToFrame`` 做上一帧 / 下一帧
    （V0.8.26 帧控制无效的根因是**播放中**跳帧被 Qt 忽略，必须暂停后跳）；
  - 图片：``QPixmap``。
  三类媒体最终都画在**普通 QLabel** 上 → 父容器 ``setMask`` 分割对比对
  视频 / GIF / 图片统一可靠（不再有 QVideoWidget 原生窗口 / mpv 渲染面的
  层级问题）。
- 分割线交互：默认在中间；鼠标进入对比区 → 光标隐藏、分割线跟随鼠标左右
  移动；鼠标离开对比区（含移入下方功能栏）→ 光标恢复、分割线回到中间。
- 删除 mpv_player 依赖与「libmpv 缺失」提示（QtMultimedia 内置，永不缺失）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRect, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QMovie, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

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


# --------------------------------------------------------------------------
class _MediaView(QWidget):
    """单侧画面容器：图片 / 视频帧 / GIF 帧统一画在普通 QLabel 上。

    v0.8.29：视频用 ``QMediaPlayer + QVideoSink`` 实时解码（QtMultimedia =
    FFmpeg 解码，画质准确、零额外依赖），每帧 ``toImage()`` 显示到 QLabel；
    GIF 用 ``QMovie``。QLabel 是普通控件 → 父容器 ``setMask`` 分割可靠。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._movie: QMovie | None = None
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            f"background: black; color:{tokens.COMPARE_TEXT}; border: none;"
        )
        self._label.setWordWrap(True)
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
        self._label.show()

    def set_video(self, path: str) -> None:
        """视频：QMediaPlayer + QVideoSink，逐帧转 QImage 显示（循环播放）。"""
        self._clear_media()
        if not path or not Path(path).exists():
            self._label.setText(tr("upscale.compare.missing"))
            self._label.show()
            return
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.setVideoOutput(self._sink)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)  # 循环播放
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._player.play()
        self._label.show()

    def set_gif(self, path: str) -> None:
        """GIF：QMovie 循环播放 + 帧步进（暂停态 jumpToFrame 才生效）。"""
        self._clear_media()
        if not path or not Path(path).exists():
            self._label.setText(tr("upscale.compare.missing"))
            self._label.show()
            return
        self._movie = QMovie(path)
        self._movie.setCacheMode(QMovie.CacheMode.CacheAll)
        self._label.setMovie(self._movie)
        self._movie.start()
        self._label.show()

    def _on_frame(self, frame) -> None:
        img = frame.toImage()
        if img.isNull():
            return
        disp = self._label.size()
        if disp.width() > 4 and disp.height() > 4:
            img = img.scaled(
                disp,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(QPixmap.fromImage(img))

    # -- 播放控制 ---------------------------------------------------------
    def play(self) -> None:
        if self._player is not None:
            self._player.play()
        if self._movie is not None and self._movie.state() != QMovie.MovieState.Running:
            self._movie.start()

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()
        if self._movie is not None:
            self._movie.setPaused(True)

    def is_playing(self) -> bool:
        if self._player is not None:
            return (
                self._player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState
            )
        if self._movie is not None:
            return self._movie.state() == QMovie.MovieState.Running
        return False

    def is_dynamic(self) -> bool:
        return self._player is not None or self._movie is not None

    def duration(self) -> float:
        """时长（秒）。"""
        if self._player is not None:
            return self._player.duration() / 1000.0
        return 0.0

    def time_pos(self) -> float:
        """当前位置（秒）。"""
        if self._player is not None:
            return self._player.position() / 1000.0
        return 0.0

    def seek(self, seconds: float) -> None:
        if self._player is not None:
            self._player.setPosition(int(seconds * 1000))

    def frame_step(self, delta: int) -> None:
        """GIF 上一帧 / 下一帧：暂停后 jumpToFrame（边界循环）。"""
        if self._movie is None:
            return
        n = self._movie.frameCount()
        if n <= 0:
            return
        self._movie.setPaused(True)
        cur = self._movie.currentFrameNumber()
        self._movie.jumpToFrame((cur + delta) % n)

    def _clear_media(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.deleteLater()
            self._player = None
        if self._sink is not None:
            self._sink.deleteLater()
            self._sink = None
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        self._label.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        pm = self._label.pixmap()
        if pm is not None and not pm.isNull():
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
      左右半。三类媒体都画在普通 QLabel 上，mask 裁剪统一可靠。
    - ``side``：左右各一个视图并排。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = _MODE_SPLIT
        self._split = 0.5
        self.setStyleSheet(f"background: {tokens.COMPARE_BG};")
        self.setMouseTracking(True)

        # 叠放分割：out 在下层、src 在上层，各自 mask 到左右半
        self._out_view = _MediaView(self)
        self._src_view = _MediaView(self)
        self._out_view.show()
        self._src_view.show()
        self._src_view.raise_()

        # 左右并排：两个视图各占一半
        self._side_layout = QHBoxLayout(self)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(4)

        self._apply_mode()

    # -- 内容 -------------------------------------------------------------
    def set_content(self, src: str | None, out: str | None, kind: str) -> None:
        """装载内容。``kind``: ``image`` / ``video`` / ``gif``。"""
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

    def is_dynamic(self) -> bool:
        return self._src_view.is_dynamic()

    # 视频进度（以 src 为准，两侧同步播放）
    def duration(self) -> float:
        return self._src_view.duration()

    def time_pos(self) -> float:
        return self._src_view.time_pos()

    def seek(self, seconds: float) -> None:
        self._src_view.seek(seconds)
        self._out_view.seek(seconds)

    # GIF 帧（两侧同步）
    def frame_step(self, delta: int) -> None:
        self._src_view.frame_step(delta)
        self._out_view.frame_step(delta)

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
            # 叠放：直接叠在本控件上，src 在上层
            self._src_view.setParent(self)
            self._out_view.setParent(self)
            self._src_view.show()
            self._out_view.show()
            self._src_view.raise_()
            self._layout_split()

    def set_split(self, val: float) -> None:
        self._split = max(0.0, min(1.0, val))
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def split_value(self) -> float:
        return self._split

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
        self._src_view.raise_()
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

    v0.8.29：
    - 视频 / GIF 用 Qt 内置 QtMultimedia（FFmpeg 解码，零额外依赖）播放，
      循环 + 进度 / 暂停 / 帧步进原生支持；
    - 分割线对比对所有媒体类型生效（全部画在普通 QLabel，setMask 可靠）；
    - 分割线交互：默认中间，鼠标进入对比区跟随移动（光标隐藏），离开恢复；
    - 不再依赖 libmpv / python-mpv。
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

        # 视频控制：进度条 + 暂停/继续
        self._progress = QSlider(Qt.Orientation.Horizontal)
        self._progress.setRange(0, 1000)
        self._progress.setFixedWidth(240)
        self._progress.sliderMoved.connect(self._on_seek)
        self._pauseBtn = QPushButton(tr("upscale.compare.pause"))
        self._pauseBtn.clicked.connect(self._on_pause)
        # GIF 帧控制
        self._stepBackBtn = QPushButton(tr("upscale.compare.prev"))
        self._stepBackBtn.clicked.connect(lambda: self._on_frame_step(-1))
        self._stepFwdBtn = QPushButton(tr("upscale.compare.next"))
        self._stepFwdBtn.clicked.connect(lambda: self._on_frame_step(1))
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
        # 进入对比区：隐藏光标，分割线随鼠标移动（纯对比体验）
        if self._area.mode() == _MODE_SPLIT:
            self._area.setCursor(Qt.CursorShape.BlankCursor)

    def _on_mouse_leave(self, _event):
        # 离开对比区（含移入下方功能栏）：恢复光标、分割线回中间
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

    # -- 播放控制 ---------------------------------------------------------
    def _on_pause(self):
        if self._area.is_playing():
            self._area.pause()
        else:
            self._area.play()
        self._update_pause_text()

    def _on_frame_step(self, delta: int):
        """GIF 上一帧 / 下一帧：内部先暂停再逐帧跳（QMovie 播放中跳帧无效）。"""
        self._area.frame_step(delta)
        self._update_pause_text()

    def _update_pause_text(self):
        self._pauseBtn.setText(
            tr("upscale.compare.resume") if not self._area.is_playing()
            else tr("upscale.compare.pause")
        )

    def _on_seek(self, value: int) -> None:
        dur = self._area.duration()
        if dur > 0:
            self._area.seek(value * dur / 1000.0)

    def _sync_progress(self):
        dur = self._area.duration()
        if dur > 0 and self._progress.isVisible():
            pos = self._area.time_pos()
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
