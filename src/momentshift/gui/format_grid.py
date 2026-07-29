"""Card-style format-selection matrix.

Replaces the old dropdown / checkbox format pickers. Formats are shown as a
grid of square "cards" grouped by media category (image / audio / video). Each
card carries a category icon plus the target extension in large text, so the
available conversions are obvious at a glance. Clicking a card selects the
target format for that category (one selection per category).

The category icons use FluentIcon. To use richer per-format artwork (e.g. the
iconfont set at https://www.iconfont.cn/), drop the SVGs into the resources
folder and extend ``ICON_BY_FORMAT`` below.
"""

from __future__ import annotations

from ..core.qt_compat import QWidget, QVBoxLayout, QGridLayout, QLabel, Signal, Qt
from qfluentwidgets import FluentIcon as FIF, StrongBodyLabel
from ..core.presets import TARGET_GROUPS
from ..i18n.translator import tr

CATEGORY_LABEL = {
    "image": tr("convert.category.image"),
    "audio": tr("convert.category.audio"),
    "video": tr("convert.category.video"),
}

# Default icon per category. Extend with per-format artwork if desired.
ICON_BY_CAT = {
    "image": FIF.PHOTO,
    "audio": FIF.MUSIC,
    "video": FIF.VIDEO,
}

# Grid columns for the matrix.
GRID_COLUMNS = 6

CARD_STYLE = """
FormatCard {
    border: 1.5px solid rgba(128, 128, 128, 0.35);
    border-radius: 12px;
    background: rgba(128, 128, 128, 0.05);
}
FormatCard[selected="true"] {
    border: 2px solid #2080f0;
    background: rgba(32, 128, 240, 0.14);
}
FormatCard:hover {
    border-color: rgba(32, 128, 240, 0.55);
}
"""


class FormatCard(QWidget):
    """A single selectable format button rendered as a card."""

    clicked = Signal(str, str)  # (category, fmt)

    def __init__(self, category: str, fmt: str, icon, parent=None):
        super().__init__(parent)
        self.category = category
        self.fmt = fmt
        self.setFixedSize(96, 82)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", "false")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_label = QLabel()
        icon_label.setPixmap(icon.icon().pixmap(30, 30))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label = StrongBodyLabel(fmt.upper())
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(icon_label)
        layout.addWidget(name_label)

    def set_selected(self, value: bool) -> None:
        self.setProperty("selected", "true" if value else "false")
        self.style().polish(self)

    def mousePressEvent(self, event):
        self.clicked.emit(self.category, self.fmt)
        super().mousePressEvent(event)


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
        self.setStyleSheet(CARD_STYLE)

    def setup(self, categories: list[str], selection: dict[str, str]) -> None:
        """(Re)build the grid for the given categories.

        ``selection`` maps category -> currently chosen target format.
        """
        self._clear()
        self._cards = {}
        self._selection = dict(selection)

        for cat in categories:
            section = QWidget()
            vbox = QVBoxLayout(section)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(8)

            header = StrongBodyLabel(CATEGORY_LABEL.get(cat, cat))
            vbox.addWidget(header)

            grid_widget = QWidget()
            grid = QGridLayout(grid_widget)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(10)
            grid.setColumnStretch(GRID_COLUMNS - 1, 1)

            cards: list[FormatCard] = []
            for i, fmt in enumerate(TARGET_GROUPS.get(cat, [])):
                card = FormatCard(cat, fmt, ICON_BY_CAT.get(cat, FIF.DOCUMENT))
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

    def retranslate(self):
        # Rebuild labels only if already populated.
        if self._cards:
            self.setup(list(self._cards.keys()), self._selection)
