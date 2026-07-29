"""Card-style format-selection matrix.

Replaces the old dropdown / checkbox format pickers. Formats are shown as a grid
of square "cards" grouped by media category (image / audio / video). Each card is
a custom-painted square with a checkbox indicator (top-left) and a large
``.PNG``-style suffix in the centre. Clicking a card selects the target format for
that category (one selection per category) with a clear checked state and a small
check-draw animation so the user always knows what is selected.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtProperty, QPropertyAnimation, QEasingCurve, Qt, QPointF, QRectF
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont
from ..core.qt_compat import QWidget, QVBoxLayout, QGridLayout, Signal
from qfluentwidgets import isDarkTheme
from ..core.presets import TARGET_GROUPS
from ..i18n.translator import tr

GRID_COLUMNS = 6
ACCENT = QColor(32, 128, 240)
GRAY_BORDER = QColor(128, 128, 128, 95)
GRAY_BORDER_LIGHT = QColor(128, 128, 128, 60)


class FormatCard(QWidget):
    """A single selectable format button rendered as a painted square card."""

    clicked = Signal(str, str)  # (category, fmt)

    def __init__(self, category: str, fmt: str, parent=None):
        super().__init__(parent)
        self.category = category
        self.fmt = fmt
        self._selected = False
        self._hover = False
        self._check = 0.0  # 0..1 check-draw animation progress

        self.setFixedSize(96, 92)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._anim = QPropertyAnimation(self, b"checkValue")
        self._anim.setDuration(170)

    # -- animation property ---------------------------------------------
    def getCheck(self) -> float:
        return self._check

    def setCheck(self, v: float) -> None:
        self._check = v
        self.update()

    checkValue = pyqtProperty(float, getCheck, setCheck)

    def set_selected(self, value: bool) -> None:
        self._selected = value
        self._anim.stop()
        self._anim.setStartValue(self._check)
        self._anim.setEndValue(1.0 if value else 0.0)
        self._anim.setEasingCurve(
            QEasingCurve.Type.OutBack if value else QEasingCurve.Type.InCubic
        )
        self._anim.start()
        self.update()

    # -- events -----------------------------------------------------------
    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit(self.category, self.fmt)
        super().mousePressEvent(event)

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()

        # card background + border
        if self._selected:
            bg = QColor(32, 128, 240, 28)
            border = ACCENT
            bw = 2
        else:
            bg = QColor(128, 128, 128, 13)
            # A faint grey border is invisible on a dark card, so lighten it
            # in dark mode; keep the usual grey in light mode.
            border = ACCENT if self._hover else (
                QColor(255, 255, 255, 40) if isDarkTheme() else GRAY_BORDER
            )
            bw = 2 if self._hover else 1
        p.setPen(QPen(border, bw))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(r.adjusted(1, 1, r.width() - 2, r.height() - 2), 12, 12)

        # checkbox indicator (top-left)
        cb = QRectF(12, 12, 22, 22)
        cb_border = QColor(255, 255, 255, 70) if isDarkTheme() else GRAY_BORDER_LIGHT
        p.setPen(QPen(ACCENT if self._selected else cb_border, 2))
        p.setBrush(QBrush(ACCENT if self._selected else Qt.GlobalColor.transparent))
        p.drawRoundedRect(cb, 6, 6)
        if self._check > 0.01:
            tick = QColor(255, 255, 255) if isDarkTheme() else QColor(20, 20, 20)
            p.setPen(QPen(tick, 3,
                          Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            cx, cy = cb.x() + cb.width() / 2, cb.y() + cb.height() / 2
            s = self._check
            p1 = QPointF(cx - 6, cy)
            p2 = QPointF(cx - 1, cy + 5)
            p3 = QPointF(cx + 7, cy - 5)

            def lerp(a, b, t):
                return a + (b - a) * t

            p.drawLine(
                QPointF(lerp(cx, p1.x(), s), lerp(cy, p1.y(), s)),
                QPointF(lerp(cx, p2.x(), s), lerp(cy, p2.y(), s)),
            )
            p.drawLine(
                QPointF(lerp(cx, p2.x(), s), lerp(cy, p2.y(), s)),
                QPointF(lerp(cx, p3.x(), s), lerp(cy, p3.y(), s)),
            )

        # large suffix text (centre)
        text = "." + self.fmt.upper()
        p.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(20, 20, 20))
        f = QFont()
        f.setBold(True)
        f.setPointSize(20)
        p.setFont(f)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


class FormatGrid(QWidget):
    """Matrix of :class:`FormatCard` grouped by media category."""

    selectionChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selection: dict[str, str] = {}
        self._cards: dict[str, list[FormatCard]] = {}
        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(0, 0, 0, 0)
        self.mainLayout.setSpacing(14)

    def setup(self, categories: list[str], selection: dict[str, str]) -> None:
        """(Re)build the grid for the given categories."""
        self._clear()
        self._cards = {}
        self._selection = dict(selection)

        for cat in categories:
            section = QWidget()
            vbox = QVBoxLayout(section)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(8)

            header = QLabelStyle(tr("convert.category." + cat))
            vbox.addWidget(header)

            grid_widget = QWidget()
            grid = QGridLayout(grid_widget)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(10)
            grid.setColumnStretch(GRID_COLUMNS - 1, 1)

            cards: list[FormatCard] = []
            for i, fmt in enumerate(TARGET_GROUPS.get(cat, [])):
                card = FormatCard(cat, fmt)
                card.set_selected(self._selection.get(cat) == fmt)
                card.clicked.connect(self._on_card)
                grid.addWidget(card, i // GRID_COLUMNS, i % GRID_COLUMNS)
                cards.append(card)
            vbox.addWidget(grid_widget)

            self._cards[cat] = cards
            self.mainLayout.addWidget(section)

    def _on_card(self, cat: str, fmt: str) -> None:
        self._selection[cat] = fmt
        for c in self._cards.get(cat, []):
            c.set_selected(c.fmt == fmt)
        self.selectionChanged.emit(dict(self._selection))

    def get_selection(self) -> dict[str, str]:
        return dict(self._selection)

    def _clear(self) -> None:
        while self.mainLayout.count():
            item = self.mainLayout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()

    def retheme(self) -> None:
        """Repaint every card so theme-aware colours (tick/border) refresh."""
        for card in self.findChildren(FormatCard):
            card.update()

    def retranslate(self):
        if self._cards:
            self.setup(list(self._cards.keys()), self._selection)


def QLabelStyle(text: str):
    """A simple strong body label for section headers (avoids extra import)."""
    from qfluentwidgets import StrongBodyLabel
    return StrongBodyLabel(text)
