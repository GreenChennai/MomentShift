"""放大前后对比弹窗（v0.3.5 弹出式，1280×720）。

从 UpscaleInterface 内联控件中提取出来，完成项放大镜图标 → 弹窗。
"""

from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QSlider, QFrame,
    QPushButton,
)
from qfluentwidgets import FluentIcon as FIF, CaptionLabel, StrongBodyLabel

from ..i18n.translator import tr
from .theme import accent_color, accent_name, muted_text

class CompareWindow(QDialog):
    """1280×720 放大前后对比窗口。顶部分割对比 + 底部信息栏。"""

    def __init__(self, src: str, out: str, parent=None):
        super().__init__(parent)
        self._src = src
        self._out = out
        self._split = 0.5  # 分割比例（0=全原图, 1=全放大）

        self.setWindowTitle(tr("upscale.compare.title"))
        self.resize(1280, 740)
        self.setMinimumSize(900, 560)
        self.setStyleSheet(
            "CompareWindow { background-color: #1a1a1a; }"
        )

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 图片区域（占据主要空间）
        self.view = QWidget()
        self.view.setMinimumHeight(400)
        self.view.setStyleSheet("background: #0d0d0d;")
        root.addWidget(self.view, 1)

        # 控制栏
        ctrl = QWidget()
        ctrl.setFixedHeight(60)
        ctrl.setStyleSheet(
            "QWidget{ background: #1e1e1e; border-top: 1px solid #333; }"
            "QLabel{ color: #ccc; }"
        )
        ch = QHBoxLayout(ctrl)
        ch.setContentsMargins(20, 0, 20, 0)
        ch.setSpacing(16)

        self.titleLbl = CaptionLabel(tr("upscale.compare.title"))
        ch.addWidget(self.titleLbl)
        ch.addStretch(1)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(50)
        self.slider.setFixedWidth(200)
        self.slider.valueChanged.connect(self._on_split)
        ch.addWidget(QLabel(tr("upscale.compare.original")))
        ch.addWidget(self.slider)
        ch.addWidget(QLabel(tr("upscale.compare.upscaled")))

        rc = QPushButton(tr("upscale.compare.reset"))
        rc.setStyleSheet(
            "QPushButton{ background: #333; color: #ccc; border: none;"
            " border-radius: 6px; padding: 6px 14px; font-size: 12px; }"
            "QPushButton:hover{ background: #444; }")
        rc.clicked.connect(self._reset)
        ch.addWidget(rc)
        root.addWidget(ctrl)

        # 加载图片
        self._load_images()

    def _load_images(self):
        self._src_pix = QPixmap(self._src)
        self._out_pix = QPixmap(self._out)
        if self._src_pix.isNull():
            self._src_pix = QPixmap(1, 1)
        if self._out_pix.isNull():
            self._out_pix = QPixmap(1, 1)
        self.view.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # 在 view 区域绘制分割对比
        r = self.view.geometry()
        # 转换为 view 的本地坐标
        vr = QRect(0, r.y(), self.width(), r.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 计算缩放后的图片区域
        margin = 20
        draw_x = margin
        draw_w = self.width() - 2 * margin
        draw_y = r.y() + margin
        draw_h = r.height() - 2 * margin

        # 绘制原图（左侧）
        split_x = int((self.width() - 2 * margin) * self._split + margin)
        if split_x > draw_x:
            src_clip = QRect(draw_x - draw_x, 0, split_x - draw_x,
                             self._src_pix.height())
            painter.drawPixmap(
                QRect(draw_x, draw_y, split_x - draw_x, draw_h),
                self._src_pix.scaled(draw_w, draw_h, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))

        # 绘制放大图（右侧）
        if split_x < draw_x + draw_w:
            painter.drawPixmap(
                QRect(split_x, draw_y, draw_x + draw_w - split_x, draw_h),
                self._out_pix.scaled(draw_w, draw_h, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))

        # 分割线
        pen = QPen(QColor("#238636"), 2)
        painter.setPen(pen)
        painter.drawLine(split_x, r.y(), split_x, r.y() + r.height())

        painter.end()

    def _on_split(self, value):
        self._split = value / 100.0
        self.update()

    def _reset(self):
        self.slider.setValue(50)
        self._split = 0.5
        self.update()
