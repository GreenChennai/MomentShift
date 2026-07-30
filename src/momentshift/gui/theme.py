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

import os
from pathlib import Path
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QFrame, QSizePolicy

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

# -- Primer-inspired design tokens (single source of truth) -----------------
# Every colour the UI uses is derived from this palette so light/dark stay
# coherent and on-brand. Accessors below resolve the active theme at call time.
_PALETTE_LIGHT = {
    "bg000": "#FFFFFF", "bg100": "#F5F5F5", "bg200": "#EEEEEE",
    "text900": "#212121", "text600": "#757575", "text500": "#9E9E9E",
    "text400": "#BDBDBD",
    "border300": "#E0E0E0", "border400": "#BDBDBD",
    "brand": "#2F98FF", "blue900": "#2270F4",
    "red500": "#FF7279", "red900": "#B4324B", "green500": "#3EB68F",
}
_PALETTE_DARK = {
    "bg000": "#171619", "bg100": "#292929", "bg200": "#424242",
    "surface_base": "#10131A", "surface_raised": "#11122F",
    "text900": "#EFEFEF", "text600": "#C7C7C7", "text500": "#767577",
    "text_primary": "#878A91", "text_secondary": "#74777E", "text400": "#8B8B93",
    "border300": "#575757", "border_default": "#1A1835",
    "brand": "#2F98FF", "blue900": "#4AAEFF", "accent": "#030036",
    "red500": "#E46D70", "red900": "#F6BFBF", "green500": "#27B17D",
    "success": "#A3D4AD",
}


def _dark() -> bool:
    return isDarkTheme()


def _c(key: str) -> QColor:
    pal = _PALETTE_DARK if _dark() else _PALETTE_LIGHT
    return QColor(pal[key])


def _hex(key: str) -> str:
    return _c(key).name()


# Window chrome + content background.
LIGHT_BG = QColor(_PALETTE_LIGHT["bg000"])
DARK_BG = QColor(_PALETTE_DARK["bg000"])

# Uniform component (card) surface — the colour any transparent text/icon inside
# a card resolves to, so the patch behind a label always matches the card.
COMPONENT_LIGHT = QColor(_PALETTE_LIGHT["bg100"])
COMPONENT_DARK = QColor(_PALETTE_DARK["bg100"])
HOVER_LIGHT = QColor(_PALETTE_LIGHT["bg200"])
HOVER_DARK = QColor(_PALETTE_DARK["bg200"])
PRESS_LIGHT = QColor(_PALETTE_LIGHT["bg200"])
PRESS_DARK = QColor(_PALETTE_DARK["bg200"])

# Brand accent (kept vivid on both themes per the supplied palette).
ACCENT_LIGHT = QColor(_PALETTE_LIGHT["brand"])
ACCENT_DARK = QColor(_PALETTE_DARK["brand"])

# Shared geometry tokens.
RADIUS = 12
SPACING = 12
CARD_MARGIN = 16

# -- SVG icon paths (bundled in resources/icons/) -------------------------
_RESOURCES = Path(__file__).parent.parent / "resources" / "icons"

def _icon_path(name: str) -> str:
    return os.fspath(_RESOURCES / name)

ICON_EXPAND = _icon_path("\u4e0b\u62c9.svg")    # 下拉 (down arrow — collapsed state)
ICON_COLLAPSE = _icon_path("\u6536\u8d77.svg")   # 收起 (up arrow — expanded state)


def component_bg() -> QColor:
    return COMPONENT_DARK if _dark() else COMPONENT_LIGHT


def content_bg() -> QColor:
    return DARK_BG if _dark() else LIGHT_BG


def surface() -> QColor:
    """Card/component surface colour (same as ``component_bg``)."""
    return component_bg()


def surface_hover() -> QColor:
    return HOVER_DARK if _dark() else HOVER_LIGHT


def surface_pressed() -> QColor:
    """Colour a pressable surface (e.g. the drop zone) shows while pressed."""
    return PRESS_DARK if _dark() else PRESS_LIGHT


def surface_raised() -> QColor:
    """Raised surfaces (stat pills, popovers) — slightly distinct from a card."""
    if _dark():
        return QColor(_PALETTE_DARK["surface_raised"])
    return COMPONENT_LIGHT


def accent_color() -> QColor:
    return ACCENT_DARK if _dark() else ACCENT_LIGHT


def accent_name() -> str:
    return accent_color().name()


def link_color() -> QColor:
    return QColor(_PALETTE_DARK["blue900"] if _dark() else _PALETTE_LIGHT["blue900"])


# -- text roles -------------------------------------------------------------
def text_strong() -> str:
    return _hex("text900")


def text_secondary() -> str:
    return _hex("text_secondary" if _dark() else "text600")


def placeholder_text() -> str:
    return _hex("text500")


def text_disabled() -> str:
    return _hex("text400")


def sub_text() -> str:
    return text_secondary()


def hint_text() -> str:
    return placeholder_text()


def muted_text() -> str:
    return text_disabled()


# -- borders ----------------------------------------------------------------
def border_color() -> str:
    return _hex("border300")


def border_hover() -> str:
    return _hex("border400")


def divider_color() -> str:
    return _hex("border_default" if _dark() else "border300")


# -- status -----------------------------------------------------------------
def danger_color() -> QColor:
    return QColor(_PALETTE_DARK["red500"] if _dark() else _PALETTE_LIGHT["red500"])


def danger_text() -> str:
    return _hex("red900")


def success_color() -> QColor:
    return QColor(_PALETTE_DARK["green500"] if _dark() else _PALETTE_LIGHT["green500"])


