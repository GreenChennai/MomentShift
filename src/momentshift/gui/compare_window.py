"""放大前后对比弹窗（v0.7.8 全权重构）。

窗口上半是分割对比图，下半是滑块控制栏。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QSlider,
    QPushButton,
)
from qfluentwidgets import CaptionLabel

from ..i18n.translator import tr
from .theme import accent_color


# --------------------------------------------------------------------------
class _CompareView(QWidget):
    """分割对比绘制区域。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QPixmap | None = None
        self._out: QPixmap | None = None
        self._split = 0.5
        self.setMinimumHeight(300)
        self.setStyleSheet("background: #0d0d0d;")

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

        # 缩放时保持等比，取能完整容纳的尺寸
        src_scaled = self._src.scaled(
            view_w, view_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        out_scaled = self._out.scaled(
            view_w, view_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)

        # 左上角偏移（居中安置）
        sx = margin + (view_w - src_scaled.width()) // 2
        sy = margin + (view_h - src_scaled.height()) // 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        split_x = int(sx + src_scaled.width() * self._split)

        # 左半：原图
        painter.save()
        painter.setClipRect(QRect(sx, sy, split_x - sx, view_h))
        painter.drawPixmap(QPoint(sx, sy), src_scaled)
        painter.restore()

        # 右半：放大图
        painter.save()
        painter.setClipRect(QRect(split_x, sy, sx + src_scaled.width() - split_x, view_h))
        painter.drawPixmap(QPoint(sx, sy), out_scaled)
        painter.restore()

        # 分割线
        pen = QPen(QColor(accent_color().name()), 2)
        painter.setPen(pen)
        painter.drawLine(split_x, sy - 4, split_x, sy + src_scaled.height() + 4)
        painter.end()


# --------------------------------------------------------------------------
class CompareWindow(QDialog):
    """放大前后对比窗口。

    v0.7.8 重构：对比绘制移入 `_CompareView`，修复重叠与坐标错误。
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

        # 图片对比区
        self._view = _CompareView()
        root.addWidget(self._view, 1)

        # 控制栏
        ctrl = QWidget()
        ctrl.setFixedHeight(56)
        ctrl.setStyleSheet(
            "background:#1e1e1e; border-top:1px solid #333;"
        )
        ch = QHBoxLayout(ctrl)
        ch.setContentsMargins(20, 0, 20, 0)
        ch.setSpacing(14)

        ch.addWidget(QLabel(tr("upscale.compare.original")))
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setValue(50)
        self._slider.setFixedWidth(220)
        self._slider.valueChanged.connect(self._on_slider)
        ch.addWidget(self._slider)
        ch.addWidget(QLabel(tr("upscale.compare.upscaled")))

        ch.addStretch(1)

        reset = QPushButton(tr("upscale.compare.reset"))
        reset.setStyleSheet(
            "QPushButton{background:#333;color:#ccc;border:none;"
            "border-radius:6px;padding:6px 14px;font-size:12px;}"
            "QPushButton:hover{background:#444;}")
        reset.clicked.connect(self._reset)
        ch.addWidget(reset)
        root.addWidget(ctrl)

    def showEvent(self, event):
        """弹窗出现时加载图片。"""
        super().showEvent(event)
        if not hasattr(self, "_loaded"):
            self._loaded = True
            self._load_images()

    def _on_slider(self, val):
        self._view.set_split(val / 100.0)

    def _reset(self):
        self._slider.setValue(50)

    # ----- 外部调用入口 -----
    @classmethod
    def show_compare(cls, src: str, out: str, parent=None):
        w = cls(parent)
        w._src = src
        w._out = out
        w._load_images()
        w.exec()

    def _load_images(self):
        src = QPixmap(self._src_path)
        out = QPixmap(self._out_path)
        if src.isNull():
            src = QPixmap(1, 1)
        if out.isNull():
            out = QPixmap(1, 1)
        self._view.set_images(src, out)
