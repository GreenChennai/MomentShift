"""Format-selection dialogs.

- :class:`FormatChoiceDialog` — single file: pick a target format from the
  **same category only** (image -> image, video -> video, audio -> audio).
- :class:`BatchFormatDialog` — batch: pick a target format per media category
  present in the queue (image / audio / video independently).
"""

from __future__ import annotations

from ..core.qt_compat import QWidget, QHBoxLayout, QVBoxLayout, Signal
from qfluentwidgets import Dialog, ComboBox, StrongBodyLabel
from ..core.presets import TARGET_GROUPS
from ..i18n.translator import tr


CATEGORY_LABEL = {
    "image": tr("convert.category.image"),
    "audio": tr("convert.category.audio"),
    "video": tr("convert.category.video"),
}


def _set_combo_current(combo: ComboBox, fmt: str) -> None:
    for i in range(combo.count()):
        combo.setCurrentIndex(i)
        if combo.currentData() == fmt:
            break


class FormatChoiceDialog(Dialog):
    """Single-file target format picker (same-category formats only)."""

    def __init__(self, category: str, current: str | None = None, parent=None):
        super().__init__(
            tr("convert.dialog.format.title"),
            tr("convert.dialog.format.hint"),
            parent,
        )
        self.combo = ComboBox()
        self.combo.setFixedWidth(170)
        for fmt in TARGET_GROUPS.get(category, []):
            self.combo.addItem(fmt.upper(), userData=fmt)
        if current and current in TARGET_GROUPS.get(category, []):
            _set_combo_current(self.combo, current)
        else:
            self.combo.setCurrentIndex(0)
        # Insert the combo between the hint text and the buttons.
        self.textLayout.insertWidget(1, self.combo)

    def get_format(self) -> str:
        return self.combo.currentData()


class BatchFormatDialog(Dialog):
    """Per-category target format picker for a batch queue."""

    def __init__(self, tasks, parent=None):
        super().__init__(
            tr("convert.dialog.batch.title"),
            tr("convert.dialog.batch.hint"),
            parent,
        )
        cats = sorted({t.category for t in tasks})
        container = QWidget()
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(10)
        self.combos: dict[str, ComboBox] = {}

        for cat in cats:
            row = QHBoxLayout()
            row.addWidget(StrongBodyLabel(CATEGORY_LABEL.get(cat, cat)))
            row.addStretch(1)
            cb = ComboBox()
            cb.setFixedWidth(170)
            for fmt in TARGET_GROUPS.get(cat, []):
                cb.addItem(fmt.upper(), userData=fmt)
            cur = next(
                (t.target_format for t in tasks if t.category == cat),
                TARGET_GROUPS.get(cat, [""])[0],
            )
            _set_combo_current(cb, cur)
            row.addWidget(cb)
            vbox.addLayout(row)
            self.combos[cat] = cb

        self.textLayout.insertWidget(1, container)

    def get_targets(self) -> dict[str, str]:
        return {cat: cb.currentData() for cat, cb in self.combos.items()}
