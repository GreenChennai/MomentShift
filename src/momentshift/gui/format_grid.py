"""Format selection matrix for the Convert screen.

One target format is chosen per source category (image / audio / video). The
public API consumed by ``convert_interface`` is preserved:

- ``selectionChanged = Signal(dict)``
- ``setup(categories, selection)``
- ``get_selection() -> dict``
- ``retheme()`` / ``retranslate()``
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QBrush
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from qfluentwidgets import FlowLayout, isDarkTheme

from ..core.qt_compat import Signal
from ..core.presets import TARGET_GROUPS
from ..i18n.translator import tr
from .theme import section_label, muted_text, accent_color, component_bg, RADIUS


class FormatCard(QWidget):
    """A selectable format chip. Emits ``clicked(category, fmt)``."""

    clicked = Signal(str, str)

    def __init__(self, category: str, fmt: str, parent=None):
        super().__init__(parent)
        self.category = category
        self.fmt = fmt
        self._selected = False
        self.setFixedSize(74, 74)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, b: bool):
        self._selected = b
        self.update()

    def _colors(self):
        if self._selected:
            accent = accent_color()
            return accent, "#ffffff" if not isDarkTheme() else "#ffffff"
        border = QColor(200, 200, 200) if not isDarkTheme() else QColor(80, 80, 80)
        text = QColor(90, 90, 90) if not isDarkTheme() else QColor(180, 180, 180)
        return border, text

    def paintEvent(self, event):
        from PyQt6.QtCore import QRect

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        border, text = self._colors()
        if self._selected:
            painter.setBrush(QBrush(border))
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setBrush(QBrush(component_bg()))
            painter.setPen(QPen(border, 1.5))
        painter.drawRoundedRect(QRect(1, 1, w - 2, h - 2), RADIUS, RADIUS)

        painter.setPen(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        # Display as ".Png" (dot prefix, title case) for visual clarity
        display = "." + self.fmt.capitalize()
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, display)

    def mousePressEvent(self, event):
        self.clicked.emit(self.category, self.fmt)
        super().mousePressEvent(event)


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
        for card in self._cards:
            card.update()

    def retranslate(self):
        self.setup(self._categories, self.selection)

    def _clear(self):
        from PyQt6.QtWidgets import QLayout

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
    while layout.count():
        item = layout.takeAt(0)
        # qfluentwidgets FlowLayout.takeAt() returns the widget directly,
        # while other layouts return a QLayoutItem with .widget().
        if isinstance(item, QWidget):
            item.deleteLater()
            continue
        w = item.widget() if hasattr(item, "widget") else None
        if w is not None:
            w.deleteLater()
