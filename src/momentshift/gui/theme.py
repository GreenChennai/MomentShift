"""MomentShift 设计系统（v0.3.2 简化：仅浅色主题）

提供统一的视觉语言：
- 色彩 tokens（窗口背景、组件表面、hover/press、accent、文字）
- ThemedCard — 绘制实心主题感知表面的 CardWidget
- CollapsibleCard — 带动效的折叠卡片
- 共享 UI 构建器（panel/field_row/按钮等）
"""

from __future__ import annotations

import os
from pathlib import Path
from PyQt6.QtCore import QSize, Qt, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QFrame, QSizePolicy

from qfluentwidgets import (
    CardWidget,
    CaptionLabel,
    BodyLabel,
    StrongBodyLabel,
    PrimaryPushButton,
    PushButton,
    TransparentPushButton,
    TransparentToolButton,
)

# =============================================================================
# 设计 tokens（仅浅色）
# =============================================================================

# 窗口 + 内容背景
WINDOW_BG  = QColor("#FFFFFF")
SURFACE    = QColor("#F5F5F5")   # 卡片/组件表面
SURFACE_HOVER = QColor("#EEEEEE")  # hover 态
SURFACE_PRESS = QColor("#EEEEEE")  # press 态

# 文字
TEXT_STRONG     = "#212121"   # 主文字
TEXT_SECONDARY  = "#757575"   # 次要文字
TEXT_PLACEHOLDER = "#9E9E9E"  # 占位符
TEXT_MUTED      = "#BDBDBD"   # 禁用/弱化
TEXT_LINK       = "#2270F4"   # 链接蓝

# 边框
BORDER_COLOR    = "#E0E0E0"
BORDER_HOVER    = "#BDBDBD"

# 品牌色
ACCENT = QColor("#238636")    # GitHub 绿
ACCENT_HEX = "#238636"

# 状态色
COLOR_DANGER    = "#FF7279"
COLOR_SUCCESS   = "#3EB68F"
DANGER_TEXT     = "#B4324B"
SUCCESS_TEXT    = "#3EB68F"

# 几何
RADIUS = 12
SPACING = 12
CARD_MARGIN = 16

# =============================================================================
# SVG 图标路径
# =============================================================================
_RESOURCES = Path(__file__).parent.parent / "resources" / "icons"

def _icon_path(name: str) -> str:
    return os.fspath(_RESOURCES / name)

ICON_EXPAND = _icon_path("\u4e0b\u62c9.svg")
ICON_COLLAPSE = _icon_path("\u6536\u8d77.svg")

# =============================================================================
# 颜色访问器
# =============================================================================

def content_bg() -> QColor:
    return WINDOW_BG

def component_bg() -> QColor:
    return SURFACE

def surface() -> QColor:
    return SURFACE

def surface_hover() -> QColor:
    return SURFACE_HOVER

def surface_pressed() -> QColor:
    return SURFACE_PRESS

def accent_color() -> QColor:
    return ACCENT

def accent_name() -> str:
    return ACCENT_HEX

def text_strong() -> str:
    return TEXT_STRONG

def text_secondary() -> str:
    return TEXT_SECONDARY

def placeholder_text() -> str:
    return TEXT_PLACEHOLDER

def text_disabled() -> str:
    return TEXT_MUTED

def muted_text() -> str:
    return TEXT_MUTED

def sub_text() -> str:
    return TEXT_SECONDARY

def hint_text() -> str:
    return TEXT_PLACEHOLDER

def link_color() -> QColor:
    return QColor(TEXT_LINK)

def border_color() -> str:
    return BORDER_COLOR

def border_hover() -> str:
    return BORDER_HOVER

def danger_color() -> QColor:
    return QColor(COLOR_DANGER)

def danger_text() -> str:
    return DANGER_TEXT

def success_color() -> QColor:
    return QColor(COLOR_SUCCESS)

def success_text() -> str:
    return SUCCESS_TEXT

