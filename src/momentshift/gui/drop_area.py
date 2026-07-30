"""Drag-and-drop / click-to-pick input zone, shared by Convert/Compress/Upscale.

Premium rebuild (v0.2.4):
- A soft circular icon badge with an accent tint.
- Format chips (parsed from the locale sentence) for an at-a-glance summary.
- A dashed inner zone whose border turns accent on hover and whose fill deepens
  on press, so a click reads as a button press on the drop zone.
- A subtle drop shadow for depth that follows the active theme.
"""

from __future__ import annotations

import re

from PyQt6.QtGui import QColor, QPixmap, QPainter, QPen, QRegion
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout,
)
from qfluentwidgets import FluentIcon as FIF, StrongBodyLabel, CaptionLabel, isDarkTheme

from ..core.qt_compat import Signal, QDragEnterEvent, QDropEvent
from .theme import (
    ThemedCard, muted_text, accent_name, surface, surface_pressed, border_color,
    placeholder_text, surface_raised,
)


class DropArea(ThemedCard):
    """A dashed drop zone. Emits ``filesDropped`` (list of paths) and ``clicked``."""

    filesDropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(14)
        self.setAcceptDrops(True)
        self._hover = False
        self._pressed = False
        self._formats = ""

        # NOTE: a QGraphicsDropShadowEffect is intentionally NOT used here — it
        # cannot coexist with a widget mask (the effect draws outside the widget
        # rect and gets clipped away) and its blur kernel produces a light fringe
        # at the rounded corners. Strict rounding + circular masking (below) give
        # clean, closed corners instead (v0.2.6, #6).

        # --- inner dashed zone (the only part that changes on hover/press) ---
        self.inner = QWidget(self)
        self.inner.setObjectName("dropInner")
        vb = QVBoxLayout(self.inner)
        vb.setContentsMargins(18, 22, 18, 22)
        vb.setSpacing(10)
        vb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # circular accent icon badge
        self.iconBadge = QLabel(self)
        self.iconBadge.setFixedSize(62, 62)
        self.iconBadge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Strict circular clipping: the badge background must stay inside the
        # circle (no rectangular colour block leaking past the round edge, #6).
        self.iconBadge.setMask(
            QRegion(self.iconBadge.rect(), QRegion.RegionType.Ellipse))
        vb.addWidget(self.iconBadge, alignment=Qt.AlignmentFlag.AlignCenter)

        self.titleLabel = StrongBodyLabel()
        self.titleLabel.setObjectName("dropTitle")
        vb.addWidget(self.titleLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.hintLabel = CaptionLabel()
        self.hintLabel.setObjectName("dropHint")
        # Hint text is explicitly black per design spec (v0.2.6, #5).
        self.hintLabel.setStyleSheet("color: #212121;")
        vb.addWidget(self.hintLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        # format chips row
        self.chipsWrap = QWidget(self)
        # Transparent background prevents any inherited colour from showing
        # between format capsules or at rounded corners (v0.3.1, #4).
        self.chipsWrap.setStyleSheet("background: transparent;")
        self.chipsLayout = QHBoxLayout(self.chipsWrap)
        self.chipsLayout.setContentsMargins(0, 0, 0, 0)
        self.chipsLayout.setSpacing(6)
        vb.addWidget(self.chipsWrap, alignment=Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.inner)

        self.retheme()

    # -- theming ----------------------------------------------------------
    def _normalBackgroundColor(self):
        # The card itself stays a stable surface colour — only ``inner`` reacts.
        return surface()

    def _hoverBackgroundColor(self):
        return surface()

    def _pressedBackgroundColor(self):
        return surface()

    def _accent_tint(self) -> str:
        c = QColor(accent_name())
        return f"rgba({c.red()},{c.green()},{c.blue()},0.14)"

    def retheme(self):
        self.iconBadge.setPixmap(
            FIF.FOLDER_ADD.icon(QColor(accent_name())).pixmap(30, 30))
        self.iconBadge.setStyleSheet(
            f"background: {self._accent_tint()}; border-radius: 31px;")
        self._render_chips(self._parse_formats(self._formats))
        self._apply_style()

    def _apply_style(self) -> None:
        """Repaint the inner dashed zone (border + press background)."""
        if self._pressed:
            border = accent_name()
            bg = surface_pressed().name()
        elif self._hover:
            border = accent_name()
            bg = self._accent_tint()
        else:
            border = border_color()
            bg = surface().name()
        self.inner.setStyleSheet(
            f"#dropInner{{ border: 2px dashed {border}; "
            f"border-radius: 12px; background: {bg}; }}"
        )

    # -- format chips -----------------------------------------------------
    @staticmethod
    def _parse_formats(text: str) -> list[str]:
        t = text
        for p in ("支持", "Supports", " supports"):
            t = t.replace(p, "")
        parts = re.split(r"[·•、,，\s]+", t)
        return [p.strip() for p in parts if p.strip()]

    def _render_chips(self, tokens: list[str]) -> None:
        while self.chipsLayout.count():
            w = self.chipsLayout.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not tokens:
            self.chipsWrap.hide()
            return
        self.chipsWrap.show()
        # Format capsules: solid brand-green background with white text (v0.2.6, #5).
        bg = accent_name()
        for tok in tokens:
            chip = QLabel(tok)
            chip.setObjectName("dropChip")
            chip.setStyleSheet(
                f"QLabel#dropChip{{ color: #FFFFFF; background: {bg};"
                f" border-radius: 6px; padding: 2px 9px; font-size: 12px; }}")
            self.chipsLayout.addWidget(chip)
        self.chipsLayout.addStretch(1)

    # -- text -------------------------------------------------------------
    def retranslate(self, title: str = "", hint: str = "", formats: str = ""):
        if title:
            self.titleLabel.setText(title)
        if hint:
            self.hintLabel.setText(hint)
        if formats:
            self._formats = formats
            self._render_chips(self._parse_formats(formats))

    # -- interaction ------------------------------------------------------
    def enterEvent(self, event):
        self._hover = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._pressed = True
        self._apply_style()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._pressed = False
        self._apply_style()
        # Emit synchronously — callers (_pick_files etc.) are responsible for
        # protecting against re-entrancy from the modal dialog's inner event
        # loop (v0.3.0: per-caller _picking guard instead of the broken timer).
        self.clicked.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # v0.3.1: REMOVED setMask — it makes corners truly transparent,
        # revealing the white view background behind the card. ThemedCard's
        # paintEvent already draws rounded rect borders; CSS border-radius
        # handles the visual rounding without transparency artifacts.

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover = True
            self._apply_style()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._hover = False
        self._pressed = False
        self._apply_style()
        super().dragLeaveEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        self._hover = False
        self._pressed = False
        self._apply_style()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            event.ignore()
