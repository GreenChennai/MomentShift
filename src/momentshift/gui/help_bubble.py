"""Shared help popover (replaces QMessageBox).

Why a custom dialog instead of ``QMessageBox``?
- ``QMessageBox`` plays a system sound on Windows when shown with an
  Information icon. The help popovers are frequent and should be silent.
- A custom ``QDialog`` lets us lay out the text beautifully (card + divider
  + green title) instead of the plain default box.

Public API:
- ``HelpDialog(text, parent=None)`` — beautified, sound-less popover.
- ``attach_help(field_row, help_key, parent=None)`` — append a grey help
  button to the right of a ``field_row``; click opens ``HelpDialog``.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from qfluentwidgets import FluentIcon as FIF, TransparentToolButton

from ..i18n.translator import tr
from .theme import muted_text, accent_color


class HelpDialog(QDialog):
    """Beautified parameter-help popover (no system sound)."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("advanced.help"))
        self.setMinimumWidth(340)
        self.setModal(True)
        self.setStyleSheet("QDialog{ background:#ffffff; border-radius:12px; }")

        vb = QVBoxLayout(self)
        vb.setContentsMargins(22, 20, 22, 20)
        vb.setSpacing(14)

        # 标题行：图标 + 标题
        hb = QHBoxLayout()
        hb.setSpacing(10)
        ico = QLabel()
        ico.setPixmap(FIF.INFO.icon(accent_color()).pixmap(22, 22))
        ico.setFixedSize(22, 22)
        hb.addWidget(ico)
        title = QLabel(tr("advanced.help"))
        title.setStyleSheet("font-size:15px; font-weight:700; color:#212121;")
        hb.addWidget(title)
        hb.addStretch(1)
        vb.addLayout(hb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{muted_text()};")
        vb.addWidget(sep)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet(
            "font-size:13px; color:#424242; line-height:1.7; background:transparent;")
        vb.addWidget(body)

        rb = QHBoxLayout()
        rb.addStretch(1)
        ok = QPushButton(tr("common.ok"))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(
            "QPushButton{ background:#238636; color:#ffffff; border:none;"
            " border-radius:8px; padding:8px 22px; font-weight:600; font-size:13px; }"
            "QPushButton:hover{ background:#2ea043; }"
            "QPushButton:pressed{ background:#196c2e; }")
        ok.clicked.connect(self.accept)
        rb.addWidget(ok)
        vb.addLayout(rb)


def attach_help(field_row_widget, help_key: str, parent=None):
    """Append a grey help button to the right of ``field_row_widget``.

    Clicking it opens ``HelpDialog`` populated from ``tr(help_key)``.
    """
    btn = TransparentToolButton(FIF.HELP.icon(color=QColor("#888888")), parent)
    btn.setFixedSize(20, 20)
    btn.setToolTip(tr("advanced.help"))

    def _show():
        dlg = HelpDialog(tr(help_key), parent)
        dlg.exec()

    btn.clicked.connect(_show)
    field_row_widget.layout().addWidget(btn)
