"""放大前后对比弹窗（v0.7.9 交互重构）。

鼠标在窗口内移动时分割线与指针同步；移出窗口则恢复居中。
删除滑块与重置按钮，改为纯鼠标驱动。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QWidget,
    QPushButton,
)
from qfluentwidgets import CaptionLabel

from ..i18n.translator import tr
from .theme import accent_color, muted_text


# --------------------------------------------------------------------------
class _CompareView(QWidget):
    """分割对比绘制区域（鼠标驱动）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QPixmap | None = None
        self._out: QPixmap | None = None
        self._split = 0.5
        self.setMinimumHeight(300)
        self.setStyleSheet("background: #0d0d0d;")
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
            view_w, view_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        out_scaled = self._out.scaled(
            view_w, view_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)

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


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.7.9：删除滑块/重置按钮，分隔线跟随鼠标移动，移出窗口后恢复居中。
    """

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src_path = src
        self._out_path = out
        self.setWindowTitle(tr("upscale.compare.title"))
        self.resize(1100, 740)
        self.setMinimumSize(800, 500)
        self.setStyleSheet("CompareWindow{background:#1e1e1e;}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏（含全屏/最小化按钮）
        title_bar = QWidget()
        title_bar.setFixedHeight(40)
        title_bar.setStyleSheet("background:#1e1e1e;border-bottom:1px solid #333;")
        th = QHBoxLayout(title_bar)
        th.setContentsMargins(16, 0, 12, 0)
        th.setSpacing(8)
        t = QLabel(tr("upscale.compare.title"))
        t.setStyleSheet("color:#ccc;font-size:13px;font-weight:600;background:transparent;")
        th.addWidget(t)
        th.addStretch(1)

        # 全屏/还原
        self._fullscreen = False
        fs = QPushButton("全屏")
        fs.setStyleSheet(self._btn_style())
        fs.clicked.connect(self._toggle_fullscreen)
        th.addWidget(fs)
        # 最小化
        mi = QPushButton("—")
        mi.setStyleSheet(self._btn_style())
        mi.clicked.connect(self.showMinimized)
        th.addWidget(mi)
        # 关闭
        cl = QPushButton("×")
        cl.setStyleSheet(self._btn_style())
        cl.clicked.connect(self.close)
        th.addWidget(cl)
        root.addWidget(title_bar)

        # 图片对比区
        self._view = _CompareView()
        root.addWidget(self._view, 1)

        # 设置鼠标事件
        self._view.mouseMoveEvent = self._on_mouse_move
        self._view.leaveEvent = self._on_mouse_leave
        self._view.enterEvent = self._on_mouse_enter

    def _btn_style(self):
        return (
            "QPushButton{background:#333;color:#ccc;border:none;"
            "border-radius:4px;padding:4px 12px;font-size:12px;}"
            "QPushButton:hover{background:#444;}")

    def showEvent(self, event):
        super().showEvent(event)
        if not hasattr(self, "_loaded"):
            self._loaded = True
            self._load_images()

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

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.showFullScreen()
        else:
            self.showNormal()

    def _load_images(self):
        src = QPixmap(self._src_path)
        out = QPixmap(self._out_path)
        if src.isNull():
            src = QPixmap(1, 1)
        # v0.7.9 调整2：输出文件不存在时，用源图占位并提示
        if out.isNull():
            out = src.copy()
        self._view.set_images(src, out)

    # ----- 外部调用入口（保持兼容）-----
    @classmethod
    def show_compare(cls, src: str, out: str, parent=None):
        w = cls(src, out, parent)
        w.exec()
