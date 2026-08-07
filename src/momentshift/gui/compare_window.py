"""放大前后对比弹窗。

职责边界：
- 做：以独立弹窗展示放大前后对比；图片走 QPixmap，视频走 QtMultimedia
  （QMediaPlayer + QVideoSink 逐帧解码），GIF 走 QMovie；三类媒体统一画在
  普通 QLabel 上，叠放分割用 ``setMask`` 裁剪，分割线由独立 overlay 绘制。
- 不做：不解析媒体元数据；不管理下载。

依赖：gui/theme、i18n/translator；被依赖：gui/upscale_interface。

v0.8.24~v0.8.29 历次迭代：支持视频/GIF 对比；抽帧 → mpv → QtMultimedia。
v0.8.30 完全重写（用户实测反馈 Bug 太多，要求重写整个模块）：
- 分割线没有绘制 → 独立 ``_SplitOverlay`` 透明层画线（不再画在父控件
  paintEvent 里被全窗口媒体视图遮挡）。
- 分割线不跟随鼠标（要按住左键）→ overlay 全程 ``setMouseTracking`` 捕获
  ``mouseMoveEvent``，悬停即跟随。
- 「叠放分割/左右并排」按钮黑字灰底看不清 → QSS 改为背景 ``#333333`` 白字。
- 左右并排只显示左边 → 模式切换时 ``clearMask()`` 清除叠放残留的裁剪
  mask（旧实现切 side 后右侧仍被 mask 裁没）。
- 叠放分割鼠标移动鬼畜 + 布局仍并排 → 模式切换彻底重建（side 布局摘除 /
  overlay 显隐 / 视图 reparent），状态不再残留。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QMovie, QPainter, QPen, QPixmap, QRegion
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

# 工具条按钮（v0.8.30：深底白字，用户反馈灰底黑字看不清）
_BTN_QSS = (
    "QPushButton{background:#333333;color:white;border:none;"
    "border-radius:6px;padding:5px 14px;font-size:12px;}"
    "QPushButton:hover{background:#4a4a4a;}"
    "QPushButton:checked{background:#238636;color:white;}"
    "QPushButton:disabled{background:#555555;color:#bbbbbb;}"
)


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXTS


def _is_gif(path: str) -> bool:
    return Path(path).suffix.lower() == ".gif"


# --------------------------------------------------------------------------
class _MediaView(QWidget):
    """单侧画面容器：图片 / 视频帧 / GIF 帧统一画在普通 QLabel 上。

    视频用 ``QMediaPlayer + QVideoSink`` 实时解码（QtMultimedia = FFmpeg
    解码，零额外依赖），每帧 ``toImage()`` 显示到 QLabel；GIF 用 ``QMovie``。
    QLabel 是普通控件 → 父容器 ``setMask`` 分割可靠。

    v0.8.33：支持整体缩放（Ctrl+滚轮）——``_zoom`` 因子（0.25~8）作用于
    「适配窗口尺寸」之上；图片 / 视频帧用 scaled 重画，GIF 用
    ``QMovie.setScaledSize``。缩放值由 ``_CompareArea`` 统一广播。
    """

    # Ctrl+滚轮：把滚轮 delta 上报给对比区（统一缩放两侧）
    zoomRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._movie: QMovie | None = None
        self._zoom = 1.0
        self._raw_pixmap = QPixmap()  # 图片原始图（缩放时基于它重画，不累积模糊）
        self._last_frame = None       # 视频最近帧 QImage（暂停后缩放仍可重画）
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("background: black; color: #cccccc; border: none;")
        self._label.setWordWrap(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._label, 1)

    # -- 缩放 -------------------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        """设置缩放因子并按当前媒体重画（0.25~8）。"""
        self._zoom = max(0.25, min(8.0, zoom))
        if not self._raw_pixmap.isNull():
            self._apply_pixmap(self._raw_pixmap)
        elif self._last_frame is not None and not self._last_frame.isNull():
            self._apply_image(self._last_frame)
        elif self._movie is not None:
            self._gif_rescale()

    def zoom(self) -> float:
        return self._zoom

    def _fit_size(self, sw: int, sh: int) -> tuple[int, int]:
        """保持宽高比适配 label 的尺寸（zoom=1 的基准）。"""
        lw, lh = self._label.width(), self._label.height()
        if sw <= 0 or sh <= 0 or lw < 4 or lh < 4:
            return sw, sh
        k = min(lw / sw, lh / sh)
        return max(1, int(sw * k)), max(1, int(sh * k))

    def _disp_size(self, sw: int, sh: int) -> tuple[int, int]:
        fw, fh = self._fit_size(sw, sh)
        return max(1, int(fw * self._zoom)), max(1, int(fh * self._zoom))

    def _apply_pixmap(self, pm: QPixmap) -> None:
        w, h = self._disp_size(pm.width(), pm.height())
        if w > 0 and h > 0:
            self._label.setPixmap(
                pm.scaled(
                    w, h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _apply_image(self, img) -> None:
        w, h = self._disp_size(img.width(), img.height())
        if w > 0 and h > 0:
            self._label.setPixmap(
                QPixmap.fromImage(
                    img.scaled(
                        w, h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            )

    def _gif_rescale(self) -> None:
        """按缩放因子给 QMovie 设 scaledSize（Qt 6.1+）。"""
        if self._movie is None:
            return
        cur = self._movie.currentImage()
        if not cur.isNull():
            sw, sh = cur.width(), cur.height()
        else:
            r = self._movie.frameRect()
            if r.isEmpty():
                return
            sw, sh = r.width(), r.height()
        w, h = self._disp_size(sw, sh)
        if w > 0 and h > 0:
            self._movie.setScaledSize(QSize(w, h))

    # -- 内容装载 ---------------------------------------------------------
    def set_static(self, path: str | None) -> None:
        """静态图片（单帧）。"""
        self._clear_media()
        self._raw_pixmap = QPixmap(path) if path else QPixmap()
        if self._raw_pixmap.isNull():
            self._label.setPixmap(QPixmap())
        else:
            self._apply_pixmap(self._raw_pixmap)
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
        self._movie.started.connect(self._gif_rescale)
        self._movie.start()
        self._label.show()

    def _on_frame(self, frame) -> None:
        img = frame.toImage()
        if img.isNull():
            return
        self._last_frame = img
        self._apply_image(img)

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
        return self._player.duration() / 1000.0 if self._player is not None else 0.0

    def time_pos(self) -> float:
        """当前位置（秒）。"""
        return self._player.position() / 1000.0 if self._player is not None else 0.0

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
        self._last_frame = None
        self._raw_pixmap = QPixmap()
        self._label.show()

    def wheelEvent(self, event):
        # v0.8.33：Ctrl+滚轮 → 上报给对比区统一缩放
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomRequested.emit(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 重算适配尺寸并重画（缩放保持不变）
        if not self._raw_pixmap.isNull():
            self._apply_pixmap(self._raw_pixmap)
        elif self._last_frame is not None and not self._last_frame.isNull():
            self._apply_image(self._last_frame)
        elif self._movie is not None:
            self._gif_rescale()


# --------------------------------------------------------------------------
class _SplitOverlay(QWidget):
    """叠放分割的透明覆盖层：画分割线 + 捕获鼠标（v0.8.30 重写）。

    旧实现把分割线画在父控件 paintEvent 里，被全窗口的媒体视图盖住看不
    见；鼠标事件也被子视图吃掉（要按住左键才触发 move）。本覆盖层浮在所有
    媒体视图之上：透明、全窗口、``setMouseTracking`` 悬停即跟随。
    """

    def __init__(self, area: "_CompareArea"):
        super().__init__(area)
        self._area = area
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, _event):
        x = int(self.width() * self._area.split_value())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(accent_color().name()), 2)
        painter.setPen(pen)
        painter.drawLine(x, 0, x, self.height())
        painter.end()

    def mouseMoveEvent(self, event):
        # 悬停即跟随（不需要按住左键）
        w = self.width()
        if w > 0:
            self._area.set_split(event.position().x() / w)
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        # v0.8.33：Ctrl+滚轮 → 对比区整体缩放
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._area.change_zoom(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def enterEvent(self, _event):
        # 进入对比区：隐藏光标，分割线随鼠标移动（纯对比体验）
        self.setCursor(Qt.CursorShape.BlankCursor)
        super().enterEvent(_event)

    def leaveEvent(self, _event):
        # 离开对比区（含移入下方功能栏）：恢复光标、分割线回中间
        self.unsetCursor()
        self._area.set_split(0.5)
        super().leaveEvent(_event)


# --------------------------------------------------------------------------
class _CompareArea(QWidget):
    """对比显示区：两种模式共用。

    - ``split``：src / out 两个视图叠放，各自 ``setMask`` 裁剪到分割线
      左右半；``_SplitOverlay`` 浮在最上层画分割线并接收鼠标。
    - ``side``：左右各一个视图并排（无 overlay、无 mask）。

    模式切换做**彻底重建**（v0.8.30）：先摘除 side 布局中的视图、
    ``clearMask()`` 清除叠放残留裁剪、再按目标模式挂载——杜绝旧实现
    「切 side 右侧被 mask 裁没 / 切回 split 布局仍残留并排」的状态污染。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = _MODE_SPLIT
        self._split = 0.5
        self._zoom = 1.0  # v0.8.33：整体缩放因子（Ctrl+滚轮），0.25~8
        self.setStyleSheet(f"background: {tokens.COMPARE_BG};")

        self._out_view = _MediaView(self)
        self._src_view = _MediaView(self)
        # v0.8.33：任一视图收到 Ctrl+滚轮 → 统一缩放两侧
        self._src_view.zoomRequested.connect(self.change_zoom)
        self._out_view.zoomRequested.connect(self.change_zoom)

        # 左右并排布局
        self._side_layout = QHBoxLayout(self)
        self._side_layout.setContentsMargins(0, 0, 0, 0)
        self._side_layout.setSpacing(4)

        # 叠放分割覆盖层（画线 + 鼠标）
        self._overlay = _SplitOverlay(self)

        self._apply_mode()

    # -- 缩放（v0.8.33：Ctrl+滚轮整体缩放对比内容）------------------------
    def change_zoom(self, delta: int) -> None:
        """滚轮 delta → 缩放因子（每格 ×1.2）。"""
        step = 1.2 ** (delta / 120.0)
        self.set_zoom(self._zoom * step)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.25, min(8.0, zoom))
        self._src_view.set_zoom(self._zoom)
        self._out_view.set_zoom(self._zoom)

    def zoom_value(self) -> float:
        return self._zoom

    def reset_zoom(self) -> None:
        self.set_zoom(1.0)

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
        if mode == self._mode:
            return
        self._mode = mode
        self._apply_mode()

    def mode(self) -> str:
        return self._mode

    def _apply_mode(self) -> None:
        # 1) 先彻底清理：摘除 side 布局中的视图、清除叠放 mask、隐藏 overlay
        self._side_layout.removeWidget(self._src_view)
        self._side_layout.removeWidget(self._out_view)
        self._src_view.clearMask()
        self._out_view.clearMask()
        self._overlay.hide()
        if self._mode == _MODE_SIDE:
            # 2a) 左右并排：视图重挂到 side 布局
            self._src_view.setParent(None)
            self._out_view.setParent(None)
            self._side_layout.addWidget(self._src_view, 1)
            self._side_layout.addWidget(self._out_view, 1)
            self._src_view.show()
            self._out_view.show()
        else:
            # 2b) 叠放分割：视图叠在本控件上 + overlay 浮顶
            self._src_view.setParent(self)
            self._out_view.setParent(self)
            self._src_view.show()
            self._out_view.show()
            self._src_view.raise_()
            self._overlay.raise_()
            self._overlay.show()
            self._layout_split()

    def set_split(self, val: float) -> None:
        self._split = max(0.0, min(1.0, val))
        if self._mode == _MODE_SPLIT:
            self._layout_split()
            self._overlay.update()

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
        self._overlay.raise_()
        self._overlay.setGeometry(0, 0, w, h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == _MODE_SPLIT:
            self._layout_split()

    def paintEvent(self, _event):
        """v0.8.33：背景水印——灰色小字错位平铺（左右两半各铺各的标签词），
        中间留一条无字的竖向留白带；媒体视图叠在上面会盖住这些字。
        """
        w, h = self.width(), self.height()
        if w < 8 or h < 8:
            return
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(tokens.COMPARE_BG))
            # 中间无字留白带
            gap = min(160, max(80, w // 6))
            gx0 = (w - gap) // 2
            gx1 = gx0 + gap
            base_font = QFont()
            base_font.setPointSize(9)
            painter.setFont(base_font)
            for half, key in (
                (0, "upscale.compare.original"),
                (1, "upscale.compare.upscaled"),
            ):
                x0 = 0 if half == 0 else gx1
                x1 = gx0 if half == 0 else w
                if x1 - x0 < 60:
                    continue
                text = tr(key)
                fm = painter.fontMetrics()
                tw = fm.horizontalAdvance(text) + 28
                th = fm.height() + 14
                painter.setPen(QColor("#3c4250"))
                row = 0
                y = th // 2
                while y < h:
                    off = (tw // 2) if (row % 2) else 0  # 奇数行错位半字宽
                    x = x0 + 10 - off
                    while x < x1:
                        painter.drawText(int(x), int(y), text)
                        x += tw
                    y += th
                    row += 1
                # 半区中央的大号标签（更亮，标明左右身份）
                lab = QFont()
                lab.setPointSize(13)
                lab.setBold(True)
                painter.setFont(lab)
                fm2 = painter.fontMetrics()
                painter.setPen(QColor("#8a94a6"))
                lx = x0 + (x1 - x0 - fm2.horizontalAdvance(text)) // 2
                painter.drawText(int(max(x0, lx)), int(h // 2), text)
                painter.setFont(base_font)
        finally:
            painter.end()


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.8.30 完全重写：分割线由独立 overlay 绘制且悬停跟随；模式切换彻底
    重建（无 mask/布局残留）；工具条按钮深底白字；视频 / GIF / 图片统一
    QtMultimedia / QMovie / QPixmap，分割对比全部生效。
    """

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src_path = src
        self._out_path = out
        self._kind = "image"
        self.setWindowTitle(tr("upscale.compare.title"))
        # 显式补上最小化/最大化，恢复原生 -、口、X
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
        for b in (self._splitBtn, self._sideBtn):
            b.setStyleSheet(_BTN_QSS)
        tb.addWidget(self._splitBtn)
        tb.addWidget(self._sideBtn)

        # v0.8.33：操作提示（Ctrl+滚轮缩放）
        self._zoomHint = QLabel(tr("upscale.compare.zoom_hint"))
        self._zoomHint.setStyleSheet(
            f"color:{tokens.COMPARE_TEXT};font-size:11px;background:transparent;"
        )
        tb.addWidget(self._zoomHint)

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
            b.setStyleSheet(_BTN_QSS)

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

    def showEvent(self, event):
        super().showEvent(event)
        if not hasattr(self, "_loaded"):
            self._loaded = True
            self._load_media()

    # -- 模式 -------------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        self._area.set_mode(mode)
        self._splitBtn.setChecked(mode == _MODE_SPLIT)
        self._sideBtn.setChecked(mode == _MODE_SIDE)

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
