"""放大前后对比弹窗。

职责边界：
- 做：以独立弹窗展示放大前后对比；图片走鼠标分割，视频/GIF 走双栏并排播放。
- 不做：不解码封面图（图片直接用 QPixmap；视频/GIF 用播放器渲染）。

依赖：gui/theme、i18n/translator；被依赖：gui/upscale_interface。

鼠标在窗口内移动时分割线与指针同步；移出窗口则恢复居中。

v0.8.24 重构：
- 支持视频 / GIF 对比（此前 QPixmap 加载不了，显示一片绿色背景）。
  图片用「分割对比」，视频/GIF 用「左右双栏并排同步播放」——分割遮罩
  依赖对原生窗口（QVideoWidget）做 z-order / 裁剪，Windows 下不可靠，
  双栏并排是跨平台稳定的折中。
- 删除窗口内自定义标题栏（标题文字 + 全屏 / 最小化 / 关闭按钮），
  恢复 QDialog 原生窗口按钮（-、口、X）。
- 窗口尺寸调整为 1280×720。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, Qt, QUrl
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..i18n.translator import tr
from . import tokens
from .theme import accent_color

# 视频扩展名（走播放器）；其余按图片 / GIF 处理
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv",
    ".m4v", ".3gp", ".ts", ".mts", ".m2ts",
}


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXTS


def _is_gif(path: str) -> bool:
    return Path(path).suffix.lower() == ".gif"


# --------------------------------------------------------------------------
class _CompareView(QWidget):
    """分割对比绘制区域（鼠标驱动，静态图片模式）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QPixmap | None = None
        self._out: QPixmap | None = None
        self._split = 0.5
        self.setMinimumHeight(300)
        self.setStyleSheet(f"background: {tokens.COMPARE_BG};")
        self.setMouseTracking(True)

    def set_images(self, src: QPixmap, out: QPixmap):
        self._src = src
        self._out = out
        self.update()

    def set_split(self, val: float):
        self._split = max(0.0, min(1.0, val))
        self.update()

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        if w < 4 or h < 4 or self._src is None or self._out is None:
            return
        margin = 20
        view_w = w - 2 * margin
        view_h = h - 2 * margin
        if view_w < 10 or view_h < 10:
            return

        src_scaled = self._src.scaled(
            view_w,
            view_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        out_scaled = self._out.scaled(
            view_w,
            view_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        sx = margin + (view_w - src_scaled.width()) // 2
        sy = margin + (view_h - src_scaled.height()) // 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        split_x = int(sx + src_scaled.width() * self._split)

        painter.save()
        painter.setClipRect(QRect(sx, sy, split_x - sx, view_h))
        painter.drawPixmap(QPoint(sx, sy), src_scaled)
        painter.restore()

        painter.save()
        painter.setClipRect(QRect(split_x, sy, sx + src_scaled.width() - split_x, view_h))
        painter.drawPixmap(QPoint(sx, sy), out_scaled)
        painter.restore()

        pen = QPen(QColor(accent_color().name()), 2)
        painter.setPen(pen)
        painter.drawLine(split_x, sy - 4, split_x, sy + src_scaled.height() + 4)
        painter.end()


class _MediaPane(QWidget):
    """单侧媒体面板：图片显示静态图，GIF 播放 QMovie，视频播放 QMediaPlayer。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._vb = QVBoxLayout(self)
        self._vb.setContentsMargins(0, 0, 0, 0)
        self._vb.setSpacing(6)

        cap = QLabel(title)
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(
            f"color:{tokens.COMPARE_TEXT};font-size:{tokens.FONT_SMALL}px;"
            f"font-weight:600;background:transparent;"
        )
        self._vb.addWidget(cap)

        self._video: QVideoWidget | None = None
        self._player: QMediaPlayer | None = None
        self._movie = None
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("background: transparent; border: none;")
        self._vb.addWidget(self._label, 1)

    def load(self, path: str, is_gif: bool) -> None:
        if not path or not Path(path).exists():
            self._label.setText("")
            return
        if _is_video(path) and not is_gif:
            # 视频：播放器
            self._video = QVideoWidget(self)
            self._video.setStyleSheet("background: transparent; border: none;")
            self._player = QMediaPlayer(self)
            self._player.setVideoOutput(self._video)
            self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self._player.play()
            self._vb.insertWidget(1, self._video, 1)
            self._label.hide()
            return
        if is_gif:
            from PyQt6.QtGui import QMovie  # noqa: PLC0415 - 局部导入

            self._movie = QMovie(path)
            self._movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self._label.setMovie(self._movie)
            self._movie.start()
            self._label.show()
            return
        # 静态图片
        pm = QPixmap(path)
        if pm.isNull():
            self._label.setText("")
        else:
            self._label.setPixmap(
                pm.scaled(
                    self._label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self._label.show()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
        if self._movie is not None:
            self._movie.stop()


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.7.9：删除滑块/重置按钮，分隔线跟随鼠标移动，移出窗口后恢复居中。
    v0.8.24：支持视频/GIF 对比（双栏并排播放）；删除窗口内自定义标题栏，
    恢复原生窗口按钮；窗口 1280×720。
    """

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src_path = src
        self._out_path = out
        self.setWindowTitle(tr("upscale.compare.title"))
        self.resize(1280, 720)
        self.setMinimumSize(960, 540)
        self.setStyleSheet(f"CompareWindow{{background:{tokens.COMPARE_SURFACE};}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._is_dynamic = False
        self._view = _CompareView()
        self._src_pane: _MediaPane | None = None
        self._out_pane: _MediaPane | None = None

        # 动态媒体（视频/GIF）：左右双栏
        self._media_row = QHBoxLayout()
        self._media_row.setContentsMargins(12, 12, 12, 12)
        self._media_row.setSpacing(12)
        self._media_host = QWidget()
        self._media_host.setStyleSheet(f"background: {tokens.COMPARE_BG};")
        self._media_host.setLayout(self._media_row)
        self._media_host.hide()
        root.addWidget(self._media_host, 1)

        # 静态图片对比区
        root.addWidget(self._view, 1)

        # 鼠标事件
        self._view.mouseMoveEvent = self._on_mouse_move
        self._view.leaveEvent = self._on_mouse_leave
        self._view.enterEvent = self._on_mouse_enter

    def showEvent(self, event):
        super().showEvent(event)
        if not hasattr(self, "_loaded"):
            self._loaded = True
            self._load_media()

    def _on_mouse_move(self, event):
        self._split_from_pos(event.pos())

    def _split_from_pos(self, pos):
        w = self._view.width()
        margin = 20
        view_w = w - 2 * margin
        if view_w <= 0:
            return
        rel = (pos.x() - margin) / max(1, view_w)
        self._view.set_split(rel)

    def _on_mouse_enter(self, _event):
        self._view.setCursor(Qt.CursorShape.BlankCursor)

    def _on_mouse_leave(self, _event):
        self._view.unsetCursor()
        self._view.set_split(0.5)

    def _load_media(self):
        """按文件类型装载对比内容。

        图片走静态分割对比；视频 / GIF 走左右双栏并排同步播放（分割遮罩
        依赖原生窗口裁剪，跨平台不可靠，双栏是稳定折中）。
        """
        src = self._src_path
        out = self._out_path
        if not src or not Path(src).exists():
            return

        is_gif = _is_gif(src) or _is_gif(out)
        is_video = _is_video(src) or _is_video(out)
        if is_gif or is_video:
            # out 可能尚未生成（对比时任务刚完成）；动态面板仍创建，
            # load 内部对缺失路径显示空白即可。
            self._is_dynamic = True
            self._view.hide()
            self._media_host.show()
            self._src_pane = _MediaPane(tr("upscale.compare.original"), self._media_host)
            self._out_pane = _MediaPane(tr("upscale.compare.upscaled"), self._media_host)
            self._media_row.addWidget(self._src_pane, 1)
            self._media_row.addWidget(self._out_pane, 1)
            self._src_pane.load(src, is_gif=is_gif)
            self._out_pane.load(out, is_gif=is_gif)
            return

        # 静态图片
        src_pix = QPixmap(src)
        if src_pix.isNull():
            src_pix = QPixmap(1, 1)
        out_pix = QPixmap(out)
        if out_pix.isNull():
            out_pix = src_pix.copy()
        self._view.set_images(src_pix, out_pix)

    def closeEvent(self, event):
        if self._src_pane is not None:
            self._src_pane.stop()
        if self._out_pane is not None:
            self._out_pane.stop()
        super().closeEvent(event)

    # ----- 外部调用入口（保持兼容）-----
    @classmethod
    def show_compare(cls, src: str, out: str, parent=None):
        w = cls(src, out, parent)
        w.exec()
