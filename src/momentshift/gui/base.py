"""Base class for scrollable navigation interfaces."""

from ..core.qt_compat import QWidget, QVBoxLayout
from qfluentwidgets import ScrollArea, TitleLabel, CaptionLabel


class InterfaceBase(ScrollArea):
    """A scrollable interface with a consistent title header."""

    def __init__(self, object_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)

        self.view = QWidget()
        self.setWidget(self.view)
        # Let the FluentWindow's themed background show through so dark mode
        # tints the whole content area (not just the navigation panel).
        self.setStyleSheet("background-color: transparent;")
        self.view.setStyleSheet("background-color: transparent;")
        # The scroll viewport owns its own background; make it transparent too
        # or it would paint a default grey over the themed window behind it.
        if self.viewport():
            self.viewport().setStyleSheet("background-color: transparent;")

        self.vbox = QVBoxLayout(self.view)
        self.vbox.setContentsMargins(30, 22, 30, 22)
        self.vbox.setSpacing(14)

        self.header = QWidget()
        hb = QVBoxLayout(self.header)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        self.titleLabel = TitleLabel(title)
        hb.addWidget(self.titleLabel)
        if subtitle:
            self.subLabel = CaptionLabel(subtitle)
            hb.addWidget(self.subLabel)
        self.vbox.addWidget(self.header)

    def retranslate(self, title: str = None, subtitle: str = None):
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)