# =============================================================================
# ThemedCard — 实色表面 + 1px 主题边框
# =============================================================================
class ThemedCard(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(RADIUS)
        self.setStyleSheet(
            "ThemedCard > QWidget { background-color: transparent; }"
            "FluentLabelBase, QLabel { background-color: transparent; }"
        )

    def _normalBackgroundColor(self):
        return component_bg()

    def _hoverBackgroundColor(self):
        return SURFACE_HOVER

    def _pressedBackgroundColor(self):
        return SURFACE_PRESS

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(border_color()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = self.borderRadius
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.drawRoundedRect(rect, r, r)

    def retheme(self):
        self.update()

# =============================================================================
# CollapsibleCard — 折叠卡片（带动效）
# =============================================================================
class CollapsibleCard(ThemedCard):
    _ICON_W = _ICON_H = 20
    _ANIM_DURATION = 250

    def __init__(self, title: str = "", subtitle: str = "",
                 parent=None, collapsed: bool = False):
        super().__init__(parent)
        self._collapsed = collapsed
        self._anim = None
        self._content_height = 0

        self.setStyleSheet(
            "CollapsibleCard > QWidget { background-color: transparent; }"
            "QLabel, FluentLabelBase, BodyLabel, CaptionLabel, StrongBodyLabel,"
            " TitleLabel, SubtitleLabel { background-color: transparent; }"
        )

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        self._bar = QWidget()
        hb = QHBoxLayout(self._bar)
        hb.setContentsMargins(CARD_MARGIN, 10, 6, 10)
        hb.setSpacing(8)

        self.titleLabel = StrongBodyLabel(title)
        hb.addWidget(self.titleLabel, 1)
        hb.addStretch()

        self._toggleBtn = TransparentToolButton(self._toggle_icon(), self)
        self._toggleBtn.setIconSize(QSize(self._ICON_W, self._ICON_H))
        self._toggleBtn.setFixedSize(30, 30)
        self._toggleBtn.clicked.connect(self.toggle)
        hb.addWidget(self._toggleBtn)

        self._outer.addWidget(self._bar)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(CARD_MARGIN, 0, CARD_MARGIN, 14)
        self._body_layout.setSpacing(10)

        self._body.setStyleSheet(
            "QLineEdit { border: 1px solid #d0d0d0; border-radius: 4px;"
            " padding: 4px 8px; background: #ffffff; }"
        )

        self.subtitleLabel = None
        if subtitle:
            self.subtitleLabel = CaptionLabel(subtitle)
            self._body_layout.insertWidget(0, self.subtitleLabel)

        self._outer.addWidget(self._body)

        self._toggle_guard = None
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        if collapsed:
            self._collapse_instant()

    def _collapse_instant(self):
        """初始化时的即时折叠（v0.7.3 Bug2）。

        走 ``_apply_collapsed`` 会启动一段 250ms 的 maximumHeight 动画，
        起点是控件默认的 16777215 —— 于是卡片首次显示时会先整个铺开再收拢，
        表现为「展开 → 收起」的闪烁。构造期直接置位，不跑动画。
        """
        self._body.setMaximumHeight(0)
        self._body.setVisible(False)
        self._toggleBtn.setIcon(self._toggle_icon())

    def _toggle_icon(self) -> QIcon:
        path = ICON_EXPAND if self._collapsed else ICON_COLLAPSE
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _anim_target(self, target_h: int):
        if self._anim is not None:
            # 中途停掉上一段动画，避免它的 finished 回调污染新状态
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        cur = self._body.maximumHeight()
        real_target = target_h
        if target_h <= 0:
            real_target = 0
        elif target_h == 16777215:
            if self._content_height > 0:
                real_target = self._content_height
            else:
                real_target = self._body.sizeHint().height()
                if real_target <= 0:
                    real_target = 200
        self._content_height = real_target if real_target > 0 else self._content_height
        self._body.show()
        self._anim = QPropertyAnimation(self._body, b"maximumHeight", self)
        self._anim.setDuration(self._ANIM_DURATION)
        self._anim.setStartValue(cur)
        self._anim.setEndValue(real_target)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)
        self._anim.start()

    def _on_anim_finished(self):
        """按结束时的实际状态收尾，避免快速连点造成状态错位。"""
        if self._collapsed:
            self._body.setVisible(False)
        else:
            # 解除高度上限，内容后续变化（如切换格式）不会被裁剪
            self._body.setMaximumHeight(16777215)

    def _apply_collapsed(self):
        h = self._body.height()
        if h > 0:
            self._content_height = h
        self._anim_target(0)
        self._toggleBtn.setIcon(self._toggle_icon())

    def _apply_expanded(self):
        self._body.setVisible(True)
        self._anim_target(16777215)
        self._toggleBtn.setIcon(self._toggle_icon())

    @property
    def body(self) -> QVBoxLayout:
        return self._body_layout

    def set_toggle_guard(self, fn) -> None:
        self._toggle_guard = fn

    def toggle(self):
        self.setCollapsed(not self._collapsed)

    def setCollapsed(self, collapsed: bool):
        if self._collapsed == collapsed:
            return
        if collapsed and self._toggle_guard is not None and not self._toggle_guard(self, True):
            return
        self._collapsed = collapsed
        if collapsed:
            self._apply_collapsed()
        else:
            self._apply_expanded()

    def isCollapsed(self) -> bool:
        return self._collapsed

    def refresh_content_height(self):
        """内容动态变化后调用，展开态下解除 maximumHeight 上限。

        v0.7.3 Bug3：展开动画结束时 maximumHeight 停在当时的内容高度；
        之后若再显示更多控件（例如压缩后端切到「自动选择」，三组参数同时出现），
        布局会被这个陈旧上限压扁 —— 表现为所有条目挤成一团。
        """
        self._content_height = 0
        if not self._collapsed:
            self._body.setMaximumHeight(16777215)

# =========================================================================
# 共享 UI 构建器
# =========================================================================

def section_label(text: str, parent=None):
    lbl = CaptionLabel(text, parent)
    lbl.setObjectName("sectionLabel")
    return lbl

def panel(title: str | None = None, subtitle: str | None = None,
          parent=None, radius: int = RADIUS) -> tuple[ThemedCard, QVBoxLayout]:
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

def field_row(label_text: str, control, parent=None, label_width: int = 96) -> QWidget:
    from PyQt6.QtWidgets import QLayout
    row = QWidget(parent)
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

def primary_btn(text: str, icon=None, parent=None) -> PrimaryPushButton:
    if icon is not None:
        return PrimaryPushButton(text, icon=icon, parent=parent)
    return PrimaryPushButton(text, parent=parent)

def ghost_btn(text: str, icon=None, parent=None) -> TransparentPushButton:
    if icon is not None:
        return TransparentPushButton(text, icon=icon, parent=parent)
    return TransparentPushButton(text, parent=parent)

def icon_btn(icon, parent=None) -> TransparentToolButton:
    """图标按钮。v0.7.3 调整2：全局取消鼠标悬停提示，不再接受 tooltip 参数。"""
    return TransparentToolButton(icon, parent)

def scrollbar_qss() -> str:
    handle = "rgba(140, 140, 140, 0.6)"
    hover = "rgba(120, 120, 120, 0.85)"
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

# =============================================================================
# Monkey-patch: 强制 FluentLabelBase + SwitchButton 标签背景透明
# =============================================================================
def _patch_fluent_label_background():
    from qfluentwidgets.components.widgets.label import FluentLabelBase
    from qfluentwidgets.common.style_sheet import setCustomStyleSheet

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


# v0.7.3 调整2：软件已全局取消鼠标悬停提示，原先用于修正 ToolTip 配色的
# _patch_tooltip_style() 补丁随之移除（没有提示就不存在「黑块」问题）。

_patch_fluent_label_background()
_patch_switch_button_label_background()
