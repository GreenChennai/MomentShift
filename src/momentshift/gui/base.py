"""Base class for scrollable navigation interfaces."""

from ..core.qt_compat import QWidget, QVBoxLayout
from PyQt6.QtGui import QPalette
from qfluentwidgets import ScrollArea, TitleLabel, CaptionLabel, isDarkTheme
from .theme import LIGHT_BG, DARK_BG


class InterfaceBase(ScrollArea):
    """A scrollable interface with a consistent title header."""

    def __init__(self, object_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)

        self.view = QWidget()
        self.setWidget(self.view)
        # The content area must have a solid theme-aware background. Cards from
        # qfluentwidgets are painted as semi-transparent white overlays; they
        # only look like subtle grey cards when the background beneath them is
        # dark. Making everything transparent left the content area white in
        # dark mode (the stacked widget's default background showed through).
        self.retheme()

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

    def retheme(self):
        """Apply a solid theme background to the scroll view and viewport.

        Using a stylesheet is more reliable than QPalette because qfluentwidgets
        may install a global stylesheet that reverts palette colours.
        """
        bg = DARK_BG if isDarkTheme() else LIGHT_BG
        css = f"background-color: {bg.name()}; border: none;"
        self.view.setStyleSheet(css)
        if self.viewport():
            self.viewport().setStyleSheet(css)
        # Do not set a stylesheet on ``self`` (the ScrollArea) so child widgets
        # with their own paintEvent (CardWidget, SettingCard, etc.) keep their
        # semi-transparent overlays instead of inheriting a flat colour.

    def retranslate(self, title: str = None, subtitle: str = None):
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)
