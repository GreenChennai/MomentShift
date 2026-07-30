"""Drag-and-drop / click-to-pick input zone, shared by Convert/Compress/Upscale.

Only the inner dashed zone changes colour on press (and restores on release);
the surrounding card itself never changes colour, so a click feels like a
button press on the drop zone — not a full-card flash.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout
from qfluentwidgets import FluentIcon as FIF, StrongBodyLabel, CaptionLabel, isDarkTheme

from ..core.qt_compat import Signal, QDragEnterEvent, QDropEvent
from .theme import (
    ThemedCard, muted_text, accent_name, surface, surface_pressed, border_color,
    placeholder_text,
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

        self.inner = QWidget(self)
        self.inner.setObjectName("dropInner")
        vb = QVBoxLayout(self.inner)
        vb.setContentsMargins(16, 20, 16, 20)
        vb.setSpacing(8)
        vb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.iconLabel = QLabel(self)
        self.iconLabel.setObjectName("dropIcon")
        vb.addWidget(self.iconLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.titleLabel = StrongBodyLabel()
        self.titleLabel.setObjectName("dropTitle")
        vb.addWidget(self.titleLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.hintLabel = CaptionLabel()
        self.hintLabel.setObjectName("dropHint")
        self.hintLabel.setStyleSheet(f"color: {muted_text()};")
        vb.addWidget(self.hintLabel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.formatsLabel = CaptionLabel()
        self.formatsLabel.setObjectName("dropFormats")
        self.formatsLabel.setStyleSheet(f"color: {muted_text()};")
        vb.addWidget(self.formatsLabel, alignment=Qt.AlignmentFlag.AlignCenter)

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

    def retheme(self):
        self.iconLabel.setPixmap(
            FIF.FOLDER_ADD.icon(QColor(placeholder_text())).pixmap(42, 42))
        self._apply_style()

    def _apply_style(self) -> None:
        """Repaint the inner dashed zone (border + press background)."""
        border = accent_name() if self._hover else border_color()
        bg = surface_pressed().name() if self._pressed else surface().name()
        self.inner.setStyleSheet(
            f"#dropInner{{ border: 2px dashed {border}; "
            f"border-radius: 12px; background: {bg}; }}"
        )

    # -- text -------------------------------------------------------------
    def retranslate(self, title: str = "", hint: str = "", formats: str = ""):
        if title:
            self.titleLabel.setText(title)
        if hint:
            self.hintLabel.setText(hint)
        if formats:
            self.formatsLabel.setText(formats)

    # -- interaction ------------------------------------------------------
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._pressed = True
        self._apply_style()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._pressed = False
        self._apply_style()
        self.clicked.emit()

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
