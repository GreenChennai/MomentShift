"""Centralized, theme-aware design system for MomentShift.

This module is the single source of truth for the UI's visual language:

- Colour tokens (window bg, component surface, hover/press, accent, text).
- ``ThemedCard`` — a ``CardWidget`` that paints a *solid* theme-aware surface so
  labels/icons on it resolve to the card colour (no more #F4F4F4 vs #FBFBFB halo).
- Shared builders (``panel``, ``field_row``, ``section_label``, buttons) so every
  screen composes from the same primitives and stays consistent.
- Two import-time monkey-patches that force transparent label backgrounds (needed
  because qfluentwidgets installs widget-level stylesheets that would otherwise
  paint an opaque default fill behind text inside cards, most visible in dark mode).
"""

from __future__ import annotations

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QFrame

from qfluentwidgets import (
    isDarkTheme,
    qconfig,
    Theme,
    CardWidget,
    CaptionLabel,
    BodyLabel,
    StrongBodyLabel,
    PrimaryPushButton,
    PushButton,
    TransparentPushButton,
    TransparentToolButton,
)

# -- Window chrome + content background --------------------------------------
LIGHT_BG = QColor(244, 244, 244)
DARK_BG = QColor(32, 32, 32)

# Uniform component (card) surface — the colour any transparent text/icon inside
# a card resolves to, so the patch behind a label always matches the card.
COMPONENT_LIGHT = QColor(251, 251, 251)   # #FBFBFB
COMPONENT_DARK = QColor(43, 43, 43)       # #2B2B2B
HOVER_LIGHT = QColor(242, 242, 242)
HOVER_DARK = QColor(54, 54, 54)
PRESS_LIGHT = QColor(236, 236, 236)
PRESS_DARK = QColor(38, 38, 38)

# Brand accent (Fluent primary blue), tuned per theme so it stays vivid on both.
ACCENT_LIGHT = QColor(15, 108, 189)    # #0F6CBD
ACCENT_DARK = QColor(38, 132, 209)     # brighter on dark

# Shared geometry tokens.
RADIUS = 12
SPACING = 12
CARD_MARGIN = 16


def component_bg() -> QColor:
    return COMPONENT_DARK if isDarkTheme() else COMPONENT_LIGHT


def content_bg() -> QColor:
    return DARK_BG if isDarkTheme() else LIGHT_BG


def accent_color() -> QColor:
    return ACCENT_DARK if isDarkTheme() else ACCENT_LIGHT


def accent_name() -> str:
    return accent_color().name()


def sub_text() -> str:
    return "rgba(96, 96, 96, 1)" if not isDarkTheme() else "rgba(165, 165, 165, 1)"


def hint_text() -> str:
    return "rgba(128, 128, 128, 1)" if not isDarkTheme() else "rgba(170, 170, 170, 1)"


def muted_text() -> str:
    return "rgba(140, 140, 140, 1)" if not isDarkTheme() else "rgba(170, 170, 170, 1)"


def map_theme(value: str) -> Theme:
    return {
        "auto": Theme.AUTO,
        "light": Theme.LIGHT,
        "dark": Theme.DARK,
    }.get(value, Theme.AUTO)


