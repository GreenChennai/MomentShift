"""Base class for scrollable navigation interfaces."""

from ..core.qt_compat import QWidget, QVBoxLayout, QFrame
from qfluentwidgets import ScrollArea, TitleLabel, CaptionLabel, isDarkTheme
from .theme import content_bg, LIGHT_BG, DARK_BG, accent_name


class InterfaceBase(ScrollArea):
    """A scrollable interface with a consistent, premium title header."""

    def __init__(self, object_name: str, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWidgetResizable(True)

        self.view = QWidget()
        self.view.setObjectName(object_name + "View")
        self.setWidget(self.view)

        self.vbox = QVBoxLayout(self.view)
        self.vbox.setContentsMargins(16, 14, 16, 14)
        self.vbox.setSpacing(12)

        # --- header -------------------------------------------------------
        self.header = QWidget()
        hb = QVBoxLayout(self.header)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(4)
        self.titleLabel = TitleLabel(title)
        hb.addWidget(self.titleLabel)
        if subtitle:
            self.subLabel = CaptionLabel(subtitle)
            hb.addWidget(self.subLabel)
        # subtle accent underline beneath the title
        self.accentRule = QFrame()
        self.accentRule.setFrameShape(QFrame.Shape.HLine)
        self.accentRule.setFixedHeight(3)
        self.accentRule.setFixedWidth(38)
        self._style_accent()
        hb.addWidget(self.accentRule)
        hb.addSpacing(4)
        self.vbox.addWidget(self.header)

        # Apply only the base (scroll-view) theme here. Calling ``self.retheme()``
        # would dispatch to a subclass override that may touch widgets not yet
        # created (the subclass __init__ runs *after* this). Subclasses call their
        # own ``retheme()`` at the end of __init__ to theme their children.
        InterfaceBase.retheme(self)

        # Collapsible-card registry + "always keep at least one open" guard.
        self._collapsibles: list = []
        self._collapse_ready = False

    def register_collapsible(self, card) -> None:
        """Register a CollapsibleCard so the "at least one stays open" rule applies."""
        if card not in self._collapsibles:
            self._collapsibles.append(card)
            card.set_toggle_guard(self._can_collapse)

    def _can_collapse(self, card, want_collapse: bool) -> bool:
        """Guard: refuse to collapse the last expanded (visible) card.

        Returns ``True`` to allow the toggle. Programmatic collapses during
        construction (``_collapse_ready`` is ``False``) are always allowed so
        initial collapsed states work; the rule only protects user interaction
        once the interface is live.
        """
        if not want_collapse or not self._collapse_ready:
            return True
        expanded = [
            c for c in self._collapsibles
            if c.isVisible() and not c.isCollapsed()
        ]
        return len(expanded) > 1

    def _style_accent(self):
        self.accentRule.setStyleSheet(
            f"QFrame{{ background: {accent_name()}; border: none; border-radius: 2px; }}"
        )

    def retheme(self):
        """Apply a solid theme background to the scroll view + viewport.

        Uses an ID-qualified selector so the bare ``background-color`` applies
        ONLY to this view — not to descendant cards/widgets (which caused the
        grey #F4F4F4 halo behind labels inside cards in light mode).
        Labels are forced transparent so the card surface shows through.
        """
        bg = DARK_BG if isDarkTheme() else LIGHT_BG
        oid = self.view.objectName() or "view"
        css = (
            f"#{oid} {{ background-color: {bg.name()}; border: none; }}"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )
        self.view.setStyleSheet(css)
        if self.viewport():
            self.viewport().setStyleSheet(f"background-color: {bg.name()}; border: none;")
        self._style_accent()

    def retranslate(self, title: str = None, subtitle: str = None):
        if title is not None:
            self.titleLabel.setText(title)
        if subtitle is not None and hasattr(self, "subLabel"):
            self.subLabel.setText(subtitle)
