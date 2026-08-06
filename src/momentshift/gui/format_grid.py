"""转换界面的格式选择矩阵。

职责边界：
- 做：每个源分类（图片 / 音频 / 视频）选一个目标格式，向 convert_interface
  暴露选择变化信号与读写接口。
- 不做：不执行转码；不持有文件列表。

依赖：gui/theme；被依赖：gui/convert_interface。

公开 API：
- selectionChanged = Signal(dict)
- setup(categories, selection)
- get_selection() -> dict
- retheme() / retranslate()
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import FlowLayout

from ..core.presets import TARGET_GROUPS
from ..core.qt_compat import Signal
from ..i18n.translator import tr
from .theme import (
    RADIUS,
    accent_color,
    border_hover,
    component_bg,
    section_label,
    text_secondary,
)


class FormatCard(QWidget):
    """可选中的格式方块，点击后发出 ``clicked(category, fmt)``。

    典型用法::

        card = FormatCard("image", "png", parent)
        card.clicked.connect(self._on_format_picked)

    信号：
        clicked(str, str): 参数为 (分类, 格式名)，由外层负责互斥选中态。

    为什么自绘而不是用现成按钮：需要固定 74×74 的方形与半透明主色填充，
    改现成控件的 QSS 反而更难保证各主题下一致。
    """

    clicked = Signal(str, str)

    def __init__(self, category: str, fmt: str, parent=None):
        super().__init__(parent)
        self.category = category
        self.fmt = fmt
        self._selected = False
        self._hover = False  # V0.8.19 优化8：悬停反馈
        self.setFixedSize(74, 74)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, b: bool):
        """设置选中态并触发重绘。

        Args:
            b: True 表示选中（主色填充），False 表示未选中。
        """
        self._selected = b
        self.update()

    def _colors(self):
        """返回 (边框/填充色, 文字色)。

        v0.8.0 ODD-01：未选中分支原为 ``浅色 if not False else 深色`` 的恒真
        三元，深色分支自 v0.7.x 移除深色主题后已是死代码；颜色也全是魔法数。
        改用 theme token（``border_hover()`` / ``text_secondary()``，与
        原来的 (200,200,200)/(90,90,90) 视觉基本一致）。

        V0.8.19 优化8：未选中且悬停时边框提为主色，给鼠标经过明确反馈。
        """
        if self._selected:
            accent = accent_color()
            # 半透明主色填充 + 白字，避免变成一整块实心色导致格式名看不清。
            accent.setAlpha(180)
            return accent, QColor(255, 255, 255)
        if self._hover:
            return accent_color(), QColor(text_secondary())
        return QColor(border_hover()), QColor(text_secondary())

    def paintEvent(self, event):
        """自绘圆角方块与居中的格式名。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        fill, text = self._colors()
        if self._selected:
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(accent_color(), 2))
        else:
            painter.setBrush(QBrush(component_bg()))
            # 悬停时边框加粗与选中态一致（V0.8.19 优化8）
            painter.setPen(QPen(fill, 2 if self._hover else 1.5))
        painter.drawRoundedRect(QRect(1, 1, w - 2, h - 2), RADIUS, RADIUS)

        painter.setPen(text)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        painter.setFont(font)
        # 统一显示成「.PNG」这种带点前缀 + 全大写的样式，与队列 FormatPill /
        # ext_badge 的大小写一致（V0.8.19 优化4），避免同一种格式两种写法
        display = "." + self.fmt.upper()
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, display)

    def mousePressEvent(self, event):
        """按下即发出点击信号，不等待释放，交互更跟手。"""
        self.clicked.emit(self.category, self.fmt)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        """V0.8.19 优化8：悬停反馈（边框提为主色）。"""
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)


class FormatGrid(QWidget):
    selectionChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._categories: list[str] = []
        self.selection: dict[str, str] = {}
        self._cards: list[FormatCard] = []
        self._labels: list[QWidget] = []
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(14)

    def setup(self, categories: list[str], selection: dict[str, str]):
        self._clear()
        self._categories = list(categories)
        self.selection = dict(selection)
        for cat in categories:
            lbl = section_label(tr(f"category.{cat}"))
            self._labels.append(lbl)
            self.vbox.addWidget(lbl)
            flow = FlowLayout()
            flow.setContentsMargins(0, 0, 0, 0)
            flow.setVerticalSpacing(10)
            flow.setHorizontalSpacing(10)
            for fmt in TARGET_GROUPS.get(cat, []):
                card = FormatCard(cat, fmt)
                card.set_selected(self.selection.get(cat) == fmt)
                card.clicked.connect(self._on_card)
                self._cards.append(card)
                flow.addWidget(card)
            self.vbox.addLayout(flow)
        self.vbox.addStretch(1)

    def _on_card(self, cat: str, fmt: str):
        self.selection[cat] = fmt
        for card in self._cards:
            if card.category == cat:
                card.set_selected(card.fmt == fmt)
        self.selectionChanged.emit(dict(self.selection))

    def get_selection(self) -> dict[str, str]:
        return dict(self.selection)

    def retheme(self):
        """主题切换后重绘全部格式卡片（颜色由 _colors() 实时取用）。"""
        for card in self._cards:
            card.update()

    def retranslate(self):
        """语言切换后整体重建，分类标题才能跟着变。"""
        self.setup(self._categories, self.selection)

    def _clear(self):
        """清空当前所有分类标题与格式卡片，供重建前调用。"""
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            child = item.widget()
            if child:
                child.deleteLater()
            lay = item.layout()
            if lay:
                _clear_layout(lay)
                lay.deleteLater()
        self._cards.clear()
        self._labels.clear()


def _clear_layout(layout):
    """递归清空布局并销毁其中的控件。

    Args:
        layout: 任意 QLayout，也兼容 qfluentwidgets 的 FlowLayout。

    Notes:
        踩坑教训：qfluentwidgets 的 FlowLayout.takeAt() 直接返回控件本身，
        而 Qt 原生布局返回的是带 .widget() 的 QLayoutItem。这里必须先做
        类型判断，否则对 FlowLayout 调用 .widget() 会直接抛异常。
    """
    while layout.count():
        item = layout.takeAt(0)
        if isinstance(item, QWidget):
            item.deleteLater()
            continue
        w = item.widget() if hasattr(item, "widget") else None
        if w is not None:
            w.deleteLater()