class ThemedCard(CardWidget):
    """A ``CardWidget`` that paints a solid theme-aware component colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(RADIUS)
        # Force all child labels to have transparent backgrounds so the card's
        # painted surface shows through (not the view's background #F4F4F4).
        # This card-level stylesheet has higher priority than ancestor rules.
        self.setStyleSheet(
            "ThemedCard > QWidget { background-color: transparent; }"
            "FluentLabelBase, QLabel { background-color: transparent; }"
        )

    def _normalBackgroundColor(self):
        return component_bg()

    def _hoverBackgroundColor(self):
        return HOVER_DARK if isDarkTheme() else HOVER_LIGHT

    def _pressedBackgroundColor(self):
        return PRESS_DARK if isDarkTheme() else PRESS_LIGHT


class CollapsibleCard(ThemedCard):
    """A ``ThemedCard`` with a toggle button in the title bar.

    The title bar stays visible; the body widget can be collapsed/expanded by
    clicking the arrow button or via ``setCollapsed(True/False)``.

    Usage pattern (replaces the ``_card()`` / ``panel()`` pattern):

    .. code-block:: python

        card = CollapsibleCard(tr("my.title"), tr("my.sub"))
        # card.body is a QVBoxLayout – add your content here
        card.body.addWidget(...)
        return card, card.body, card.titleLabel
    """

    def __init__(self, title: str = "", subtitle: str = "",
                 parent=None, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = collapsed

        # Outer layout – zero margins so the card border-radius applies cleanly.
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        # ---- title bar (always visible) ----------------------------------
        self._bar = QWidget()
        hb = QHBoxLayout(self._bar)
        hb.setContentsMargins(CARD_MARGIN, 12, 8, 6)
        hb.setSpacing(6)

        if subtitle:
            tc = QWidget()
            tv = QVBoxLayout(tc)
            tv.setContentsMargins(0, 0, 0, 0)
            tv.setSpacing(2)
            self.titleLabel = StrongBodyLabel(title)
            tv.addWidget(self.titleLabel)
            self.subtitleLabel = CaptionLabel(subtitle)
            tv.addWidget(self.subtitleLabel)
            hb.addWidget(tc, 1)
        else:
            self.titleLabel = StrongBodyLabel(title)
            self.subtitleLabel = None
            hb.addWidget(self.titleLabel, 1)

        hb.addStretch()

        self._toggleBtn = TransparentPushButton("\u25BC")  # ▼
        self._toggleBtn.setFixedSize(28, 28)
        self._toggleBtn.clicked.connect(self.toggle)
        self._toggleBtn.setToolTip("")
        hb.addWidget(self._toggleBtn)

        self._outer.addWidget(self._bar)

        # ---- body (collapsible) ------------------------------------------
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(CARD_MARGIN, 0, CARD_MARGIN, 14)
        self._body_layout.setSpacing(10)
        self._outer.addWidget(self._body)

        if collapsed:
            self._body.setVisible(False)
            self._toggleBtn.setText("\u25B6")  # ▶

    # -- public API ---------------------------------------------------------

    @property
    def body(self) -> QVBoxLayout:
        """The body layout where card content should be added."""
        return self._body_layout

    def toggle(self):
        """Toggle the collapsed state."""
        self.setCollapsed(not self._collapsed)

    def setCollapsed(self, collapsed: bool):
        """Programmatically expand or collapse the body."""
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._toggleBtn.setText("\u25B6" if collapsed else "\u25BC")  # ▶ / ▼

    def isCollapsed(self) -> bool:
        """Return whether the card body is currently hidden."""
        return self._collapsed


# ---------------------------------------------------------------------------
# Shared composition primitives — every screen builds from these so the look
# stays coherent.
# ---------------------------------------------------------------------------
def panel(title: str | None = None, subtitle: str | None = None,
          parent=None, radius: int = RADIUS) -> tuple[ThemedCard, QVBoxLayout]:
    """Create a titled card. Returns ``(card, body_layout)``.

    The body layout already has consistent margins/spacing and an optional
    title + subtitle on top.
    """
    card = ThemedCard(parent)
    card.setBorderRadius(radius)
    vb = QVBoxLayout(card)
    vb.setContentsMargins(CARD_MARGIN, 14, CARD_MARGIN, 14)
    vb.setSpacing(10)
    if title:
        t = StrongBodyLabel(title)
        t.setObjectName("panelTitle")
        vb.addWidget(t)
    if subtitle:
        s = CaptionLabel(subtitle)
        s.setObjectName("panelSub")
        vb.addWidget(s)
    return card, vb


def section_label(text: str, parent=None) -> CaptionLabel:
    """A small, theme-aware section caption used inside cards."""
    lbl = CaptionLabel(text, parent)
    lbl.setObjectName("sectionLabel")
    return lbl


def field_row(label_text: str, control, parent=None, label_width: int = 96) -> QWidget:
    """A left-aligned label + control row (label fixed width, control stretched).

    ``control`` may be a ``QWidget`` or a ``QLayout``.
    """
    from PyQt6.QtWidgets import QLayout

    row = QWidget(parent)
    hb = QHBoxLayout(row)
    hb.setContentsMargins(0, 0, 0, 0)
    hb.setSpacing(12)
    lbl = BodyLabel(label_text)
    lbl.setObjectName("fieldLabel")
    lbl.setFixedWidth(label_width)
    hb.addWidget(lbl)
    if isinstance(control, QLayout):
        hb.addLayout(control, 1)
    else:
        hb.addWidget(control, 1)
    return row


def hint_label(text: str, parent=None) -> BodyLabel:
    """A body label tinted with the muted colour (re-set on each update)."""
    lbl = BodyLabel(text, parent)
    lbl.setObjectName("hintLabel")
    lbl.setStyleSheet(f"color: {muted_text()};")
    return lbl


def divider(parent=None) -> QFrame:
    """A 1px hairline separator tinted with the muted colour."""
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"color: {muted_text()}; background: {muted_text()};")
    return line


def primary_btn(text: str, icon=None, parent=None) -> PrimaryPushButton:
    if icon is not None:
        return PrimaryPushButton(text, icon=icon, parent=parent)
    return PrimaryPushButton(text, parent=parent)


def ghost_btn(text: str, icon=None, parent=None) -> TransparentPushButton:
    if icon is not None:
        return TransparentPushButton(text, icon=icon, parent=parent)
    return TransparentPushButton(text, parent=parent)


def icon_btn(icon, tooltip: str = "", parent=None) -> TransparentToolButton:
    btn = TransparentToolButton(icon, parent)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def scrollbar_qss() -> str:
    """Theme-aware, modern scrollbar stylesheet for QScrollArea."""
    handle = "rgba(140, 140, 140, 0.6)" if not isDarkTheme() else "rgba(160, 160, 160, 0.5)"
    hover = "rgba(120, 120, 120, 0.85)" if not isDarkTheme() else "rgba(180, 180, 180, 0.8)"
    return (
        "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
        "QScrollBar::handle:vertical {"
        f" background: {handle}; border-radius: 4px; min-height: 30px;"
        " }"
        f"QScrollBar::handle:vertical:hover {{ background: {hover}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        "QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }"
        "QScrollBar::handle:horizontal {"
        f" background: {handle}; border-radius: 4px; min-width: 30px;"
        " }"
        f"QScrollBar::handle:horizontal:hover {{ background: {hover}; }}"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }"
    )


# ---------------------------------------------------------------------------
# Workaround #1: FluentLabelBase installs a widget-level stylesheet for colour.
# Once a widget has its own stylesheet, ancestor rules such as
# ``QLabel { background-color: transparent }`` are no longer the only authority,
# and some labels pick up a default fill that differs from the card surface. We
# patch setTextColor so the custom rule always carries ``background:transparent``.
# ---------------------------------------------------------------------------
def _patch_fluent_label_background():
    from qfluentwidgets.components.widgets.label import FluentLabelBase
    from qfluentwidgets.common.style_sheet import setCustomStyleSheet

    # Patch setTextColor — when a label's colour changes, ensure the custom
    # stylesheet always carries ``background:transparent`` so the card surface
    # shows through (not the view background behind the card).
    _orig = FluentLabelBase.setTextColor

    def _set_text_color(self, light=QColor(0, 0, 0), dark=QColor(255, 255, 255)):
        _orig(self, light, dark)
        light_qss = (
            f"FluentLabelBase{{"
            f"color:{self.lightColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        dark_qss = (
            f"FluentLabelBase{{"
            f"color:{self.darkColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        setCustomStyleSheet(self, light_qss, dark_qss)

    FluentLabelBase.setTextColor = _set_text_color


# ---------------------------------------------------------------------------
# Workaround #2: SwitchButton's text is a plain QLabel (not FluentLabelBase) and
# SwitchButton.setTextColor applies "SwitchButton>QLabel{color:...}" with no
# background. That widget-level rule overrides ancestor transparent rules, so the
# switch's text label picks up a default fill in dark mode. Patch it like #1.
# ---------------------------------------------------------------------------
def _patch_switch_button_label_background():
    from qfluentwidgets.components.widgets.switch_button import SwitchButton
    from qfluentwidgets.common.style_sheet import setCustomStyleSheet

    _orig = SwitchButton.setTextColor

    def _set_text_color(self, light, dark):
        _orig(self, light, dark)
        light_qss = (
            f"SwitchButton>QLabel{{"
            f"color:{self.lightTextColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        dark_qss = (
            f"SwitchButton>QLabel{{"
            f"color:{self.darkTextColor.name(QColor.NameFormat.HexArgb)};"
            f"background-color:transparent}}"
        )
        setCustomStyleSheet(self.label, light_qss, dark_qss)

    SwitchButton.setTextColor = _set_text_color


_patch_fluent_label_background()
_patch_switch_button_label_background()