def success_text() -> str:
    return _hex("success" if _dark() else "green500")


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

    - **Title bar** (always visible): title + SVG arrow toggle button.
    - **Body** (collapsible): subtitle (if any) + user content.

    When collapsed only the compact title bar is visible, keeping the card
    height minimal. The subtitle lives in the body so it collapses together
    with the content — it does NOT stay in the title bar (avoiding the visual
    glitch where a separate subtitle line shifts when the body toggles).

    Usage (replaces ``_card()``):
    ::

        card = CollapsibleCard(tr("my.title"), tr("my.sub"))
        card.body.addWidget(...)   # card.body is a QVBoxLayout
        return card, card.body, card.titleLabel
    """

    _ICON_W = _ICON_H = 20

    def __init__(self, title: str = "", subtitle: str = "",
                 parent=None, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = collapsed

        # Force ALL descendant labels to have transparent backgrounds so the
        # card's painted surface (#FBFBFB / #2B2B2B) shows through cleanly.
        # The ``> QWidget`` rule covers intermediate containers; explicit
        # label-type rules catch deeper descendants regardless of depth.
        self.setStyleSheet(
            "CollapsibleCard > QWidget { background-color: transparent; }"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )

        # ---- outer layout ------------------------------------------------
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        # ---- title bar (always visible, compact) -------------------------
        self._bar = QWidget()
        hb = QHBoxLayout(self._bar)
        hb.setContentsMargins(CARD_MARGIN, 10, 6, 10)
        hb.setSpacing(8)

        self.titleLabel = StrongBodyLabel(title)
        hb.addWidget(self.titleLabel, 1)
        hb.addStretch()

        # arrow toggle button using SVG icons
        self._toggleBtn = TransparentToolButton(self._toggle_icon(), self)
        self._toggleBtn.setIconSize(QSize(self._ICON_W, self._ICON_H))
        self._toggleBtn.setFixedSize(30, 30)
        self._toggleBtn.clicked.connect(self.toggle)
        self._toggleBtn.setToolTip("")
        hb.addWidget(self._toggleBtn)

        self._outer.addWidget(self._bar)

        # ---- body (collapsible, hides when collapsed) --------------------
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(CARD_MARGIN, 0, CARD_MARGIN, 14)
        self._body_layout.setSpacing(10)

        # Preserve QLineEdit borders — the parent transparent-background rules
        # can make input boxes appear borderless on some themes.
        self._body.setStyleSheet(
            "QLineEdit { border: 1px solid #d0d0d0; border-radius: 4px;"
            " padding: 4px 8px; background: #ffffff; }"
        )

        # Optional subtitle lives inside the body so it hides on collapse.
        self.subtitleLabel = None
        if subtitle:
            self.subtitleLabel = CaptionLabel(subtitle)
            self._body_layout.insertWidget(0, self.subtitleLabel)

        self._outer.addWidget(self._body)

        # Guard consulted before collapsing: returns True to allow, False to
        # refuse (e.g. when this is the last expanded card in its group).
        self._toggle_guard = None
        # Cards never stretch to fill the window — they keep their content
        # height and stay top-aligned (collapsed => title bar only).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        if collapsed:
            self._apply_collapsed()

    # -- helpers ------------------------------------------------------------

    def _toggle_icon(self) -> QIcon:
        """Return the appropriate SVG icon for the current state."""
        path = ICON_EXPAND if self._collapsed else ICON_COLLAPSE
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _apply_collapsed(self):
        """Apply the collapsed visual state without re-emitting the toggle signal."""
        self._body.setVisible(False)
        # Setting a fixed height of 0 ensures the layout truly shrinks the
        # card to title-bar height, avoiding the "collapsed but still tall" bug.
        self._body.setFixedHeight(0)
        self._toggleBtn.setIcon(self._toggle_icon())

    def _apply_expanded(self):
        """Apply the expanded visual state."""
        self._body.setFixedHeight(self._body.sizeHint().height() if False else 16777215)
        # ^ use QWIDGETSIZE_MAX to unrestrict height — qt constant is 16777215
        self._body.setMinimumHeight(0)
        self._body.setMaximumHeight(16777215)
        self._body.setVisible(True)
        self._toggleBtn.setIcon(self._toggle_icon())

    # -- public API ---------------------------------------------------------

    @property
    def body(self) -> QVBoxLayout:
        """The body layout where card content should be added."""
        return self._body_layout

    def set_toggle_guard(self, fn) -> None:
        """Register a guard ``fn(card, want_collapse: bool) -> bool``.

        ``fn`` is called whenever the user (or code) requests a collapse. Return
        ``False`` to refuse the collapse (the card stays expanded). The user's
        own ``toggle`` button then appears to "do nothing".
        """
        self._toggle_guard = fn

    def toggle(self):
        """Toggle the collapsed state (subject to the toggle guard)."""
        self.setCollapsed(not self._collapsed)

    def setCollapsed(self, collapsed: bool):
        """Programmatically expand or collapse the body.

        A collapse request is refused (and silently ignored) when the registered
        ``toggle_guard`` returns ``False`` — used to keep at least one card open.
        """
        if self._collapsed == collapsed:
            return
        if collapsed and self._toggle_guard is not None and not self._toggle_guard(self, True):
            # Refused: this is the last expanded card in its group. Do nothing
            # so the toggle button appears unresponsive, as required.
            return
        self._collapsed = collapsed
        if collapsed:
            self._apply_collapsed()
        else:
            self._apply_expanded()

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
    # Prevent the row container from painting the view's background (#F4F4F4)
    # through the card surface — the root cause of repeated "grey block" reports.
    row.setStyleSheet("background: transparent;")
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
